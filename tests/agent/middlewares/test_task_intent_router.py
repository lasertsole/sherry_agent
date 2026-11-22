"""WorkMode stage 1: the long-task interruption router.

Pins the rules-only classifier (four languages, fail-open) and the before_agent
middleware's routing decisions:

* no active flow -> strict no-op (short turns unaffected);
* CANCEL -> active flows cancelled via taskflow_cancel + a neutral notice;
* SOFT_PREEMPT / NEW -> advisory steering, NO cancellation;
* RESUME / CONTINUE -> no-op (existing HITL / injection paths own them);

and that internal/system messages are never re-classified on a later model call.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.task_intent_router.core as router_core
from agent.middlewares.task_intent_router.core import TaskIntentRouterMiddleware
from agent.middlewares.task_intent_router.intent import TurnIntent, classify_turn_intent

pytestmark = [pytest.mark.unit]


class TestClassifier:
    @pytest.mark.parametrize(
        "text",
        [
            "stop",
            "Stop!",
            "cancel",
            "abort",
            "don't do that",
            "停下",
            "停止",
            "取消",
            "别做了",
            "やめて",
            "キャンセル",
            "취소",
            "그만",
        ],
    )
    def test_cancel_phrases(self, text):
        assert classify_turn_intent(text) is TurnIntent.CANCEL

    @pytest.mark.parametrize(
        "text",
        [
            "wait, actually change the approach",
            "instead do X",
            "等等，换个方式",
            "其实应该改成这样",
            "不对",
            "ちょっと待って",
            "やっぱり違う",
            "잠깐, 대신",
        ],
    )
    def test_soft_preemption(self, text):
        assert classify_turn_intent(text) is TurnIntent.SOFT_PREEMPT

    @pytest.mark.parametrize(
        "text",
        ["new task: refactor auth", "新任务，写一个脚本", "새 작업 시작"],
    )
    def test_new_task(self, text):
        assert classify_turn_intent(text) is TurnIntent.NEW

    @pytest.mark.parametrize(
        "text",
        [
            "yes",
            "ok, approved",
            "好的，继续",
            "承認",
            "네, 계속",
        ],
    )
    def test_resume_when_hitl_pending(self, text):
        assert classify_turn_intent(text, has_pending_hitl=True) is TurnIntent.RESUME

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "please add a test for the parser",
            "继续补充一点信息",
            "whatever comes to mind here without any special keywords at all",
        ],
    )
    def test_continue_is_fail_open(self, text):
        assert classify_turn_intent(text) is TurnIntent.CONTINUE

    def test_long_cancel_sentence_is_not_a_cancel(self):
        # A long message containing "stop" is likely prose, not a cancel
        # command; fail open to CONTINUE so a running flow is not killed.
        text = "I was reading the docs and it says stop after the first error but " + "x" * 90
        assert classify_turn_intent(text) is not TurnIntent.CANCEL

    def test_resume_disabled_without_hitl(self):
        # Without a pending approval, "yes" is a plain continuation, not resume.
        assert classify_turn_intent("yes", has_pending_hitl=False) is TurnIntent.CONTINUE


class _FakeFlowStore:
    def __init__(self, flows):
        self._flows = flows

    def get_active_flows_sync(self, session_id):
        return self._flows


@pytest.fixture
def cancel_calls(monkeypatch):
    calls: list[dict] = []

    async def fake_cancel(flow_id, session_id, reason):
        calls.append({"flow_id": flow_id, "session_id": session_id, "reason": reason})
        return "TaskFlow cancelled: flow_id=flow-1, revision=2, status=cancelled, steps_cancelled=1"

    monkeypatch.setattr(router_core, "_cancel_flow", fake_cancel)
    return calls


def _state(text: str, session_id: str = "s-1") -> dict:
    return {"session_id": session_id, "messages": [HumanMessage(content=text)]}


@pytest.mark.asyncio
class TestRouterMiddleware:
    async def test_no_active_flow_is_strict_noop(self, monkeypatch):
        monkeypatch.setattr(router_core.store_sqlite, "get_active_flows_sync", lambda sid: [])
        result = await TaskIntentRouterMiddleware().abefore_agent(_state("stop"))
        assert result is None

    async def test_cancel_cancels_active_flows_and_injects_notice(self, monkeypatch, cancel_calls):
        monkeypatch.setattr(
            router_core.store_sqlite,
            "get_active_flows_sync",
            lambda sid: [{"flow_id": "flow-1"}],
        )
        monkeypatch.setattr(router_core, "is_hitl_pending", lambda sid: False)
        result = await TaskIntentRouterMiddleware().abefore_agent(_state("取消"))
        assert result is not None
        assert len(cancel_calls) == 1
        assert cancel_calls[0]["flow_id"] == "flow-1"
        msg = result["messages"][0]
        assert msg.metadata.get("internal") is True
        assert "cancelled" in msg.content.lower()

    async def test_soft_preempt_is_advisory_only(self, monkeypatch, cancel_calls):
        monkeypatch.setattr(
            router_core.store_sqlite,
            "get_active_flows_sync",
            lambda sid: [{"flow_id": "flow-1"}],
        )
        monkeypatch.setattr(router_core, "is_hitl_pending", lambda sid: False)
        result = await TaskIntentRouterMiddleware().abefore_agent(_state("wait, change direction"))
        assert result is not None
        assert cancel_calls == []  # no destructive action
        msg = result["messages"][0]
        assert "NOT been interrupted" in msg.content

    async def test_new_task_is_advisory_only(self, monkeypatch, cancel_calls):
        monkeypatch.setattr(
            router_core.store_sqlite,
            "get_active_flows_sync",
            lambda sid: [{"flow_id": "flow-1"}],
        )
        monkeypatch.setattr(router_core, "is_hitl_pending", lambda sid: False)
        result = await TaskIntentRouterMiddleware().abefore_agent(_state("新任务，写脚本"))
        assert result is not None
        assert cancel_calls == []
        assert "new task" in result["messages"][0].content.lower()

    @pytest.mark.parametrize("text", ["继续补充", "yes"])
    async def test_continue_and_resume_are_noop(self, monkeypatch, cancel_calls, text):
        monkeypatch.setattr(
            router_core.store_sqlite,
            "get_active_flows_sync",
            lambda sid: [{"flow_id": "flow-1"}],
        )
        monkeypatch.setattr(router_core, "is_hitl_pending", lambda sid: text == "yes")
        result = await TaskIntentRouterMiddleware().abefore_agent(_state(text))
        assert result is None
        assert cancel_calls == []

    async def test_internal_messages_not_reclassified(self, monkeypatch, cancel_calls):
        # A turn whose last message is the router's own injection must not
        # trigger a second cancel on a later model call.
        monkeypatch.setattr(
            router_core.store_sqlite,
            "get_active_flows_sync",
            lambda sid: [{"flow_id": "flow-1"}],
        )
        monkeypatch.setattr(router_core, "is_hitl_pending", lambda sid: False)
        state = {
            "session_id": "s-1",
            "messages": [
                HumanMessage(content="stop"),
                HumanMessage(
                    content="[SYSTEM: user asked to stop]",
                    metadata={"origin": "task_intent_router", "internal": True},
                ),
            ],
        }
        result = await TaskIntentRouterMiddleware().abefore_agent(state)
        assert result is None
        assert cancel_calls == []

    async def test_error_is_fail_open(self, monkeypatch):
        def boom(sid):
            raise RuntimeError("db down")

        monkeypatch.setattr(router_core.store_sqlite, "get_active_flows_sync", boom)
        result = await TaskIntentRouterMiddleware().abefore_agent(_state("stop"))
        assert result is None
