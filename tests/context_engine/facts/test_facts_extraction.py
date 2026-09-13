"""P2-3: dual-watermark facts extraction over the tiered facts store.

Covers: cursor monotonicity + range validation, extraction parsing, the
at-least-once replay contract (crash between extract and advance_consumed),
and dedup behavior of the tiered store.
"""

import pytest
from langchain_core.messages import HumanMessage

import context_engine.facts.cursor as cursor
import context_engine.facts.extractor as extractor
from context_engine.facts.queue import enqueue_turn, process_pending
from context_engine.store import add_messages
from context_engine.store.core import get_max_turn_num
from context_engine.store import db as store_db
from agent.tools.memory import MemoryStore
from agent.tools.memory_tiered import TieredMemoryStore

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())

    state_db = tmp_path / "state_register.db"
    monkeypatch.setattr("runtime.state_register.state_register_db.db_path", state_db)
    from runtime import state_register_db as sdb

    monkeypatch.setattr(sdb, "db_path", state_db)
    sdb._init_db()
    return db_path


@pytest.fixture
def tiered(tmp_path):
    return TieredMemoryStore(memory_store=MemoryStore(), facts_dir=tmp_path / "facts")


class _StubLLM:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls: list[str] = []

    async def ainvoke(self, prompt):
        self.calls.append(prompt)
        return type("Resp", (), {"content": self.raw})()


def _stub_llm(monkeypatch, raw: str) -> _StubLLM:
    stub = _StubLLM(raw)
    monkeypatch.setattr(extractor, "_get_llm", lambda: stub)
    return stub


async def test_cursor_is_monotonic(isolated_db):
    sid = "p23-cursor"
    cursor.advance_enqueued(sid, 3)
    cursor.advance_enqueued(sid, 1)  # regression attempt is ignored
    assert cursor.get_cursor(sid)["enqueued_through_turn"] == 3

    cursor.advance_consumed(sid, 2)
    with pytest.raises(ValueError, match="enqueued"):
        cursor.advance_consumed(sid, 5)  # cannot consume beyond enqueue
    assert cursor.get_pending(sid) == (3, 3)


async def test_extraction_parses_stubbed_llm_output(isolated_db, tiered, monkeypatch):
    stub = _stub_llm(
        monkeypatch,
        '[{"category": "project", "fact": "uses uv for deps"}, '
        '{"category": "bogus", "fact": "falls back"}, {"fact": ""}]',
    )
    sid = "p23-parse"
    await add_messages(sid, [HumanMessage(content="we use uv now")])
    turn = get_max_turn_num(sid)
    await enqueue_turn(sid, turn)

    written = await process_pending(sid, tiered_store=tiered)

    assert written == 2  # valid fact + bogus-category fallback both land in project
    assert stub.calls
    facts = tiered.read_facts("project")
    assert "uses uv for deps" in facts.get("project", "")
    assert "falls back" in facts.get("project", "")


async def test_crash_replay_writes_once_and_advances(isolated_db, tiered, monkeypatch):
    sid = "p23-replay"
    await add_messages(sid, [HumanMessage(content="persist a key decision")])
    turn = get_max_turn_num(sid)
    await enqueue_turn(sid, turn)

    # First consumption crashes mid-way (LLM raises): watermark not advanced.
    failing = _stub_llm(monkeypatch, "not json at all {{")
    failing.raw = None  # type: ignore[assignment]
    with pytest.raises(Exception):
        await process_pending(sid, tiered_store=tiered)
    assert cursor.get_cursor(sid)["consumed_through_turn"] == 0

    # Recovery: a healthy extraction consumes the same range exactly once.
    _stub_llm(monkeypatch, '[{"category": "decisions", "fact": "adopt sqlite WAL"}]')
    written = await process_pending(sid, tiered_store=tiered)
    assert written == 1
    assert cursor.get_cursor(sid)["consumed_through_turn"] == turn

    # Replaying the queue after consumption is a no-op (no duplicate facts).
    assert await process_pending(sid, tiered_store=tiered) == 0
    facts = tiered.read_facts("decisions")
    assert facts.get("decisions", "").count("adopt sqlite WAL") == 1
    assert failing.calls  # the failing attempt did reach the extractor


async def test_no_pending_turns_is_noop(isolated_db, tiered):
    sid = "p23-empty"
    assert await process_pending(sid, tiered_store=tiered) == 0
    assert cursor.get_cursor(sid) == {
        "enqueued_through_turn": 0,
        "consumed_through_turn": 0,
    }
