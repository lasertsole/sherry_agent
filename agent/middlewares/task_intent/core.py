"""Task-intent detection and steering — a ``before_model`` middleware.

Two independent components of the original design are fused here:

- **Arming** (mirrors omo ``ultrawork``): when a user message looks like a
  work request and no plan is active, inject the full orchestrator steering
  prompt on the first qualifying turn, then a short reminder on later turns
  (module-level ``_armed_sessions`` ledger). ``rearm_after_compact`` clears the
  entry after context compression so the full prompt is injected again.

Intent detection is TWO layers, in this order and for this reason:

1. **Rules** (``_detect_task_intent``): zero latency, zero cost, and the only
   layer an armed session ever runs. Question / chat patterns decide first;
   then a keyword in ANY of the four UI languages (EN / ZH / JA / KO) decides
   "task"; then length (> ``_LONG_MESSAGE_CHARS``) decides "task" for a long
   message that is neither.
2. **LLM fallback** (``_detect_task_intent_with_llm``): only for FIRST arming,
   only when the rules were inconclusive — a 20-100 char message that carries
   request phrasing (``してください`` / ``해줘`` / ``please`` / ``帮我`` …) but no
   keyword. One auxiliary-LLM call (~50 tokens) picks up the short
   Japanese/Korean request the keyword lists miss. Fail-open in every
   direction: a failure returns False and the turn proceeds without steering.
- **Plan-active steering** (mirrors omo ``ulw-execute-continuation``
  input hook): when the boulder file (``config.path.resolve_boulder_path``)
  holds an active/paused work whose plan file exists and contains a checkbox,
  append the plan-active reminder instead and skip arming entirely
  (plan-active steering has priority).

Anti-loop guarantees (design intent; see this module + skills/builtin/core/ulw-execute/SKILL.md):

- **Per-turn dedup**: injection happens only on the first model call of a turn,
  i.e. when the last non-directive ``HumanMessage`` is the FINAL message in
  ``state["messages"]``. Once an ``AIMessage``/``ToolMessage`` follows it, the
  turn is already in flight and the middleware is a no-op.
- **Completion-carrier skip**: a final message carrying the frozen task-4
  contract (``metadata.internal`` + ``provenance == "subagent_completion"``,
  see ``subagent_completion_drain.py``) never triggers steering — the drained
  carrier drives that turn.
- **System-directive filtering**: ``[SYSTEM DIRECTIVE`` / ``<sherry-ulw-execute>``
  / ``metadata.internal`` messages are never mistaken for user input, so
  injected directives cannot re-trigger steering.
- **Fail-open**: any internal exception is logged and swallowed (return ``None``)
  — steering must never break a turn.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from loguru import logger

from config.path import resolve_boulder_path, resolve_plan_path

__all__ = ["TaskIntentMiddleware", "rearm_after_compact"]


# ── Intent-detection heuristics (no LLM: zero latency, zero cost) ─────────

_TASK_KEYWORDS = {
    # English
    "implement",
    "fix",
    "create",
    "add",
    "refactor",
    "build",
    "deploy",
    "test",
    "update",
    "migrate",
    "write",
    "setup",
    "configure",
    "integrate",
    "optimize",
    "debug",
    "resolve",
    "enhance",
    "rewrite",
    "convert",
    # Chinese
    "实现",
    "修复",
    "创建",
    "添加",
    "重构",
    "构建",
    "部署",
    "测试",
    "更新",
    "迁移",
    "编写",
    "设置",
    "配置",
    "集成",
    "优化",
    "调试",
    "解决",
    "增强",
    "重写",
    "转换",
    # 日本語（漢字 + カタカナ + ひらがな）
    "実装",
    "修正",
    "作成",
    "追加",
    "リファクタ",
    "構築",
    "デプロイ",
    "テスト",
    "更新",
    "移行",
    "記述",
    "設定",
    "最適化",
    "デバッグ",
    "解決",
    "強化",
    "書き直し",
    "変換",
    "統合",
    # 한국어
    "구현",
    "수정",
    "생성",
    "추가",
    "리팩토링",
    "빌드",
    "배포",
    "테스트",
    "업데이트",
    "마이그레이션",
    "작성",
    "설정",
    "통합",
    "최적화",
    "디버그",
    "해결",
    "개선",
    "재작성",
    "변환",
}

_QUESTION_PATTERNS = [
    r"^(what|how|why|where|who|when|can you|is it|are you|do you)\b",
    r"^(什么是|怎么|为什么|哪里|谁|什么时候|能否|是否|是不是|能不能)",
    r"^(explain|describe|tell me about)",
    r"^(解释|说明|介绍一下)",
    r"^(何が|どうやって|なぜ|どこに|誰が|いつ|できますか|ですか|教えて)",
    r"^(説明|解説|紹介して)",
    r"^(무엇이|어떻게|왜|어디에|누가|언제|할 수 있|인가요|알려주세요)",
    r"^(설명|소개해)",
]

_CHAT_PATTERNS = [
    r"^(hello|hi|hey|thanks|thank you|ok|good|great|bye)\b",
    r"^(你好|谢谢|好的|再见|嗯|哦)",
    r"^(こんにちは|ありがとう|お疲れ様|はい|さようなら|うん|ええ|おはよう)",
    r"^(안녕하세요|감사합니다|네|좋아요|안녕히|응)",
]

#: A message longer than this is a work request on LENGTH alone: no keyword or
#: pattern check applies (a long, specific message is a request in any
#: language, and CJK packs more meaning per character than English).
_LONG_MESSAGE_CHARS = 100

#: The LLM-fallback window. Shorter than this and the rules have already
#: decided (a keyword, a question/chat pattern, or simply not enough text to
#: be a request); longer and ``_LONG_MESSAGE_CHARS`` decided.
_LLM_FALLBACK_MIN_CHARS = 20

#: Request phrasing in the four languages: a message carrying one of these is
#: the shape the keyword lists can miss (a short JA/KO request), so the one
#: auxiliary-LLM classification is worth its ~50 tokens.
_REQUEST_MARKERS = (
    "please",
    "帮我",
    "帮忙",
    "看一下",
    "处理一下",
    # 日本語
    "して",
    "してください",
    "お願い",
    "やって",
    "直して",
    "してほしい",
    # 한국어
    "해줘",
    "해주세요",
    "부탁",
    "처리",
    "봐줘",
)

#: The classification prompt: one category name out, nothing else in.
_INTENT_CLASSIFY_SYSTEM_PROMPT = (
    "Classify the user's message into one category. Respond with ONLY the "
    "category name, nothing else:\n"
    "- work_request: asks to implement, fix, build, configure, or perform a "
    "development task\n"
    "- question: asks for explanation or information\n"
    "- chat: greeting, thanks, or casual conversation"
)

#: Bound on the message text sent to the classifier (the window is already
#: 20-100 chars; this is a hard ceiling, not a budget).
_LLM_PROMPT_MESSAGE_CHARS = 500


def _matches_any(patterns: list[str], text: str) -> bool:
    """True when any anchored pattern matches *text*."""
    return any(re.match(pattern, text) for pattern in patterns)


def _detect_task_intent(content: str) -> bool:
    """Return True when ``content`` looks like a work request.

    Strategy (conservative — a false negative only skips steering):
    1. question patterns (short messages only) → not a task;
    2. chat patterns (short messages only) → not a task;
    3. a task keyword in any of the four languages → task;
    4. long messages (> ``_LONG_MESSAGE_CHARS``) that are neither question nor
       chat → task.
    """
    text = content.strip().lower()
    is_long = len(content) > _LONG_MESSAGE_CHARS

    if not is_long and (
        _matches_any(_QUESTION_PATTERNS, text) or _matches_any(_CHAT_PATTERNS, text)
    ):
        return False

    for keyword in _TASK_KEYWORDS:
        if keyword in text:
            return True

    return is_long


def _looks_like_request(text: str) -> bool:
    """True when *text* carries request phrasing (``~して`` / ``~해줘`` / please).

    The phrasing is what a short JA/KO work request looks like when no keyword
    matched, so it is the fallback's trigger — not a decision of its own.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _REQUEST_MARKERS)


def _should_try_llm(content: str) -> bool:
    """True when the rules were inconclusive and one LLM call is worth it.

    Only the uncertain window qualifies: 20-100 characters, no
    question/chat pattern (those are decisive negatives), and request phrasing
    present. Anything shorter was already decided by the rules, anything longer
    by ``_LONG_MESSAGE_CHARS``.
    """
    text = content.strip()
    if not (_LLM_FALLBACK_MIN_CHARS <= len(text) <= _LONG_MESSAGE_CHARS):
        return False
    lowered = text.lower()
    if _matches_any(_QUESTION_PATTERNS, lowered) or _matches_any(_CHAT_PATTERNS, lowered):
        return False
    return _looks_like_request(text)


async def _llm_classify_intent(content: str) -> bool:
    """Ask the auxiliary LLM whether *content* is a work request.

    Fail-open: any failure (no model configured, timeout, a model that answers
    prose instead of a category) returns False, so the fallback can only ever
    cause MISSING steering, never a broken turn.
    """
    try:
        from models import build_auxiliary_llm

        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": _INTENT_CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": content[:_LLM_PROMPT_MESSAGE_CHARS]},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return "work_request" in str(raw).strip().lower()
    except Exception:
        logger.debug("task-intent LLM classification failed; treating as non-task", exc_info=True)
        return False


async def _detect_task_intent_with_llm(content: str, *, allow_llm: bool = True) -> bool:
    """Two-layer detection: rules first, one bounded LLM call only if unsure.

    ``allow_llm=False`` (an already-armed session, or a call path with no
    session id to arm) keeps the decisions purely rule-based, so the fallback
    fires at most once per session.
    """
    if _detect_task_intent(content):
        return True
    if not allow_llm or not _should_try_llm(content):
        return False
    logger.debug("task-intent: rules were inconclusive; asking the auxiliary model")
    return await _llm_classify_intent(content)


# ── Steering prompts ───────────────────────────────────────────────────────

# Full steering (first arming, when no active plan).
_TASK_STEERING_PROMPT = """[SYSTEM DIRECTIVE: TASK INTENT DETECTED]

This message appears to be a work request. Before responding, assess the scope:

## If this is a multi-step task (2+ steps):

1. Consider the plan gate for work that is hard to undo: a multi-file change, an
   architectural decision, unclear requirements, or several valid approaches.
   `enter_plan_mode` narrows you to read-only tools while you design, and
   `exit_plan_mode` submits the plan for the user's approval before anything is
   written. It is optional — a clear, well-scoped task can go straight to
   orchestrating.

2. Load the ulw-execute skill to understand the orchestration workflow.
   Use the skill_view tool to read skill files by name when you need their
   full instructions.

3. You are an ORCHESTRATOR, not an implementer:
   - Create a session plan file under workspace/sessions/<session_id>/plans/
     or use todowrite to register tasks
   - Set proper category, delegation, flow_id/step_id fields
   - For dependencies, register TaskFlow steps with depends_on and let TaskFlow
     block/unlock/parallel-dispatch — do NOT build a second DAG
   - DELEGATE implementation to subagents — you do NOT write code

4. Workflow:
   a. todowrite: register all tasks with atomic granularity
   b. For dependent steps: taskflow_run_task(..., depends_on=[...]); then
      taskflow_dispatch the ready ones and taskflow_wait_all
   c. For delegation="subagent" tasks: spawn subagent via task tool
   d. Wait for subagent return → taskflow_resume → verify → mark completed
   e. For delegation="self" tasks: execute directly

## If this is a simple single-step task:
Respond directly — no plan needed.

## If this is a question (not a task):
Respond directly — ignore this directive.

Decision: Is this a multi-step task? If yes, create todos FIRST."""

# Light steering: short reminder once the session is already armed.
_TASK_STEERING_REMINDER = (
    "[SYSTEM DIRECTIVE: ORCHESTRATOR MODE ARMED]\n"
    "Orchestrator mode is already active for this session. "
    "The full directive above remains binding — re-read it and continue. "
    "Create todos FIRST for any multi-step work."
)

# Plan-active reminder (appended when a boulder work is active).
_PLAN_ACTIVE_REMINDER = (
    "\n\n<sherry-ulw-execute>\n"
    "An active ulw-execute plan is present in this working directory.\n"
    "Before continuing, read the boulder state (`src/data/boulder.json`) and the\n"
    "plan file its active_plan points to (session-scoped\n"
    "`workspace/sessions/<session_id>/plans/`), then determine what remains; use\n"
    "the plan and the evidence ledger as the source of truth.\n"
    "Continue the current work with evidence-bound execution; do not start "
    "unrelated work until every top-level checkbox is `- [x]`.\n"
    "</sherry-ulw-execute>"
)


# ── once-per-session arming ledger (mirrors omo ``armedSessionIds``) ───────

_armed_sessions: set[str] = set()

# Absolute repo-root path (was cwd-relative); kept as a module attribute so tests can repoint it.
_BOULDER_PATH: Path = resolve_boulder_path()


# ── Helpers ────────────────────────────────────────────────────────────────


def _message_text(content: Any) -> str:
    """The message's text, whatever shape the content arrived in.

    The server builds each turn's ``HumanMessage`` from the client's multimodal
    envelope, so the content is a LIST of blocks
    (``[{"type": "text", "text": ...}]``) — an ``isinstance(content, str)``
    check silently skipped every real turn (the middleware was inert in
    production until 2026-10-12). Text blocks are joined in order; a payload
    with no text block (an image with no caption) yields ``""``.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if isinstance(block, dict):
            block_type, text = block.get("type"), block.get("text")
        else:
            block_type, text = getattr(block, "type", None), getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _is_system_directive(msg: Any) -> bool:
    """True when ``msg`` is a system-injected directive, not user input.

    Filters continuation prompts and steering directives so they can never be
    mistaken for a fresh user request (anti-loop).
    """
    text = _message_text(getattr(msg, "content", None))
    if text.startswith("[SYSTEM DIRECTIVE") or text.startswith("<sherry-ulw-execute>"):
        return True
    meta = getattr(msg, "metadata", None) or {}
    return bool(meta.get("internal"))


def _is_internal_completion(msg: Any) -> bool:
    """True when ``msg`` carries the frozen subagent-completion metadata contract.

    Contract (``subagent_completion_drain.py``): ``metadata.internal`` is truthy
    AND ``metadata.provenance == "subagent_completion"``. Such a final message
    is an internal carrier — the drain's turn, not a user turn.
    """
    meta = getattr(msg, "metadata", None) or {}
    return bool(meta.get("internal")) and meta.get("provenance") == "subagent_completion"


def _task_intent_message(content: str) -> HumanMessage:
    """Build a steering message tagged as an internal TaskIntent injection.

    ``origin="task_intent"`` lets the message store positively identify the
    injection (never a real user request) and keeps it out of session titles;
    ``internal=True`` marks it as a system directive for every consumer.
    """
    return HumanMessage(content=content, metadata={"origin": "task_intent", "internal": True})


def _has_active_boulder(session_id: str | None = None) -> bool:
    """True when the boulder file holds continuable work.

    Mirrors omo ``findContinuableBoulderWork``: the active work's status is
    ``active`` or ``paused`` AND its plan file exists and contains at least one
    checkbox (``- [ ]`` or ``- [x]``). The plan reference resolves through
    ``config.path.resolve_plan_path`` (session-scoped
    ``workspace/sessions/<session_id>/plans/``). A missing boulder file means no
    active work; any read/parse failure → False.
    """
    try:
        boulder_path = _BOULDER_PATH
        if not boulder_path.exists():
            return False
        with boulder_path.open("r", encoding="utf-8") as fh:
            boulder = json.load(fh)
        active_id = boulder.get("active_work_id", "")
        work = (boulder.get("works") or {}).get(active_id, {})
        if work.get("status") not in ("active", "paused"):
            return False
        plan_ref = work.get("active_plan", "")
        if not plan_ref:
            return False
        plan_file = resolve_plan_path(plan_ref, session_id)
        if plan_file is None:
            return False
        plan_content = plan_file.read_text(encoding="utf-8")
        return "- [ ]" in plan_content or "- [x]" in plan_content
    except Exception:
        logger.debug("plan-file check failed for plan_ref={!r}", plan_ref, exc_info=True)
        return False


# ── Middleware ─────────────────────────────────────────────────────────────


class TaskIntentMiddleware(AgentMiddleware):
    """``before_model``: arming + plan-active steering.

    Registered in ``agent/core.py`` immediately AFTER
    ``SubagentCompletionDrainMiddleware`` so its injected message bypasses the
    sanitize rewrite on the injection turn (same rationale as the drain).
    """

    async def abefore_model(self, state, runtime=None) -> dict[str, Any] | None:
        """Inject a steering message, or return ``None`` (no-op)."""
        from agent.middlewares.agent_switch import middleware_enabled

        if not middleware_enabled(str((state or {}).get("session_id") or ""), type(self).__name__):
            return None  # 会话配置（预设-中间件）关闭该中间件时，本钩子整体 no-op。
        try:
            messages = state.get("messages", []) if isinstance(state, dict) else []
            if not messages:
                return None

            final = messages[-1]

            # Internal completion carrier drives its own turn — never steer it.
            if _is_internal_completion(final):
                logger.debug(
                    "TaskIntentMiddleware: final message is a subagent completion carrier; skip"
                )
                return None

            # Last genuine user message (system directives filtered out).
            last_human = None
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage) and not _is_system_directive(msg):
                    last_human = msg
                    break
            if last_human is None:
                return None

            # Per-turn dedup: only the first model call of a turn qualifies.
            if last_human is not final:
                return None

            # Multimodal turns arrive as a content BLOCK LIST, so the text is
            # extracted before any detection runs.
            content = _message_text(last_human.content)
            if not content.strip():
                return None

            session_id = str(state.get("session_id") or "")

            # ── Plan-active steering (priority over arming) ────────────────
            if _has_active_boulder(session_id):
                logger.info("TaskIntentMiddleware: plan-active reminder for session {}", session_id)
                return {"messages": [_task_intent_message(_PLAN_ACTIVE_REMINDER.strip())]}

            # ── First arming / short reminder ──────────────────────────────
            is_armed = bool(session_id) and session_id in _armed_sessions
            if is_armed:
                # Already in orchestrator mode: the rules alone decide whether
                # the reminder rides along. The LLM fallback serves FIRST
                # arming only, so a long session never pays it twice.
                if not _detect_task_intent(content):
                    return None
                steering = _task_intent_message(_TASK_STEERING_REMINDER)
                logger.info("TaskIntentMiddleware: re-arming reminder for session {}", session_id)
            else:
                # No session id means no ledger entry to arm, i.e. the one-shot
                # guarantee does not hold — keep those calls rule-only rather
                # than paying an LLM call on every turn.
                if not await _detect_task_intent_with_llm(content, allow_llm=bool(session_id)):
                    return None
                if session_id:
                    _armed_sessions.add(session_id)
                steering = _task_intent_message(_TASK_STEERING_PROMPT)
                logger.info("TaskIntentMiddleware: first-arm steering for session {}", session_id)

            return {"messages": [steering]}
        except Exception:
            logger.exception("TaskIntentMiddleware: failed; continuing without steering")
            return None

    def before_model(self, state, runtime=None) -> dict[str, Any] | None:
        """Sync best-effort path (production main agent streams async-only)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self.abefore_model(state, runtime))
            except Exception:
                logger.exception("TaskIntentMiddleware (sync) failed; continuing without steering")
                return None
        logger.debug("TaskIntentMiddleware: running loop detected; async-only path skips")
        return None


def rearm_after_compact(session_id: str) -> None:
    """Discard a session's armed flag so the full prompt is injected again.

    Called by the Summarization call site (wired in a later todo) after a
    successful compression; mirrors omo's ``session_compact`` → re-arm.
    """
    _armed_sessions.discard(session_id)
