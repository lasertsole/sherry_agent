import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger

from config.features import NUDGE, SUMMARIZATION
from config.path import resolve_plan_path
from pub.func import sanitize_tool_use_result_pairing
from runtime import StateKey, state_register_db, state_register_mem
from runtime.lane import LaneType, lane_slot


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
    "3. Did the work surface a broad pitfall or convention that is NOT bound to a "
    "specific file/module — something any future task in this environment could hit? "
    "(target: 'facts')\n\n"
    "Routing: user preferences and traits go to 'user'; general agent-side notes about "
    "this environment/project go to 'memory'; broad cross-plan pitfalls and conventions "
    "go to 'facts'.\n\n"
    "Current FACTS.md content (target 'facts', limit {facts_limit} chars):\n"
    "<facts>\n{facts_block}\n</facts>\n\n"
    "Maintain FACTS.md like an editor, not an appender: merge entries describing the "
    "same pitfall, replace outdated ones, and prefer saving nothing over bloating the "
    "file (prefer nothing over noise).\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)


def _render_facts_context() -> tuple[str, str]:
    """Return ``(current FACTS.md content, formatted char limit)`` for nudge prompts.

    Reads the live content (not the frozen system-prompt snapshot) so a
    compression-time nudge hands the model what the file holds right now.
    """
    from agent.tools.memory import memory_store

    content = memory_store.format_live_content("facts")
    return content or "(FACTS.md is empty)", f"{memory_store.facts_char_limit:,}"


def _render_prompt_facts(template: str) -> str:
    """Substitute the FACTS placeholders (injected content may contain braces)."""
    facts_block, facts_limit = _render_facts_context()
    return template.replace("{facts_limit}", facts_limit).replace("{facts_block}", facts_block)


# Plan-aware extraction prompt. Part 1 writes structured JSON knowledge via
# knowledge(action="write"); Part 2 carries the complete skill-library-update
# guidance (the former standalone skill-review prompt, merged here and removed
# as a separate constant) including the module-bound `<module>-notes`
# carry-forward; Part 3 routes broad, module-independent pitfalls to the
# FACTS.md memory target.
_PLAN_EXTRACTION_PROMPT = """You are a knowledge extraction and skill maintenance specialist.
A plan has just been completed. Do THREE things:

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
  2. UPDATE AN EXISTING UMBRELLA (via skill_list + skill_view). If no
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

### Frontmatter standard (the ONLY text the index shows)

`<available_skills>` renders a skill's frontmatter, and those few lines are the
model's entire basis for deciding whether to load the skill — the body is read
only AFTER loading. So the fields are the trigger mechanism, not metadata:

- `name` — class-level kebab-case identifier.
- `description` — one-line capability summary (what the skill does).
- `when_to_use` — **REQUIRED** on every skill you create or patch: the specific
  scenarios, user intents, or triggers that should make the agent load this
  skill. Not "when needed" — name the situation. Example: "When implementing
  authentication that needs non-blocking bcrypt/scrypt verification, or when
  synchronous hashing blocks the event loop."
- `when_not_to_use` — optional but valuable: exclusions, alternatives, or
  conditions where the approach does not apply. Example: "For simple CLI
  scripts where blocking is acceptable, or when the project already uses
  argon2."

Keep each field short (~60 tokens for `description`, ~50 for the others — CJK
characters cost about twice as many tokens per character). When patching a
skill that lacks `when_to_use`, add it in the same patch.

If you notice two existing skills that overlap, note it in your reply —
the background curator handles consolidation at scale.

### Module-bound carry-forward (`<module>-notes` skills)

The class-level umbrellas above cover reusable classes of work. Module-bound
pitfalls need a narrower channel: a lesson that binds to a SPECIFIC file /
module / test and whose module can foreseeably be modified again.

- Judgment: the lesson names a concrete file, module, or test (e.g.
  `auth/token.py`, `test_auth_17`) and whoever touches that module next would
  hit the same pitfall. If the module is finished for good, leave the detail
  to Part 1 knowledge instead.
- Output: write that module's notes skill with
  `skill_manage(action="create", name="<module>-notes", content=...)` —
  naming convention `<module>-notes` (e.g. `auth-module-notes`). If a skill
  with that exact name already exists, do NOT create a duplicate: append the
  new rows with
  `skill_manage(action="patch", name="<module>-notes", old_string=..., new_string=...)`
  (use action="edit" only for a full rewrite).
- Content format: a short, actionable list grouped by module — "symptom →
  cause → avoidance action", one line per lesson.
- Keep it lean (prefer nothing over noise): one-off task details stay in
  Part 1 knowledge; only carry forward what is genuinely reusable by the next
  plan that touches the same module.

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

## Part 3: Broad Pitfalls → FACTS.md

Not every durable lesson is bound to a module or belongs to a skill class.
Broad pitfalls and conventions — environment/toolchain constraints, recurring
cross-module gotchas, ways of working that any future task could hit — are
kept in FACTS.md, the third memory file, which is injected into every system
prompt.

Judge each candidate against the routing rules:
- Bound to a specific file / module / test → Part 2 `<module>-notes` skill.
- About the user (preferences, traits, expectations) → USER.
- A reusable class of work or technique → Part 2 skill.
- Broad, cross-plan pitfall or convention (not bound to one module) → FACTS.md.
- General agent-side environment/project note → MEMORY.

To write: call the memory tool with target="facts" (actions: add / replace /
remove). Keep each entry to one short line — symptom plus avoidance.
A transient setup failure the user can fix is not durable (see the Do-NOT
list); a recurring constraint of this environment is.

Current FACTS.md content (target 'facts', limit {facts_limit} chars):
<facts>
{facts_block}
</facts>

Maintain it like an editor: merge entries describing the same pitfall, replace
outdated ones, and prefer saving nothing over bloating the file (prefer
nothing over noise).

## Important

The knowledge you write via the knowledge tool (action='write') is stored
as JSON files in workspace/knowledge/plans/. In future sessions, you can query this
knowledge using the SAME tool with action='read' to look up failure_set,
success_path, and method from previously completed plans before starting
similar work. A condensed summary is also auto-injected into the system
prompt so you always know what previously failed/succeeded.
"""

_PLAN_EXTRACTION_LOCK_KEY = StateKey.NUDGE_PLAN_EXTRACTION_LOCK
_PLAN_REF_STATE_KEY = StateKey.PLAN_REF
# Subagent result text is truncated before it enters the extraction prompt so a
# verbose child transcript cannot blow up the nudge context.
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
# Compression-time nudge scheduling (memory review + plan extraction)
#
# Both triggers are driven by the compression pipeline: the Summarization
# middleware calls
# ``schedule_memory_review`` on every compression that actually discards
# messages. The memory review is dispatched on **every** compression (a
# compaction discards content, so the review must never be skipped), and plan
# extraction is evaluated at compression time. The single-fire flag semantics
# are unchanged: ``nudge_plan_extraction_fired`` still guarantees one
# extraction per completion cycle and resets whenever the list is not
# all-complete.
#
# The in-flight locks live in ``state_register_mem``. Dispatch is
# fire-and-forget: compression runs inside ``wrap_model_call`` and must never
# block on a nudge agent's LLM call. While a nudge lock is held the
# compression dispatches nothing — nothing is queued.
# ---------------------------------------------------------------------------

_NUDGE_MEMORY_LOCK_KEY = StateKey.NUDGE_REVIEW_MEMORY_LOCK
_PLAN_EXTRACTION_FIRED_KEY = StateKey.NUDGE_PLAN_EXTRACTION_FIRED
#: Fingerprint of the plan content the last SUCCESSFUL extraction consumed.
_PLAN_SNAPSHOT_HASH_KEY = StateKey.NUDGE_PLAN_SNAPSHOT_HASH
_PLAN_EXTRACTION_ENABLED = NUDGE["plan_extraction_enabled"]

# Keeps scheduled nudge coroutines referenced until they complete (asyncio
# holds only weak references to tasks).
_COMPRESSION_NUDGE_TASKS: set[asyncio.Task[None]] = set()


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
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
    if not already_fired:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, True)
        return True

    stored = state_register_db.get_state(session_id, _PLAN_SNAPSHOT_HASH_KEY, "") or ""
    return _plan_snapshot_hash(todos) != stored


def _nudges_locked(session_id: str) -> bool:
    """True while a memory-review or plan-extraction nudge is in flight."""
    return bool(
        state_register_mem.get_state(session_id, _NUDGE_MEMORY_LOCK_KEY, False)
        or state_register_mem.get_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)
    )


async def _run_memory_review(
    session_id: str,
    messages_to_summarize: list[BaseMessage],
) -> None:
    """Run the compression-scoped memory review under the NUDGE lane (fail-open).

    Compaction carries the MEMORY review only, and it is handed the messages
    about to be DISCARDED — those are the ones whose facts are about to be lost,
    and they are a fraction of the whole conversation. Plan extraction lives on
    the plan-completion path (:func:`maybe_trigger_plan_extraction`) — it needs
    no message history and must not wait for a window to fill.
    """
    try:
        # Call-time import keeps the patch seam on
        # ``agent.middlewares.system_prompt.core._get_and_reload_system_prompt``
        # effective (same contract as before the package move).
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt

        system_prompt = _get_and_reload_system_prompt(session_id)
        await _nudge_memory(session_id, system_prompt, list(messages_to_summarize))
    except Exception:
        logger.exception("memory review dispatch failed (fail-open) for {}", session_id)


def schedule_memory_review(session_id: str, messages_to_summarize: Sequence[BaseMessage]) -> bool:
    """Dispatch the compression-time memory review.

    Called by the Summarization middleware once per compression that actually
    discards messages, with the messages being discarded. Returns True when a
    task was created.

    Plan extraction does not ride this path — it forks on plan completion (see
    :func:`maybe_trigger_plan_extraction`). While a nudge is in flight, dispatch
    is skipped entirely — nothing is queued. With no running event loop (the
    sync compression path outside an async caller) scheduling is skipped
    entirely.

    A session may turn the review OFF (预设-中间件 → Summarization → nudge, the
    one option a REQUIRED middleware exposes; the service only accepts it beside
    an enabled ``memory`` tool, which is what the review writes with).
    """
    from agent.middlewares.agent_switch import middleware_option

    if not middleware_option(session_id, "Summarization", "nudge"):
        logger.debug("memory review: disabled by the session config ({})", session_id)
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "memory review: no running event loop, skipping for session {}",
            session_id,
        )
        return False

    if _nudges_locked(session_id):
        return False

    coro = _run_memory_review(session_id, list(messages_to_summarize))
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        return False
    _COMPRESSION_NUDGE_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_NUDGE_TASKS.discard)
    logger.debug("memory review scheduled for session {}", session_id)
    return True


class _NudgeLimitTool(AgentMiddleware):
    """Tool gate for nudge sub-agents.

    With no ``allowed_metadata_key`` (the default), a tool passes only when its
    metadata carries ``nudge: True`` — the memory review's rule (``memory``,
    ``knowledge``, ``question``, plan mode). With an explicit metadata key, a
    tool passes only when ``tool.metadata[key] is True`` (strict identity), so
    the compression todo fork can admit exactly the metadata-marked
    ``todowrite`` while every unmarked tool is rejected.

    The skills toolset is deliberately UNTAGGED: skill extraction is the
    plan-completion fork's job, and that fork binds its own explicit four-tool
    face (``_extraction_tool_face``) instead of riding this gate.
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


def _session_tool_face(session_id: str) -> list[BaseTool]:
    """The tools the session's turn actually binds, in the same order.

    The provider's prefix cache is keyed on the request prefix, and the tool
    block is part of it — a review that binds a different tool list (or a
    different order) misses the cache at the very first token. The selection
    middleware narrows the face per session (预设-工具), so the review mirrors it
    instead of assuming the full list.
    """
    from agent import get_agent_tools
    from agent.middlewares.tool_selection.core import enabled_tool_names

    enabled = enabled_tool_names(session_id)
    tools = list(get_agent_tools())
    if enabled is None:
        return tools
    return [tool for tool in tools if getattr(tool, "name", "") in enabled]


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
    if override:
        return build_main_llm_for_profile(
            provider=override.get("provider"),
            model=override["model"],
            api_key=override.get("api_key"),
            base_url=override.get("base_url"),
            temperature=temperature,
            **thinking_kwargs,
        )
    return build_main_llm(temperature=temperature, **thinking_kwargs)


async def _create_memory_review_agent(session_id: str, system_prompt: str):
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
    system prompt, the session's own tool face and order. The nudge gate still
    applies — the review may only EXECUTE the tools carrying ``nudge: True``.
    """
    from agent.middlewares import ToolCallNormalize, ToolGuardrails

    return create_agent(
        model=_build_session_model(session_id),
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            _NudgeLimitTool(),
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=_session_tool_face(session_id),
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
        logger.exception("plan extraction: failed to read todos for {}", session_id)
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
            logger.exception("plan extraction: failed to read plan file {}", resolved)
    if not plan_name:
        # Full-id hash: ids sharing an 8-char prefix must not share a fallback dir.
        from agent.tools.todolist.knowledge.identity import fallback_plan_name

        plan_name = fallback_plan_name(session_id)
    plan_path = str(resolved) if resolved is not None else plan_ref

    return {
        "plan_name": plan_name,
        "plan_path": plan_path,
        "plan_content": plan_content,
        "todos": todos,
        "subagent_runs": _read_subagent_runs(session_id),
    }


def _memory_review_messages(session_id: str, messages: list[BaseMessage]) -> list[BaseMessage]:
    """The messages the review sends, per ``NUDGE["memory_review_mode"]``.

    ``cache_warm`` (default): the discarded slice VERBATIM — it is the main
    model's own prefix, so the provider serves it from cache and the review sees
    the real text.

    ``digest``: the folded view (``digest_history``), for a deployment whose
    provider does not cache prefixes well or whose review model has a small
    window — the fold bounds the input regardless of provider behaviour.
    """
    if str(NUDGE.get("memory_review_mode", "cache_warm")) == "digest":
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


async def _nudge_memory(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    """Compression-time memory review: the main model, on the turn's own prefix.

    The caller hands it the DISCARDED slice, which — because the cut is a prefix
    cut — is exactly the part of the request immediately following the system
    prompt. Sending it verbatim with the same model, system prompt and tools
    turns the whole thing into a prefix-cache hit; only the review instruction
    is fresh input. ``NUDGE["memory_review_mode"] = "digest"`` switches to the
    folded view for deployments whose provider caches poorly.

    NUDGE lane is event-loop-bound: acquire only on the main loop. Nudges are
    dispatched exclusively from the async compression path, so no run_async()
    worker loop ever touches this semaphore.
    """
    state_register_mem.set_state(session_id, _NUDGE_MEMORY_LOCK_KEY, True)
    try:
        prepared = _memory_review_messages(session_id, messages)
        sanitized = sanitize_tool_use_result_pairing(prepared)
        async with lane_slot(LaneType.NUDGE):
            _agent = await _create_memory_review_agent(session_id, system_prompt)
            res = await _agent.ainvoke(
                input={
                    "session_id": session_id,
                    "messages": [
                        *sanitized,
                        HumanMessage(content=_render_prompt_facts(_MEMORY_REVIEW_PROMPT)),
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
        logger.debug("nudge memory res is {}", reply)
    finally:
        state_register_mem.set_state(session_id, _NUDGE_MEMORY_LOCK_KEY, False)


async def _create_plan_extraction_agent(system_prompt: str):
    """The extraction fork's agent: auxiliary model, an EXPLICIT tool binding.

    NOT ``_create_nudge_agent``: the memory review keeps the main model (it reads
    a conversation), while this pass consumes structured data (todos + subagent
    runs + the plan file) and runs a template-driven extraction — the same shape
    the step / completion judges already run on the auxiliary model, at a
    fraction of the cost and with no chance of crowding the main window.
    The system prompt is kept: the skills index in it decides patch-vs-create.

    The tool face is bound to exactly the tools the extraction prompt drives
    (knowledge write, the skills trio, and ``memory`` for the FACTS.md target) —
    the bound list IS the barrier, so nothing else is even callable. The skills
    toolset deliberately carries NO ``nudge`` marker: the memory review
    (``_NudgeLimitTool()``, the tag-based gate) must not extract skills — the
    experience extraction is this pass's whole job.
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
        tools=_extraction_tool_face(),
    )


#: The tools the extraction prompt drives, by name — knowledge write, the
#: skills-index read / skill read / skill write trio, and ``memory`` (its Part 3
#: routes broad pitfalls to the FACTS.md target through that tool).
_EXTRACTION_TOOL_NAMES = ("knowledge", "skill_list", "skill_view", "skill_manage", "memory")


def _extraction_tool_face() -> list[BaseTool]:
    """The extraction fork's bound tools, in the main face's order."""
    from agent import get_agent_tools

    return [
        tool for tool in get_agent_tools() if getattr(tool, "name", "") in _EXTRACTION_TOOL_NAMES
    ]


async def _run_plan_extraction(session_id: str) -> None:
    """One independent extraction pass: instruction + plan context, no history.

    Plan-aware knowledge extraction + skill library update, driven by the
    rendered ``_PLAN_EXTRACTION_PROMPT`` (Part 1: JSON knowledge extraction via
    knowledge(action="write"); Part 2: skill library update via skill_manage,
    including ``<module>-notes`` carry-forward; Part 3: broad pitfalls to the
    ``facts`` memory target).

    The snapshot fingerprint is stored only AFTER a successful pass (and for the
    very todos this pass consumed), so a failed or raced extraction is retried on
    the next completion turn instead of being silently marked done.

    Fail-open: errors are logged and swallowed, never propagated into the turn's
    ``after_agent`` hook.
    """
    state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, True)
    try:
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt

        context = _build_plan_context(session_id)
        if not context:
            logger.debug("plan extraction: no plan context for session {}", session_id)
            return

        system_prompt = _get_and_reload_system_prompt(session_id)
        context_str = json.dumps(context, ensure_ascii=False, indent=2)
        prompt = _render_prompt_facts(
            _PLAN_EXTRACTION_PROMPT.replace("{plan_context}", context_str)
        )

        async with lane_slot(LaneType.NUDGE):
            _agent = await _create_plan_extraction_agent(system_prompt)
            res = await _agent.ainvoke(
                input={
                    "session_id": session_id,
                    "messages": [HumanMessage(content=prompt)],
                }
            )
        logger.debug("plan extraction res is {}", res["messages"][-1])

        state_register_db.set_state(
            session_id,
            _PLAN_SNAPSHOT_HASH_KEY,
            _plan_snapshot_hash(list(context.get("todos") or [])),
        )
    except Exception:
        logger.exception("plan extraction failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)


async def maybe_trigger_plan_extraction(session_id: str) -> None:
    """Fork the extraction pass when the plan just finished (fail-open).

    Called by the plan-completion hook (``PlanContinuationEnforcer`` at the end
    of a turn whose plan has no incomplete rows). Independent of composition and
    of the window: a session whose plan completes after two turns extracts just
    like one that took fifty, and no conversation history rides along.

    Every gate is checked BEFORE the completion detector (which consumes the
    once-per-cycle flag) so a bail-out never marks a plan as extracted.
    """
    if not session_id or not _PLAN_EXTRACTION_ENABLED:
        return
    from agent.middlewares.agent_switch import middleware_option

    if not middleware_option(session_id, "Summarization", "nudge"):
        logger.debug("plan extraction: disabled by the session config ({})", session_id)
        return
    if state_register_mem.get_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("plan extraction: no running event loop, skipping for {}", session_id)
        return
    if not _detect_todo_all_complete(session_id):
        return

    coro = _run_plan_extraction(session_id)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        return
    _COMPRESSION_NUDGE_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_NUDGE_TASKS.discard)
    logger.debug("plan extraction scheduled for session {}", session_id)


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

        from agent.middlewares.summarization.memory_flush import _render_discarded_text

        discarded_text = _render_discarded_text(discarded_messages)
        if not discarded_text.strip():
            logger.debug("compression todo update: empty slice for session {}", session_id)
            return

        context = _COMPRESSION_TODO_CONTEXT_TEMPLATE.replace(
            "{discarded_text}", discarded_text
        ).replace("{todos}", json.dumps(todos, ensure_ascii=False, indent=2))
        async with lane_slot(LaneType.NUDGE):
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
