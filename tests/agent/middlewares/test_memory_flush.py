"""Unit + integration tests for P0-1 pre-compression Memory Flush.

Plan: ``TODO/P0-1_MEMORY_FLUSH_PLAN.md`` §6.1 (13 tests).

The flush extraction LLM is fully stubbed (zero network); ``MemoryStore`` is a
real instance backed by a per-test tmp directory so the on-disk MEMORY.md write
is actually exercised.
"""

import asyncio
import uuid

import pytest

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.memory_flush as memory_flush_module
import agent.tools.memory as memory_module
from agent.middlewares.summarization import Summarization
from agent.tools.memory import MemoryStore
from config.features import MEMORY_FLUSH

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal LLM response carrying both ``.content`` and ``.text``."""

    def __init__(self, content: str = "(none)") -> None:
        self.content = content
        self.text = content


class _StubFlushLLM:
    """Fake extraction LLM — returns a canned response or raises."""

    def __init__(self, content: str = "(none)", exc: Exception | None = None) -> None:
        self._content = content
        self._exc = exc

    def invoke(self, prompt, config=None):  # noqa: ANN001, ARG002
        if self._exc is not None:
            raise self._exc
        return _StubResponse(self._content)

    async def ainvoke(self, prompt, config=None):  # noqa: ANN001, ARG002
        if self._exc is not None:
            raise self._exc
        return _StubResponse(self._content)


def _flush_factory(content: str = "(none)", exc: Exception | None = None):
    """Return an ``llm_factory`` callable matching the production signature."""

    def factory(**kwargs):  # noqa: ANN003, ARG001
        return _StubFlushLLM(content, exc)

    return factory


class _StubSummaryModel:
    """Fake aux summary model (sync + async)."""

    _llm_type = "fake"

    def __init__(self) -> None:
        self.calls: list = []

    def invoke(self, prompt, config=None):  # noqa: ANN001, ARG002
        self.calls.append(prompt)
        return _StubResponse(
            "A sufficiently long deterministic summary of the conversation history "
            "covering decisions, files and next steps."
        )

    async def ainvoke(self, prompt, config=None):  # noqa: ANN001, ARG002
        self.calls.append(prompt)
        return _StubResponse(
            "A sufficiently long deterministic summary of the conversation history "
            "covering decisions, files and next steps."
        )


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_memory_dir(tmp_path, monkeypatch):
    """Point the real MemoryStore at an isolated tmp memory directory."""
    mem_dir = tmp_path / "memory"
    monkeypatch.setattr(memory_module, "MEMORY_DIR", mem_dir)
    return mem_dir


@pytest.fixture
def sid(request):
    s = "p01-" + request.node.name[:40] + "-" + uuid.uuid4().hex[:6]
    yield s
    try:
        from runtime import state_register_mem

        state_register_mem.clear_session(s)
    except Exception:  # noqa: S110
        pass


def _large_history(turns: int = 150, out_chars: int = 500) -> list:
    """Protected-tool history large enough that the summarization head is big."""
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i}"))
        messages.append(
            AIMessage(
                content=f"working {i}",
                tool_calls=[
                    {
                        "name": "memory",
                        "args": {"action": "read"},
                        "id": f"c{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(ToolMessage(content="x" * out_chars, tool_call_id=f"c{i}"))
    return messages


def _request(messages, session_id: str) -> ModelRequest:
    model = _StubSummaryModel()
    return ModelRequest(
        model=model,
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


# ===========================================================================
# should_flush
# ===========================================================================


def test_should_flush_below_threshold():
    messages = [HumanMessage(content="hi")]
    assert memory_flush_module.should_flush(messages, 100) is False


def test_should_flush_above_token_threshold():
    messages = [HumanMessage(content="hi")]
    assert memory_flush_module.should_flush(messages, 8_000) is True


def test_should_flush_above_char_threshold():
    messages = [HumanMessage(content="x" * 50_000)]
    assert memory_flush_module.should_flush(messages, 0) is True


def test_should_flush_disabled(monkeypatch):
    monkeypatch.setattr(
        memory_flush_module,
        "MEMORY_FLUSH",
        {**MEMORY_FLUSH, "enabled": False},
    )
    messages = [HumanMessage(content="x" * 50_000)]
    assert memory_flush_module.should_flush(messages, 999_999) is False


# ===========================================================================
# MemoryStore.append_entries
# ===========================================================================


def test_append_entries_dedup(tmp_memory_dir):
    store = MemoryStore()
    first = store.append_entries("§ alpha\n§ beta")
    assert first["success"] is True
    assert store.memory_entries == ["alpha", "beta"]

    second = store.append_entries("§ alpha\n§ gamma")
    assert second["success"] is True
    assert store.memory_entries == ["alpha", "beta", "gamma"]
    assert second["entry_count"] == 3
    assert "deduplicated 1" in second["message"]


def test_append_entries_truncation(tmp_memory_dir):
    store = MemoryStore(memory_char_limit=100)
    store.append_entries("§ " + "a" * 30 + "\n§ " + "b" * 30)
    store.append_entries("§ " + "c" * 30)
    assert store.memory_entries == ["a" * 30, "b" * 30, "c" * 30]

    store.append_entries("§ " + "d" * 30)
    assert store.memory_entries == ["b" * 30, "c" * 30, "d" * 30]


def test_append_entries_empty(tmp_memory_dir):
    store = MemoryStore()
    result = store.append_entries("   ")
    assert result["success"] is True
    assert result["message"] == "No entries to add."
    assert store.memory_entries == []


# ===========================================================================
# run_memory_flush
# ===========================================================================


def test_run_memory_flush_extracts_facts(tmp_memory_dir):
    store = MemoryStore()
    ok = asyncio.run(
        memory_flush_module.run_memory_flush(
            [HumanMessage(content="hi")],
            9_000,
            store,
            _flush_factory("§ Environment: Python 3.13\n§ Project: uses uv"),
        )
    )
    assert ok is True
    assert store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]


def test_run_memory_flush_no_facts(tmp_memory_dir):
    store = MemoryStore()
    ok = asyncio.run(
        memory_flush_module.run_memory_flush(
            [HumanMessage(content="hi")],
            9_000,
            store,
            _flush_factory("(none)"),
        )
    )
    assert ok is False
    assert store.memory_entries == []


def test_run_memory_flush_timeout(tmp_memory_dir):
    store = MemoryStore()
    ok = asyncio.run(
        memory_flush_module.run_memory_flush(
            [HumanMessage(content="hi")],
            9_000,
            store,
            _flush_factory(exc=TimeoutError("flush timed out")),
        )
    )
    assert ok is False
    assert store.memory_entries == []


def test_run_memory_flush_llm_error(tmp_memory_dir):
    store = MemoryStore()
    ok = asyncio.run(
        memory_flush_module.run_memory_flush(
            [HumanMessage(content="hi")],
            9_000,
            store,
            _flush_factory(exc=RuntimeError("provider down")),
        )
    )
    assert ok is False
    assert store.memory_entries == []


# ===========================================================================
# Compression integration (hook wiring)
# ===========================================================================


def test_compression_with_flush(tmp_memory_dir, sid):
    facts = "§ Environment: Python 3.13\n§ Project: uses uv"
    messages = _large_history()

    store = MemoryStore()
    mw = Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
        memory_store=store,
        llm_factory=_flush_factory(facts),
    )
    result = mw._apply_compression(_request(messages, sid), sid)
    written = (tmp_memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "Environment: Python 3.13" in written
    assert "Project: uses uv" in written
    assert len(result.messages) < len(messages)

    # Async compression path must mirror the sync hook.
    async_sid = sid + "-async"
    async_store = MemoryStore()
    mw_async = Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
        memory_store=async_store,
        llm_factory=_flush_factory(facts),
    )
    async_result = asyncio.run(
        mw_async._aapply_compression(_request(messages, async_sid), async_sid)
    )
    assert async_store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]
    assert len(async_result.messages) < len(messages)


def test_compression_without_flush_config(tmp_memory_dir, sid):
    messages = _large_history()
    mw = Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
    )
    assert mw._memory_store is None
    assert mw._llm_factory is None

    result = mw._apply_compression(_request(messages, sid), sid)
    assert len(result.messages) < len(messages)
    assert not (tmp_memory_dir / "MEMORY.md").exists()
