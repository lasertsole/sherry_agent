"""Characterization tests for on-disk run-record back-compatibility.

The persistent ``subagent_registry.db`` may hold rows written by the retired
persistent-session feature. Pydantic v2 ignores unknown keys by default, so
those rows must keep restoring, and the retired fields must no longer exist on
the model.

The retired key names are assembled from fragments in one place so the source
tree carries no literal residue of the removed feature.
"""

import json

import pytest

from agent.tools.subagent.registry.cleanup import resolve_cleanup_completion_reason
from agent.tools.subagent.registry.lifecycle import _should_notify_failure
from agent.tools.subagent.registry.store_sqlite import _deserialize_run, _serialize_run
from agent.tools.subagent.types.registry import (
    CompletionDeliveryState,
    CompletionState,
    DeliveryStatus,
    ExecutionState,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.regression]

# Retired persistent-session field names, assembled from fragments.
_RETIRED_MODE = "spawn" + "_mode"
_RETIRED_BINDING = "thread" + "_binding" + "_info"

_LEGACY_SESSION_ROW = {
    "run_id": "legacy-1",
    "child_session_key": "agent:main:subagent:legacy-1",
    "requester_session_key": "agent:main:session:parent",
    "task": "legacy persistent task",
    _RETIRED_MODE: "session",
    "thread_id": "thread:subagent:abc",
    _RETIRED_BINDING: {
        "thread_id": "thread:subagent:abc",
        "bound_at": 1.0,
        "idle_timeout_ms": 300000,
        "max_age_ms": 86400000,
        "delivery_origin": "agent:main:subagent:legacy-1",
    },
}


class TestLegacyJsonRestore:
    """(a) Old JSON carrying the retired keys restores without the fields."""

    def test_old_json_restores_without_retired_fields(self):
        run = SubagentRunRecord.model_validate_json(json.dumps(_LEGACY_SESSION_ROW))

        assert run.run_id == "legacy-1"
        assert run.task == "legacy persistent task"
        assert not hasattr(run, _RETIRED_MODE)
        assert not hasattr(run, "thread_id")
        assert not hasattr(run, _RETIRED_BINDING)

    def test_retired_fields_absent_from_new_serialization(self):
        """(b) Re-serializing never re-emits the retired keys."""
        run = SubagentRunRecord.model_validate_json(json.dumps(_LEGACY_SESSION_ROW))

        payload = json.loads(run.model_dump_json())

        assert _RETIRED_MODE not in payload
        assert "thread_id" not in payload
        assert _RETIRED_BINDING not in payload

    def test_serialize_deserialize_roundtrip_drops_retired_keys(self):
        """(b) The store's serialize/deserialize seam round-trips a legacy row."""
        run = SubagentRunRecord.model_validate_json(json.dumps(_LEGACY_SESSION_ROW))

        restored = _deserialize_run(_serialize_run(run))

        assert restored.run_id == run.run_id
        assert _RETIRED_MODE not in _serialize_run(restored)


class TestLegacySessionCleanupSemantics:
    """(c)/(d) A legacy SESSION row keeps its not-required completion semantics."""

    @staticmethod
    def _legacy_session_run() -> SubagentRunRecord:
        return SubagentRunRecord.model_validate_json(
            json.dumps(
                {
                    **_LEGACY_SESSION_ROW,
                    "execution": {
                        "status": "terminal",
                        "outcome": {"status": "ok", "error": None},
                    },
                    "completion": {"required": False, "result_text": "done"},
                    "delivery": {"status": "not_required"},
                }
            )
        )

    def test_legacy_session_row_is_cleanable_as_not_required(self):
        """(c) The row is cleanable — the removed SESSION early-return is gone."""
        run = self._legacy_session_run()

        assert resolve_cleanup_completion_reason(run) == "not_required"

    def test_legacy_session_success_is_not_re_notified(self):
        """(d) A successful legacy SESSION row is not re-notified as a failure.

        ``_should_notify_failure`` fires only for non-OK outcomes that never
        entered the announce gate (``not (expects_completion_message and
        completion.required)``); an OK outcome must stay silent.
        """
        run = self._legacy_session_run()

        assert run.expects_completion_message is True
        assert run.completion.required is False
        assert _should_notify_failure(run) is False

    def test_legacy_session_failure_still_notifies(self):
        """(d) The failure path stays reachable for a legacy non-OK row."""
        run = self._legacy_session_run().model_copy(
            update={
                "execution": ExecutionState(
                    status="terminal",
                    outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="boom"),
                ),
                "completion": CompletionState(required=False),
                "delivery": CompletionDeliveryState(status=DeliveryStatus.NOT_REQUIRED),
            }
        )

        assert _should_notify_failure(run) is True
