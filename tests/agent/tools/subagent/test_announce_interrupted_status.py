"""User-stopped children use the interrupted vocabulary, not failed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.tools.subagent.announce.completion_message import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
)
from agent.tools.subagent.announce.delivery import _resolve_builder_status
from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

pytestmark = [pytest.mark.unit]


def _run(status: RunOutcomeStatus, error: str | None = None):
    return SimpleNamespace(
        execution=SimpleNamespace(outcome=RunOutcome(status=status, error=error))
    )


def test_user_stop_maps_to_interrupted():
    assert _resolve_builder_status(_run(RunOutcomeStatus.KILLED, "user_stop")) == STATUS_INTERRUPTED


def test_other_kill_maps_to_failed():
    assert _resolve_builder_status(_run(RunOutcomeStatus.KILLED, "sessions_kill")) == STATUS_FAILED


def test_ok_maps_to_completed():
    assert _resolve_builder_status(_run(RunOutcomeStatus.OK)) == STATUS_COMPLETED
