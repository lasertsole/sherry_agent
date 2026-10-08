"""Secret/credential redaction engine for tool output, logs, and diagnostics.

Regex families, applied in order (cheapest and most specific first):

* vendor API key prefixes (``sk-``, ``ghp_``, ``AKIA``, ``xox*``, ``AIza``, ``hf_``, …);
* secret-named assignments (``*_API_KEY=…``, ``TOKEN: …``) whose value is long
  enough to be a real credential;
* ``Authorization`` / ``X-API-Key`` style headers;
* JWTs and PEM private-key blocks;
* URL credentials (``scheme://user:pass@host``);
* JSON ``"secret": "…"`` fields;
* config-file ``key = value`` / ``key: value`` entries.

Design notes:

* **A cheap pre-filter runs first.** Every log record and every tool result
  passes through this engine, so a single combined regex decides whether the
  text is worth scanning at all; ordinary prose costs one failed match.
* **Redaction is idempotent.** The sentinel (``«redacted»``) matches no family,
  so running the engine twice changes nothing — callers may redact defensively
  at several boundaries.
* **The switch is read once, at import.** An LLM that talks its way into writing
  ``export REDACT=false`` (or into editing ``.env``) cannot turn redaction off
  mid-session; only a restart with the variable set can. ``force=True``
  bypasses the switch for callers that must redact regardless (logs always do).
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

__all__ = ["REDACTED", "redact_sensitive_text", "truncate_for_redaction"]

#: What replaces a secret. Deliberately not a realistic-looking value, so a
#: redacted payload can never be mistaken for a rotated credential.
REDACTED = "«redacted»"

#: Master switch, snapshotted at import (see the module docstring).
_ENABLED = os.getenv("SHERRY_REDACT", "true").strip().lower() not in {"0", "false", "no", "off"}

_VENDOR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-«redacted»"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh«redacted»"),
    (re.compile(r"\bgitlab_pat_[A-Za-z0-9_-]{20,}\b"), "gitlab_pat_«redacted»"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "AKIA«redacted»"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "xox«redacted»"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"), "AIza«redacted»"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"), "sk_«redacted»"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "hf_«redacted»"),
]

#: ``NAME=value`` / ``NAME: value`` where NAME says "this is a credential".
#: The name must carry a secret-ish word and the value must be long enough that
#: it cannot be ordinary prose.
#:
#: Known trade-off, accepted deliberately: source code has plenty of
#: ``api_key=api_key`` references (``TavilySearch(tavily_api_key=tavily_api_key)``),
#: and this rule rewrites them. Requiring the value to *look* like a credential
#: would fix that and start missing all-lowercase passwords, so the engine errs
#: towards over-redaction and the corpus test below pins the surfaces where
#: rewriting would actually hurt (docs, prompts).
_SECRET_NAME = (
    r"[A-Za-z0-9_.-]*(?:API[_-]?KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.-]*"
)
_AUTH_HEADER_RE = re.compile(
    r"(?P<header>\b(?:Authorization|X-API-Key|Api-Key|api-key)\b\s*[:=]\s*)"
    r"(?:(?P<scheme>Bearer|Basic|Digest|Token)\s+)?"
    r"(?P<value>[A-Za-z0-9._~+/=-]{16,})",
    re.IGNORECASE,
)

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

# Bounded: a PEM block is a few KB at most, and DOTALL ``.*?`` over an unbounded
# document is a needless backtracking surface. The bound is written WITHOUT an
# underscore (``16000``, not ``16_000``): a digit separator makes the braces an
# invalid quantifier, which ``re`` then treats as literal text — the rule silently
# matched nothing until a PEM sample in the test corpus caught it.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]{0,16000}?-----END [A-Z ]{0,40}PRIVATE KEY-----"
)

#: Any URL carrying userinfo. Whether that userinfo holds a *password* is
#: decided by the predicate below, which peels percent-encoding first — the plain
#: ``user:pass@`` shape is just the depth-0 case of the same rule.
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]{1,20}://)(?P<userinfo>[^/\s@]{1,200})@",
    re.IGNORECASE,
)

#: How deep to peel percent-encoding before giving up (openclaw uses 8).
_URL_DECODE_DEPTH = 8

#: Secret-bearing query parameters. The value class is permissive (a signed URL's
#: value can be a long base64 blob or a JWT) but never spans whitespace.
_URL_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:token|access[_-]?token|api[_-]?key|apikey|key|secret|password|passwd"
    r"|signature|sig|x-amz-signature|x-amz-credential|x-goog-signature|auth)=)"
    r"(?P<value>[^&#\s]{4,})",
    re.IGNORECASE,
)

_JSON_SECRET_RE = re.compile(
    r'(?P<key>"[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|secret|token|password|passwd|credential'
    r'|access[_-]?token|refresh[_-]?token|client[_-]?secret)[A-Za-z0-9_.-]*"\s*:\s*")'
    r'(?P<value>[^"]{8,})(?P<close>")',
    re.IGNORECASE,
)

#: A value is quoted (may contain spaces) or bare (may not); either way it must
#: be long enough that an ordinary word cannot qualify.
#:
#: The bare class stops at ``&``, ``,`` and ``;`` — the characters that end a
#: value in the shapes this rule sees (a query string, a CSV line, an INI list).
#: Without that, the rule re-matched a query string an earlier family had already
#: rewritten (``token=«redacted»&page=2``) and swallowed everything after the
#: token. An unquoted value that genuinely contains ``&`` is instead left to the
#: quoted branch (``password="a&b12345"`` still redacts).
_VALUE = r"""(?:"(?P<dquoted>[^"\n]{8,})"|'(?P<squoted>[^'\n]{8,})'|(?P<bare>[^\s'"&,;]{8,}))"""
_QUOTED_GROUPS = ("dquoted", "squoted")

#: One rule for every config shape — ``.env``, INI, YAML, TOML, dotted keys — with
#: the NAME carrying a secret-ish word (``CURATOR_API_KEY``, ``X-Api-Key``, …).
#: Matching is not anchored to the line start: a ``password: …`` nested in YAML or
#: sitting after other content is just as much a secret.
_CONFIG_SECRET_RE = re.compile(
    rf"(?P<prefix>\b{_SECRET_NAME}\s*[:=]\s*)(?P<value>{_VALUE})",
    re.IGNORECASE,
)

#: One failed match here means the text holds nothing any family looks for.
#:
#: Every vendor family must contribute a marker, or that family silently stops
#: running — the pre-filter skipped ``sk_live_…`` until the stripe case in
#: tests/agent/security/test_redact.py failed. The per-family tests are what keep
#: this list honest; extend both together.
_CANDIDATE_RE = re.compile(
    r"sk[-_]|gh[pousr]_|gitlab_pat_|glpat|AKIA|xox|AIza|hf_|eyJ|-----BEGIN|authorization"
    r"|api[-_]?key|apikey|secret|token|password|passwd|credential|://",
    re.IGNORECASE,
)


def truncate_for_redaction(text: str, limit: int = 200_000) -> str:
    """Return ``text`` unchanged if short enough, else its head plus a marker.

    Only for callers that may receive unbounded input (a giant tool result): the
    engine itself never truncates, because dropping the tail of a payload would
    be silent data loss. Callers that must preserve the text (file reads) should
    not use this.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} more characters not scanned]"


def redact_sensitive_text(text: str, *, force: bool = False) -> str:
    """Apply every redaction family to ``text`` and return the scrubbed copy.

    ``force=True`` redacts even when the ``SHERRY_REDACT`` switch is off — the
    log pipeline uses it, because a log file outlives the session and is read by
    people who never saw the secret.
    """
    if not text or (not force and not _ENABLED):
        return text
    if not _CANDIDATE_RE.search(text):
        return text

    for pattern, replacement in _VENDOR_PATTERNS:
        text = pattern.sub(replacement, text)

    text = _AUTH_HEADER_RE.sub(
        lambda match: (
            f"{match.group('header')}{match.group('scheme') + ' ' if match.group('scheme') else ''}{REDACTED}"
        ),
        text,
    )
    text = _JWT_RE.sub("«redacted:jwt»", text)
    text = _PEM_RE.sub("«redacted:pem»", text)
    # The user name goes too: diagnostics rarely need it, and keeping it would
    # still disclose which account a leaked URL belonged to.
    # The user name goes too: diagnostics rarely need it, and keeping it would
    # still disclose which account a leaked URL belonged to.
    text = _URL_USERINFO_RE.sub(
        lambda match: (
            f"{match.group('scheme')}{REDACTED}@"
            if _userinfo_holds_a_password(match.group("userinfo"))
            else match.group(0)
        ),
        text,
    )
    text = _JSON_SECRET_RE.sub(
        lambda match: f"{match.group('key')}{REDACTED}{match.group('close')}", text
    )
    text = _CONFIG_SECRET_RE.sub(_redact_value, text)
    text = _URL_QUERY_SECRET_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    return text


def _userinfo_holds_a_password(userinfo: str, *, depth: int = _URL_DECODE_DEPTH) -> bool:
    """Whether a URL's userinfo is ``user:password``, once encoding is peeled.

    ``user:pass`` -> True directly; ``user:pa%3Ass`` and ``user:pa%253Ass``
    -> True after one or two rounds. Decoding happens on the captured fragment
    only — never on the surrounding text, which redaction must leave
    byte-identical.
    """
    candidate = userinfo
    for _ in range(max(1, depth)):
        if ":" in candidate:
            return True
        peeled = unquote(candidate)
        if peeled == candidate:
            break
        candidate = peeled
    return ":" in candidate


def _redact_value(match: re.Match[str]) -> str:
    """Rebuild ``NAME=<value>`` with the value replaced, quoting preserved.

    A quoted value keeps its quotes (and may legitimately contain spaces); a bare
    value is replaced in place.
    """
    prefix = match.group("prefix")
    for group in _QUOTED_GROUPS:
        if match.group(group) is not None:
            quote = '"' if group == "dquoted" else "'"
            return f"{prefix}{quote}{REDACTED}{quote}"
    return f"{prefix}{REDACTED}"
