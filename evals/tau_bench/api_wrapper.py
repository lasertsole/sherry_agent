"""The TAU-bench API wrapper: package tools as Sherry tools, the episode loop.

THE SURFACE THIS MODULE CALLS (pinned by ``tests/evals/test_tau_bench.py``
against a fake package — a refactor on their side shows up as a test failure):

* ``tau_bench.envs.<scene>.data.tasks`` — the task list (Task objects);
* ``tau_bench.envs.<scene>.<EnvClass>`` — resolved via
  ``api_wrapper.resolve_env_class(surface, scene)``, expected to expose
  ``tools`` (a list of objects with ``info`` = JSON-schema-like dict with
  ``function: {name, description, parameters}`` and an ``__call__`` or
  ``.run(...)`` executing against the env state);
* ``tau_bench.types.Task`` — ``user_scenario`` / ``instructions`` /
  ``expected_actions`` / ``outputs`` attributes;
* the episode reward: the adapter's generic checker over the env's recorded
  tool-call log (``api_wrapper.recorded_calls``) compared with the task's
  ``expected_actions`` (name + arg subset) and/or ``outputs`` (key match in
  the agent's final text).

The generic checker is the adapter's own grading when the package's native
reward is not reachable through the small surface above — the report SAYS so
(the ``grading`` field), never pretending to be the official task_eval.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

__all__ = [
    "resolve_env_class",
    "build_episode_tools",
    "install_episode_tools",
    "remove_episode_tools",
    "run_episode",
    "recorded_calls",
]

_installed: list[BaseTool] = []
_calls: list[dict[str, Any]] = []


def recorded_calls() -> list[dict[str, Any]]:
    """The tool calls the episode made (name + args), for the reward check."""
    return list(_calls)


def resolve_env_class(surface: dict[str, Any], scene: str) -> Any:
    """The env class for one scene, located by name (``AirlineEnv`` /
    ``RetailEnv``) with a repr-based fallback — fail loudly when absent."""
    from importlib import import_module  # noqa: PLC0415 — lazy

    module = import_module(f"tau_bench.envs.{scene}")
    for name in (f"{scene.capitalize()}Env", f"{scene.title()}Env", "TauBenchEnv"):
        candidate = getattr(module, name, None)
        if candidate is not None:
            return candidate
    raise RuntimeError(f"no env class found in tau_bench.envs.{scene}")


def _tool_specs(env_class: Any) -> list[dict[str, Any]]:
    """The env's tool specs: objects with an ``info`` schema dict. Anything
    without one is skipped (the package may carry helper members)."""
    specs = []
    for entry in getattr(env_class, "tools", []) or []:
        info = getattr(entry, "info", None)
        if isinstance(info, dict) and info.get("function"):
            specs.append({"spec": entry, "info": info["function"]})
    return specs


def build_episode_tools(
    surface: dict[str, Any], scene: str, task: dict[str, Any]
) -> tuple[list[BaseTool], Any]:
    """Instantiate the env for one task and wrap its tools as Sherry tools."""
    env_class = resolve_env_class(surface, scene)
    raw_task = task.get("raw")
    env = env_class(
        getattr(raw_task, "user_scenario", raw_task) if raw_task is not None else raw_task,
        user_strategy="human",
        user_model="human",
        tool_call_timeout=40,
    )
    wrapped: list[BaseTool] = []
    for entry in _tool_specs(env):
        function = entry["info"]
        name = str(function.get("name") or "")
        if not name:
            continue

        def _make(spec: Any, tool_name: str):
            def _invoke(**kwargs: Any) -> str:
                _calls.append({"name": tool_name, "args": dict(kwargs)})
                result = spec(**kwargs) if callable(spec) else None
                return json.dumps(result, ensure_ascii=False, default=str)[:4000]

            return _invoke

        wrapped.append(
            StructuredTool.from_function(
                func=_make(entry["spec"], name),
                name=name,
                description=str(function.get("description") or name),
                args_schema=_schema_to_args_model(name, function.get("parameters") or {}),
            )
        )
    if not wrapped:
        raise RuntimeError(f"{scene}: the env exposes no wrappable tools")
    return wrapped, env


def _schema_to_args_model(name: str, parameters: dict[str, Any]) -> Any:
    """A pydantic model from the package's JSON schema (properties/required)."""
    from pydantic import Field, create_model  # noqa: PLC0415 — lazy

    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        python_type = _python_type(str(spec.get("type") or "string"))
        description = str(spec.get("description") or "")
        if key in required:
            fields[key] = (python_type, Field(..., description=description))
        else:
            fields[key] = (python_type | None, Field(default=None, description=description))
    model = create_model(f"{name}_args", **fields)
    return model


def _python_type(json_type: str) -> Any:
    return {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }.get(json_type, str)


def install_episode_tools(tools: list[BaseTool]) -> None:
    """Swap the agent's tool surface for the episode's API tools (the
    trim_tools mechanism is already in place — this swaps the terminal bridge
    slot the harness owns)."""
    from evals import external_bench  # noqa: PLC0415 — lazy

    del external_bench
    remove_episode_tools()
    import agent.core as agent_core  # noqa: PLC0415

    original = agent_core.get_agent_tools
    episode = list(tools)

    def episode_tools() -> list[Any]:
        return list(episode)

    _installed.append(original)  # type: ignore[arg-type]
    agent_core.get_agent_tools = episode_tools  # type: ignore[method-assign]


def remove_episode_tools() -> None:
    """Restore the agent's original tool surface (pop the last install)."""
    import agent.core as agent_core  # noqa: PLC0415 — lazy

    if _installed:
        agent_core.get_agent_tools = _installed.pop()  # type: ignore[method-assign]


async def run_episode(
    surface: dict[str, Any],
    scene: str,
    task: dict[str, Any],
    harness: Any,
    session_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Agent turn → user-simulator turn → … until a stop condition or budget."""
    raw_task = task.get("raw")
    instructions = str(
        getattr(raw_task, "instructions", None)
        or (raw_task.get("instructions") if isinstance(raw_task, dict) else "")
        or "Complete the customer's request using the API tools."
    )
    prompt = (
        f"{instructions}\n\n---\nYou are a {scene} service agent. Use the API "
        "tools to serve the user; when the request is fully handled, end with "
        "a short summary for the user."
    )
    drive: dict[str, Any] = {
        "tool_calls": 0,
        "tool_names": {},
        "text": "",
        "timeout": False,
        "error": "",
        "resolved": False,
        "reward_detail": "",
    }
    turn = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout)
    drive["tool_calls"] += int(turn.get("tool_calls") or 0)
    for name, count in (turn.get("tool_names") or {}).items():
        drive["tool_names"][name] = drive["tool_names"].get(name, 0) + int(count)
    drive["text"] += str(turn.get("text") or "")
    if turn.get("timeout") or turn.get("error"):
        drive["timeout"] = bool(turn.get("timeout"))
        drive["error"] = str(turn.get("error") or drive["error"])
    expected_actions = getattr(raw_task, "expected_actions", None) or (
        raw_task.get("expected_actions") if isinstance(raw_task, dict) else None
    )
    outputs = getattr(raw_task, "outputs", None) or (
        raw_task.get("outputs") if isinstance(raw_task, dict) else None
    )
    resolved, detail = _judge(drive["text"], expected_actions, outputs)
    drive["resolved"] = resolved
    drive["reward_detail"] = detail
    return drive


def _judge(final_text: str, expected_actions: Any, outputs: Any) -> tuple[bool, str]:
    """The adapter's generic reward: every expected action must appear in the
    recorded call log (name match + the given args as a subset), and every
    ``outputs`` key:value must appear in the agent's final text. Returns
    (resolved, detail)."""
    from evals.tau_bench.api_wrapper import recorded_calls  # noqa: PLC0415

    details: list[str] = []
    resolved = True
    for action in expected_actions or []:
        name = str(
            getattr(action, "name", None)
            or (action.get("name") if isinstance(action, dict) else "")
        )
        want_args = (
            getattr(action, "kwargs", None)
            or (action.get("kwargs") if isinstance(action, dict) else {})
            or {}
        )
        match = next(
            (
                call
                for call in recorded_calls()
                if call["name"] == name and _subset(want_args, call["args"])
            ),
            None,
        )
        if match is None:
            resolved = False
            details.append(f"missing action {name}({json.dumps(want_args, default=str)[:120]})")
    for key, value in dict(outputs or {}).items():
        if str(value).lower() not in (final_text or "").lower():
            resolved = False
            details.append(f"output {key}={value!r} not in the final response")
    return resolved, "; ".join(details)[:400]


def _subset(want: dict[str, Any], have: dict[str, Any]) -> bool:
    for key, value in (want or {}).items():
        if key not in have or have[key] != value:
            return False
    return True
