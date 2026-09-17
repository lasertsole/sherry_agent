"""Compression-time nudge dispatch (migrated from test_compaction_persistence).

The compression path no longer persists messages (that moved to
``MessagePersistenceMiddleware``), but it still schedules the memory-review and
plan-extraction nudges from the same seam. These assertions move with the
scheduler, unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.summarization.nudges as nudge_mod
from agent.middlewares.system_prompt import core as ce_core
from agent.middlewares.summarization import Summarization
from context_engine.store import core as store_core
from context_engine.store import db as store_db
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class _StubSummaryModel:
    """Fake auxiliary summary model (sync + async)."""

    _llm_type = "fake"

    def invoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    @staticmethod
    def _response() -> Any:
        return type(
            "R",
            (),
            {
                "content": (
                    "A sufficiently long deterministic summary of the conversation "
                    "history covering decisions, files and next steps."
                )
            },
        )()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    value = "cmp-nudge-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield value
    state_register_mem.clear_session(value)


def _large_history(turns: int = 60, out_chars: int = 800) -> list:
    """Deterministic history long enough to force a real cutoff."""
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i}", id=f"h{i}"))
        messages.append(
            AIMessage(
                content=f"working {i}",
                id=f"a{i}",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": f"cmd {i}"},
                        "id": f"c{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                content=f"output {i} " + "x" * out_chars,
                name="terminal",
                tool_call_id=f"c{i}",
                id=f"t{i}",
            )
        )
    return messages


def _request(messages: list, session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_StubSummaryModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _make_summarization() -> Summarization:
    return Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
    )


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


class _FakeStateRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


class TestCompressionNudgeDispatch:
    @pytest.mark.asyncio
    async def test_compression_dispatches_memory_and_plan_nudges(
        self, isolated_db, sid, monkeypatch
    ):
        calls: list[str] = []

        async def _memory(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("memory")

        async def _plan(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("plan")

        monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
        monkeypatch.setattr(nudge_mod, "_nudge_plan_extraction", _plan)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: True)
        monkeypatch.setattr(
            ce_core,
            "_get_and_reload_system_prompt",
            lambda session_id: "sys-prompt",
        )
        fake_db = _FakeStateRegister()
        fake_db.set_state(
            sid, nudge_mod._NUDGE_MEMORY_COUNT_KEY, nudge_mod._NUDGE_MEMORY_THRESHOLD - 1
        )
        monkeypatch.setattr(nudge_mod, "state_register_db", fake_db)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(), sid), sid
        )
        await _settle()

        assert calls == ["memory", "plan"]
        assert fake_db.get_state(sid, nudge_mod._NUDGE_MEMORY_COUNT_KEY, 0) == 0

    @pytest.mark.asyncio
    async def test_no_cut_does_not_dispatch_nudges(self, isolated_db, sid, monkeypatch):
        calls: list[str] = []

        async def _memory(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("memory")

        async def _plan(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("plan")

        monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
        monkeypatch.setattr(nudge_mod, "_nudge_plan_extraction", _plan)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: True)
        monkeypatch.setattr(Summarization, "_determine_cutoff", lambda self, messages: 0)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(turns=6, out_chars=10), sid), sid
        )
        await _settle()

        assert calls == []
