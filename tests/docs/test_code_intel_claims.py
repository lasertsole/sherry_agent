"""The code-intel document must stay faithful to the code it describes.

This page rots the same way a threat model does: it names paths, config keys and
tool surfaces that move. One such statement had already gone stale (it claimed
`search_files` was *absent* from `_MAIN_TOOLS_BUILDERS` and from the librarian's
tool face, both of which the ripgrep wiring changed), so the checks below are the
ones that would have caught it:

1. every path-shaped code span in the document exists on disk;
2. the config objects the document names expose the keys it credits;
3. the tool-surface claims match the builders and the role definitions.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = [pytest.mark.unit]

_DOC = pathlib.Path("docs/code-intel/README.md")


def _doc_text() -> str:
    assert _DOC.exists(), "the code-intel document moved"
    return _DOC.read_text(encoding="utf-8")


def test_every_python_path_in_the_document_exists():
    """Full paths must exist; a bare ``name.py`` must exist *somewhere*.

    The prose introduces sibling modules by bare name once their directory is
    established ("the `resolver.py` probes …"), so demanding a full path for
    every mention would be a style rule, not a truth rule — but a renamed module
    should still fail, which the second lookup does.
    """
    doc = _doc_text()

    named = sorted(set(re.findall(r"`([A-Za-z0-9_./-]+\.py)`", doc)))
    assert named, "the document stopped naming any file path"

    full_paths = [name for name in named if "/" in name]
    missing = [path for path in full_paths if not pathlib.Path(path).exists()]
    assert missing == [], f"the document names paths that do not exist: {missing}"

    known_modules = {path.name for path in pathlib.Path("agent").rglob("*.py")} | {
        path.name for path in pathlib.Path("tests").rglob("*.py")
    }
    unknown = [name for name in named if "/" not in name and name not in known_modules]
    assert unknown == [], f"the document names modules that do not exist: {unknown}"


def test_the_documented_configuration_matches_the_objects():
    from config.features import RIPGREP, TOOLS_TIMEOUTS

    doc = _doc_text()

    assert "RIPGREP" in doc and "config/features/agent_side/ripgrep.py" in doc
    assert pathlib.Path("config/features/agent_side/ripgrep.py").exists()
    # The values the document quotes, not the key names: the cell names the
    # override and the durations, and those must be what the object holds.
    assert RIPGREP["path_env_key"] in doc, "the documented env override drifted"
    assert "enabled" in doc and "enabled" in RIPGREP
    assert RIPGREP["version_probe_timeout_ms"] == 5_000
    assert RIPGREP["terminate_grace_seconds"] == 5.0 and RIPGREP["kill_grace_seconds"] == 5.0
    assert "5 s" in doc, "the documented graces drifted from the config"
    assert "TOOLS_TIMEOUTS" in doc
    for key in ("file_tools_search_time_budget_s", "file_tools_search_max_matches"):
        assert key in TOOLS_TIMEOUTS


def test_the_keyword_layer_claims_match_the_tool_surface():
    """The statement that went stale: who can actually call `search_files`."""
    from agent.tools import _MAIN_TOOLS_BUILDERS

    doc = _doc_text()

    assert "search_files" in doc
    builder_names = {getattr(builder, "__name__", "") for builder in _MAIN_TOOLS_BUILDERS}
    assert "build_search_files_tool" in builder_names, (
        "the main tool set stopped offering search_files, so the document's claim "
        "that the keyword layer is a shared, main-agent tool is wrong"
    )

    for role in ("researcher", "librarian"):
        definition = pathlib.Path(f"agent/tools/subagent/roles/definitions/{role}/AGENTS.md")
        assert definition.exists(), f"the {role} role definition moved"
        assert "search_files" in definition.read_text(encoding="utf-8"), (
            f"the {role} role no longer names search_files, which the document claims it does"
        )


def test_the_documented_measurements_are_history_not_assertions():
    """The timing figures are measurements, so they only have to stay labelled."""
    doc = _doc_text()

    assert "16.6 s" in doc and "1.2 s" in doc
    assert "Measured on this repository" in doc, (
        "the timing figures lost their 'measured on this repository' framing"
    )
