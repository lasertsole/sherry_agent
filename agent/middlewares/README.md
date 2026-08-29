# EMA Agent Middleware System

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

The middleware layer of the EMA AI Agent: eight `AgentMiddleware` components that shape every model call and tool call — context engineering, multimodal input handling, iteration budgets, tool guardrails, transcript repair, heartbeat staleness detection, human-in-the-loop approvals, and context summarization — plus an output repetition guard used by the worker agents.

> Every claim in this document was verified against the source code (installed `langchain 1.3.9`, `agent/core.py`, `agent/tools/subagent/spawn/core.py`, and the modules under `agent/middlewares/`). Class names, file names, defaults, and state keys below all exist in code.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Middleware Chain](#middleware-chain)
- [Middleware Reference](#middleware-reference)
  - [ContextEngineHook](#contextenginehook)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [Summarization](#summarization)
  - [OutputRepetitionGuard & RepetitionGuardWrapper](#outputrepetitionguard--repetitionguardwrapper)
- [Shared State System](#shared-state-system)
- [Configuration](#configuration)
- [Lifecycle & Data Flow](#lifecycle--data-flow)
- [Writing a Custom Middleware](#writing-a-custom-middleware)
- [Appendix](#appendix)

---

## Architecture Overview

### What Is a Middleware?

A middleware extends `langchain.agents.middleware.AgentMiddleware` and hooks into the agent loop at well-defined points. The system uses four hook families (each available in sync and async form):

| Hook family | Sync | Async | Runs |
|---|---|---|---|
| Before / after agent | `before_agent` / `after_agent` | `abefore_agent` / `aafter_agent` | Once per conversational turn, around the whole model–tool loop |
| Before / after model | `before_model` / `after_model` | `abefore_model` / `aafter_model` | Around each individual model request |
| Model-call wrap | `wrap_model_call` | `awrap_model_call` | Intercepts the model request itself (modify messages / system prompt, short-circuit the LLM) |
| Tool-call wrap | `wrap_tool_call` | `awrap_tool_call` | Intercepts each tool execution |

### Hook Ordering Semantics

Verified against the installed `langchain 1.3.9` source (`agents/middleware/factory.py` and `agents/middleware/types.py`):

- `before_agent` hooks run in **list order** — the first registered middleware runs first.
- `after_agent` hooks run in **reverse list order** — the last registered middleware's `after_agent` runs first (it is the exit-node chain in the compiled graph).
- `wrap_model_call` / `wrap_tool_call` compose with the **first middleware in the list as the outermost layer** and the last one as the innermost (closest to the LLM / tool).

> ⚠️ Older middleware frameworks used `awrap_before_agent`-style hooks; LangChain 1.3 does not. The async forms are direct prefixes: `abefore_agent`, `abefore_model`, `aafter_model`, `aafter_agent`, `awrap_model_call`, `awrap_tool_call`.

### State Persistence

Middleware state does **not** live in the LangGraph state (with a few framework-managed exceptions). Cross-call state is kept in session-scoped runtime registers:

- `state_register_mem` (`StateRegisterMeM`) — in-memory dict, volatile (cleared on process restart).
- `state_register_db` (`StateRegisterDB`) — SQLite-backed (`src/data/state_register.db`), survives restarts.
- `timer_call_register` (`TimerCallRegister`) — background countdown timers (1–60 minutes) used by `HeartbeatStaleness`.

Details in [Shared State System](#shared-state-system).

---

## Middleware Chain

### Main Agent (`agent/core.py`)

```python
middleware = [
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        trigger=[("tokens", int(main_llm_max_tokens / 2))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# the compiled graph is then wrapped:
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
```

`main_llm_max_tokens` is read from the `MAIN_LLM_MAX_TOKEN` environment variable (`models/LLMs/main_llm.py`), so the main-agent summarization trigger sits at roughly half of the main model's context window.

> **Note:** `OutputRepetitionGuard` is **not** registered as main-agent middleware. For the main agent, its behavior is provided by the `RepetitionGuardWrapper` that wraps the compiled graph — see [OutputRepetitionGuard & RepetitionGuardWrapper](#outputrepetitionguard--repetitionguardwrapper).

### Worker / Subagent Pipeline (`agent/tools/subagent/spawn/core.py`)

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        trigger=[("messages", 40), ("tokens", 30000)],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# the child graph is wrapped the same way:
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

Differences vs the main agent:

- Summarization triggers on message count (40) **or** tokens (30 000) instead of half the context window.
- A tighter iteration budget (60 instead of 90).
- No `ContextEngineHook`, no `MultimodalProcessor`, no `HumanInTheLoop`.
- `OutputRepetitionGuard` runs as a real middleware here.
- When a child session finishes, the spawn code deletes the six `OutputRepetitionGuard` state keys (`SESSION_STATE_KEYS`) from `state_register_mem` in its `finally` block.

### Effective Per-Turn Order (main agent)

| Phase | Order |
|---|---|
| `before_agent` (list order) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `wrap_model_call` (outermost → innermost) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization (Summarization sits closest to the LLM) |
| `after_agent` (reverse order) | Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

Only middlewares that implement a given hook participate in that phase; the table shows where each would run if it did.

---

## Middleware Reference

### ContextEngineHook

**Module:** `agent/middlewares/context_engine/core.py` · **Class:** `ContextEngineHook(AgentMiddleware)`
**Hooks:** `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`, `after_agent` / `aafter_agent`

First in the list, therefore the outermost wrap layer.

**`wrap_model_call` — system-prompt injection**

1. Look up `system_prompt` in `state_register_mem`.
2. Fall back to `state_register_db`; if still missing, rebuild via `workspace.prompt_builder.build_system_prompt(session_id)`.
3. Inject with `request.override(system_message=...)` and cache the prompt back to `state_register_mem`.

**`wrap_tool_call` — skill-review accounting**

Increments `nudge_review_skill_count` in `state_register_db` for every tool call, unless the tool's metadata sets `nudge: true` (self-exempting nudge/limit tools).

**`after_agent` / `aafter_agent` — turn finalization**

1. Increment `nudge_review_memory_count` in `state_register_db`.
2. If a counter reaches its threshold — `_NUDGE_MEMORY_THRESHOLD = 10` turns, `_NUDGE_SKILL_THRESHOLD = 10` tool calls — launch the corresponding **nudge sub-agent** (below) under the per-session locks `nudge_review_memory_lock` / `nudge_review_skill_lock` in `state_register_mem`. While a lock is held, `after_agent` skips the nudge decision (the counter still increments).
3. Persist the last turn to MesMemory: `slice_last_turn` → `sanitize_tool_use_result_pairing` → `add_messages(session_id, messages)` (SQLite).
4. Sync `after_agent` runs sub-agents via `run_async`; `aafter_agent` runs persistence and nudges concurrently through `asyncio.gather`.

**Nudge sub-agents** (`context_engine/nudge.py`): separate `create_agent` instances built on the main LLM with middleware `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget()]`. `_NudgeLimitTool` rejects any tool whose metadata lacks `nudge: true`, so nudge agents can only touch the memory/skill tools. Prompts: `_MEMORY_REVIEW_PROMPT` (memory review), `_SKILL_REVIEW_PROMPT` (skill library review), `_COMBINED_REVIEW_PROMPT` (both at once).

> The previous version of this document claimed knowledge-graph maintenance (`after_turn`) and a `MemoryCache`. **Neither exists in the current code.** System prompts come from the state registers and `build_system_prompt()`; there is no knowledge-graph call anywhere in the middleware layer.

### MultimodalProcessor

**Module:** `agent/middlewares/multimodal_processor.py` · **Class:** `MultimodalProcessor(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `after_agent` / `aafter_agent`

`before_agent` processes the **last** `HumanMessage` when its content is a multimodal list:

- **Text** items pass through (at most one).
- **`image_url`**: remote `http(s)` URLs are kept as-is; `data:` / base64 payloads are decoded and saved with PIL under `src/<session_id>/mutil_temp/<timestamp><ext>` (extension inferred from magic bytes via `_IMAGE_MAGIC`), with a durable copy in `media/`.
- **`audio_url`**: downloaded to a temp file (30 s timeout). **`audio_bytes` / `video_url` / `video_bytes`**: decoded and saved the same way (`_AUDIO_MAGIC` / `_VIDEO_MAGIC`).
- An `"[Uploaded media]"` instruction block is appended to the message text, telling the model to inspect the files with the `skill_view` tools `image_to_text` / `speech_to_text` / `video_text_to_text` (the model has no native vision).
- Persisted paths are stored in `additional_kwargs["images"]` / `["audios"]` / `["videos"]` and later written to MesMemory for history rendering.
- `image_url` blocks are stripped from **older** `HumanMessage`s so stale base64 blobs do not linger in context.

`after_agent` cleans `mutil_temp`: deletes files whose stem is not a pure numeric timestamp or that are older than 7 days.

### IterationBudget

**Module:** `agent/middlewares/iteration_budget.py` · **Class:** `IterationBudget(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`

Hard cap on **model calls + tool calls combined** within one turn. Constructor: `__init__(max_iterations: int = 50)`; the main agent registers `IterationBudget(90)` and worker agents `IterationBudget(60)`.

- `before_agent` resets the counters in `state_register_mem`: `iteration_budget = max_iterations`, `iteration_budget_used = 0`.
- `wrap_model_call` consumes 1 per model call; when the budget is exhausted it returns a terminal `AIMessage` **without calling the model**.
- `wrap_tool_call` consumes 1 per tool call; when exhausted it returns an error `ToolMessage` ("Tool [x] skipped — iteration budget exhausted") instead of executing.

### ToolGuardrails

**Module:** `agent/middlewares/tool_guardrails.py` · **Class:** `ToolGuardrails(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `wrap_tool_call` / `awrap_tool_call`

Detects three failure pathologies and reacts with a four-level escalation `ALLOW → WARN → BLOCK → HALT` (the `GuardrailAction` enum):

| Pathology | Trigger | Default reaction |
|---|---|---|
| Exact failure repetition | Same tool + same arguments (MD5 of the JSON args, `sort_keys`) failing | Warn at ≥ 2, block at ≥ 5 (`exact_failure_warn_after=2`, `exact_failure_block_after=5`) |
| Same-tool failure accumulation | Same tool failing with **different** arguments | Warn at ≥ 3, halt at ≥ 8 (`same_tool_failure_warn_after=3`, `same_tool_failure_halt_after=8`) |
| Idempotent no-progress | Tool with metadata `idempotent: true` returning an identical result hash | Warn at ≥ 2, block at ≥ 5 (`no_progress_warn_after=2`, `no_progress_block_after=5`) |

- `before_agent` resets the per-turn guard state (key `tool_guardrail_state` in `state_register_mem`).
- `wrap_tool_call` pre-checks blocked tools and halt state (returns an error `ToolMessage` without executing), runs the tool, then evaluates the result:
  - `warn` appends a warning to the `ToolMessage`;
  - `block` records the tool in `blocked_tools`;
  - `halt` sets a sticky halt for the rest of the turn (`halt_decision`).
- `ToolCallGuardrailConfig` defaults: `warnings_enabled=True`, `hard_stop_enabled=False` — with `hard_stop_enabled=True` the *block* levels also escalate to halt.

### ToolCallNormalize

**Module:** `agent/middlewares/tool_call_normalize.py` · **Class:** `ToolCallNormalize(AgentMiddleware)`
**Hooks:** `before_model` / `abefore_model` only

Repairs tool-call / tool-result pairing after context trimming to prevent "Message ordering conflict" errors from the provider. Delegates to `pub_func.sanitize_tool_use_result_pairing(state["messages"])` (defined in `pub_func/transcript_repair.py`), which:

- deduplicates `ToolMessage`s by `tool_call_id`;
- drops empty `ToolMessage`s;
- inserts a placeholder `ToolMessage` ("tool result missing after context trim.") for missing results;
- clears `invalid_tool_calls` on error-status `AIMessage`s so they are not serialized as OpenAI tool calls.

The hook returns a full message replacement: `[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`.

### HeartbeatStaleness

**Module:** `agent/middlewares/heartbeat_staleness.py` · **Class:** `HeartbeatStaleness(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `after_agent` / `aafter_agent`, `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`

Watchdog for stuck turns. Registered in **both** the main agent and the worker agents (an earlier version of this document claimed worker-only — that was wrong).

- `before_agent` resets the state keys and starts a background timer via `timer_call_register.register(..., execute_now=True)` (1-minute cadence).
- `wrap_model_call` increments `heartbeat_iter` — but first raises `HeartbeatTimeoutError` if a previous check already killed the turn. `wrap_tool_call` sets `heartbeat_tool` while a tool runs and clears it afterwards.
- The timer callback compares `(heartbeat_iter, heartbeat_tool)` against `_last_heartbeat_iter` / `_last_heartbeat_tool`. Progress resets the stale counter; no progress increments it. After `stale_cycles_idle = 7` checks while idle, or `stale_cycles_in_tool = 20` checks while stuck inside one tool, `heartbeat_killed = True` — the next model / tool call raises `HeartbeatTimeoutError` instead of proceeding.
- `after_agent` stops the timer.
- State keys: `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, plus `_last_heartbeat_iter` / `_last_heartbeat_tool`.

### HumanInTheLoop

**Module:** `agent/middlewares/humanInTheLoop/core.py` · **Class:** `HumanInTheLoop(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `after_model` / `aafter_model`, `wrap_tool_call` / `awrap_tool_call`

Registered in the main agent as `HumanInTheLoop(HITLConfig())` — all defaults, i.e. mode `ApprovalMode.SMART`. Intercepts tool calls after each model response and, where policy requires it, suspends the graph with the LangGraph-native `interrupt()` so the frontend can render an approval dialog. Denied calls are replaced with an error `ToolMessage` (`BLOCKED_MESSAGE`); the `GraphInterrupt` is re-raised, never swallowed.

Per-call pipeline in `after_model`:

1. Hard-line / dangerous command detection (`detection.py`: `detect_hardline_command`, `detect_dangerous_command`, backed by `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS`) via `ApprovalPipeline.check_command` (`approval.py`).
2. Smart approval (`ApprovalMode.SMART`, optional `smart_approval_llm`) — auto-approves clearly safe calls.
3. `interrupt()` — decision timeout 60 s by default.
4. Memory-tool writes go through `WriteApprovalGate` when `write_approval_memory=True`; tools listed in `interrupted_tools` always interrupt with decisions `approve` / `edit` / `reject` (`edit` rewrites the tool-call args/name).
5. `wrap_tool_call` rejects execution for calls whose approval was denied or timed out (the per-turn flag is reset in `before_agent`).

Sub-gates (`gates.py` / `approval.py`): `ApprovalPipeline`, `WriteApprovalGate`, `InterruptManager`, `MCPElicitationConsent`, `KanbanTriage`, `PairingStore`, `SlashConfirm`. State is namespaced under `hitl:` keys in `state_register_mem`.

`HITLConfig` defaults:

| Parameter | Default | Meaning |
|---|---|---|
| `mode` | `ApprovalMode.SMART` | `SMART` / `MANUAL` / `OFF` |
| `timeout` | `60` | Interrupt decision timeout |
| `deny_rules` | `[]` | Explicit deny patterns |
| `yolo_mode` | `False` | Skip all approvals |
| `write_approval_memory` | `False` | Gate memory-tool writes |
| `write_approval_skills` | `False` | Gate skill writes |
| `clarify_timeout` | `3600` | Clarification-question timeout |
| `kanban_recurrence_limit` | `3` (`BLOCK_RECURRENCE_LIMIT`) | Repeated-block limit before kanban triage |
| `mcp_reload_confirm` | `True` | Confirm MCP server reloads |
| `destructive_slash_confirm` | `True` | Confirm destructive slash commands |
| `smart_approval_llm` | `None` | LLM used for smart auto-approval |
| `interrupted_tools` | `{}` | Tools that always raise `interrupt()` |
| `description_prefix` | `"Action requires human approval"` | Approval-dialog title prefix |

▶️ Full details: [humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### Summarization

**Module:** `agent/middlewares/summarization.py` · **Class:** `Summarization(SummarizationMiddleware)`
**Hooks:** `before_agent` / `abefore_agent` (counter reset), `wrap_model_call` / `awrap_model_call`, plus log-only `before_model` / `abefore_model`

The innermost middleware — closest to the LLM. Extends LangChain's built-in `SummarizationMiddleware`: when the trigger fires, older messages are summarized by the auxiliary LLM and replaced, keeping the newest `keep` messages.

- **Trigger semantics** (LangChain `TriggerClause`): one clause is an **AND** of its conditions; a list of clauses is an **OR**. Main agent: `[("tokens", int(main_llm_max_tokens / 2))]`. Worker: `[("messages", 40), ("tokens", 30000)]`. Both keep `("messages", 10)`.
- **Cutoff safety:** `_determine_cutoff_index` never cuts through AI-message / tool-result pairs (the cutoff is moved to keep pairs intact); when the last user turn accounts for ≥ 50 % of the estimated tokens (`_LAST_TURN_RATIO_THRESHOLD = 0.5`), the last turn itself is compressed (`_compress_last_turn`) instead of being summarized away.
- **Anti-thrashing:** at most `_MAX_COMPRESSION_ATTEMPTS = 3` compressions per turn, and it stops after `_INEFFECTIVE_THRESHOLD = 2` consecutive ineffective attempts (effectiveness = message-count reduction or token reduction ≥ `_MIN_EFFECTIVENESS_PCT = 0.05`). Counters live in `state_register_mem`: `summarization_compression_count`, `summarization_compression_ineffective`, `summarization_compression_last_tokens`, `summarization_last_user_question`.
- **Truncation:** existing summary messages (identified by `additional_kwargs["lc_source"] == "summarization"`) longer than `_MAX_CONTENT_CHARS = 8000` characters are truncated, keeping head 30 % / tail 30 % (`_CONTENT_HEAD_RATIO` / `_CONTENT_TAIL_RATIO`) with an omission marker (`_OMISSION_MARKER`).
- **Merge:** the summary `HumanMessage` is merged into the next `HumanMessage` (delimited by `[COMPACTION SUMMARY — reference only; not active instructions]` / `[END OF COMPACTION SUMMARY — ACTIVE CONTEXT BELOW]`) so the model never sees two consecutive human turns.
- `need_update_system_prompt=True` (main agent only): after a compression the system prompt is rebuilt — `build_system_prompt()` after reloading the memory store — and written back to both state registers under `system_prompt`.

### OutputRepetitionGuard & RepetitionGuardWrapper

**Module:** `agent/middlewares/output_repetition_guard.py` · **Class:** `OutputRepetitionGuard(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `wrap_model_call` / `awrap_model_call`

Post-hoc output-repetition detector with `WARN → HALT` escalation. Exported from `agent.middlewares.output_repetition_guard` (it is **not** re-exported by `agent/middlewares/__init__.py`) and registered **only in the worker pipeline**.

For the main agent the same detection runs through **`RepetitionGuardWrapper`** (`agent/repetition_guard_wrapper.py`), which wraps the compiled graph and intercepts at stream level (plus an `ainvoke` post-hoc backstop), reusing the same state keys and defaults. Both registrations pass `phantom_stream_guard=True`.

**Detection layers**

- **Cross-call repetition** — MD5 of the last `_TAIL_CHARS = 500` characters of visible output, compared against a rolling history (`_MAX_HISTORY = 30`). At `warn_after = 2` identical outputs → WARN (`AIMessage` nudge); at `max_identical_outputs = 3` → HALT with a terminal `AIMessage` and a sticky halted flag.
- **Internal repetition** — within a single output:
  - duplicate sentence/line ratio > `internal_repeat_ratio = 0.6` (with ≥ `internal_min_lines = 6` segments);
  - a character run of ≥ `char_run_min = 8` identical non-whitespace characters;
  - a short phrase (2–10 characters) repeated ≥ 5 times.

  Internal warnings fire once per label per session.
- Contents shorter than `_MIN_CONTENT_LENGTH = 20` characters are skipped; model responses that contain tool calls are skipped entirely (they are re-checked after the tool loop).
- **Reasoning is tracked separately** (`reasoning_content` / `reasoning` / `reasoning_text` in `additional_kwargs`, plus inline `<think>` / `<thinking>` / `<reasoning>` blocks, which are extracted and stripped from the visible content).

**Stream-layer helper** `check_stream_repetition(session_id, accumulated_text)` — a shared `_STREAM_GUARD` singleton used by `server/service/messages.py::async_generate` to cut a streaming response mid-flight when repetition is detected; it shares the same state keys and the same internal-warn dedupe gate.

**Worker cleanup:** `SESSION_STATE_KEYS` (six keys) are deleted from `state_register_mem` when the child session finishes.

---

## Shared State System

All cross-call middleware state is session-scoped and lives in two registers plus a timer registry:

| Register | Backing | Notes |
|---|---|---|
| `state_register_mem` (`StateRegisterMeM`) | In-memory dict | Volatile; an `_initialized` guard resets it once per process start |
| `state_register_db` (`StateRegisterDB`) | SQLite (`src/data/state_register.db`) | Survives restarts; `clear_session` is not supported (returns `False`); exposes `get_all_session_ids` |
| `timer_call_register` (`TimerCallRegister`) | Asyncio timers | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

Common interface (`runtime/state_register.py`): `set_state`, `get_state`, `get_all_states`, `delete_state`, `clear_session`, `has_session`, `has_key`, `update_states`.

### Namespace Convention

| Key(s) | Owner | Register |
|---|---|---|
| `system_prompt` | ContextEngineHook / Summarization | mem + db |
| `nudge_review_memory_count`, `nudge_review_skill_count` | ContextEngineHook | db |
| `nudge_review_memory_lock`, `nudge_review_skill_lock` | ContextEngineHook | mem |
| `iteration_budget`, `iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_compression_count`, `summarization_compression_ineffective`, `summarization_compression_last_tokens`, `summarization_last_user_question` | Summarization | mem |
| `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, `_last_heartbeat_iter`, `_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard keys (`SESSION_STATE_KEYS`, six) | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `hitl:`-prefixed keys (`_STATE_PREFIX = "hitl"`) | HumanInTheLoop | mem |

---

## Configuration

### Environment & Config Knobs

| Knob | Where | Effect |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | Main-agent Summarization trigger = half of this value |

> **Related but separate:** per-tool timeouts are hard-coded module constants — `WEB_SEARCH_TIMEOUT = 15` (`agent/tools/web_search.py`), `TERMINAL_TIMEOUT = 30` (`agent/tools/terminal.py`), `PYTHON_REPL_TIMEOUT = 30` (`agent/tools/python_repl.py`; the child process is killed on expiry). `TOOL_CALL_TIMEOUT_MINUTES = 5` exists in `.env.example` but **no code consumes it** — it is not an active knob. The `config/num.py` constants (`ARCHIVE_THRESHOLD`, `MEMORY_THRESHOLD`, `COMPRESS_RATIO`) are not consumed by the middleware layer either.

### Example Builder Configuration

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook, MultimodalProcessor, IterationBudget, ToolGuardrails,
    ToolCallNormalize, HeartbeatStaleness, HumanInTheLoop, HITLConfig, Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),          # system prompt + nudge + persistence
        MultimodalProcessor(),        # multimodal input normalization
        IterationBudget(90),          # per-turn call budget
        ToolGuardrails(),             # failure-pathology detection
        ToolCallNormalize(),          # tool_use/tool_result repair
        HeartbeatStaleness(),         # stuck-turn watchdog
        HumanInTheLoop(HITLConfig()), # approval gates
        Summarization(                # context compaction (innermost)
            need_update_system_prompt=True,
            model=auxiliary_llm,
            trigger=[("tokens", int(main_llm_max_tokens / 2))],
            keep=("messages", 10),
        ),
    ],
)
```

### Per-Middleware Parameters

| Middleware | Parameter | Default | Registered value |
|---|---|---|---|
| `IterationBudget` | `max_iterations` | `50` | `90` (main) / `60` (worker) |
| `Summarization` | `need_update_system_prompt` | `False` | `True` (main) |
| `Summarization` | `model` | required | `auxiliary_llm` |
| `Summarization` | `trigger` | required | see [Middleware Chain](#middleware-chain) |
| `Summarization` | `keep` | required | `("messages", 10)` |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | defaults above | defaults |
| `HumanInTheLoop` | `config: HITLConfig` | defaults above | defaults |
| `HeartbeatStaleness` | (defaults) | interval 1 min, idle 7 / in-tool 20 | defaults |
| `OutputRepetitionGuard` | (defaults) | 3 / 2 / 0.6 / 6 / 8 | defaults |

---

## Lifecycle & Data Flow

### Single Turn (Detailed)

```
user turn arrives
│
├─ before_agent (list order)
│   ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails
│   → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · ContextEngineHook   no-op here (persistence happens in after_agent)
│   · MultimodalProcessor  normalize last HumanMessage, strip old image_url blocks
│   · IterationBudget  reset budget counters
│   · ToolGuardrails  reset per-turn guard state
│   · HeartbeatStaleness  reset keys + start 1-min heartbeat timer
│   · HumanInTheLoop  reset per-turn interrupt flags
│   · Summarization  reset compression counters
│
├─ loop: model call
│   ├─ before_model
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage rewrite
│   │   · Summarization  (log only)
│   ├─ wrap_model_call (outermost → innermost)
│   │   · ContextEngineHook  inject system prompt (request.override)
│   │   · IterationBudget  consume 1; terminal AIMessage when exhausted
│   │   · HeartbeatStaleness  raise HeartbeatTimeoutError if killed; else heartbeat_iter += 1
│   │   · Summarization  maybe compact history (auxiliary LLM), anti-thrash counters
│   ├─ LLM responds
│   └─ after_model
│       · HumanInTheLoop  policy checks; interrupt() where required; block → error ToolMessage
│
├─ loop: tool calls (per call)
│   └─ wrap_tool_call
│       · IterationBudget  consume 1; error ToolMessage when exhausted
│       · ToolGuardrails  pre-check block/halt → run → evaluate → warn/block/halt
│       · ContextEngineHook  skill-review counter (unless tool metadata nudge: true)
│       · HeartbeatStaleness  raise if killed; set heartbeat_tool, clear after return
│       · HumanInTheLoop  reject calls with denied/timed-out approval
│
└─ after_agent (reverse order)
    Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize
    → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook
    · HeartbeatStaleness  stop heartbeat timer
    · MultimodalProcessor  clean mutil_temp (> 7 days / non-numeric stems)
    · ContextEngineHook  memory-review counter → maybe nudge sub-agents (locks)
                        → persist last turn to MesMemory (slice → sanitize → add_messages)
```

---

## Writing a Custom Middleware

Subclass `AgentMiddleware` and override only the hooks you need (signatures from the installed `langchain 1.3.9` — state hooks receive `(state, runtime)`, wrap hooks receive `(request, handler)`):

```python
from langchain.agents.middleware import AgentMiddleware


class MyMiddleware(AgentMiddleware):
    """Runs once per turn, before and after the whole loop."""

    def before_agent(self, state, runtime):
        # return a state update dict, or None
        return None

    def after_agent(self, state, runtime):
        return None

    def wrap_model_call(self, request, handler):
        # inspect/modify `request`, then delegate to `handler(request)`
        return handler(request)

    def wrap_tool_call(self, request, handler):
        return handler(request)
```

Async variants follow the `a` prefix convention: `abefore_agent`, `aafter_agent`, `awrap_model_call`, `awrap_tool_call`, etc. Keep wrap hooks cheap and side-effect-light — they run on **every** model/tool call, and in this codebase the first registered middleware is the outermost wrap layer.

---

## Appendix

### File Layout

```
agent/middlewares/
├── __init__.py                  # public exports
├── context_engine/              # ContextEngineHook + nudge sub-agents
│   ├── __init__.py              # exports ContextEngineHook only
│   ├── core.py                  # ContextEngineHook
│   └── nudge.py                 # nudge prompts + sub-agent builders
├── heartbeat_staleness.py       # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig (has its own README)
│   ├── __init__.py              # exports HumanInTheLoop, HITLConfig
│   ├── types.py                 # enums + config dataclass (_STATE_PREFIX = "hitl")
│   ├── detection.py             # hard-line / dangerous command patterns
│   ├── approval.py              # ApprovalPipeline
│   ├── gates.py                 # WriteApprovalGate, InterruptManager, MCPElicitationConsent,
│   │                            # KanbanTriage, PairingStore, SlashConfirm
│   └── core.py                  # HumanInTheLoop
├── iteration_budget.py          # IterationBudget
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard (not re-exported below)
├── summarization.py             # Summarization
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # this file (+ .zh / .ja / .ko variants)

agent/repetition_guard_wrapper.py  # RepetitionGuardWrapper (lives outside this package)
```

### Exports (`__init__.py`)

```python
from agent.middlewares import (
    Summarization,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# OutputRepetitionGuard is NOT re-exported here — import it from
# agent.middlewares.output_repetition_guard instead.
```

