"""ToolBench's official function schemas + a proxy-backed execution tool.

Two pieces are ported from the official repository
(https://github.com/OpenBMB/ToolBench, pinned commit in
``tasks.TOOLBENCH_PIN``) so the benchmark sees the same names and schemas its
own harness would produce:

* ``standardize`` / ``change_name`` and ``api_to_function`` mirror
  ``toolbench/utils.py`` + ``rapidapi_wrapper.api_json_to_openai_json``;
* the execution payload mirrors ``rapidapi_wrapper``'s call to the official
  ToolBench RapidAPI proxy (same ``category`` / ``tool_name`` / ``api_name`` /
  ``tool_input`` / ``toolbench_key`` fields).

The agent calls ONE dispatcher (``toolbench_call``) with the official function
name instead of receiving dozens of dynamically generated LangChain tools. The
selection + argument construction being measured is the same; the interface is
flattened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_SERVICE_URL = "http://8.130.32.149:8080/rapidapi"
_MAX_OBSERVATION_CHARS = 4_000

#: The official ReAct system/user text (Prompt/ReAct_prompts.py).
SYSTEM_PROMPT = """You are AutoGPT, you can use many tools(functions) to do the following task.
First I will give you the task description, and your task start.
At each step, you need to give your thought to analyze the status now and what to do next, with a function call to actually excute your step.
After the call, you will get the call result, and you are now in a new state.
Then you will analyze your status now, then decide what to do next...
After many (Thought-call) pairs, you finally perform the task, then you can give your finial answer.
Remember: 
1.the state change is irreversible, you can't go back to one of the former state, if you want to restart the task, say "I give up and restart".
2.All the thought is short, at most in 5 sentence.
3.You can do more then one trys, so if your plan is to continusly try some conditions, you can do one of the conditions per try.
Let's Begin!
Task description: {task_description}"""

_USER_TEMPLATE = """
{input_description}
Begin!
"""


@dataclass
class ToolBenchEvalState:
    """Everything the official answer conversion needs."""

    calls: list[dict[str, str]] = field(default_factory=list)
    final_answer: str = ""
    finished: bool = False
    finish_type: str = ""


def standardize(value: Any) -> str:
    """Official ``standardize``: lowercase, non-alphanumerics → ``_``."""
    text = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_]", "_", str(value or ""))
    text = re.sub(r"(_)\1+", "_", text).lower()
    return text.strip("_")


def change_name(name: str) -> str:
    """Official ``change_name``: keywords are prefixed with ``is_``."""
    if name in {"from", "class", "return", "false", "true", "id", "and"}:
        return "is_" + name
    return name


def _param_description(parameter: dict[str, Any], parameter_type: str) -> dict[str, Any]:
    prompt: dict[str, Any] = {
        "type": parameter_type,
        "description": str(parameter.get("description") or "")[:256],
    }
    default_value = parameter.get("default", "")
    if len(str(default_value)) != 0:
        prompt["example_value"] = default_value
    return prompt


def api_to_function(api: dict[str, Any]) -> dict[str, Any]:
    """Official ``api_json_to_openai_json`` (name / description / parameters)."""
    map_type = {"NUMBER": "integer", "STRING": "string", "BOOLEAN": "boolean"}
    standard_tool = standardize(api.get("tool_name"))
    pure_api = change_name(standardize(api.get("api_name")))
    function: dict[str, Any] = {
        "name": (pure_api + f"_for_{standard_tool}")[-64:],
        "description": (
            f'This is the subfunction for tool "{standard_tool}", you can use this tool.'
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "optional": [],
        },
    }
    description = str(api.get("api_description") or "").strip()
    if description:
        replacement = api.get("api_name") or ""
        function["description"] += (
            'The description of this function is: "'
            + description.replace(str(replacement), function["name"])[:256]
            + '"'
        )
    for parameter in api.get("required_parameters") or []:
        name = change_name(standardize(parameter.get("name")))
        prompt = _param_description(parameter, map_type.get(parameter.get("type"), "string"))
        function["parameters"]["properties"][name] = prompt
        function["parameters"]["required"].append(name)
    for parameter in api.get("optional_parameters") or []:
        name = change_name(standardize(parameter.get("name")))
        prompt = _param_description(parameter, map_type.get(parameter.get("type"), "string"))
        function["parameters"]["properties"][name] = prompt
        function["parameters"]["optional"].append(name)
    return function


def available_tools(task: Any) -> list[dict[str, Any]]:
    """The task's official function list, de-duplicated by name."""
    seen: set[str] = set()
    functions: list[dict[str, Any]] = []
    for api in task.api_list:
        function = api_to_function(api)
        if function["name"] in seen:
            continue
        seen.add(function["name"])
        functions.append(function)
    return functions


def function_index(task: Any) -> dict[str, dict[str, Any]]:
    """Map the official function name back to its API descriptor."""
    index: dict[str, dict[str, Any]] = {}
    for api in task.api_list:
        index[api_to_function(api)["name"]] = api
    return index


def _observation(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "")
    response = str(payload.get("response") or "")
    text = f"error: {error}\nresponse: {response}" if error else response
    return text[:_MAX_OBSERVATION_CHARS]


def call_api(
    task: Any,
    function: str,
    arguments: str,
    *,
    service_url: str,
    toolbench_key: str,
    timeout: float,
) -> str:
    """POST one call to the official ToolBench RapidAPI proxy."""
    api = function_index(task).get(function)
    if api is None:
        allowed = ", ".join(sorted(function_index(task))[:20])
        return f"Error: unknown function {function!r}. Allowed functions: {allowed}"
    payload = {
        "category": api.get("category_name", ""),
        "tool_name": api.get("tool_name", ""),
        "api_name": api.get("api_name", ""),
        "tool_input": arguments or "{}",
        "strip": "filter",
        "toolbench_key": toolbench_key,
    }
    try:
        response = httpx.post(
            service_url,
            json=payload,
            headers={"toolbench_key": toolbench_key},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return f"Error: request failed: {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return f"Error: proxy returned HTTP {response.status_code}: {response.text[:1000]}"
    try:
        body = response.json()
    except ValueError:
        return f"Error: proxy answered non-JSON: {response.text[:1000]}"
    return _observation(body if isinstance(body, dict) else {"response": body})


def build_prompt(task: Any) -> str:
    """The official ReAct prompt + the official function list."""
    functions = available_tools(task)
    tool_note = (
        "You have one execution tool, `toolbench_call`, whose `function` argument "
        "MUST be one of the function names below and whose `arguments` argument is "
        "a JSON object string for that function. When the task is complete, call "
        "`Finish` with return_type='give_answer' and final_answer=<your answer>. "
        "The ONLY accepted completion is a Finish call."
    )
    return (
        SYSTEM_PROMPT.format(task_description=task.query)
        + _USER_TEMPLATE.format(input_description=task.query)
        + "\nAvailable functions (official ToolBench schemas):\n"
        + json.dumps(functions, ensure_ascii=False)
        + "\n\n"
        + tool_note
    )


def install_toolbench_tools(
    task: Any,
    state: ToolBenchEvalState,
    *,
    service_url: str,
    toolbench_key: str,
    request_timeout: float = 30.0,
) -> ToolBenchEvalState:
    """Install ``toolbench_call`` + ``Finish`` on the live agent tool list."""
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, ConfigDict, Field

    import agent.core as agent_core

    class _CallInput(BaseModel):
        function: str = Field(
            description="The exact function name from the task's available functions."
        )
        arguments: str = Field(
            default="{}",
            description="A JSON object string with the function's parameters.",
        )

    class _FinishInput(BaseModel):
        return_type: str = Field(
            default="give_answer",
            description="'give_answer' to submit the final answer, or 'give_up_and_restart'.",
        )
        final_answer: str = Field(default="", description="The final answer string.")

    class ToolBenchCallTool(BaseTool):
        name: str = "toolbench_call"
        description: str = (
            "Execute ONE official ToolBench function. Pass the exact `function` "
            "name from the prompt's available-functions list and its parameters "
            "as a JSON object string."
        )
        args_schema: Any = _CallInput
        task: Any = Field(exclude=True)
        state: Any = Field(exclude=True)
        service_url: str = Field(exclude=True)
        toolbench_key: str = Field(exclude=True)
        request_timeout: float = Field(exclude=True)
        model_config = ConfigDict(arbitrary_types_allowed=True)

        def _run(self, function: str, arguments: str = "{}", **_: Any) -> str:
            observation = call_api(
                self.task,
                function,
                arguments,
                service_url=self.service_url,
                toolbench_key=self.toolbench_key,
                timeout=self.request_timeout,
            )
            self.state.calls.append(
                {"function": function, "arguments": arguments or "{}", "response": observation}
            )
            return observation

    class FinishTool(BaseTool):
        name: str = "Finish"
        description: str = (
            "Submit the final answer (return_type='give_answer') or restart the task."
        )
        args_schema: Any = _FinishInput
        state: Any = Field(exclude=True)
        model_config = ConfigDict(arbitrary_types_allowed=True)

        def _run(self, return_type: str = "give_answer", final_answer: str = "", **_: Any) -> str:
            self.state.finish_type = return_type
            self.state.finished = return_type == "give_answer"
            self.state.final_answer = final_answer
            self.state.calls.append(
                {
                    "function": "Finish",
                    "arguments": json.dumps(
                        {"return_type": return_type, "final_answer": final_answer},
                        ensure_ascii=False,
                    ),
                    "response": "",
                }
            )
            return (
                "successfully giving the final answer."
                if self.state.finished
                else "chose to give up and restart"
            )

    tools = agent_core.get_agent_tools()
    tools[:] = [
        tool for tool in tools if getattr(tool, "name", "") not in {"toolbench_call", "Finish"}
    ]
    tools.append(
        ToolBenchCallTool(
            task=task,
            state=state,
            service_url=service_url,
            toolbench_key=toolbench_key,
            request_timeout=request_timeout,
            metadata={"idempotent": False, "side_effect_scope": "system"},
        )
    )
    tools.append(
        FinishTool(
            state=state,
            metadata={"idempotent": False, "side_effect_scope": "user_interaction"},
        )
    )
    return state
