"""Command approval pipeline — 6-layer escalation from hardline to human.

Layers (executed in order):
    1. Hardline blocklist  — unconditional deny (from :mod:`detection`)
    2. User deny rules      — glob-style patterns configured in :class:`HITLConfig`
    3. YOLO bypass          — skip all checks when YOLO mode is active
    4. Permanent allowlist  — cross-session approved patterns (persistent state)
    5. Session allowlist    — per-session approved patterns (in-memory state)
    6. Dangerous detection  — pattern-match from :mod:`detection`, escalate to human

Smart approval (LLM-assisted) and plugin tool approval are additional layers
provided as separate methods.
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
    """Check whether YOLO (bypass-all) mode is active.

    Activated by any of:
    - ``config.yolo_mode == True``
    - ``config.mode == ApprovalMode.OFF``
    - Environment variable ``SHERRY_YOLO_MODE`` set to ``1`` / ``true`` / ``yes``
    """
    if config.yolo_mode:
        return True
    if config.mode == ApprovalMode.OFF:
        return True
    return os.environ.get("SHERRY_YOLO_MODE", "").strip() in ("1", "true", "yes")


def _check_deny_rules(command: str, deny_rules: list[str]) -> str | None:
    """Return the first deny-rule glob that matches *command*, or ``None``."""
    import fnmatch
    for rule in deny_rules:
        if fnmatch.fnmatch(command, rule):
            return rule
    return None


def _extract_pattern(command: str) -> str:
    """Extract a glob-style pattern from a command string for allowlist storage.

    E.g. ``"git push --force origin main"`` → ``"git push*"``.
    """
    parts = command.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}*"
    return f"{parts[0]}*" if parts else command


def _args_hash(args: dict[str, Any]) -> str:
    """Return an MD5 hash of serialized tool arguments for deduplication."""
    try:
        serialized = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(args)
    return hashlib.md5(serialized.encode()).hexdigest()


def _get_state(session_id: str, key: str, default: Any = None) -> Any:
    """Read a value from the HITL namespace in the in-memory state register."""
    return state_register_mem.get_state(session_id, f"{_STATE_PREFIX}:{key}", default)


def _set_state(session_id: str, key: str, value: Any) -> bool:
    """Write a value to the HITL namespace in the in-memory state register."""
    return state_register_mem.set_state(session_id, f"{_STATE_PREFIX}:{key}", value)


class ApprovalPipeline:
    """6-layer command approval pipeline with allowlist management.

    Layers (in order):
    1. Hardline blocklist — unconditional deny
    2. User-configured deny rules (glob patterns)
    3. YOLO mode bypass — skip all checks
    4. Permanent allowlist — cross-session approved patterns
    5. Session allowlist — per-session approved patterns
    6. Dangerous pattern detection — escalate dangerous commands to human approval

    The pipeline is called from :class:`~agent.middlewares.HumanInTheLoop.core.HumanInTheLoop`
    middleware. After a human decision, :meth:`_apply_decision` persists the choice
    back to the allowlists.
    """

    def __init__(self, config: HITLConfig, fire_hooks: Callable[[str, ApprovalResult], None]):
        """Initialize the pipeline.

        Args:
            config: HITL configuration dataclass.
            fire_hooks: Callback invoked after every approval decision (for event tracking).
        """
        self.config = config
        self._fire_hooks = fire_hooks

    def check_command(self, command: str, session_id: str) -> ApprovalResult:
        """Run the full approval pipeline on a command string.

        Args:
            command: The raw shell command to evaluate.
            session_id: Active session ID for state lookups.

        Returns:
            :class:`ApprovalResult` — ``approved=True`` if the command passes all layers,
            ``approved=False`` (with ``decision=DENY``) if blocked before dangerous detection,
            or ``approved=False`` (with ``decision=None``) if escalated for human approval.
        """
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
        """Run the pipeline and, if escalated, call *prompt_fn* for human decision.

        Args:
            command: Shell command to evaluate.
            session_id: Active session ID.
            prompt_fn: Callback that takes a command string and returns an
                :class:`ApprovalDecision`. If ``None``, defaults to deny.

        Returns:
            :class:`ApprovalResult` — final decision after human input if applicable.
        """
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
        """Persist a human decision and return the corresponding result.

        - ``DENY`` → returns blocked result
        - ``ALWAYS`` → adds to permanent allowlist
        - ``SESSION`` → adds to session allowlist
        - ``ONCE`` → approves for this invocation only

        Every decision fires the hooks callback.
        """
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
        """Add a command pattern to the permanent (cross-session) allowlist."""
        current: list[str] = _get_state(session_id, "permanent", [])
        pattern = _extract_pattern(command)
        if pattern not in current:
            current.append(pattern)
            _set_state(session_id, "permanent", current)

    def _add_to_session(self, session_id: str, command: str):
        """Add a command pattern to the session (in-memory) allowlist."""
        current: list[str] = _get_state(session_id, "session_approved", [])
        pattern = _extract_pattern(command)
        if pattern not in current:
            current.append(pattern)
            _set_state(session_id, "session_approved", current)

    # ── Layer 6: Smart approval ─────────────────────────────────────────

    def smart_approve(self, command: str) -> SmartApprovalResult:
        """Use an LLM to auto-assess whether a command is safe.

        Requires ``config.smart_approval_llm`` to be set. The LLM is asked to
        classify the command as ``safe`` / ``suspicious`` / ``dangerous``.

        Returns:
            :class:`SmartApprovalResult` — ``APPROVE``, ``DENY``, or ``ESCALATE``
            if the LLM is unavailable or uncertain.
        """
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
        """Check whether a plugin tool invocation has been pre-approved.

        Previously session-approved tool calls (by args hash) are auto-approved.
        All others are denied by default and must go through :meth:`approve_tool_for_session`.
        """
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
        """Mark a specific tool + args combination as approved for the current session."""
        key = f"tool_approved:{tool_name}"
        approved_args: dict[str, bool] = _get_state(session_id, key, {})
        approved_args[_args_hash(tool_args)] = True
        _set_state(session_id, key, approved_args)
