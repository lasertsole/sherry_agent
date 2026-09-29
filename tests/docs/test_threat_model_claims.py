"""The threat model must stay faithful to the code it describes.

A threat model rots in a specific way: it keeps naming files that moved, symbols
that were renamed, and protections that no longer exist, while still reading as
authoritative. These four contracts catch that mechanically:

1. every path-shaped code span in the document exists on disk;
2. the symbols it tells callers to import are importable;
3. the documented scope names and scan window match the module's constants;
4. the protections its boundary table credits with enforcement are real files.

They are deliberately about *facts*, not prose — the four translations stay in
step through the README parity gate, which is a different check.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = [pytest.mark.unit]

_DOC = pathlib.Path("docs/threat-model/README.md")


def _doc_text() -> str:
    assert _DOC.exists(), "the threat model document moved"
    return _DOC.read_text(encoding="utf-8")


def test_every_python_path_in_the_document_exists():
    doc = _doc_text()

    named = sorted(set(re.findall(r"`([A-Za-z0-9_./-]+\.py)`", doc)))

    assert named, "the document stopped naming any file path"
    missing = [path for path in named if not pathlib.Path(path).exists()]
    assert missing == [], f"the document names paths that do not exist: {missing}"


def test_the_documented_imports_are_importable():
    from agent.security.threat_patterns import (
        first_threat_message,
        scan_for_threats,
    )

    assert callable(scan_for_threats)
    assert callable(first_threat_message)


def test_the_documented_scopes_and_window_match_the_code():
    from agent.security.threat_patterns import _MAX_SCAN_CHARS, SCAN_SCOPES

    doc = _doc_text()

    assert set(SCAN_SCOPES) == {"all", "context", "strict"}
    for scope in SCAN_SCOPES:
        assert f"`{scope}`" in doc, f"the document stopped documenting scope {scope!r}"

    assert _MAX_SCAN_CHARS == 65_536
    assert "65,536" in doc, "the documented scan window drifted from the code"


def test_the_protections_it_credits_with_enforcement_exist():
    doc = _doc_text()

    # The boundary table hands these two the enforcement role by name.
    assert "PathGuard" in doc
    assert pathlib.Path("agent/middlewares/path_guard/core.py").exists()
    assert "SubagentCompletionDrain" in doc
    assert list(pathlib.Path("agent/middlewares").rglob("*completion_drain*"))


def test_the_documented_fence_matches_the_wrapper():
    """The fence section names a tag, a switch and a tool set — all three are real."""
    from agent.security.untrusted_wrapper import UNTRUSTED_TOOL_NAMES, WRAPPER_TAG
    from config.features import UNTRUSTED_OUTPUT

    doc = _doc_text()

    assert f"<{WRAPPER_TAG}" in doc
    assert "UNTRUSTED_OUTPUT" in doc and "enabled" in UNTRUSTED_OUTPUT
    # Both web-search shapes are documented because only one of them ships in a
    # given deployment — and only they decide whether web content is fenced.
    assert {"web_search", "tavily_search", "message_search"} <= UNTRUSTED_TOOL_NAMES
    for name in sorted(UNTRUSTED_TOOL_NAMES):
        assert f"`{name}`" in doc, f"the fence section stopped naming {name}"
