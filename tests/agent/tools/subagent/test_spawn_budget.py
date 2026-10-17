"""The per-session spawn budget: the counter, the admission refusal, and the purge reset.

The concurrent-child cap bounds children running AT A TIME; this budget bounds
"finish a child, start the next" repeating without end. The counter lives in the
registry (in-memory, process-local) and is incremented by ``register_run`` — a
run that reached registration is a real spawn.
"""

import pytest

from agent.tools.subagent.config import SubagentConfig, get_config, set_config
from agent.tools.subagent.registry import spawn_budget
from agent.tools.subagent.registry.session_keys import denormalize_session_key
from agent.tools.subagent.spawn.core import _validate_spawn_request
from agent.tools.subagent.spawn.depth import validate_total_spawns

pytestmark = [pytest.mark.unit]

_KEY = "agent:main:session:spawn-budget"


@pytest.fixture(autouse=True)
def _clean_budget():
    spawn_budget.reset_spawn_count(_KEY)
    yield
    spawn_budget.reset_spawn_count(_KEY)


@pytest.fixture()
def small_budget():
    """A config with a tiny ceiling, restored afterwards."""
    original = get_config()
    set_config(original.model_copy(update={"max_total_spawns_per_session": 2}))
    yield
    set_config(original)


class TestSpawnBudgetCounter:
    def test_increment_and_total(self):
        assert spawn_budget.total_spawns(_KEY) == 0

        assert spawn_budget.increment_spawn_count(_KEY) == 1
        assert spawn_budget.increment_spawn_count(_KEY) == 2

        assert spawn_budget.total_spawns(_KEY) == 2

    def test_reset_drops_the_count(self):
        spawn_budget.increment_spawn_count(_KEY)

        spawn_budget.reset_spawn_count(_KEY)

        assert spawn_budget.total_spawns(_KEY) == 0

    def test_the_purge_key_is_the_prefixed_session_key(self):
        """The purge path (a bare session id) must hit the same entry."""
        spawn_budget.increment_spawn_count(_KEY)

        spawn_budget.reset_spawn_count(denormalize_session_key("spawn-budget"))

        assert spawn_budget.total_spawns(_KEY) == 0


class TestTotalSpawnAdmission:
    def test_the_validator_reports_the_ceiling(self, small_budget):
        ok, reason = validate_total_spawns(1)
        assert ok and reason == ""

        ok, reason = validate_total_spawns(2)

        assert not ok
        assert "Total spawns 2" in reason and "max 2" in reason

    def test_a_session_below_the_budget_passes(self, small_budget):
        spawn_budget.increment_spawn_count(_KEY)

        child_depth, refusal = _validate_spawn_request("do a thing", "main", _KEY, get_config())

        assert refusal is None
        assert child_depth == 1

    def test_a_session_at_the_budget_is_refused(self, small_budget):
        for _ in range(2):
            spawn_budget.increment_spawn_count(_KEY)

        _, refusal = _validate_spawn_request("do a thing", "main", _KEY, get_config())

        assert refusal is not None
        assert refusal.status == "forbidden"
        assert "max 2" in refusal.error

    def test_the_budget_does_not_leak_across_sessions(self, small_budget):
        for _ in range(2):
            spawn_budget.increment_spawn_count(_KEY)

        _, refusal = _validate_spawn_request(
            "do a thing", "main", "agent:main:session:someone-else", get_config()
        )

        assert refusal is None

    def test_the_default_ceiling_is_generous(self):
        assert SubagentConfig().max_total_spawns_per_session == 50
