# EMA Agent Middleware System

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

The middleware layer of the EMA AI Agent: `AgentMiddleware` components that shape every model call and tool call — context engineering, multimodal input handling, iteration budgets, tool guardrails, transcript repair, heartbeat staleness detection, human-in-the-loop approvals, context summarization, and classified LLM error retry with model fallback (`LLMRetryMiddleware`) — plus an output repetition guard and stream-level graph wrappers (`RepetitionGuardWrapper`, `ContextLimitGuardWrapper`).

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
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [LLMRetryMiddleware](#llmretrymiddleware)
  - [Summarization](#summarization)
  - [MaxTokensBoostMiddleware](#maxtokensboostmiddleware)
  - [OutputRepetitionGuard & RepetitionGuardWrapper](#outputrepetitionguard--repetitionguardwrapper)
  - [ContextLimitGuardWrapper](#contextlimitguardwrapper)
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
    SubagentCompletionDrainMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    LLMRetryMiddleware(fallback_chain=fallback_chain),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# the compiled graph is then wrapped (innermost → outermost):
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
agent = ContextLimitGuardWrapper(agent, context_window=main_llm_max_tokens)
```

`main_llm_max_tokens` is read from the `MAIN_LLM_MAX_TOKEN` environment variable (`models/LLMs/main_llm.py`), so the main-agent summarization trigger sits at 80 % of the main model's context window (`COMPRESSION_TRIGGER_RATIO = 0.80`).

> **Note:** `OutputRepetitionGuard` **is** registered as main-agent middleware (per-call interception) **and** the compiled graph is additionally wrapped by `RepetitionGuardWrapper` for stream-level detection — see [OutputRepetitionGuard & RepetitionGuardWrapper](#outputrepetitionguard--repetitionguardwrapper).

### Worker / Subagent Pipeline (`agent/tools/subagent/spawn/core.py`)

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[
            ("messages", 40),
            ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
        ],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# the child graph is wrapped the same way:
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

Differences vs the main agent:

- Summarization triggers on message count (40) **or** tokens (80 % of the context window) instead of only tokens.
- A tighter iteration budget (60 instead of 90).
- No `ContextEngineHook`, no `MultimodalProcessor`, no `HumanInTheLoop`, no `LLMRetryMiddleware` (children do not get the classified retry/fallback loop).
- `OutputRepetitionGuard` runs as a real middleware here.
- `MaxTokensBoostMiddleware` takes its non-streaming path: children run via
  `ainvoke`, so the `is_stream_turn` flag is never set for a child session id.
- When a child session finishes, the spawn code deletes the six `OutputRepetitionGuard` state keys (`SESSION_STATE_KEYS`) from `state_register_mem` in its `finally` block.

### Effective Per-Turn Order (main agent)

| Phase | Order |
|---|---|
| `before_agent` (list order) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → LLMRetryMiddleware → Summarization |
| `wrap_model_call` (outermost → innermost) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → OutputRepetitionGuard → MaxTokensBoostMiddleware → HeartbeatStaleness → HumanInTheLoop → LLMRetryMiddleware → Summarization (Summarization sits closest to the LLM; LLMRetry wraps Summarization's T4/T5 recovery from the outside and sits inside MaxTokensBoost so it only sees genuine truncations) |
| `after_agent` (reverse order) | Summarization → LLMRetryMiddleware → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

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

Detects five failure pathologies and reacts with a four-level escalation `ALLOW → WARN → BLOCK → HALT` (the `GuardrailAction` enum):

| Pathology | Trigger | WARN after | BLOCK after | hard-stop mode |
|---|---|---|---|---|
| Exact failure repetition | Same tool + same arguments (MD5 of the JSON args, `sort_keys`) failing | 2 (`exact_failure_warn_after`) | 5 (`exact_failure_block_after`) | HALT at 5 |
| Same-tool failure accumulation | Same tool failing with **different** arguments | 3 (`same_tool_failure_warn_after`) | 8 (`same_tool_failure_halt_after`) | HALT at 8 |
| Idempotent no-progress | Tool with metadata `idempotent: true` returning an identical result hash | 2 (`no_progress_warn_after`) | 5 (`no_progress_block_after`) | HALT at 5 |
| Ping-pong | Unbroken read-only A → B → A → B bouncing between two tools | 4 (`ping_pong_warn_after`) | 6 (`ping_pong_block_after`) | HALT at 6 |
| Argument churn | Same idempotent tool cycling through argument variants | 3 variants (`arg_churn_warn_after`) | 5 variants (`arg_churn_block_after`) | HALT at 5 |

- `before_agent` resets the per-turn guard state (key `tool_guardrail_state` in `state_register_mem`) — strictly turn-scoped, so a fresh turn starts clean.
- `wrap_tool_call` pre-checks blocked tools and halt state (returns an error `ToolMessage` without executing), runs the tool, then evaluates the result:
  - `warn` appends a warning to the `ToolMessage`;
  - `block` records the tool in `blocked_tools`;
  - `halt` sets a sticky halt for the rest of the turn (`halt_decision`).
- **Recovery mode** (`recovery_mode_enabled=True` by default): the first BLOCK does not brick the turn. The turn enters recovery, and the *precheck* path releases the blocked tool so the retry is evaluated fresh. Each further BLOCK increments a violation counter; once the counter exceeds `recovery_max_violations` (default 1), the action escalates to HALT — a managed retry window instead of an immediate wall.
- **Ping-pong pairs** hash the two tool names of adjacent calls and accumulate only while *both* consecutive calls are successful idempotent calls (both records carry a result hash). Any error, or any successful non-idempotent (mutating) call, zeroes every accumulated pair streak. Result content is never compared: unbroken read-only bouncing is a loop signal on its own. A non-idempotent tool success likewise resets argument-churn state.
- `ToolCallGuardrailConfig` defaults: `warnings_enabled=True`, `hard_stop_enabled=False`, `recovery_mode_enabled=True`, `recovery_max_violations=1` — with `hard_stop_enabled=True` every *block* threshold converts into HALT (the old strict wall); `recovery_mode_enabled=False` restores the immediate block behavior.

▶️ Full details: [docs/harness/loop-prevention/README.md](../../docs/harness/loop-prevention/README.md) · [中文](../../docs/harness/loop-prevention/README.zh.md) · [한국어](../../docs/harness/loop-prevention/README.ko.md) · [日本語](../../docs/harness/loop-prevention/README.ja.md)

### ToolCallNormalize

**Module:** `agent/middlewares/tool_call_normalize.py` · **Class:** `ToolCallNormalize(AgentMiddleware)`
**Hooks:** `before_model` / `abefore_model` only

Repairs tool-call / tool-result pairing after context trimming to prevent "Message ordering conflict" errors from the provider. Delegates to `pub.func.sanitize_tool_use_result_pairing(state["messages"])` (defined in `pub/func/transcript_repair.py`), which:

- deduplicates `ToolMessage`s by `tool_call_id`;
- drops empty `ToolMessage`s;
- inserts a placeholder `ToolMessage` ("tool result missing after context trim.") for missing results;
- clears `invalid_tool_calls` on error-status `AIMessage`s so they are not serialized as OpenAI tool calls.

The hook returns a full message replacement: `[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`.

### SubagentCompletionDrainMiddleware

**Module:** `agent/middlewares/subagent_completion_drain.py` · **Class:** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**Hooks:** `before_model` / `abefore_model` only

Registered in the main agent immediately AFTER `ToolCallNormalize`, so the messages it injects bypass the sanitize rewrite on the injection turn. At `before_model` it rehydrates and drains the session's `SteeringQueue` — completion carriers queued by the announce pipeline while the parent was busy — and returns `{"messages": [carrier, ...]}`, injecting the rebuilt completion-carrier `HumanMessage`s right before the next model call.

- Each drained queue item is marked `CONSUMED` in the queue's SQLite store, so a carrier is injected exactly once (checkpoint persistence keeps HITL-resume replays safe).
- Fail-open: a blank/missing `session_id`, an empty queue, or any error is swallowed (log + no-op) — the drain never breaks the parent turn, and the queue survives for retry.
- The injected carrier is persisted to MesMemory with `origin='subagent_completion'`.

### HeartbeatStaleness

**Module:** `agent/middlewares/heartbeat_staleness.py` · **Class:** `HeartbeatStaleness(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `after_agent` / `aafter_agent`, `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`

Watchdog for stuck turns. Registered in **both** the main agent and the worker agents (an earlier version of this document claimed worker-only — that was wrong).

- `before_agent` resets the state keys and starts a background timer via `timer_call_register.register(..., execute_now=True)` (1-minute cadence).
- `wrap_model_call` increments `heartbeat_iter` — but first raises `HeartbeatTimeoutError` if a previous check already killed the turn. `wrap_tool_call` sets `heartbeat_tool` while a tool runs and clears it afterwards.
- `skip_heartbeat` bypass: a tool whose metadata sets `skip_heartbeat: true` (interrupt-based tools park the graph waiting for human input, so the after-tool hook never runs until resume) sets `heartbeat_skip` instead of `heartbeat_tool`; while the flag is up the timer callback skips the progress check — the unchanged `(iter, tool)` pair during that wait is not a stall. The flag clears in the after-tool hook and resets in `before_agent`.
- The timer callback compares `(heartbeat_iter, heartbeat_tool)` against `_last_heartbeat_iter` / `_last_heartbeat_tool`. Progress resets the stale counter; no progress increments it. After `stale_cycles_idle = 7` checks while idle, or `stale_cycles_in_tool = 20` checks while stuck inside one tool, `heartbeat_killed = True` — the next model / tool call raises `HeartbeatTimeoutError` instead of proceeding.
- `after_agent` stops the timer.
- State keys: `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, `heartbeat_skip`, plus `_last_heartbeat_iter` / `_last_heartbeat_tool`.

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

### LLMRetryMiddleware

**Module:** `agent/middlewares/llm_retry.py` · **Class:** `LLMRetryMiddleware(AgentMiddleware)` (plus `LLMRetryConfig`, `FallbackCandidate`, `ContentFilterError`)
**Hooks:** `wrap_model_call` / `awrap_model_call` only

Registered in the main agent **between `HumanInTheLoop` and `Summarization`**: inner relative to `MaxTokensBoostMiddleware` (it only sees genuine truncations that survived the boost re-calls) and outer relative to Summarization (the retry loop wraps the T4/T5 overflow-recovery ring from the outside). Not registered in the worker pipeline. When the state carries no `session_id`, the middleware is a passthrough.

Each handler call runs through a classify → act loop built on `pub/func/message/llm_error_classifier.py` — the `FailoverReason` enum (18 reasons), the `ClassifiedError` verdict (`retryable` / `should_compress` / `should_fallback` flags), and the 8-step priority pipeline `classify_api_error` — and backs off via `pub/func/retry_utils.py::jittered_backoff`.

**Retry semantics by `FailoverReason`**

| Class | Reasons | Action |
|---|---|---|
| Retry with jittered backoff (`retryable=True`) | `auth`, `rate_limit`, `overloaded`, `server_error`, `timeout`, `image_too_large`, `invalid_response`, `unknown` | Up to `max_retries` re-calls; delay = `base_delay × 2^(attempt−1)` capped at `max_delay`, ±`jitter`, clamped to `[0.1, max_delay]` |
| Compress-delegate (`should_compress=True`) | `context_overflow`, `payload_too_large` | Re-raise immediately — Summarization's T4/T5 recovery ring owns overflow errors |
| Fallback (`should_fallback=True`, non-retryable) | `auth_permanent`, `billing`, `upstream_rate_limit`, `ssl_cert_verification`, `model_not_found`, `provider_policy_blocked`, `content_policy_blocked` | Switch to the next fallback candidate; chain exhausted → re-raise |
| Hard fail | `format_error` | Re-raise (no retry, no fallback) |

**Stale-streak circuit breaker (cross-turn):** every timeout-classified failure — and every timeout-classified partial-stream-stub retry — increments the session-scoped `llm_stale_streak` in `state_register_mem`; any successful handler call resets it to 0. When the streak reaches `stale_giveup_threshold`, the next model call raises `RuntimeError("Provider unresponsive — aborting to avoid indefinite stall.")` **before** the LLM is invoked, ending a persistent provider stall across turns.

**Model fallback chain:** `FallbackCandidate(provider, model_name, model)` entries are built by `models/LLMs/main_llm.py::build_fallback_chain()` from `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` env vars (i = 1…, stops at the first missing `NAME`; `PROVIDER` defaults to `openai`; candidates whose client cannot be constructed are skipped with a warning). The 1-based active index is sticky per session in `llm_fallback_index`: at the start of every model call the request is re-bound to the already-activated candidate via `request.override(model=...)`, and on a fallback-classified failure (or a content-filter flag) the next candidate is activated. With no `FALLBACK_LLM_*` configured (the default) the middleware is a plain bounded retry loop.

**Content-filter flag consumption:** the stream layer (`server/service/stream_dispatch.py`) sets `llm_content_filter_blocked` (explicit `finish_reason == "content_filter"`) or `llm_content_filter_terminated` (mid-stream safety cut). The middleware checks both flags after every handler call — on success or on a classified exception — clears them, and either re-binds to the fallback model or raises `ContentFilterError("Model declined to respond (safety refusal).")` when no candidate remains. `content_policy_blocked` never retries.

**Partial-stream stub consumption:** after a mid-stream network cut the stream layer sets `llm_partial_stream_stub` plus `llm_partial_stream_cause` (the preserved `FailoverReason` value, defaulting to `timeout`). The middleware consumes the flag after a successful handler call: the cut result is discarded and the handler is re-called once as a fresh attempt after backoff — never boosted with larger max_tokens. On stream turns (`is_stream_turn` flag) the re-call strips `request.config["callbacks"]` first and restores them in `finally` (the MaxTokensBoost strip → call → restore contract), so the already-streamed tokens are not duplicated. A timeout-classified cause bumps the stale streak. When the retry budget is exhausted, the middleware degrades gracefully and returns the current (partial) result.

**State keys (all in `state_register_mem`):** `llm_stale_streak`, `llm_fallback_index` (owned here); `llm_content_filter_blocked`, `llm_content_filter_terminated`, `llm_partial_stream_stub`, `llm_partial_stream_cause` (written by the stream layer, consumed here).

### Summarization

**Module:** `agent/middlewares/summarization.py` · **Class:** `Summarization(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent` (counter reset), `wrap_model_call` / `awrap_model_call`

The innermost middleware — closest to the LLM. A from-scratch `AgentMiddleware` (**not** LangChain's `SummarizationMiddleware`): when the trigger fires, it compacts history with a budget-based cutoff — non-LLM strategies first, auxiliary-LLM summarization only when text degradation is safe. The `keep` parameter is accepted but unused; tail retention is budget-based: `clamp(context_window × 0.25, 2 000, 15 000)` tokens (`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`).

- **Lifecycle & routing:** the middleware now spans five trigger points (T1–T5) — T1 preflight (`before_agent` / `abefore_agent`), T2 pre-call dispatch (`wrap_model_call` / `awrap_model_call`), T3 post-response re-check on real (reported) tokens, and the T4 (413 Payload Too Large) / T5 (context overflow) error-recovery ring — and every trigger runs the four-route overflow decision (truncate / compact / both / pass), delegated to `pub/func/message/overflow_router.py`, `pub/func/message/tool_result_ttl.py`, `pub/func/message/tool_args_truncate.py` (tool-call args truncation), and `pub/func/message/llm_error_classifier.py`. State lives in session-scoped `summarization_*` keys (14 total, 10 reset per turn). Full docs: see the link row below.
- **Trigger semantics**: a clause is `("messages", N)` or `("tokens", N)`; a list of clauses is an **OR** — any clause firing starts compression. Main agent: `[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`. Worker: `[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`. `COMPRESSION_TRIGGER_RATIO = 0.80`.
- **Cutoff safety:** `_determine_cutoff` picks the cut point, then `_adjust_for_orphan_pairs` walks it backwards until no `ToolMessage` is separated from its `AIMessage` tool-call; when the last user turn accounts for ≥ 50 % of the estimated tokens (`LAST_TURN_RATIO_THRESHOLD = 0.5`), the last turn itself is compressed (the `self._compress_last_turn` flag) instead of being summarized away.
- **Anti-thrashing:** at most `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` compressions **per session** (not per turn); after `INEFFECTIVE_THRESHOLD = 2` consecutive ineffective attempts (effectiveness = message-count reduction or token reduction ≥ `MIN_EFFECTIVENESS_PCT = 0.05`) the LLM step is disabled (`summarization_skip_llm`) and only non-LLM strategies run. Counters live in `state_register_mem` under session-level `summarization_*` keys (compression count, ineffective streak, last tokens, last strategy, skip flag, recovery state, …).
- **Truncation:** existing summary messages (identified by `additional_kwargs["lc_source"] == "summarization"`) longer than `SUMMARY_TOTAL_MAX_CHARS = 16 000` characters are re-truncated, keeping head 30 % / tail 30 % (`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`) with an omission marker.
- **Output:** the replacement messages are a `HumanMessage` / `AIMessage` **pair** — a neutral `"What did we do so far?"` followed by an `AIMessage` carrying `additional_kwargs={"lc_source": "summarization"}` — so the model never sees two consecutive same-role messages and no post-hoc pairing repair is needed.
- `need_update_system_prompt=True` (main agent only): after a compression the system prompt is rebuilt — `build_system_prompt()` after reloading the memory store — and written back to both state registers under `system_prompt`.

▶️ Full details: [docs/harness/summarization/README.md](../../docs/harness/summarization/README.md) · [中文](../../docs/harness/summarization/README.zh.md) · [한국어](../../docs/harness/summarization/README.ko.md) · [日本語](../../docs/harness/summarization/README.ja.md)

### MaxTokensBoostMiddleware

**Module:** `agent/middlewares/max_tokens_boost.py` · **Class:** `MaxTokensBoostMiddleware(AgentMiddleware)`
**Hooks:** `wrap_model_call` / `awrap_model_call`

Recovers from **tool-call truncation**: when a model call returns with
`finish_reason == "length"` (OpenAI) / `stop_reason == "max_tokens"` (Anthropic)
AND the response carries tool calls, the tool-call JSON was cut off. The
middleware re-calls the handler inside `wrap_model_call` / `awrap_model_call`
with a boosted `max_tokens = base × 2^attempt` (capped at 32768; max 3 retries)
so the model can emit the complete tool-call payload. The boost base resolves
through three layers — first positive value wins:

1. the call's own `request.model_settings["max_tokens"]` — the actual current
   limit, so the boost never restarts from a lower default when the call was
   configured higher;
2. the `MAIN_LLM_OUTPUT_MAX_TOKEN` environment variable (default 8192);
3. the hardcoded 8192 default.

The truncated intermediate result is discarded — the agent
loop only sees the final result, so nothing truncated reaches the checkpointer
and the IterationBudget is charged once per outer model call.

- **Text-only truncation** (no tool calls) is NOT handled here — the
  service-layer `StreamTurn` outer loop owns it (continuation HumanMessage).
- **Streaming re-calls strip callbacks**: the truncated first-call tokens
  already streamed to the client; before each re-call the middleware removes
  `request.config["callbacks"]` so no duplicate output is produced, and
  restores the original callbacks in `finally` (even on exception). The
  streaming/non-streaming decision reads the `is_stream_turn` flag that
  `StreamTurn.run()` sets per session — children (ainvoke) never carry it and
  always take the non-streaming path.
- `_extract_ai_message` handles both bare `AIMessage` results and
  `ModelRequest`-shaped response objects carrying `.messages`.
- **Thinking-budget interaction:** when `MAIN_LLM_ENABLE_THINKING=true`,
  `models/LLMs/main_llm.py::apply_thinking_budget` pre-inflates the request's
  `max_tokens` to `OUTPUT_MAX_TOKEN + thinking budget` (sharing the
  `MAIN_LLM_OUTPUT_MAX_TOKEN` env / 8192 default with this middleware), so the
  layer-1 boost base already starts above the thinking-inflated output cap.
- **Service-layer diagnostics:** on a stream failure,
  `server/service/stream_diag.py` counts chunks/bytes and time-to-first-chunk
  and appends the summary to the re-raised exception — complementary
  observability to the middleware-level recovery above.

### OutputRepetitionGuard & RepetitionGuardWrapper

**Module:** `agent/middlewares/output_repetition_guard.py` · **Class:** `OutputRepetitionGuard(AgentMiddleware)`
**Hooks:** `before_agent` / `abefore_agent`, `wrap_model_call` / `awrap_model_call`

Post-hoc output-repetition detector with `WARN → HALT` escalation. Exported from `agent.middlewares.output_repetition_guard` and re-exported by `agent/middlewares/__init__.py`; registered in **both** the main agent (per-call interception, complementing the wrapper below) and the worker pipeline.

For the main agent the same detection runs through **`RepetitionGuardWrapper`** (`agent/stream_repetition_guard_wrapper.py`), which wraps the compiled graph and intercepts at stream level (plus an `ainvoke` post-hoc backstop), reusing the same state keys and defaults. Both registrations pass `phantom_stream_guard=True`.

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

### ContextLimitGuardWrapper

**Module:** `agent/context_limit_guard_wrapper.py` · **Class:** `ContextLimitGuardWrapper`

A graph wrapper (like `RepetitionGuardWrapper`), **not** a middleware. In `agent/core.py` it wraps the compiled agent **outside** the `RepetitionGuardWrapper` (`agent → RepetitionGuardWrapper → ContextLimitGuardWrapper`), so it sees stream chunks before repetition filtering. It closes the middlewares' streaming blind spot: middlewares never see mid-stream chunks, and a post-response overflow signal cannot retroactively compact the context.

**Defense 1 — model-call boundary force-compress:** real `usage_metadata` input/output tokens are captured from the `messages` chunks; at each model-call boundary (`updates` mode) and at stream end, both the current call (`input_tokens` alone) and the predictive view (`input + output`, since the output becomes the next call's input) are checked against `COMPRESSION_TRIGGER_RATIO` (80 %) of the context window. At/over the threshold, Summarization's force-recovery key (`summarization_force_recovery`) is set in `state_register_mem` so the next pre-call check compresses instead of being skipped by the cooldown / attempt-cap anti-thrash gates.

**Defense 2 — mid-stream output budget:** accumulated model text is estimated at `_CHARS_PER_TOKEN = 4` characters per token and checked every `check_interval = 20` chunks; once the estimate exceeds `output_cut_ratio = 0.20` of the window, further text chunks stop being forwarded to the client (tool-call chunks still pass) and a one-time truncation marker (`"[System notice: the response exceeded the mid-stream output budget and was truncated.]"`) is emitted instead. The graph still accumulates the full `AIMessage` — the truncated tail is exactly what the next compression pass removes.

`ainvoke` delegates untouched (Summarization's T1–T3 triggers already cover the non-streaming path); unknown attributes delegate to the inner graph. Constructor: `(inner, context_window, output_cut_ratio=0.20, check_interval=20)` — `context_window` is `MAIN_LLM_MAX_TOKEN`, the same source Summarization's trigger uses.

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
| `summarization_*` keys (compression counters, ineffective streak, last tokens/strategy, skip-LLM flag, recovery state, last user question) | Summarization | mem |
| `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, `heartbeat_skip`, `_last_heartbeat_iter`, `_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard keys (`SESSION_STATE_KEYS`, six) | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `llm_stale_streak`, `llm_fallback_index` | LLMRetryMiddleware | mem |
| `llm_content_filter_blocked`, `llm_content_filter_terminated` | stream layer (written) → LLMRetryMiddleware (consumed) | mem |
| `llm_partial_stream_stub`, `llm_partial_stream_cause` | stream layer (written) → LLMRetryMiddleware (consumed) | mem |
| `summarization_force_recovery` | ContextLimitGuardWrapper (written) → Summarization (consumed) | mem |
| `hitl:`-prefixed keys (`_STATE_PREFIX = "hitl"`) | HumanInTheLoop | mem |

---

## Configuration

### Environment & Config Knobs

| Knob | Where | Effect |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | Main-agent Summarization trigger = 80 % of this value; also passed as `main_llm_context_window` and as `ContextLimitGuardWrapper.context_window` |
| `MAIN_LLM_OUTPUT_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | Output-token budget (default 8192): layer 2 of MaxTokensBoost's boost base, and the base that thinking-budget inflation adds to |
| `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` | `.env` → `build_fallback_chain()` | Model fallback chain candidates for `LLMRetryMiddleware` (i = 1…, stops at the first missing `NAME`) |

> **Related but separate:** per-tool timeouts are hard-coded module constants — `WEB_SEARCH_TIMEOUT = 15` (`agent/tools/web_search.py`), `TERMINAL_TIMEOUT = 30` (`agent/tools/terminal.py`), `PYTHON_REPL_TIMEOUT = 30` (`agent/tools/python_repl.py`; the child process is killed on expiry). `TOOL_CALL_TIMEOUT_MINUTES = 5` exists in `.env.example` but **no code consumes it** — it is not an active knob. These legacy constants previously lived in `config/num.py` (now removed); the summarization pipeline reads them from `config/features/agent_side/summarization.py`.

### Example Builder Configuration

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook,
    MultimodalProcessor,
    IterationBudget,
    ToolGuardrails,
    ToolCallNormalize,
    HeartbeatStaleness,
    HumanInTheLoop,
    HITLConfig,
    Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),  # system prompt + nudge + persistence
        MultimodalProcessor(),  # multimodal input normalization
        IterationBudget(90),  # per-turn call budget
        ToolGuardrails(),  # failure-pathology detection
        ToolCallNormalize(),  # tool_use/tool_result repair
        HeartbeatStaleness(),  # stuck-turn watchdog
        HumanInTheLoop(HITLConfig()),  # approval gates
        Summarization(  # context compaction (innermost)
            need_update_system_prompt=True,
            model=auxiliary_llm,
            main_llm_context_window=main_llm_max_tokens,
            trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
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
| `Summarization` | `main_llm_context_window` | required | `main_llm_max_tokens` |
| `Summarization` | `trigger` | required | see [Middleware Chain](#middleware-chain) |
| `Summarization` | `keep` | required | `("messages", 10)` (accepted but unused) |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | defaults above | defaults |
| `HumanInTheLoop` | `config: HITLConfig` | defaults above | defaults |
| `HeartbeatStaleness` | (defaults) | interval 1 min, idle 7 / in-tool 20 | defaults |
| `OutputRepetitionGuard` | (defaults) | 3 / 2 / 0.6 / 6 / 8 | defaults |
| `MaxTokensBoostMiddleware` | (env) | base: request `max_tokens` → `MAIN_LLM_OUTPUT_MAX_TOKEN` (8192) → 8192, cap 32768, 3 retries | defaults |
| `LLMRetryMiddleware` | `config: LLMRetryConfig` | `max_retries=3`, `base_delay=2.0`, `max_delay=60.0`, `jitter=0.3`, `stale_giveup_threshold=5` | defaults (+ `fallback_chain` from `FALLBACK_LLM_*`) |

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
│   ├─ wrap_model_call (outermost → innermost)
│   │   · ContextEngineHook  inject system prompt (request.override)
│   │   · IterationBudget  consume 1; terminal AIMessage when exhausted
│   │   · HeartbeatStaleness  raise HeartbeatTimeoutError if killed; else heartbeat_iter += 1
│   │   · LLMRetryMiddleware  breaker check; classified retry w/ backoff; fallback /
│   │                        content-filter / partial-stream-stub flag consumption
│   │   · Summarization  maybe compact history (non-LLM strategies + auxiliary LLM), anti-thrash counters
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
├── base.py                      # require_session_id / args_hash helpers
├── mixins.py                    # BeforeAgentHooksMixin / AfterAgentHooksMixin
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
├── llm_retry.py                 # LLMRetryMiddleware (+ LLMRetryConfig, FallbackCandidate, ContentFilterError)
├── max_tokens_boost.py          # MaxTokensBoostMiddleware (tool-call truncation re-call)
├── media_handlers.py            # per-media-type strategies for MultimodalProcessor
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard (re-exported by __init__.py)
├── repetition_detectors.py      # pure repetition-detection primitives
├── repetition_state.py          # session-scoped repetition state helpers
├── subagent_completion_drain.py # SubagentCompletionDrainMiddleware
├── summarization.py             # Summarization
├── summarization_components.py  # shared Summarization helpers (_FORCE_RECOVERY_KEY etc.)
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # this file (+ .zh / .ja / .ko variants)

agent/stream_repetition_guard_wrapper.py   # RepetitionGuardWrapper (lives outside this package)
agent/context_limit_guard_wrapper.py       # ContextLimitGuardWrapper (lives outside this package)
```

### Exports (`__init__.py`)

```python
from agent.middlewares import (
    Summarization,
    LLMRetryMiddleware,
    LLMRetryConfig,
    FallbackCandidate,
    ContentFilterError,
    OutputRepetitionGuard,
    MaxTokensBoostMiddleware,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# Shared helpers are exported as well: BeforeAgentHooksMixin,
# AfterAgentHooksMixin, require_session_id, args_hash.
```

