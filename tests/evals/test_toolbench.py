"""Hermetic checks for the ToolBench arm (no network, no model, no judge)."""

from __future__ import annotations

import pytest

from evals.toolbench.convert import build_answer
from evals.toolbench.tasks import Task
from evals.toolbench.tools import (
    ToolBenchEvalState,
    api_to_function,
    available_tools,
    standardize,
)

pytestmark = [pytest.mark.unit]

_API = {
    "category_name": "Logistics",
    "tool_name": "SQUAKE",
    "api_name": "Checkhealth",
    "api_description": "Verify the API health.",
    "required_parameters": [
        {"name": "id", "type": "NUMBER", "description": "identifier", "default": ""}
    ],
    "optional_parameters": [
        {"name": "note", "type": "STRING", "description": "note", "default": "x"}
    ],
}


def _task() -> Task:
    return Task(
        subset="G1_instruction",
        query_id=1,
        query="Check the API health.",
        api_list=(_API,),
    )


def test_dispatcher_rejects_an_unknown_function_without_a_network_call():
    from evals.toolbench.tools import call_api

    result = call_api(
        _task(),
        "no_such_function",
        "{}",
        service_url="http://127.0.0.1:9/rapidapi",
        toolbench_key="unused",
        timeout=1,
    )

    assert "unknown function" in result
    assert "checkhealth_for_squake" in result


def test_prompt_lists_the_official_function_names():
    from evals.toolbench.tools import build_prompt

    prompt = build_prompt(_task())

    assert "checkhealth_for_squake" in prompt
    assert "toolbench_call" in prompt
    assert "Task description: Check the API health." in prompt


def test_official_name_derivation():
    assert standardize("Check health!") == "check_health"
    function = api_to_function(_API)
    assert function["name"] == "checkhealth_for_squake"
    assert function["parameters"]["required"] == ["is_id"]  # official change_name("id")
    assert function["parameters"]["optional"] == ["note"]
    assert function["parameters"]["properties"]["is_id"]["type"] == "integer"
    assert function["parameters"]["properties"]["note"]["example_value"] == "x"


def test_answer_conversion_has_the_official_finish_node():
    task = _task()
    state = ToolBenchEvalState(
        calls=[
            {
                "function": "checkhealth_for_squake",
                "arguments": '{"id": 1}',
                "response": "ok",
            },
            {
                "function": "Finish",
                "arguments": '{"return_type": "give_answer", "final_answer": "healthy"}',
                "response": "",
            },
        ],
        final_answer="healthy",
        finished=True,
        finish_type="give_answer",
    )

    entry = build_answer(task, state)

    assert entry["available_tools"] == available_tools(task)
    details = entry["answer"]["answer_details"][0]
    assert details["role"] == "system"
    user = details["next"][0]
    assert user["role"] == "user"
    tool = user["next"][0]
    assert tool["role"] == "tool"
    assert tool["message"]["name"] == "checkhealth_for_squake"
    final = tool["next"][0]
    assert final["message"]["name"] == "Finish"
    assert "'name': 'Finish'" in str(final["message"])


def test_toolbench_is_registered():
    from evals.evals import SUITES

    assert SUITES["toolbench"] == "evals.toolbench.suite"
