"""User-configurable lifecycle hooks — the dispatcher.

Operator-supplied commands observed against the agent's turn (``sherry.jsonc``
``hooks`` section; see ``config/sherry_settings.py::load_sherry_hooks``). This
is **Step 1** of the plan: the events are dispatched read-only — a hook can
audit, notify and meter, and that is all. It cannot block a tool, rewrite its
arguments or inject context into the conversation.

Events served here (the Step-1 subset; the config vocabulary is wider so a
config written ahead of the implementation validates):

* ``PostToolUse`` — a tool call returned a (non-error) result,
* ``PostToolUseFailure`` — a tool call returned an error result,
* ``Stop`` — the turn ended.

Deliberately NOT served: ``PreToolUse`` (allow/deny — needs a trust gate this
build does not have), ``UserPromptSubmit`` / ``SessionStart`` (context
injection / process events) and the ``updatedInput`` rewrite. A dispatch to
one of those logs once and does nothing.

Safety posture (Step 1):

* Hooks run only when ``hooks.enabled`` is true (default OFF) — a tracked
  config file must not make the agent execute commands on its own.
* The command is ``shlex.split`` into an argv; there is NO shell.
* The environment is scrubbed (``scrub_env``) and carries ``SHERRY_SESSION_ID``.
* A timeout (declared or default, clamped) kills the process; nothing waits
  forever. Dispatch is serial per event and never raises: a broken hook is a
  log line, never a failed turn.
* stdout is read up to a byte cap and parsed as the optional outcome JSON;
  non-JSON output is success (a notification hook returns nothing). stderr is
  logged (capped) and never reaches the model.
* The payload carries names, ids and PREVIEWS — never the full prompt, full
  tool output or any secret.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from collections.abc import Callable, Awaitable

from langgraph.runtime import Runtime
from loguru import logger
from langgraph.typing import ContextT
from langgraph.errors import GraphInterrupt
from langgraph.prebuilt.tool_node import ToolCallRequest
from typing import override
from langchain_core.messages import ToolMessage
from langchain.agents.middleware import AgentMiddleware, AgentState

from config.features import USER_HOOKS
from config.sherry_settings import load_sherry_hooks
from agent.middlewares.agent_switch import middleware_enabled
from agent.tools.pub_base.env_scrub import scrub_env

__all__ = ["STEP1_EVENTS", "HookOutcome", "UserHooksMiddleware", "hook_matcher_matches"]

#: The events THIS build dispatches. The rest of the vocabulary validates in the
#: config (so a config written for the full plan loads) but never dispatches —
#: ``_runners_for`` refuses them, the constructor says so once.
STEP1_EVENTS: frozenset[str] = frozenset({"PostToolUse", "PostToolUseFailure", "Stop"})


@dataclass
class HookOutcome:
    """What one hook run produced.

    Step 1 reads ``status`` (and logs the rest): ``decision`` / ``updated_input``
    are the Step-2/3 fields, parsed so the contract is visible and refuses to
    grow silently.
    """

    status: str  # "success" | "blocked" | "failed" | "cancelled" | "timed_out"
    decision: str | None = None
    reason: str | None = None
    updated_input: dict | None = None
    additional_context: str | None = None
    exit_code: int | None = None


def hook_matcher_matches(matcher: str, tool_name: str) -> bool:
    """Whether a hook's ``matcher`` selects this tool.

    Three forms, in this order:

    * ``""`` or ``"*"`` — every tool;
    * ``/regex/`` (slashes on both ends) — a regular expression, matched with
      ``re.search``; an invalid pattern never matches (fail-closed) and is
      logged once by the caller;
    * anything else — a ``|``-separated list of shell-style globs
      (``write_*|patch_file``).
    """
    if not matcher or matcher == "*":
        return True
    if len(matcher) > 2 and matcher.startswith("/") and matcher.endswith("/"):
        import re

        try:
            return re.search(matcher[1:-1], tool_name) is not None
        except re.error:
            return False
    return any(fnmatchcase(tool_name, part.strip()) for part in matcher.split("|") if part.strip())


def _preview(value: Any, limit: int) -> str:
    """A bounded, single-line-ish text preview of a tool result / error."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[{len(text) - limit} chars truncated]"


class _HookRunner:
    """Runs ONE hook spec; every failure is an outcome, never an exception."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.command: str = spec["command"]
        self.matcher: str = spec.get("matcher", "*")
        self.timeout_ms: int = int(spec.get("timeout_ms") or USER_HOOKS["default_timeout_ms"])
        self.blocking: bool = bool(spec.get("blocking"))

    async def run(self, payload: dict[str, Any], session_id: str) -> HookOutcome:
        """Spawn the hook, feed it the payload JSON on stdin, read its outcome."""
        try:
            argv = shlex.split(self.command)
        except ValueError as error:  # unbalanced quotes
            return HookOutcome(status="failed", reason=f"command does not parse: {error}")
        if not argv:
            return HookOutcome(status="failed", reason="empty command")

        env = scrub_env()
        env["SHERRY_SESSION_ID"] = session_id
        env["SHERRY_HOOK_EVENT"] = str(payload.get("event") or "")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as error:
            return HookOutcome(status="failed", reason=f"spawn failed: {error}")

        stdin_data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=self.timeout_ms / 1000
            )
        except TimeoutError:
            _kill(proc)
            return HookOutcome(status="timed_out", reason=f"no exit within {self.timeout_ms} ms")
        except asyncio.CancelledError:
            _kill(proc)
            raise

        stderr_text = stderr[: USER_HOOKS["max_output_bytes"]].decode("utf-8", errors="replace")
        if stderr_text.strip():
            logger.debug(
                "hook stderr ({}): {}",
                self.command,
                stderr_text[: USER_HOOKS["stderr_preview_chars"]],
            )
        if proc.returncode != 0:
            return HookOutcome(
                status="failed",
                reason=f"exit {proc.returncode}: {stderr_text[:400]}",
                exit_code=proc.returncode,
            )

        raw = stdout[: USER_HOOKS["max_output_bytes"]].decode("utf-8", errors="replace").strip()
        if not raw:
            return HookOutcome(status="success", exit_code=0)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # A notification hook prints whatever it likes: non-JSON stdout is a
            # successful run, not an error (the plan's contract).
            return HookOutcome(status="success", exit_code=0)
        if not isinstance(parsed, dict):
            return HookOutcome(status="success", exit_code=0)
        updated = parsed.get("updatedInput")
        return HookOutcome(
            status="success",
            decision=parsed.get("decision") if isinstance(parsed.get("decision"), str) else None,
            reason=parsed.get("reason") if isinstance(parsed.get("reason"), str) else None,
            updated_input=updated if isinstance(updated, dict) else None,
            additional_context=(
                parsed.get("additionalContext")
                if isinstance(parsed.get("additionalContext"), str)
                else None
            ),
            exit_code=0,
        )


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of a hook that outlived its budget."""
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


class UserHooksMiddleware(AgentMiddleware):
    """Dispatch user-configured lifecycle hooks (Step 1: read-only events).

    Registered after ``ToolGuardrails`` (a guardrail-blocked call never reaches
    a hook) and before the approval/persistence layers, so ``PostToolUse`` sees
    the result the model will see. Every hook is best-effort: the tool result is
    returned untouched whatever the hooks do, and the config is read once at
    construction (a change needs a restart, like the other boot-read settings).
    """

    def __init__(self, hooks: dict[str, Any] | None = None) -> None:
        super().__init__()
        config = hooks if hooks is not None else load_sherry_hooks()
        self._enabled: bool = bool(config.get("enabled"))
        self._hooks: dict[str, list[_HookRunner]] = {
            event: [_HookRunner(spec) for spec in specs]
            for event, specs in config.items()
            if event != "enabled" and isinstance(specs, list)
        }
        if self._enabled and self._hooks:
            logger.info(
                "user hooks enabled: {}", {event: len(runs) for event, runs in self._hooks.items()}
            )
        for event in ("PreToolUse", "UserPromptSubmit", "SessionStart"):
            if self._hooks.get(event):
                logger.warning(
                    "user hooks: {} is configured but not dispatched in this build "
                    "(Step 1 serves PostToolUse / PostToolUseFailure / Stop); entries are ignored",
                    event,
                )

    # -- dispatch ---------------------------------------------------------

    def _runners_for(self, event: str, tool_name: str | None) -> list[_HookRunner]:
        """The specs of one event that match this tool (config order).

        An event outside :data:`STEP1_EVENTS` has no runners whatever the
        config says: this build cannot honour a blocking or context-injecting
        hook, and half-honouring one would be worse than ignoring it.
        """
        if event not in STEP1_EVENTS:
            return []
        if not (self._enabled and self._hooks.get(event)):
            return []
        if tool_name is None:
            return list(self._hooks[event])
        return [
            runner
            for runner in self._hooks[event]
            if hook_matcher_matches(runner.matcher, tool_name)
        ]

    async def _dispatch(
        self, event: str, payload: dict[str, Any], session_id: str, tool_name: str | None = None
    ) -> list[tuple[_HookRunner, HookOutcome]]:
        """Run every matching hook for one event, serially, never raising."""
        results: list[tuple[_HookRunner, HookOutcome]] = []
        for runner in self._runners_for(event, tool_name):
            payload_with_event = {"event": event, "sessionId": session_id, **payload}
            try:
                outcome = await runner.run(payload_with_event, session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a hook must never break a turn
                outcome = HookOutcome(status="failed", reason=f"{type(error).__name__}: {error}")
            results.append((runner, outcome))
            logger.bind(middleware=type(self).__name__).debug(
                "hook {} for {} -> {} ({} ms budget)",
                event,
                runner.command,
                outcome.status,
                runner.timeout_ms,
            )
        return results

    # -- hooks ------------------------------------------------------------

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        if not self._enabled or not self._hooks:
            return await handler(request)

        session_id = self._session_of_request(request)
        if not middleware_enabled(session_id, type(self).__name__):
            return await handler(request)

        call = request.tool_call or {}
        tool_name = str(call.get("name") or getattr(request.tool, "name", "") or "")
        tool_call_id = str(call.get("id") or "")
        started = time.monotonic()
        try:
            result = await handler(request)
        except GraphInterrupt:
            # The turn is PARKED for approval, not failed: a hook must not see a
            # failure it would report as one.
            raise
        duration_ms = int(max(0.0, (time.monotonic() - started) * 1000))

        failed = str(getattr(result, "status", "") or "") == "error"
        event = "PostToolUseFailure" if failed else "PostToolUse"
        payload: dict[str, Any] = {
            "toolName": tool_name,
            "toolCallId": tool_call_id,
            "durationMs": duration_ms,
        }
        if failed:
            payload["error"] = _preview(result.content, USER_HOOKS["error_preview_chars"])
        else:
            payload["result"] = _preview(result.content, USER_HOOKS["result_preview_chars"])
        await self._dispatch(event, payload, session_id, tool_name)
        return result

    @override
    async def aafter_agent(
        self, state: AgentState, runtime: Runtime[ContextT] | None = None
    ) -> None:
        if not (self._enabled and self._hooks.get("Stop")):
            return None
        session_id = ""
        if isinstance(state, dict):
            session_id = str(state.get("session_id") or "")
        if not middleware_enabled(session_id, type(self).__name__):
            return None
        messages = list((state or {}).get("messages") or []) if isinstance(state, dict) else []
        await self._dispatch(
            "Stop",
            {
                "responsePreview": _preview(
                    _last_ai_text(messages), USER_HOOKS["response_preview_chars"]
                ),
                "toolCallCount": sum(1 for message in messages if isinstance(message, ToolMessage)),
            },
            session_id,
        )
        return None

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _session_of_request(request: ToolCallRequest) -> str:
        """The session id a tool call belongs to (state first, then runtime)."""
        state = getattr(request, "state", None) or {}
        if isinstance(state, dict):
            session_id = str(state.get("session_id") or "")
            if session_id:
                return session_id
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        session_id = getattr(context, "session_id", "") if context is not None else ""
        return str(session_id or "")


def _last_ai_text(messages: list[Any]) -> str:
    """The last AIMessage's text (the turn's response preview source)."""
    for message in reversed(messages):
        if type(message).__name__ != "AIMessage":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") in (None, "text")
            )
    return ""
