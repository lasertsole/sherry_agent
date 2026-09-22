"""T1.1: FunctionalRole enum values and SubagentRunRecord.functional_role."""

import pytest

from agent.tools.subagent.types import FunctionalRole, SubagentRunRecord

pytestmark = [pytest.mark.unit]


class TestFunctionalRoleEnum:
    def test_enum_values(self):
        assert FunctionalRole.GENERAL == "general"
        assert FunctionalRole.RESEARCHER == "researcher"
        assert FunctionalRole.EXECUTOR == "executor"
        assert FunctionalRole.REVIEWER == "reviewer"

    def test_value_lookup(self):
        assert FunctionalRole("researcher") is FunctionalRole.RESEARCHER
        assert FunctionalRole("executor") is FunctionalRole.EXECUTOR

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            FunctionalRole("wizard")


class TestRunRecordFunctionalRole:
    def test_defaults_to_general(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:subagent:x",
            requester_session_key="agent:main:session:p",
            task="t",
        )
        assert run.functional_role is FunctionalRole.GENERAL

    def test_legacy_payload_without_field_defaults_to_general(self):
        payload = {
            "run_id": "r1",
            "child_session_key": "agent:main:subagent:x",
            "requester_session_key": "agent:main:session:p",
            "task": "t",
        }
        run = SubagentRunRecord.model_validate(payload)
        assert run.functional_role is FunctionalRole.GENERAL

    def test_explicit_role_roundtrips(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:subagent:x",
            requester_session_key="agent:main:session:p",
            task="t",
            functional_role=FunctionalRole.REVIEWER,
        )
        restored = SubagentRunRecord.model_validate(run.model_dump())
        assert restored.functional_role is FunctionalRole.REVIEWER
