"""Phase-1 prompt-bound handoff: the child block, its parser, and the record.

The child appends a ```json block with four EXPERIENCE fields (its OWN layer
only); ``_extract_handoff_json`` parses the LAST such block, strips program
metadata (depth/agent_label/children/outcome), and fails open to None so the
plan extractor can fall back to the free-text result.
"""

from __future__ import annotations

import pytest

from agent.tools.subagent.spawn.core import _extract_handoff_json
from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole
from agent.tools.subagent.types.registry import CompletionState, SubagentRunRecord

pytestmark = [pytest.mark.unit]

_BLOCK = """{
  "task_understanding": "add a token",
  "discovered_hard_constraints": ["no network"],
  "locked_decisions": [{"decision": "use jwt", "reason": "stateless"}],
  "failed_branches": [{"branch": "sessions", "failure_reason": "needs a store"}]
}"""


def _wrap(block: str) -> str:
    return f"Some answer text.\n```json\n{block}\n```\n"


def test_a_valid_block_is_parsed():
    parsed = _extract_handoff_json(_wrap(_BLOCK))

    assert parsed == {
        "task_understanding": "add a token",
        "discovered_hard_constraints": ["no network"],
        "locked_decisions": [{"decision": "use jwt", "reason": "stateless"}],
        "failed_branches": [{"branch": "sessions", "failure_reason": "needs a store"}],
    }


def test_metadata_fields_are_stripped():
    block = (
        '{"task_understanding": "x", "depth": 2, "agent_label": "a", '
        '"outcome": "ok", "children": [{"task_understanding": "child"}]}'
    )

    parsed = _extract_handoff_json(_wrap(block))

    assert parsed == {"task_understanding": "x"}


def test_the_last_parseable_block_wins():
    first = '```json\n{"task_understanding": "first"}\n```'
    second = '```json\n{"task_understanding": "second"}\n```'

    assert _extract_handoff_json(f"{first}\n{second}") == {"task_understanding": "second"}


def test_a_block_without_experience_fields_is_ignored():
    assert _extract_handoff_json(_wrap('{"depth": 1, "children": []}')) is None


def test_malformed_json_falls_back_to_none():
    assert _extract_handoff_json(_wrap("{not json")) is None


def test_a_trailing_bad_block_does_not_hide_a_good_one():
    good = '```json\n{"task_understanding": "good"}\n```'
    bad = "```json\n{broken\n```"

    assert _extract_handoff_json(f"{good}\n{bad}") == {"task_understanding": "good"}


def test_no_block_or_no_text_is_none():
    assert _extract_handoff_json("just prose") is None
    assert _extract_handoff_json("") is None
    assert _extract_handoff_json(None) is None


def test_the_system_prompt_asks_for_the_block_first():
    prompt = build_subagent_system_prompt(
        role=SubagentSessionRole.LEAF,
        task="t",
        functional_role=FunctionalRole.GENERAL,
        role_description="",
        role_prompt_body="",
        requester_label="r",
        depth=1,
        max_depth=2,
        child_session_key="c",
        requester_session_key="r",
        can_spawn=False,
    )

    assert "handoff JSON block FIRST" in prompt
    for field in (
        "task_understanding",
        "discovered_hard_constraints",
        "locked_decisions",
        "failed_branches",
        "success_steps",
    ):
        assert field in prompt
    assert "Do NOT add depth/agent_label/children/outcome" in prompt


def test_completion_state_carries_the_handoff():
    assert CompletionState().handoff is None
    record = SubagentRunRecord(
        run_id="r",
        child_session_key="c",
        requester_session_key="p",
        task="t",
        step_id="step-1",
        flow_id="flow-1",
    )
    assert record.completion.handoff is None
    assert (record.step_id, record.flow_id) == ("step-1", "flow-1")


def test_success_steps_are_parsed():
    block = '{"success_steps": [{"step": "install", "tool": "terminal", "params": "uv add x", "outcome": "ok"}]}'

    parsed = _extract_handoff_json(_wrap(block))

    assert parsed == {
        "success_steps": [
            {"step": "install", "tool": "terminal", "params": "uv add x", "outcome": "ok"}
        ]
    }


def test_the_old_failed_paths_key_is_normalized():
    block = '{"failed_paths": [{"path": "old", "failure_reason": "r"}]}'

    parsed = _extract_handoff_json(_wrap(block))

    assert parsed == {"failed_branches": [{"branch": "old", "failure_reason": "r"}]}
