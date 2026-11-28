"""SQLite Store, read queries, transaction and migration tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from superagent.app.migration import SCHEMA_VERSION, create_schema_10_fixture
from superagent.app.store import Store, StoreLockedError
from superagent.core.ids import thread_key
from superagent.core.model import (
    Actor,
    Channel,
    DecisionOutcome,
    Effect,
    Eval,
    Input,
    Reason,
    Receipt,
    Report,
    ReportKind,
    SourceRef,
    Subscription,
    to_dict,
)
from superagent.kv import KV, KVEntry
from tests.superagent.conftest import (
    make_receipt,
    make_report,
    make_ticket,
    make_thread,
    make_trigger,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Return a fresh state directory."""
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def store(state_dir: Path) -> Store:
    """Return an open Store and close it after the test."""
    instance = Store(state_dir / "orchestrator.sqlite", state_dir)
    yield instance
    instance.close()


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def test_schema_11_tables_generated_columns_and_indexes(store: Store, state_dir: Path) -> None:
    """The Store creates the exact schema-11 surface."""
    assert store.schema_version() == SCHEMA_VERSION == 11
    conn = sqlite3.connect(state_dir / "orchestrator.sqlite")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "trigger",
        "disposition",
        "trigger_thread",
        "thread",
        "ticket",
        "decision",
        "effect",
        "outbox",
        "kv",
    } <= tables
    indexes = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "ix_trigger_thread_thread",
        "ix_thread_active",
        "ix_thread_subscribed",
        "ix_ticket_open",
        "ix_ticket_active",
        "ix_decision_thread",
        "ix_effect_due",
        "ix_outbox_ticket_conv",
        "ix_outbox_dropped",
    } <= indexes
    thread_cols = {row[1] for row in conn.execute("PRAGMA table_xinfo(thread)")}
    ticket_cols = {row[1] for row in conn.execute("PRAGMA table_xinfo(ticket)")}
    assert {"has_queued", "has_eval", "is_active", "is_subscribed"} <= thread_cols
    assert {
        "ended_at",
        "is_open",
        "latest_assignment_status",
        "has_undelivered_input",
        "is_active",
    } <= ticket_cols
    conn.close()


def test_thread_generated_active_column(store: Store, state_dir: Path) -> None:
    """Thread activity follows queue/Eval flags."""
    thread = make_thread()
    store.threads.save(thread)
    conn = sqlite3.connect(state_dir / "orchestrator.sqlite")

    def active() -> int:
        return int(
            _scalar(conn, "SELECT is_active FROM thread WHERE thread_key = ?", (thread.key,))
        )

    assert active() == 0
    thread.queued_trigger_ids.append("trig-1")
    store.threads.save(thread)
    assert active() == 1
    thread.queued_trigger_ids.clear()
    thread.eval = Eval(
        id="eval-1",
        thread=SourceRef(source="github", id="thread-1"),
        primary="trig-1",
        trigger_ids=["trig-1"],
    )
    store.threads.save(thread)
    assert active() == 1
    conn.close()


def test_ticket_generated_columns(store: Store, state_dir: Path) -> None:
    """Ticket open/active follow explicit state columns."""
    ticket = make_ticket()
    ticket.inputs.append(
        Input(
            id="input-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-1",
            text="hello",
        )
    )
    store.tickets.save(ticket)
    conn = sqlite3.connect(state_dir / "orchestrator.sqlite")
    assert _scalar(conn, "SELECT is_open FROM ticket WHERE id = ?", (ticket.id,)) == 1
    assert _scalar(conn, "SELECT is_active FROM ticket WHERE id = ?", (ticket.id,)) == 1
    ticket.ended = {"at": "2026-11-28T02:00:00+00:00", "by": "person"}
    store.tickets.save(ticket)
    assert _scalar(conn, "SELECT is_open FROM ticket WHERE id = ?", (ticket.id,)) == 0
    assert _scalar(conn, "SELECT is_active FROM ticket WHERE id = ?", (ticket.id,)) == 0
    conn.close()


def test_repository_crud_round_trip(store: Store) -> None:
    """Every repository can save and reload its domain object."""
    trigger = make_trigger()
    store.triggers.save(trigger)
    store.trigger_threads.save(trigger.id, trigger.threads[0], trigger.at, trigger.human)
    assert store.triggers.get(trigger.id) == trigger
    assert store.triggers.exists(trigger.id) is True

    thread = make_thread()
    thread.subscriptions.append(Subscription(ticket_id="ticket-1", thread=trigger.threads[0]))
    thread.queued_trigger_ids.append(trigger.id)
    thread.eval = Eval(
        id="eval-1",
        thread=trigger.threads[0],
        primary=trigger.id,
        trigger_ids=[trigger.id],
    )
    store.threads.save(thread)
    assert store.threads.get(thread.key) == thread

    ticket = make_ticket()
    ticket.latest_report = make_report(ReportKind.READY)
    ticket.assignments[0].latest_report = ticket.latest_report
    store.tickets.save(ticket)
    assert store.tickets.get(ticket.id) == ticket

    from superagent.core.model import Decision

    decision = Decision(
        id="dec-1",
        thread=trigger.threads[0],
        outcome=DecisionOutcome.ACCEPT,
        reason=Reason(code="startedTicket", values={"ticket_id": ticket.id}),
        at="2026-11-28T00:01:00+00:00",
        trigger_ids=[trigger.id],
    )
    store.decisions.save(decision)
    assert store.decisions.get(decision.id) == decision
    assert store.decisions.list(thread_key(trigger.threads[0])) == [decision]

    from superagent.core.model import Effect

    effect = Effect(kind="reply", key="msg-1", payload={"text": "hello"})
    store.effects.save(effect)
    first = store.effects.get("reply", "msg-1")
    assert first is not None
    effect.last_error = "temporary"
    store.effects.save(effect)
    second = store.effects.get("reply", "msg-1")
    assert second is not None
    assert second.seq == first.seq
    assert second.last_error == "temporary"

    receipt = make_receipt()
    store.receipts.save(receipt)
    assert store.receipts.get(receipt.id) == receipt
    assert store.receipts.list(ticket.id) == [receipt]


def test_kv_and_atomic_callbacks(store: Store) -> None:
    """KV values persist and commit callbacks obey rollback."""
    store.kv_set(KV.SCHEMA, SCHEMA_VERSION)
    assert store.kv_get(KV.SCHEMA) == SCHEMA_VERSION
    custom = KVEntry("source", "github-cursor", str)
    store.kv_set(custom, "cursor-1")
    assert store.kv_get(custom) == "cursor-1"

    calls: list[str] = []
    with store.atomic as tx:
        tx.after_commit(lambda: calls.append("committed"))
        store.triggers.save(make_trigger("trig-ok"))
    assert calls == ["committed"]
    assert store.triggers.exists("trig-ok") is True

    with pytest.raises(RuntimeError):
        with store.atomic as tx:
            tx.after_commit(lambda: calls.append("discarded"))
            store.triggers.save(make_trigger("trig-bad"))
            raise RuntimeError("rollback")
    assert store.triggers.exists("trig-bad") is False
    assert calls == ["committed"]


def test_nested_atomic_rollback_and_commit(store: Store) -> None:
    """Nested frames use savepoints and defer callbacks to the outer commit."""
    calls: list[str] = []
    with store.atomic as outer:
        outer.after_commit(lambda: calls.append("outer"))
        store.triggers.save(make_trigger("trig-outer"))
        with pytest.raises(ValueError):
            with store.atomic as inner:
                inner.after_commit(lambda: calls.append("inner-bad"))
                store.triggers.save(make_trigger("trig-inner-bad"))
                raise ValueError("inner")
        assert store.triggers.exists("trig-inner-bad") is False
        with store.atomic as inner:
            inner.after_commit(lambda: calls.append("inner-ok"))
            store.triggers.save(make_trigger("trig-inner-ok"))

    assert store.triggers.exists("trig-inner-ok") is True
    assert calls == ["outer", "inner-ok"]


def test_store_read_queries(store: Store) -> None:
    """Read queries use indexes and return decoded domain objects."""
    first = make_trigger("trig-1", at="2026-11-28T00:00:00+00:00", text="need alpha")
    second = make_trigger("trig-2", at="2026-11-28T00:01:00+00:00", text="need beta")
    for trigger in (first, second):
        store.triggers.save(trigger)
        store.trigger_threads.save(trigger.id, trigger.threads[0], trigger.at, trigger.human)
    thread = make_thread()
    thread.key = thread_key(first.threads[0])
    from superagent.core.model import Subscription

    thread.subscriptions.append(Subscription(ticket_id="ticket-1", thread=first.threads[0]))
    store.threads.save(thread)

    assert [item.id for item in store.read.thread_triggers(thread.key)] == ["trig-1", "trig-2"]
    assert [
        item.id
        for item in store.read.thread_triggers(
            thread.key, cursor=("2026-11-28T00:00:00+00:00", "trig-1")
        )
    ] == ["trig-2"]
    stats = store.read.trigger_stats("github", "2026-11-27T00:00:00+00:00")
    assert stats == {"total": 2, "human": 2, "bot": 0}
    assert {item.id for item in store.read.search("alpha")} == {"trig-1"}
    assert store.read.subscribed_threads("github") == [thread.key]

    ticket = make_ticket()
    ticket.inputs.append(
        Input(
            id="input-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-1",
            text="hello",
        )
    )
    store.tickets.save(ticket)
    assert [item.id for item in store.read.tickets_open()] == [ticket.id]
    assert [item.id for item in store.read.tickets_active()] == [ticket.id]

    receipt = make_receipt()
    store.receipts.save(receipt)
    failing = make_receipt("msg-2")
    failing.attempts = 1
    store.receipts.save(failing)
    dropped = make_receipt("msg-3")
    dropped.status = "dropped"
    dropped.dropped = {"at": "2026-11-28T03:00:00+00:00", "reason": "channelGone"}
    store.receipts.save(dropped)
    assert [item.id for item in store.read.failing_replies()] == ["msg-2"]
    assert [
        item.id for item in store.read.dropped_replies("github", "2026-11-28T00:00:00+00:00")
    ] == ["msg-3"]
    assert [item.id for item in store.read.dropped_since("2026-11-28T00:00:00+00:00")] == ["msg-3"]


def test_second_store_on_same_state_dir_is_rejected(state_dir: Path) -> None:
    """The advisory lock rejects a second owner of the same state directory."""
    db = state_dir / "orchestrator.sqlite"
    with Store(db, state_dir):
        with pytest.raises(StoreLockedError):
            Store(db, state_dir)


def test_schema_10_to_11_migration(state_dir: Path) -> None:
    """A schema-10 database is rebuilt with explicit columns and preserved rows."""
    db = state_dir / "legacy.sqlite"
    conn = sqlite3.connect(db)
    create_schema_10_fixture(conn)

    trigger = make_trigger()
    conn.execute(
        "INSERT INTO trigger(id, source, actor_json, human, addressed, at, text, channel_json, "
        "url, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trigger.id,
            trigger.source,
            json.dumps(to_dict(trigger.actor)),
            1,
            None,
            trigger.at,
            trigger.text,
            json.dumps(to_dict(trigger.channel)),
            trigger.url,
            json.dumps(to_dict(trigger.meta)),
        ),
    )
    thread = make_thread()
    conn.execute(
        "INSERT INTO thread(thread_key, source, thread_id, subscriptions_json, context_json, "
        "queued_trigger_ids_json, eval_id, expired_count, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thread.key,
            thread.source,
            thread.id,
            json.dumps([to_dict(Subscription(ticket_id="ticket-1", thread=trigger.threads[0]))]),
            None,
            json.dumps(["trig-1"]),
            "eval-1",
            0,
            None,
        ),
    )
    ticket = make_ticket()
    ticket.inputs.append(
        Input(
            id="input-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-1",
            text="hello",
        )
    )
    conn.execute(
        "INSERT INTO ticket(id, origin_json, mandate_json, execution_json, channels_json, "
        "inputs_json, follows_json, assignments_json, latest_report_json, ended_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticket.id,
            json.dumps(to_dict(ticket.origin)),
            json.dumps(to_dict(ticket.mandate)),
            json.dumps(to_dict(ticket.execution)),
            json.dumps(to_dict(ticket.channels)),
            json.dumps(to_dict(ticket.inputs)),
            json.dumps(to_dict(ticket.follows)),
            json.dumps(to_dict(ticket.assignments)),
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()

    with Store(db, state_dir / "migration-lock") as migrated:
        assert migrated.schema_version() == 11
        assert migrated.kv_get(KV.SCHEMA) == 11
        migrated_thread = migrated.threads.get(thread.key)
        assert migrated_thread is not None
        assert migrated_thread.has_queued is True
        assert migrated_thread.has_eval is True
        assert migrated_thread.is_subscribed is True
        migrated_ticket = migrated.tickets.get(ticket.id)
        assert migrated_ticket is not None
        assert migrated_ticket.has_undelivered_input is True
        assert migrated_ticket.latest_assignment_status == "pending"
        assert migrated.triggers.get(trigger.id) is not None


def test_kv_literals_have_a_single_registry() -> None:
    """KV key literals appear only in kv.py and migration.py."""
    literals = ("schema-version", "settings-revision", "prompt-revision", "master-id")
    allowed = {
        REPO_ROOT / "superagent" / "kv.py",
        REPO_ROOT / "superagent" / "app" / "migration.py",
    }
    for path in (REPO_ROOT / "superagent").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text:
                assert path in allowed, f"{literal} leaked into {path}"


def test_effect_and_receipt_imports_are_used() -> None:
    """Dataclass re-exports remain importable for downstream phases."""
    assert Effect is not None
    assert Receipt is not None
    assert Report is not None
    assert Actor is not None
    assert Channel is not None
