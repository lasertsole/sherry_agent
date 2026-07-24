import pytest
import asyncio
from future_subagent.registry.work_admission import (
    is_gateway_draining,
    set_draining,
    run_with_work_admission,
    _root_work_tasks,
)


@pytest.fixture(autouse=True)
def _clean():
    set_draining(False)
    _root_work_tasks.clear()
    yield
    set_draining(False)
    for task in list(_root_work_tasks):
        if not task.done():
            task.cancel()
    _root_work_tasks.clear()


class TestIsGatewayDraining:
    def test_default_not_draining(self):
        assert is_gateway_draining() is False

    def test_set_draining(self):
        set_draining(True)
        assert is_gateway_draining() is True

    def test_unset_draining(self):
        set_draining(True)
        set_draining(False)
        assert is_gateway_draining() is False


class TestRunWithWorkAdmission:
    @pytest.mark.asyncio
    async def test_runs_cooroutine(self):
        result = []

        async def work():
            result.append("done")

        await run_with_work_admission(work(), label="test")
        await asyncio.sleep(0.1)
        assert "done" in result

    @pytest.mark.asyncio
    async def test_tracks_task(self):
        event = asyncio.Event()

        async def work():
            await asyncio.sleep(0.2)
            event.set()

        await run_with_work_admission(work(), label="test")
        await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_draining_defers(self):
        set_draining(True)
        result = []

        async def work():
            result.append("done")

        await run_with_work_admission(work(), label="test")
        assert len(result) == 0
        set_draining(False)
        await asyncio.sleep(6.0)
        assert "done" in result

    @pytest.mark.asyncio
    async def test_exception_does_not_crash(self):
        result = []

        async def bad_work():
            raise RuntimeError("test error")

        await run_with_work_admission(bad_work(), label="bad")
        await asyncio.sleep(0.1)


class TestPendingRootWorkCount:
    @pytest.mark.asyncio
    async def test_task_removed_after_completion(self):
        async def quick_work():
            pass

        await run_with_work_admission(quick_work(), label="quick")
        await asyncio.sleep(0.2)
