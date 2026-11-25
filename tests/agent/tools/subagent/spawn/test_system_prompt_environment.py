"""WorkMode stage 5: task-level environment injected into a child's system prompt.

The environment block is a task-level fact sheet (paths / stack / constraints /
credential REFERENCES). It must render when present, stay absent when empty
(byte-for-byte backward compatible), and never resolve a credential ref into a
secret.
"""

import pytest

from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import SubagentSessionRole

pytestmark = [pytest.mark.unit]


class TestTaskEnvironmentSection:
    def test_no_environment_means_no_section(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.LEAF, "Do X")
        assert "## Task Environment" not in prompt

    def test_empty_environment_means_no_section(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.LEAF, "Do X", environment={})
        assert "## Task Environment" not in prompt

    def test_environment_renders_key_value_facts(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            environment={
                "project_dir": "/repo/sherry_agent",
                "stack": "python3.13",
                "constraints": ["no network", "read-only"],
            },
        )
        assert "## Task Environment" in prompt
        assert "project_dir: /repo/sherry_agent" in prompt
        assert "stack: python3.13" in prompt
        # Non-string values are JSON-rendered, not repr-leaked.
        assert '["no network", "read-only"]' in prompt

    def test_environment_placed_after_role_before_rules(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            role_description="worker",
            environment={"project_dir": "/repo"},
        )
        assert prompt.index("## Your Role") < prompt.index("## Task Environment")
        assert prompt.index("## Task Environment") < prompt.index("## Rules")

    def test_credential_ref_is_kept_as_reference_not_secret(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            environment={
                "api_token": {"credential_ref": "keychain://svc/api-token"},
                "db_password": {"credential_ref": "keychain://svc/db"},
            },
        )
        assert "## Task Environment" in prompt
        assert "keychain://svc/api-token" in prompt
        assert "keychain://svc/db" in prompt
        # The block explicitly tells the child these are refs, not secrets.
        assert "Credential values are REFERENCES" in prompt
        assert "resolve them through the approved credential tool" in prompt
        # No resolved secret value ever appears: only the ref string is present.
        assert "api-token" in prompt  # part of the ref path
        assert prompt.count("keychain://svc/") == 2

    def test_no_credential_note_without_credential_refs(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            environment={"project_dir": "/repo"},
        )
        assert "## Task Environment" in prompt
        assert "Credential values are REFERENCES" not in prompt
