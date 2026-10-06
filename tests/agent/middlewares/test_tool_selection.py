"""Per-session tool selection + the middleware on/off switch.

The config lives in the state register (written by ``PUT /sessions/agent_config``);
the middleware reads it per call. These tests drive the hooks directly and pin:
the model-visible tool list is narrowed, a disabled tool is refused at EXECUTION,
an unset config is a no-op, a broken read is fail-open, and each gateable
middleware goes fully inert when its switch is off.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.agent_switch import disabled_middlewares, middleware_enabled
from agent.middlewares.tool_selection import ToolSelectionMiddleware
from agent.middlewares.tool_selection.core import enabled_tool_names
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.unit]

SESSION = "sess-tool-selection"


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Request:
    """Minimal ModelRequest double: state + tools + an override() that copies."""

    def __init__(self, state: dict, tools: list[_Tool]) -> None:
        self.state = state
        self.tools = list(tools)
        self.overrides: list[dict] = []

    def override(self, **kwargs):
        clone = _Request(self.state, kwargs.get("tools", self.tools))
        clone.overrides = [*self.overrides, kwargs]
        return clone


class _ToolCallRequest:
    def __init__(self, session_id: str, name: str, call_id: str = "call-1") -> None:
        self.state = {"session_id": session_id}
        self.tool_call = {"name": name, "id": call_id, "args": {}}


@pytest.fixture(autouse=True)
def _isolated_config():
    state_register_mem.clear_session(SESSION)
    yield
    state_register_mem.clear_session(SESSION)


def _set_config(payload: dict) -> None:
    state_register_mem.set_state(SESSION, StateKey.AGENT_CONFIG, payload)


# ----------------------------------------------------------------------
# reading the config
# ----------------------------------------------------------------------


def test_an_unset_config_means_every_tool():
    assert enabled_tool_names(SESSION) is None
    assert enabled_tool_names(None) is None


def test_a_config_without_tools_means_every_tool():
    _set_config({"middlewares_disabled": ["TaskIntentMiddleware"]})

    assert enabled_tool_names(SESSION) is None


def test_the_enabled_set_is_read_from_the_register():
    _set_config({"tools": ["read_file", "terminal"]})

    assert enabled_tool_names(SESSION) == frozenset({"read_file", "terminal"})


def test_a_broken_register_read_is_fail_open(monkeypatch):
    import runtime

    class _Boom:
        def get_state(self, *_args, **_kwargs):
            raise RuntimeError("register down")

    monkeypatch.setattr(runtime, "state_register_mem", _Boom())

    assert enabled_tool_names(SESSION) is None


# ----------------------------------------------------------------------
# model view + execution
# ----------------------------------------------------------------------


def test_the_model_view_is_narrowed_to_the_enabled_tools():
    _set_config({"tools": ["read_file", "terminal"]})
    middleware = ToolSelectionMiddleware()
    request = _Request(
        {"session_id": SESSION}, [_Tool("read_file"), _Tool("terminal"), _Tool("web_search")]
    )
    seen: dict = {}

    def handler(req):
        seen["tools"] = [tool.name for tool in req.tools]
        return "ok"

    assert middleware.wrap_model_call(request, handler) == "ok"
    assert seen["tools"] == ["read_file", "terminal"]


def test_an_unset_config_passes_the_request_through_untouched():
    middleware = ToolSelectionMiddleware()
    request = _Request({"session_id": SESSION}, [_Tool("read_file"), _Tool("web_search")])

    def handler(req):
        assert req is request, "no override should be built when nothing is configured"
        return "ok"

    assert middleware.wrap_model_call(request, handler) == "ok"


@pytest.mark.asyncio
async def test_the_async_path_narrows_too():
    _set_config({"tools": ["web_search"]})
    middleware = ToolSelectionMiddleware()
    request = _Request({"session_id": SESSION}, [_Tool("read_file"), _Tool("web_search")])

    async def handler(req):
        return [tool.name for tool in req.tools]

    assert await middleware.awrap_model_call(request, handler) == ["web_search"]


def test_a_disabled_tool_is_refused_at_execution():
    _set_config({"tools": ["read_file"]})
    middleware = ToolSelectionMiddleware()
    ran: list[str] = []

    result = middleware.wrap_tool_call(
        _ToolCallRequest(SESSION, "terminal"),
        lambda req: ran.append(req.tool_call["name"]),  # type: ignore[arg-type,return-value]
    )

    assert ran == [], "the handler must not run for a disabled tool"
    assert getattr(result, "status", None) == "error"
    assert "disabled for this session" in str(result.content)
    assert getattr(result, "tool_call_id", None) == "call-1"


@pytest.mark.asyncio
async def test_an_enabled_tool_still_runs():
    _set_config({"tools": ["terminal"]})
    middleware = ToolSelectionMiddleware()

    async def handler(_req):
        return "ran"

    assert await middleware.awrap_tool_call(_ToolCallRequest(SESSION, "terminal"), handler) == "ran"


@pytest.mark.asyncio
async def test_an_unset_config_lets_every_tool_run():
    middleware = ToolSelectionMiddleware()

    async def handler(_req):
        return "ran"

    assert (
        await middleware.awrap_tool_call(_ToolCallRequest(SESSION, "web_search"), handler) == "ran"
    )


# ----------------------------------------------------------------------
# the middleware on/off switch
# ----------------------------------------------------------------------


def test_the_switch_helper_reads_the_disabled_set():
    assert middleware_enabled(SESSION, "TaskIntentMiddleware") is True

    _set_config({"middlewares_disabled": ["TaskIntentMiddleware"]})

    assert disabled_middlewares(SESSION) == frozenset({"TaskIntentMiddleware"})
    assert middleware_enabled(SESSION, "TaskIntentMiddleware") is False
    assert middleware_enabled(SESSION, "TodoContinuationEnforcer") is True


def test_the_switch_is_fail_open_on_a_broken_read(monkeypatch):
    import runtime

    class _Boom:
        def get_state(self, *_args, **_kwargs):
            raise RuntimeError("register down")

    monkeypatch.setattr(runtime, "state_register_mem", _Boom())

    assert middleware_enabled(SESSION, "TaskIntentMiddleware") is True


def test_a_disabled_task_intent_middleware_never_steers():
    from agent.middlewares.task_intent import TaskIntentMiddleware

    middleware = TaskIntentMiddleware()
    state = {
        "session_id": SESSION,
        "messages": [HumanMessage("帮我实现一个登录页面并对接后端接口")],
    }

    # The switch ON steers this request (the heuristic fires)...
    armed = asyncio.run(middleware.abefore_model(state))
    assert armed is not None and armed.get("messages"), "control case: the hook must inject"

    # ...and the switch OFF silences the very same hook.
    _set_config({"middlewares_disabled": ["TaskIntentMiddleware"]})
    assert asyncio.run(middleware.abefore_model(state)) is None


def test_a_disabled_multimodal_processor_leaves_the_message_untouched():
    from agent.middlewares.media_pipeline import MultimodalProcessor

    processor = MultimodalProcessor()
    content = [
        {"type": "text", "text": "看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]
    state = {"session_id": SESSION, "messages": [HumanMessage(content=content)]}
    _set_config({"middlewares_disabled": ["MultimodalProcessor"]})

    processor._before_agent_impl(state)

    # The processor would have rewritten the content list into a text block +
    # media hints; with the switch off it must not be touched at all.
    assert state["messages"][0].content == content


def test_a_disabled_completion_drain_never_touches_the_queue(monkeypatch):
    from agent.middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware

    calls: list[str] = []
    import agent.tools.subagent.announce.steering_queue as steering_queue

    monkeypatch.setattr(steering_queue, "drain", lambda *_a, **_k: calls.append("drain") or [])
    _set_config({"middlewares_disabled": ["SubagentCompletionDrainMiddleware"]})

    update = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model(
            {"session_id": SESSION, "messages": [HumanMessage("继续")]}
        )
    )

    assert update is None
    assert calls == [], "a disabled drain must not even look at the queue"


def test_a_disabled_todo_continuation_never_reads_the_plan(monkeypatch):
    from agent.middlewares.todo_continuation import TodoContinuationEnforcer

    calls: list[str] = []
    import agent.tools.todolist.registry.store_sqlite as store_sqlite

    monkeypatch.setattr(
        store_sqlite, "get_todos_sync", lambda *_a, **_k: calls.append("todos") or [], raising=False
    )
    _set_config({"middlewares_disabled": ["TodoContinuationEnforcer"]})

    asyncio.run(TodoContinuationEnforcer().aafter_agent({"session_id": SESSION, "messages": []}))

    assert calls == [], "a disabled continuation must not read the todo list"


def test_a_disabled_directory_notice_never_announces():
    from agent.middlewares.project_dir_notice import ProjectDirNoticeMiddleware

    _set_config({"middlewares_disabled": ["ProjectDirNoticeMiddleware"]})
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR_ANNOUNCED, "/tmp/old-root")
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, "/tmp/new-root")
    middleware = ProjectDirNoticeMiddleware()

    update = asyncio.run(
        middleware.abefore_agent({"session_id": SESSION, "messages": [HumanMessage("你好")]})
    )

    assert update is None
