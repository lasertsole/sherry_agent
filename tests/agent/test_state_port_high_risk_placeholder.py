"""Healed transcripts warn when a missing result may hide an executed action."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent.state_port import heal_trailing_tool_calls
from pub.func.transcript_repair import MISSING_HIGH_RISK_TOOL_RESULT, MISSING_TOOL_RESULT

pytestmark = [pytest.mark.unit]


def _ai(*calls: tuple[str, str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {}, "id": call_id, "type": "tool_call"}
            for call_id, name in calls
        ],
    )


def test_only_the_high_risk_tools_get_the_side_effect_warning():
    messages = [
        _ai(
            ("c1", "terminal"),
            ("c2", "read_file"),
            ("c3", "mcp__stripe__charge"),
            ("c4", "python_repl"),
        )
    ]

    placeholders = heal_trailing_tool_calls(messages, "interrupted-t-1")
    by_name = {str(p.name): p for p in placeholders}

    assert by_name["terminal"].content == MISSING_HIGH_RISK_TOOL_RESULT
    assert by_name["mcp__stripe__charge"].content == MISSING_HIGH_RISK_TOOL_RESULT
    assert by_name["python_repl"].content == MISSING_HIGH_RISK_TOOL_RESULT
    assert by_name["read_file"].content == MISSING_TOOL_RESULT


def test_the_repair_marker_contract_is_unchanged():
    placeholders = heal_trailing_tool_calls([_ai(("c1", "terminal"))], "m-1")

    assert len(placeholders) == 1
    placeholder = placeholders[0]
    # Still a successful repair marker (not a tool failure), with a stable id.
    assert placeholder.status == "success"
    assert placeholder.additional_kwargs["sherry_repair"] == "missing_tool_result"
    assert placeholder.id == "m-1-heal-c1"


def test_an_answered_call_is_not_healed_again():
    messages = [
        _ai(("c1", "terminal")),
        ToolMessage(content="done", tool_call_id="c1", name="terminal"),
    ]

    assert heal_trailing_tool_calls(messages, "m-2") == []
