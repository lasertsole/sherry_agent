"""Unit tests for the task-intent middleware (`agent/middlewares/task_intent/core.py`).

Covers the mandatory design corrections over the original sketch:

- per-turn dedup (inject only when the last non-directive ``HumanMessage`` is
  the FINAL message in the state);
- internal subagent-completion carriers never arm/steer;
- Plan-active steering (active boulder) takes priority over task-intent arming;
- Arming happens once per session (full prompt) then a short reminder;
- fail-open on any internal exception;
- ``rearm_after_compact`` clears the arming ledger.

Imports the leaf module directly (stub-tolerant when the subagent conftest is
collected in the same process).
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.task_intent.core as ti
from config import path as config_path

pytestmark = [pytest.mark.unit]

# Contains the task keyword "implement" → task-intent regardless of length.
_TASK = "Please implement a login endpoint and add tests for it."


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Hermetic isolation: no live boulder, empty arming ledger per test."""
    monkeypatch.setattr(
        ti,
        "_BOULDER_PATH",
        tmp_path / "missing-boulder.json",
        raising=False,
    )
    ti._armed_sessions.clear()
    yield
    ti._armed_sessions.clear()


def _state(*messages, session_id: str = "s-e7") -> dict:
    return {"messages": list(messages), "session_id": session_id}


# ============================================================================
# (a) Arming: full prompt first turn, short reminder on a later turn
# ============================================================================


class TestArming:
    @pytest.mark.asyncio
    async def test_first_turn_full_prompt_then_short_reminder(self):
        mw = ti.TaskIntentMiddleware()

        first = await mw.abefore_model(_state(HumanMessage(content=_TASK)))
        assert first is not None
        assert ti._TASK_STEERING_PROMPT in first["messages"][0].content

        later = await mw.abefore_model(
            _state(
                HumanMessage(content=_TASK),
                HumanMessage(content=ti._TASK_STEERING_PROMPT),
                AIMessage(content="working"),
                HumanMessage(content="now also fix the logout bug"),
            )
        )
        assert later is not None
        assert later["messages"][0].content == ti._TASK_STEERING_REMINDER


# ============================================================================
# (b) Per-turn dedup: an AI/Tool message after the user message suppresses it
# ============================================================================


class TestPerTurnDedup:
    @pytest.mark.asyncio
    async def test_ai_message_after_user_suppresses_injection(self):
        mw = ti.TaskIntentMiddleware()
        state = _state(HumanMessage(content=_TASK), AIMessage(content="working"))
        assert await mw.abefore_model(state) is None
        assert "s-e7" not in ti._armed_sessions

    @pytest.mark.asyncio
    async def test_tool_message_after_user_suppresses_injection(self):
        mw = ti.TaskIntentMiddleware()
        state = _state(
            HumanMessage(content=_TASK),
            AIMessage(content=""),
            ToolMessage(content="result", tool_call_id="c1", name="terminal"),
        )
        assert await mw.abefore_model(state) is None
        assert "s-e7" not in ti._armed_sessions


# ============================================================================
# (c) Active boulder → plan-active reminder, arming suppressed
# ============================================================================


class TestPlanActiveBoulder:
    @pytest.mark.asyncio
    async def test_active_boulder_injects_plan_reminder_and_suppresses_e7a(
        self, tmp_path, monkeypatch
    ):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n- [ ] step one\n", encoding="utf-8")
        boulder = tmp_path / "boulder.json"
        boulder.write_text(
            json.dumps(
                {
                    "active_work_id": "w1",
                    "works": {
                        "w1": {"status": "active", "active_plan": str(plan)},
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder, raising=False)

        mw = ti.TaskIntentMiddleware()
        out = await mw.abefore_model(_state(HumanMessage(content=_TASK)))

        assert out is not None
        content = out["messages"][0].content
        assert "<sherry-ulw-execute>" in content
        assert ti._TASK_STEERING_PROMPT not in content
        assert "s-e7" not in ti._armed_sessions

    @pytest.mark.asyncio
    async def test_completed_boulder_does_not_inject(self, tmp_path, monkeypatch):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n- [x] done\n", encoding="utf-8")
        boulder = tmp_path / "boulder.json"
        boulder.write_text(
            json.dumps(
                {
                    "active_work_id": "w1",
                    "works": {
                        "w1": {"status": "completed", "active_plan": str(plan)},
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder, raising=False)

        out = await ti.TaskIntentMiddleware().abefore_model(_state(HumanMessage(content=_TASK)))
        assert out is not None
        # Falls through to arming (full prompt), not the plan-active reminder.
        assert "<sherry-ulw-execute>" not in out["messages"][0].content


def _point_roots_at(monkeypatch, tmp_path) -> None:
    """Repoint the resolver's roots so tests never read the real repo tree."""
    monkeypatch.setattr(config_path, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config_path, "SESSIONS_DIR", tmp_path / "workspace" / "sessions")


def _write_boulder(path, *, plan: str, status: str = "active") -> None:
    path.write_text(
        json.dumps(
            {
                "active_work_id": "w1",
                "works": {"w1": {"status": status, "active_plan": plan}},
            }
        ),
        encoding="utf-8",
    )


def _write_plan(path, content: str = "# Plan\n- [ ] step\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestPlanPathResolution:
    """Plan-active steering must see checkbox-bearing plans in both location generations."""

    def test_legacy_relative_plan_is_resolved(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / ".omo" / "plans" / "legacy.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan=".omo/plans/legacy.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is True

    def test_session_scoped_bare_filename_is_resolved(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / "workspace" / "sessions" / "sess-x" / "plans" / "scoped.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan="scoped.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is True
        assert ti._has_active_boulder("other-sess") is False

    def test_migrated_plan_found_behind_legacy_ref(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / "workspace" / "sessions" / "sess-x" / "plans" / "moved.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan=".omo/plans/moved.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is True

    def test_new_explicit_relative_plan_is_resolved(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / "workspace" / "sessions" / "sess-x" / "plans" / "explicit.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan="workspace/sessions/sess-x/plans/explicit.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is True

    def test_missing_plan_is_not_active(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan=".omo/plans/ghost.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is False


# ============================================================================
# (d) Question / chat messages are not task intent
# ============================================================================


class TestNonTaskMessages:
    @pytest.mark.parametrize(
        "message",
        ["What is a REST API?", "How do I center a div?", "你好", "谢谢", "thanks!", "ok"],
    )
    @pytest.mark.asyncio
    async def test_question_or_chat_not_injected(self, message):
        mw = ti.TaskIntentMiddleware()
        assert await mw.abefore_model(_state(HumanMessage(content=message))) is None


# ============================================================================
# (e) System directives are never treated as user messages
# ============================================================================


class TestSystemDirectiveFiltering:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            ti._TASK_STEERING_PROMPT,
            ti._TASK_STEERING_REMINDER,
            "<sherry-ulw-execute>\ncontinue\n</sherry-ulw-execute>",
        ],
    )
    async def test_system_directive_never_injected(self, content):
        mw = ti.TaskIntentMiddleware()
        assert await mw.abefore_model(_state(HumanMessage(content=content))) is None
        assert "s-e7" not in ti._armed_sessions


# ============================================================================
# (f) Internal subagent-completion carrier as the final message → skip turn
# ============================================================================


class TestInternalCompletionCarrier:
    @pytest.mark.asyncio
    async def test_internal_carrier_final_skips_turn(self):
        carrier = HumanMessage(
            content="subagent finished",
            metadata={
                "internal": True,
                "provenance": "subagent_completion",
                "run_id": "r1",
            },
        )
        state = _state(HumanMessage(content=_TASK), carrier)

        assert await ti.TaskIntentMiddleware().abefore_model(state) is None
        assert "s-e7" not in ti._armed_sessions


# ============================================================================
# (g) Fail-open on any internal exception
# ============================================================================


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_internal_exception_returns_none(self, monkeypatch):
        mw = ti.TaskIntentMiddleware()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boulder read blew up")

        monkeypatch.setattr(ti, "_has_active_boulder", _boom, raising=True)
        assert await mw.abefore_model(_state(HumanMessage(content=_TASK))) is None


# ============================================================================
# (h) rearm_after_compact clears the arming ledger
# ============================================================================


class TestRearmAfterCompact:
    @pytest.mark.asyncio
    async def test_rearm_after_compact_clears_arming(self):
        mw = ti.TaskIntentMiddleware()

        first = await mw.abefore_model(_state(HumanMessage(content=_TASK)))
        assert first is not None
        assert ti._TASK_STEERING_PROMPT in first["messages"][0].content
        assert "s-e7" in ti._armed_sessions

        ti.rearm_after_compact("s-e7")
        assert "s-e7" not in ti._armed_sessions

        again = await mw.abefore_model(_state(HumanMessage(content=_TASK)))
        assert again is not None
        assert ti._TASK_STEERING_PROMPT in again["messages"][0].content


# ============================================================================
# Intent-detection heuristics (direct)
# ============================================================================


class TestDetectTaskIntent:
    def test_task_keyword_detected(self):
        assert ti._detect_task_intent("fix the login bug") is True

    def test_chinese_task_keyword_detected(self):
        assert ti._detect_task_intent("实现登录功能") is True

    def test_long_message_detected(self):
        assert ti._detect_task_intent("x" * 101) is True

    def test_short_non_task_not_detected(self):
        assert ti._detect_task_intent("2 + 2 = ?") is False
