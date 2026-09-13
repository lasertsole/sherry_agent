import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, override

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger

from config.features import SUMMARIZATION
from config.path import ROOT_DIR
from runtime import state_register_db, state_register_mem


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
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

# Plan-aware extraction prompt. Part 1 writes structured JSON knowledge via
# knowledge(action="write"); Part 2 carries the complete skill-library-update
# guidance (the former standalone skill-review prompt, merged here and removed
# as a separate constant).
_PLAN_EXTRACTION_PROMPT = """You are a knowledge extraction and skill maintenance specialist.
A plan has just been completed. Do TWO things:

## Part 1: Structured Knowledge Extraction

Extract knowledge from the completed plan below into JSON knowledge files.

### Context

{plan_context}

### Writing Style (CRITICAL)

ALL text fields must be CONCISE and DENSE:
- failure_set entries: one sentence per failure — root cause + what failed, no stack traces
- success_path entries: one sentence per step — verb + library/function name + why it works
- method: 3-5 words, kebab-case (e.g., "async-password-hashing-with-passlib")
- key_failures/key_successes (plan layer): one sentence each, max 150 chars
- reusable_patterns: one-liner code or config snippet, max 100 chars
- Strip error messages to the essential signal: "bcrypt hash mismatch" not the full traceback
- No narrative, no explanations of context, no "we decided to" — just the fact

BAD:  "We initially tried using synchronous bcrypt but it caused test failures because
       the hash function was blocking the event loop, so we switched to passlib"
GOOD: "Synchronous bcrypt blocks event loop — use passlib CryptContext for async"

### For EACH task, extract:

1. **failure_set**: What went wrong? Root cause + what failed, one sentence each.
   - Include file paths, command names, error strings (stripped to essential signal)
   - If no failures, use empty array []

2. **success_path**: Final successful approach. Ordered one-liner steps.
   - Verb + library/function name + why it works
   - If trivial, use ["straightforward implementation"]

3. **method**: 3-5 word kebab-case identifier (e.g., "async-password-hashing-with-passlib")

### For EACH WAVE, summarize (one-liner each):
- Common failure patterns across tasks
- Common success patterns across tasks

### For the PLAN as a whole (one-liner each, max 150 chars):
- Overall method/approach (3-5 words)
- Key failures (most impactful, max 5)
- Key successes (most reusable, max 5)
- Reusable patterns (one-liner code/config, max 8)

### Output (Part 1)

Write knowledge files using the knowledge tool with action='write':
- knowledge(action="write", plan_name="...", layer="task", position=0, data={...})
- knowledge(action="write", plan_name="...", layer="wave", wave_index=0, data={...})
- knowledge(action="write", plan_name="...", layer="plan", data={...})

Extract ALL layers. Be thorough — this knowledge will be queried in future sessions
to avoid repeating the same mistakes and to replicate successful approaches.

If the plan had no subagent runs and no notable patterns, still write the files
with empty failure_set and minimal success_path.

## Part 2: Skill Library Update

Be ACTIVE — most completed plans produce at least one skill update, even if
small. A pass that does nothing is a missed learning opportunity, not a
neutral outcome.

Target shape of the library: CLASS-LEVEL skills, each with a rich SKILL.md
and a `references/` directory for session-specific detail. Not a long flat
list of narrow one-session-one-skill entries. This shapes HOW you update,
not WHETHER you update.

### Signals to look for (any one warrants action):
  - User corrected your style, tone, format, legibility, or verbosity during
    the plan execution. Frustration signals like 'stop doing X', 'this is too
    verbose', 'don't format like this', 'why are you explaining', 'just give
    me the answer', 'you always do Y and I hate it', or an explicit 'remember
    this' are FIRST-CLASS skill signals. Update the relevant skill(s) to embed
    the preference so the next session starts already knowing.
  - User corrected your workflow, approach, or sequence of steps. Encode the
    correction as a pitfall or explicit step in the skill that governs that
    class of task.
  - Non-trivial technique, fix, workaround, debugging path, or tool-usage
    pattern emerged from the subagent runs. Capture it.
  - A skill that got loaded or consulted during the plan turned out to be
    wrong, missing a step, or outdated. Patch it NOW.

### Preference order — prefer the earliest action that fits:
  1. UPDATE A CURRENTLY-LOADED SKILL. Look through the plan context for
     skills the user loaded or you read. If any covers the territory of
     the new learning, PATCH that one first.
  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). If no
     loaded skill fits but an existing class-level skill does, patch it.
     Add a subsection, a pitfall, or broaden a trigger.
  3. ADD A SUPPORT FILE under an existing umbrella. Three kinds:
       - `references/<topic>.md` — session-specific detail (error
         transcripts, reproduction recipes, provider quirks) AND condensed
         knowledge banks: quoted research, API docs, external authoritative
         excerpts, or domain notes. Write it concise and for the value of
         the task, not as a full mirror of upstream docs.
       - `templates/<name>.<ext>` — starter files meant to be copied and
         modified (boilerplate configs, scaffolding, a known-good example).
       - `scripts/<name>.<ext>` — statically re-runnable actions the skill
         can invoke directly (verification scripts, fixture generators,
         deterministic probes).
     Add support files via skill_manage action=write_file with file_path
     starting 'references/', 'templates/', or 'scripts/'. The umbrella's
     SKILL.md should gain a one-line pointer to any new support file.
  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing skill covers
     the class. The name MUST be at the class level — NOT a specific PR
     number, error string, feature codename, library-alone name, or
     'fix-X / debug-Y / audit-Z-today' session artifact. If the proposed
     name only makes sense for today's task, it's wrong — fall back to
     (1), (2), or (3).

If you notice two existing skills that overlap, note it in your reply —
the background curator handles consolidation at scale.

### User-preference embedding:
When the user expressed a style/format/workflow preference during the plan,
the update belongs in the SKILL.md body, not just in memory. Memory
captures 'who the user is and what the current situation and state of your
operations are'; skills capture 'how to do this class of task for this
user'. When they complain about how you handled a task, the skill that
governs that task needs to carry the lesson.

### Do NOT capture as skills:
  - Environment-dependent failures: missing binaries, fresh-install errors,
    post-migration path mismatches, 'command not found', unconfigured
    credentials, uninstalled packages. The user can fix these — they are
    not durable rules.
  - Negative claims about tools or features ('browser tools do not work',
    'X tool is broken', 'cannot use Y from execute_code'). These harden
    into refusals the agent cites against itself for months after the
    actual problem was fixed.
  - Session-specific transient errors that resolved before the plan ended.
    If retrying worked, the lesson is the retry pattern, not the original
    failure.
  - One-off task narratives. A user asking 'summarize today's market' or
    'analyze this PR' is not a class of work that warrants a skill.

If a tool failed because of setup state, capture the FIX (install command,
config step, env var to set) under an existing setup or troubleshooting
skill — never 'this tool does not work' as a standalone constraint.

### Output (Part 2)

Use the skill_manage tool to update or create skills as described above.
Act on Part 2 only if there is real signal from the plan execution. If
genuinely nothing stands out, skip Part 2 and say 'No skill updates needed.'

{facts_section}
## Important

The knowledge you write via the knowledge tool (action='write') is stored
as JSON files in workspace/knowledge/plans/. In future sessions, you can query this
knowledge using the SAME tool with action='read' to look up failure_set,
success_path, and method from previously completed plans before starting
similar work. A condensed summary is also auto-injected into the system
prompt so you always know what previously failed/succeeded.
"""

_PLAN_EXTRACTION_LOCK_KEY = "nudge_plan_extraction_lock"
_PLAN_REF_STATE_KEY = "plan_ref"
# Subagent result text is truncated before it enters the extraction prompt so a
# verbose child transcript cannot blow up the nudge context.
_MAX_SUBAGENT_RESULT_CHARS = 24 * 1024

# Part 3 is rendered only when a pending facts range exists. Placeholders are
# substituted with ``str.replace`` (conversation text may itself contain braces,
# so ``str.format`` is unsafe here).
_FACTS_SECTION_TEMPLATE = """## Part 3: Persistent Fact Extraction

The pending conversation turns {start}..{end} below have not been fact-extracted yet.
The per-turn facts pipeline is skipped on this plan-extraction turn, so THIS single
pass must cover them: extract durable facts and write each one with the memory tool.

Extraction rules:
1. User preferences and habits -> user_prefs
2. Project conventions and environment facts -> project / environment
3. Key technical decisions and their reasons -> decisions
4. Tool usage experience -> tool_lessons
5. Never extract temporary task progress, status updates, or one-off narrative.

Write each fact as:
  memory(action="fact_add", target="<category>", content="<one concise fact>")
Valid categories: environment, project, decisions, user_prefs, tool_lessons.
Deduplicate against facts already stored; if nothing durable stands out, say
'No facts to save.' and continue.

<conversation turns="{start}-{end}">
{conversation}
</conversation>"""

# Post-compression todo reconciliation. The Summarization middleware fires this
# fire-and-forget after a compaction that actually discarded messages and only
# when the session has a non-empty todo list. The runner never blocks or breaks
# compression (fail-open), and a per-session lock prevents overlapping runs.
#
# The lock is written on the MAIN session on purpose: it is the cross-path
# re-entrancy coordinator, so later compressions of the same session observe it.
# Everything else the fork writes lives under the derived session key below.
_COMPRESSION_TODO_LOCK_KEY = "compression_todo_update_lock"

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


class _NudgeLimitTool(AgentMiddleware):
    """Tool gate for nudge sub-agents.

    With no ``allowed_metadata_key`` (the default), a tool passes only when its
    metadata carries ``nudge: True`` — the legacy rule every existing memory /
    plan-extraction nudge agent relies on. With an explicit metadata key, a
    tool passes only when ``tool.metadata[key] is True`` (strict identity), so
    the compression todo fork can admit exactly the metadata-marked
    ``todowrite`` while every unmarked tool is rejected.
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
        logger.debug("{} awrap_tool_call hook fired", type(self).__name__)
        tool_name: str = request.tool_call.get("name", "unknown")

        if not self._is_allowed(request.tool):
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] is not allowed during nudge phase. "
                    "Execution has been skipped. Please reconsider your approach."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return await handler(request)

    @staticmethod
    def _is_nudge_allowed(tool: Any) -> bool:
        """Legacy metadata rule: only tools tagged ``nudge: True`` pass."""
        if tool is not None and isinstance(getattr(tool, "metadata", None), dict):
            return bool(tool.metadata.get("nudge", False))
        return False

    def _is_allowed(self, tool: Any) -> bool:
        if self._allowed_metadata_key is None:
            return self._is_nudge_allowed(tool)
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            return metadata.get(self._allowed_metadata_key) is True
        return False


class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""

    session_id: str


async def _create_nudge_agent(
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
            _NudgeLimitTool(allowed_metadata_key=allowed_metadata_key),
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=list(agent_tools),
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


def _read_ledger_entries(plan_path: str, plan_name: str) -> list[dict]:
    """Read the start-work ledger, keeping entries for this plan (fail-open)."""
    entries: list[dict] = []
    ledger_path = ROOT_DIR / ".omo" / "start-work" / "ledger.jsonl"
    try:
        if not ledger_path.is_file():
            return entries
        with ledger_path.open("r", encoding="utf-8") as ledger_file:
            for line in ledger_file:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("plan") in (plan_path, plan_name):
                    entries.append(entry)
    except OSError:
        logger.exception("plan extraction: failed to read ledger {}", ledger_path)
    return entries


def _read_subagent_runs(session_id: str) -> list[dict]:
    """Collect the trimmed subagent run records for this session (fail-open)."""
    runs_out: list[dict] = []
    try:
        from agent.tools.subagent.registry import list_runs_for_requester

        runs = list_runs_for_requester(f"agent:main:session:{session_id}")
    except Exception:
        logger.exception("plan extraction: failed to list subagent runs for {}", session_id)
        return runs_out

    for run in runs:
        outcome = run.execution.outcome
        result_text = run.completion.result_text or ""
        runs_out.append(
            {
                "task_name": run.task_name,
                "task": run.task,
                "result_text": result_text[:_MAX_SUBAGENT_RESULT_CHARS],
                "outcome": outcome.status.value if outcome else "unknown",
                "error": outcome.error if outcome else None,
            }
        )
    return runs_out


def _build_plan_context(session_id: str) -> dict[str, Any]:
    """Build the structured context handed to one plan-extraction pass.

    Reads:
    1. Plan file (``plan_ref`` from session state, else first non-empty todo ref)
    2. Todos (todos.db)
    3. Ledger (``.omo/start-work/ledger.jsonl``)
    4. Subagent runs (registry queries)

    Returns an empty dict when there is no todo list to extract from; every
    downstream read is fail-open so a missing plan file or ledger never breaks
    the extraction pass.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("plan extraction: failed to read todos for {}", session_id)
        return {}
    if not todos:
        return {}

    plan_path = _resolve_plan_ref(session_id, todos)
    plan_name = ""
    plan_content = ""
    if plan_path:
        candidate = Path(plan_path)
        if not candidate.is_absolute():
            candidate = ROOT_DIR / candidate
        try:
            if candidate.is_file():
                plan_content = candidate.read_text(encoding="utf-8")
                plan_name = candidate.stem
        except OSError:
            logger.exception("plan extraction: failed to read plan file {}", candidate)
    if not plan_name:
        plan_name = f"session-{session_id[:8]}"

    return {
        "plan_name": plan_name,
        "plan_path": plan_path,
        "plan_content": plan_content,
        "todos": todos,
        "ledger_entries": _read_ledger_entries(plan_path, plan_name),
        "subagent_runs": _read_subagent_runs(session_id),
    }


async def _nudge_memory(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    state_register_mem.set_state(session_id, "nudge_review_memory_lock", True)
    try:
        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(
            input={
                "session_id": session_id,
                "messages": [*messages, HumanMessage(content=_MEMORY_REVIEW_PROMPT)],
            }
        )
        logger.debug("nudge memory res is {}", res["messages"][-1])
    finally:
        state_register_mem.set_state(session_id, "nudge_review_memory_lock", False)


def _render_facts_section(pending: dict[str, Any]) -> str:
    """Render the Part 3 block for the fetched pending range, or '' when none."""
    if not pending:
        return ""
    return (
        _FACTS_SECTION_TEMPLATE.replace("{start}", str(pending["start"]))
        .replace("{end}", str(pending["end"]))
        .replace("{conversation}", pending["conversation"])
    )


def _fetch_pending_facts(session_id: str) -> dict[str, Any]:
    """Fetch the not-yet-consumed facts interval and format it for the prompt.

    Returns ``{"start": int, "end": int, "conversation": str}`` or ``{}`` when
    nothing is pending, the range has no persisted conversation rows, or any
    read fails. Callers may advance the consumed watermark only when this
    returns a range — the facts must have been handed to the extraction pass.
    """
    try:
        from context_engine.facts.cursor import get_pending
        from context_engine.facts.queue import _format_range
        from context_engine.store.core import get_turns_by_turn_num_scope

        start, end = get_pending(session_id)
        if end <= 0 or end < start:
            return {}
        middle = (start + end) // 2
        half_scope = max(1, end - start + 1)
        rows = get_turns_by_turn_num_scope(
            session_id, target_turn_num=middle, half_scope=half_scope, only_eligible=False
        )
        rows = [row for row in rows if start <= row.get("turn_num", 0) <= end]
        conversation = _format_range(rows)
        if not conversation.strip():
            return {}
        return {"start": start, "end": end, "conversation": conversation}
    except Exception:
        logger.exception("plan extraction: failed to read pending facts for {}", session_id)
        return {}


def _advance_facts_consumed(session_id: str, end: int) -> None:
    """Advance the facts consumed watermark after the injected range was processed.

    Only called after a successful extraction pass whose prompt carried the
    pending conversation; a failure leaves the watermark untouched so the range
    replays later (at-least-once, mirroring ``facts.queue.process_pending``).
    """
    try:
        from context_engine.facts import cursor as facts_cursor

        facts_cursor.advance_consumed(session_id, end)
    except Exception:
        logger.exception("plan extraction: failed to advance facts cursor for {}", session_id)


async def _nudge_plan_extraction(
    session_id: str, system_prompt: str, messages: list[BaseMessage]
) -> None:
    """Plan-aware knowledge extraction + skill library update + facts absorption.

    Triggered when all todos are complete. Builds plan context (plan file +
    todos + ledger + subagent runs) and launches a nudge agent with the
    rendered ``_PLAN_EXTRACTION_PROMPT`` (Part 1: JSON knowledge extraction via
    knowledge(action="write"); Part 2: skill library update via skill_manage;
    Part 3: persistent facts via memory(action="fact_add"), rendered only when a
    pending facts range exists). All three tools carry ``nudge: True`` metadata
    and pass ``_NudgeLimitTool``.

    Decision #2: this single LLM pass covers the pending facts interval that the
    per-turn pipeline would otherwise have processed. The consumed watermark
    advances ONLY after a successful ``ainvoke`` AND only when the pending range
    was actually injected — so a skip, an empty range, or any failure leaves the
    range pending for replay. Fail-open: errors are logged and swallowed, never
    propagated into the turn's ``after_agent`` hook.
    """
    state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, True)
    try:
        context = _build_plan_context(session_id)
        if not context:
            logger.debug("plan extraction: no plan context for session {}", session_id)
            return

        pending = _fetch_pending_facts(session_id)
        context_str = json.dumps(context, ensure_ascii=False, indent=2)
        prompt = _PLAN_EXTRACTION_PROMPT.replace("{plan_context}", context_str)
        prompt = prompt.replace("{facts_section}", _render_facts_section(pending))

        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(
            input={
                "session_id": session_id,
                "messages": [*messages, HumanMessage(content=prompt)],
            }
        )
        logger.debug("plan extraction res is {}", res["messages"][-1])
        if pending:
            _advance_facts_consumed(session_id, pending["end"])
    except Exception:
        logger.exception("plan extraction failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)


def _build_main_session_todowrite(main_session_id: str) -> BaseTool:
    """Bound ``todowrite`` shim that always writes the MAIN session's todos.

    The fork graph runs under a derived session key (engine-state isolation), so
    the state-injected real ``todowrite`` would resolve the wrong session and
    could not see the main session at all. This shim reuses the real tool's
    ``args_schema`` and ``description`` verbatim — zero schema drift: the
    injected ``session_id`` (the DERIVED key) is dropped, every other field is
    forwarded to the service, and a future field the service does not accept
    fails fast instead of being silently dropped. It carries the real tool's
    ``todo_update`` metadata marker and delegates to the real service with the
    main session id captured.
    """
    from agent.tools.todolist.tools import build_todolist_tools
    from langchain_core.tools import StructuredTool

    real_todowrite = next(t for t in build_todolist_tools() if t.name == "todowrite")

    async def _todowrite(**kwargs: Any) -> str:
        kwargs.pop("session_id", None)
        from agent.tools.todolist import service

        result = await service.TodoService.update_todos(main_session_id, **kwargs)
        return json.dumps(result, ensure_ascii=False, indent=2)

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

    Runs a dedicated nudge agent whose system prompt is
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

        from agent.middlewares.memory_flush import _render_discarded_text

        discarded_text = _render_discarded_text(discarded_messages)
        if not discarded_text.strip():
            logger.debug("compression todo update: empty slice for session {}", session_id)
            return

        context = _COMPRESSION_TODO_CONTEXT_TEMPLATE.replace(
            "{discarded_text}", discarded_text
        ).replace("{todos}", json.dumps(todos, ensure_ascii=False, indent=2))
        agent = await _create_nudge_agent(
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
