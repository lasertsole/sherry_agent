"""Locks the removal of compression-time persistence.

T1 (preflight), T2 (pre-call) and T3 (post-response) each run a real
compaction through the public trigger entry points and must not write to the
message store. ``context_engine.store.core._persist_batch`` is the single
funnel behind both ``add_messages`` and ``add_messages_sync``; the spy records
zero calls, and the ``messages`` table stays empty. ``_build_new_messages`` is
called only inside the ``cutoff > 0`` replacement branch — spying on it proves
the branch that used to flush the discarded prefix really executed.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

import agent.middlewares.summarization.core as summarization_module
from context_engine.store import core as store_core
from context_engine.store import db as store_db
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

CTX_WINDOW = 41600
TRIGGER_TOKENS = 80000


class StubModel:
    _llm_type = "fake"

    def invoke(self, prompt, config=None):  # noqa: ARG002
        return SimpleNamespace(text=self._summary())

    async def ainvoke(self, prompt, config=None):  # noqa: ARG002
        return SimpleNamespace(text=self._summary())

    @staticmethod
    def _summary() -> str:
        return "A sufficiently long deterministic summary of the conversation history."


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    value = "cmp-nopersist-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield value
    state_register_mem.clear_session(value)


@pytest.fixture
def persist_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        store_core,
        "_persist_batch",
        lambda session_id, pending: calls.append(session_id),
    )
    return calls


@pytest.fixture
def build_calls(monkeypatch):
    calls: list[str] = []
    real = summarization_module.Summarization._build_new_messages

    def _spy(self, summary):
        calls.append(summary)
        return real(self, summary)

    monkeypatch.setattr(summarization_module.Summarization, "_build_new_messages", _spy)
    return calls


def _chat_history(turns: int, span: int) -> list:
    """Human/AI-only history: no truncatable tool results → compact route."""
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"q{i} " + "q" * span, id=f"h{i}"))
        messages.append(AIMessage(content=f"a{i} " + "a" * span, id=f"a{i}"))
    return messages


def _make_middleware() -> summarization_module.Summarization:
    return summarization_module.Summarization(
        model=StubModel(),
        trigger=[("tokens", TRIGGER_TOKENS)],
        keep=("messages", 10),
        main_llm_context_window=CTX_WINDOW,
    )


def _request(messages: list, session_id: str) -> ModelRequest:
    return ModelRequest(
        model=StubModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _row_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


@contextlib.contextmanager
def _capture_logs(level: str = "INFO"):
    lines: list[str] = []
    handler_id = logger.add(lines.append, level=level, format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


class TestCompressionDoesNotPersist:
    def test_t1_preflight_compaction_writes_nothing(
        self, isolated_db, sid, persist_calls, build_calls
    ):
        messages = _chat_history(turns=60, span=800)
        middleware = _make_middleware()

        with _capture_logs() as lines:
            result = middleware.before_agent({"session_id": sid, "messages": messages}, None)

        assert result is not None
        assert any("trigger=T1" in line and "route=compact" in line for line in lines)
        assert build_calls
        assert persist_calls == []
        assert _row_count(sid) == 0

    def test_t2_precall_compaction_writes_nothing(
        self, isolated_db, sid, persist_calls, build_calls
    ):
        messages = _chat_history(turns=60, span=800)
        middleware = _make_middleware()
        captured: dict[str, list] = {}

        def handler(request):
            captured["messages"] = list(request.messages)
            return AIMessage(content="ok")

        with _capture_logs() as lines:
            response = middleware.wrap_model_call(_request(messages, sid), handler)

        assert response.content == "ok"
        assert len(captured["messages"]) < len(messages)
        assert any("trigger=T2" in line and "route=compact" in line for line in lines)
        assert build_calls
        assert persist_calls == []
        assert _row_count(sid) == 0

    @pytest.mark.asyncio
    async def test_t2_async_precall_compaction_writes_nothing(
        self, isolated_db, sid, persist_calls, build_calls
    ):
        messages = _chat_history(turns=60, span=800)
        middleware = _make_middleware()
        captured: dict[str, list] = {}

        async def ahandler(request):
            captured["messages"] = list(request.messages)
            return AIMessage(content="ok")

        with _capture_logs() as lines:
            response = await middleware.awrap_model_call(_request(messages, sid), ahandler)

        assert response.content == "ok"
        assert len(captured["messages"]) < len(messages)
        assert any("trigger=T2" in line and "route=compact" in line for line in lines)
        assert build_calls
        assert persist_calls == []
        assert _row_count(sid) == 0

    def test_t3_post_response_compaction_writes_nothing(
        self, isolated_db, sid, persist_calls, build_calls
    ):
        # est ~16000 < threshold_truncate -> T2 no-op; reported 30000 -> T3 compact.
        messages = _chat_history(turns=40, span=800)
        middleware = _make_middleware()
        response = ModelResponse(
            result=[
                AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 30000,
                        "output_tokens": 100,
                        "total_tokens": 30100,
                    },
                )
            ]
        )

        with _capture_logs() as lines:
            returned = middleware.wrap_model_call(_request(messages, sid), lambda request: response)

        assert returned is response
        assert any("trigger=T3" in line and "route=compact" in line for line in lines)
        assert build_calls
        assert persist_calls == []
        assert _row_count(sid) == 0
