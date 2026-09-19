"""Unit tests for the SummaryDoc schema, its code-layer caps and the renderer."""

import pytest

from agent.middlewares.summarization.summary_doc import (
    ACTIVE_PLAN_NOTES_MAX_ITEMS,
    COMPLETED_MAX_ITEMS,
    CRITICAL_CONTEXT_MAX_ITEMS,
    EVICTED_REFS_MAX_ITEMS,
    KEY_DECISIONS_MAX_ITEMS,
    SummaryDoc,
    cap_items,
    cap_summary_doc,
    render_summary_markdown,
)

pytestmark = [pytest.mark.unit]

_BASE_HEADERS = (
    "## Latest Unresolved User Request",
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "### Completed",
    "### In Progress",
    "### Blocked",
    "## Key Decisions",
    "## Next Steps",
    "## Critical Context",
    "## Relevant Files",
)


def _section(markdown: str, header: str) -> str:
    start = markdown.index(header)
    end = len(markdown)
    for other in (
        "\n## ",
        "\n### ",
    ):
        probe = markdown.find(other, start + len(header))
        if probe != -1:
            end = min(end, probe + 1)
    return markdown[start:end]


class TestSummaryDocSchema:
    def test_defaults_are_empty(self):
        doc = SummaryDoc()
        assert doc.latest_user_request == ""
        assert doc.goal == ""
        assert doc.constraints == []
        assert doc.active_plan_notes == []
        assert doc.evicted_refs == []

    def test_lone_string_is_wrapped_into_list(self):
        doc = SummaryDoc.model_validate({"constraints": "single constraint"})
        assert doc.constraints == ["single constraint"]

    def test_none_list_and_string_fields_are_coerced(self):
        doc = SummaryDoc.model_validate(
            {"goal": None, "completed": None, "blocked": ["a", None, 7]}
        )
        assert doc.goal == ""
        assert doc.completed == []
        assert doc.blocked == ["a", "", "7"]

    def test_unknown_keys_are_ignored(self):
        doc = SummaryDoc.model_validate({"goal": "g", "future_field": "x"})
        assert doc.goal == "g"


class TestRenderRoundTrip:
    def test_every_field_value_appears_in_render(self):
        doc = SummaryDoc(
            latest_user_request="VERBATIM-Q-123",
            goal="GOAL-456",
            constraints=["CONSTRAINT-1"],
            completed=["COMPLETED-1"],
            in_progress=["PROGRESS-1"],
            blocked=["BLOCKED-1"],
            key_decisions=["DECISION-1"],
            next_steps=["NEXT-1"],
            critical_context=["CRITICAL-1"],
            relevant_files=["path/x.py: WHY-1"],
            active_plan_notes=["NOTE-1"],
            evicted_refs=["/sessions/s/evicted/a.txt"],
        )
        markdown = render_summary_markdown(doc)
        for marker in (
            "VERBATIM-Q-123",
            "GOAL-456",
            "CONSTRAINT-1",
            "COMPLETED-1",
            "PROGRESS-1",
            "BLOCKED-1",
            "DECISION-1",
            "NEXT-1",
            "CRITICAL-1",
            "path/x.py: WHY-1",
            "NOTE-1",
            "/sessions/s/evicted/a.txt",
        ):
            assert marker in markdown

    def test_section_order_matches_the_legacy_template(self):
        markdown = render_summary_markdown(SummaryDoc())
        positions = [markdown.index(header) for header in _BASE_HEADERS]
        assert positions == sorted(positions)

    def test_empty_doc_renders_none_placeholders(self):
        markdown = render_summary_markdown(SummaryDoc())
        for header in _BASE_HEADERS:
            assert header in markdown
        assert markdown.count("- (none)") >= 7

    def test_optional_sections_absent_when_empty(self):
        markdown = render_summary_markdown(SummaryDoc(goal="g"))
        assert "## Active Plan Notes" not in markdown
        assert "## Evicted References" not in markdown

    def test_optional_sections_rendered_when_present(self):
        doc = SummaryDoc(active_plan_notes=["n"], evicted_refs=["/e/a.txt"])
        markdown = render_summary_markdown(doc)
        assert "## Active Plan Notes" in markdown
        assert "## Evicted References" in markdown
        assert markdown.index("## Active Plan Notes") > markdown.index("## Relevant Files")


class TestRenderDeterminism:
    def test_same_doc_renders_identical_bytes(self):
        doc = SummaryDoc(
            latest_user_request="q",
            goal="g",
            completed=["a", "b"],
            active_plan_notes=["n1"],
            evicted_refs=["/e/1.txt", "/e/2.txt"],
        )
        assert render_summary_markdown(doc) == render_summary_markdown(doc)

    def test_render_is_pure_relative_to_the_doc_copy(self):
        doc = SummaryDoc(goal="g")
        before = doc.model_copy(deep=True)
        render_summary_markdown(doc)
        assert doc == before


class TestLatestUserRequestVerbatim:
    def test_long_request_is_not_truncated(self):
        request = "Q" * 5000
        markdown = render_summary_markdown(SummaryDoc(latest_user_request=request))
        assert request in markdown
        assert "truncated" not in markdown

    def test_newlines_survive_inside_the_bullet(self):
        request = "line one\nline two"
        markdown = render_summary_markdown(SummaryDoc(latest_user_request=request))
        assert f"- {request}" in markdown


class TestRenderCaps:
    def test_completed_keeps_tail_and_annotates(self):
        doc = SummaryDoc(completed=[f"c{i}" for i in range(8)])
        markdown = render_summary_markdown(doc)
        section = _section(markdown, "### Completed")
        assert "- c7" in section
        assert "- c3" in section
        assert "- c2" not in section
        assert f"({8 - COMPLETED_MAX_ITEMS} earlier items omitted for brevity)" in section

    def test_key_decisions_keeps_tail_and_annotates(self):
        doc = SummaryDoc(key_decisions=[f"d{i}" for i in range(7)])
        section = _section(render_summary_markdown(doc), "## Key Decisions")
        assert "- d6" in section
        assert "- d2" in section
        assert "- d1" not in section
        assert f"({7 - KEY_DECISIONS_MAX_ITEMS} earlier items omitted for brevity)" in section

    def test_critical_context_keeps_tail_and_annotates(self):
        doc = SummaryDoc(critical_context=[f"x{i}" for i in range(6)])
        section = _section(render_summary_markdown(doc), "## Critical Context")
        assert "- x5" in section
        assert "- x3" in section
        assert "- x2" not in section
        assert f"({6 - CRITICAL_CONTEXT_MAX_ITEMS} earlier items omitted for brevity)" in section

    def test_active_plan_notes_keep_tail_and_annotate(self):
        total = ACTIVE_PLAN_NOTES_MAX_ITEMS + 5
        doc = SummaryDoc(active_plan_notes=[f"n{i}" for i in range(total)])
        section = _section(render_summary_markdown(doc), "## Active Plan Notes")
        assert f"- n{total - 1}" in section
        assert f"- n{total - ACTIVE_PLAN_NOTES_MAX_ITEMS}" in section
        assert "- n0" not in section
        assert "(5 earlier items omitted for brevity)" in section

    def test_evicted_refs_keep_tail_and_annotate(self):
        total = EVICTED_REFS_MAX_ITEMS + 2
        doc = SummaryDoc(evicted_refs=[f"/e/{i}.txt" for i in range(total)])
        section = _section(render_summary_markdown(doc), "## Evicted References")
        assert f"- /e/{total - 1}.txt" in section
        assert f"- /e/{total - EVICTED_REFS_MAX_ITEMS}.txt" in section
        assert "- /e/0.txt" not in section
        assert "(2 earlier items omitted for brevity)" in section

    def test_uncapped_sections_grow_without_annotation(self):
        doc = SummaryDoc(constraints=[f"k{i}" for i in range(30)])
        section = _section(render_summary_markdown(doc), "## Constraints & Preferences")
        assert "- k29" in section
        assert "- k0" in section
        assert "omitted" not in section


class TestCapSummaryDoc:
    def test_caps_every_capped_array(self):
        doc = SummaryDoc(
            completed=[f"c{i}" for i in range(8)],
            key_decisions=[f"d{i}" for i in range(8)],
            critical_context=[f"x{i}" for i in range(8)],
            active_plan_notes=[f"n{i}" for i in range(25)],
            evicted_refs=[f"e{i}" for i in range(25)],
        )
        capped = cap_summary_doc(doc)
        assert len(capped.completed) == COMPLETED_MAX_ITEMS
        assert capped.completed == doc.completed[-COMPLETED_MAX_ITEMS:]
        assert len(capped.key_decisions) == KEY_DECISIONS_MAX_ITEMS
        assert len(capped.critical_context) == CRITICAL_CONTEXT_MAX_ITEMS
        assert len(capped.active_plan_notes) == ACTIVE_PLAN_NOTES_MAX_ITEMS
        assert len(capped.evicted_refs) == EVICTED_REFS_MAX_ITEMS

    def test_uncapped_fields_are_untouched(self):
        doc = SummaryDoc(goal="keep me", constraints=["a", "b"], relevant_files=["f"])
        assert cap_summary_doc(doc) is doc

    def test_idempotent(self):
        doc = SummaryDoc(completed=[f"c{i}" for i in range(8)])
        once = cap_summary_doc(doc)
        assert cap_summary_doc(once) == once

    def test_cap_items_boundaries(self):
        assert cap_items([], 5) == ([], 0)
        assert cap_items(["a", "b"], 2) == (["a", "b"], 0)
        assert cap_items(["a", "b", "c"], 2) == (["b", "c"], 1)
        assert cap_items(["a"], 0) == (["a"], 0)

    def test_capped_doc_renders_without_annotation(self):
        doc = SummaryDoc(completed=[f"c{i}" for i in range(8)])
        assert "omitted" not in render_summary_markdown(cap_summary_doc(doc))
