"""E7: task-intent detection and steering — a ``before_model`` middleware.

Two independent components of the original design are fused here:

- **E7a — arming** (mirrors omo ``ultrawork``): when a user message looks like a
  work request and no plan is active, inject the full orchestrator steering
  prompt on the first qualifying turn, then a short reminder on later turns
  (module-level ``_armed_sessions`` ledger). ``rearm_after_compact`` clears the
  entry after context compression so the full prompt is injected again.
- **E7b — plan-active steering** (mirrors omo ``ulw-execute-continuation``
  input hook): when ``.omo/boulder.json`` holds an active/paused work whose plan
  file exists and contains a checkbox, append the plan-active reminder instead
  and skip E7a entirely (E7b has priority).

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
  / ``metadata.internal`` messages are never mistaken for user input, so E3/E7
  injections cannot re-trigger E7.
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
}

_QUESTION_PATTERNS = [
    r"^(what|how|why|where|who|when|can you|is it|are you|do you)\b",
    r"^(什么是|怎么|为什么|哪里|谁|什么时候|能否|是否|是不是|能不能)",
    r"^(explain|describe|tell me about)",
    r"^(解释|说明|介绍一下)",
]

_CHAT_PATTERNS = [
    r"^(hello|hi|hey|thanks|thank you|ok|good|great|bye)\b",
    r"^(你好|谢谢|好的|再见|嗯|哦)",
]


def _detect_task_intent(content: str) -> bool:
    """Return True when ``content`` looks like a work request.

    Strategy (conservative — a false negative only skips steering):
    1. question patterns (short messages only) → not a task;
    2. chat patterns (short messages only) → not a task;
    3. a task keyword anywhere → task;
    4. long messages (> 100 chars) that are neither question nor chat → task.
    """
    text = content.strip().lower()
    is_long = len(content) > 100

    if not is_long:
        for pattern in _QUESTION_PATTERNS:
            if re.match(pattern, text):
                return False
        for pattern in _CHAT_PATTERNS:
            if re.match(pattern, text):
                return False

    for keyword in _TASK_KEYWORDS:
        if keyword in text:
            return True

    return is_long


# ── Steering prompts ───────────────────────────────────────────────────────

# E7a: full steering (first arming, when no active plan).
_TASK_STEERING_PROMPT = """[SYSTEM DIRECTIVE: TASK INTENT DETECTED]

This message appears to be a work request. Before responding, assess the scope:

## If this is a multi-step task (2+ steps):

1. Load the ulw-execute skill to understand the orchestration workflow.
   Use the skill_view tool to read skill files by name when you need their
   full instructions.

2. You are an ORCHESTRATOR, not an implementer:
   - Create a plan (in .omo/plans/ if applicable) or use todowrite to register tasks
   - Set proper category, delegation, flow_id/step_id fields
   - For dependencies, register TaskFlow steps with depends_on and let TaskFlow
     block/unlock/parallel-dispatch — do NOT build a second DAG
   - DELEGATE implementation to subagents — you do NOT write code

3. Workflow:
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

# E7a light: short reminder once the session is already armed.
_TASK_STEERING_REMINDER = (
    "[SYSTEM DIRECTIVE: ORCHESTRATOR MODE ARMED]\n"
    "Orchestrator mode is already active for this session. "
    "The full directive above remains binding — re-read it and continue. "
    "Create todos FIRST for any multi-step work."
)

# E7b: plan-active steering reminder (appended when a boulder work is active).
_PLAN_ACTIVE_REMINDER = (
    "\n\n<sherry-ulw-execute>\n"
    "An active ulw-execute plan is present in this working directory.\n"
    "Before continuing, read `.omo/boulder.json` and the active plan file to "
    "determine what remains; use the ledger and plan as the source of truth.\n"
    "Continue the current work with evidence-bound execution; do not start "
    "unrelated work until every top-level checkbox is `- [x]`.\n"
    "</sherry-ulw-execute>"
)


# ── once-per-session arming ledger (mirrors omo ``armedSessionIds``) ───────

_armed_sessions: set[str] = set()

# Injectable for hermetic tests; production resolves relative to cwd.
_BOULDER_PATH: Path = Path(".omo/boulder.json")


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_system_directive(msg: Any) -> bool:
    """True when ``msg`` is a system-injected directive, not user input.

    Filters E3 continuation prompts and E7 steering so they can never be
    mistaken for a fresh user request (anti-loop).
    """
    content = getattr(msg, "content", None)
    if isinstance(content, str) and (
        content.startswith("[SYSTEM DIRECTIVE") or content.startswith("<sherry-ulw-execute>")
    ):
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


def _has_active_boulder() -> bool:
    """True when ``.omo/boulder.json`` holds continuable work.

    Mirrors omo ``findContinuableBoulderWork``: the active work's status is
    ``active`` or ``paused`` AND its plan file exists and contains at least one
    checkbox (``- [ ]`` or ``- [x]``). Any read/parse failure → False.
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
        plan_path = work.get("active_plan", "")
        if not plan_path:
            return False
        plan_file = Path(plan_path)
        if not plan_file.exists():
            return False
        plan_content = plan_file.read_text(encoding="utf-8")
        return "- [ ]" in plan_content or "- [x]" in plan_content
    except Exception:
        return False


# ── Middleware ─────────────────────────────────────────────────────────────


class TaskIntentMiddleware(AgentMiddleware):
    """``before_model``: E7a arming + E7b plan-active steering.

    Registered in ``agent/core.py`` immediately AFTER
    ``SubagentCompletionDrainMiddleware`` so its injected message bypasses the
    sanitize rewrite on the injection turn (same rationale as the drain).
    """

    async def abefore_model(self, state, runtime=None) -> dict[str, Any] | None:
        """Inject a steering message, or return ``None`` (no-op)."""
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

            content = last_human.content
            if not isinstance(content, str) or not content.strip():
                return None

            session_id = str(state.get("session_id") or "")

            # ── E7b: plan-active steering (priority over E7a) ──────────────
            if _has_active_boulder():
                logger.info(
                    "TaskIntentMiddleware E7b: plan-active reminder for session {}", session_id
                )
                return {"messages": [HumanMessage(content=_PLAN_ACTIVE_REMINDER.strip())]}

            # ── E7a: first arming / short reminder ─────────────────────────
            if not _detect_task_intent(content):
                return None

            is_armed = bool(session_id) and session_id in _armed_sessions
            if is_armed:
                steering = HumanMessage(content=_TASK_STEERING_REMINDER)
                logger.info(
                    "TaskIntentMiddleware E7a: re-arming reminder for session {}", session_id
                )
            else:
                if session_id:
                    _armed_sessions.add(session_id)
                steering = HumanMessage(content=_TASK_STEERING_PROMPT)
                logger.info(
                    "TaskIntentMiddleware E7a: first-arm steering for session {}", session_id
                )

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
    """Discard a session's armed flag so E7a re-injects the full prompt.

    Called by the Summarization call site (wired in a later todo) after a
    successful compression; mirrors omo's ``session_compact`` → re-arm.
    """
    _armed_sessions.discard(session_id)
