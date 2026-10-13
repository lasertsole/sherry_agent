"""Behavioural tests for the secret-redaction engine.

Two halves matter equally: that every credential family is caught, and that
ordinary text is left alone. The corpus below is real content — this repository's
own docs and code — because a redactor that rewrites prose is worse than none:
it corrupts the very output an operator is reading to diagnose a failure.

Also pinned here: the PEM bound trap. A quantifier written with a digit
separator (``{0,16_000}``) is not a quantifier to ``re`` — it parses as literal
text — so the private-key rule silently matched nothing until a real PEM sample
was put in front of it.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

from agent.security.redact import (
    REDACTED,
    redact_sensitive_text,
    truncate_for_redaction,
)

pytestmark = [pytest.mark.unit]


# Vendor-shaped samples are assembled at runtime: the literal strings would match
# a secret-scanning rule (GitHub's push protection blocks `sk_live_…` by pattern,
# entropy is irrelevant), and a test fixture must not be a push blocker.
_OPENAI = "sk-" + "x" * 32
_GH = "ghp_" + "x" * 36
_GH_SHORT = "ghp_" + "x" * 20
_GITLAB = "gitlab_pat_" + "x" * 20
_AWS = "AKIA" + "X" * 16
_SLACK = "xoxb-" + "x" * 20
_GOOGLE = "AIza" + "x" * 33
_STRIPE = "sk_" + "live_" + "x" * 24
_HF = "hf_" + "x" * 30

# ---------------------------------------------------------------------------
# Every family fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("openai", f"using {_OPENAI} now"),
        ("github", f"token {_GH}"),
        ("gitlab", f"gitlab token {_GITLAB}"),
        ("aws", f"creds {_AWS}"),
        ("slack", f"webhook {_SLACK}"),
        ("google", f"maps key {_GOOGLE}"),
        ("stripe", f"billing {_STRIPE}"),
        ("huggingface", f"hf access {_HF}"),
    ],
)
def test_vendor_prefixes_are_redacted(label, text):
    assert REDACTED in redact_sensitive_text(text), label


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("env-var", "MAIN_LLM_API_KEY=abcdefghijklmnop"),
        ("env-colon", "DB_PASSWORD: hunter2hunter2"),
        ("config-ini", "password = hunter2hunter2"),
        ("config-yaml", "  api_key: abcdefghijklmnop"),
        ("toml", 'client_secret = "abcdefghijklmnop"'),
        ("dotted", "auth.token: abcdefghijklmnop"),
    ],
)
def test_secret_named_assignments_are_redacted(label, text):
    scrubbed = redact_sensitive_text(text)
    assert REDACTED in scrubbed, label
    assert "abcdefghijklmnop" not in scrubbed and "hunter2hunter2" not in scrubbed


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("bearer", "Authorization: Bearer abcdefghijklmnop.qrstuvwx"),
        ("basic", "authorization: Basic YWxpY2U6cGFzc3dvcmQ="),
        ("api-key-header", "X-API-Key: abcdefghijklmnop1234"),
        ("digest", "Authorization: Digest abcdefghijklmnop1234"),
    ],
)
def test_authorization_headers_are_redacted(label, text):
    scrubbed = redact_sensitive_text(text)
    assert REDACTED in scrubbed, label
    assert "abcdefghijklmnop" not in scrubbed


def test_jwts_are_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"

    scrubbed = redact_sensitive_text(f"session {jwt}")

    assert jwt not in scrubbed and "redacted:jwt" in scrubbed


@pytest.mark.parametrize(
    "label",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_private_key_blocks_are_redacted(label):
    """Regression: the bound must be a real quantifier, not literal text."""
    body = f"{label}\nMIIabcde\n-----END {label.split('BEGIN ')[1]}"

    scrubbed = redact_sensitive_text(f"here it is:\n{body}")

    assert "redacted:pem" in scrubbed
    assert "MIIabcde" not in scrubbed


def test_url_credentials_are_redacted_but_the_host_stays_readable():
    scrubbed = redact_sensitive_text("clone https://alice:s3cr3tp4ss@github.com/org/repo.git")

    assert "s3cr3tp4ss" not in scrubbed and "alice" not in scrubbed
    assert "github.com/org/repo.git" in scrubbed, "diagnostics need the host and path"


def test_json_secret_fields_are_redacted():
    scrubbed = redact_sensitive_text('{"api_key": "abcdefghijklmnop", "model": "glm-4.6"}')

    assert "abcdefghijklmnop" not in scrubbed
    assert '"model": "glm-4.6"' in scrubbed


def test_redaction_is_idempotent():
    """The sentinel matches no family, so a second pass is a no-op."""
    text = "key sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx and password: hunter2hunter2"

    once = redact_sensitive_text(text)

    assert redact_sensitive_text(once) == once


# ---------------------------------------------------------------------------
# It leaves ordinary text alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "The token budget was refreshed during the turn.",
        "Authorization failed for the request.",
        "password: short",  # below the value floor
        "see docs/sandbox/README.md for the sandbox threat model",
        "secret: nothing to hide here",
        "The system prompt builder assembles persona and memory blocks.",
    ],
)
def test_ordinary_prose_is_untouched(text):
    assert redact_sensitive_text(text) == text


def test_the_projects_own_docs_are_not_rewritten():
    """A redactor that rewrites docs would corrupt the text operators read.

    Source files are deliberately NOT in this corpus: the assignment rule
    rewrites ``api_key=api_key`` references in code by design (see the rule's
    comment) — over-redacting is the safer error, and code meets the engine only
    when someone reads it as tool output.
    """
    rewritten: list[str] = []
    for name in [
        "AGENTS.md",
        "README.md",
        "pyproject.toml",
        "docs/threat-model/README.md",
        "docs/sandbox/README.md",
        "docs/summarization/README.md",
    ]:
        path = pathlib.Path(name)
        assert path.exists(), f"corpus file moved: {name}"
        text = path.read_text(encoding="utf-8")
        # Compare before/after rather than looking for the sentinel: the threat
        # model DOCUMENTS the sentinel as a log signal, so its presence is prose,
        # not evidence of a rewrite.
        if redact_sensitive_text(text) != text:
            rewritten.append(name)
    assert rewritten == [], rewritten


# ---------------------------------------------------------------------------
# The switch and the input guard
# ---------------------------------------------------------------------------


def test_the_env_switch_is_snapshotted_at_import(monkeypatch):
    """An LLM that writes ``export REDACT=false`` must not disarm a live session."""
    import agent.security.redact as redact_module

    monkeypatch.setenv("SHERRY_REDACT", "false")
    try:
        reloaded = importlib.reload(redact_module)
        assert reloaded._ENABLED is False
        text = "key sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        assert reloaded.redact_sensitive_text(text) == text
        # …but a caller that must redact (the log pipeline) still can.
        assert REDACTED in reloaded.redact_sensitive_text(text, force=True)
    finally:
        monkeypatch.undo()
        importlib.reload(redact_module)


def test_truncating_helper_only_bounds_what_it_scans():
    text = "x" * 50

    assert truncate_for_redaction(text, limit=100) == text
    bounded = truncate_for_redaction(text, limit=10)
    assert bounded.startswith("x" * 10) and "40 more characters not scanned" in bounded


# ---------------------------------------------------------------------------
# URL credentials, including the encoded ones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("plain", "https://alice:s3cr3tp4ss@example.com/x"),
        ("encoded-colon", "https://alice:s3cr3t%3Ap4ss@example.com/x"),
        ("double-encoded", "https://alice:pa%253A55@example.com/x"),
        ("postgres", "postgres://dbuser:hunter2hunter2@db.internal:5432/app"),
        ("redis-auth", "redis://:s3cretvalue@cache:6379/0"),
        ("mongodb", "mongodb+srv://u:hunter2hunter2@cluster.example/db"),
    ],
)
def test_url_credentials_are_redacted_at_any_encoding_depth(label, text):
    scrubbed = redact_sensitive_text(text)

    assert REDACTED in scrubbed, label
    for leaked in ("s3cr3tp4ss", "s3cr3t", "p4ss", "hunter2hunter2", "s3cretvalue"):
        assert leaked not in scrubbed, (label, leaked)


def test_a_url_without_a_password_is_left_alone():
    """``user@host`` is an identity, not a credential."""
    text = "git clone https://alice@example.com/org/repo.git"

    assert redact_sensitive_text(text) == text


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("token", "https://api.example.com/v1?token=abcdefghijklmnop&page=2"),
        ("api-key", "https://api.example.com/v1?api_key=abcdefghijklmnop"),
        ("aws-sig", "https://s3.example.com/o?X-Amz-Signature=abcdef123456&X-Amz-Date=20260930"),
        ("google-sig", "https://storage.example.com/o?X-Goog-Signature=abcdef123456"),
        ("password-param", "https://db.example.com/connect?password=hunter2hunter2"),
    ],
)
def test_secret_query_parameters_are_redacted_without_touching_the_rest(label, text):
    scrubbed = redact_sensitive_text(text)

    assert "abcdef" not in scrubbed and "hunter2hunter2" not in scrubbed, label
    # The host stays readable; a non-secret parameter in the same URL survives.
    assert "example.com" in scrubbed, label
    if label == "aws-sig":
        assert "X-Amz-Date=20260930" in scrubbed, "a non-secret parameter was eaten"


def test_the_rest_of_a_query_string_survives():
    scrubbed = redact_sensitive_text("https://api.example.com/v1?token=abcdefghijklmnop&page=2")

    assert "page=2" in scrubbed and REDACTED in scrubbed


# ---------------------------------------------------------------------------
# Config-file shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("dotenv", "MAIN_LLM_API_KEY=abcdefghijklmnop"),
        ("dotenv-quoted", 'DB_PASSWORD="hunter2hunter2"'),
        ("ini", "password = hunter2hunter2"),
        ("yaml-nested", "database:\n  password: hunter2hunter2"),
        ("yaml-quoted-spaces", 'password: "correct horse battery staple"'),
        ("toml", '[auth]\ntoken = "abcdefghijklmnop"'),
        ("json", '{"client_secret": "abcdefghijklmnop"}'),
        ("json-prefixed-key", '"CURATOR_API_KEY": "abcdefghijklmnop"'),
        ("jsonc", '"AUXILIARY_LLM_API_KEY": "abcdefghijklmnop",'),
        ("dotted", "auth.token: abcdefghijklmnop"),
        ("single-quoted", "token: 'hunter2hunter2'"),
    ],
)
def test_every_config_file_shape_is_covered(label, text):
    scrubbed = redact_sensitive_text(text)

    assert REDACTED in scrubbed, label
    for leaked in ("abcdefghijklmnop", "hunter2hunter2", "correct horse battery staple"):
        assert leaked not in scrubbed, (label, leaked)


def test_quoting_is_preserved_around_the_sentinel():
    assert redact_sensitive_text('password: "x1234567x"') == 'password: "«redacted»"'
    assert redact_sensitive_text("password: 'x1234567x'") == "password: '«redacted»'"
    assert redact_sensitive_text("password: x1234567x") == "password: «redacted»"
