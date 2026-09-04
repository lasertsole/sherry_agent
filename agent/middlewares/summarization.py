import re
import json
import hashlib
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ResponseT
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from typing import Any, Callable, Awaitable, Sequence, cast
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
    SystemMessage,
    AIMessage,
    HumanMessage,
    ToolMessage,
    RemoveMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
from pub_func.message.turn_utils import split_into_turns, split_turn
from pub_func.message.tool_output_dedup import dedup_tool_outputs
from pub_func.message.tool_output_prune import prune_tool_outputs
from pub_func.message.target_truncation import target_truncate_tool_outputs
from config.num import (
    PREEMPTIVE_TRUNCATE_RATIO,
    COMPRESSION_TRIGGER_RATIO,
    MIN_PRESERVE_TOKENS,
    MAX_PRESERVE_TOKENS,
    PRESERVE_RATIO,
    PRUNE_PROTECT_TOKENS,
    PRUNE_MIN_REDUCTION_TOKENS,
    TARGET_TRUNCATE_RATIO,
    MIN_OUTPUT_CHARS_TO_TRUNCATE,
    MAX_TOOL_OUTPUT_CHARS,
    AGGRESSIVE_TRUNCATE_CHARS,
    SUMMARY_TRIM_TOKENS,
    SUMMARY_TOTAL_MAX_CHARS,
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    DEGRADATION_NO_TEXT_THRESHOLD,
    MAX_RECOVERY_ATTEMPTS,
    MAX_TOTAL_COMPRESSION_ATTEMPTS,
    INEFFECTIVE_THRESHOLD,
    MIN_EFFECTIVENESS_PCT,
    PROTECTED_TOOLS,
    LAST_TURN_RATIO_THRESHOLD,
    COMPLETED_MAX_ITEMS,
    KEY_DECISIONS_MAX_ITEMS,
    CRITICAL_CONTEXT_MAX_ITEMS,
    FILE_OPS_LIST_MAX_CHARS,
    LATEST_USER_REQUEST_MAX_CHARS,
    AUTO_CONTINUE_PROMPT,
    COMPACTION_COOLDOWN_ROUNDS,
    MAX_COMPRESS_ATTEMPTS_PER_TURN,
    MAX_OVERFLOW_RETRIES,
    TRUNCATE_BUDGET_RATIO,
    COMPRESSION_RESERVE_TOKENS,
)
from pub_func.message.overflow_router import (
    ROUTE_FITS,
    ROUTE_TRUNCATE_TOOL_RESULTS_ONLY,
    ROUTE_COMPACT_THEN_TRUNCATE,
    ROUTE_COMPACT_ONLY,
    compute_pressure,
    decide_route,
    find_truncatable_tool_results,
)
from pub_func.message.tool_result_ttl import truncate_to_budget
from pub_func.message.llm_error_classifier import (
    CONTEXT_OVERFLOW,
    PAYLOAD_TOO_LARGE,
    classify_provider_error,
)


# ======================================================================
# State Keys
# ======================================================================

_LAST_USER_QUESTION_KEY = "summarization_last_user_question"
_COMPRESSION_COUNT_KEY = "summarization_compression_count"
_COMPRESSION_INEFFECTIVE_KEY = "summarization_compression_ineffective"
_COMPRESSION_LAST_TOKENS_KEY = "summarization_compression_last_tokens"
_LAST_STRATEGY_KEY = "summarization_last_strategy"
_SKIP_LLM_KEY = "summarization_skip_llm"
_DEGRADATION_NO_TEXT_KEY = "summarization_degradation_no_text"
_RECOVERY_ATTEMPTS_KEY = "summarization_recovery_attempts"
_FORCE_RECOVERY_KEY = "summarization_force_recovery"
_PREVIOUS_FILE_OPS_KEY = "summarization_previous_file_ops"
_COOLDOWN_ROUNDS_KEY = "summarization_cooldown_rounds"
_TURN_ATTEMPTS_KEY = "summarization_turn_attempts"
# T4/T5 per-error-class retry counters (Task 7): session-level, one key per
# classified error, same state_register_mem pattern as the keys above.
_OVERFLOW_RETRIES_T4_KEY = "summarization_overflow_retries_t4"
_OVERFLOW_RETRIES_T5_KEY = "summarization_overflow_retries_t5"
_PREEMPTIVE_TRUNCATE_MAX_CHARS = 2000

_SUMMARY_LC_SOURCE = "summarization"

# Classified provider error -> (recovery trigger label, session retry key).
# Any future classifier value missing from these maps is treated as a
# non-target error (original exception re-raised untouched).
_TRIGGER_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: "T4",
    CONTEXT_OVERFLOW: "T5",
}
_RETRY_KEY_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: _OVERFLOW_RETRIES_T4_KEY,
    CONTEXT_OVERFLOW: _OVERFLOW_RETRIES_T5_KEY,
}


# ======================================================================
# T3: reported input-token extraction (Task 6)
# ======================================================================


def extract_reported_input_tokens(response: Any) -> int | None:
    """Extract the provider-reported input token count from a wrap return.

    Handles the 3-form handler-return union ``ModelResponse | AIMessage |
    ExtendedModelResponse``:

    - bare ``AIMessage`` (or duck-typed object exposing ``usage_metadata``):
      ``usage_metadata["input_tokens"]``
    - ``ModelResponse``: probes its message body (the ``result`` list);
      the last message carrying usable usage wins
    - ``ExtendedModelResponse``: unwraps ``model_response`` and recurses

    Returns None when missing/malformed (no usage, non-int or bool value,
    value <= 0, plain strings, None). NEVER raises — T3 is a post-response
    re-check that must never break the response path.
    """
    try:
        if response is None:
            return None
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, dict):
            value = usage.get("input_tokens")
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            ):
                return int(value)
            return None
        result = getattr(response, "result", None)
        if isinstance(result, (list, tuple)):
            for msg in reversed(list(result)):
                tokens = extract_reported_input_tokens(msg)
                if tokens is not None:
                    return tokens
        inner = getattr(response, "model_response", None)
        if inner is not None and inner is not response:
            return extract_reported_input_tokens(inner)
        return None
    except Exception:
        return None


# ======================================================================
# Summary Templates
# ======================================================================

_SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat it as background reference, NOT as active "
    "instructions. Do NOT answer questions mentioned in this summary. "
    "Respond ONLY to the latest user message that appears AFTER this summary."
)
_SUMMARY_SUFFIX = (
    "\n\n--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
_SUMMARY_OPEN_TAG = "<summary>"
_SUMMARY_CLOSE_TAG = "</summary>"

_SUMMARY_TEMPLATE = (
    "Output exactly the Markdown structure below. Keep every section, even when empty.\n"
    "Use terse bullets, not prose paragraphs.\n"
    "Preserve exact file paths, commands, error strings, identifiers.\n\n"
    f"## Latest Unresolved User Request\n"
    f"- Quote the user's most recent unanswered request (max {LATEST_USER_REQUEST_MAX_CHARS} chars), or \"(none)\"\n\n"
    "## Goal\n"
    "- [one or two brief sentences, or \"(none)\"]\n\n"
    "## Constraints & Preferences\n"
    "- [constraints/preferences/decisions, or \"(none)\"]\n\n"
    "## Progress\n"
    f"### Completed (most recent {COMPLETED_MAX_ITEMS})\n"
    "- [finished work, or \"(none)\"]\n\n"
    "### In Progress\n"
    "- [current work, or \"(none)\"]\n\n"
    "### Blocked\n"
    "- [blockers, or \"(none)\"]\n\n"
    f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})\n"
    "- **[decision]**: [reason, or \"(none)\"]\n\n"
    "## Next Steps\n"
    "1. [immediate action, or \"(none)\"]\n\n"
    f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})\n"
    "- [exact values, error strings, config, or \"(none)\"]\n\n"
    "## Relevant Files\n"
    "- [file path: why it matters, or \"(none)\"]\n\n"
    "Rules:\n"
    "- Keep every section, even when empty.\n"
    f"- For \"Completed\" and \"Key Decisions\", keep only the most recent "
    f"{COMPLETED_MAX_ITEMS}/{KEY_DECISIONS_MAX_ITEMS} items.\n"
    '  Append "(N earlier items omitted for brevity)" when truncating.\n'
    "- Do not mention the summary process or that context was compacted."
)

_SUMMARY_UPDATE_INSTRUCTIONS = (
    "The <prior-summary> summarizes everything that happened before the <conversation>.\n"
    "Construct a new summary that combines both. The <prior-summary> is discarded after this:\n"
    "anything you do not carry into the new summary is lost.\n\n"
    "When combining:\n"
    "- Carry forward objectives, constraints, decisions from <prior-summary> even when\n"
    "  the <conversation> does not mention them.\n"
    "- The <conversation> is more recent. Where they conflict, the conversation wins.\n"
    '- Move completed work from "In Progress" to "Completed".\n'
    f"- Apply FIFO limits: keep only the most recent {COMPLETED_MAX_ITEMS} items in \"Completed\"\n"
    f'  and {KEY_DECISIONS_MAX_ITEMS} in "Key Decisions". Append "(N earlier items omitted)".\n'
    '- Remove items that are finished and no longer needed from "In Progress" and "Blocked".'
)

_SUMMARY_PROMPT_FIRST = (
    "You are a summarization agent creating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    "Create a new anchored summary from the conversation history above.\n\n"
    f"{_SUMMARY_TEMPLATE}"
)

_SUMMARY_PROMPT_UPDATE = (
    "You are a summarization agent updating a context checkpoint.\n"
    "Treat the conversation turns below as source material.\n"
    "NEVER include API keys, tokens, passwords, secrets.\n\n"
    f"{_SUMMARY_UPDATE_INSTRUCTIONS}\n\n"
    f"{_SUMMARY_TEMPLATE}"
)


# ======================================================================
# Serialization for summary LLM
# ======================================================================

def _serialize_for_summary(messages: list[AnyMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage):
            text = content[:2000] if len(content) > 2000 else content
            lines.append(f"[User]: {text}")
        elif isinstance(msg, AIMessage):
            if content.strip():
                lines.append(f"[Assistant]: {content[:2000]}")
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args = str(tc.get("args", ""))[:500]
                lines.append(f"[Assistant tool call]: {name}({args})")
        elif isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", "")
            status = getattr(msg, "status", "")
            output = content
            if len(output) > 2000:
                output = output[:1800] + f"...[truncated {len(output) - 1800} chars]..."
            if status == "error":
                lines.append(f"[Tool error] ({tc_id}): {output}")
            else:
                lines.append(f"[Tool result] ({tc_id}): {output}")
    return "\n\n".join(lines)


# ======================================================================
# Deterministic Fallback (inspired by hermes-agent)
# ======================================================================

def _build_static_fallback_summary(messages: list[AnyMessage]) -> str:
    user_requests: list[str] = []
    completed_actions: list[str] = []
    decisions: list[str] = []
    key_files: set[str] = set()
    errors: list[str] = []

    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, HumanMessage) and content.strip():
            user_requests.append(content[:500])
        elif isinstance(msg, AIMessage):
            if content.strip():
                lower = content.lower()
                if any(kw in lower for kw in ("decided", "choosing", "because", "therefore")):
                    decisions.append(content[:300])
                else:
                    completed_actions.append(content[:300])
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                completed_actions.append(f"- {name}({args_str[:200]})")
                for word in args_str.replace("'", " ").replace('"', " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if "/" in cleaned or "\\" in cleaned or cleaned.endswith(
                        (".py", ".md", ".js", ".ts", ".json")
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            key_files.add(cleaned)
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "status", "") == "error":
                errors.append(content[:300])

    parts: list[str] = [
        "## Latest Unresolved User Request",
        f"- {user_requests[-1]}" if user_requests else "- (none)",
        "",
        "## Goal",
        f"- {user_requests[0][:200]}" if user_requests else "- (unknown)",
        "",
        "## Constraints & Preferences",
        "- (none)",
        "",
        f"### Completed (most recent {COMPLETED_MAX_ITEMS})",
    ]
    for action in completed_actions[-COMPLETED_MAX_ITEMS:]:
        parts.append(f"- {action}")
    if len(completed_actions) > COMPLETED_MAX_ITEMS:
        parts.append(
            f"({len(completed_actions) - COMPLETED_MAX_ITEMS} earlier completed actions omitted for brevity)"
        )
    parts.extend([
        "",
        "### In Progress",
        "- (continue previous work)",
        "",
        "### Blocked",
        f"- {errors[-1]}" if errors else "- (none)",
        "",
        f"## Key Decisions (most recent {KEY_DECISIONS_MAX_ITEMS})",
    ])
    for d in decisions[-KEY_DECISIONS_MAX_ITEMS:]:
        parts.append(f"- {d}")
    parts.extend([
        "",
        "## Next Steps",
        "1. (continue previous work)",
        "",
        f"## Critical Context (most recent {CRITICAL_CONTEXT_MAX_ITEMS})",
    ])
    for e in errors[-CRITICAL_CONTEXT_MAX_ITEMS:]:
        parts.append(f"- {e}")
    parts.extend(["", "## Relevant Files"])
    for f in list(key_files)[:10]:
        parts.append(f"- {f}")
    if not key_files:
        parts.append("- (none)")

    return "\n".join(parts)


# ======================================================================
# FIFO Enforcement
# ======================================================================

def _enforce_fifo_limits(summary_text: str) -> str:
    def _fifo_section(text: str, header_pattern: str, max_items: int) -> str:
        match = re.search(header_pattern, text)
        if not match:
            return text
        header_end = match.end()
        next_section = re.search(r"\n#{2,3} ", text[header_end:])
        block_end = header_end + next_section.start() if next_section else len(text)
        block = text[header_end:block_end]
        items = [line for line in block.split("\n") if line.strip().startswith("-")]
        if len(items) <= max_items:
            return text
        kept = items[-max_items:]
        omitted = len(items) - max_items
        omitted_line = f"({omitted} earlier items omitted for brevity)"
        new_block = "\n".join(kept) + "\n" + omitted_line + "\n"
        return text[:header_end] + new_block + text[block_end:]

    summary_text = _fifo_section(
        summary_text, r"### Completed[^\n]*\n", COMPLETED_MAX_ITEMS
    )
    summary_text = _fifo_section(
        summary_text, r"## Key Decisions[^\n]*\n", KEY_DECISIONS_MAX_ITEMS
    )
    summary_text = _fifo_section(
        summary_text, r"## Critical Context[^\n]*\n", CRITICAL_CONTEXT_MAX_ITEMS
    )
    return summary_text


# ======================================================================
# File Operations Ratchet (inspired by openclaw)
# ======================================================================

def _extract_file_operations(messages: list[AnyMessage]) -> dict[str, list[str]]:
    read_files: set[str] = set()
    modified_files: set[str] = set()

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args_str = str(tc.get("args", ""))
                paths: set[str] = set()
                for word in args_str.replace("'", " ").replace('"', " ").replace(",", " ").split():
                    cleaned = word.strip("'\".,;:()[]{}")
                    if "/" in cleaned or "\\" in cleaned or cleaned.endswith(
                        (".py", ".md", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".cfg")
                    ):
                        if len(cleaned) > 2 and not cleaned.startswith(("http", "//")):
                            paths.add(cleaned)
                if name in ("read_file", "read", "cat", "view", "edit", "write_file", "write", "patch_file", "create_file"):
                    if name in ("write_file", "write", "patch_file", "edit", "create_file"):
                        modified_files.update(paths)
                        read_files.update(paths)
                    else:
                        read_files.update(paths)

    read_only = read_files - modified_files
    return {
        "read_files": sorted(read_only),
        "modified_files": sorted(modified_files),
    }


def _format_file_ops(file_ops: dict[str, list[str]], previous: dict | None = None) -> str:
    if previous:
        prev_read = set(previous.get("read_files", []))
        prev_mod = set(previous.get("modified_files", []))
        all_modified = prev_mod | set(file_ops.get("modified_files", []))
        all_read = (prev_read | set(file_ops.get("read_files", []))) - all_modified
    else:
        all_read = set(file_ops.get("read_files", []))
        all_modified = set(file_ops.get("modified_files", []))

    def _fmt(files: set[str], max_chars: int) -> str:
        lines = [f"- {f}" for f in sorted(files)]
        total = sum(len(l) for l in lines)
        while total > max_chars and lines:
            dropped = lines.pop(0)
            total -= len(dropped)
        if not lines and files:
            lines.append(f"- (file list truncated, {len(files)} files)")
        return "\n".join(lines)

    read_section = _fmt(all_read, FILE_OPS_LIST_MAX_CHARS)
    mod_section = _fmt(all_modified, FILE_OPS_LIST_MAX_CHARS)

    result = "<read-files>\n"
    result += read_section if read_section else "- (none)"
    result += "\n</read-files>\n"
    result += "<modified-files>\n"
    result += mod_section if mod_section else "- (none)"
    result += "\n</modified-files>"
    return result


def _parse_file_ops_from_summary(summary_text: str) -> dict | None:
    read_match = re.search(r"<read-files>\n?(.*?)\n?</read-files>", summary_text, re.DOTALL)
    mod_match = re.search(r"<modified-files>\n?(.*?)\n?</modified-files>", summary_text, re.DOTALL)
    if not read_match and not mod_match:
        return None
    read_files = [
        line.strip("- ").strip()
        for line in (read_match.group(1) if read_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    mod_files = [
        line.strip("- ").strip()
        for line in (mod_match.group(1) if mod_match else "").split("\n")
        if line.strip().startswith("-")
    ]
    return {"read_files": read_files, "modified_files": mod_files}


# ======================================================================
# Main Middleware Class
# ======================================================================

class Summarization(AgentMiddleware):
    """Context compaction middleware — written from scratch.

    Does NOT inherit from SummarizationMiddleware. All compression logic
    is self-contained: trigger checking, cutoff determination, summary
    generation, multi-strategy pipeline, degradation monitoring.

    Post-compression format: HumanMessage("What did we do so far?") +
    AIMessage(summary, lc_source="summarization") pair. No consecutive
    same-role messages, no _fix_consecutive_human_messages needed.
    """

    def __init__(
        self,
        model,
        trigger: list | None = None,
        keep: tuple = ("messages", 10),
        main_llm_context_window: int | None = None,
        need_update_system_prompt: bool = False,
        **kwargs,
    ):
        self._model = model
        self._trigger = trigger or [("tokens", 80_000)]
        self._keep = keep
        self._main_llm_context_window = main_llm_context_window
        self._need_update_system_prompt = need_update_system_prompt
        self._compress_last_turn: bool = False
        self._compaction_just_happened: bool = False

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_session_or_raise(state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_msg_tokens(msg: BaseMessage) -> int:
        return estimate_msg_tokens(msg)

    def _estimate_tokens(self, messages: Sequence[BaseMessage]) -> int:
        return estimate_messages_tokens(list(messages))

    def _get_reported_tokens(self, messages: list[AnyMessage]) -> int:
        last_ai = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai and last_ai.usage_metadata:
            return int(last_ai.usage_metadata.get("total_tokens", 0))
        return 0

    # ------------------------------------------------------------------
    # Budget calculation
    # ------------------------------------------------------------------

    def _calculate_preserve_budget(self) -> int:
        ctx = self._main_llm_context_window
        if ctx:
            budget = int(ctx * PRESERVE_RATIO)
            return min(MAX_PRESERVE_TOKENS, max(MIN_PRESERVE_TOKENS, budget))
        return MIN_PRESERVE_TOKENS

    # ------------------------------------------------------------------
    # Trigger checking
    # ------------------------------------------------------------------

    def _check_trigger(self, messages: list[AnyMessage]) -> bool:
        """Check if any trigger condition is met."""
        for trigger_type, threshold in self._trigger:
            if trigger_type == "messages" and len(messages) >= threshold:
                return True
            if trigger_type == "tokens":
                local_est = self._estimate_tokens(messages)
                reported = self._get_reported_tokens(messages)
                effective = max(local_est, reported) if reported > 0 else local_est
                if effective >= threshold:
                    return True
        return False

    def _preemptive_check(
        self, messages: list[AnyMessage], session_id: str
    ) -> str | None:
        """Pre-prompt token pressure estimation.

        Returns None / 'truncate_only' / 'compact'.
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None

        local_est = self._estimate_tokens(messages)
        reported = self._get_reported_tokens(messages)
        effective = max(local_est, reported) if reported > 0 else local_est
        pressure = effective / ctx_window

        if pressure >= COMPRESSION_TRIGGER_RATIO:
            return "compact"
        if pressure >= PREEMPTIVE_TRUNCATE_RATIO:
            return "truncate_only"
        return None

    # ------------------------------------------------------------------
    # 4-route overflow routing (Task 5: upgraded _preemptive_check decision)
    # ------------------------------------------------------------------

    def _usable_budget(self) -> int:
        """usable_budget = dynamic context window − COMPRESSION_RESERVE_TOKENS.

        The dynamic window is the constructor-injected ``main_llm_context_window``
        (same source as agent/core.py:156 ``main_llm_max_tokens``) — never a
        hardcoded value; langchain 1.3.9 ``ModelRequest`` has no model_profile
        field.
        """
        ctx = self._main_llm_context_window or 0
        return max(int(ctx) - COMPRESSION_RESERVE_TOKENS, 0)

    def _estimate_system_prompt_tokens(self, session_id: str) -> int:
        prompt = state_register_mem.get_state(session_id, "system_prompt", "")
        if isinstance(prompt, str) and prompt:
            return len(prompt) // 4
        return 0

    def _decide_overflow_route(
        self, messages: list[AnyMessage], session_id: str
    ) -> str | None:
        """4-way route decision (upgrades the former 2-band _preemptive_check).

        Returns one of ROUTE_FITS / ROUTE_TRUNCATE_TOOL_RESULTS_ONLY /
        ROUTE_COMPACT_THEN_TRUNCATE / ROUTE_COMPACT_ONLY, or None when no
        dynamic context window is configured. T1/T2 are estimate-driven; the
        reported-usage input belongs to T3 (Task 6).
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None

        usable = self._usable_budget()
        est = self._estimate_tokens(list(messages))
        system_est = self._estimate_system_prompt_tokens(session_id)
        pressure = compute_pressure(est, None, system_est)
        truncatable = find_truncatable_tool_results(list(messages))
        route = decide_route(pressure, int(ctx_window), usable, truncatable)
        logger.debug(
            "Overflow route decision: est={} system_est={} usable={} "
            "candidates={} route={} session={}",
            est, system_est, usable, len(truncatable), route, session_id,
        )
        return route

    def _run_budget_truncation(
        self, messages: list[BaseMessage], usable: int
    ) -> int:
        """Task 4 budget truncation over Task 3's candidate rule (in place).

        Candidates come from ``find_truncatable_tool_results`` (skips the last
        TRUNCATABLE_RECENT_SKIP messages, >= MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE)
        — Task 4's module intentionally does NOT apply that rule itself.
        """
        candidates = find_truncatable_tool_results(list(messages))
        return truncate_to_budget(
            list(messages), candidates, int(usable * TRUNCATE_BUDGET_RATIO)
        )

    def _log_route(
        self, trigger: str, route: str, old_tokens: int, new_tokens: int, usable: int
    ) -> None:
        ratio = round(new_tokens / usable, 4) if usable > 0 else 0.0
        logger.info(
            "Context compression: trigger={}, route={}, old_tokens={}, "
            "new_tokens={}, pressure_ratio={}",
            trigger, route, old_tokens, new_tokens, ratio,
        )

    def _record_compaction_bookkeeping(self, session_id: str) -> None:
        """After an ACTUAL compression: arm the cooldown, count the turn attempt."""
        state_register_mem.set_state(
            session_id, _COOLDOWN_ROUNDS_KEY, COMPACTION_COOLDOWN_ROUNDS
        )
        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0) + 1
        state_register_mem.set_state(session_id, _TURN_ATTEMPTS_KEY, attempts)

    def _execute_compact(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str,
    ) -> ModelRequest[ContextT]:
        """compact_only / compact_then_truncate execution (sync)."""
        messages: list[AnyMessage] = request.state.get("messages", [])
        old_tokens = self._estimate_tokens(list(messages))
        usable = self._usable_budget()
        try:
            request = self._apply_compression(request, session_id)
        except Exception as e:
            logger.error("Compression failed: {}", e)
            return request
        self._record_compaction_bookkeeping(session_id)
        if route == ROUTE_COMPACT_THEN_TRUNCATE:
            final_messages = list(request.messages)
            self._run_budget_truncation(
                cast("list[BaseMessage]", final_messages), usable
            )
            request = request.override(
                messages=cast("list[AnyMessage]", final_messages)
            )
        new_tokens = self._estimate_tokens(list(request.messages))
        self._log_route(trigger, route, old_tokens, new_tokens, usable)
        return request

    async def _aexecute_compact(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str,
    ) -> ModelRequest[ContextT]:
        """compact_only / compact_then_truncate execution (async)."""
        messages: list[AnyMessage] = request.state.get("messages", [])
        old_tokens = self._estimate_tokens(list(messages))
        usable = self._usable_budget()
        try:
            request = await self._aapply_compression(request, session_id)
        except Exception as e:
            logger.error("Compression failed: {}", e)
            return request
        self._record_compaction_bookkeeping(session_id)
        if route == ROUTE_COMPACT_THEN_TRUNCATE:
            final_messages = list(request.messages)
            self._run_budget_truncation(
                cast("list[BaseMessage]", final_messages), usable
            )
            request = request.override(
                messages=cast("list[AnyMessage]", final_messages)
            )
        new_tokens = self._estimate_tokens(list(request.messages))
        self._log_route(trigger, route, old_tokens, new_tokens, usable)
        return request

    def _dispatch_overflow_route(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str = "T2",
    ) -> ModelRequest[ContextT]:
        """Single reusable 4-route executor.

        T1 (before_agent) and T2 (wrap/awrap_model_call) both call this —
        Tasks 6/7 (T3 post-response check, provider-error retry) must reuse
        it instead of copying a second dispatch.
        """
        usable = self._usable_budget()
        messages: list[AnyMessage] = request.state.get("messages", [])

        if route == ROUTE_TRUNCATE_TOOL_RESULTS_ONLY:
            old_tokens = self._estimate_tokens(list(messages))
            self._run_budget_truncation(
                cast("list[BaseMessage]", list(messages)), usable
            )
            request = request.override(
                messages=cast("list[AnyMessage]", list(messages))
            )
            new_tokens = self._estimate_tokens(list(messages))
            self._log_route(trigger, route, old_tokens, new_tokens, usable)
            # Recheck: truncation freed less than estimated and pressure is
            # still at/above threshold_compact → compact backstop; otherwise
            # pass through WITHOUT compression.
            if usable > 0 and new_tokens >= usable * COMPRESSION_TRIGGER_RATIO:
                return self._execute_compact(
                    request, ROUTE_COMPACT_THEN_TRUNCATE, session_id, trigger
                )
            return request

        if route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            return self._execute_compact(request, route, session_id, trigger)

        return request

    async def _adispatch_overflow_route(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str = "T2",
    ) -> ModelRequest[ContextT]:
        """Async twin of :meth:`_dispatch_overflow_route` (parity by shape)."""
        usable = self._usable_budget()
        messages: list[AnyMessage] = request.state.get("messages", [])

        if route == ROUTE_TRUNCATE_TOOL_RESULTS_ONLY:
            old_tokens = self._estimate_tokens(list(messages))
            self._run_budget_truncation(
                cast("list[BaseMessage]", list(messages)), usable
            )
            request = request.override(
                messages=cast("list[AnyMessage]", list(messages))
            )
            new_tokens = self._estimate_tokens(list(messages))
            self._log_route(trigger, route, old_tokens, new_tokens, usable)
            if usable > 0 and new_tokens >= usable * COMPRESSION_TRIGGER_RATIO:
                return await self._aexecute_compact(
                    request, ROUTE_COMPACT_THEN_TRUNCATE, session_id, trigger
                )
            return request

        if route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            return await self._aexecute_compact(request, route, session_id, trigger)

        return request

    def _tick_cooldown(self, session_id: str) -> bool:
        """Cooldown bookkeeping: EVERY wrap_model_call decrements when >0.

        Returns True when the decrement happened (T2 proactive trigger is
        suppressed for that call). Cooldown blocks proactive compression
        only — forced recovery is exempt (caller checks the force flag).
        """
        rounds = state_register_mem.get_state(session_id, _COOLDOWN_ROUNDS_KEY, 0) or 0
        if rounds > 0:
            state_register_mem.set_state(session_id, _COOLDOWN_ROUNDS_KEY, rounds - 1)
            return True
        return False

    # ------------------------------------------------------------------
    # T3: post-response real-token re-check (Task 6)
    # ------------------------------------------------------------------

    def _post_response_check(
        self,
        request: ModelRequest[ContextT],
        response: ModelResponse[ResponseT]
        | AIMessage
        | ExtendedModelResponse[ResponseT],
        session_id: str,
        t2_compressed: bool = False,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Post-response real-token re-check (T3, Task 6).

        Runs AFTER the handler returns inside wrap/awrap_model_call: the
        provider-reported input tokens are the most accurate overflow signal,
        and per-call granularity covers multiple model calls within one turn
        (which the per-turn T1 preflight cannot). Dispatch reuses the Task 5
        executors (``_dispatch_overflow_route``) — never a second copy — and
        a failure NEVER loses the original response.
        """
        try:
            if t2_compressed:
                # T2 dispatched an actual compact in THIS wrap call: exactly
                # one compression per model call (anti-double-compress).
                return response
            reported = extract_reported_input_tokens(response)
            if reported is None:
                return response
            attempts = state_register_mem.get_state(
                session_id, _TURN_ATTEMPTS_KEY, 0
            ) or 0
            if attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN:
                return response
            cooldown = state_register_mem.get_state(
                session_id, _COOLDOWN_ROUNDS_KEY, 0
            ) or 0
            if cooldown > 0:
                # Anti-thrash gate respected: T3 reads the post-tick value.
                return response
            ctx_window = self._main_llm_context_window
            if not ctx_window or ctx_window <= 0:
                return response
            usable = self._usable_budget()
            if usable <= 0:
                return response
            messages = list(request.messages)
            est = self._estimate_tokens(messages)
            system_est = self._estimate_system_prompt_tokens(session_id)
            # reported wins (compute_pressure takes the max) — T3 is
            # real-token driven, NOT estimate-driven like T1/T2.
            pressure = compute_pressure(est, reported, system_est)
            if pressure < usable * COMPRESSION_TRIGGER_RATIO:
                return response
            truncatable = find_truncatable_tool_results(list(messages))
            route = decide_route(pressure, int(ctx_window), usable, truncatable)
            if route == ROUTE_FITS:
                return response
            request = self._dispatch_overflow_route(
                request, route, session_id, trigger="T3"
            )
            new_tokens = self._estimate_tokens(list(request.messages))
            logger.info(
                "Context compression: trigger=T3, reported_input_tokens={}, "
                "route={}, old_tokens={}, new_tokens={}, pressure_ratio={:.2f}",
                reported, route, est, new_tokens, pressure / usable,
            )
            return response
        except Exception as exc:
            # T3 must never break the response path: original response wins.
            logger.error(
                "Context compression: T3 check failed (response preserved): {}",
                exc,
            )
            return response

    async def _apost_response_check(
        self,
        request: ModelRequest[ContextT],
        response: ModelResponse[ResponseT]
        | AIMessage
        | ExtendedModelResponse[ResponseT],
        session_id: str,
        t2_compressed: bool = False,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Async twin of :meth:`_post_response_check` (parity by shape)."""
        try:
            if t2_compressed:
                return response
            reported = extract_reported_input_tokens(response)
            if reported is None:
                return response
            attempts = state_register_mem.get_state(
                session_id, _TURN_ATTEMPTS_KEY, 0
            ) or 0
            if attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN:
                return response
            cooldown = state_register_mem.get_state(
                session_id, _COOLDOWN_ROUNDS_KEY, 0
            ) or 0
            if cooldown > 0:
                return response
            ctx_window = self._main_llm_context_window
            if not ctx_window or ctx_window <= 0:
                return response
            usable = self._usable_budget()
            if usable <= 0:
                return response
            messages = list(request.messages)
            est = self._estimate_tokens(messages)
            system_est = self._estimate_system_prompt_tokens(session_id)
            pressure = compute_pressure(est, reported, system_est)
            if pressure < usable * COMPRESSION_TRIGGER_RATIO:
                return response
            truncatable = find_truncatable_tool_results(list(messages))
            route = decide_route(pressure, int(ctx_window), usable, truncatable)
            if route == ROUTE_FITS:
                return response
            request = await self._adispatch_overflow_route(
                request, route, session_id, trigger="T3"
            )
            new_tokens = self._estimate_tokens(list(request.messages))
            logger.info(
                "Context compression: trigger=T3, reported_input_tokens={}, "
                "route={}, old_tokens={}, new_tokens={}, pressure_ratio={:.2f}",
                reported, route, est, new_tokens, pressure / usable,
            )
            return response
        except Exception as exc:
            logger.error(
                "Context compression: T3 check failed (response preserved): {}",
                exc,
            )
            return response

    # ------------------------------------------------------------------
    # T4/T5: provider-error recovery loop (Task 7)
    # ------------------------------------------------------------------

    def _forced_recovery_request(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
    ) -> ModelRequest[ContextT]:
        """One forced-compression step for the T4/T5 recovery loop (sync).

        compact_only equivalent + budget truncation, bypassing ALL
        anti-thrash gates by construction (``_should_skip_compression``,
        cooldown rounds and the per-turn attempt cap are simply never
        consulted here — the forced semantics of ``_FORCE_RECOVERY_KEY``
        without setting the key). Reuses ``_apply_compression`` and
        ``_run_budget_truncation`` (Task 3 candidate rule; TTL=0 semantics
        = every candidate truncatable; budget = usable *
        TRUNCATE_BUDGET_RATIO) — no second dispatch copy. Pairing
        invariants stay intact (Task 4 truncation is pairing-safe; the
        summary output is a Human/AI pair).

        Does NOT arm the cooldown or count a turn attempt (error recovery
        bypasses those gates by design); it DOES go through
        ``_record_compression`` inside ``_apply_compression`` so the
        session-level compression stats stay truthful. The per-class retry
        counter is incremented AFTER a successful compression step.
        """
        trigger = _TRIGGER_BY_ERROR_CLASS[error_class]
        retry_key = _RETRY_KEY_BY_ERROR_CLASS[error_class]
        retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
        attempt = retries + 1
        old_tokens = self._estimate_tokens(list(request.messages))
        request = self._apply_compression(request, session_id)
        usable = self._usable_budget()
        final_messages = list(request.messages)
        self._run_budget_truncation(cast("list[BaseMessage]", final_messages), usable)
        request = request.override(messages=cast("list[AnyMessage]", final_messages))
        new_tokens = self._estimate_tokens(list(final_messages))
        state_register_mem.set_state(session_id, retry_key, attempt)
        logger.warning(
            "Context compression: trigger={}, attempt={}/{}, error_class={}, "
            "old_tokens={}, new_tokens={}",
            trigger, attempt, MAX_OVERFLOW_RETRIES, error_class,
            old_tokens, new_tokens,
        )
        return request

    async def _aforced_recovery_request(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
    ) -> ModelRequest[ContextT]:
        """Async twin of :meth:`_forced_recovery_request` (parity by shape)."""
        trigger = _TRIGGER_BY_ERROR_CLASS[error_class]
        retry_key = _RETRY_KEY_BY_ERROR_CLASS[error_class]
        retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
        attempt = retries + 1
        old_tokens = self._estimate_tokens(list(request.messages))
        request = await self._aapply_compression(request, session_id)
        usable = self._usable_budget()
        final_messages = list(request.messages)
        self._run_budget_truncation(cast("list[BaseMessage]", final_messages), usable)
        request = request.override(messages=cast("list[AnyMessage]", final_messages))
        new_tokens = self._estimate_tokens(list(final_messages))
        state_register_mem.set_state(session_id, retry_key, attempt)
        logger.warning(
            "Context compression: trigger={}, attempt={}/{}, error_class={}, "
            "old_tokens={}, new_tokens={}",
            trigger, attempt, MAX_OVERFLOW_RETRIES, error_class,
            old_tokens, new_tokens,
        )
        return request

    def _execute_with_recovery(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
        session_id: str,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Run ``handler`` inside the T4/T5 bounded recovery loop (sync).

        - Non-target errors (``classify_provider_error`` -> None): the
          ORIGINAL exception re-raises untouched — zero retries, zero
          state writes, never swallowed.
        - ``payload_too_large`` (T4) / ``context_overflow`` (T5): forced
          compression + ``request.override(messages=...)`` rebuild +
          handler retry, at most MAX_OVERFLOW_RETRIES times per error
          class (independent session-level counters). When the counter is
          exhausted the ORIGINAL exception re-raises (error-frame
          propagation via the existing messages.py -> turn_runner.py
          chain) — never an empty response.
        - A failure of the forced-compression step itself also propagates
          the ORIGINAL exception (never the compression error).
        - ``_monitor_degradation`` is NOT called here: wrap calls it once,
          AFTER this helper returns, on the final successful response only
          (Metis lock: failed retry calls must not pollute degradation
          statistics). T3 post-response checks run after the recovered
          final response (Task 6 wiring preserved).
        """
        while True:
            try:
                return handler(request)
            except BaseException as exc:
                error_class = classify_provider_error(exc)
                if error_class is None:
                    raise
                trigger = _TRIGGER_BY_ERROR_CLASS.get(error_class)
                retry_key = _RETRY_KEY_BY_ERROR_CLASS.get(error_class)
                if trigger is None or retry_key is None:
                    # Unknown future classifier value: treat as non-target.
                    raise
                retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
                if retries >= MAX_OVERFLOW_RETRIES:
                    logger.error(
                        "Context compression: trigger={} retries exhausted "
                        "({}, error_class={}) - propagating original error",
                        trigger, retries, error_class,
                    )
                    raise
                try:
                    request = self._forced_recovery_request(
                        request, session_id, error_class
                    )
                except Exception as compression_exc:
                    logger.error(
                        "Context compression: trigger={} forced compression "
                        "failed ({}) - propagating original error",
                        trigger, compression_exc,
                    )
                    raise exc from compression_exc

    async def _aexecute_with_recovery(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
        session_id: str,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Async twin of :meth:`_execute_with_recovery` (parity by shape)."""
        while True:
            try:
                return await handler(request)
            except BaseException as exc:
                error_class = classify_provider_error(exc)
                if error_class is None:
                    raise
                trigger = _TRIGGER_BY_ERROR_CLASS.get(error_class)
                retry_key = _RETRY_KEY_BY_ERROR_CLASS.get(error_class)
                if trigger is None or retry_key is None:
                    raise
                retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
                if retries >= MAX_OVERFLOW_RETRIES:
                    logger.error(
                        "Context compression: trigger={} retries exhausted "
                        "({}, error_class={}) - propagating original error",
                        trigger, retries, error_class,
                    )
                    raise
                try:
                    request = await self._aforced_recovery_request(
                        request, session_id, error_class
                    )
                except Exception as compression_exc:
                    logger.error(
                        "Context compression: trigger={} forced compression "
                        "failed ({}) - propagating original error",
                        trigger, compression_exc,
                    )
                    raise exc from compression_exc

    # ------------------------------------------------------------------
    # Preemptive truncation (no LLM call)
    # ------------------------------------------------------------------

    def _preemptive_truncate(
        self, messages: list[BaseMessage], session_id: str
    ) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        truncated_count = 0

        for m in messages:
            if isinstance(m, ToolMessage):
                tc_id = getattr(m, "tool_call_id", "")
                tool_name = self._find_tool_name(messages, m, tc_id)
                if tool_name in PROTECTED_TOOLS:
                    result.append(m)
                    continue
                content = str(getattr(m, "content", ""))
                if len(content) > _PREEMPTIVE_TRUNCATE_MAX_CHARS:
                    head = content[: int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_HEAD_RATIO)]
                    tail = content[-int(_PREEMPTIVE_TRUNCATE_MAX_CHARS * CONTENT_TAIL_RATIO):]
                    omitted = len(content) - len(head) - len(tail)
                    truncated = f"{head}...[omitted {omitted} chars]...{tail}"
                    result.append(m.model_copy(update={"content": truncated}))
                    truncated_count += 1
                else:
                    result.append(m)
            else:
                result.append(m)

        if truncated_count > 0:
            logger.debug(
                "Preemptive truncation: {} tool outputs, session={}",
                truncated_count, session_id,
            )
        return result

    @staticmethod
    def _find_tool_name(
        messages: list[BaseMessage], tool_msg: ToolMessage, tc_id: str
    ) -> str:
        if not tc_id:
            return ""
        idx = messages.index(tool_msg)
        for i in range(idx - 1, -1, -1):
            m = messages[i]
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                for tc in m.tool_calls:
                    if tc.get("id") == tc_id:
                        return tc.get("name", "")
        return ""

    # ------------------------------------------------------------------
    # Last-turn detection
    # ------------------------------------------------------------------

    @staticmethod
    def _slice_last_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
        if not messages:
            return []
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            None,
        )
        if last_user_idx is None:
            return []
        return messages[last_user_idx:]

    def _check_last_turn_ratio(self, messages: list[AnyMessage], session_id: str) -> bool:
        total_tokens = self._estimate_tokens(messages)
        if total_tokens <= 0:
            self._compress_last_turn = False
            return False
        last_turn = self._slice_last_turn(messages)
        last_turn_tokens = self._estimate_tokens(last_turn)
        ratio = last_turn_tokens / total_tokens
        compress = ratio >= LAST_TURN_RATIO_THRESHOLD
        self._compress_last_turn = compress
        if compress:
            last_user_msg = next(
                (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
            )
            question = (
                last_user_msg.content
                if last_user_msg and isinstance(last_user_msg.content, str)
                else ""
            )
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, question)
        else:
            state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")
        logger.debug(
            "Compaction: last-turn ratio={:.1f}%, compress_last_turn={}, session={}",
            ratio * 100, compress, session_id,
        )
        return compress

    # ------------------------------------------------------------------
    # Anti-thrashing: progressive escalation
    # ------------------------------------------------------------------

    def _should_skip_compression(self, session_id: str) -> bool:
        if state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False):
            state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
            state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
            state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            return False

        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        if attempts >= MAX_TOTAL_COMPRESSION_ATTEMPTS:
            logger.debug("Max compression attempts ({}) reached", MAX_TOTAL_COMPRESSION_ATTEMPTS)
            return True

        ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        if ineffective >= INEFFECTIVE_THRESHOLD:
            if not state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, True)
                logger.debug("LLM summary ineffective, switching to non-LLM strategies only")
            return False

        return False

    def _record_compression(
        self,
        session_id: str,
        before_messages: Sequence[BaseMessage],
        after_messages: Sequence[BaseMessage],
        strategy_used: str = "",
    ) -> None:
        attempts = state_register_mem.get_state(session_id, _COMPRESSION_COUNT_KEY, 0) + 1
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, attempts)
        state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, strategy_used or "unknown")

        before_tokens = self._estimate_tokens(before_messages)
        after_tokens = self._estimate_tokens(after_messages)
        msg_reduced = len(after_messages) < len(before_messages)
        token_reduction_pct = (
            (before_tokens - after_tokens) / before_tokens if before_tokens > 0 else 0.0
        )
        effective = msg_reduced or token_reduction_pct >= MIN_EFFECTIVENESS_PCT

        if not effective:
            ineffective = state_register_mem.get_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0) + 1
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, ineffective)
        else:
            state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
            if strategy_used in ("dedup", "prune", "truncate", "fallback", "aggressive"):
                state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)

        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, after_tokens)

    # ------------------------------------------------------------------
    # Cutoff determination (budget-based tail selection)
    # ------------------------------------------------------------------

    def _determine_cutoff(self, messages: list[AnyMessage]) -> int:
        budget = self._calculate_preserve_budget()
        turns = split_into_turns(messages)

        total = 0
        cutoff = 0
        for turn in reversed(turns):
            size = self._estimate_tokens(turn.messages)
            if total + size <= budget:
                total += size
                cutoff = turn.start_idx
            else:
                remaining = budget - total
                split_idx = split_turn(turn, remaining, lambda msgs: self._estimate_tokens(msgs))
                if split_idx is not None:
                    cutoff = split_idx
                break

        cutoff = self._adjust_for_orphan_pairs(messages, cutoff)

        if not self._compress_last_turn:
            last_user_idx = next(
                (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
                None,
            )
            if last_user_idx is not None and cutoff > last_user_idx:
                cutoff = last_user_idx

        return max(cutoff, 0)

    def _adjust_for_orphan_pairs(self, messages: list[AnyMessage], cutoff: int) -> int:
        adjusted = cutoff
        while adjusted > 0:
            orphan_ids: set[str] = set()
            for m in messages[adjusted:]:
                if isinstance(m, ToolMessage) and m.tool_call_id:
                    orphan_ids.add(m.tool_call_id)
            for m in messages[adjusted:]:
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        orphan_ids.discard(tc.get("id"))
            if not orphan_ids:
                break

            earliest_orphan_ai = len(messages)
            for i in range(adjusted):
                m = messages[i]
                if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                    if any(tc.get("id") in orphan_ids for tc in m.tool_calls):
                        earliest_orphan_ai = min(earliest_orphan_ai, i)
            if earliest_orphan_ai < adjusted:
                adjusted = earliest_orphan_ai
            else:
                prev_user_idx = next(
                    (i for i in range(adjusted - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
                    None,
                )
                if prev_user_idx is None:
                    break
                adjusted = prev_user_idx
        return adjusted

    # ------------------------------------------------------------------
    # Previous summary chaining
    # ------------------------------------------------------------------

    def _extract_previous_summary(self, messages: list[AnyMessage]) -> str | None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if _SUMMARY_CLOSE_TAG in content:
                    start = content.find(_SUMMARY_OPEN_TAG)
                    end = content.find(_SUMMARY_CLOSE_TAG)
                    if start >= 0 and end > start:
                        return content[start + len(_SUMMARY_OPEN_TAG):end].strip()
                return content
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage) and getattr(msg, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                return msg.content if isinstance(msg.content, str) else str(msg.content)
        return None

    # ------------------------------------------------------------------
    # Summary prompt construction
    # ------------------------------------------------------------------

    def _build_summary_prompt(self, messages_text: str, previous_summary: str | None) -> str:
        conversation = f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
        if previous_summary:
            return "\n\n".join([
                conversation,
                f"Here is the summary of the conversation before the <conversation> above:\n\n"
                f"<prior-summary>\n{previous_summary}\n</prior-summary>",
                _SUMMARY_PROMPT_UPDATE,
            ])
        return "\n\n".join([conversation, _SUMMARY_PROMPT_FIRST])

    # ------------------------------------------------------------------
    # LLM summary creation (sync / async)
    # ------------------------------------------------------------------

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_summary = self._extract_previous_summary(messages_to_summarize)
        serialized = _serialize_for_summary(messages_to_summarize)
        if not serialized.strip():
            return "No previous conversation history."

        prompt = self._build_summary_prompt(serialized, previous_summary)

        try:
            response = self._model.invoke(
                prompt,
                config={"metadata": {"lc_source": _SUMMARY_LC_SOURCE}},
            )
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(messages_to_summarize)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(messages_to_summarize)

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        if not messages_to_summarize:
            return "No previous conversation history."

        previous_summary = self._extract_previous_summary(messages_to_summarize)
        serialized = _serialize_for_summary(messages_to_summarize)
        if not serialized.strip():
            return "No previous conversation history."

        prompt = self._build_summary_prompt(serialized, previous_summary)

        try:
            response = await self._model.ainvoke(
                prompt,
                config={"metadata": {"lc_source": _SUMMARY_LC_SOURCE}},
            )
            summary = response.text.strip()
            if not summary or len(summary) < 50:
                logger.warning("Summary too short, using fallback")
                return _build_static_fallback_summary(messages_to_summarize)
            return summary
        except Exception as e:
            logger.error("LLM summary failed: {}, using fallback", e)
            return _build_static_fallback_summary(messages_to_summarize)

    # ------------------------------------------------------------------
    # Build new messages (HumanMessage + AIMessage pair)
    # ------------------------------------------------------------------

    def _build_new_messages(self, summary: str) -> list[BaseMessage]:
        summary = _enforce_fifo_limits(summary)

        if len(summary) > SUMMARY_TOTAL_MAX_CHARS:
            head = summary[: int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_HEAD_RATIO)]
            tail = summary[-int(SUMMARY_TOTAL_MAX_CHARS * CONTENT_TAIL_RATIO):]
            omitted = len(summary) - len(head) - len(tail)
            summary = f"{head}...[summary truncated, omitted {omitted} chars]...{tail}"

        full_content = (
            f"{_SUMMARY_PREFIX}\n\n"
            f"{_SUMMARY_OPEN_TAG}\n"
            f"{summary}\n"
            f"{_SUMMARY_CLOSE_TAG}"
            f"{_SUMMARY_SUFFIX}"
        )

        return [
            HumanMessage(content="What did we do so far?"),
            AIMessage(
                content=full_content,
                additional_kwargs={"lc_source": _SUMMARY_LC_SOURCE},
            ),
        ]

    # ------------------------------------------------------------------
    # Multi-strategy pipeline (non-LLM strategies)
    # ------------------------------------------------------------------

    def _run_non_llm_strategies(
        self, messages: list[BaseMessage], session_id: str
    ) -> tuple[list[BaseMessage], int]:
        current = list(messages)
        total_reduced = 0

        current, reduced = dedup_tool_outputs(current, set(PROTECTED_TOOLS))
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Dedup reduced ~{} tokens, session={}", reduced, session_id)

        current, reduced = prune_tool_outputs(
            current,
            protect_tokens=PRUNE_PROTECT_TOKENS,
            min_reduction_tokens=PRUNE_MIN_REDUCTION_TOKENS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Prune reduced ~{} tokens, session={}", reduced, session_id)

        current_tokens = self._estimate_tokens(current)
        target = int(current_tokens * TARGET_TRUNCATE_RATIO)
        current, reduced = target_truncate_tool_outputs(
            current,
            target_reduction_tokens=target,
            min_output_chars=MIN_OUTPUT_CHARS_TO_TRUNCATE,
            max_output_chars=MAX_TOOL_OUTPUT_CHARS,
            protected_tools=set(PROTECTED_TOOLS),
        )
        total_reduced += reduced
        if reduced > 0:
            logger.debug("Target truncation reduced ~{} tokens, session={}", reduced, session_id)

        return current, total_reduced

    def _aggressive_truncate(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                content = str(getattr(msg, "content", ""))
                if len(content) > AGGRESSIVE_TRUNCATE_CHARS:
                    truncated = content[:AGGRESSIVE_TRUNCATE_CHARS] + (
                        f"...[aggressively truncated, {len(content) - AGGRESSIVE_TRUNCATE_CHARS} chars omitted]"
                    )
                    msg = msg.model_copy(update={"content": truncated})
            result.append(msg)
        return result

    # ------------------------------------------------------------------
    # Recovery context capture & injection
    # ------------------------------------------------------------------

    def _capture_recovery_context(self, messages: list[BaseMessage], session_id: str) -> dict:
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
        )
        user_intent = ""
        if last_human and isinstance(last_human.content, str):
            user_intent = last_human.content[:LATEST_USER_REQUEST_MAX_CHARS]

        file_ops = _extract_file_operations(messages)
        previous_file_ops = state_register_mem.get_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)
        state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, file_ops)

        return {
            "user_intent": user_intent,
            "file_ops": file_ops,
            "previous_file_ops": previous_file_ops,
        }

    def _inject_recovery_context(
        self, messages: list[BaseMessage], ctx: dict, session_id: str
    ) -> list[BaseMessage]:
        file_ops_section = _format_file_ops(ctx.get("file_ops", {}), ctx.get("previous_file_ops"))

        for i, m in enumerate(messages):
            if isinstance(m, AIMessage) and getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                existing = m.content if isinstance(m.content, str) else str(m.content)
                pattern = r"## Relevant Files\n.*?(?=\n---|\n</summary>|\Z)"
                if re.search(pattern, existing, re.DOTALL):
                    replacement = f"## Relevant Files\n{file_ops_section}"
                    new_content = re.sub(pattern, replacement, existing, flags=re.DOTALL)
                else:
                    new_content = existing.replace(
                        _SUMMARY_CLOSE_TAG,
                        f"\n## Relevant Files\n{file_ops_section}\n{_SUMMARY_CLOSE_TAG}",
                    )
                messages[i] = m.model_copy(update={"content": new_content})
                break

        return messages

    # ------------------------------------------------------------------
    # Truncation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_content(content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content
        head = content[: int(max_chars * CONTENT_HEAD_RATIO)]
        tail = content[-int(max_chars * CONTENT_TAIL_RATIO):]
        omitted = len(content) - len(head) - len(tail)
        return f"{head}...[omitted {omitted} chars]...{tail}"

    def _truncate_summary_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for m in messages:
            if getattr(m, "additional_kwargs", {}).get("lc_source") == _SUMMARY_LC_SOURCE:
                content = getattr(m, "content", "")
                if isinstance(content, str) and len(content) > SUMMARY_TOTAL_MAX_CHARS:
                    truncated = self._truncate_content(content, SUMMARY_TOTAL_MAX_CHARS)
                    m = m.model_copy(update={"content": truncated})
            result.append(m)
        return result

    # ------------------------------------------------------------------
    # Degradation monitoring
    # ------------------------------------------------------------------

    @staticmethod
    def _is_empty_response(response) -> bool:
        if response is None:
            return True
        if isinstance(response, AIMessage):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
            if isinstance(content, list):
                return not any(p.get("text", "").strip() for p in content if isinstance(p, dict))
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, str):
                return not content.strip()
        return False

    def _monitor_degradation(self, response, session_id: str):
        if not self._compaction_just_happened:
            return
        self._compaction_just_happened = False

        if self._is_empty_response(response):
            count = state_register_mem.get_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0) + 1
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, count)

            if count >= DEGRADATION_NO_TEXT_THRESHOLD:
                attempts = state_register_mem.get_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
                if attempts < MAX_RECOVERY_ATTEMPTS:
                    state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, attempts + 1)
                    state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, True)
                    state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
                    state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
                    logger.warning(
                        "Degradation detected ({} empty responses), forcing recovery",
                        count,
                    )
        else:
            state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)

    # ------------------------------------------------------------------
    # Core compression application (sync)
    # ------------------------------------------------------------------

    def _apply_compression(
        self, request: ModelRequest[ContextT], session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]

                if skip_llm:
                    summary_text = _build_static_fallback_summary(messages_to_summarize)
                    strategy_used = "fallback"
                else:
                    summary_text = self._create_summary(messages_to_summarize)
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(
                final_messages, recovery_ctx, session_id
            )

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            from agent.tools import memory_store
            memory_store.load_from_disk()
            system_prompt = build_system_prompt(session_id=session_id)
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    # ------------------------------------------------------------------
    # Core compression application (async)
    # ------------------------------------------------------------------

    async def _aapply_compression(
        self, request: ModelRequest[ContextT], session_id: str,
    ) -> ModelRequest[ContextT]:
        original_messages: list[AnyMessage] = request.state.get("messages", [])
        recovery_ctx = self._capture_recovery_context(original_messages, session_id)

        current_messages, non_llm_reduced = self._run_non_llm_strategies(
            list(original_messages), session_id
        )
        strategy_used = "non_llm" if non_llm_reduced > 0 else None

        current_tokens = self._estimate_tokens(current_messages)
        skip_llm = state_register_mem.get_state(session_id, _SKIP_LLM_KEY, False)
        budget = self._calculate_preserve_budget()

        if current_tokens > budget * 2 or skip_llm or non_llm_reduced == 0:
            cutoff = self._determine_cutoff(current_messages)
            if cutoff > 0:
                messages_to_summarize = current_messages[:cutoff]
                preserved = current_messages[cutoff:]

                if skip_llm:
                    summary_text = _build_static_fallback_summary(messages_to_summarize)
                    strategy_used = "fallback"
                else:
                    summary_text = await self._acreate_summary(messages_to_summarize)
                    strategy_used = "llm_summary"

                new_messages = self._build_new_messages(summary_text)
                final_messages = [*new_messages, *preserved]
            else:
                final_messages = current_messages
                strategy_used = strategy_used or "noop"
        else:
            final_messages = current_messages
            strategy_used = strategy_used or "non_llm_sufficient"

        if self._estimate_tokens(final_messages) > budget * 2:
            final_messages = self._aggressive_truncate(final_messages)
            strategy_used = "aggressive"

        final_messages = self._truncate_summary_messages(final_messages)

        if recovery_ctx:
            final_messages = self._inject_recovery_context(
                final_messages, recovery_ctx, session_id
            )

        self._record_compression(session_id, original_messages, final_messages, strategy_used)
        self._compaction_just_happened = True
        self._compress_last_turn = False
        state_register_mem.set_state(session_id, _LAST_USER_QUESTION_KEY, "")

        system_prompt: str | None = None
        if self._need_update_system_prompt:
            from agent.tools import memory_store
            memory_store.load_from_disk()
            system_prompt = build_system_prompt(session_id=session_id)
            state_register_mem.set_state(session_id, "system_prompt", system_prompt)
            state_register_db.set_state(session_id, "system_prompt", system_prompt)

        override_kwargs: dict[str, Any] = {
            "messages": cast("list[AnyMessage]", final_messages),
        }
        if system_prompt:
            override_kwargs["system_message"] = SystemMessage(content=system_prompt)
        return request.override(**override_kwargs)

    # ------------------------------------------------------------------
    # before_agent: reset state
    # ------------------------------------------------------------------

    def _before_agent_impl(self, state: AgentState) -> dict[str, Any] | None:
        session_id = state.get("session_id", "")
        if session_id.strip():
            self._reset_turn_state(session_id)
            # T1 PREFLIGHT: budget-truncate / compact the OVERFLOWED history
            # before the turn starts (4-route decision, truncate track is
            # always allowed; compact routes are cooldown-gated).
            return self._t1_preflight(state, session_id)
        return None

    async def _abefore_agent_impl(self, state: AgentState) -> dict[str, Any] | None:
        session_id = state.get("session_id", "")
        if session_id.strip():
            self._reset_turn_state(session_id)
            return await self._at1_preflight(state, session_id)
        return None

    def _reset_turn_state(self, session_id: str) -> None:
        state_register_mem.set_state(session_id, _COMPRESSION_COUNT_KEY, 0)
        state_register_mem.set_state(session_id, _COMPRESSION_INEFFECTIVE_KEY, 0)
        state_register_mem.set_state(session_id, _COMPRESSION_LAST_TOKENS_KEY, None)
        state_register_mem.set_state(session_id, _SKIP_LLM_KEY, False)
        state_register_mem.set_state(session_id, _LAST_STRATEGY_KEY, "")
        state_register_mem.set_state(session_id, _DEGRADATION_NO_TEXT_KEY, 0)
        state_register_mem.set_state(session_id, _RECOVERY_ATTEMPTS_KEY, 0)
        state_register_mem.set_state(session_id, _FORCE_RECOVERY_KEY, False)
        state_register_mem.set_state(session_id, _PREVIOUS_FILE_OPS_KEY, None)
        # NEW (Task 5): per-turn proactive-compression attempt counter.
        state_register_mem.set_state(session_id, _TURN_ATTEMPTS_KEY, 0)

    def _t1_state_update(
        self, request: ModelRequest[ContextT]
    ) -> dict[str, Any]:
        """Translate a T1-dispatched request into a before_agent state update.

        ALWAYS clears the state messages first (RemoveMessage with the
        REMOVE_ALL_MESSAGES sentinel) and rebuilds from the dispatched
        request: the add_messages reducer never removes by itself, so a
        plain-list update leaks every message the compression summarized
        away — and a compact that swaps a single huge head message for the
        two-message summary pair even GROWS the list (cutoff=1), which no
        length-based guard can catch. Rebuilding is exact for every track:
        in-place truncation keeps the same ids and content, compact replaces
        the head with the summary pair (same pattern as
        ToolCallNormalize.before_model).
        """
        new_messages = list(request.messages)
        return {
            "messages": cast(
                "list[AnyMessage]",
                [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages],
            )
        }

    def _t1_preflight(
        self, state: AgentState, session_id: str
    ) -> dict[str, Any] | None:
        messages: list[AnyMessage] = list(state.get("messages", []) or [])
        if not messages:
            return None
        route = self._decide_overflow_route(messages, session_id)
        if route is None or route == ROUTE_FITS:
            return None
        # Cooldown blocks the PROACTIVE compact routes at T1; the cheap
        # truncate track still runs (it is the recovery mechanism itself).
        cooldown = state_register_mem.get_state(
            session_id, _COOLDOWN_ROUNDS_KEY, 0
        ) or 0
        if cooldown > 0 and route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            logger.debug(
                "T1 compact route suppressed by cooldown ({} rounds left), "
                "session={}",
                cooldown, session_id,
            )
            return None
        request = ModelRequest(
            model=self._model,
            messages=cast("list[AnyMessage]", messages),
            state=state,
        )
        request = self._dispatch_overflow_route(
            request, route, session_id, trigger="T1"
        )
        return self._t1_state_update(request)

    async def _at1_preflight(
        self, state: AgentState, session_id: str
    ) -> dict[str, Any] | None:
        messages: list[AnyMessage] = list(state.get("messages", []) or [])
        if not messages:
            return None
        route = self._decide_overflow_route(messages, session_id)
        if route is None or route == ROUTE_FITS:
            return None
        cooldown = state_register_mem.get_state(
            session_id, _COOLDOWN_ROUNDS_KEY, 0
        ) or 0
        if cooldown > 0 and route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            logger.debug(
                "T1 compact route suppressed by cooldown ({} rounds left), "
                "session={}",
                cooldown, session_id,
            )
            return None
        request = ModelRequest(
            model=self._model,
            messages=cast("list[AnyMessage]", messages),
            state=state,
        )
        request = await self._adispatch_overflow_route(
            request, route, session_id, trigger="T1"
        )
        return self._t1_state_update(request)

    def before_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        logger.debug("Compaction before_agent hook fired")
        return self._before_agent_impl(state)

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("Compaction abefore_agent hook fired")
        return await self._abefore_agent_impl(state)

    # ------------------------------------------------------------------
    # wrap_model_call (sync)
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction wrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        # T2 anti-thrash bookkeeping (EVERY call): tick the cooldown down
        # before anything else. The force flag is read BEFORE the skip check
        # because _should_skip_compression consumes it.
        forced = state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False)
        cooldown_active = self._tick_cooldown(session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = self._execute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            return response

        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        if not forced and (
            cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN
        ):
            # T2 anti-thrash gate: cooldown / per-turn attempt cap suppress
            # the PROACTIVE trigger only (forced recovery is exempt above).
            self._compress_last_turn = False
            if self._compaction_just_happened:
                # T1 compacted earlier this turn: a second compression is
                # exactly the thrash the cooldown prevents, but the rebuilt
                # system prompt must still reach the model (chains without
                # ContextEngineHook rely on this middleware delivering it),
                # and the response still needs degradation monitoring — the
                # flag is left for _monitor_degradation to consume.
                if self._need_update_system_prompt:
                    rebuilt = state_register_mem.get_state(
                        session_id, "system_prompt", ""
                    )
                    if rebuilt:
                        request = request.override(
                            system_message=SystemMessage(content=rebuilt)
                        )
            response = self._execute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            # T3 post-response re-check (Task 6). Gate path: T2 did NOT
            # dispatch here, so t2_compressed=False — the check re-reads the
            # anti-thrash state itself (post-tick cooldown still > 0 blocks).
            return self._post_response_check(request, response, session_id)

        # T3 anti-double-compress snapshot (Task 6): turn attempts BEFORE the
        # T2 dispatch; only actual compact executions increment the key, so a
        # bump means T2 compressed in THIS wrap call.
        t2_attempts_before = state_register_mem.get_state(
            session_id, _TURN_ATTEMPTS_KEY, 0
        )
        # 4-route decision (upgraded _preemptive_check) → single dispatch
        route = self._decide_overflow_route(messages, session_id)
        if route is not None and route != ROUTE_FITS:
            request = self._dispatch_overflow_route(
                request, route, session_id, trigger="T2"
            )
        elif self._check_trigger(request.state.get("messages", [])):
            # legacy trigger-clause fallback (e.g. ("messages", N) triggers)
            request = self._dispatch_overflow_route(
                request, ROUTE_COMPACT_ONLY, session_id, trigger="T2"
            )

        response = self._execute_with_recovery(request, handler, session_id)
        self._monitor_degradation(response, session_id)
        t2_compressed = (
            state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
            > t2_attempts_before
        )
        # T3: post-response real-token re-check (Task 6); T2-compressed calls
        # skip via the local flag — one compression per model call.
        return self._post_response_check(
            request, response, session_id, t2_compressed=t2_compressed
        )

    # ------------------------------------------------------------------
    # awrap_model_call (async)
    # ------------------------------------------------------------------

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction awrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        # T2 anti-thrash bookkeeping (EVERY call): tick the cooldown down
        # before anything else. The force flag is read BEFORE the skip check
        # because _should_skip_compression consumes it.
        forced = state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False)
        cooldown_active = self._tick_cooldown(session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = await self._aexecute_with_recovery(
                request, handler, session_id
            )
            self._monitor_degradation(response, session_id)
            return response

        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        if not forced and (
            cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN
        ):
            # T2 anti-thrash gate: cooldown / per-turn attempt cap suppress
            # the PROACTIVE trigger only (forced recovery is exempt above).
            self._compress_last_turn = False
            if self._compaction_just_happened:
                # T1 compacted earlier this turn: a second compression is
                # exactly the thrash the cooldown prevents, but the rebuilt
                # system prompt must still reach the model (chains without
                # ContextEngineHook rely on this middleware delivering it),
                # and the response still needs degradation monitoring — the
                # flag is left for _monitor_degradation to consume.
                if self._need_update_system_prompt:
                    rebuilt = state_register_mem.get_state(
                        session_id, "system_prompt", ""
                    )
                    if rebuilt:
                        request = request.override(
                            system_message=SystemMessage(content=rebuilt)
                        )
            response = await self._aexecute_with_recovery(
                request, handler, session_id
            )
            self._monitor_degradation(response, session_id)
            # T3 post-response re-check (Task 6); see the sync twin.
            return await self._apost_response_check(request, response, session_id)

        # T3 anti-double-compress snapshot (Task 6): turn attempts BEFORE the
        # T2 dispatch; only actual compact executions increment the key, so a
        # bump means T2 compressed in THIS wrap call.
        t2_attempts_before = state_register_mem.get_state(
            session_id, _TURN_ATTEMPTS_KEY, 0
        )
        # 4-route decision (upgraded _preemptive_check) → single dispatch
        route = self._decide_overflow_route(messages, session_id)
        if route is not None and route != ROUTE_FITS:
            request = await self._adispatch_overflow_route(
                request, route, session_id, trigger="T2"
            )
        elif self._check_trigger(request.state.get("messages", [])):
            # legacy trigger-clause fallback (e.g. ("messages", N) triggers)
            request = await self._adispatch_overflow_route(
                request, ROUTE_COMPACT_ONLY, session_id, trigger="T2"
            )

        response = await self._aexecute_with_recovery(
            request, handler, session_id
        )
        self._monitor_degradation(response, session_id)
        t2_compressed = (
            state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
            > t2_attempts_before
        )
        # T3: post-response real-token re-check (Task 6); T2-compressed calls
        # skip via the local flag — one compression per model call.
        return await self._apost_response_check(
            request, response, session_id, t2_compressed=t2_compressed
        )
