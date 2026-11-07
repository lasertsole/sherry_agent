import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger

from config.features import MEMORY_TOOL, BACKGROUND_REVIEW_CONFIG, SUMMARIZATION
from config.path import resolve_plan_path
from pub.func import sanitize_tool_use_result_pairing
from pub.func.message.system_reminder import is_system_reminder
from pub.func.message.workspace_notice import is_workspace_notice
from runtime import StateKey, state_register_db, state_register_mem
from runtime.lane import LaneType, lane_slot

from .skill_review import (
    changed_skill_paths,
    judge_skill_update,
    restore_auto_skills,
    snapshot_auto_skills,
)


# Lazy import to avoid circular dependency with IterationBudget
def _get_iteration_budget():
    from agent.middlewares import IterationBudget

    # Mirrors the main agent's wiring (agent/core.py): middleware lists take
    # instances, not classes.
    return IterationBudget(90)


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

#: Session deletion mirrors the compression cut without compressing: wave 1
#: covers the prefix that would have been discarded, wave 2 the preserved tail.
_SESSION_END_REVIEW_FIRST_PROMPT = (
    "This session is being deleted. The messages above are the earlier portion "
    "that a compression would have discarded. Rescue any durable information — "
    "user preferences, corrections, project conventions, or system-level pitfalls — "
    "before the transcript disappears.\n\n" + _MEMORY_REVIEW_PROMPT
)
_SESSION_END_REVIEW_REST_PROMPT = (
    "This session is being deleted. The messages above are the remaining portion "
    "after that compression line; memory was just refreshed from the earlier "
    "portion. Rescue any durable information that is not already saved.\n\n" + _MEMORY_REVIEW_PROMPT
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


def _render_facts_context() -> tuple[str, str]:
    """Return ``(current facts-target content, formatted char limit)`` for review prompts.

    Reads the live content (not the frozen system-prompt snapshot) so a
    compression-time review hands the model what the file holds right now.
    """
    from agent.tools.memory import memory_store

    content = memory_store.format_live_content("facts")
    return content or "(the facts target is empty)", f"{memory_store.facts_char_limit:,}"


def _render_prompt_facts(template: str) -> str:
    """Substitute the FACTS placeholders (injected content may contain braces)."""
    facts_block, facts_limit = _render_facts_context()
    return template.replace("{facts_limit}", facts_limit).replace("{facts_block}", facts_block)


def _msg_to_text(msg: Any) -> str:
    """Flatten a message (or raw string) into plain text."""
    if isinstance(msg, str):
        return msg
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(msg)


def _msg_type(msg: Any) -> str:
    """Return a short role label for a message.

    A workspace notice or catalogue reminder is labelled ``system``: both are
    HumanMessages by role that the user never wrote, so a discarded-slice
    renderer must not read them as the user's words.
    """
    if is_workspace_notice(msg) or is_system_reminder(msg):
        return "system"
    label = getattr(msg, "type", "")
    return str(label) if label else type(msg).__name__.lower()


def _render_discarded_text(discarded_messages: Sequence[Any]) -> str:
    """Render the soon-to-be-discarded slice for background-review / skill-review prompts."""
    return "\n\n".join(f"[{_msg_type(m)}] {_msg_to_text(m)}" for m in discarded_messages)


# Skill-review prompt. The plan-completion pass is skill-only: success/failure
# evidence, loaded skills and user corrections feed class-level skill updates.
# No knowledge or FACTS writes; the raw handoff tree stays as an audit file.
_SKILL_REVIEW_PROMPT = """You are a skill-review maintainer.
A plan has just completed. Update the class-level skill library from the real
success/failure evidence below. Do NOT write knowledge; the only durable output
is a skill change, or a judge-approved no-op.

## Plan context

{plan_context}

## Evidence to consume

The context contains:
- `aggregated_handoff.success_steps`: the direct procedure that worked;
- `aggregated_handoff.failed_branches`: abandoned approaches and why they failed;
- `aggregated_handoff.hard_constraints`: invariants that must always hold;
- `aggregated_handoff.locked_decisions`: decisions and their rationale;
- `feedback.loaded_skills`: skills actually loaded during this plan;
- `feedback.user_corrections`: bounded user corrections/preferences.

Map them explicitly:
- success_steps -> procedure / verification steps in the skill;
- failed_branches -> pitfalls / when_not_to_use / anti-patterns;
- hard_constraints -> guardrails / must-hold constraints;
- locked_decisions -> why / decision rationale (never a substitute for steps);
- user_corrections -> SKILL.md body or when_to_use triggers.

## Update rules

1. Prefer patching a skill in `feedback.loaded_skills`.
2. Otherwise patch the best existing class-level umbrella (use `skill_list` and
   `skill_view`).
3. Otherwise add a support file under an existing umbrella (`references/`,
   `templates/`, or `scripts/`) and add a one-line pointer in its SKILL.md.
4. Create a new class-level umbrella only when no existing skill covers the class.
5. The skill name MUST be class-level. Never use plan_key, step_id, a PR number,
   an error string, a feature codename, or a session artifact.
6. `when_to_use` is required on every created or patched skill. It must name the
   concrete situations that should trigger loading the skill.
7. Do NOT capture environment setup failures, transient errors, negative claims
   about tools, or one-off task narratives.
8. Do not execute code. You may write scripts for a skill but cannot run them.

### Module-bound carry-forward

A lesson that names a concrete file/module/test and will be hit again by the
next plan touching that module goes into a `<module>-notes` skill (for example
`auth-module-notes`). Patch an existing one instead of creating a duplicate.
Keep it to one-line symptom -> cause -> avoidance entries.

### User preferences

Put user corrections/style/workflow preferences in the governing skill's
SKILL.md body (and, when useful, its `when_to_use`), not only in memory.

## Output contract

After the tool calls, end with exactly one JSON object on its own:

{"status":"updated","skills":["skill-name"],"reason":"what changed and why"}
or
{"status":"no_op","reason":"a concrete explanation of why this plan has no reusable signal"}

A no-op is allowed only when the evidence truly contains no reusable procedure,
pitfall, or hard constraint. The quality judge will reject an unjustified no-op.
If `judge_feedback` is present in the context, address it directly and do not
repeat the rejected approach.

"""
_SKILL_REVIEW_LOCK_KEY = StateKey.SKILL_REVIEW_LOCK
_PLAN_REF_STATE_KEY = StateKey.PLAN_REF
# Subagent result text is truncated before it enters the extraction prompt so a
# verbose child transcript cannot blow up the review context.
_MAX_SUBAGENT_RESULT_CHARS = 24 * 1024

# Post-compression todo reconciliation. The Summarization middleware fires this
# fire-and-forget after a compaction that actually discarded messages and only
# when the session has a non-empty todo list. The runner never blocks or breaks
# compression (fail-open), and a per-session lock prevents overlapping runs.
#
# The lock is written on the MAIN session on purpose: it is the cross-path
# re-entrancy coordinator, so later compressions of the same session observe it.
# Everything else the fork writes lives under the derived session key below.
_COMPRESSION_TODO_LOCK_KEY = StateKey.COMPRESSION_TODO_UPDATE_LOCK

# Metadata marker admitted by the fork's tool gate. The REAL ``todowrite``
# tool carries it (agent/tools/todolist/tools/__init__.py); ``todoread`` does
# not — the fork may only MUTATE the list, it already receives it in the prompt.
_COMPRESSION_TODO_METADATA_KEY = "todo_update"

# Derived session key for the fork's graph: keeps IterationBudget /
# ToolGuardrails / ToolCallNormalize state keys out of the MAIN session's
# namespace (compression runs inside the main agent's awrap_model_call).
_COMPRESSION_TODO_SESSION_SUFFIX = "::compression-todo"

_COMPRESSION_TODO_PROMPT = (
    "The conversation history was just compressed. The request that follows "
    "carries the discarded conversation slice and the current todo list.\n\n"
    "Reconcile the todo list with the ACTUAL progress evidenced in the "
    "discarded slice by calling `todowrite` — it is the only tool available "
    "to you:\n"
    '1. Mark items that were actually finished as "completed" and items that '
    'were abandoned or superseded as "cancelled".\n'
    '2. Add newly discovered work as "pending" items — never invent work that '
    "the discarded slice does not evidence.\n"
    "3. Write the COMPLETE list back in one `todowrite` call (full "
    "replacement, not a delta).\n"
    "4. For items you keep, preserve their existing field semantics exactly: "
    "priority / category / delegation / plan_ref / flow_id / step_id stay as "
    "they were.\n"
    "5. If nothing actually changed, write the list back unchanged — do not "
    "manufacture changes."
)

# Placeholders are substituted with ``str.replace`` (the injected conversation
# may itself contain braces, so ``str.format`` is unsafe here).
_COMPRESSION_TODO_CONTEXT_TEMPLATE = """## Discarded conversation slice (just compacted away)

<discarded_conversation>
{discarded_text}
</discarded_conversation>

## Current todo list (JSON, ordered)

<current_todos>
{todos}
</current_todos>

Update the session todo list now with `todowrite`, passing the complete \
replacement list."""

# asyncio only keeps weak references to tasks; this module-level set keeps the
# fire-and-forget compression-todo task alive until it completes.
_COMPRESSION_TODO_TASKS: set[asyncio.Task[None]] = set()

# ---------------------------------------------------------------------------
# Compression-time background-review scheduling (memory review + skill review)
#
# Both triggers are driven by the compression pipeline: the Summarization
# middleware calls
# ``schedule_memory_review`` on every compression that actually discards
# messages. The memory review is dispatched on **every** compression (a
# compaction discards content, so the review must never be skipped), and plan
# extraction is evaluated at compression time. The single-fire flag semantics
# are unchanged: ``skill_review_fired`` still guarantees one
# extraction per completion cycle and resets whenever the list is not
# all-complete.
#
# The in-flight locks live in ``state_register_mem``. Dispatch is
# fire-and-forget: compression runs inside ``wrap_model_call`` and must never
# block on a background-review agent's LLM call. While a review lock is held the
# compression dispatches nothing — nothing is queued.
# ---------------------------------------------------------------------------

_BACKGROUND_REVIEW_MEMORY_LOCK_KEY = StateKey.BACKGROUND_REVIEW_MEMORY_LOCK
_SKILL_REVIEW_FIRED_KEY = StateKey.SKILL_REVIEW_FIRED
#: Fingerprint of the plan content the last SUCCESSFUL extraction consumed.
_SKILL_REVIEW_SNAPSHOT_HASH_KEY = StateKey.SKILL_REVIEW_SNAPSHOT_HASH
_SKILL_REVIEW_FAILED_HASH_KEY = StateKey.SKILL_REVIEW_FAILED_HASH
_SKILL_REVIEW_FAILED_AT_KEY = StateKey.SKILL_REVIEW_FAILED_AT
_SKILL_REVIEW_ENABLED = BACKGROUND_REVIEW_CONFIG["skill_review_enabled"]

# Keeps scheduled background-review coroutines referenced until they complete (asyncio
# holds only weak references to tasks).
_BACKGROUND_REVIEW_TASKS: set[asyncio.Task[None]] = set()

# Session-end salvage: deletion snapshots the transcript, schedules this task,
# and returns to the purge. The derived session keys carry the fork's engine
# state so a deleted session's state is never recreated.
_SESSION_END_REVIEW_SESSION_SUFFIX = "::session-end-salvage"
_SESSION_END_REVIEW_TASKS: set[asyncio.Task[None]] = set()
_SESSION_END_REVIEW_INFLIGHT: set[str] = set()
_SESSION_END_SNAPSHOT_TIMEOUT_S = 5.0


def _plan_snapshot_hash(todos: list[dict]) -> str:
    """Content fingerprint of the plan (id + content + status + plan_ref).

    Deliberately blind to everything else a todo row carries: the extraction is
    about the plan's own content, and a fingerprint that moved for unrelated
    bookkeeping would re-extract on every turn.
    """
    items = sorted(
        (
            {
                "id": str(todo.get("id", "")),
                "content": str(todo.get("content", "")),
                "status": str(todo.get("status", "")),
                "plan_ref": str(todo.get("plan_ref", "")),
            }
            for todo in todos
        ),
        key=lambda item: item["id"],
    )
    return hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _detect_todo_all_complete(session_id: str) -> bool:
    """Whether the plan is complete AND its current content is not extracted yet.

    Conditions:
    1. todos exist (non-empty)
    2. every todo is ``completed`` or ``cancelled``
    3. either nothing was extracted for this completion cycle yet, or the plan
       content CHANGED since the last successful extraction

    The once-per-cycle flag alone cannot see an in-place edit of a completed todo
    (status unchanged), which is how a changed plan used to go un-extracted; the
    snapshot fingerprint closes that hole. The flag is reset whenever the todo
    list is not (or no longer) all-complete, so the next completion extracts
    again. Reads fail open — a broken todo store must never break a turn.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("todo-complete detection failed (fail-open) for {}", session_id)
        return False

    if not todos:
        return False

    all_done = all(t.get("status") in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _SKILL_REVIEW_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _SKILL_REVIEW_FIRED_KEY, False)
    if not already_fired:
        state_register_db.set_state(session_id, _SKILL_REVIEW_FIRED_KEY, True)
        return True

    stored = state_register_db.get_state(session_id, _SKILL_REVIEW_SNAPSHOT_HASH_KEY, "") or ""
    return _plan_snapshot_hash(todos) != stored


def _background_reviews_locked(session_id: str) -> bool:
    """True while a memory review or skill review is in flight."""
    return bool(
        state_register_mem.get_state(session_id, _BACKGROUND_REVIEW_MEMORY_LOCK_KEY, False)
        or state_register_mem.get_state(session_id, _SKILL_REVIEW_LOCK_KEY, False)
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


class _BackgroundReviewLimitTool(AgentMiddleware):
    """Tool gate for background-review sub-agents.

    With no ``allowed_metadata_key`` (the default), a tool passes only when its
    metadata carries ``background_review: True`` — the memory review's rule (``memory``,
    ``knowledge``, ``question``, plan mode). With an explicit metadata key, a
    tool passes only when ``tool.metadata[key] is True`` (strict identity), so
    the compression todo fork can admit exactly the metadata-marked
    ``todowrite`` while every unmarked tool is rejected.

    The skills toolset is deliberately UNTAGGED: skill extraction is the
    plan-completion fork's job, and that fork binds its own explicit four-tool
    face (``_skill_review_tool_face``) instead of riding this gate.
    """

    def __init__(self, allowed_metadata_key: str | None = None) -> None:
        super().__init__()
        self._allowed_metadata_key = allowed_metadata_key

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        logger.bind(middleware=type(self).__name__).debug("awrap_tool_call hook fired")
        tool_name: str = request.tool_call.get("name", "unknown")

        if not self._is_allowed(request.tool):
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] is not allowed during a background review. "
                    "Execution has been skipped. Please reconsider your approach."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return await handler(request)

    @staticmethod
    def _is_background_review_allowed(tool: Any) -> bool:
        """Legacy metadata rule: only tools tagged ``background_review: True`` pass."""
        if tool is not None and isinstance(getattr(tool, "metadata", None), dict):
            return bool(tool.metadata.get("background_review", False))
        return False

    def _is_allowed(self, tool: Any) -> bool:
        if self._allowed_metadata_key is None:
            return self._is_background_review_allowed(tool)
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            return metadata.get(self._allowed_metadata_key) is True
        return False


class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""

    session_id: str


async def _create_background_review_agent(
    system_prompt: str,
    allowed_metadata_key: str | None = None,
    tools: Sequence[BaseTool] | None = None,
):
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_main_llm

    if tools is None:
        from agent import get_agent_tools

        agent_tools: Sequence[BaseTool] = get_agent_tools()
    else:
        agent_tools = list(tools)

    main_llm = build_main_llm()
    return create_agent(
        model=main_llm,
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            _BackgroundReviewLimitTool(allowed_metadata_key=allowed_metadata_key),
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=list(agent_tools),
    )


def _session_tool_face(session_id: str) -> list[BaseTool]:
    """The tools the session's turn actually binds, in the same order.

    The provider's prefix cache is keyed on the request prefix, and the tool
    block is part of it — a review that binds a different tool list (or a
    different order) misses the cache at the very first token. Every main tool
    is always available now (the per-session tool selection was removed), so the
    review mirrors the process-wide list verbatim.
    """
    from agent import get_agent_tools

    return list(get_agent_tools())


def _build_session_model(session_id: str) -> Any:
    """A fresh main-model instance matching this session's last turn.

    Built from the CONFIGURATION the turn used (``current_session_model_config``)
    rather than a captured instance: the graph is rebuilt — and its transport
    pools closed — every turn, so a captured client can outlive its loop;
    a fresh instance with the same provider/model/thinking/temperature
    serialises the identical body and still hits the provider's prefix cache.
    No descriptor known (no main-chain call yet in this process, e.g. a session
    restored from disk right at a compaction) builds the env-default model.
    """
    from agent.middlewares.thinking_control.core import current_session_model_config
    from models import build_main_llm, build_main_llm_for_profile

    config = current_session_model_config(session_id) or {}
    override = config.get("override")
    temperature = config.get("temperature")
    thinking_kwargs = dict(config.get("thinking_kwargs") or {})
    custom_mapping = config.get("custom_mapping") or None
    if override:
        return build_main_llm_for_profile(
            provider=override.get("provider"),
            model=override["model"],
            api_key=override.get("api_key"),
            base_url=override.get("base_url"),
            temperature=temperature,
            custom_mapping=custom_mapping,
            **thinking_kwargs,
        )
    return build_main_llm(temperature=temperature, custom_mapping=custom_mapping, **thinking_kwargs)


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


def _resolve_plan_ref(session_id: str, todos: list[dict]) -> str:
    """Resolve the plan ref: session-level state first, then first todo carrying one."""
    try:
        stored = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
    except Exception:
        stored = ""
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    for todo in todos:
        ref = todo.get("plan_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    return ""


_PLAN_CONTEXT_LIST_CAP = 10

#: Run relevance values used by the snapshot filter.
_RELEVANCE_ACTIVE = "active"
_RELEVANCE_UNKNOWN = "unknown"
_RELEVANCE_CANCELLED = "cancelled"
_RELEVANCE_SUPERSEDED = "superseded"


def _read_subagent_runs(session_id: str) -> list[dict]:
    """Collect every descendant run for this session, handoff included (fail-open).

    Descendants (not just direct children) so a grandchild's experience reaches
    the tree: ``_assemble_handoff_tree`` re-parents by ``requester_session_key``.
    ``handoff`` is the dict captured at completion; ``None`` means the child
    did not emit the block and the plan extractor falls back to ``result_text``.
    """
    runs_out: list[dict] = []
    try:
        from agent.tools.subagent.registry import list_descendant_runs

        runs = list_descendant_runs(f"agent:main:session:{session_id}")
    except Exception:
        logger.exception("skill review: failed to list subagent runs for {}", session_id)
        return runs_out

    for run in runs:
        outcome = run.execution.outcome
        result_text = run.completion.result_text or ""
        role = getattr(run.functional_role, "value", str(run.functional_role))
        runs_out.append(
            {
                "task_name": run.task_name,
                "task": run.task,
                "result_text": result_text[:_MAX_SUBAGENT_RESULT_CHARS],
                "outcome": outcome.status.value if outcome else "unknown",
                "error": outcome.error if outcome else None,
                "handoff": run.completion.handoff,
                "step_id": run.step_id,
                "flow_id": run.flow_id,
                "spawn_depth": run.depth,
                "label": run.label,
                "functional_role": role,
                "requester_session_key": run.requester_session_key,
                "child_session_key": run.child_session_key,
            }
        )
    return runs_out


def _read_flow_status(session_id: str, flow_id: str) -> dict:
    """One flow's status + its steps' statuses, or {} (fail-open)."""
    try:
        from agent.tools.taskflow.registry.store_sqlite import get_flow_sync

        flow = get_flow_sync(flow_id, session_id)
    except Exception:
        logger.debug("skill review: failed to read taskflow {}", flow_id, exc_info=True)
        return {}
    if not flow:
        return {}
    steps: dict[str, str] = {}
    for step in (flow.get("state") or {}).get("steps") or []:
        step_id = str(step.get("step_id") or "")
        if step_id:
            steps[step_id] = str(step.get("status") or "")
    return {
        "status": str(flow.get("status") or ""),
        "superseded_by": flow.get("superseded_by"),
        "steps": steps,
    }


def _read_plan_snapshot(session_id: str) -> dict | None:
    """The CURRENT todos + flows, used to filter/re-map runs (None = fail-open).

    Pure read, no writes: the same runs + the same snapshot produce the same
    aggregate, so a retried extraction overwrites idempotently.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("skill review: failed to read the plan snapshot for {}", session_id)
        return None

    todo_by_step: dict[str, dict] = {}
    flows: dict[str, dict] = {}
    for todo in todos:
        step_id = str(todo.get("step_id") or "").strip()
        if step_id:
            todo_by_step[step_id] = todo
        flow_id = str(todo.get("flow_id") or "").strip()
        if flow_id and flow_id not in flows:
            flows[flow_id] = _read_flow_status(session_id, flow_id)
    return {"todos": todos, "todo_by_step": todo_by_step, "flows": flows}


def _classify_run_relevance(run: dict, snapshot: dict | None) -> str:
    """Why a run is (or is not) part of the CURRENT plan, for the filter.

    ``cancelled`` / ``superseded`` runs are dropped: the branch they belong to
    is gone, so including them would over-extract. A run whose coordinates are
    absent from the snapshot (a free-form spawn, or a todo deleted by a
    replan) stays ``unknown`` — its experience is still world fact.
    """
    if not snapshot:
        return _RELEVANCE_UNKNOWN
    flow = snapshot["flows"].get(str(run.get("flow_id") or ""))
    if flow:
        status = str(flow.get("status") or "").lower()
        if flow.get("superseded_by") or status in (_RELEVANCE_SUPERSEDED, _RELEVANCE_CANCELLED):
            return _RELEVANCE_SUPERSEDED
    step_id = str(run.get("step_id") or "")
    if step_id:
        todo = snapshot["todo_by_step"].get(step_id)
        if todo is None:
            return _RELEVANCE_UNKNOWN
        if str(todo.get("status") or "").lower() == _RELEVANCE_CANCELLED:
            return _RELEVANCE_CANCELLED
        return _RELEVANCE_ACTIVE
    return _RELEVANCE_UNKNOWN


def _resolve_position(run: dict, snapshot: dict | None) -> int | None:
    """The run's step_id → the todo's CURRENT position (None when unresolvable).

    Re-resolved against the live snapshot, so a replan that shifted the list
    cannot silently write the spawn-time position. The extraction prompt is
    told to fall back to task-text matching when this is None.
    """
    if not snapshot:
        return None
    step_id = str(run.get("step_id") or "")
    if not step_id:
        return None
    todo = snapshot["todo_by_step"].get(step_id)
    if todo is None:
        return None
    try:
        return int(todo.get("position"))
    except (TypeError, ValueError):
        return None


def _filter_runs_by_snapshot(runs: list[dict], snapshot: dict | None = None) -> list[dict]:
    """Drop cancelled/superseded runs and attach relevance + resolved_position.

    Runs with no snapshot (None) pass through unchanged (fail-open). The plan
    context's ``subagent_runs`` IS this filtered list, so the extraction LLM
    never re-adds a discarded branch.
    """
    if snapshot is None:
        return list(runs)
    kept: list[dict] = []
    for run in runs:
        relevance = _classify_run_relevance(run, snapshot)
        if relevance in (_RELEVANCE_CANCELLED, _RELEVANCE_SUPERSEDED):
            continue
        enriched = dict(run)
        enriched["relevance"] = relevance
        enriched["resolved_position"] = _resolve_position(run, snapshot)
        kept.append(enriched)
    return kept


def _handoff_label(run: dict) -> str:
    """A readable node label; falls back to the role plus a run-id prefix."""
    label = str(run.get("label") or "").strip()
    if label:
        return label
    role = str(run.get("functional_role") or "subagent")
    run_id = str(run.get("child_session_key") or "")
    return f"{role}-{run_id[-8:]}" if run_id else role


def _assemble_handoff_tree(runs: list[dict]) -> list[dict]:
    """Code-assemble the per-layer handoffs into a parent→child tree.

    Each node's four experience fields stay exactly what ITS layer produced;
    depth/label/outcome/children are injected here (never by the LLM). Orphans
    (a parent was filtered out by the snapshot) are promoted to roots so their
    experience survives.
    """
    children_map: dict[str, list[dict]] = {}
    for run in runs:
        children_map.setdefault(str(run.get("requester_session_key") or ""), []).append(run)
    run_by_child_session = {
        str(run.get("child_session_key")): run for run in runs if run.get("child_session_key")
    }

    def _build_node(run: dict) -> dict | None:
        handoff = run.get("handoff")
        if not isinstance(handoff, dict) or not handoff:
            return None
        node = dict(handoff)
        node["depth"] = run.get("spawn_depth")
        node["agent_label"] = _handoff_label(run)
        node["outcome"] = run.get("outcome")
        node["children"] = [
            child_node
            for child_node in (
                _build_node(child)
                for child in children_map.get(str(run.get("child_session_key") or ""), [])
            )
            if child_node
        ]
        return node

    def _is_root(run: dict) -> bool:
        requester = str(run.get("requester_session_key") or "")
        if requester.startswith("agent:main:session:"):
            return True
        # Orphan (parent filtered away) → promote so its experience survives.
        return requester not in run_by_child_session

    roots = [_build_node(run) for run in runs if _is_root(run)]
    return [node for node in roots if node]


def _walk_handoff(node: dict, aggregate: dict, seen: set[str], source_prefix: str) -> None:
    """Depth-first walk of one handoff node, flattening into ``aggregate``."""
    depth = node.get("depth", "?")
    label = node.get("agent_label", "?")
    source = f"{source_prefix}depth-{depth}:{label}" if source_prefix else f"depth-{depth}:{label}"
    branches = node.get("failed_branches")
    if not isinstance(branches, list):
        # Pre-rename handoffs stored ``failed_paths`` with a ``path`` key.
        branches = node.get("failed_paths") or []
    for failed in branches:
        if not isinstance(failed, dict):
            continue
        aggregate["failed_branches"].append(
            {
                "branch": str(failed.get("branch") or failed.get("path") or ""),
                "failure_reason": str(failed.get("failure_reason") or ""),
                "source": source,
            }
        )
    for step in node.get("success_steps", []) or []:
        if not isinstance(step, dict):
            continue
        aggregate["success_steps"].append(
            {
                "step": str(step.get("step") or ""),
                "tool": str(step.get("tool") or ""),
                "params": str(step.get("params") or ""),
                "outcome": str(step.get("outcome") or ""),
                "source": source,
            }
        )
    for constraint in node.get("discovered_hard_constraints", []) or []:
        text = str(constraint)
        if text and text not in seen:
            seen.add(text)
            aggregate["hard_constraints"].append(text)
    for decision in node.get("locked_decisions", []) or []:
        if not isinstance(decision, dict):
            continue
        aggregate["locked_decisions"].append(
            {
                "decision": str(decision.get("decision") or ""),
                "reason": str(decision.get("reason") or ""),
                "source": source,
            }
        )
    for child in node.get("children", []) or []:
        _walk_handoff(child, aggregate, seen, source_prefix=f"{source} > ")


def _aggregate_handoffs(tree_roots: list[dict]) -> dict[str, Any]:
    """Flatten the assembled forest (pure; same input → same output)."""
    aggregate: dict[str, Any] = {
        "failed_branches": [],
        "success_steps": [],
        "hard_constraints": [],
        "locked_decisions": [],
        "handoff_forest": tree_roots,
    }
    seen: set[str] = set()
    for root in tree_roots:
        _walk_handoff(root, aggregate, seen, source_prefix="")
    return aggregate


def _compact_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    """The plan-context view: capped flat lists + totals, NO forest.

    Keeps the prompt increment fixed (~500-800 tokens) regardless of how many
    subagents ran; the full tree lives in ``handoff-tree.json`` as the audit
    artifact and is no longer exposed as a model-facing knowledge read.
    """
    failed_branches = aggregate.get("failed_branches") or []
    success_steps = aggregate.get("success_steps") or []
    hard_constraints = aggregate.get("hard_constraints") or []
    locked_decisions = aggregate.get("locked_decisions") or []
    return {
        "failed_branches": failed_branches[:_PLAN_CONTEXT_LIST_CAP],
        "success_steps": success_steps[:_PLAN_CONTEXT_LIST_CAP],
        "hard_constraints": hard_constraints[:_PLAN_CONTEXT_LIST_CAP],
        "locked_decisions": locked_decisions[:_PLAN_CONTEXT_LIST_CAP],
        "total_failed_branches": len(failed_branches),
        "total_success_steps": len(success_steps),
        "total_hard_constraints": len(hard_constraints),
        "total_locked_decisions": len(locked_decisions),
    }


def _row_text(row: dict[str, Any]) -> str:
    """Flatten a persisted message row's content to text."""
    content = row.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _row_skill_name(call: Any) -> str:
    """Extract a skill name from a LangChain/OpenAI-style tool call."""
    if not isinstance(call, dict):
        return ""
    name = call.get("name")
    args = call.get("args")
    if not name:
        function = call.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            args = function.get("arguments")
    if str(name or "") != "skill_view":
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return ""
    if isinstance(args, dict):
        return str(args.get("name") or "").strip()
    return ""


def _loaded_skill_details(names: list[str]) -> list[dict[str, str]]:
    """Resolve loaded skill names to their index metadata (best-effort)."""
    if not names:
        return []
    try:
        from skills.loader import scan_skills

        index = {str(skill.get("name") or ""): skill for skill in scan_skills()}
    except Exception:
        index = {}
    result: list[dict[str, str]] = []
    for name in names:
        meta = index.get(name) or {}
        result.append(
            {
                "name": name,
                "description": str(meta.get("description") or ""),
                "when_to_use": str(meta.get("when_to_use") or ""),
            }
        )
    return result


def _recent_usage_skill_names(limit: int) -> list[str]:
    """Recently used auto skills, as a best-effort fallback for preload paths.

    TaskFlow/cron skill preloading bumps ``use_count`` without a transcript
    ``skill_view`` call. This is not plan-scoped, so callers add it only after
    the exact transcript-derived names.
    """
    if limit <= 0:
        return []
    try:
        from agent.tools.pub_base.skill_usage import load_usage

        records = load_usage()
    except Exception:
        return []
    rows = []
    for name, record in records.items():
        if not isinstance(record, dict):
            continue
        stamp = str(record.get("last_used_at") or record.get("last_viewed_at") or "")
        if stamp:
            rows.append((stamp, str(name)))
    rows.sort(reverse=True)
    return [name for _stamp, name in rows[:limit]]


def _collect_skill_review_feedback(session_id: str) -> dict[str, Any]:
    """Bounded user corrections + actually-loaded skills for the skill review.

    Skill review has never received the conversation; the skill-review
    prompt needs the user's explicit corrections and the skills the plan really
    touched. This reads only the recent persisted turns, skips internal carriers
    and system reminders, and caps both lists so the skill-review prompt stays small.
    """
    corrections: list[str] = []
    loaded_names: list[str] = []
    try:
        from context_engine.store.core import get_messages_by_lastest_n_turns

        rows = get_messages_by_lastest_n_turns(
            session_id,
            last_n=max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_turns"])),
            only_eligible=True,
        )
    except Exception as exc:  # noqa: BLE001 - advisory context, never load-bearing
        logger.debug("skill review: feedback read failed for {}: {}", session_id, exc)
        return {"loaded_skills": [], "user_corrections": []}

    max_items = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_max_items"]))
    max_chars = max(0, int(BACKGROUND_REVIEW_CONFIG["skill_review_feedback_chars"]))
    for row in reversed(rows or []):
        role = str(row.get("role") or "")
        if role == "human":
            if row.get("origin") not in (None, "user"):
                continue
            text = _row_text(row).strip()
            if (
                not text
                or text.startswith("[SYSTEM DIRECTIVE")
                or text.startswith("<system-reminder")
            ):
                continue
            if len(corrections) >= max_items:
                continue
            corrections.append(text[:500])
            continue
        if role != "ai":
            continue
        for call in row.get("tool_calls") or []:
            name = _row_skill_name(call)
            if name and name not in loaded_names:
                loaded_names.append(name)

    # Chronological order is more useful to a reviewer than newest-first.
    max_loaded = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_loaded_skills_max"]))
    for name in _recent_usage_skill_names(max_loaded):
        if name not in loaded_names:
            loaded_names.append(name)
    loaded_names = loaded_names[:max_loaded]
    if max_chars:
        budget = 0
        bounded: list[str] = []
        for text in corrections:
            remaining = max_chars - budget
            if remaining <= 0:
                break
            bounded.append(text[:remaining])
            budget += min(len(text), remaining)
        corrections = bounded
    return {
        "loaded_skills": _loaded_skill_details(loaded_names),
        "user_corrections": corrections,
    }


def _build_plan_context(session_id: str) -> dict[str, Any]:
    """Build the structured context handed to one skill-review pass.

    Reads:
    1. Plan file (``plan_ref`` from session state, else first non-empty todo
       ref), resolved through ``config.path.resolve_plan_path``
       (``workspace/sessions/<session_id>/plans/``)
    2. Todos (todos.db)
    3. Subagent runs (registry queries)

    Returns an empty dict when there is no todo list to extract from; every
    downstream read is fail-open so a missing plan file never breaks the
    extraction pass.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("skill review: failed to read todos for {}", session_id)
        return {}
    if not todos:
        return {}

    plan_ref = _resolve_plan_ref(session_id, todos)
    resolved = resolve_plan_path(plan_ref, session_id) if plan_ref else None
    plan_name = ""
    plan_content = ""
    if resolved is not None:
        try:
            plan_content = resolved.read_text(encoding="utf-8")
            plan_name = resolved.stem
        except OSError:
            logger.exception("skill review: failed to read plan file {}", resolved)
    if not plan_name:
        # Full-id hash: ids sharing an 8-char prefix must not share a fallback dir.
        from agent.tools.todolist.knowledge.identity import fallback_plan_name

        plan_name = fallback_plan_name(session_id)
    plan_path = str(resolved) if resolved is not None else plan_ref

    runs = _read_subagent_runs(session_id)
    snapshot = _read_plan_snapshot(session_id)
    filtered = _filter_runs_by_snapshot(runs, snapshot)
    aggregated = _aggregate_handoffs(_assemble_handoff_tree(filtered))

    return {
        "plan_name": plan_name,
        "plan_path": plan_path,
        "plan_content": plan_content,
        "todos": todos,
        "subagent_runs": filtered,
        "aggregated_handoff": aggregated,
        "feedback": _collect_skill_review_feedback(session_id),
        "plan_snapshot": snapshot,
    }


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


def _session_end_cutoff(messages: list[BaseMessage], session_id: str) -> int:
    """The read-only compression cut for the captured session transcript.

    This deliberately does NOT run any compression step: no non-LLM pruning, no
    media offload, no summary, no checkpoint write. It only reuses the existing
    preserve-budget cutoff so the two salvage waves cover exactly the prefix a
    compression would have discarded and the tail it would have kept.
    """
    from agent.middlewares.summarization.core import Summarization
    from models.LLMs.main_llm import max_tokens as main_llm_context_window

    try:
        window = int(main_llm_context_window) if main_llm_context_window else None
    except (TypeError, ValueError):
        window = None
    summarizer = Summarization(model=None, main_llm_context_window=window)
    cutoff = summarizer._determine_cutoff(messages, session_id)
    return max(0, min(int(cutoff), len(messages)))


async def _snapshot_session_for_salvage(
    session_id: str,
) -> tuple[list[BaseMessage], Any, str, list[BaseTool], int] | None:
    """Capture everything the detached review needs before the purge starts.

    The checkpoint read is the only framework interaction. The model, system
    prompt and tool face are captured here rather than resolved by the background
    task because the session's config/registers are gone once the purge lands.
    """
    from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt
    from agent.state_port import read_messages

    messages = list(await read_messages(session_id))
    if not messages:
        return None
    system_prompt = _get_and_reload_system_prompt(session_id)
    model = _build_session_model(session_id)
    tools = list(_session_tool_face(session_id))
    if not any(getattr(tool, "name", "") == "memory" for tool in tools):
        tools = _memory_only_tool_face()
    cutoff = _session_end_cutoff(messages, session_id)
    return messages, model, system_prompt, tools, cutoff


def _rebuild_session_end_prompt(session_id: str, fallback: str) -> str:
    """Reload memory after wave 1 and rebuild the second wave's system prompt.

    The rebuild is intentionally NON-persisting: the session is being deleted,
    so writing the prompt back through ``_persist_system_prompt`` would recreate
    its state-register rows. A rebuild failure falls back to the captured prompt.
    """
    try:
        from agent.tools.memory import memory_store
        from workspace.prompt_builder import build_system_prompt

        memory_store.load_from_store()
        rebuilt = build_system_prompt(session_id=session_id)
        return rebuilt or fallback
    except Exception:  # noqa: BLE001 - the second wave must still run
        logger.warning(
            "session-end memory review: prompt rebuild failed; using captured prompt session={}",
            session_id,
            exc_info=True,
        )
        return fallback


async def _run_session_end_wave(
    *,
    session_id: str,
    wave: int,
    model: Any,
    system_prompt: str,
    tools: Sequence[BaseTool],
    messages: Sequence[BaseMessage],
    prompt_template: str,
) -> bool:
    """Run one detached review wave under its own derived session key."""
    derived_session_id = f"{session_id}{_SESSION_END_REVIEW_SESSION_SUFFIX}:{wave}"
    try:
        agent = _build_memory_review_agent(
            model,
            system_prompt,
            tools,
            allowed_metadata_key="background_review_memory",
        )
        timeout = max(1.0, float(BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]))
        await asyncio.wait_for(
            _invoke_memory_review(
                agent,
                derived_session_id,
                messages,
                prompt_template,
                origin_session=session_id,
            ),
            timeout=timeout,
        )
        return True
    except Exception:  # noqa: BLE001 - a deleted session's review is best-effort
        logger.exception(
            "session-end memory review wave {} failed (fail-open) session={}",
            wave,
            session_id,
        )
        return False
    finally:
        try:
            state_register_mem.clear_session(derived_session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "session-end memory review: failed to clear derived state {}: {}",
                derived_session_id,
                exc,
            )


async def _run_session_end_salvage(
    *,
    session_id: str,
    model: Any,
    system_prompt: str,
    tools: Sequence[BaseTool],
    first_wave: Sequence[BaseMessage],
    rest_wave: Sequence[BaseMessage],
) -> None:
    """Run the two deletion-time waves in order, each independently fail-open."""
    first_ok = False
    updated_prompt = system_prompt
    if first_wave:
        first_ok = await _run_session_end_wave(
            session_id=session_id,
            wave=1,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            messages=first_wave,
            prompt_template=_SESSION_END_REVIEW_FIRST_PROMPT,
        )
    if rest_wave:
        if first_ok:
            updated_prompt = _rebuild_session_end_prompt(session_id, system_prompt)
        await _run_session_end_wave(
            session_id=session_id,
            wave=2,
            model=model,
            system_prompt=updated_prompt,
            tools=_memory_only_tool_face(),
            messages=rest_wave,
            prompt_template=_SESSION_END_REVIEW_REST_PROMPT,
        )
    try:
        await run_memory_consolidation(
            session_id,
            max_turns=5,
            timeout_s=60,
            origin_session=session_id,
            system_prompt=updated_prompt,
        )
    except Exception:  # noqa: BLE001 - deletion continues without consolidation
        logger.exception("session-end memory consolidation failed (fail-open) for {}", session_id)


async def salvage_session_memory(session_id: str) -> bool:
    """Snapshot a session and schedule its detached session-end memory review.

    Called by ``purge_session`` BEFORE any destructive step. It returns as soon
    as the snapshot is captured and the background task is scheduled; the purge
    therefore proceeds immediately. The snapshot has its own short budget so a
    slow checkpoint read cannot delay deletion indefinitely.

    Scope is the MAIN session only; side chats and subagent children are not
    passed through this function. The review writes global memory files, uses no
    checkpointer, and never resurrects the deleted session's state.
    """
    if not bool(BACKGROUND_REVIEW_CONFIG.get("session_end_review_enabled", True)):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("session-end memory review: no running loop for {}", session_id)
        return False
    if session_id in _SESSION_END_REVIEW_INFLIGHT:
        return False

    _SESSION_END_REVIEW_INFLIGHT.add(session_id)
    try:
        snapshot = await asyncio.wait_for(
            _snapshot_session_for_salvage(session_id),
            timeout=_SESSION_END_SNAPSHOT_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        raise
    except Exception:  # noqa: BLE001 - deletion continues without salvage
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        logger.warning(
            "session-end memory review: snapshot failed/ timed out (fail-open) session={}",
            session_id,
            exc_info=True,
        )
        return False
    if snapshot is None:
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        logger.debug("session-end memory review: no transcript to salvage for {}", session_id)
        return False

    messages, model, system_prompt, tools, cutoff = snapshot
    if cutoff > 0:
        first_wave = messages[:cutoff]
        rest_wave = messages[cutoff:]
    else:
        first_wave = messages
        rest_wave = []

    coro = _run_session_end_salvage(
        session_id=session_id,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        first_wave=first_wave,
        rest_wave=rest_wave,
    )
    try:
        task = loop.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        return False

    _SESSION_END_REVIEW_TASKS.add(task)

    def _done(completed: asyncio.Task[None]) -> None:
        _SESSION_END_REVIEW_TASKS.discard(completed)
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)

    task.add_done_callback(_done)
    logger.info(
        "session-end memory review scheduled session={} cutoff={} first={} rest={}",
        session_id,
        cutoff,
        len(first_wave),
        len(rest_wave),
    )
    return True


async def _create_skill_review_agent(system_prompt: str):
    """The skill-review fork: auxiliary model, explicit skill tool binding.

    NOT ``_create_background_review_agent``: the memory review keeps the main model (it reads
    a conversation), while this pass consumes structured plan evidence and runs
    a template-driven skill review — the same shape the step / completion judges
    already run on the auxiliary model, at a fraction of the cost and with no
    chance of crowding the main window. The system prompt is kept: the skills
    index in it decides patch-vs-create.

    The tool face is bound to exactly the skill tools the prompt drives. This
    pass must not write knowledge or facts: those outputs were retired from the
    plan-completion path, and the bound list is the barrier.
    """
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_auxiliary_llm

    return create_agent(
        model=build_auxiliary_llm(temperature=0),
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=_skill_review_tool_face(),
    )


#: The skill tools the skill-review pass drives. Knowledge and memory are
#: deliberately absent: this pass only updates the skill library.
_SKILL_REVIEW_TOOL_NAMES = ("skill_list", "skill_view", "skill_manage")


def _skill_review_tool_face() -> list[BaseTool]:
    """The extraction fork's bound tools, in the main face's order."""
    from agent import get_agent_tools

    return [
        tool for tool in get_agent_tools() if getattr(tool, "name", "") in _SKILL_REVIEW_TOOL_NAMES
    ]


async def _write_handoff_tree_safely(session_id: str, context: dict[str, Any]) -> None:
    """Persist the code-merged handoff tree BEFORE the extraction LLM runs.

    An interrupted synthesis must not lose the raw merged data: the file is
    written first (atomic replace). The review's skill update is a second,
    lossy layer on top and the audit file remains available if it is rejected.
    Fail-open.
    """
    aggregated = context.get("aggregated_handoff") or {}
    if not aggregated.get("handoff_forest"):
        return
    try:
        from agent.tools.todolist.knowledge.identity import resolve_plan_identity
        from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

        identity = resolve_plan_identity(session_id, str(context.get("plan_name") or ""))
        if identity is None:
            return
        await KnowledgeStore.write_handoff_tree(identity, aggregated)
    except Exception:
        logger.exception(
            "skill review: failed to write handoff-tree.json for {} (fail-open)",
            session_id,
        )


def _last_ai_text(res: dict[str, Any]) -> str:
    """Return the final assistant text from a skill-review invocation."""
    messages = res.get("messages") if isinstance(res, dict) else None
    for message in reversed(messages or []):
        if isinstance(message, AIMessage) or getattr(message, "type", "") == "ai":
            return _msg_to_text(message)
    return ""


def _extract_no_op_reason(text: str) -> str | None:
    """Read the skill review's final ``{"status":"no_op",...}`` JSON block."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("status") or "").lower() != "no_op":
        return None
    return str(payload.get("reason") or "").strip() or "no-op without a reason"


def _skill_review_failure_active(session_id: str, snapshot_hash: str) -> bool:
    """Whether this exact plan snapshot is in a skill-review failure cooldown."""
    stored_hash = str(
        state_register_db.get_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, "") or ""
    )
    if stored_hash != snapshot_hash:
        return False
    try:
        failed_at = float(state_register_db.get_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, 0))
    except (TypeError, ValueError):
        failed_at = 0.0
    cooldown = max(0, int(BACKGROUND_REVIEW_CONFIG["skill_review_failure_cooldown_s"]))
    return failed_at > 0 and (time.time() - failed_at) < cooldown


def _mark_skill_review_failed(session_id: str, snapshot_hash: str) -> None:
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, snapshot_hash)
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, time.time())


def _clear_skill_review_failed(session_id: str) -> None:
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, "")
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, 0)


async def _run_skill_review(session_id: str) -> None:
    """Run the skill review with judge/rollback/retry.

    The plan's raw handoff tree is written first for audit. The review then
    receives a bounded view of the evidence, loaded skills and user corrections;
    its only tools are the skill trio (``skill_list`` / ``skill_view`` /
    ``skill_manage``). No knowledge or facts are written.

    Each attempt snapshots ``skills/auto`` first. A judge-approved change (or a
    justified no-op) advances the success fingerprint; a rejected attempt is
    restored and retried with the judge's feedback. Exhaustion records a failure
    marker and leaves the plan eligible for a later retry after the cooldown.
    """
    state_register_mem.set_state(session_id, _SKILL_REVIEW_LOCK_KEY, True)
    try:
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt
        from agent.tools.pub_base.skill_provenance import (
            BACKGROUND_REVIEW as BACKGROUND_REVIEW_ORIGIN,
            reset_current_write_origin,
            set_current_write_origin,
        )

        context = _build_plan_context(session_id)
        if not context:
            logger.debug("skill review: no plan context for session {}", session_id)
            return

        snapshot_hash = _plan_snapshot_hash(list(context.get("todos") or []))
        if _skill_review_failure_active(session_id, snapshot_hash):
            logger.debug("skill review: skill-review failure cooldown active for {}", session_id)
            return

        # Raw merged tree first (code), then the compact view for the LLM.
        await _write_handoff_tree_safely(session_id, context)
        render_context = dict(context)
        render_context.pop("plan_snapshot", None)
        render_context["aggregated_handoff"] = _compact_aggregate(
            context.get("aggregated_handoff") or {}
        )
        system_prompt = _get_and_reload_system_prompt(session_id)

        attempts = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_max_attempts"]))
        before = snapshot_auto_skills()
        feedback = ""
        for attempt in range(1, attempts + 1):
            prompt_context = dict(render_context)
            if feedback:
                prompt_context["judge_feedback"] = feedback
            prompt = _SKILL_REVIEW_PROMPT.replace(
                "{plan_context}", json.dumps(prompt_context, ensure_ascii=False, indent=2)
            )
            async with lane_slot(LaneType.BACKGROUND_REVIEW):
                agent = await _create_skill_review_agent(system_prompt)
                token = set_current_write_origin(BACKGROUND_REVIEW_ORIGIN)
                try:
                    res = await agent.ainvoke(
                        input={
                            "session_id": session_id,
                            "messages": [HumanMessage(content=prompt)],
                        }
                    )
                finally:
                    reset_current_write_origin(token)

            text = _last_ai_text(res)
            logger.debug("skill review attempt {}/{} result: {}", attempt, attempts, text[:2000])
            after = snapshot_auto_skills()
            judgement = await judge_skill_update(
                render_context,
                before,
                after,
                _extract_no_op_reason(text),
            )
            logger.info(
                "skill-review attempt {}/{}: pass={} verdict={} grounded={} "
                "reusable={} trigger={} quality={} changed={} reason={}",
                attempt,
                attempts,
                judgement.passed,
                judgement.verdict,
                judgement.grounded_in_run,
                judgement.reusable,
                judgement.trigger_quality,
                judgement.quality,
                len(changed_skill_paths(before, after)),
                judgement.feedback,
            )
            if judgement.passed:
                _clear_skill_review_failed(session_id)
                state_register_db.set_state(
                    session_id, _SKILL_REVIEW_SNAPSHOT_HASH_KEY, snapshot_hash
                )
                return

            restore_auto_skills(before)
            feedback = judgement.feedback

        logger.warning(
            "skill-review exhausted {} attempt(s) for {}; rolled back skills/auto",
            attempts,
            session_id,
        )
        _mark_skill_review_failed(session_id, snapshot_hash)
    except Exception:
        logger.exception("skill review failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _SKILL_REVIEW_LOCK_KEY, False)


async def maybe_trigger_skill_review(session_id: str) -> None:
    """Fork the extraction pass when the plan just finished (fail-open).

    Called by the plan-completion hook (``PlanContinuationEnforcer`` at the end
    of a turn whose plan has no incomplete rows). Independent of composition and
    of the window: a session whose plan completes after two turns extracts just
    like one that took fifty, and no conversation history rides along.

    Every gate is checked BEFORE the completion detector (which consumes the
    once-per-cycle flag) so a bail-out never marks a plan as extracted.
    """
    if not session_id or not _SKILL_REVIEW_ENABLED:
        return
    if state_register_mem.get_state(session_id, _SKILL_REVIEW_LOCK_KEY, False):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("skill review: no running event loop, skipping for {}", session_id)
        return
    if not _detect_todo_all_complete(session_id):
        return

    coro = _run_skill_review(session_id)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        return
    _BACKGROUND_REVIEW_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REVIEW_TASKS.discard)
    logger.debug("skill review scheduled for session {}", session_id)


def _build_main_session_todowrite(main_session_id: str) -> BaseTool:
    """Bound ``todowrite`` shim that always writes the MAIN session's todos.

    The fork graph runs under a derived session key (engine-state isolation), so
    the state-injected real ``todowrite`` would resolve the wrong session and
    could not see the main session at all. This shim reuses the real tool's
    ``args_schema`` and ``description`` verbatim — zero schema drift: the
    injected ``session_id`` (the DERIVED key) is dropped, every other field is
    forwarded to the same action dispatcher the real tool uses, and a future
    field the dispatcher does not accept fails fast instead of being silently
    dropped. It carries the real tool's ``todo_update`` metadata marker and
    delegates with the main session id captured.
    """
    from agent.tools.todolist.tools import build_todolist_tools
    from langchain_core.tools import StructuredTool

    real_todowrite = next(t for t in build_todolist_tools() if t.name == "todowrite")

    async def _todowrite(**kwargs: Any) -> str:
        kwargs.pop("session_id", None)
        from agent.tools.todolist.tools.todowrite import _run_todo_action

        return await _run_todo_action(session_id=main_session_id, **kwargs)

    shim = StructuredTool.from_function(
        coroutine=_todowrite,
        name=real_todowrite.name,
        description=real_todowrite.description,
        args_schema=real_todowrite.args_schema,
    )
    # ``from_function`` strips the description; restore the real tool's exact text.
    shim.description = real_todowrite.description
    shim.metadata = {"scope": "main_only", _COMPRESSION_TODO_METADATA_KEY: True}
    shim.handle_tool_error = True
    return shim


async def update_todos_from_compaction(session_id: str, discarded_messages: Sequence[Any]) -> None:
    """Reconcile the session todo list with a just-discarded compaction slice.

    Runs a dedicated background-review agent whose system prompt is
    :data:`_COMPRESSION_TODO_PROMPT` and whose tool set is exactly the
    metadata-marked ``todowrite`` shim bound to the main session.

    Engine-state isolation: the fork graph runs under the derived session key
    ``f"{session_id}::compression-todo"`` so IterationBudget / ToolGuardrails /
    ToolCallNormalize can never write the MAIN session's state keys while the
    main agent's ``awrap_model_call`` is in flight. The one deliberate exception
    is ``_COMPRESSION_TODO_LOCK_KEY`` (written on the main session as the
    re-entrancy coordinator — see its definition). The fork result messages are
    only logged: nothing from ``res["messages"]`` reaches the main graph or its
    checkpointer, and the fork itself has no checkpointer.

    Fail-open by construction: a missing todo list, an unreadable slice, or any
    agent error is logged and swallowed. The per-session lock is always
    released, and the derived session's middleware state is cleared in
    ``finally`` so per-compression engine state does not accumulate in
    ``state_register_mem``.
    """
    derived_session_id = f"{session_id}{_COMPRESSION_TODO_SESSION_SUFFIX}"
    state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, True)
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
        if not todos:
            logger.debug("compression todo update: no todos for session {}", session_id)
            return

        discarded_text = _render_discarded_text(discarded_messages)
        if not discarded_text.strip():
            logger.debug("compression todo update: empty slice for session {}", session_id)
            return

        context = _COMPRESSION_TODO_CONTEXT_TEMPLATE.replace(
            "{discarded_text}", discarded_text
        ).replace("{todos}", json.dumps(todos, ensure_ascii=False, indent=2))
        async with lane_slot(LaneType.BACKGROUND_REVIEW):
            agent = await _create_background_review_agent(
                _COMPRESSION_TODO_PROMPT,
                allowed_metadata_key=_COMPRESSION_TODO_METADATA_KEY,
                tools=[_build_main_session_todowrite(session_id)],
            )
            res = await agent.ainvoke(
                input={
                    "session_id": derived_session_id,
                    "messages": [HumanMessage(content=context)],
                }
            )
        logger.debug("compression todo update res is {}", res["messages"][-1])
    except Exception:
        logger.exception("compression todo update failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False)
        try:
            state_register_mem.clear_session(derived_session_id)
        except Exception as exc:
            logger.debug(
                "compression todo update: failed to clear derived state for {}: {}",
                derived_session_id,
                exc,
            )


def schedule_compression_todo_update(session_id: str, discarded_messages: Sequence[Any]) -> bool:
    """Gate and fire-and-forget :func:`update_todos_from_compaction`.

    Schedules only when ALL hold: the feature switch is on, the cut actually
    discarded messages, the session has a non-empty todo list, and no update is
    already in flight for this session. Returns True when a task was created.

    Never blocks and never raises: with no running event loop (the sync
    compression path) it logs at debug level and skips; a task failure cannot
    reach the compression result.
    """
    if not SUMMARIZATION["compression_todo_update_enabled"]:
        return False
    if not discarded_messages:
        return False
    if state_register_mem.get_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False):
        return False
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        if not get_todos_sync(session_id):
            return False
    except Exception:
        logger.exception("compression todo update: failed to read todos for {}", session_id)
        return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "compression todo update: no running event loop, skipping for session {}",
            session_id,
        )
        return False

    state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, True)
    coro = update_todos_from_compaction(session_id, list(discarded_messages))
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False)
        logger.debug(
            "compression todo update: event loop unavailable, skipping for session {}",
            session_id,
        )
        return False
    _COMPRESSION_TODO_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_TODO_TASKS.discard)
    logger.debug("compression todo update scheduled for session {}", session_id)
    return True
