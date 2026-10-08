"""T1.1: FunctionalRole enum values and SubagentRunRecord.functional_role."""

import pytest

from agent.tools.subagent.types import CODE_INTEL_ROLES, FunctionalRole, SubagentRunRecord

pytestmark = [pytest.mark.unit]


class TestFunctionalRoleEnum:
    def test_enum_values(self):
        assert FunctionalRole.GENERAL == "general"
        assert FunctionalRole.RESEARCHER == "researcher"
        assert FunctionalRole.EXECUTOR == "executor"
        assert FunctionalRole.REVIEWER == "reviewer"
        assert FunctionalRole.LIBRARIAN == "librarian"

    def test_five_members(self):
        assert len(FunctionalRole) == 5

    def test_value_lookup(self):
        assert FunctionalRole("researcher") is FunctionalRole.RESEARCHER
        assert FunctionalRole("executor") is FunctionalRole.EXECUTOR
        assert FunctionalRole("librarian") is FunctionalRole.LIBRARIAN

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            FunctionalRole("wizard")


class TestCodeIntelRoles:
    def test_exactly_researcher_and_librarian(self):
        assert CODE_INTEL_ROLES == {FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN}

    def test_excludes_the_other_three_roles(self):
        assert FunctionalRole.GENERAL not in CODE_INTEL_ROLES
        assert FunctionalRole.EXECUTOR not in CODE_INTEL_ROLES
        assert FunctionalRole.REVIEWER not in CODE_INTEL_ROLES


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
