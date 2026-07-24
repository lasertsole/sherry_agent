import pytest
import asyncio
from future_subagent.swarm.fifo import SwarmFifoQueue
from future_subagent.swarm.collector import (
    configure_swarm_group,
    get_group_config,
    reserve_swarm_run,
    activate_swarm_run,
    complete_swarm_run,
    build_structured_output_prompt,
)
from future_subagent.types.swarm import SwarmMode, SwarmRunState, SwarmGroupConfig
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    from future_subagent.swarm import collector as _collector
    _collector._group_configs.clear()
    from future_subagent.swarm.fifo import get_fifo
    get_fifo()._queues.clear()
    yield
    clear()
    _collector._group_configs.clear()
    get_fifo()._queues.clear()


class TestSwarmFifoQueue:
    @pytest.mark.asyncio
    async def test_enqueue_dequeue(self):
        q = SwarmFifoQueue()
        await q.enqueue("g1", "r1")
        await q.enqueue("g1", "r2")
        assert await q.dequeue("g1") == "r1"
        assert await q.dequeue("g1") == "r2"

    @pytest.mark.asyncio
    async def test_dequeue_empty(self):
        q = SwarmFifoQueue()
        assert await q.dequeue("g1") is None

    @pytest.mark.asyncio
    async def test_size(self):
        q = SwarmFifoQueue()
        assert q.size("g1") == 0
        await q.enqueue("g1", "r1")
        assert q.size("g1") == 1
        await q.enqueue("g1", "r2")
        assert q.size("g1") == 2

    @pytest.mark.asyncio
    async def test_remove(self):
        q = SwarmFifoQueue()
        await q.enqueue("g1", "r1")
        await q.enqueue("g1", "r2")
        assert await q.remove("g1", "r1") is True
        assert q.size("g1") == 1

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self):
        q = SwarmFifoQueue()
        assert await q.remove("g1", "r1") is False

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        q = SwarmFifoQueue()
        for i in range(5):
            await q.enqueue("g1", f"r{i}")
        for i in range(5):
            assert await q.dequeue("g1") == f"r{i}"

    @pytest.mark.asyncio
    async def test_independent_groups(self):
        q = SwarmFifoQueue()
        await q.enqueue("g1", "r1")
        await q.enqueue("g2", "r2")
        assert q.size("g1") == 1
        assert q.size("g2") == 1
        assert await q.dequeue("g1") == "r1"
        assert await q.dequeue("g2") == "r2"


class TestSwarmTypes:
    def test_swarm_mode_values(self):
        assert SwarmMode.COLLECT == "collect"
        assert SwarmMode.DISTRIBUTE == "distribute"

    def test_swarm_run_state_values(self):
        assert SwarmRunState.RESERVED == "reserved"
        assert SwarmRunState.ACTIVE == "active"
        assert SwarmRunState.COMPLETED == "completed"
        assert SwarmRunState.FAILED == "failed"

    def test_group_config_defaults(self):
        cfg = SwarmGroupConfig(group_id="g1")
        assert cfg.max_children_per_group == 5
        assert cfg.max_concurrent == 3
        assert cfg.output_schema is None
        assert cfg.fifo_queue is True

    def test_group_config_custom(self):
        cfg = SwarmGroupConfig(
            group_id="g1",
            max_children_per_group=10,
            max_concurrent=5,
            output_schema={"type": "object"},
            fifo_queue=False,
        )
        assert cfg.max_children_per_group == 10
        assert cfg.output_schema == {"type": "object"}


class TestConfigureSwarmGroup:
    def test_configure_and_get(self):
        cfg = SwarmGroupConfig(group_id="test-group", max_concurrent=2)
        configure_swarm_group(cfg)
        result = get_group_config("test-group")
        assert result is not None
        assert result.max_concurrent == 2

    def test_get_nonexistent(self):
        assert get_group_config("nonexistent") is None


class TestReserveSwarmRun:
    @pytest.mark.asyncio
    async def test_reserve_success(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        assert run is not None
        assert run.swarm_group_id == "g1"
        assert run.swarm_run_state == SwarmRunState.RESERVED.value
        assert run.task == "task1"

    @pytest.mark.asyncio
    async def test_reserve_unknown_group(self):
        run = await reserve_swarm_run("unknown", "task1", "agent:main:session:p1")
        assert run is None

    @pytest.mark.asyncio
    async def test_reserve_at_capacity(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1", max_children_per_group=1))
        run1 = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        assert run1 is not None
        run2 = await reserve_swarm_run("g1", "task2", "agent:main:session:p1")
        assert run2 is None

    @pytest.mark.asyncio
    async def test_reserve_with_task_name(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1", task_name="build")
        assert run is not None
        assert run.task_name == "build"


class TestActivateSwarmRun:
    @pytest.mark.asyncio
    async def test_activate_from_reserved(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        activated = await activate_swarm_run(run.run_id)
        assert activated is not None
        assert activated.swarm_run_state == SwarmRunState.ACTIVE.value

    @pytest.mark.asyncio
    async def test_activate_respects_max_concurrent(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=1))
        run1 = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        await activate_swarm_run(run1.run_id)

        run2 = await reserve_swarm_run("g1", "task2", "agent:main:session:p1")
        result = await activate_swarm_run(run2.run_id)
        assert result.swarm_run_state == SwarmRunState.RESERVED.value

    @pytest.mark.asyncio
    async def test_activate_nonexistent_run(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        result = await activate_swarm_run("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_activate_already_active(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        activated1 = await activate_swarm_run(run.run_id)
        activated2 = await activate_swarm_run(run.run_id)
        assert activated2.run_id == run.run_id


class TestCompleteSwarmRun:
    @pytest.mark.asyncio
    async def test_complete_ok(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        await activate_swarm_run(run.run_id)
        completed = await complete_swarm_run(run.run_id, RunOutcome(status=RunOutcomeStatus.OK), "done")
        assert completed is not None
        assert completed.swarm_run_state == SwarmRunState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_complete_error(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1"))
        run = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        await activate_swarm_run(run.run_id)
        completed = await complete_swarm_run(run.run_id, RunOutcome(status=RunOutcomeStatus.ERROR, error="fail"), None)
        assert completed is not None
        assert completed.swarm_run_state == SwarmRunState.FAILED.value

    @pytest.mark.asyncio
    async def test_complete_activates_next(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=1))
        run1 = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        await activate_swarm_run(run1.run_id)
        run2 = await reserve_swarm_run("g1", "task2", "agent:main:session:p1")
        await activate_swarm_run(run2.run_id)

        await complete_swarm_run(run1.run_id, RunOutcome(status=RunOutcomeStatus.OK), "done")
        from future_subagent.registry import get_run as _get
        run2_refreshed = _get(run2.run_id)
        assert run2_refreshed.swarm_run_state == SwarmRunState.ACTIVE.value


class TestBuildStructuredOutputPrompt:
    def test_none_schema(self):
        assert build_structured_output_prompt(None) == ""

    def test_simple_schema(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        prompt = build_structured_output_prompt(schema)
        assert "JSON schema" in prompt
        assert "name" in prompt

    def test_empty_schema(self):
        prompt = build_structured_output_prompt({})
        assert "JSON schema" in prompt
