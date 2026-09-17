"""Per-session isolation for the ``@dynamic_prompt`` system-prompt middleware.

``test_system_prompt_reuse`` covers injection, same-content skip, dual writes
and the session guard for a single session. These tests lock the
*cross-session* contract: interleaved and concurrent A/B calls must each
resolve their own prompt, the skip must reuse only the same session's message
object, both state registers must keep each session's own value, and neither
the decorator product nor the module may grow mutable session-scoped state.
"""

import ast
import asyncio
import inspect
import uuid

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent.middlewares.context_engine.core as ce_core
from agent.middlewares.context_engine.core import context_engine_prompt
from runtime import state_register_db, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _StubModel:
    _llm_type = "fake"


@pytest.fixture
def sessions():
    sid_a = "iso-a-" + uuid.uuid4().hex
    sid_b = "iso-b-" + uuid.uuid4().hex
    yield sid_a, sid_b
    for sid in (sid_a, sid_b):
        state_register_mem.clear_session(sid)
        state_register_db.delete_state(sid, "system_prompt")


@pytest.fixture
def builder_calls(monkeypatch):
    calls: list[str] = []

    def _build(session_id: str = "") -> str:
        calls.append(session_id)
        return f"PROMPT-{session_id}"

    monkeypatch.setattr(ce_core, "build_system_prompt", _build)
    return calls


def _request(session_id: str, system_message: SystemMessage | None = None) -> ModelRequest:
    messages = [HumanMessage(content="q")]
    return ModelRequest(
        model=_StubModel(),
        messages=list(messages),
        system_message=system_message,
        state={"session_id": session_id, "messages": list(messages)},
    )


def _inject(request: ModelRequest) -> ModelRequest:
    captured: dict[str, ModelRequest] = {}

    def handler(inner_request: ModelRequest) -> AIMessage:
        captured["request"] = inner_request
        return AIMessage(content="ok")

    context_engine_prompt.wrap_model_call(request, handler)
    return captured["request"]


async def _ainject(request: ModelRequest) -> ModelRequest:
    captured: dict[str, ModelRequest] = {}

    async def handler(inner_request: ModelRequest) -> AIMessage:
        await asyncio.sleep(0)
        captured["request"] = inner_request
        return AIMessage(content="ok")

    await context_engine_prompt.awrap_model_call(request, handler)
    return captured["request"]


class TestInterleavedSessions:
    def test_interleaved_injections_keep_own_prompt(self, sessions, builder_calls):
        # Given two sessions with no cached prompt
        sid_a, sid_b = sessions

        # When A, B, A are injected in order
        inner_a1 = _inject(_request(sid_a))
        inner_b1 = _inject(_request(sid_b))
        inner_a2 = _inject(_request(sid_a))

        # Then every call sees its own prompt, objects are not shared, and the
        # builder ran exactly once per session
        assert inner_a1.system_message.content == f"PROMPT-{sid_a}"
        assert inner_b1.system_message.content == f"PROMPT-{sid_b}"
        assert inner_a2.system_message.content == f"PROMPT-{sid_a}"
        assert inner_b1.system_message is not inner_a1.system_message
        assert builder_calls == [sid_a, sid_b]

    def test_repeat_injection_reuses_same_session_message(self, sessions, builder_calls):
        # Given session A injected once and session B interleaved after it
        sid_a, sid_b = sessions
        inner_a1 = _inject(_request(sid_a))
        _inject(_request(sid_b))

        # When A is injected again carrying its own previous system message
        inner_a2 = _inject(_request(sid_a, inner_a1.system_message))

        # Then the skip hands back A's own object and nothing was rebuilt
        assert inner_a2.system_message is inner_a1.system_message
        assert inner_a2.system_message.content == f"PROMPT-{sid_a}"
        assert builder_calls == [sid_a, sid_b]


class TestConcurrentSessions:
    @pytest.mark.asyncio
    async def test_parallel_injections_do_not_cross_talk(self, sessions, builder_calls):
        # Given two sessions and an interleaved batch of model calls
        sid_a, sid_b = sessions
        sids = [sid_a, sid_b] * 4

        # When all calls run concurrently
        inners = await asyncio.gather(*(_ainject(_request(sid)) for sid in sids))

        # Then each result carries its own session prompt and each session's
        # prompt was built exactly once
        assert [inner.system_message.content for inner in inners] == [
            f"PROMPT-{sid}" for sid in sids
        ]
        assert sorted(builder_calls) == sorted([sid_a, sid_b])


class TestRegisterIsolation:
    def test_registers_store_prompts_per_session(self, sessions, builder_calls):
        # Given two sessions injected once each
        sid_a, sid_b = sessions
        _inject(_request(sid_a))
        _inject(_request(sid_b))

        # Then both registers hold each session's own prompt
        assert state_register_mem.get_state(sid_a, "system_prompt") == f"PROMPT-{sid_a}"
        assert state_register_mem.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"
        assert state_register_db.get_state(sid_a, "system_prompt") == f"PROMPT-{sid_a}"
        assert state_register_db.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"

    def test_later_injection_does_not_rewrite_other_session(self, sessions, builder_calls):
        # Given both sessions cached
        sid_a, sid_b = sessions
        _inject(_request(sid_a))
        _inject(_request(sid_b))

        # When A is injected again
        _inject(_request(sid_a))

        # Then B's entries are untouched and no prompt was rebuilt
        assert state_register_mem.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"
        assert state_register_db.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"
        assert builder_calls == [sid_a, sid_b]

    def test_clear_session_touches_only_that_session(self, sessions, builder_calls):
        # Given both sessions cached in mem and db
        sid_a, sid_b = sessions
        _inject(_request(sid_a))
        _inject(_request(sid_b))

        # When A's in-memory session state is cleared
        assert state_register_mem.clear_session(sid_a) is True

        # Then only A's mem entry is gone; B and both db entries survive
        assert state_register_mem.get_state(sid_a, "system_prompt") is None
        assert state_register_mem.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"
        assert state_register_db.get_state(sid_a, "system_prompt") == f"PROMPT-{sid_a}"
        assert state_register_db.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"

    def test_cleared_session_reloads_from_db_without_rebuild(self, sessions, builder_calls):
        # Given both sessions cached and A's mem entry cleared
        sid_a, sid_b = sessions
        _inject(_request(sid_a))
        _inject(_request(sid_b))
        state_register_mem.clear_session(sid_a)

        # When A is injected again
        inner_a = _inject(_request(sid_a))

        # Then the db tier serves A without rebuilding and B stays untouched
        assert inner_a.system_message.content == f"PROMPT-{sid_a}"
        assert builder_calls == [sid_a, sid_b]
        assert state_register_mem.get_state(sid_b, "system_prompt") == f"PROMPT-{sid_b}"


class TestNoMutableSessionState:
    def test_decorated_function_closes_over_no_state(self):
        # Given the decorator-generated sync wrapper
        wrapper = type(context_engine_prompt).__dict__["wrap_model_call"]
        closure = inspect.getclosurevars(wrapper)

        # Then it captures only the decorated function, and that function
        # itself closes over nothing (no captured per-session container)
        assert set(closure.nonlocals) == {"func"}
        raw_function = closure.nonlocals["func"]
        assert inspect.getclosurevars(raw_function).nonlocals == {}

    def test_module_has_no_prompt_container_or_cache_decorator(self):
        # Given the middleware module source
        source = inspect.getsource(ce_core)

        def _module_level_container_bindings(tree: ast.Module) -> list[str]:
            names: list[str] = []
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                binds_container = isinstance(value, (ast.Dict, ast.List, ast.Set)) or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in {"dict", "list", "set"}
                )
                if binds_container:
                    names.extend(
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name) and target.id != "__all__"
                    )
            return names

        # Then no module-level dict/list/set binding exists besides __all__
        assert _module_level_container_bindings(ast.parse(source)) == []
        # And no caching decorator can grow a hidden prompt cache
        assert "@cache" not in source
        assert "lru_cache" not in source
