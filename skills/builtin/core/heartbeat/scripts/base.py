"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
from pathlib import Path
from loguru import logger
from config import HEARTBEAT_PATH
from models import auxiliary_llm
from .evaluate import evaluate_response
from typing import Any, Callable, Coroutine
from pydantic import BaseModel, Field


_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]

# Pydantic model for heartbeat decision (fallback path)
class _HeartbeatDecision(BaseModel):
    """Report heartbeat decision after reviewing tasks."""

    action: str = Field(
        description="skip = nothing to do, run = has active tasks",
        pattern=r"^(skip|run)$",
    )
    tasks: str = Field(
        default="",
        description="Natural-language summary of active tasks (required for run)",
    )


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
    ):
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _read_heartbeat_file() -> str | None:
        return Path(HEARTBEAT_PATH).read_text(encoding="utf-8")

    def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Falls back to ``with_structured_output`` if the underlying model
        does not support ``bind_tools`` (e.g. local GGUF branch).

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from pub_func import current_time_str
        from langchain_core.messages import HumanMessage, SystemMessage

        system_msg = f"You are a heartbeat agent. Call the heartbeat tool to report your decision."
        user_msg = (
            f"Current Time: {current_time_str(self.timezone)}\n\n"
            "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
            f"{content}"
        )

        # ── Attempt bind_tools path ──────────────────────────────────
        try:
            response = auxiliary_llm.bind_tools(_HEARTBEAT_TOOL).invoke([
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ])

            if response.tool_calls and len(response.tool_calls) > 0:
                args = response.tool_calls[0].get("args", {})
                return args.get("action", "skip"), args.get("tasks", "")

            # Empty tool_calls → treat as skip
            logger.debug("Heartbeat _decide: no tool calls returned, treating as skip")
            return "skip", ""
        except NotImplementedError:
            logger.warning("Heartbeat _decide: bind_tools not supported, falling back to with_structured_output")
        except Exception:
            logger.exception("Heartbeat _decide: bind_tools failed, falling back to with_structured_output")

        # ── Fallback: with_structured_output ──────────────────────────
        try:
            structured = auxiliary_llm.with_structured_output(_HeartbeatDecision).invoke([
                SystemMessage(content=system_msg),
                HumanMessage(content=user_msg),
            ])
            return structured.action, structured.tasks or ""
        except Exception:
            logger.exception("Heartbeat _decide: with_structured_output also failed, defaulting to skip")
            return "skip", ""

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)

                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()

        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response: str = await self.on_execute(tasks)
                if response:
                    should_notify:bool = evaluate_response(response, tasks)
                    if should_notify and self.on_notify:
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            return None
        action, tasks = self._decide(content)
        if action != "run" or not self.on_execute:
            return None
        return await self.on_execute(tasks)

heartbeat_service: HeartbeatService = HeartbeatService()