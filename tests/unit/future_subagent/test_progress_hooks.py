import pytest
from future_subagent.hooks.progress import (
    fire_spawned_hook,
    fire_progress_hook,
    fire_ended_hook,
)
from future_subagent.types.registry import SubagentRunRecord


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestSpawnedHook:
    @pytest.mark.asyncio
    async def test_fire_spawned_no_hooks(self):
        await fire_spawned_hook(_make_run())


class TestProgressHook:
    @pytest.mark.asyncio
    async def test_fire_progress_no_hooks(self):
        await fire_progress_hook(_make_run(), "50% done")

    @pytest.mark.asyncio
    async def test_progress_default_message(self):
        await fire_progress_hook(_make_run())


class TestEndedHook:
    @pytest.mark.asyncio
    async def test_fire_ended_no_hooks(self):
        await fire_ended_hook(_make_run())
