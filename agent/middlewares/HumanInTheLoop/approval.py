"""Command approval pipeline: hardline → deny → YOLO → allowlist → dangerous → human.

Stateful layer that manages per-session allowlists and delegates to
detection.py for pattern matching.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable

from loguru import logger
from runtime.state_register import state_register_mem

from .types import (
    ApprovalDecision, ApprovalMode, ApprovalResult, HITLConfig,
    SmartApprovalResult, _STATE_PREFIX, BLOCKED_MESSAGE,
)
from .detection import detect_hardline_command, detect_dangerous_command


def _is_yolo_active(config: HITLConfig) -> bool:
    if config.yolo_mode:
        return True
    if config.mode == ApprovalMode.OFF:
        return True
    return os.environ.get("SHERRY_YOLO_MODE", "").strip() in ("1", "true", "yes")


def _check_deny_rules(command: str, deny_rules: list[str]) -> str | None:
    import fnmatch
    for rule in deny_rules:
        if fnmatch.fnmatch(command, rule):
            return rule
    return None


def _extract_pattern(command: str) -> str:
    parts = command.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}*"
    return f"{parts[0]}*" if parts else command


def _args_hash(args: dict[str, Any]) -> str:
    try:
        serialized = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(args)
    return hashlib.md5(serialized.encode()).hexdigest()


def _get_state(session_id: str, key: str, default: Any = None) -> Any:
    return state_register_mem.get_state(session_id, f"{_STATE_PREFIX}:{key}", default)


def _set_state(session_id: str, key: str, value: Any) -> bool:
    return state_register_mem.set_state(session_id, f"{_STATE_PREFIX}:{key}", value)


class ApprovalPipeline:
    """Layer 1-6: command approval pipeline with allowlist management."""

    def __init__(self, config: HITLConfig, fire_hooks: Callable[[str, ApprovalResult], None]):
        self.config = config
        self._fire_hooks = fire_hooks

    def check_command(self, command: str, session_id: str) -> ApprovalResult:
        """Run the full approval pipeline on a command string."""
        # Layer 1: Hardline blocklist
        hardline = detect_hardline_command(command)
        if hardline:
            result = ApprovalResult(
                approved=False, decision=ApprovalDecision.DENY,
                reason=f"Hardline blocklist: {hardline}. {BLOCKED_MESSAGE}",
                pattern_key=hardline,
            )
            self._fire_hooks(session_id, result)
            return result

        # Layer 2: User deny rules
        deny_match = _check_deny_rules(command, self.config.deny_rules)
        if deny_match:
            result = ApprovalResult(
                approved=False, decision=ApprovalDecision.DENY,
                reason=f"User deny rule: {deny_match}. {BLOCKED_MESSAGE}",
                pattern_key=f"deny:{deny_match}",
            )
            self._fire_hooks(session_id, result)
            return result

        # Layer 3: YOLO bypass
        if _is_yolo_active(self.config):
            result = ApprovalResult(approved=True, decision=ApprovalDecision.ONCE, reason="YOLO mode active")
            self._fire_hooks(session_id, result)
            return result

        # Layer 4: Permanent allowlist
        permanent: list[str] = _get_state(session_id, "permanent", [])
        import fnmatch
        for pattern_str in permanent:
            if fnmatch.fnmatch(command, pattern_str):
                result = ApprovalResult(approved=True, decision=ApprovalDecision.ALWAYS, reason="Permanent allowlist match")
                self._fire_hooks(session_id, result)
                return result

        # Layer 5: Session allowlist
        session_list: list[str] = _get_state(session_id, "session_approved", [])
        for pattern_str in session_list:
            if fnmatch.fnmatch(command, pattern_str):
                result = ApprovalResult(approved=True, decision=ApprovalDecision.SESSION, reason="Session allowlist match")
                self._fire_hooks(session_id, result)
                return result

        # Layer 5: Dangerous pattern detection
        dangerous = detect_dangerous_command(command)
        if not dangerous:
            result = ApprovalResult(approved=True, decision=ApprovalDecision.ONCE, reason="No dangerous patterns detected")
            self._fire_hooks(session_id, result)
            return result

        return ApprovalResult(
            approved=False, decision=None,
            reason=f"Dangerous pattern(s): {', '.join(tag for _, tag in dangerous)}. Requires human approval.",
            pattern_key=dangerous[0][1],
        )

    def check_command_with_approval(
        self, command: str, session_id: str,
        prompt_fn: Callable[[str], ApprovalDecision] | None = None,
    ) -> ApprovalResult:
        auto_result = self.check_command(command, session_id)
        if auto_result.approved or auto_result.decision == ApprovalDecision.DENY:
            return auto_result
        if prompt_fn is None:
            return ApprovalResult(
                approved=False, decision=ApprovalDecision.DENY,
                reason="Human approval required but no prompt function. Defaulting to deny.",
            )
        decision = prompt_fn(command)
        return self._apply_decision(auto_result.pattern_key, decision, session_id, command)

    def _apply_decision(
        self, pattern_key: str, decision: ApprovalDecision, session_id: str, command: str
    ) -> ApprovalResult:
        if decision == ApprovalDecision.DENY:
            result = ApprovalResult(
                approved=False, decision=ApprovalDecision.DENY,
                reason=f"User denied. {BLOCKED_MESSAGE}", pattern_key=pattern_key,
            )
        elif decision == ApprovalDecision.ALWAYS:
            self._add_to_permanent(session_id, command)
            result = ApprovalResult(
                approved=True, decision=ApprovalDecision.ALWAYS,
                reason="User approved permanently", pattern_key=pattern_key,
            )
        elif decision == ApprovalDecision.SESSION:
            self._add_to_session(session_id, command)
            result = ApprovalResult(
                approved=True, decision=ApprovalDecision.SESSION,
                reason="User approved for this session", pattern_key=pattern_key,
            )
        else:
            result = ApprovalResult(
                approved=True, decision=ApprovalDecision.ONCE,
                reason="User approved once", pattern_key=pattern_key,
            )
        self._fire_hooks(session_id, result)
        return result

    def _add_to_permanent(self, session_id: str, command: str):
        current: list[str] = _get_state(session_id, "permanent", [])
        pattern = _extract_pattern(command)
        if pattern not in current:
            current.append(pattern)
            _set_state(session_id, "permanent", current)

    def _add_to_session(self, session_id: str, command: str):
        current: list[str] = _get_state(session_id, "session_approved", [])
        pattern = _extract_pattern(command)
        if pattern not in current:
            current.append(pattern)
            _set_state(session_id, "session_approved", current)

    # ── Layer 6: Smart approval ─────────────────────────────────────────

    def smart_approve(self, command: str) -> SmartApprovalResult:
        llm = self.config.smart_approval_llm
        if llm is None:
            return SmartApprovalResult.ESCALATE
        try:
            response = llm.invoke(
                f"Assess the risk of this shell command on a scale of "
                f"safe/suspicious/dangerous. Reply with ONLY one word.\n\n"
                f"Command: {command}"
            )
            text = str(response).strip().lower()
            if "safe" in text:
                return SmartApprovalResult.APPROVE
            if "dangerous" in text:
                return SmartApprovalResult.DENY
            return SmartApprovalResult.ESCALATE
        except Exception:
            logger.exception("Smart approval LLM call failed, escalating")
            return SmartApprovalResult.ESCALATE

    # ── Layer 10: Plugin tool approval ───────────────────────────────────

    def request_tool_approval(
        self, tool_name: str, tool_args: dict[str, Any], session_id: str,
    ) -> ApprovalResult:
        key = f"tool_approved:{tool_name}"
        approved_args: dict[str, bool] = _get_state(session_id, key, {})
        args_hash = _args_hash(tool_args)
        if args_hash in approved_args:
            return ApprovalResult(approved=True, decision=ApprovalDecision.SESSION, reason="Previously session-approved")
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"Tool '{tool_name}' requires human approval. Defaulting to deny.",
        )

    def approve_tool_for_session(self, tool_name: str, tool_args: dict[str, Any], session_id: str):
        key = f"tool_approved:{tool_name}"
        approved_args: dict[str, bool] = _get_state(session_id, key, {})
        approved_args[_args_hash(tool_args)] = True
        _set_state(session_id, key, approved_args)
