import pytest
import asyncio
from future_subagent.hooks.base import (
    SubagentStartEvent,
    SubagentStopEvent,
    register_start_hook,
    register_stop_hook,
    fire_start_hooks,
    fire_stop_hooks,
    clear_hooks,
)


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


class TestHooks:
    @pytest.mark.asyncio
    async def test_start_hook(self):
        received = []
        async def on_start(event):
            received.append(event)

        register_start_hook(on_start)
        event = SubagentStartEvent(
            parent_session_key="parent",
            child_session_key="child",
            child_role="leaf",
            child_goal="do something",
        )
        await fire_start_hooks(event)
        assert len(received) == 1
        assert received[0].child_goal == "do something"

    @pytest.mark.asyncio
    async def test_stop_hook(self):
        received = []
        async def on_stop(event):
            received.append(event)

        register_stop_hook(on_stop)
        event = SubagentStopEvent(
            parent_session_key="parent",
            child_session_key="child",
            child_role="leaf",
            child_status="ok",
            child_summary="done",
        )
        await fire_stop_hooks(event)
        assert len(received) == 1
        assert received[0].child_summary == "done"

    @pytest.mark.asyncio
    async def test_multiple_hooks(self):
        count = [0]
        async def hook1(event):
            count[0] += 1
        async def hook2(event):
            count[0] += 10

        register_start_hook(hook1)
        register_start_hook(hook2)
        await fire_start_hooks(SubagentStartEvent(
            parent_session_key="p", child_session_key="c", child_role="leaf", child_goal="t"
        ))
        assert count[0] == 11

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_stop_others(self):
        count = [0]
        async def bad_hook(event):
            raise RuntimeError("fail")
        async def good_hook(event):
            count[0] += 1

        register_start_hook(bad_hook)
        register_start_hook(good_hook)
        await fire_start_hooks(SubagentStartEvent(
            parent_session_key="p", child_session_key="c", child_role="leaf", child_goal="t"
        ))
        assert count[0] == 1

    @pytest.mark.asyncio
    async def test_clear_hooks(self):
        register_start_hook(lambda e: None)
        register_stop_hook(lambda e: None)
        clear_hooks()
        await fire_start_hooks(SubagentStartEvent(
            parent_session_key="p", child_session_key="c", child_role="leaf", child_goal="t"
        ))
