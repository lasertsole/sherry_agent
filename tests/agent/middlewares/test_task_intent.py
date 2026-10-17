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
        assert first["messages"][0].metadata == {"origin": "task_intent", "internal": True}

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
        assert later["messages"][0].metadata == {"origin": "task_intent", "internal": True}

    def test_injection_metadata_marks_system_directive(self):
        """The origin/internal metadata alone identifies a directive (content aside)."""
        msg = ti._task_intent_message("plain text without a directive prefix")
        assert ti._is_system_directive(msg) is True


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
        assert out["messages"][0].metadata == {"origin": "task_intent", "internal": True}
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
    """Plan-active steering must see checkbox-bearing plans in the session tree."""

    def test_external_orchestration_plan_ref_is_not_resolved(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / ".omo" / "plans" / "legacy.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan=".omo/plans/legacy.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is False

    def test_session_scoped_bare_filename_is_resolved(self, tmp_path, monkeypatch):
        _point_roots_at(monkeypatch, tmp_path)
        _write_plan(tmp_path / "workspace" / "sessions" / "sess-x" / "plans" / "scoped.md")
        boulder = tmp_path / "boulder.json"
        _write_boulder(boulder, plan="scoped.md")
        monkeypatch.setattr(ti, "_BOULDER_PATH", boulder)

        assert ti._has_active_boulder("sess-x") is True
        assert ti._has_active_boulder("other-sess") is False

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
        _write_boulder(boulder, plan="ghost.md")
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


# ============================================================================
# Four-language rules (EN / ZH / JA / KO)
# ============================================================================


class TestFourLanguageRules:
    @pytest.mark.parametrize(
        "message",
        [
            "fix the bug",  # EN
            "修复这个bug",  # ZH
            "バグを修正して",  # JA (漢字)
            "リファクタリングして",  # JA (カタカナ, matches the リファクタ prefix)
            "実装をお願いします",  # JA
            "버그 수정해줘",  # KO
            "테스트 추가해줘",  # KO
            "구현해주세요",  # KO
        ],
    )
    def test_task_requests_detected(self, message):
        assert ti._detect_task_intent(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "what is X",  # EN question
            "何が起きてるの",  # JA question
            "무엇이 문제인가요",  # KO question
            "こんにちは",  # JA chat
            "안녕하세요",  # KO chat
            "お疲れ様です",  # JA chat
        ],
    )
    def test_questions_and_chat_not_detected(self, message):
        assert ti._detect_task_intent(message) is False

    def test_a_long_cjk_message_is_a_task_on_length_alone(self):
        # CJK packs more meaning per character, so the 100-char floor is the
        # same for every language (no per-language threshold).
        assert ti._detect_task_intent("あ" * 101) is True


# ============================================================================
# LLM fallback (first arming only, uncertain window only, fail-open)
# ============================================================================

#: 22 chars: the old LLM-fallback window — request phrasing ("して"), no keyword.
_UNCERTAIN = "この表示、なんとかしてほしいなと思ってます"


class TestRulesOnly:
    """Detection is rules-only: the LLM fallback layer was removed."""

    @pytest.mark.asyncio
    async def test_the_leftover_window_is_not_a_task(self):
        # No keyword, under the length fallback: rules say no, and nothing
        # consults a model any more — the conservative direction.
        out = await ti.TaskIntentMiddleware().abefore_model(
            _state(HumanMessage(content=_UNCERTAIN))
        )

        assert out is None and "s-e7" not in ti._armed_sessions

    @pytest.mark.asyncio
    async def test_no_auxiliary_model_is_ever_consulted(self, monkeypatch):
        import models

        def _boom(**_kwargs):  # pragma: no cover - must not run
            raise AssertionError("task-intent must not call the auxiliary model")

        monkeypatch.setattr(models, "build_auxiliary_llm", _boom)

        # Both a rule-positive and a rule-inconclusive message stay modelless.
        armed = await ti.TaskIntentMiddleware().abefore_model(_state(HumanMessage(content=_TASK)))
        assert armed is not None and ti._TASK_STEERING_PROMPT in armed["messages"][0].content
        assert (
            await ti.TaskIntentMiddleware().abefore_model(_state(HumanMessage(content=_UNCERTAIN)))
            is None
        )


# ============================================================================
# Multimodal content: the production turn shape (the regression this fixes)
# ============================================================================


class TestMultimodalContentShape:
    """Real turns carry a content BLOCK LIST, not a plain string.

    The server builds the turn's HumanMessage from the client's multimodal
    envelope, so an ``isinstance(content, str)`` guard made every production
    turn a silent no-op (the middleware never injected anything). These cases
    pin the extracted text as the detection input.
    """

    @staticmethod
    def _blocks(*texts: str) -> list[dict]:
        return [{"type": "text", "text": text} for text in texts]

    def test_a_block_list_yields_its_text(self):
        assert ti._message_text(self._blocks("バグを修正して")) == "バグを修正して"

    def test_multiple_text_blocks_are_joined_in_order(self):
        assert ti._message_text(self._blocks("first", "second")) == "first\nsecond"

    def test_a_plain_string_passes_through(self):
        assert ti._message_text("fix the bug") == "fix the bug"

    def test_string_blocks_inside_the_list_count(self):
        assert ti._message_text(["hello", {"type": "text", "text": "world"}]) == "hello\nworld"

    def test_an_image_only_payload_has_no_text(self):
        assert ti._message_text([{"type": "image_url", "image_url": {"url": "x"}}]) == ""
        assert ti._message_text(None) == ""

    @pytest.mark.asyncio
    async def test_a_block_list_task_message_arms_the_session(self):
        message = HumanMessage(
            content=[{"type": "text", "text": "バグを修正して。返答は「了解」だけ。"}]
        )

        out = await ti.TaskIntentMiddleware().abefore_model(_state(message))

        assert out is not None, "a block-list task message must arm (regression: was a no-op)"
        assert ti._TASK_STEERING_PROMPT in out["messages"][0].content
        assert "s-e7" in ti._armed_sessions

    @pytest.mark.asyncio
    async def test_a_block_list_question_stays_a_non_task(self):
        message = HumanMessage(content=[{"type": "text", "text": "何が起きてるの"}] * 1)

        assert await ti.TaskIntentMiddleware().abefore_model(_state(message)) is None

    @pytest.mark.asyncio
    async def test_an_image_only_message_is_not_a_task(self):
        message = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}])

        assert await ti.TaskIntentMiddleware().abefore_model(_state(message)) is None

    def test_a_directive_in_a_block_list_is_still_a_system_directive(self):
        message = HumanMessage(content=[{"type": "text", "text": ti._TASK_STEERING_PROMPT}])

        assert ti._is_system_directive(message) is True
