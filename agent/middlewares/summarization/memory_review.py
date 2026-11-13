"""Background memory review + consolidation (job 1): a forked agent that updates the long-term memory store."""

import asyncio
from collections.abc import Sequence
from typing import Any
from langchain.agents import create_agent
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from loguru import logger
from config.features import MEMORY_TOOL, BACKGROUND_REVIEW_CONFIG
from pub.func import sanitize_tool_use_result_pairing
from runtime import state_register_mem
from runtime.lane import LaneType, lane_slot
from ._common import _msg_to_text, _render_prompt_facts
from .background_scaffold import (
    StateSchema,
    _BACKGROUND_REVIEW_MEMORY_LOCK_KEY,
    _BACKGROUND_REVIEW_TASKS,
    _BackgroundReviewLimitTool,
    _background_reviews_locked,
    _build_session_model,
    _session_tool_face,
)


_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering? (target: 'user')\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate? (target: 'user')\n"
    "3. Did the work surface a SYSTEM-LEVEL or CROSS-PROJECT pitfall — something "
    "that ANY project on this machine could hit (not specific to the current "
    "project)? Examples: toolchain behavior ('uv run pytest needs --no-sync'), "
    "environment constraints ('python_repl hits 30s timeout'), generic command "
    "gotchas. (target: 'facts')\n\n"
    "IMPORTANT: the 'facts' target is for SYSTEM-LEVEL facts only — things true "
    "in ANY project. Project-specific facts (e.g. 'this project uses pnpm') do "
    "NOT belong in 'facts' — they go to 'memory' or the project's AGENTS.md.\n\n"
    "Routing: user preferences and traits go to 'user'; project-specific notes "
    "and conventions go to 'memory'; SYSTEM-LEVEL/CROSS-PROJECT pitfalls go to "
    "'facts' (only if true in ANY project, not just the current one).\n\n"
    "Current 'facts' target content (limit {facts_limit} chars):\n"
    "<facts>\n{facts_block}\n</facts>\n\n"
    "Maintain the 'facts' target like an editor, not an appender: merge entries "
    "describing the same pitfall, replace outdated ones, and prefer saving nothing "
    "over bloating the store (prefer nothing over noise). If a fact is "
    "project-specific, remove it from 'facts' and add it to 'memory' instead.\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)


_MEMORY_CONSOLIDATION_PROMPT = (
    "You are the memory consolidator. The active entries of all three targets are "
    "listed below. Maintain them like an editor — the goal is a smaller, sharper "
    "store, not more text.\n\n"
    "Work in this order:\n"
    "1. MERGE duplicates: entries describing the same fact become ONE entry. Use "
    'memory(action="replace", target=..., old_text=<a unique substring from the '
    "entry being replaced>, content=<the merged entry>), then remove the other "
    "duplicates.\n"
    "2. RESOLVE contradictions: when two entries disagree, keep the newer / more "
    "specific one and replace or remove the outdated one.\n"
    "3. ROUTE: project-specific facts belong in 'memory'; system-level pitfalls go "
    "to 'facts'; user preferences/traits go to 'user'. Move a misfiled entry with "
    "replace/remove/add.\n"
    "4. TIGHTEN: rewrite verbose entries without losing their hard constraints; "
    "keep identifiers, paths and exact commands intact.\n"
    "5. REMOVE noise: obsolete, redundant or unactionable entries go away with "
    'memory(action="remove").\n\n'
    "Rules: never invent a fact; never delete an entry whose usefulness you cannot "
    "judge; use the EXACT old_text substrings shown below so a replace/remove "
    "matches one entry; do not touch archived entries. If the store is already "
    "clean, say 'Nothing to consolidate.' and stop.\n\n"
    "Current entries:\n{entries_block}"
)


async def _run_memory_review(
    session_id: str,
    messages_to_summarize: list[BaseMessage],
) -> None:
    """Run the compression-scoped memory review under the BACKGROUND_REVIEW lane (fail-open).

    Compaction carries the MEMORY review only, and it is handed the messages
    about to be DISCARDED — those are the ones whose facts are about to be lost,
    and they are a fraction of the whole conversation. Skill review lives on
    the plan-completion path (:func:`maybe_trigger_skill_review`) — it needs
    no message history and must not wait for a window to fill.
    """
    try:
        # Call-time import keeps the patch seam on
        # ``agent.middlewares.system_prompt.core._get_and_reload_system_prompt``
        # effective (same contract as before the package move).
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt

        system_prompt = _get_and_reload_system_prompt(session_id)
        await _execute_memory_review(session_id, system_prompt, list(messages_to_summarize))
    except Exception:
        logger.exception("memory review dispatch failed (fail-open) for {}", session_id)


def schedule_memory_review(session_id: str, messages_to_summarize: Sequence[BaseMessage]) -> bool:
    """Dispatch the compression-time memory review.

    Called by the Summarization middleware once per compression that actually
    discards messages, with the messages being discarded. Returns True when a
    task was created.

    Skill review does not ride this path — it forks on plan completion (see
    :func:`maybe_trigger_skill_review`). While a background review is in flight, dispatch
    is skipped entirely — nothing is queued. With no running event loop (the
    sync compression path outside an async caller) scheduling is skipped
    entirely.

    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "memory review: no running event loop, skipping for session {}",
            session_id,
        )
        return False

    if _background_reviews_locked(session_id):
        return False

    coro = _run_memory_review(session_id, list(messages_to_summarize))
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        return False
    _BACKGROUND_REVIEW_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REVIEW_TASKS.discard)
    logger.debug("memory review scheduled for session {}", session_id)
    return True


def _build_memory_review_agent(
    model: Any,
    system_prompt: str,
    tools: Sequence[BaseTool],
    *,
    allowed_metadata_key: str | None = None,
    max_turns: int = 90,
):
    """Build one memory-review fork from CAPTURED model/prompt/tool inputs.

    Split from :func:`_create_memory_review_agent` so the session-end salvage
    can build its agents from values snapshotted before the session is purged.
    The fork has no checkpointer; its result messages never reach the main graph.
    ``allowed_metadata_key`` is the session-end ``memory``-only gate; the
    compression review leaves it ``None`` and keeps the broader background-review allowlist.
    ``max_turns`` bounds the iteration budget (15 for consolidation, 90 for a
    review).
    """
    from agent.middlewares import IterationBudget, ToolCallNormalize, ToolGuardrails

    return create_agent(
        model=model,
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            _BackgroundReviewLimitTool(allowed_metadata_key=allowed_metadata_key),
            ToolCallNormalize(),
            ToolGuardrails(),
            IterationBudget(max(1, int(max_turns))),
        ],
        tools=list(tools),
    )


async def _create_memory_review_agent(session_id: str, system_prompt: str, *, max_turns: int = 90):
    """The memory review's agent: the MAIN model the turn ran on.

    Cache-warm on purpose. The compaction cut is a PREFIX cut
    (``messages_to_summarize = current_messages[:cutoff]``), so the discarded
    segment sits immediately after the system prompt in the request the main
    model just served. A review request built as
    ``[tools][system prompt][discarded messages][review instruction]`` therefore
    reproduces that prefix exactly, and the provider serves it from its prefix
    cache: the review reads the REAL discarded text (no digest folding) while
    paying cache-read prices for it, and only the instruction is fresh input.

    Everything that is part of the prefix is reproduced: the model
    configuration the turn used (``_build_session_model`` — the session's
    override and thinking variant, or the env default), the session's assembled
    system prompt, the session's own tool face and order. The background-review gate still
    applies — the review may only EXECUTE the tools carrying ``background_review: True``.
    """
    return _build_memory_review_agent(
        _build_session_model(session_id),
        system_prompt,
        _session_tool_face(session_id),
        max_turns=max_turns,
    )


def _memory_review_messages(session_id: str, messages: list[BaseMessage]) -> list[BaseMessage]:
    """The messages the review sends, per ``BACKGROUND_REVIEW_CONFIG["memory_review_mode"]``.

    ``cache_warm`` (default): the discarded slice VERBATIM — it is the main
    model's own prefix, so the provider serves it from cache and the review sees
    the real text.

    ``digest``: the folded view (``digest_history``), for a deployment whose
    provider does not cache prefixes well or whose review model has a small
    window — the fold bounds the input regardless of provider behaviour.
    """
    if str(BACKGROUND_REVIEW_CONFIG.get("memory_review_mode", "cache_warm")) == "digest":
        from agent.middlewares.summarization.digest import digest_history

        return digest_history(list(messages))
    return list(messages)


def _cache_hit_tokens(message: Any) -> int | None:
    """Prompt tokens the provider served from its prefix cache, when reported.

    langchain only normalizes the OpenAI shape; the DeepSeek / Anthropic fields
    survive in ``response_metadata`` — the same provider-aware read the message
    store uses (``context_engine.store.core._cache_read_tokens_from_usage``), so
    the log line reflects every configured backend rather than reporting
    "unknown" on the ones the SDK does not map.
    """
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    value = details.get("cache_read")
    if isinstance(value, (int, float)):
        return int(value)
    from context_engine.store.core import _cache_read_tokens_from_usage

    metadata = getattr(message, "response_metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    return _cache_read_tokens_from_usage(metadata)


async def _invoke_memory_review(
    agent: Any,
    session_id: str,
    messages: Sequence[BaseMessage],
    prompt_template: str,
    *,
    origin_session: str | None = None,
) -> Any:
    """Invoke one memory-review fork under the BACKGROUND_REVIEW lane.

    ``session_id`` is the state identity for the fork (compression uses the main
    session; session-end salvage uses a per-wave derived key so the deleted
    session's state is never recreated). ``origin_session`` is the session a
    written memory entry is stamped with — the salvage passes the REAL session
    id while its state key stays derived. ``prompt_template`` may contain the
    FACTS placeholders rendered by :func:`_render_prompt_facts`.
    """
    sanitized = sanitize_tool_use_result_pairing(list(messages))
    review_state: dict[str, Any] = {"session_id": session_id}
    if origin_session:
        review_state["memory_origin_session"] = origin_session
    async with lane_slot(LaneType.BACKGROUND_REVIEW):
        res = await agent.ainvoke(
            input={
                **review_state,
                "messages": [
                    *sanitized,
                    HumanMessage(content=_render_prompt_facts(prompt_template)),
                ],
            }
        )
    reply = res["messages"][-1]
    cache_read = _cache_hit_tokens(reply)
    logger.info(
        "memory review done: session={} prefix_served_from_cache={}",
        session_id,
        cache_read if cache_read is not None else "unknown",
    )
    logger.debug("memory review res is {}", reply)
    return reply


async def _execute_memory_review(
    session_id: str, system_prompt: str, messages: list[BaseMessage]
) -> None:
    """Compression-time memory review: the main model, on the turn's own prefix.

    The caller hands it the DISCARDED slice, which — because the cut is a prefix
    cut — is exactly the part of the request immediately following the system
    prompt. Sending it verbatim with the same model, system prompt and tools
    turns the whole thing into a prefix-cache hit; only the review instruction
    is fresh input. ``BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"`` switches to the
    folded view for deployments whose provider caches poorly.

    BACKGROUND_REVIEW lane is event-loop-bound: acquire only on the main loop. Reviews are
    dispatched exclusively from the async compression path, so no run_async()
    worker loop ever touches this semaphore.
    """
    state_register_mem.set_state(session_id, _BACKGROUND_REVIEW_MEMORY_LOCK_KEY, True)
    try:
        prepared = _memory_review_messages(session_id, messages)
        agent = await _create_memory_review_agent(session_id, system_prompt)
        await _invoke_memory_review(agent, session_id, prepared, _MEMORY_REVIEW_PROMPT)
    finally:
        state_register_mem.set_state(session_id, _BACKGROUND_REVIEW_MEMORY_LOCK_KEY, False)
    # Consolidation runs AFTER the extraction pass (newly rescued entries are
    # available to the merge/dedupe pass) and after the extraction lock is
    # released, so a long consolidation cannot block skill review.
    if int(MEMORY_TOOL["consolidation_min_entries"]) > 0:
        try:
            await run_memory_consolidation(session_id, timeout_s=300)
        except Exception:  # noqa: BLE001 - consolidation is never load-bearing
            logger.exception("memory consolidation failed (fail-open) for {}", session_id)


def _format_entry_created(created_at: Any) -> str:
    """Compact YYYY-MM-DD for the consolidation prompt (unknown -> '-')."""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(created_at) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "-"


def _render_memory_entries_for_consolidation(max_chars: int = 12_000) -> str:
    """Render the active entries (id/usage/created + body) for the consolidator."""
    from agent.tools.memory import memory_store
    from context_engine.store.memory_entry_repository import MEMORY_TARGETS

    parts: list[str] = []
    for target in MEMORY_TARGETS:
        entries = memory_store.repo.active_entries_ordered(target)
        if not entries:
            continue
        parts.append(f"### {target} ({len(entries)} entries)")
        for entry in entries:
            content = str(entry.get("content") or "").strip()
            if len(content) > 600:
                content = content[:600] + "…"
            parts.append(
                f"- [id {entry.get('id')}, used {int(entry.get('usage_count') or 0)}x, "
                f"created {_format_entry_created(entry.get('created_at'))}] {content}"
            )
    return "\n".join(parts)[:max_chars]


async def run_memory_consolidation(
    session_id: str,
    *,
    max_turns: int = 15,
    origin_session: str | None = None,
    timeout_s: float | None = 300,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Run the deterministic aging pass, then the LLM consolidation when due.

    Cost gate: below ``MEMORY_TOOL["consolidation_min_entries"]`` active entries
    only ``prune_unused`` runs (free). The LLM pass sees a rendered listing and
    may only use the memory tool's replace/remove/add actions.
    """
    from agent.tools.memory import memory_store

    if not session_id:
        raise ValueError("session_id is required for memory consolidation")

    archived = memory_store.repo.prune_unused(int(memory_store.max_unused_days))
    memory_store.refresh_live()
    active = memory_store.repo.count()
    minimum = int(MEMORY_TOOL["consolidation_min_entries"])
    if minimum <= 0:
        return {
            "status": "skipped",
            "reason": "disabled",
            "archived": archived,
            "entries": active,
        }
    if active < minimum:
        return {
            "status": "skipped",
            "reason": "too_few_entries",
            "archived": archived,
            "entries": active,
        }
    listing = _render_memory_entries_for_consolidation()
    if not listing:
        return {"status": "skipped", "reason": "empty", "archived": archived, "entries": active}

    from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt

    prompt_text = system_prompt or _get_and_reload_system_prompt(session_id)
    agent = _build_memory_review_agent(
        _build_session_model(session_id),
        prompt_text,
        _memory_only_tool_face(),
        max_turns=max(1, int(max_turns)),
    )
    prompt = _MEMORY_CONSOLIDATION_PROMPT.replace("{entries_block}", listing)
    invoke = _invoke_memory_review(
        agent,
        session_id,
        [],
        prompt,
        origin_session=origin_session or session_id,
    )
    if timeout_s is not None and timeout_s > 0:
        reply = await asyncio.wait_for(invoke, timeout=float(timeout_s))
    else:
        reply = await invoke
    memory_store.refresh_live()
    reply_text = _msg_to_text(reply) if reply is not None else ""
    return {
        "status": "completed",
        "archived": archived,
        "entries_before": active,
        "entries_after": memory_store.repo.count(),
        "reply": reply_text[:800],
    }


def _memory_only_tool_face() -> list[BaseTool]:
    """The second session-end wave's face: only the memory tool."""
    from agent.tools.memory import build_memory_tool

    return [build_memory_tool()]
