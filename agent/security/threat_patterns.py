r"""Prompt-injection / promptware / exfiltration pattern scanner.

Three scope tiers, each a superset of the previous one — callers pick the
strictness their surface warrants:

``all``
    Classic injection ("ignore previous instructions", role hijack, system-prompt
    extraction) plus obvious key exfiltration. Narrow enough to run over every
    tool result without drowning it in false positives.
``context``
    ``all`` plus C2/promptware shapes (node registration, heartbeat/beacon,
    tasking pulls, known C2 framework names) and instructions that rewrite agent
    instruction files. For tool results and context files.
``strict``
    ``context`` plus SSH backdoors, shell-rc persistence, hardcoded provider
    secrets and invisible Unicode. For memory writes and skill installs, where
    the text persists and will be re-read into every later prompt.

Detection is advisory-but-cheap on purpose: it reports finding IDs, never
mutates content, and never raises on odd input.

Usage::

    from agent.security.threat_patterns import scan_for_threats, first_threat_message

    findings = scan_for_threats(page_text, scope="context")
    if findings:
        block_reason = first_threat_message(page_text, scope="context")

Design notes:

* **Bounded filler** between key tokens (``(?:\s+\w+){0,8}``) matches the
  obfuscated verb forms (an extra adjective dropped between the verb and its
  object) without allowing unbounded backtracking. Note the leading ``\s+``:
  the filler comes *after* the verb, and the text between them starts with
  whitespace — a filler without it cannot match the canonical phrasing at all.
* **Every quantification is bounded.** The first draft of the HTML-comment rule
  used ``.*`` with ``re.DOTALL`` over attacker-controlled text, which is both a
  backtracking hazard and a 64 KB scan of mostly-irrelevant text; it now walks a
  bounded window around the comment markers.
* **The invisible-Unicode class excludes U+200E/U+200F.** Those are the standard
  LTR/RTL marks used by Arabic and Hebrew text, so flagging them would fire on
  legitimate content; the remaining code points (zero-width space/joiner family,
  line/paragraph separators, bidi overrides, BOM) have no textual purpose.
"""

from __future__ import annotations

import re

__all__ = ["SCAN_SCOPES", "first_threat_message", "scan_for_threats"]

# Bounded filler between key tokens: catches obfuscated verb forms without
# allowing unbounded backtracking. The leading \s+ matters — the filler sits
# BETWEEN two tokens, so it must consume the space that precedes each word.
_FILLER = r"(?:\s+\w+){0,8}"

# A finding is a (pattern, id) pair; the id is what callers act on and log.
_Finding = tuple[re.Pattern[str], str]

_PATTERNS_ALL: list[_Finding] = [
    # Classic prompt injection
    (
        re.compile(
            rf"ignore{_FILLER}\s+(all|any|previous|above|prior|earlier|system)\s+instructions?",
            re.IGNORECASE,
        ),
        "injection_ignore_instructions",
    ),
    (
        re.compile(
            rf"disregard{_FILLER}\s+(all|any|previous|above|prior)\s+instructions?",
            re.IGNORECASE,
        ),
        "injection_disregard_instructions",
    ),
    (re.compile(r"you\s+are\s+now\s+a\s+", re.IGNORECASE), "injection_role_hijack"),
    (re.compile(r"pretend\s+you\s+are\s+", re.IGNORECASE), "injection_role_hijack"),
    (
        re.compile(r"system\s+prompt\s*:", re.IGNORECASE),
        "injection_system_prompt_extraction",
    ),
    # Exfiltration
    (re.compile(r"curl\s+.{0,200}\$\w*(KEY|TOKEN|SECRET)", re.IGNORECASE), "exfil_curl_key"),
    (re.compile(r"wget\s+.{0,200}\$\w*(KEY|TOKEN|SECRET)", re.IGNORECASE), "exfil_wget_token"),
    # Hidden content: a bounded window around the comment markers (see module docstring).
    (
        re.compile(
            r"<!--.{0,200}?(?:ignore|system|instruction|prompt).{0,60}?-->",
            re.IGNORECASE | re.DOTALL,
        ),
        "injection_html_comment",
    ),
]

_PATTERNS_CONTEXT: list[_Finding] = _PATTERNS_ALL + [
    # C2 / Brainworm-style promptware
    (re.compile(r"register\s+as\s+a\s+node", re.IGNORECASE), "c2_register_node"),
    (re.compile(r"(heartbeat|beacon)\s+to\s+", re.IGNORECASE), "c2_heartbeat"),
    (re.compile(r"pull\s+tasking", re.IGNORECASE), "c2_pull_tasking"),
    (
        re.compile(r"cobalt\s+strike|sliver|havoc|mythic|brainworm", re.IGNORECASE),
        "c2_known_framework",
    ),
    # Instructions that rewrite the agent's own instruction files
    (
        re.compile(r"(?:modify|overwrite|replace)\s+.{0,60}(?:AGENTS|CLAUDE)\.md", re.IGNORECASE),
        "injection_modify_agents_md",
    ),
]

_PATTERNS_STRICT: list[_Finding] = _PATTERNS_CONTEXT + [
    # SSH backdoors
    (re.compile(r"authorized_keys|ssh\s+.{0,40}backdoor", re.IGNORECASE), "ssh_backdoor"),
    # Persistence
    (
        re.compile(r"crontab\s+-e|\.bashrc|\.zshrc|\.profile\b", re.IGNORECASE),
        "persistence_shell_rc",
    ),
    # Hardcoded provider secrets
    (
        re.compile(r"(?:AKIA|sk-|ghp_|gho_|xox[baprs]-|AIza)[A-Za-z0-9]{16,}"),
        "hardcoded_secret",
    ),
    # Invisible Unicode (U+200E/U+200F excluded — see the module docstring)
    (re.compile(r"[\u200b-\u200d\u2028-\u202e\ufeff]"), "invisible_unicode"),
]

#: Scope name → its pattern tier. ``SCAN_SCOPES`` is the public surface a
#: caller may iterate (the later wiring stages read it rather than hardcoding
#: the three names).
SCAN_SCOPES: dict[str, list[_Finding]] = {
    "all": _PATTERNS_ALL,
    "context": _PATTERNS_CONTEXT,
    "strict": _PATTERNS_STRICT,
}

#: Scans are bounded: a finding past this offset is not reported, and the
#: truncation is deliberate — the callers are tool outputs, whose tail is the
#: least likely place for an injected instruction to survive re-serialization.
_MAX_SCAN_CHARS = 65_536


def scan_for_threats(content: str, scope: str = "all") -> list[str]:
    """Return the finding IDs matching ``content``; an empty list means clean.

    An unknown scope falls back to ``"all"`` (never raises): the scanner sits on
    tool paths where a typo must not take the tool down.
    """
    if not content:
        return []
    patterns = SCAN_SCOPES.get(scope, _PATTERNS_ALL)
    text = content[:_MAX_SCAN_CHARS]
    return [name for pattern, name in patterns if pattern.search(text)]


def first_threat_message(content: str, scope: str = "all") -> str | None:
    """Return a human-readable message for the first threat found, or ``None``.

    The message carries the finding ID only — never the matched text, which is
    attacker-controlled and would otherwise be echoed back into a prompt or log.
    """
    findings = scan_for_threats(content, scope)
    if not findings:
        return None
    return f"Potential security threat detected: {findings[0]}"
