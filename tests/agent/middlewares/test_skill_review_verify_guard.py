"""skill_review must not fire for a plan whose acceptance was skipped.

A cancelled SYSTEM verify todo means the plan finished WITHOUT passing
acceptance (the reviewer budget was exhausted, or the model abandoned it).
Extracting/upgrading a skill from such a plan would teach from failure.
"""

from __future__ import annotations

import pytest

from agent.middlewares.summarization import background_reviews as br
from agent.tools.todolist.registry import store_sqlite as todo_store

pytestmark = [pytest.mark.unit]

_SID = "sess-skill-review-guard"


class _FakeState:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.values.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.values[(session_id, key)] = value
        return True


@pytest.fixture()
def state(monkeypatch: pytest.MonkeyPatch) -> _FakeState:
    fake = _FakeState()
    monkeypatch.setattr(br, "state_register_db", fake)
    return fake


def _seed(monkeypatch: pytest.MonkeyPatch, todos: list[dict]) -> None:
    monkeypatch.setattr(todo_store, "get_todos_sync", lambda _sid: list(todos))


def _verify(status: str) -> dict:
    return {
        "content": "[VERIFY] run acceptance",
        "status": status,
        "metadata": {"kind": "verify", "auto_inserted": True},
    }


def _work(status: str) -> dict:
    return {"content": "work", "status": status, "metadata": {}}


def test_a_completed_verify_allows_skill_review(monkeypatch, state):
    _seed(monkeypatch, [_work("completed"), _work("completed"), _verify("completed")])

    assert br._detect_todo_all_complete(_SID) is True


def test_a_cancelled_verify_blocks_skill_review(monkeypatch, state):
    _seed(monkeypatch, [_work("completed"), _work("completed"), _verify("cancelled")])

    assert br._detect_todo_all_complete(_SID) is False
    assert state.get_state(_SID, br._SKILL_REVIEW_FIRED_KEY, False) is False


def test_a_plan_without_a_verify_keeps_the_old_behavior(monkeypatch, state):
    _seed(monkeypatch, [_work("completed"), _work("completed")])

    assert br._detect_todo_all_complete(_SID) is True


def test_an_open_work_todo_still_blocks(monkeypatch, state):
    _seed(monkeypatch, [_work("pending"), _verify("completed")])

    assert br._detect_todo_all_complete(_SID) is False


def _linked_verify(status: str, subagent_id: str = "reviewer-1") -> dict:
    return {**_verify(status), "subagent_id": subagent_id}


def _reviewer_outcome(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    # The detector (and its lazy registry read) live in the runner module;
    # ``background_reviews`` only re-exports the detector.
    from agent.middlewares.summarization import skill_review_runner as srr

    monkeypatch.setattr(srr, "_verify_reviewer_outcome", lambda _todo: value)


def test_a_completed_verify_whose_reviewer_did_not_pass_blocks(monkeypatch, state):
    """``budget_exhausted`` is not a pass: the model ticked the box, the run did not."""
    _seed(monkeypatch, [_work("completed"), _linked_verify("completed")])
    _reviewer_outcome(monkeypatch, "budget_exhausted")

    assert br._detect_todo_all_complete(_SID) is False
    assert state.get_state(_SID, br._SKILL_REVIEW_FIRED_KEY, False) is False


@pytest.mark.parametrize("outcome", ["error", "timeout", "killed", "unknown"])
def test_every_non_ok_reviewer_outcome_blocks(monkeypatch, state, outcome):
    _seed(monkeypatch, [_work("completed"), _linked_verify("completed")])
    _reviewer_outcome(monkeypatch, outcome)

    assert br._detect_todo_all_complete(_SID) is False


def test_an_ok_reviewer_allows_skill_review(monkeypatch, state):
    _seed(monkeypatch, [_work("completed"), _linked_verify("completed")])
    _reviewer_outcome(monkeypatch, "ok")

    assert br._detect_todo_all_complete(_SID) is True


def test_an_unknown_reviewer_outcome_stays_fail_open(monkeypatch, state):
    """No run / no outcome (legacy rows) keeps the previous behaviour."""
    _seed(monkeypatch, [_work("completed"), _linked_verify("completed")])
    _reviewer_outcome(monkeypatch, None)

    assert br._detect_todo_all_complete(_SID) is True


def test_an_old_cancelled_verify_does_not_poison_a_later_plan(monkeypatch, state):
    _seed(
        monkeypatch,
        [
            _work("completed"),
            _verify("cancelled"),  # an earlier abandoned plan
            _work("completed"),
            _verify("completed"),  # the current plan passed
        ],
    )

    assert br._detect_todo_all_complete(_SID) is True
