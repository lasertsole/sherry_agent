import pytest
from future_subagent.config import SubagentConfig, get_config, set_config


class TestSubagentConfig:
    def test_defaults(self):
        c = SubagentConfig()
        assert c.max_spawn_depth == 3
        assert c.max_children_per_agent == 5
        assert c.run_timeout_seconds == 300.0
        assert c.require_agent_id is False
        assert c.allow_agents == ["*"]
        assert c.default_cleanup == "delete"
        assert c.announce_retry_max == 3
        assert c.delivery_suspend_soft_cap == 25
        assert c.delivery_suspend_hard_cap == 50
        assert c.sweeper_interval_seconds == 60

    def test_custom(self):
        c = SubagentConfig(max_spawn_depth=5, max_children_per_agent=10)
        assert c.max_spawn_depth == 5
        assert c.max_children_per_agent == 10

    def test_get_set_config(self):
        original = get_config()
        custom = SubagentConfig(max_spawn_depth=7)
        set_config(custom)
        assert get_config().max_spawn_depth == 7
        set_config(original)
        assert get_config().max_spawn_depth == original.max_spawn_depth
