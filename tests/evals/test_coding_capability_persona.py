"""The persona arms of the coding/swebench harness.

The capability baseline seeds a NEUTRAL rules file on purpose (the shipped
templates carry the persona and its orchestrator doctrine). The SWE-bench arm
can instead be run with that shipped 编码助手 preset system prompt (``--persona
coding``) to measure what the production persona does to the benchmark —
recorded, never silently mixed into the neutral baseline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from evals.coding_capability import suite as harness

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_persona_globals() -> Iterator[None]:
    """The seeded mode is module state; no test may leak it into another."""
    mode, lang = harness._PERSONA_MODE, harness._PERSONA_TEMPLATE_LANG
    yield
    harness._PERSONA_MODE = mode
    harness._PERSONA_TEMPLATE_LANG = lang


def test_neutral_persona_writes_rules_only(tmp_path) -> None:
    harness._seed_persona(tmp_path)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text == harness._NEUTRAL_AGENTS_MD
    assert not any(marker in text for marker in harness._FORBIDDEN_MARKERS)
    assert harness._PERSONA_MODE == "neutral"
    for name in harness._EMPTY_PERSONA_FILES:
        assert (tmp_path / name).read_text(encoding="utf-8") == ""


def test_coding_persona_copies_the_shipped_template(tmp_path) -> None:
    harness._seed_persona(tmp_path, persona="coding", template_lang="zh")

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    shipped = (harness.REPO_ROOT / "workspace" / "template" / "zh" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert text == shipped
    # The doctrine this arm exists to measure is present verbatim.
    assert "ORCHESTRATOR" in text
    assert harness._PERSONA_MODE == "coding"
    # The preset keeps the persona FILES empty; only the rules template is used.
    for name in harness._EMPTY_PERSONA_FILES:
        assert (tmp_path / name).read_text(encoding="utf-8") == ""
    assert (tmp_path / "memory" / "FACTS.md").read_text(encoding="utf-8") == ""


def test_an_unknown_persona_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown persona"):
        harness._seed_persona(tmp_path, persona="roleplay")
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_missing_coding_template_is_an_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="coding persona template"):
        harness._seed_persona(tmp_path, persona="coding", template_lang="xx")
