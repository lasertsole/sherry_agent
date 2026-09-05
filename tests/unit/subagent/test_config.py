import pytest
from pydantic import ValidationError

from agent.tools.subagent.config import SubagentConfig, get_config, set_config


class TestSubagentConfig:
    def test_defaults(self):
        c = SubagentConfig()
        assert c.max_spawn_depth == 2
        assert c.max_children_per_agent == 5
        assert c.run_timeout_seconds == 0.0
        assert c.require_agent_id is False
        assert c.allow_agents == ["*"]
        assert c.default_cleanup == "delete"
        assert c.announce_retry_max == 3
        assert c.delivery_suspend_soft_cap == 25
        assert c.delivery_suspend_hard_cap == 50
        assert c.sweeper_interval_seconds == 60
        assert c.max_concurrent == 8
        assert c.archive_after_minutes == 60

    def test_custom(self):
        c = SubagentConfig(max_spawn_depth=2, max_children_per_agent=10, max_concurrent=10)
        assert c.max_spawn_depth == 2
        assert c.max_children_per_agent == 10
        assert c.max_concurrent == 10

    def test_get_set_config(self):
        original = get_config()
        custom = SubagentConfig(max_concurrent=7)
        set_config(custom)
        assert get_config().max_concurrent == 7
        set_config(original)
        assert get_config().max_concurrent == original.max_concurrent

    def test_max_spawn_depth_cap_rejected(self):
        with pytest.raises(ValidationError):
            SubagentConfig(max_spawn_depth=3)

    def test_max_spawn_depth_assignment_cap_rejected(self):
        cfg = SubagentConfig()
        with pytest.raises(ValidationError):
            cfg.max_spawn_depth = 3

    def test_max_spawn_depth_at_cap_ok(self):
        from agent.tools.subagent.config import MAX_SPAWN_DEPTH_CAP

        c = SubagentConfig(max_spawn_depth=2)
        assert c.max_spawn_depth == 2
        assert MAX_SPAWN_DEPTH_CAP == 2
