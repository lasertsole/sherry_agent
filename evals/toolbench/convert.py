"""Convert a Sherry ToolBench trajectory into the official answer format.

The shape and the system message are taken from ToolBench's own
``convert_to_answer_format.py`` (``generate_init_message_node`` +
``process_valid_data``): a single system→user→tool chain where every tool node
carries ``{"name", "arguments", "response"}`` and the final ``Finish`` call is a
tool node. The official evaluator's ``get_steps`` walks exactly this chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.toolbench.tools import ToolBenchEvalState, available_tools

_INIT_MESSAGE = (
    "You are AutoGPT, you can use many tools(functions) to do the following task.\n"
    "First I will give you the task description, and your task start.\n"
    "At each step, you need to give your thought to analyze the status now and what "
    "to do next, with a function call to actually excute your step.\n"
    "After the call, you will get the call result, and you are now in a new state.\n"
    "Then you will analyze your status now, then decide what to do next...\n"
    "After many (Thought-call) pairs, you finally perform the task, then you can give "
    "your finial answer.\n"
    "Remember: \n"
    "1.the state change is irreversible, you can't go back to one of the former state, "
    'if you want to restart the task, say "I give up and restart".\n'
    "2.All the thought is short, at most in 5 sentence.\n"
    "3.You can do more then one trys, so if your plan is to continusly try some "
    "conditions, you can do one of the conditions per try.\n"
    "Let's Begin!\n"
    "Task description: You should use functions to help handle the real time user "
    'querys. Remember to ALWAYS call "Finish" function at the end of the task. And '
    "the final answer should contain enough information to show to the user.\n"
    "Specifically, you have access to the following functions: "
)


def _node(role: str, message: Any) -> dict[str, Any]:
    return {"role": role, "message": message, "next": []}


def build_answer(task: Any, state: ToolBenchEvalState, final_text: str = "") -> dict[str, Any]:
    """Build the official converted-answer entry for one task."""
    functions = available_tools(task)
    root = _node("system", _INIT_MESSAGE + str(functions))
    cursor = root
    user = _node("user", task.query)
    cursor["next"].append(user)
    cursor = user
    for call in state.calls:
        # The official converter passes the tool payload as a DICT; the
        # evaluator's f-string then renders its repr (and looks for
        # "'name': 'Finish'" in the final step).
        tool = _node(
            "tool",
            {
                "name": call["function"],
                "arguments": call["arguments"],
                "response": call["response"],
            },
        )
        cursor["next"].append(tool)
        cursor = tool
    return {
        "query": task.query,
        "available_tools": functions,
        "answer": {
            "method": "Sherry",
            "total_steps": len(state.calls) + 2,
            "final_answer": state.final_answer or final_text,
            "answer_details": [root],
        },
    }


def write_converted(entries: dict[str, dict[str, Any]], path: Path) -> None:
    """Write the official ``{query_id: converted_answer}`` JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
