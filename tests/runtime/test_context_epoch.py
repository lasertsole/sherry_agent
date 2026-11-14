"""Context epoch lifecycle (initialize / prepare / replace / advance)."""

import pytest

from runtime.session.state_register import ContextEpoch, StateRegisterDB

pytestmark = [pytest.mark.unit]


@pytest.fixture
def epoch(tmp_path):
    db = StateRegisterDB()
    db.db_path = tmp_path / "state_register.db"
    db._init_db()
    return ContextEpoch(db)


def test_initialize_then_prepare_returns_ok_snapshot(epoch):
    epoch.initialize("s1", {"prompt": "base", "tools": ["a"]})
    context, action = epoch.prepare("s1", {"prompt": "base", "tools": ["a"]}, None)
    assert action == "ok"
    assert context == {"prompt": "base", "tools": ["a"]}


def test_prepare_without_epoch_initializes_implicitly(epoch):
    context, action = epoch.prepare("s2", {"prompt": "fresh"}, None)
    assert action == "ok"
    assert context == {"prompt": "fresh"}


def test_prepare_reads_under_the_connection_lock(epoch, monkeypatch):
    """M21: the epoch SELECT runs inside ``_conn_lock`` like every writer."""
    epoch.initialize("locked", {"prompt": "base"})

    class _TrackingLock:
        def __init__(self, inner):
            self._inner = inner
            self.depth = 0

        def __enter__(self):
            self.depth += 1
            return self._inner.__enter__()

        def __exit__(self, *exc_info):
            self.depth -= 1
            return self._inner.__exit__(*exc_info)

    tracker = _TrackingLock(epoch._db._conn_lock)
    monkeypatch.setattr(epoch._db, "_conn_lock", tracker)
    original_init = epoch._db._init_db
    seen: list[int] = []

    def _init_db():
        seen.append(tracker.depth)
        return original_init()

    monkeypatch.setattr(epoch._db, "_init_db", _init_db)

    context, action = epoch.prepare("locked", {"prompt": "base"}, None)

    assert action == "ok"
    assert context == {"prompt": "base"}
    assert seen and all(depth > 0 for depth in seen), "the SELECT ran outside the lock"


def test_compaction_after_epoch_triggers_replace(epoch):
    epoch.initialize(
        "s3",
        {"prompt": "old baseline"},
    )
    epoch.replace("s3", {"prompt": "old baseline"}, seq=0)

    context, action = epoch.prepare("s3", {"prompt": "rebuilt baseline"}, latest_compaction_seq=5)
    assert action == "replace"
    assert context == {"prompt": "rebuilt baseline"}

    # The baseline itself moved with the replace.
    _, action2 = epoch.prepare("s3", {"prompt": "rebuilt baseline"}, None)
    assert action2 == "ok"


def test_context_change_without_compaction_triggers_reconcile(epoch):
    epoch.initialize("s4", {"prompt": "base"})
    context, action = epoch.prepare("s4", {"prompt": "base + new tool"}, None)
    assert action == "reconcile"
    assert context == {"prompt": "base + new tool"}

    # Snapshot now matches: next prepare is a no-op.
    _, action2 = epoch.prepare("s4", {"prompt": "base + new tool"}, None)
    assert action2 == "ok"
