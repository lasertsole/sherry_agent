# AGENTS.md — sherry_agent

AI coding assistant guide for the EMA AI Agent (Sherry) project. Read this before exploring the codebase.

## Project

**EMA AI Agent** — a role-playing AI agent with long-term memory, multi-level subagents, and a task orchestration engine. Python 3.13 (uv) · LangChain/LangGraph 1.3 · Robyn backend · Nuxt 4 + Tauri 2 frontend · SQLite (WAL) storage.

## Quick Commands

```bash
uv run python -m server                              # start backend (127.0.0.1:8080)
uv run pytest tests/agent/middlewares -q -k "not llm_e2e"  # run middleware tests
uv run --no-sync python tests/run_tests_split.py     # CI test gate (3-process; group C also runs the perf guards)
uv run --no-sync pytest tests/perf -q -s             # hot-path perf guards: prints each measured growth ratio
uv run --no-sync pytest tests/ -m "not llm_e2e" --cov=agent --cov=server --cov=context_engine \
  --cov=pub --cov=runtime --cov=workspace --cov-report=term-missing   # coverage report (single process; use run_tests_split.py -- --cov-append for the gate grouping)
uv run --no-sync python scripts/check_docs_parity.py # four-language README parity gate
uv run --no-sync python scripts/check_doc_links.py   # Markdown dead-link + anchor gate
uv run --no-sync python scripts/dev/mock_searxng.py 8888  # throwaway SearXNG stand-in (the exercise-the-fallback instance)
uv run --no-sync lint-imports                        # import-linter contract check
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .  # lint + format
uv run --no-sync basedpyright agent/                 # type check
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm  # frontend tests
./scripts/verify_feature.sh                         # one-shot feature verification (client typecheck/unit/integration/dpdm + repo pytest gate + docs gates; --quick for client-only)
```

## Directory Structure

| Directory                            | Purpose                                                                                                                                                                              | Key entry point                                      |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| `agent/`                             | Agent core: middleware chain, tools, subagent system, checkpointer                                                                                                                   | `agent/core.py::built_agent()`                       |
| `agent/middlewares/`                 | Middleware pipeline (summarization, guardrails, HITL, intent, continuation, message_persistence)                                                                                     | `agent/middlewares/__init__.py`                      |
| `agent/tools/`                       | LLM-callable tools (taskflow, todolist, memory, subagent, file, search, ...)                                                                                                         | `agent/tools/__init__.py::build_main_tools()`        |
| `agent/tools/taskflow/`              | Task orchestration engine (DAG, budget, deadline, progress, board, step judge, expectation→actual closure, evidence collector)                                                       | `agent/tools/taskflow/config.py`                     |
| `agent/tools/todolist/`              | Session-scoped todo planning layer                                                                                                            | `agent/tools/todolist/service.py`                    |
| `agent/tools/subagent/`              | Multi-level subagent system (spawn/registry/announce/sweeper, completion judge, functional roles)                                                                                    | `agent/tools/subagent/spawn/core.py`                 |
| `agent/tools/pub_base/`              | Shared tool infrastructure (`BaseSQLiteRepository` for the three SQLite stores, path utils, skill usage, the SQLite evidence ledger/recorder)                                                                             | `agent/tools/pub_base/sqlite_store.py`               |
| `agent/wrapper/`                     | Graph-level wrappers (repetition guard, context limit) + pluggable registry                                                                                                          | `agent/wrapper/registry.py`                          |
| `agent/state_port.py`                | The transcript port: read the live message list / pending HITL interrupt, heal an interrupted turn (server calls this, never the checkpoint API)                                     | `agent/state_port.py`                                |
| `agent/trajectory_events.py`         | Agent-side producers of trajectory events (reaches the server-side ledger through `runtime.hooks`)                                                                                   | `agent/trajectory_events.py`                         |
| `server/service/trajectory_store.py` | The per-turn trajectory ledger: one row per tool call (start/end merged), plus turn/compact/error events                                                                             | `server/service/trajectory_store.py`                 |
| `config/`                            | Centralized configuration (paths, features TypedDicts, schema, settings)                                                                                                             | `config/__init__.py`                                 |
| `config/features/`                   | Per-object feature config (82 TypedDicts)                                                                                                                                            | `config/features/__init__.py`                        |
| `server/`                            | Robyn HTTP/WS backend (trigger → service → queue/DAO → utils)                                                                                                                        | `server/__main__.py`                                 |
| `context_engine/`                    | Memory engine (MesMemory SQLite + curator)                                                                                                                                           | `context_engine/store/db.py`                         |
| `workspace/`                         | Live persona files (gitignored; templates in `workspace/template/`)                                                                                                                  | `workspace/prompt_builder.py::build_system_prompt()` |
| `pub/`                               | Shared utilities (message pipeline, retry, validators)                                                                                                                               | `pub/func/message/`                                  |
| `models/`                            | LLM/model wrappers (main, reasoner, auxiliary, vision, embed, reranker)                                                                                                              | `models/LLMs/main_llm.py`                            |
| `runtime/`                           | Runtime state registers (`session/`; canonical keys in `state_keys.py`) + process-level services (`process/`, `lane/`) + dependency-inversion seams (`hooks.py`, `data_provider.py`) | `runtime/session/state_register.py`                  |
| `runtime/hooks.py`                   | Process-level callback registry — `agent`/`skills` resolve server-owned callables (auto-turn trigger, WS task table, skill scan) without importing `server`                          | `runtime/hooks.py`                                   |
| `runtime/data_provider.py`           | Prompt/skill-write provider registries (`PromptDataProvider`/`SkillWriteProvider`) — `workspace`/`context_engine` obtain agent-owned data without importing `agent`                  | `runtime/data_provider.py`                           |
| `runtime/lane/`                      | Process-level concurrency lanes (`Lane`/`LaneManager`/`lane_slot`/`LaneType`)                                                                                                        | `runtime/lane/core.py`                               |
| `tests/`                             | Mirror-structured pytest suite (markers: unit/integration/module/system/regression/perf)                                                                                             | `tests/run_tests_split.py`                           |
| `skills/`                            | SKILL.md skill system (builtin/auto/plugins)                                                                                                                                         | `skills/loader.py::scan_skills()`                    |
| `wiki/`                              | Four-language subsystem docs (published to the GitHub Wiki)                                                                                                                                                         | `wiki/long-running-tasks/README.md`                  |
| `wiki/context-governance/`           | Context governance docs (persistence, eviction, slices, overflow clip, summary filtering)                                                                                            | `wiki/context-governance/README.md`                  |
| `scripts/`                           | Dev tooling (git hooks, maintenance scripts)                                                                                                                                         | `scripts/hooks/`                                     |

## Architecture

```
User message → Robyn WS → agent.core.built_agent() graph
  │
  ├─ middleware chain (before_agent → before_model → LLM → tools → after_model → after_agent)
  │    system_prompt_injection (@dynamic_prompt) → WorkspaceNotice → ToolSelection → MultimodalProcessor → IterationBudget
  │    → ToolGuardrails → ToolResourceLock → ContextEviction(P0-2/P2-4) → ToolCallNormalize → PathGuard → SubagentCompletionDrain
  │    → TaskIntent(E7) → DynamicReminderMiddleware → OutputRepetitionGuard → MaxTokensBoost → ThinkingControl → HeartbeatStaleness → HITL
  │    (TaskIntent's intent detection is RULES-ONLY: four-language rules — EN/ZH/JA/KO
  │     keywords plus question/chat patterns, then a length fallback over 100 chars.
  │     The old auxiliary-LLM fallback for the 20-100 char request-phrasing window was
  │     removed by the over-engineering audit; a short JA/KO request with no keyword is
  │     no longer armed — the conservative direction, fail-open to "not a task")
  │    → MessagePersistence → LLMRetry → Summarization → InvalidToolCallRepair → PlanContinuationEnforcer(E3; gates the plan = todos + open TaskFlow flows; delivered through the auto-turn trigger, which DEFERS past the ending turn's own `ws_task`/`answering` busy signals — up to `TODOLIST_INFRA["continuation_self_idle_wait_s"]` (30 s) — because the enforcer's hook runs while that stream is still draining; a directive still refused at the cap means a real user turn took over and is dropped. The client cannot stream that turn (it sends no `turn_started`), so `use-chat-stream.ts` refetches the newest turn page on its `done` — without that, the continuation ran but stayed invisible until a manual reload. The gate is BOUNDED by a consecutive-continuation ceiling: `mark_injected` counts every CONFIRMED delivery (a `busy`/`already_pending` answer from the trigger counts as none and is retried next turn end), and at `TODOLIST_INFRA["max_continuation_turns"]` (15) the gate delivers one final `[SYSTEM DIRECTIVE: CONTINUATION BUDGET EXHAUSTED]` directive and latches the session silent until a turn it did NOT inject — a real user message, or a subagent completion — resets the counter and the latch)
  │    (InvalidToolCallRepair is registered LAST = the innermost wrap_model_call
  │     layer, closest to the raw model call: a tool call whose `arguments` fail
  │     JSON parsing arrives as `AIMessage.invalid_tool_calls` with `tool_calls`
  │     empty, and the loop — which routes on `tool_calls` only — used to treat
  │     the message as a call-less final answer and END the turn silently
  │     (SWE-bench 20261017_003529: the django instance ended after 14.7 s with
  │     zero edits). The layer repairs the syntax (quote-only, byte-identical
  │     tail value; refuses ambiguous shapes; truncation-shaped misses are left
  │     to MaxTokensBoost) and re-attaches the call with its original id so the
  │     already-streamed `tool_start` frame pairs with the result; when nothing
  │     is repairable AND no valid call remains it re-calls the model with a
  │     corrective HumanMessage appended to the REQUEST only, bounded by
  │     `INVALID_TOOL_CALL["resend_max_retries"]`, stripping streaming callbacks
  │     like MaxTokensBoost does. Repaired calls still pass every wrap_tool_call
  │     gate; the middleware is in `scaffolding._MAIN_REQUIRED` AND
  │     `scaffolding._SUBAGENT_REQUIRED` (children carry it innermost too — their
  │     chain has no LLMRetry, so the resend re-calls the child's model directly),
  │     and the attempt is logged + recorded as an `invalid_tool_call` event)
  │    (WorkspaceNotice is a before_agent node: once per turn it compares the
  │     session's effective root with PROJECT_DIR_ANNOUNCED and the root's git
  │     branch+HEAD with GIT_HEAD_ANNOUNCED — what the agent was last told about
  │     — and, when either differs, splices ONE HumanMessage notice per change
  │     immediately before the turn's HumanMessage. Several switches coalesce
  │     into the final state, switching back sends none, the first turn baselines
  │     both silently; the notices are HumanMessages tagged origin/internal/
  │     provenance (the chat renders them as neutral cards, and
  │     pub.func.message.workspace_notice.is_workspace_notice lets the human-
  │     message scanners skip them), and the human message keeps its LAST place
  │     for TaskIntent and ContextEviction. It replaced the old switch-time
  │     aupdate_state announcement, which fired per switch)
  │    (DynamicReminderMiddleware injects the LIVE views as
  │     `<system-reminder source="…">` messages once per turn's first model call:
  │     plan mode, working directory, project instructions, todo list, boulder,
  │     TaskFlow board, continuity, knowledge index — the catalogue in
  │     `agent/middlewares/reminder_catalog/` decides what, at which priority, with
  │     which cooldown and under a 30 000-char total budget (lowest priority
  │     dropped first). Priority is the BUDGET order; delivery reverses the kept
  │     set so the highest-priority view (plan mode, then the todo list) lands at
  │     the TAIL of the injected block, where recency bias gives it the strongest
  │     attention. They used to be REBUILT INTO THE CACHED SYSTEM PROMPT,
  │     which froze them until the next rebuild and made the prompt a bad cache
  │     citizen; the prompt now holds only stable content (persona + memory +
  │     skills index), each reminder is re-derived from live state every turn,
  │     and a compaction resets the throttle state so nothing is lost with the
  │     compressed history. The three older injectors (TaskIntent steering,
  │     todo-continuation directives, workspace notices) keep their own paths —
  │     the catalogue is an overlay, migration is batched.)
  │    (MessagePersistence flushes tool results the moment they return via
  │     wrap_tool_call; after_model nodes chain in reverse registration order, so it
  │     is also the first after_model hook — new human/ai/tool messages reach
  │     MesMemory before HITL rewrites denials or interrupts. HITL denials are the
  │     exception: its short-circuit bypasses the wrap layer and lands next boundary.
  │     ContextEviction wraps outside MessagePersistence: the raw result is
  │     persisted first, then replaced by an evicted-head/tail preview before it
  │     reaches state; read_file results are sliced, never offloaded. A payload
  │     the 5+5-LINE window cannot shrink (ten lines or fewer — a single
  │     newline-free line, i.e. a compact JSON body, is the extreme — has
  │     overlapping windows and would be reproduced twice) falls back to a
  │     CHAR-budgeted head/tail (per-line 2 048, per-side 8 192 — the mirror of
  │     the fd path's byte budgets), so it is offloaded like any oversized
  │     result instead of riding the window in full. Its
  │     before_model tags an oversized trailing HumanMessage (full text stays in
  │     state/MesMemory) and wrap_model_call truncates only the model view)
  │    ThinkingControl: per-session thinking control (client switch / level
  │     picker → PUT /sessions/thinking → state register); swaps request.model
  │     for the variant built by build_main_llm(thinking=... / level=...) when
  │     the session carries an explicit LLM_THINKING_ENABLED flag, else env
  │     default. Levels are DECLARATIVE since the pi-style rework: the value
  │     domain is the shared EXTENDED_THINKING_LEVELS enum (off / minimal / low
  │     / medium / high / xhigh / max), models/LLMs/reasoning_payload.py
  │     resolves a per-model thinkingLevelMap + thinkingFormat and CLAMPS the
  │     request onto the nearest level the model accepts, and GET
  │     /sessions/thinking returns the model's `levels` list so the client's
  │     picker never offers an unsupported level. A session can also carry a
  │     CUSTOM wire mapping (GET/PUT /sessions/thinking/custom,
  │     StateKey.CUSTOM_THINKING_MAPPING with the usual parked twin): validated
  │     at the API boundary (lowercase dotted wire_field, alphanumeric values,
  │     >=1 level, budgets positive and under the context window) and it
  │     overrides the declarative map outright — it rides into the variant AND
  │     into the memory review's cache-warm rebuild (the published descriptor
  │     carries it). Anthropic has an adaptive branch, Bedrock splits Claude
  │     from GPT, and the headroom scales per level via
  │     REASONING_BUDGET["thinking_budgets"]. The middleware also refreshes the
  │     frozen memory snapshot when the request configuration changes: a
  │     model switch starts a new prefix-cache namespace and a thinking switch
  │     was measured to zero cache_read on an identical prefix (DeepSeek,
  │     2026-10-22), so `before_model` compares the turn's (thinking key,
  │     profile fingerprint, custom-mapping fingerprint) against the last
  │     applied one — process-level because the graph is rebuilt every turn —
  │     and, on a change, reloads `MemoryStore.load_from_disk()` and deletes
  │     `StateKey.SYSTEM_PROMPT` from both registers BEFORE
  │     `system_prompt_injection` resolves the prompt. The switched turn's
  │     first model call therefore sees mid-session FACTS/MEMORY/USER writes;
  │     first turns, unchanged controls, and reload failures keep the cache.
  │
  ├─ tools: build_main_tools() → taskflow(14) + todolist(2) + memory + subagent(8)
  │         + file_tools + web_search + web_fetch + terminal + python_repl + question + ...
  │
  ├─ graph wrappers: apply_graph_wrappers(inner)
  │    → ContextLimitGuard(RepetitionGuard(graph))
  │
  ├─ lanes: runtime/lane/lane_slot() gates every dispatch point —
  │    MAIN(turn) / SUBAGENT(child) / NUDGE(memory) / NESTED(sessions.send)
  │
  └─ subagents: spawn_subagent_direct() → accepted as PENDING when the
       SUBAGENT lane is full → RUNNING inside the lane slot (`started_at`
       stamped there) → detached child agents → announce pipeline → drain

     spawn carries a per-spawn WALL-CLOCK timeout (`sessions_spawn`'s
     `run_timeout_seconds`, clamped by the tool to `[600, 7200]`; internal
     callers bypass the clamp and keep their own bounds), and after a
     timeout/kill whose completion notice has no Result the caller can pull the
     child's transcript — `sessions_messages` reads its checkpoint by
     session_key (`agent/tools/subagent/tools/sessions_messages.py`, token-
     budgeted, spawner-only)

     Checkpoint retention is latest-only per thread: a child's
     `_release_child_resources` prunes its thread to the newest top-level
     checkpoint + its writes (tools/model namespaces dropped), and a main
     `on_turn_finished` does the same for its session before the drain starts
     (HITL-pending sessions skip it). Deleting a main session cascades through
     every descendant run: suppress/kill, then delete each child's registry row,
     checkpoint thread, session dir, planning rows and attachments. After
     `init_registry()` a boot sweep deletes every orphan subtree whose root main
     session or parent run is gone, in one fail-open pass.

     Spawns are ROLE-CONTAINED: a specialized child (researcher / executor /
     reviewer / librarian) may only spawn its own functional role (same role =
     same tool allow-list; MAIN and general stay unrestricted), so a read-only
     researcher cannot hire a write-capable executor — the refusal names the
     way out and `functional_role` rides on the tool for a same-role fan-out

  completion gates (all always-on, all fail-open):
    · Tier 1 schema gate — a step's response_schema is validated on
      taskflow_resume (json parse + validate_structured_output); a miss is a
      retry signal ("schema_error") that never spends a judge call, and an
      unretried miss marks the step failed. The retry carries the validator's
      own message as its feedback (the judge never ran, so judge_feedback is
      empty) and the replacement inherits the step's spawn contract
      (_retry.step_spawn_kwargs → response_schema / functional_role /
      step_model / step_timeout_seconds — every dispatch site passes it,
      including the manual batch and the auto-dispatch wave). The shape is
      ALSO enforced at generation time: the child agent is built with
      create_agent(response_format=<schema>) (AutoStrategy: provider-native
      structured output, else a structured-output tool call), the schema
      text stays in the child's system prompt as the prompt-level fallback,
      and sessions_spawn exposes the same output_schema field so a
      tool-driven child can be constrained the same way
    · StepJudge — step_judge.py, on taskflow_resume for criteria-bearing steps
      (judge_criteria or validation_criteria; pass / retry within
      STEP_JUDGE["max_retries"] / block); a Tier 1 pass never skips it, and a
      caller-declared step_outcome outranks both
    · CompletionJudge goal loop — every spawn, bounded by
      COMPLETION_JUDGE["goal_max_turns"] and gated by its master switch
      (COMPLETION_JUDGE["enabled"]; off = no judge call at all, whatever budget
      a caller passes); continue injects a follow-up turn. The judge's FIRST
      call is skipped when the opening turn produced a result AND the child
      session's ledger shows a passing, non-stale row — the ledger already
      carries the verdict a simple spawn would otherwise pay a model call for
    · Evidence ledger — always-on terminal/python_repl auto-record + file-edit
      stale events in SQLite (WAL; lazy retention + session/boot purge); a write
      also broadcasts a `scope="cross_session"` stale row to every OTHER session
      whose evidence command names the path (gated by
      EVIDENCE_CROSS_SESSION_STALENESS; the writer's own session is marked
      separately); at taskflow_finish the regression gate replays the flow's
      passed verification commands once (allowlisted, no shell, capped) and
      writes broken / unmark events, so a cross-file regression is confirmed
      while a false-positive stale is cleared; read side derives
      broken/stale (evidence_collector.py)
    · taskflow_finish gates A–C — DAG completeness, no unresolved steps
      (blocked/failed/skipped/cancelled are reported by id with their reason),
      the flow's AGGREGATED evidence (every dispatched step's child session +
      the creator's; rows carry a session key, never a flow id) carrying no
      FAIL/[stale]/[broken], then Gate E's goal judge (only for a flow with a
      description and at least GOAL_GATE["min_steps"] = 3 steps)
    · SubagentCompletionDrainMiddleware — unconditional programmatic gate on
      drained completion carriers when the session lacks passing evidence
```

## Queued User Input (`server/service/input_queue_service.py`)

A message sent while the session is busy is persisted as a `QUEUED` row in
`user_input_queue` (SQLite, `server/queue/user_input_queue.py`; cap =
`INPUT_QUEUE["max_active_per_session"]`, dedup by `client_msg_id`) and answered
by the `queued` WS frame with its FIFO position. The composer says so: while a
turn runs, `inputBox.vue`'s placeholder switches from 请输入内容 to
`chatInput.queueHint` (继续输入消息以排队 — four languages in the component's
inline block), and that hint is skipped when the box is disabled (a HITL prompt
takes the input slot; the disabled text wins). The turn runner drains those
rows **one per turn** (`server/service/turn_runner.py::_drain_loop` claims a
single row via `claim_next` and `_execute_single` drives it), so N queued
messages produce N turns and N replies — one answer per user bubble — never one
merged answer. `on_turn_finished` kicks the drain; a HITL-pending session drains
nothing until the resume completion.

Each queued row is manageable from the client (`cancel_queued` / `edit_queued` /
`send_now` frames → `queued_cancelled` / `queued_updated` / `send_now_ack`
`{event, session_id, msg_id, ok}` acks): cancel voids a `QUEUED` row (a
`CLAIMED` one is already inside a turn and is refused), edit rewrites only a
`QUEUED` payload, and `send_now` re-stamps `created_at` below the session
minimum **before** cancelling the running turn — the cancel-triggered drain then
claims the prioritised row first (`claim_next`'s `ORDER BY created_at` stays the
single ordering rule; there is no priority column).

## Chat Selection Quote & Side Session (`ChatBox.vue` / `SideChatPanel.vue`)

Right-clicking a **valid text selection inside one message row** opens a PrimeVue
`ContextMenu` anchored at the pointer. A right-click with no selection (or on a
control) does NOT call `preventDefault`, so the browser's native copy menu still
works.

- **Guard** — `client/app/composables/use-chat-selection.ts::inspectSelection(root)`
  returns `null` for a collapsed/empty selection, more than
  `MAX_SELECTION_CHARS` (8 000) characters, a selection outside the chat list,
  one starting in `button/input/textarea/select/[contenteditable=true]/
  [role=dialog]/[role=menu]/[data-hitl-card]/.p-contextmenu`, or one whose
  common ancestor has no `[data-message-id]` (a selection spanning two rows).
  `ChatBox` adds `data-message-id` / `data-message-role` to each message row;
  `selection-enabled` is false while a HITL card owns the input (the page passes
  `!hitlRequest`), and `allow-side-chat` is false inside the side panel.
- **添加到当前会话** — `ChatBox` emits `selection-add`; the page calls
  `inputBox.addQuoteChip(text, sourceLabel)` →
  `use-text-selection.ts::insertQuoteChip` inserts an atomic
  `<span contenteditable="false" class="text-quote-chip" data-quote-text="…">`
  (the label is a 40-char preview). `extractTextForSend` now turns that chip into
  a markdown blockquote (`> line` per line) and never leaks the preview; the
  file-mention branch is unchanged and still extracts `@path`.
- **在辅助会话中提问** — `createSideChatSession` (`use-side-chat.ts`) mints an
  ordinary child session (NOT a subagent), copies the parent's preset binding,
  locked character snapshot, agent config and PROJECT DIRECTORY, caches its
  placeholder meta and announces it via `session:created`, then opens a
  session-scoped `sideChat` right-sidebar tab whose payload carries
  `{sessionId, quote, source}`. The panel reads that payload on mount (no
  mitt race) and prefills the quote chip. `SideChatPanel.vue` reuses the page's
  lifecycle / draft-persistence / chunk / HITL / chat-stream slices over the
  child's own persistent socket, history and approvals; it has a compact
  question/plan/generic approval card. Closing the tab does not delete the
  session — it stays in the normal session list, so its history is recoverable
  (the plan's "hidden side session" was traded for that recoverability).
- **i18n** — menu and chip labels are `chat.selection.*` (central locales); the
  side panel's HITL copy mirrors `[sid].vue`'s inline `hitl.*` block.

## Voice Input (`use-voice-input.ts` / `server/service/audio_transcribe_service.py`)

The composer's mic button (`inputBox.vue`, in the `sendActions` cluster) records,
transcribes and inserts the text at the caret — the whole flow lives inside
`inputBox.vue` (`insertVoiceText`, exposed beside `addQuoteChip`), so `[sid].vue`
and `SideChatPanel.vue` get it without wiring. **Capture** is `MediaRecorder`
(mime picked from an `isTypeSupported` chain: webm/opus → ogg/opus → mp4) with a
300 s auto-stop cap and a `<0.3 s` refuse; the blob is then decoded by the SAME
browser that recorded it (`AudioContext.decodeAudioData`) and re-encoded to a
16 kHz mono PCM16 WAV (`encodeWavPcm16` / `resampleLinear` / `mixdownToMono`).
The WAV detour is deliberate: the STT daemon's decode chain (soundfile →
audioread) cannot read webm/opus and the packaged backend ships no ffmpeg,
while a browser always decodes its own container — the server keeps zero
decode dependencies. **Transport** is one gateway proxy over the local FunASR
SenseVoice daemon (`skills/builtin/core/speech_to_text/scripts/server.py`,
`127.0.0.1:9011`, file-path contract, no CORS/auth — unreachable from a
browser): `POST /audio/transcribe` parks the upload under `TEMP_DIR`, drives
the daemon through the skill client's public aliases (`daemon_alive` /
`spawn_daemon` — one spawn implementation, one `.stt_daemon.pid`), and unlinks
the file; `GET /audio/transcribe/status` reports
`{model_dir_present, daemon_ready}`. **Availability is the weights**:
`MODELS_DIR/STT_model/model_weight` on disk gates the button (there is no
auto-download and the `.env` `STT_*` keys have no consumers), while
`daemon_ready: false` is just "cold" — the daemon spawns on first use, so the
route answers a cold start with `503 {warming: true}` (the client retries for
~60 s) and a missing deployment with `503 {unavailable: true}`. `null`
availability (backend unreachable) keeps the button clickable so the first
attempt surfaces the real error. Insertion failures never touch the box's
content; `chatInput.voice*` keys cover the four languages.

## Project Directory Binding (`runtime/session/project_dir.py`)

Every session has a **project directory**: the root its tools resolve relative
paths against. Precedence is session binding → `SHERRY_PROJECT_DIR` →
`sherry.jsonc`'s `project_dir` key → `ROOT_DIR`, so with nothing configured the
behaviour is byte-for-byte the old behavior. `PUT /sessions/project` binds (or
clears, with `null`) the session's own value; a choice made while a turn is
running is PARKED (`PROJECT_DIR_PENDING`) and promoted by
`turn_runner.on_turn_finished` at the turn boundary — the same park/promote pair
the thinking/model controls use, including the HITL deferral.

The agent is told about a change **at send time**, not at switch time:
`WorkspaceNoticeMiddleware` (`agent/middlewares/workspace_notice/`) runs one
comparison per turn over two facts — the effective root against
`StateKey.PROJECT_DIR_ANNOUNCED`, and that root's git branch + HEAD against
`StateKey.GIT_HEAD_ANNOUNCED` (`<branch>@<short-hash>`, one bounded
`git rev-parse`; a non-repository root reads as "nothing to announce") — and
splices ONE notice immediately before the turn's HumanMessage per fact that
differs. Any number of switches between two messages coalesce into the single
notice naming the FINAL state, a switch back to the announced state sends
nothing, and the first turn of a session only records the baselines (silently).
Both values are mirrored durably and re-warmed at boot
(`prime_mem_from_store()` / `prime_git_head_from_store()`), so a restart cannot
lose them. A directory switch re-bases the git token in the same turn and words
its notice as "the working directory moved to X, whose repository is on Y"
(the old token belonged to another repository). The notices are
**HumanMessages** — the injected-carrier role — carrying
`metadata={"origin": "project_dir"|"git_head", "internal": True,
"provenance": "workspace_notice"}`: the message store keeps that origin on the
row (both row builders), and the chat renders a non-`user` origin as a neutral
system card (labels 项目目录切换 / 分支切换, folder and branch glyphs) instead of a
user bubble. Because they are human rows, the scanners that walk human messages
skip them through `pub.func.message.workspace_notice.is_workspace_notice`:
`split_into_turns` (a notice is not a turn boundary), HITL's
`_is_headless_turn` (a trailing notice does not make a turn operator-less), and
the summarizer's serializer (labelled `[System notice]` /
`system`, never `[User]`).

The read rule that keeps this working: **resolve the root per call, never at
construction time.** Tool objects are process-level singletons
(`agent/core.py`), so a captured root would freeze every session onto the first
caller's directory; and a session can switch at a turn boundary, so even a
per-turn cache would go stale after a HITL resume. The single accessor is
`agent.tools.pub_base.session_workspace_root(session_id)` feeding
`resolve_workspace_path(path, root)`; `PathGuard`, the four file tools,
`terminal`, `python_repl`, `ptc` and the code-intel runners all read it (the
guard also moves its boundary, keeping its policy: in-root passes, only the
hard-deny floor rejects outside it).

Derivation: a subagent inherits the parent's binding at spawn
(`resolve_spawned_workspace_inheritance`) and the value is persisted as the
CHILD session's own binding, so a child is frozen to the root it started with
and does not follow a later parent switch. `sessions_spawn(cwd=...)` overrides
it. The prompt gains a `## Current Working Directory` block naming the effective
root, and marks an unbound session with 未绑定项目目录 instead of quietly
pointing the model at the sherry checkout.

**The root is a small project-level config surface.** `.sherry/` is the ONE
hidden entry sherry creates inside a project:

```
<project>/.sherry/
├── codeintel/index.db    # per-project code index (was .codeintel/)
└── shadow-git/           # per-project write snapshots
```

It is created LAZILY by `runtime/session/project_dir.py::maybe_create_sherry_dir`,
called from `WorkspaceNoticeMiddleware` before it records the announced baseline:
only when `project_dir_source(session_id) == "session"` (a process default never
counts), the root is the first announced one or differs from it, and `.sherry/`
does not exist. Binding alone creates nothing; the first MESSAGE does. It creates
the shell only — FACTS.md stays global, see below. An existing shell is never
recreated, and a legacy `.sherry/memory/FACTS.md` from the short-lived
per-project experiment is left untouched but is not read. The code index and
shadow git create their own subtrees on first use, so a missing `.sherry/` is
never an error. `CODE_INTEL_DIR` (the ast-grep binary cache under SHERRY_HOME)
and `.sherry-tmp-*.swp` (same-filesystem atomic replace) are deliberately NOT
part of this directory.

**Project instructions**: the builder in `workspace/prompt_builder.py` reads
`<root>/AGENTS.md` and falls back to `CLAUDE.md`. It is
registered as the `project_instructions` reminder source
(`agent/middlewares/reminder_catalog/sources.py`, priority 62 between continuity
65 and the directory notice 60, `budget_chars` 8 000, `only_if_changed=True`) —
never the cached system prompt, so a large file cannot invalidate the provider
prefix cache and a directory switch automatically re-reads the new project's
file. No nested traversal (git root → cwd) yet, no file creation, no
`.gitignore` edits.

**FACTS.md is ONE global, system-level file.** All three memory files live in
`workspace/memory/` (`MEMORY.md`, `USER.md`, `FACTS.md`) and are loaded by the
boot-time `MemoryStore.load_from_disk()`; there is no per-project FACTS path and
`runtime/session/context.py` no longer exists. FACTS holds only SYSTEM-LEVEL /
CROSS-PROJECT pitfalls — things true in ANY project on this machine (toolchain
behavior, environment limits, generic command gotchas), not project conventions
(those go to MEMORY.md or the project's AGENTS.md). The memory review and plan
extraction prompts state this routing and instruct the model to migrate a
project-specific entry out of FACTS into MEMORY when it spots one; the 1 375-char
oldest-first rollover still bounds the single file. Because nothing is
per-project, a directory switch no longer invalidates the cached system prompt
for FACTS, and `GET/PUT /memory` reads/writes all three files globally without a
`session_id`. A legacy `<project>/.sherry/memory/FACTS.md` is ignored (left on
disk, never rewritten) — it is not migrated automatically.

The external-path HITL gate agrees with that root: `resolve_external_path`'s
first check is "inside `ROOT_DIR` **or inside the session's project directory**"
(state lookup only on the non-repo branch, so the repo fast path stays
state-free). Selecting a project directory therefore exempts it and every child
path from "external file" approvals — including when a caller arrives with the
process default as its root (cold mem tier right after a restart) — and the
approval prompt names the session's root, not the checkout.

File browsing (`GET /project/tree`, `GET /project/file`,
`server/service/project_files_service.py`) is read-only and hard-refused to the
session's root: `resolve_within(base, path)` runs the same gate order as the
tools, reads go through `_open_no_follow` (TOCTOU), and `FILE_BROWSER` bounds
size (413), non-UTF-8 (415), depth and entries per level. It deliberately does
NOT use the agent's `resolve_external_path` HITL flow. The client shows the chip
(`ProjectDirectoryChip`), the lazy tree (`ProjectFileTree`) and the viewer tab
(`FileViewerPanel`, a SESSION-scoped tab: a file of the session's directory belongs
to that session, so it opens under 当前会话 like the commit diff). The tree stays
READ-ONLY but its rows are actionable (ZCode's workspace-file-tree parity): one
`<ContextMenu>` per tree (rows emit their right-click upward — a per-row popup
would mount one hidden menu per file) offers Open / Open with (viewer, plus the
system default app on the desktop build) / Open in the built-in browser (HTML
files; the page is served from the read-only endpoint as a blob URL, so relative
references inside it do not resolve) / Reveal in the file manager (desktop only,
`@tauri-apps/plugin-opener`; the label follows the platform, and the web build
hides the entry because a browser cannot reach the OS) / Copy absolute path /
Copy relative path / Add to chat. A row is also `draggable`, and a file-viewer
TAB is too: both publish a `text/x-sherry-file` JSON payload plus the plain-text
`@./path`, and the composer's drop handler turns either into a mention chip
(`use-file-mention.ts`: an atomic `<span contenteditable="false">` whose visible
label is the icon + file name while `data-file-path` holds the `@path` the send
extracts — `extractTextForSend` also feeds the draft model, so chips never leak
their label into a message). "Add to chat" travels the app-wide mitt bus
(`chat:file-mention` with the session id) from the left sidebar to the chat page,
which relays it to the composer it owns; the agent learns the convention from a
stable `## File References` block in the system prompt. The sidebar's 工作目录 body stacks TWO collapsible sections —
文件树 and the read-only **Git Graph** (`GET /git/graph`,
`server/service/git_graph_service.py`; `config/features/infra_side/git_graph.py`
bounds the page size, the served text and the git timeout): one
`git log --all --date-order` page (newest first, `limit`/`skip`, one extra commit
to report `has_more`; the panel appends the next page when the list is scrolled
near its end — there is no load-more button, and a page that fails offers a retry
in the same row) plus the current branch and a capped dirty count, with
refs parsed to `head`/`branch`/`tag`/`remote` chips. A directory that is not a
repository (or a host without git) answers `available: false` with a reason
instead of an error, so the panel renders its own empty state. `GitGraphPanel.vue`
lays the commits into lanes and draws one SVG per row (merges and HEAD get their
own node shapes), and it is the client's only consumer of that route. The two
sidebar section headers (文件树 / Git Graph) share one row.
The row's context menu offers 回退/切换 — `POST /git/reset` (`soft`/`mixed`/
`hard` only) and `POST /git/checkout`, both behind the client's confirm dialog,
with git's own stderr surfacing as a 409 — and clicking a row opens a DRAWER of
the files that commit touched directly under that row (an accordion, never a block
parked at the end of the list — a click elsewhere in that drawer replays the row's
own contextmenu, so the SAME action menu opens anchored at the row's right edge; `GET /git/commit`: metadata plus `--name-status -M -z`,
NUL-framed so a non-ASCII path is never quoted and the entry lookup behind the
diff still matches). Clicking one of those files opens its diff in a right-sidebar
tab (`GET /git/commit/file`, `GitDiffPanel.vue`, one tab per file): both sides come
from the commit's own blobs (`<parent>:<old_path>` / `<hash>:<path>`) and are aligned
by `difflib` opcodes, so a change reads as remove-left / add-right (red / green
tints), an added or deleted file is a SINGLE column, a rename follows its old path,
and a binary or oversized blob (2 MB / 4000-row caps) is flagged instead of decoded.
A path that is not part of the commit is a 404, never an empty diff. On the
client side every git call THROWS on failure (`bridge/git.ts`): the two write
actions go through the raw transport so a refusal reaches the toast with git's
own reason (a checkout blocked by a dirty tree names the files), and a failed
read rejects rather than resolving the shared client's null payload — a null
assigned to the panel's page threw on every later render, which left the panel
visibly dead (no row expansion, no branch switch) until a reload. The header's
branch label is also the **branch switcher** (VS Code's graph has the same
dropdown): clicking it lists the page's `branches` — local refs, most recently
committed first, clipped by `GIT_GRAPH["max_branches"]` — with the current one
ticked and inert; picking another checks it out (`POST /git/checkout`, no
confirmation, matching the ref-chip menu, and git itself refuses a checkout that
would clobber local changes). A switch made here (or in a terminal, or a
commit/reset outside the session) reaches the AGENT as the git notice described
under Project Directory Binding: the next turn's transcript gets one
`git_head` card before the human message.

The taskbar's **hammer button opens a two-entry toolbox** (浏览器 / 终端) as the
same dialog shape the settings menu uses, and both entries open a right-sidebar tab
in the SESSION group PER CLICK (a fresh `instance` key in the tab payload — the
strip numbers the twins 浏览器 1 / 浏览器 2, and each panel keeps its own history /
scrollback under `<sessionId>::<instance>`); the settings entries keep deduping on
their empty payload (`toolboxTools` in `pages/home/config.ts`, wired through the
same `HOME_TOOLBAR_EVENTS` registry as the settings grid — a pinned test keeps the
grids and the commands in step). The browser is an address bar over an iframe with
a per-session history (back / forward / reload; sites that refuse framing stay
blank, which the empty state says), a **free-size mode** that emulates a mobile
device viewport (resizable frame with edge/corner handles), and 打开调试工具,
which opens the page in a real window because an iframe cannot hand out devtools;
the terminal is a console for commands the
OPERATOR types: `GET /terminal/info` names the directory (the session's project
directory — the selected 工作目录 wins) and `POST /terminal/run` executes one line
through `/bin/sh -c` there with a scrubbed environment, bounded by
`TOOLS_TIMEOUTS["user_terminal_timeout_seconds"]` and an output cap, answering the
exit code instead of raising. It is a one-shot console, not a PTY — an interactive
full-screen program has nothing to attach to — and it is NOT the agent's
`terminal` tool (that one keeps its HITL gate and sandbox wrapper; this one is the
human's own shell, loopback-only via the gateway). The browser panel's address bar
suggests the persistent VISIT HISTORY: every navigation (iframe mode from
`stores/toolbox.ts::navigate`, CDP mode from the `page` frame the server reports —
the devtools frontend is excluded) records one row in the Dexie `browserVisits`
table (`composables/use-browser-history.ts`, schema version 3), so the list
survives a reload and an app restart. A repeat visit inside 30 s UPDATEs the
newest row instead of appending (CDP reports several frames per navigation), the
table is capped at 500 rows (oldest pruned), and the dropdown
(`BrowserHistoryDropdown.vue`) matches URL or title case-insensitively, newest
first, showing a favicon (hidden when it 404s), the relative age in the active
language and a 当前会话 mark, with a clear action for the whole history.
Both panels keep their state in
`stores/toolbox.ts` because the sidebar unmounts an inactive panel.

Choosing a directory is one action with two runtimes: the desktop build (Tauri)
opens the OS folder dialog (`app/utils/project-directory.ts` →
`@tauri-apps/plugin-dialog`), the browser build opens the in-app picker
(`ProjectDirectoryPicker.vue`) — a browser cannot hand out an absolute path, so
there is no typing channel. That picker is the one deliberate exception to the
in-root rule (`GET /system/dirs`, `server/service/system_folders_service.py`):
it lists the direct **subdirectories** of one absolute path (names only, no
files, no contents; `FILE_BROWSER` caps the level) and sits behind the same
gateway auth/CORS/CSRF middleware as every other route. The toolbar's files
button (`pages/home/index.vue`) exists only while a session is open AND its
project directory is bound — the tree it opens is scoped to both, so an unbound
session shows no control at all (nothing to press, no empty picker) — and the
persisted sidebar body falls back to the session list whenever the button goes
away (last session closed, binding cleared).

## Packaged Desktop App (Tauri) (`client/src-tauri/`)

A release is TWO artifacts. `pnpm tauri build` (client/) produces the
**installer** — the static SPA plus the Rust shell, ~30 MiB, no Python — and
`packaging/build_backend.py` produces the **backend tree** (PyInstaller onedir,
~1 GiB), which ships as the per-platform GitHub release asset the shell downloads
on the app's first launch. `.github/workflows/desktop-build.yml` builds both on
ubuntu/windows/macos (artifacts for a push to `main`, a GitHub Release for a `v*`
tag); the split is forced by GitHub's 2 GiB release-asset cap, which a bundled
backend exceeds several times over.

Release hygiene is a build gate: `packaging/check_release.py` rejects the repo's
`tests/`, `evals/`, `wiki/` and `wiki/` trees (plus frontend `*.test.*` /
`*.spec.*` files) in BOTH halves, and `build_backend.py` runs the backend half
itself before the tree is compressed. `.dockerignore` applies the same boundary
to the server image. The wiki/wiki trees are also outside the pre-commit hooks
and the code CI jobs (docs parity/link gates run in their own job); see
`.github/workflows/ci.yml`.

- **Frontend**: `nuxt generate` (`pnpm build:desktop`) writes `.output/public`
  — the `frontendDist` a bundled app can serve from disk (the `nuxt build`
  output needs a Nitro server and is NOT it).
- **Backend build**: `packaging/build_backend.py` runs PyInstaller **onedir**
  into `client/src-tauri/sherry-backend/` (per-OS; PyInstaller cannot
  cross-compile), writes the `VERSION` marker (the `tauri.conf.json` version —
  what `package_info()` reports and the value the shell compares) and the
  archive is built with the tree's CONTENTS at its root. The entry
  (`packaging/backend_entry.py`) imports `server.__main__` STATICALLY and calls
  its `main()` — a `runpy.run_module` is invisible to the freezer, which is how
  the first build shipped without the `server` package at all. The Linux job
  swaps PyPI's CUDA-pulling `torch` for PyTorch's CPU-only wheel (~5 GiB saved
  — the difference between fitting the asset cap and not); macOS/Windows wheels
  are already CPU-only.
- **Which backend runs**: `services/backend_downloader.rs` owns the first-run
  flow — `<app_home>/sherry-backend/sherry-backend` present _and_ its `VERSION`
  equal to this build's → spawn it straight away; otherwise download
  `sherry-backend-<os>-<arch>.tar.gz|.zip` from the latest release
  (`EMA_BACKEND_URL_BASE` overrides the base URL) on a background thread, unpack
  into a staging dir with the system `tar`, swap it in, then spawn it. Tauri
  events (`backend-download-started` / `-progress` / `-complete` / `-error`,
  plus the `backend_download_status` pull and the `retry_backend_download`
  command; `use-backend-download.ts` + `BackendDownloadOverlay.vue` render it,
  and the SPA reloads once the backend is up). The whole path is gated on
  `cfg!(dev)` being FALSE: a dev build (`pnpm tauri dev`, a plain `cargo build`)
  neither ships nor downloads a frozen backend, and spawns `python -m server`
  from the checkout when `EMA_AUTO_START_BACKEND` asks for it.
- **Never symlink the backend tree**: while the backend was a
  `bundle.resources` entry, `tauri-build` copied it into `target/release/` at
  BUILD time with `std::fs::copy`, which truncates the destination before
  reading the source — a `target/release/sherry-backend` symlink pointing back
  at the source made that copy empty every file (observed: 7,634 zero-byte
  files in one build). The resource entry is gone (the downloader replaced it),
  so that copy no longer runs; `packaging/build_backend.py` still removes a
  leftover symlink of that name.
- **`SHERRY_HOME` is the writable root** (`config/path.py`): the shell sets it
  to the OS app-data dir, and every writable path derives from it — `src/`,
  `workspace/` (sessions, memory), `temp/`, `logs/`, `models/`, `.codeintel/`,
  `.env`, `.models` / `.network` (gitignored; seeded from their templates),
  `sherry.jsonc`, and the WRITABLE half of the skills tree
  (`USER_SKILLS_DIR`, i.e. `auto/` + `plugins/`, with the loader scanning both
  it and the shipped `skills/builtin`). The downloaded backend tree lives there
  too — the installed app is read-only and is replaced by updates. Unset (a
  checkout, a server, CI) every
  path stays exactly where it was, so nothing changes for existing installs; a
  first run seeds `.env` (from the tracked file) / `.models` / `.network` (from
  their `.example` templates) and `sherry.jsonc`. Locations
  reported to the UI keep the CHECKOUT shape (`./skills/auto/…`) whichever home
  they live in (`app_relative` / `resolve_app_path` map between the two).
- **First run without a model**: the boot's 128K token gate aborts a SERVER,
  but `SHERRY_DESKTOP=1` (set by the shell) lets an UNCONFIGURED desktop app
  serve anyway, so the window can open and 模型配置 can write the files; a
  CONFIGURED-but-invalid value still aborts. The backend must be restarted
  (relaunch the app) after configuring — the runtime reads the file at boot.

## Configuration Files (`.env` / `.models` / `.network`)

The backend's user configuration is split by sensitivity into three project-root
files (`APP_HOME`, so a packaged app keeps them in its app-data dir):

- **`.env`** — TRACKED in git. Non-secret application configuration only
  (`SANDBOX_*`, `SHERRY_GATEWAY_*`, `SHERRY_AUTH_*`). Never put a key here.
- **`.models`** (gitignored; template `.models.example`) — the model families:
  provider / endpoint / key / local-model switch for main, auxiliary, TaskFlow
  decisions, compression, reasoner, ITTT, VTTT, TTI, reranker, embedding and
  STT, plus the scanner's LLM selection. Loaded by
  `config/models_settings.py`.
- **`.network`** (gitignored; template `.network.example`) — network services:
  `TAVILY_API_KEY` + the `SEARXNG_*` backends, the `SHERRY_BROWSER_*` switches
  and `LANGSMITH_*` tracing (moved out of `sherry.jsonc`, which is tracked and
  must not hold a key). Loaded by `config/network_settings.py`.

Both files are JSON5 (flat `"KEY": value,` lines; comments and trailing commas
allowed). The loaders SEED a missing file from its template, overlaying the
values still found in a legacy `.env` (and, for the tracing keys, in
sherry.jsonc's old `LANGSMITH` object), then `hydrate` every key into
`os.environ` so the existing readers (`os.getenv`, the import-time feature
snapshots, subprocess scrubbing) are unchanged. Precedence is **explicit process
variable → `.models` / `.network` → legacy `.env` → registry default**: the
hydration skips an already-set key, and `config.path` hydrates BEFORE it
(re)loads the environment file — `config.path.load_env()` is the single reload
seam (bare `load_dotenv(..., override=True)` calls were replaced by it).

Two OPTIONAL internal families are configured here too, both system-level (they
are deliberately NOT part of the per-session agent config):

- `TASKFLOW_LLM_*` — the TaskFlow decision model. It serves the four internal
  calls `taskflow_plan` / `taskflow_replan` / `step_judge` / `taskflow_finish`
  (temperature 0). Non-taskflow callers stay on auxiliary: the subagent
  completion judge, the spawn/steer branches and plan extraction.
- `COMPRESSION_LLM_*` — the summarization middleware's own model: the LLM
  summary (`_create_summary` / `_acreate_summary`). It does NOT serve memory
  review (the MAIN model, cache-warm by design) or plan extraction (the
  auxiliary model).

Both are built by `models/LLMs/family_llm.py::build_family_llm` (exposed as
`build_taskflow_llm` / `build_compression_llm`): an empty provider + api_base
falls back to `build_auxiliary_llm()` (an existing deployment is unchanged), a
remote configuration builds the same client shape as auxiliary (bounded
timeout / retries / per-provider max-output), and `*_MODEL_LOCAL=true` reuses
the auxiliary GGUF — there is no second local model to download. A configured
`*_MAX_TOKEN` must be >= 131072 (128K) and `*_MAX_OUTPUT_TOKEN` empty or
>= 8192 (8K): the save path refuses a smaller value and the boot/runtime gates
abort, exactly like the main/auxiliary keys. The 模型配置 tab renders both
families as model-profile cards, whose local entry reports the aux family's
download progress; a pre-existing `.models` gains a wholly missing family block
on import (`ensure_models_config_file`), because the panel only renders keys the
file contains. A configured-but-unbuildable remote model (unknown provider)
logs a warning and falls back to auxiliary instead of stranding TaskFlow or
compression.

Drift is refused, not merged: `server/service/env.py` treats the moved keys as
`SPLIT_OUT_KEYS` (the two registries are the source of truth) — a legacy `.env`
line is neither shown by `GET /env` nor accepted by `PUT /env` (the error names
the file to edit). `GET/PUT /models-config` and `GET/PUT /network-config`
(mirroring `/sherry-config`) are the writers; the 模型配置 tab merges the three
sources and routes each group's save back to its own endpoint, while the
模型联网 tab writes Tavily / SearXNG through `PUT /network-config`.
`.env` no longer gets a `.env.bak` (it is tracked — git is the backup);
`.models` / `.network` keep one.

The 模型配置 panel's built-in local entry (embedding, reranker, ITTT / VTTT,
auxiliary) prefetches the same GGUFs the runtime would download lazily, with a
live progress bar. `config/features/infra_side/model_downloads.py` (`MODEL_DOWNLOADS`)
lists each family's files and their `model_weight` target; `models/downloader.py`
does the HEAD size probe plus a streaming `.part` write and atomic replace;
`server/service/model_downloads_service.py` runs one background worker per
family (HEAD/no-Content-Length files report bytes rather than a bogus percent,
an ITTT/VTTT GGUF already under `auxiliary_llm` is copied locally instead of
re-downloaded) and `GET /model-downloads/status` / `POST /model-downloads/download`
(family in the body) expose it. Live progress is PUSHED, not polled:
`server/service/model_download_events.py` sends `model_download_updated` through
`runtime/session/ws_push.py::broadcast_to_all` to every live socket (a download
is process-level, not session state), throttled per family by
`MODEL_DOWNLOADS["progress_push_interval_s"]` (0.25 s) with state transitions
forced, and the worker thread schedules onto the loop captured by the start
handler. The panel takes ONE status snapshot when opened or when a socket
reconnects, then applies frames; there is no polling interval. The code
embedder publishes the same event with `kind: "code_embedder"` and keeps its own
`/code-embedder/*` snapshot pair.

## Compression Thresholds, Microcompact & Output Caps (ZCode/PI parity)

- **Per-model output caps** — every chat family carries its own
  `*_MAX_OUTPUT_TOKEN` in `.models` (main / auxiliary / TaskFlow / compression /
  reasoner) beside the context-window keys. Empty means "do not send a
  field" (provider default); a set value must be >= 8192 (8K) and is refused at
  boot, at `built_agent()`/spawn validation, at model build and at the
  `/models-config` save (M9). The 模型配置 panel labels it 最大输出窗口 and every
  model category title carries a help mark describing its purpose/use cases
  (four languages). `models/LLMs/provider_max_output.py` maps the
  provider to the kwarg `init_chat_model`'s class accepts — `max_tokens` for
  OpenAI-compatible providers, Azure and Anthropic; `max_output_tokens` for
  Google Generative AI / Vertex; an unknown gateway gets no field (M4). When
  thinking is enabled the budget headroom is based on the configured cap, not
  the legacy `MAIN_LLM_OUTPUT_MAX_TOKEN`.
- **Sampling-time repetition penalties** — every chat family carries a
  `*_FREQUENCY_PENALTY` / `*_PRESENCE_PENALTY` pair in `.models` (main,
  auxiliary, TaskFlow, compression, reasoner). Empty uses the global
  `REPETITION_PENALTY` defaults (0.3 / 0.2); a set value must be a finite number
  in [-2, 2] and is refused by `/models-config` otherwise.
  `models/LLMs/provider_repetition_penalty.py` maps it per provider:
  OpenAI-compatible and Google get both fields, Ollama additionally gets its
  native `repeat_penalty` (1.1), and Anthropic / Bedrock / an OpenAI reasoning
  model (o-series / gpt-5, whose API rejects the parameter) / an unknown
  gateway get none (M4). The 模型配置 panel renders each pair as a number input that
  rejects non-numeric keystrokes and clamps to the range; this is preventive,
  and `OutputRepetitionGuard` remains the reactive backstop.
- **Compression line** — `agent/middlewares/summarization/thresholds.py` is the
  single source: with `MAIN_LLM_MAX_OUTPUT_TOKEN` set, the line is
  `context_window - max_output` (the full input budget); unset keeps the legacy
  80% behavior (the overflow router keeps its budget-relative band when no cap
  is set, so existing timing is unchanged). `GET /context_usage` returns
  `max_output`, `compress_tokens` and `compress_ratio = compress_tokens/window`;
  the ring/dashed line/percent label use that ratio, never a local 0.8. A cap
  equal to or larger than the window is refused by `/models-config`
  (`max_output < MAIN_LLM_MAX_TOKEN`) and clamps to a 0% line at runtime.
  `CompressionMixin._effective_context_window()` remains
  `ctx - min(max_output, ctx)` for preserve/overflow budgets; `_preemptive_check`
  compares absolute thresholds and the soft truncate line scales with the hard
  line (0.70/0.80) only when a cap is set.
- **Microcompact** — `agent/middlewares/summarization/microcompact.py` runs at
  the end of `_run_non_llm_strategies`: on a 60-minute idle (process-local
  completion clock) or at `min(auto_threshold * 0.9, auto_threshold - 2000)` it
  replaces OLD tool results with `[Old tool result content cleared]` (newest 5
  kept, media/already-cleared skipped, min 256-token savings). No LLM call.
- **User-requested compaction** — the `compact_now` main tool (memory catalogue
  group) queues `StateKey.SUMMARIZATION_COMPACT_REQUESTED`; the next model
  call's T2 dispatch runs one compaction with trigger `USER`, bypasses the
  anti-thrash gate and records a `{trigger: user_requested, phase: standalone_turn}`
  trajectory event.
- **ModelDownshift** — a session model profile may carry an optional
  `context_window` (decimal string; client descriptor + backend sanitizer).
  `_session_context_window(session_id)` feeds it into the preserve/overflow
  budgets, so a switch to a smaller window compacts at the next pre-request
  check and the compact event carries `reason=model_downshift`. An absent field
  keeps the env window. It is a threshold effect, not a switch-time hook.
- **Compact phase/reason** — `phase.py` derives `CompactPhase` (standalone_turn /
  pre_request / post_response / reactive / mid_turn) and `CompactReason`
  (context_limit / provider_overflow / user_requested / model_downshift) from the
  existing trigger string; every compact path emits one trajectory event with
  `phase`, `reason`, the unchanged `trigger`, `messages_before`, `messages_after`
  and `route`. T1/T2/T3/user compactions share `_finish_compact`; T4/T5 record
  from `_finish_forced_recovery`.

## Tool Parallel Safety (`agent/safe_tool_node.py`)

LangGraph's ToolNode gathers **every** tool call in one model batch. Sherry
replaces it in-process with `SafeToolNode` (patched into
`langchain.agents.factory.ToolNode` once at `agent/core.py` import, gated by
`TOOL_PARALLEL["enabled"]`) so a batch is scheduled by resource, not blindly:

- **Classification** (`agent/tools/parallel_safety.py`): only explicit
  `READ_ONLY_TOOLS` / `concurrent_safe` declarations are safe; unknown tools
  and missing metadata default to unsafe. `idempotent` is NOT used for this
  decision (`memory` is idempotent but writes session state).
- **File calls** (`read_file` / `write_file` / `patch_file`) group by canonical
  `file_path`. A group containing a write runs in the model's `tool_calls`
  order; an all-read group may run concurrently; different paths never wait.
  A path that cannot be resolved makes the batch a barrier.
- **Every non-file unsafe tool is a whole-batch barrier** (`terminal`,
  `python_repl`, `execute_code`, `ptc`, `memory`, session writes): its resource
  cannot be inferred from arguments.
- **Ordering**: within a batch, read-before-write reads the old content and the
  write then sees the read's license; write-before-read writes first, so the
  read sees the new content (and an unlicensed overwrite is refused by the
  existing rule). Cross-agent/cross-batch order is the file-lock acquisition
  order, not the model's call order.
- **File locks** (`agent/tools/pub_base/path_lock.py` / `file_lock.py`):
  `path_read_lock` is shared, `path_write_lock` is exclusive (writer
  preferred); `flock` uses `LOCK_SH` / `LOCK_EX`. `read_file` takes
  `file_read_lock`, the two writers keep `file_write_lock`. The batch scheduler
  orders same-batch conflicts; these locks protect concurrent agents and
  processes.
- **Resource middleware** (`agent/middlewares/tool_resource_lock.py`, required
  in BOTH the main and subagent chains): `side_effect_scope: "system"` takes a
  process-wide lock (`TOOL_PARALLEL["system_lock"]`, default OFF to avoid
  serializing all shell work); `"user_interaction"` takes a per-session lock
  (`interaction_lock`, default ON). `workspace` / `session` / `network` are
  deliberately not re-locked: file locks, memory, SQLite and the browser
  manager already own those resources.
- **Applies to every agent**: the factory patch is process-wide, so main,
  subagent and auxiliary `create_agent` graphs all use `SafeToolNode`; the
  subagent middleware chain carries the same `ToolResourceLockMiddleware`.
- **Rollout / compatibility**: `TOOL_PARALLEL["enforce"] = False` logs the plan
  but keeps native concurrency; `True` applies it. Tests pin the LangGraph
  private methods `SafeToolNode` depends on, so an upgrade fails loudly.
  `terminal`/`python_repl` still cannot be path-keyed; they remain barriers,
  and `system_lock` only adds cross-agent exclusion when explicitly enabled.

## Plan-Then-Execute Plan Mode (`agent/tools/plan_mode/`)

A session can DESIGN before it executes: the agent enters plan mode, works with
read-only tools, submits a plan and waits for the user's approval before a
single file is written. Two tools, `enter_plan_mode(reason, replan=False)` and
`exit_plan_mode(plan)`, drive a small state machine stored in BOTH register
tiers (mem for the hot path, DB so a restart keeps the mode) under
`StateKey.PLAN_STATUS` — `needs_planning` / `planning` / `ready` /
`needs_replan` — plus `PLAN_REVISION`, `PLAN_MUTATION_COUNT` and
`PLAN_FILE_PATH`. The tunables live in
`config/features/agent_side/plan_mode.py` (`PLAN_MODE`).

- **The read-only guarantee** is `ToolSelectionMiddleware`, doubled exactly like
  its per-session selection: `wrap_model_call` narrows `request.tools` to
  `PLAN_MODE["allowed_tools"]` while the status is `planning`/`needs_replan`
  (still bounded by the session's own set, except that `enter_plan_mode` /
  `exit_plan_mode` always stay reachable — a mode the model cannot leave would
  be a trap), and `wrap_tool_call` REFUSES a write tool that still gets called
  (“Plan mode is active…”). `sessions_spawn` is deliberately NOT on the
  whitelist: a child runs with its own session's state, so a spawned executor
  could write before approval.
- **The human can pin it too**: the toolbar's 访问模式 control gained a 计划模式
  entry (`PUT /sessions/access_mode` `mode=plan`) which sets the same
  `PLAN_STATUS`; the mode it reports is DERIVED from the plan status, so a plan
  the agent entered by itself shows up in the control as well, and switching to
  any other mode resets the status (leaving 计划模式 cancels the flow).
- **The prompt** carries it too: `workspace/prompt_builder.py` appends
  `_build_plan_mode_block()` on every call (never frozen, so entering/leaving
  takes effect immediately and a compression cannot lose it) — the four-phase
  workflow + “a turn may ONLY end with a `question` or an `exit_plan_mode`
  call” while active, the approved plan's file path while `ready`.
- **`exit_plan_mode`** persists the plan to
  `SESSIONS_DIR/<session_id>/plans/plan-<revision>.md` (revision =
  `sha256(plan)[:16]`, so the re-execution after a resume writes the same file)
  and suspends the graph with a HITL `interrupt` (the `question` tool's
  contract): approve → `ready` and the write tools return; reject WITH feedback
  → back to `planning` with the feedback (mutation budget
  `PLAN_MODE["max_mutations"]`, then the tool stops resubmitting); reject
  without a message → `needs_planning`.
- **Guards**: the revision binding (a stored revision or the plan FILE changing
  during the approval discards it), the size cap
  (`PLAN_MODE["max_plan_chars"]`, refused, never truncated) and the mutation
  budget. `PLAN_MODE["enabled"] = False` unregisters both tools and nothing
  else changes. The client renders the plan as an approval card in the input
  slot ([sid].vue, `isPlanHitl`) with the plan text, a feedback field, 退回修改
  and 批准 (the reject+message path is the “request changes” decision).
- Chain membership and order are unchanged (no new middleware); the tools join
  `_MAIN_TOOLS_BUILDERS` and the `plan_mode` catalogue group, so the 预设-工具
  tab can switch them per session. Tests:
  `tests/agent/tools/plan_mode/` + the plan-mode cases in
  `tests/agent/middlewares/test_tool_selection.py`.

## Per-Session Agent Config (`server/service/agent_config_service.py`)

The 预设角色 panel (the 菜单 entry renamed from 预设; it leads the nine-grid, and the right
sidebar's tab label — global and 当前会话 alike — carries the same name) covers one JSON payload
per session in its last four tabs (工具 / 中间件 / 代理模型 / 技能), stored under
`StateKey.AGENT_CONFIG` (parked twin `AGENT_CONFIG_PENDING`, promoted at the turn boundary like
the model/thinking controls)::

    {"tools": [...]|null, "middlewares_disabled": [...],
     "subagent_models": {"<role>": {profile}|null}, "skills": [...]|null,
     "middleware_options": {"Summarization": {"nudge": bool}}}

- `GET /agent/catalog` is the client's only source of tool / middleware / role / skill names
  (`agent/tools/catalog.py::TOOL_GROUPS`, `agent/middlewares/catalog.py::MIDDLEWARE_ORDER`
  - `scaffolding.MAIN_REQUIRED_NAMES`, `roles/loader.py`, `skills/loader.py::skills_catalog`);
    the skill list carries the ACTIVE skills visible to main plus the 内置 / 第三方 split (`builtin:
false` = under `skills/plugins/`); each tool entry also carries the
    tool's own first description line (bounded) — the 工具 tabs show it as a hover tooltip, so
    it always matches the code (runtime notes like web_search's missing key included). `GET|PUT /sessions/agent_config`
    reads/writes the payload; the service rejects an unknown tool / skill / role, and any
    system-required middleware name, with a 400 that names it.
- **Tools** are applied per call by `ToolSelectionMiddleware`
  (`agent/middlewares/tool_selection/`): `request.override(tools=<enabled subset>)` narrows
  what the model sees, and `wrap_tool_call` refuses a disabled tool at EXECUTION (a stale
  checkpoint or a hallucinated name would otherwise still run — the ToolNode holds the full
  list). Unset config = every tool. `agent/tools/catalog.py` carries the three rules the
  payload obeys: `TOOL_ORDER` (group render order — 技能 / 终端与代码 / 文件读写 / 交互 first,
  memory fifth, then the task and delegation surfaces), `REQUIRED_TOOLS` (the file tools, code
  execution, the three skill tools, `question` and `message_search` — the 工具 tab shows those
  rows as locked chips with NO checkbox, the service 400s on a payload that omits one, and
  `enabled_tool_names()` unions
  them back in so an older register value cannot strip them either), and `BULK_ONLY_GROUPS`
  (`tasks` and `subagents` move as a whole in the UI — select-all / clear-all only, never a
  per-tool switch).
- **Skills** (`AGENT_CONFIG["skills"]`): the names whose `<available_skills>` index entries enter
  the system prompt — `null`/absent = every skill (today's behaviour), an explicit list is EXACT
  (an empty list means only the required chain). `skills/loader.py::REQUIRED_SKILLS` is the
  multimedia chain (`image_to_text` / `speech_to_text` / `video_text_to_text` / `text_to_image`):
  the catalogue marks it `required`, serves it FIRST (the 技能 tab renders those rows as locked
  chips with no checkbox), the service 400s on a payload that drops one, and
  `build_system_prompt` unions them back for a stale register value. `workspace/prompt_builder.build_system_prompt` reads the
  session's selection through `runtime/session/agent_config_view.py::session_skill_names` and
  passes `exact=` to `skills.loader.get_skills_text`; an explicit argument still outranks it.
  Because the index lives INSIDE the cached system prompt (`system_prompt_injection`'s three-tier
  cache), `agent_config_service.invalidate_session_prompt` clears that cache whenever a payload
  lands — at the live write and at the turn-boundary promotion for a parked one
  (`session_settings_service.promote_pending_settings_sync`) — and it deletes only
  `StateKey.SYSTEM_PROMPT`, never the frozen persona snapshot.
- **Middleware options** (`AGENT_CONFIG["middleware_options"]`): a REQUIRED entry cannot be
  switched off, so its tuning rides here — today exactly one pair, `Summarization.nudge` (the
  中间件 tab renders it as its own section with a switch). The service rejects an unknown section /
  option / non-boolean value and refuses `nudge: true` beside a payload whose tool list omits
  `memory` (the nudge writes memory THROUGH that tool). At runtime
  `agent.middlewares.agent_switch.middleware_option(session_id, "Summarization", "nudge")` gates
  both nudge entries — the compression-time memory review
  (`schedule_memory_review`, handed the messages about to be discarded and run
  CACHE-WARM: the slice goes back VERBATIM on the model configuration the turn
  used — `nudges._build_session_model` rebuilds it from the descriptor
  `thinking_control.current_session_model_config` publishes every main-chain
  call — with the session's system prompt and tool face, so the compaction's
  prefix cut makes the request a prefix-cache hit on the main model and only
  the review instruction is fresh input; `NUDGE["memory_review_mode"] =
"digest"` falls back to the folded view, `summarization/digest.py`) and the
  plan-completion extraction fork
  (`maybe_trigger_plan_extraction`, which replaced plan extraction's old home
  on the compaction path: it fires from `PlanContinuationEnforcer.aafter_agent`
  on the AUXILIARY model with no conversation history, and dedupes on a plan
  snapshot fingerprint under `StateKey.NUDGE_PLAN_SNAPSHOT_HASH`);
  the panel's switch is DISABLED while the draft has memory off —
  a display/run-time condition, so nothing extra is stored and re-enabling memory brings the
  nudge back. Of the built-ins only 全量 leaves it alone (absent = on): 纯净 / 编程助手 /
  情感陪伴 pin `nudge: false`.
- **Middlewares**: FOUR optional entries are switchable (`PlanContinuationEnforcer`
  — renamed from `TodoContinuationEnforcer`, which old preset payloads still name via
  `agent_switch._RENAMED_MIDDLEWARES` — `TaskIntentMiddleware`,
  `DynamicReminderMiddleware`, `SubagentCompletionDrainMiddleware`); each hook early-returns through
  `agent/middlewares/agent_switch.py::middleware_enabled`, and that helper answers `True` for
  every REQUIRED name (a payload written before an entry was promoted cannot switch it off).
  `WorkspaceNoticeMiddleware` and `MultimodalProcessor` joined the required set: the chain
  build now fails without them (`scaffolding._MAIN_REQUIRED`, 16 entries) and the service
  rejects a payload naming one. Together with `system_prompt_injection`,
  `ThinkingControlMiddleware` and `ToolSelectionMiddleware` they are LOCKED in the UI — chain
  membership and order never change.
- **Subagent models**: `subagent_models[role]` is a profile descriptor (the same shape as the
  session main-model override); at spawn `resolve_role_model_profile` reads it for the
  requester session and the child LLM is built with `build_main_llm_for_profile` (provider /
  key / base_url can change), with the role's `model_tier` as the fallback. `steer` re-reads the
  same profile so a steered run keeps its model.
- **代理模型** is the 子代理模型 tab renamed, split into 主代理 (the main agent's model — a profile
  picker whose "follow the environment config" entry maps to `null`) and 子代理 (the per-role
  pickers). It is a PRESET-level field (`PersonaPreset.main_model`, applied through
  `PUT /sessions/model` so the session-model control stays the single owner of that key) rather
  than part of the agent payload. The 当前会话预设 viewer puts 代理模型 FIRST (the session's own
  model pair is what it is opened for), and BOTH of its sub-tabs write through the
  session-model store — 主代理 is literally the same control as the toolbar's model switch
  (one store, one endpoint, so the two can never disagree; the toolbar shows the parked
  change with its 下一轮生效 badge) and 子代理 keeps the role pickers.
- Presets carry the block (`PersonaPreset.agent`, no Dexie version bump); 保存预设 stores it,
  应用 writes it to the open session (the new-session dialog writes it for the session it just
  created), and the read-only 当前会话预设 tab summarises it (skills read-only there, the
  subagent-model picker editable).
- **The agent-config tabs split into sub-tabs** where the catalogue has two natures: 工具 =
  内置 / MCP (`group === 'mcp'`), 技能 = 内置 / 第三方 (`builtin` flag), 代理模型 = 主代理 /
  子代理. The pills are a VIEW filter — the preset's draft stays whole, and the count (已选 n/m) spans the whole catalogue.
- **Four built-in presets**, in display order — the whole spectrum
  (`client/app/composables/persona-catalog.ts::BUILTIN_PRESETS`): 纯净 (nobody named, every
  persona file empty, only the catalogue's REQUIRED tools and skills, every optional middleware
  off), 编程助手 (the operating-rules template, empty soul / user profile, no roles, every tool
  EXCEPT the memory store, the required skills plus `CODING_SKILLS` — code-wiki / taskflow /
  ulw-execute / todolist — and every optional middleware on), 情感陪伴 (the full role-play
  persona with the 任务与计划 / 子代理 tool groups off, every optional middleware off, the
  mirror-image skill set — the required chain plus every skill 编程助手 does NOT pick — the
  task-orchestration sections stripped out of 运行守则 — `stripTaskSections`, a per-language
  heading/label filter pinned by a test that reads the four shipped templates — and the
  couple-relationship line stripped out of SOUL — `stripRelationshipLines` plus
  `COMPANION_DROPPED_RELATIONSHIP_MARKERS`, the same per-language-template pin, so a companion
  session keeps the persona's warmth without the shipped templates' romantic pairing) and 全量 (the
  DEFAULT — the same full persona with everything on and every skill in the index).
  Their agent blocks are DERIVED from `GET /agent/catalog` at apply time
  (`builtinAgentConfig`), never hardcoded — a preset whose restriction cannot be computed
  throws instead of silently applying every tool. The `sherry` id names 全量 (the label
  changed; sessions already bound to it keep the stored name), and its shipped character is
  still 橘雪莉.

## User Login (`server/service/auth_service.py`)

Opt-in login protection in front of the API, shipped OFF: a default install has
no account and `auth_settings.enabled = 0`, so nothing changes until someone
configures it.

**Two independent auth layers.** `gateway_auth_middleware` (Origin allowlist +
per-boot token) is unchanged and still the outermost gate;
`server/trigger/auth_user.py::user_auth_middleware` is the login gate and runs
last, so an unlisted Origin or a hostile page is refused before a session is
even looked at. The per-boot token is re-fetched by the CLIENT before every
socket (re)connect (`requestApi.refreshGatewayToken`, called from
`ws-ticket.ts::prepareWsTicket`), so a page that survives a backend restart
heals within one reconnect cycle instead of replaying a dead token until a
manual reload. `server/trigger/cors.py::cors_origin_echo` (an `after_request`
handler) reflects the caller's Origin for allowlisted origins — credentialed
cross-origin fetches are refused by the browser when the response says `*`.

**ONE account, password only.** There is no username anywhere: the single
`auth_account` row (`CHECK (id = 1)` — the schema cannot hold a second) is
identified by nothing, login takes just the password, and a username-era
`auth_users` table is copied into it at init and dropped
(`_MIGRATION_DDLS` in `server/DAO/auth_store.py`). The JWT carries only
`sub`/`jti`/`typ` — no personal data.

**Brute force is throttled, injection has no surface.** `_LoginThrottle`
(`server/service/auth_service.py`) counts consecutive failures per CLIENT
SOCKET ADDRESS and escalates: `AUTH["login_max_attempts"]` free tries, then
`login_lockout_base_seconds` doubling to `login_lockout_max_seconds`
(a locked client is refused BEFORE any hashing, 429 + `retry_after`), and a
success clears the streak. Every query in the store is parameterized
(`?` placeholders only — never string-built SQL), which the test suite pins
with SQL-shaped passwords, and a missing account still pays one dummy scrypt
verify so timing says nothing.

**Who must log in.** Loopback clients never do (`request.ip_addr`, the socket
address, not a header) unless `SHERRY_AUTH_REQUIRE_NON_LOOPBACK=0`; enforcement
requires BOTH the runtime switch in SQLite AND an existing account, so an enabled
switch can never lock a deployment out of itself. `/auth/status` reports the same
rule the middleware applies.

**Session.** scrypt-hashed password (`server/utils/password.py`, stdlib only,
parameters carried in the stored string), HS256 JWT (`server/utils/jwt_utils.py`,
`algorithms` pinned, `typ` claim separating access/refresh) in two HttpOnly
cookies: `sherry_session` (Path=/, 12h) and `sherry_refresh` (Path=/auth/refresh,
7d). The client never sees a token string; `/auth/login` and `/auth/refresh`
answer `{expires_in, user}` and the browser keeps the cookies. Refresh ROTATES
(the presented `jti` is blacklisted — a replay gets 401); logout blacklists both
`jti`s; `auth_service.start_blacklist_cleanup()` prunes expired entries.

**WebSockets** carry no peer address in Robyn, so their gate is a ticket:
`GET /auth/ws-ticket` makes the loopback decision over HTTP and the handshake
spends the single-use ticket (`?ticket=`). The client prefetches one per connect
(`client/app/composables/ws-ticket.ts`, a lazy import so the socket layer does
not pull the whole auth bridge into every page).

**An expired session cannot strand the app.** A 401 that survives the transport's
rotate-and-replay (`requestApi.rotateSession()` → `'refused'`, i.e. the backend
ANSWERED and rejected; an unreachable backend never signs anyone out) emits
`auth:session-lost`; the auth-guard plugin clears the stale user and routes to
`/login`, so the form appears without waiting for the next navigation. The
scheduled rotation does the same on a refusal. Two related rules keep the guard
honest: a `/auth/status` probe that fails leaves the verdict UNKNOWN (never
"no login required") and stays retryable — the guard must not bounce a user away
from `/login` on a failed probe — and the connectivity banner reports a dead
socket as "backend unreachable" (the `navigator.onLine` hint is unreliable in
containers/webviews; it only fills in when there is no WS verdict yet) and stays
silent while nobody is signed in (the socket cannot connect by design then).

**Storage** is `src/data/auth.db` (`server/DAO/auth_store.py`): `auth_account`
(the single row: scrypt hash), `auth_settings` (single row = the runtime
switch; `SHERRY_AUTH_ENABLED` only _seeds_ a fresh row) and
`auth_jwt_blacklist` (`expires_at` in JWT seconds). Server tests isolate it with
the autouse `isolated_auth_store` fixture — no test may touch the real file.

**Client**: `pages/login.vue`, `middleware/auth.global.ts` (per-navigation
`/auth/status` decision), `stores/auth.ts`, `pages/home/components/AccountSettingsPanel.vue`
(menu → 账户, a right-sidebar tab), `composables/bridge/auth.ts` (raw transport:
codes in, no generic toasts) and the transport's own 401 → refresh-once → replay
(`requestApi.refreshSessionOnce`, single-flight).

## Session Token Budget (`agent/middlewares/token_budget/`)

A session can carry an optional token CAP (`PUT /sessions/budget`,
`StateKey.TOKEN_BUDGET_CAP`; `null` clears it, `0` means unlimited). The cap is
optional and absent by default, so the middleware is a pure pass-through until
someone sets one. `TokenBudget` sits beside `IterationBudget` in the main chain
(always on, not gateable): `wrap_model_call` / `awrap_model_call` first check the
cap and, once `TOKEN_BUDGET_USED >= cap`, return a terminal `AIMessage` WITHOUT
calling the provider (the turn ends with a summary request). After a normal
response it accumulates the provider-reported `usage_metadata` into
`TOKEN_BUDGET_USED` (exact) and `TOKEN_BUDGET_COST` (estimated USD), dual-written
to the mem + DB registers so a restart does not lose the counters. The cap/park
twin (`TOKEN_BUDGET_CAP_PENDING`) uses the same busy → park / turn-end promote
rule as the thinking/model controls; `POST /sessions/budget/reset` clears
USED/COST/LIMITED but keeps the cap.

Pricing is USER-CONFIGURABLE, not hardcoded: `.models` carries
`MODEL_PRICING_TABLE` (a JSON object serialized as a JSON string so the flat
key/value file editor keeps working; seeded from `.models.example`, and an
existing `.models` gains it on import). Lookup order is provider-returned cost
(OpenRouter-style `usage.cost` / `prompt_cost` + `completion_cost`) → the user
table → the local litellm cache (`src/data/litellm_prices.json`, gitignored,
refreshed by `scripts/sync_litellm_prices.py`) → the table's `_default`
(`config/features/infra_side/model_pricing.py::resolve_model_pricing`). The cap
uses tokens only; the cost is an estimate for display/warnings. A warning event
(`budget_warning`) fires once at `MODEL_PRICING["budget_warn_threshold"]` (0.80)
and a `budget_limited` event fires once when the cap is first reached; both are
best-effort WS pushes. `GET /sessions/budget` reports
`{cap, used, cost, limited, pending}`.

## TaskFlow Progress (`agent/tools/taskflow/progress_push.py`)

Two reporting views, both pushed (never polled) to the chat's floating progress
panel — top-right overlay in `ChatBox.vue`, collapsed to a pill by default:

- **Todos** — the existing `todo_updated` push from
  `agent/tools/todolist/service.py::_push_todo_update`.
- **Steps** — `progress_push.py` sends each flow's flat step list (`step_id`,
  task text, status) plus per-status totals. The scheduler unlocks each step on
  its own `depends_on`; there is no wave/round projection, so the payload never
  implies a barrier between groups of steps. Every flow mutation calls
  `progress_push.push_taskflow_progress(session_id)` — wired into the shared
  `update_flow_with_conflict_retry` plus create / update_steps / finish / fail /
  cancel / set_waiting — which sends `{"event":"taskflow_updated", …}` to the
  session's websocket, best-effort and never raising (a dead socket must not break
  a tool). A reconnecting client asks for a snapshot with `taskflow_refresh`
  (handled in `server/trigger/core.py`) and gets the identical payload, so the
  client has one shape to render (`client/app/stores/taskflow.ts`,
  `pages/home/components/ProgressFloat.vue`).

## Web Search Backends (`agent/tools/web_search.py`)

`web_search` is ONE tool over two interchangeable providers — the Tavily branch
pins `name = "web_search"` (TavilySearch's own default would ship the swap as a
second tool name), so the model, the per-session tool selection and the 工具 tab
all see the same tool whatever serves a call. Which one serves a call is resolved per call
(`resolve_preferred_backend`): the session's explicit choice
(`StateKey.SEARCH_BACKEND`, written by the 模型联网 panel through
`PUT /sessions/search_backend` and parked/promoted at the turn boundary like the
thinking and model controls) → else `WEB_SEARCH_CONFIG["default_backend"]` →
else availability (a default whose provider is unconfigured switches to the
other one; an explicit choice counts only while its provider IS configured, so a
session pinned to a backend that lost its `.network` entry cannot end up aiming at
nothing). Both unconfigured leaves the tool a stub whose result names the fix.
Tavily keeps its bounded retry and then falls back to SearXNG when an instance
is configured — the one thing SearXNG never does is fall back to Tavily (a
self-hosted deployment may have no key at all, and the pair must not ping-pong).

The fallback is announced, not silent: `agent/tools/web_search_push.py` pushes
the best-effort `search_backend_fallback` frame
(`{from, backend, reason, message}`, reason ∈ `tavily_auth_failed` /
`tavily_timeout` / `tavily_error`; an auth failure skips the retries) to the
session's websocket, exactly like the TaskFlow progress push. `client/app/stores/web-search.ts`
turns it into a 通知 entry (source `web_search`, reasons localized from the
CODE at push time through `resolveRuntimeT`, whose `t` takes named params) and
the bell's unread badge lights up — the listener lives in the app shell
(`pages/home/index.vue`), because the frame arrives while a TURN runs and the
user may be looking at another session. The turn it belongs to keeps streaming,
and the result text names its own backend ("SearXNG search results …"), so a
client that misses the frame still shows the truth.

SearXNG lives in `agent/tools/web_search_searxng.py`: the instance's JSON API
(`GET {SEARXNG_URL}/search?q=…&format=json&pageno=1`), results ranked by score
and formatted like Tavily's, plus the three connection options a self-hosted
deployment needs — an httpx proxy, engine/category passthrough, and a
TLS-verification toggle for MITM proxies. It NEVER raises: every failure returns a readable line
naming the status and whether the request went `via proxy …` or over a `direct
connection`. `SEARXNG_URL` / `SEARXNG_PROXY` / `SEARXNG_ENGINES` /
`SEARXNG_CATEGORIES` / `SEARXNG_VERIFY_TLS` live in the gitignored `.network`, and
so does `TAVILY_API_KEY`: the 模型联网 tab is their ONLY editing surface
(`SERVER_HTTP["env_panel_hidden_prefixes"]` withholds `TAVILY_`/`SEARXNG_` from
`GET /env` AND `GET /network-config`, so the 模型配置 tab — `config.tabs.env`,
renamed from 环境配置 — never shows a second, drift-prone editor; writes go
through `PUT /network-config`, reads through the state payload's `env` block,
and the Tavily VALUE never leaves the server). They are read at BOOT, so the panel's save says so — while the
per-session radio applies at once.
`GET /sessions/search_backend` answers the panel's read model (choice, effective
backend, `pending`, both providers' availability and SearXNG's status) and
tolerates an EMPTY `session_id`, because the panel is a settings tab that opens
without a session too. `scripts/dev/mock_searxng.py` implements exactly the call
the provider makes, for exercising the fallback end-to-end without a real
instance (and without a Tavily key: point `SEARXNG_URL` at it and set
`TAVILY_API_KEY` to a dummy to watch the downgrade fire).

## GraphRAG Query Paths (`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py`)

The vendored LightRAG retrieves entities/relations by vector similarity and leaves
multi-hop joins to the LLM, so a bridge entity outside top-k makes a relationship
question unanswerable by traversal. `path_search.py` adds the explicit layer
(the vendored tree only gains a dispatch hook in `operate.py`):

- **Endpoint resolution** — exact graph node name → graph-label containment
  (`Kepler-7 launch pad` → `Kepler-7 Launch Pad`) → entity-vector recall keyed by
  `compute_mdhash_id(name, prefix="ent-")` (the vdb keys are hashes, not names —
  a raw-name `get_vectors_by_ids` silently returns nothing).
- **BFS** — all simple paths up to `GRAPH_RAG_PATH["max_depth"] = 3`, ordered by
  `(hops, -average edge weight)`, capped at `top_k_paths = 10`, with a
  `max_visit_nodes` budget and a `max_frontier` per-layer cap; a path never
  repeats a node.
- **Beam fallback** — only when BFS finds nothing: bidirectional,
  embedding-guided, `beam_width = 5`, `beam_max_hops = 5`, score =
  cosine(neighbour, opposite endpoint) + `beam_edge_weight × edge weight`.
- **Modes** — `mode="path"` searches paths exclusively and falls back to
  `hybrid` recall when none exists (never an empty context); `mix`/`hybrid`
  queries that read as relationship-shaped (four-language patterns,
  `is_relationship_query`) auto-merge a path section. Both add the chain +
  node/edge descriptions + the traversed edges' source chunks to the prompt and
  the structured raw data (`path_info` metadata). Tunables live in
  `config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH`.
- **Env defaults belong in `graph_rag/__init__.py`**, before the vendored import:
  `QueryParam` and the LightRAG dataclass read `TOP_K` / `CHUNK_TOP_K` /
  `COSINE_THRESHOLD` / `MIN_RERANK_SCORE` (tuned to 20 / 12 / 0.4 / 0.5) and the
  graph caps into class-level defaults at import time; setting them in `base.py`
  after its own LightRAG import never took effect.
- **Sparse recall is blocked**: `models/embed_model/text/core.py::CustomEmbedding`
  is dense-only (llama.cpp `create_embedding` / a remote `/embeddings`), the vdb
  is one HNSW cosine index, and `EmbeddingFunc` returns one `ndarray`; BGE-M3
  sparse would need FlagEmbedding/PyTorch or a sparse endpoint. FTS5/BM25 remains
  the documented future option for rare ID/version strings.
- **Measurement**: `evals/graph_rag/multihop_bench.py` (20 docs, 102 nodes / 106
  edges, 6 questions, deterministic bridge-token scoring) reports bridge recall
  and context size per mode; `evals/graph_rag/suite.py` stays the single-hop RAGAS
  regression gate. Measured after the change: bridge answer recall 0.875 → 1.000,
  total context −44% (`mix`) / −85% (`path`), path latency 2-13 ms.

## URL Fetching (`agent/tools/web_fetch.py`)

`web_fetch(url)` reads ONE http(s) URL and hands it back as markdown — the
companion to `web_search` (search finds URLs, this reads one). Two branches:

- **public** (default): `is_public_url` (its own DNS resolution) then
  `safe_fetch` — the same SSRF guard + DNS pinning + per-hop redirect re-check
  the media pipeline uses; the body is capped at
  `TOOLS_TIMEOUTS["web_fetch_max_bytes"]` (10 MiB) and the fetch at
  `web_fetch_timeout_seconds` (30 s);
- **local / private** (`localhost`, literal private / loopback / link-local
  IPs): REFUSED unless `SHERRY_WEB_FETCH_ALLOW_LOCAL=1` (tracked `.env`,
  default 0, read per call). Then httpx does the fetch (`verify=False` — dev
  servers and self-signed certs — with a 5-hop redirect cap and a 10 s budget).
  Address classification is literal-only: a public hostname resolving to
  127.0.0.1 still goes down the public branch and is refused there, so the
  switch can never turn a public-looking name into a local fetch. This is the
  deliberate escape hatch for debugging a dev server the agent is running; the
  switch reaches every service this machine can see.

Both branches share the tail: a non-HTML body (JSON, plain text) is returned
VERBATIM (html2text would escape and reflow it), HTML goes through `html2text`
(scripts/styles/noscript stripped first; a tag-stripping fallback if the import
ever fails), and the output is capped at
`TOOLS_TIMEOUTS["web_fetch_max_output_chars"]` (50 K chars) — the context
eviction layer is the second net for whatever still arrives oversized. The tool
is in the `web` catalogue group with `idempotent: False`; it is not
`main_only`, but a specialized child only receives it in `general` (inherit)
mode, since no functional role's allow-list names it. Tests:
`tests/agent/tools/test_web_fetch.py`.

## Log Tracing & the Trajectory Ledger (`server/service/trajectory_store.py`)

A turn is one greppable unit. `turn_runner.WsTurnExecutor._drive` opens
`turn_scope(turn_id, session_id)` around the whole turn — a
`logger.contextualize` scope (every log line in the turn carries both ids,
including the middleware chain, tool calls, LLM retries and compaction) AND a
contextvar the frame projector reads. The WS resume path opens the same scope
with its own fresh turn id, and `_execute_subagent` rebinds the CHILD's identity
(child session key + its own turn id) so a subagent's lines never carry the
parent's. `logs/logger.py` adds a JSONL twin of the full log
(`logs/output/json/json_*.log`, `serialize=True`) so
`jq 'select(.record.extra.turn_id == "…")'` pulls one turn out.

`trajectory_events` (SQLite, `SESSIONS_DIR/trajectory.db`) is the structured
per-turn timeline for the UI: `ServerStreamTurn._project` hooks the stream's
frame yield sites and upserts `tool_start`/`tool_end` into ONE row per tool call
(deterministic `event_id`, `json_patch` merge: args from the start, result +
duration + error from the end), the turn scope records `turn_start`, the stream's
completion records `turn_end` (total ms + TTFB), and agent-side producers
(Summarization's compaction) reach the ledger through
`runtime.hooks.RECORD_TRAJECTORY_EVENT` (registered at server assembly) —
`agent/**` never imports the server module. `GET /sessions/:session_id/trajectory`
(`server/trigger/http/trajectory.py`) reads a turn back (`turn_id`, `limit` ≤ 500,
`offset`); writes are per-session pruned to `TRAJECTORY["max_turns_per_session"]`.
Everything in the ledger is fail-open: a metrics write never breaks a turn.

The message store now carries the same identity: `messages.turn_id` (additive
`TEXT` column + a partial `(session_id, turn_id)` index) is stamped by
`_persist_batch` from the server-owned `current_turn_id` contextvar through
`runtime.hooks.GET_CURRENT_TURN_ID` (`context_engine` never imports `server`).
Every persist batch of one conversation turn therefore shares the uuid while
keeping its own per-batch `turn_num`; the same values also ride on
`BaseMessage.additional_kwargs["turn_id"] / ["turn_num"]`. A missing hook or an
inactive scope stamps `""`; rows written before the column stay `NULL`. This is
the join key between `messages` and `trajectory_events.turn_id`.

## Agent-Controllable Browser (`config/features/infra_side/browser_agent.py`)

Opt-in and OFF by default (`SHERRY_BROWSER_AGENT_ENABLED=1` + restart; the
「模型配置」 panel grows a `SHERRY_BROWSER` group for its switches (they live in
`.network`)). With the feature off no `browser_*` tool is registered, every
`/browser/*` route answers 404 and no Chromium is ever spawned.

**Server side** — `server/service/browser_cdp.py` is a thin CDP transport (one
WebSocket, JSON-RPC ids, flat sessions, _droppable_ screencast events) plus the
launcher: it probes `--no-sandbox` last (the first attempt is capped at 6 s so a
host without user namespaces pays seconds, not the whole launch budget) and
honours `SHERRY_BROWSER_HEADLESS` (default: headed when a DISPLAY exists). The
process singleton `browser_manager.py` keeps pages per session, stores snapshot
refs SERVER-side (cleared on navigation), bounds every verb (timeouts, element /
text / screenshot caps), evicts the LRU past `max_pages`, reclaims pages unused
for `idle_timeout_s` unless a panel is watching them, and reaps its Chromium on
exit (`shutdown_browser_blocking` signals the exact PID). The debug port never
leaves the process: `status()` reports the page count, never the port, and the
devtools frontend is built in-process.

**Routes** — `GET /browser/status`, `POST /browser/page|navigate|close` (the
panel's HTTP face) and the live channel `/browser/ws` (gateway token + login
ticket): screencast frames out, `nav`/`reload`/`back`/`forward`/`viewport`/
`input`/`watch`/`devtools` in. A user-initiated navigation reaches the address
bar through `Page.frameNavigated` (a `chrome-error://` page keeps the URL the
user asked for); the screencast stops when nobody watches a page.

**Agent side** — the eight `browser_*` tools (navigate / snapshot / click /
type / press / scroll / screenshot / evaluate) live in the `browser` catalogue
group, are MAIN-AGENT-ONLY (`metadata["scope"] = "main_only"` — the subagent
tool policy drops them unconditionally), and are taught by the `browser`
playbook skill (`skills/builtin/core/browser/SKILL.md`, scriptless, hidden from
the index while the feature is off). `browser_screenshot` writes a PNG and
returns **the image itself** when the serving model reads images natively
(`media_pipeline/tool_media.py::native_image_content` — the `image_to_text`
skill is the fallback for a model without vision, never the route a
vision-capable one takes; the per-request capability scrub and the retry
fallback strip the block again for a model that turns out blind, and the
per-turn history strip removes it from later turns so a screenshot does not
ride every context); `browser_evaluate` registers only with
`SHERRY_BROWSER_ALLOW_EVALUATE=1`.

**Panel** — `BrowserPanel.vue` picks CDP mode from `/browser/status` and falls
back to its iframe mode otherwise (`composables/browser-channel.ts` owns the
socket). Free size drives REAL device emulation
(`Emulation.setDeviceMetricsOverride`) and pointer coordinates map through the
emulated viewport, never through the screencast frame's pixel size (Chrome's
mobile emulation reports its own scale, measured 393x852 -> 554x1200); 打开调试
工具 opens the inspector as a page the panel watches. There is NO approval gate
on browser actions — the panel IS the human's window on the same page (ZCode
parity); the external-path HITL gate still covers `terminal`/skills.

## Stream Broadcast & Reconnect Resume (`runtime/session/ws_push.py`)

Every streamed frame fans out to **all live sockets of the session** at send
time (`runtime/session/ws_push.py::broadcast_to_session`, called by the two
`StreamDriver.send_frame` sites and the ~14 push sites: turn errors, taskflow
progress, todo pushes, web-search fallback, background-terminal exits, subagent
events, heartbeat/cron pseudo-sessions). Process-level work that is NOT
session-scoped uses `broadcast_to_all` instead (every registered socket,
whatever its session; model download progress is its first consumer). The
registry keeps a per-session bag
(`relation_register.session_id_to_websocket_ids`, 1:N, ordered) plus the 1:1
"newest socket" view; a failed send prunes only that socket. **There is no
replay log and no per-client queue**: each frame reaches each socket exactly
once, and a socket that connects mid-turn receives only frames produced after
its registration.

A viewer that drops mid-turn reconnects and makes ONE HTTP request instead of
a WS snapshot frame: `GET /get_history_by_turn_page?...&include_active_turn=1`
returns `{messages, active_turn}` (without the flag the endpoint keeps returning
the plain row array). `messages` carries every turn that completed while the
socket was down, so no answer waits for the current turn to end; `active_turn`
is `active_turn_snapshot()`'s `{text, has_reasoning, seq, turn_id, turn_num}` or
null. While that request is in flight the socket HOLDS every inbound frame
(`AgentSocket.holdFrames`, capped at 1000, oldest dropped); on the response the
client merges history, applies the snapshot to its turn's row (by `turn_id`,
else `turn_num`, else a new AI row), records the watermark and releases the
queue. A held frame is dropped only when its `turn_id` matches the snapshot's
and its `seq <= snapshot.seq`: the watermark is PER-TURN (each `StreamTurn`
restarts `seq` at 1), so a new turn can never be mistaken for old data. Every
stream frame — chunks and the driver's `turn_started` / `done` /
`hitl_request` / `error` / `stopped` — carries `turn_id`; the client routes by
it, and a late `done` of a previous turn does not settle the current one. The
wait is bounded at 10 s: on timeout the response is gated out of the chat merge,
the queue is released without a watermark, and terminal frames or the busy
watchdog settle the turn. Only a send that already received a chunk can be
concluded as ended-while-disconnected; a pre-chunk send is re-sent (deduped by
`msg_id`) and stays pending. The old `resume_stream` WS handler and its
`stream_resume` reply are retained as a legacy fallback, but the client no
longer sends that frame. This snapshot + per-turn `seq` pair is Sherry's
stream-recovery anchor: it restores the text tail without replaying any frame,
while committed tool calls stay queryable in the per-turn trajectory ledger
(`GET /sessions/:session_id/trajectory`). Because the turn keeps running when
its last viewer disconnects (the socket release no longer cancels it), the
reconnect resumes the SAME turn. Inbound frames are also checked against the
socket's registered session (`_frame_session_allowed`) so one session's socket
can never read another's.

## Cron Jobs & Skill Binding (`skills/builtin/core/cron/`)

The cron engine is a builtin skill (`scripts/base.py::CronService`, jobs persisted to
`cron_jobs.json`), with the agent-facing facade in `scripts/core.py`, REST wrappers in
`server/trigger/http/cron.py`, and the panel in `client/…/CronPanel.vue`. A job can bind an
ordered list of skill names (`payload.skills`): on every run `_assemble_skill_prompt()` loads
each SKILL.md through `_skill_view(name, caller_scope="background")` and prepends it (wrapped
in an `[IMPORTANT: …]` header) ahead of the job message, counts the use (`bump_use`), and scans
the assembled prompt for injection patterns (warning only). The cron agent keeps its minimal
background tool set — skills arrive by pre-load, not by a `skill_view` tool.

Reference maintenance lives in `scripts/skill_refs.py` and keeps bindings honest when skills
move: `rewrite_skill_refs()` follows consolidation into an umbrella or drops pruned names (called
by the curator right after archiving and by `skill_manage(action="delete")`), and
`referenced_skill_names()` makes the curator's 90-day transition refuse to archive a skill that
some job still loads. Documented in the four-language `scripts/README*.md`.

## Concurrency Lanes (`runtime/lane/`)

Four process-level lanes, each an `asyncio.Semaphore` + active/queued counters, gate concurrent work instead of rejecting it: over-limit work waits FIFO.

| Lane       | Constrains                                                   | Default                                                               | Config key                               |
| ---------- | ------------------------------------------------------------ | --------------------------------------------------------------------- | ---------------------------------------- |
| `MAIN`     | main-agent turn (`_run_executor`)                            | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` (12) → 12–16 | `LANE_SYSTEM["main_max_concurrent"]`     |
| `SUBAGENT` | child-agent executions (spawn + steer)                       | 8                                                                     | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE`    | nudge/persistence calls (`summarization/nudges.py`, 3 sites) | 4                                                                     | `LANE_SYSTEM["nudge_max_concurrent"]`    |
| `NESTED`   | `sessions_send` reply turns (serial)                         | 1                                                                     | `LANE_SYSTEM["nested_max_concurrent"]`   |

- Config: `config/features/infra_side/lane_system.py`; `validate_lane_config()` runs at server startup and enforces `main >= subagent + nudge` (all limits ≥ 1); `install_lane_lifecycle()` in `server/service/lane_lifecycle.py` validates, prewarms the manager, registers `set_drain_check(is_gateway_draining)`, and installs a bounded exit drain (`atexit`, `drain_all(timeout=0)` — never blocks exit).
- Queueing: a spawn that passes per-parent admission but exceeds the SUBAGENT lane is registered `PENDING` (no `forbidden`); `PENDING → RUNNING` happens inside the lane slot and only then is `started_at` stamped (queue wait is not run time). `PENDING` counts as active in registry queries and is killable; restore finalizes restart-leftover PENDING runs the same way at startup (silently — no announce), and orphan recovery covers same-lifetime lost tasks with `ended_reason="pending_orphaned"`.
- Drain mode: `set_drain_check(fn)` makes `Lane.acquire()` refuse (without consuming a permit) while the subagent gateway reports draining.
- Observability: `GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`.
- Hot updates (`LaneManager.set_concurrency`) only affect new acquires; in-flight slots keep their permits.

## Key Configuration

| File                          | Contents                                                                                                                                                                                                                    |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config/features/agent_side/` | 54 per-object TypedDicts (summarization, guardrails, tool_result_eviction, iteration, taskflow_infra, todolist_infra, tools_timeouts, step_judge, completion_judge, evidence_ledger, evidence_cross_session, evidence_regression_gate, subagent_isolation, tool_parallel, ...) |
| `config/features/infra_side/` | 28 per-object TypedDicts (gateway, auth, bus, http_upload, retry_backoff, server_http, ws_stream, input_queue, heartbeat, cron, skill_scanner, mes_memory, curator, web_search, model_pricing, model_downloads, ...)                         |
| `config/features/__init__.py` | Aggregator — all 82 TypedDicts + instances re-exported                                                                                                                                                                      |
| `config/path.py`              | All filesystem paths (ROOT_DIR, SKILLS_DIR, WORKSPACE_DIR, ...)                                                                                                                                                             |
| `config/schema.py`            | Pydantic Config (SHERRY_ env prefix, mostly unused at runtime)                                                                                                                                                              |
| `config/sherry_settings.py`   | sherry.jsonc loader (TOOL_CALL_TIMEOUT_MINUTES, LOG_LEVEL, curator._, LANGSMITH._)                                                                                                                                          |

## Code Graph

CodeGraph MCP (`@colbymchenry/codegraph`, wired in `opencode.json`) indexes the repo into `.codegraph/codegraph.db` (gitignored) — a 20-language AST knowledge graph (Python/TS/Rust full; `.vue` gets no AST). One MCP tool, `codegraph_explore`, returns verbatim source + call paths (dynamic dispatch included) + blast radius. Auto-syncs on file events (2s debounce). CLI: `codegraph query|explore|callers|impact|status`. Rebuild index after big restructures: `codegraph init`. It is a snapshot — verify against source when editing.

## Code Intel Graph & Wiki Export

`agent/tools/code_intel/` now carries four edge families, all resolved by the same
two-pass name resolution (`indexer.py`): `call_edges` (unchanged), plus the
parallel `import_edges` / `inherit_edges` / `mixin_edges` tables created with
`CREATE TABLE IF NOT EXISTS` (an old index DB is upgraded in place — C1). The
extractor (`extract.py`) records Python imports/inheritance, TS/TSX named/default/
namespace imports plus `extends`/`implements` (interfaces are symbols), Rust
`use`/trait bounds/`impl Trait for Type`, and Go import paths + struct/interface
embedding. Module-scope edges use `importer_id = 0`; named-edge resolution prefers
the file's own language so a TS `Base` never links to a Python `Base`.

The RESEARCHER surface gained one tool, `path(symbol_a, symbol_b)` — a directed
bidirectional BFS over all four families (forward from A, backward from B,
`code_intel_call_graph_max_depth`) returning hops with `edge_type` and
`confidence` (`EXTRACTED` = resolved, `INFERRED` = name-only). It is an
independent implementation; `impact()` is untouched (C2) and still returns its
`depth` + `via` BFS view. `CalleeInfo`/`CallerInfo` gained a derived
`confidence` field alongside the unchanged `resolved` bool. Query-only methods
(exposed to the agent through `explore`/`path`, not new tools): `imports_of`,
`importers_of`, `inherits_from`, `subclasses_of`, `mixins_of`, `mixins_in_by`,
`god_nodes` (pure SQL degree) and `communities` / `community_of` (networkx
Louvain on demand with a connected-components fallback when networkx is absent).
The tool name is `path`; the tool set is still `scope=researcher_only` and never
joins `_MAIN_TOOLS_BUILDERS` (C5).

`skills/builtin/code_wiki/scripts/core.py::export_wiki_from_index(repo_name,
db_path)` turns a code_intel SQLite DB into a deterministic, zero-LLM wiki
(README/architecture/modules/class-diagram + state); `sequences.md` stays
LLM-owned (runtime flow is not in an AST). It is an independent entry point —
`init_repo_wiki` and the other path helpers are unchanged (C6) — and returns
`{"fallback": True, "reason": ...}` for a missing/empty index or unsupported
languages. Caps live in `SKILLS_TOOLING` (`code_wiki_export_*`).

## LLM Wiki Graph Layer

`skills/builtin/llm_wiki/` keeps markdown as the source of truth and adds a
derived graph at `src/data/wiki/.wiki_graph.db` (`scripts/graph.py`): nodes for
file stems and link targets, edges for `[[wikilinks]]` (`[[Target|INFERRED]]`
carries the tag; a plain link is EXTRACTED), inline links, code-span mentions and
optional frontmatter `extracted_relations`; `extracted_entities` adds nodes.
`build_wiki_graph(force=False)` is incremental by file mtime, removes deleted
files' nodes/edges, and never touches the existing `save_source`/`search_wiki`/
`lint_wiki` signatures (L1). `scripts/graph_query.py` exposes `path` (undirected
bidirectional BFS), `god_nodes`, `explain`, `communities` (networkx Louvain or
connected components) and `query_graph` (ASCII words + CJK bigrams → node recall
→ 1-hop subgraph; no jieba dependency). `scripts/ingest.py::save_binary_source`
adds PDF (pypdf → pdftotext → mineru), image, audio, video, docx/xlsx (stdlib
ZIP/XML readers, no python-docx/openpyxl) and text ingest: one auxiliary-LLM
semantic pass (fail-open), a markdown source with the additive frontmatter, then
a graph refresh. `lint_wiki` appends a `graph` section (connectivity, confidence
split, low-confidence pages, god nodes) after its original ten checks (L7).

## Import Rules

- `server/**` layering: trigger → service → queue|DAO → utils (enforced by import-linter)
- Cross-package bans (enforced by `uv run --no-sync lint-imports`, `[tool.importlinter]` in `pyproject.toml`; grimp counts function-level imports, so a lazy import does NOT satisfy them):
  - `agent/**` MUST NOT import `server/**`
  - `config/**` MUST NOT import `models/**`
  - `context_engine/**` MUST NOT import `agent/**`
  - `workspace/**` MUST NOT import `agent/**` or `context_engine/**`
  - `skills/**` MUST NOT import `server/**`
  - `skills/builtin/**` user scripts may still import `models`/`bus`/`channels`/`workspace`/`runtime` (one-way, by design) — this ban covers `server/**` only; whether to tighten those directions is evaluated separately and is not currently enforced
- Cross-boundary seams: `runtime/hooks.py` (callback registry) and `runtime/data_provider.py` (`PromptDataProvider`/`SkillWriteProvider`) are leaf modules importable from both sides — owners register at assembly time (server boot / `agent.core.init()`), consumers resolve at call time. Never reintroduce a direct import to cross a forbidden boundary. The transcript port is the third seam, in the agent's own direction: `agent/state_port.py` (`read_messages` / `read_interrupt` / `heal_interrupted_turn`) is the ONLY place the LangGraph checkpoint shape is read or rewritten — `server/` drives turns through it and never touches `snapshot.values["messages"]` or `aupdate_state` itself (a turn's INPUT messages and the auxiliary heartbeat agent stay the server's own business).
- `config/features/**` MUST NOT import from `agent/`, `server/`, or `models/` — config is dependency-free
- `config/**` is importable from ALL layers (no restriction)
- every numeric constant lives in a `config/features/**` per-object module (e.g. `SUMMARIZATION` in `config/features/agent_side/summarization.py`), never in a shared `num.py`

## Test Structure

```
tests/
├── agent/
│   ├── middlewares/          # one test file per middleware (summarization, task_intent, ...)
│   ├── tools/
│   │   ├── taskflow/         # test_dag_e2e.py, test_token_budget.py, test_deadline.py, ...
│   │   ├── todolist/         # test_todolist_e2e.py, test_store_sqlite.py, ...
│   │   └── subagent/         # test_spawn_direct_e2e.py, test_queue_e2e.py, ...
│   └── core/                 # test_built_agent_wrappers.py
├── workspace/                # test_prompt_builder.py, test_prompt_builder_todos.py, ...
├── config/                   # test_features_agent_side.py, test_features_infra_side.py, ...
├── server/                   # mirror of server/ source
├── context_engine/           # mirror of context_engine/ source
└── run_tests_split.py        # CI gate: 3-process runner (A=unit, B=integration, C=regression)
```

Markers: `unit`, `integration`, `module`, `system`, `regression`, `llm_e2e` (deselected by default).

`tests/full/` is excluded from the split runner (`--ignore`) and every live-LLM file there is tagged `llm_e2e`, so a bare `pytest` run never collects them. Run one explicitly with `uv run --no-sync pytest -m llm_e2e tests/full/<file>` — addopts default to `not llm_e2e`, so the explicit `-m` is required. Because process startup/teardown dominates the wall clock, run `tests/full/` **one file per process under an external watchdog** (e.g. `timeout 1500 uv run --no-sync pytest -m llm_e2e tests/full/<file>`); measured: 47 cases ≈ 10.8 min of net test time but ≈ 73.7 min wall clock when batched. Hermetic tests belong in the standard tree, not `tests/full/`.

`evals/` sits beside the pytest tree: a homegrown, sandboxed framework with
eleven registered suites (`uv run python evals/evals.py [suite]`; the `graph_rag`
suite
needs the eval-only dependency group — `uv run --group evals python
evals/evals.py graph_rag` — because `ragas` (and the `langchain-community` it
hard-imports) is the harness's only consumer and stays out of the shipped
runtime closure). The external benchmarks are `swebench` (SWE-bench_Lite
instances: pinned base commit, per-instance `uv venv --python 3.11`, the
instance's own FAIL_TO_PASS/PASS_TO_PASS verdict; the test patch is applied by
the JUDGE and a gold-patch miss aborts before any model call), `deepswe`
(DeepSWE tasks: the host checkout is bind-mounted into the task's own Docker
image, only the `terminal` tool is replaced by a `docker exec` bridge, and the
task's separate verifier image grades the agent's `git diff`),
`terminal_bench` (Terminal-Bench tasks: the task image is built, the agent works
inside it through the same bridge, and the task's `run-tests.sh` pytest verdict
judges the run; multi-service compose tasks are reported unsupported),
`agentbench` (AgentBench's official DBBench test set —
https://github.com/THUDM/AgentBench, `data/dbbench/standard.jsonl` from a pinned
checkout, its official table rows materialised in SQLite, the official
`execute_sql` / `commit_final_answer` function names, and AgentBench's own
comparison rules; INSERT/DELETE/UPDATE rows are reported unsupported), and
`toolbench` (ToolBench's official test instructions + official
`test_query_ids` — https://github.com/OpenBMB/ToolBench, with the data mirror
https://huggingface.co/datasets/Adorg/ToolBench pinned for reproducibility — tool
execution through the official RapidAPI proxy, the
official answer format, and the official ToolEval evaluator imported from the
pinned repository). No suite invents its own questions: the container suites
clone their task trees into `~/.cache/sherry_evals` (never the repo); the two
new suites download the benchmarks' own data into the same cache and need
their own keys (`TOOLBENCH_KEY`, `MAIN_LLM_API_KEY`, an OpenAI-compatible
ToolEval judge key); every external suite has a `--dry-run` and SKIPS loudly
with a non-zero exit when its prerequisites are absent.
`coding_capability` is the coding baseline — it drives the REAL main
agent (`server.service.messages.async_generate`) over a self-designed task set,
judges every task with that repository's own `python -m pytest`, fails a task
whose diff touched the tests, pins the thinking switch, and reports provider
failures as invalid runs (excluded from the pass rate) instead of capability.
The suites that repoint `config.path.WORKSPACE_DIR` through
`SHERRY_WORKSPACE_DIR` (coding_capability, swebench, deepswe, terminal_bench,
agentbench, toolbench) re-exec in their own process; `baseline.json` next to
each suite is the version-controlled reference (threshold + model id).

**IMPORTANT**: `tests/agent/tools/subagent/conftest.py` installs sys.modules stubs at collection time. When running multiple test dirs in one process, use the stub-tolerant loading pattern from `tests/agent/tools/taskflow/conftest.py`. The split runner (`tests/run_tests_split.py`) avoids this by running groups in separate processes.

## Conventions

- **Python 3.13** (StrEnum, PEP 695 type aliases, typing.override)
- **Config**: one TypedDict + one instance per feature in `config/features/<side>/<name>.py`, re-exported via `__init__.py` — NEVER use bare dict literals for feature config
- **Middleware hooks**: `abefore_model(self, state, runtime=None)`, `aafter_agent(self, state, runtime=None)` — LangChain 1.3.9 signatures; after_agent runs in REVERSE list order
- **Tool registration**: `@tool("name")` + `build_*_tools()` factory + `_MAIN_TOOLS_BUILDERS` list in `agent/tools/__init__.py`
- **Test fixtures**: `isolated_db` (tmp SQLite), `build_main_tools_real` (stub-tolerant), `scan_skills_real`
- **Docs parity** (`scripts/check_docs_parity.py`): gates every four-language README group on structure plus semantic metrics — link-target sets with per-target language variants (a translation that links a foreign README the EN reference does not link fails, while the language switcher and own-language links pass), per-section body-length ratio plus a non-empty check for even the smallest EN sections, a language-independent heading-marker sequence (inline code spans + link targets, order-canonicalized) that fails a heading reordered under an unchanged level sequence, and a per-section invariant-token multiset (code spans + file paths + ENV names + numbers, matched by substring so dropped backticks are not drift) that fails a long but stale translation which lost ≥2 code-span/path tokens or all of them. Section pairing matches headings by marker fingerprint first, then occurrence index (the index-fallback count and the marker-less-reorder residual limitation are printed in the summary). The ratio band is a calibration snapshot: `--calibrate` prints the per-language distribution and its margin, and `tests/scripts/test_check_docs_parity.py` pins the smallest legitimate ratio at ≥1.15× the band so a denser legitimate section turns the test red before the band silently narrows — re-run `--calibrate` and update the constants' comment before changing any band. Exempting a group via `ALLOWLIST` is a last resort: the reason must contain an explicit "Fixing the document(s) is wrong because …" clause followed by a substantive rationale, be ≥40 chars, and may not open with placeholder text; stale entries fail the gate, and tests pin the mapping empty so every addition requires an explicit test edit
- **Commits**: angular conventional (commitlint enforced), pathspec-only (`git commit -- <files>`)
- **File naming**: snake_case enforced by pre-commit hook
- **Line length**: 100 chars (ruff)

## Known Pitfalls

- `workspace/` in `.gitignore` is anchored (`/workspace/`) — live persona files are NOT tracked
- `tests/agent/tools/subagent/conftest.py` pollutes `sys.modules` globally — use stub-tolerant imports in cross-suite tests
- `agent/core.py::init()` imports `skills` lazily and calls `build_skills_snapshot()` at assembly time (server boot); a process that never calls `init()` still gets a live disk scan from `scan_skills(use_cache=True)`
- `Summarization` has both sync (`_apply_compression`) and async (`_aapply_compression`) paths — changes must cover both
- `taskflow_resume` and `taskflow_run_task` both dispatch via `_dispatch.dispatch_child` — the seam is monkeypatchable
- after_agent hooks run in REVERSE list order — first registered = last executed
- `asyncio.Semaphore` is event-loop-bound, so lanes must be acquired on the main loop — a cross-loop `acquire()` logs a warning and rebinds a fresh semaphore with outstanding slots deducted (never double-issues permits)
- a session's BOUND project directory is snapshotted into
  `<project>/.sherry/shadow-git` before the first write of each turn
  (`agent/tools/pub_base/shadow_git.py::ensure_checkpoint`): write_file,
  patch_file, terminal and python_repl all call it, and it is fail-open — a
  broken store logs a warning and the write proceeds. The user's `.git` is
  NEVER initialized, staged, committed, reset, checked out or worktree'd; the
  shadow store uses `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` isolation and a
  single `refs/heads/main` chain. `info/exclude` carries the deny patterns
  (secrets, bulk dirs and the shadow store) before `git add -A`; snapshots are
  pruned to `SHADOW_GIT["max_snapshots"]`. Restore takes a pre-restore snapshot
  and supports one file or the whole tree; the `checkpoint` tool exposes
  list/show/restore. The git-graph panel reads the shadow chain (branch label
  `checkpoint`), and `git_head` announces `checkpoint@<hash>`. An UNBOUND
  session is untouched, and reads are never gated.
- the three tool classes that used to inherit from the archived
  `langchain-community` / `langchain-experimental` base classes
  (``FormattedWriteFileTool``, ``SafeShellTool``, ``TimedPythonREPLTool``) are
  re-based on plain `BaseTool` with the schemas reproduced locally — the field
  names, descriptions, required sets and the `str -> [str]` commands
  normalization are byte-identical, pinned by
  `tests/agent/tools/test_tool_schema_goldens.py` (which also asserts those
  modules no longer import the archived packages at all)
- file writes go through `file_write_lock` (`agent/tools/pub_base/file_lock.py`: in-process per-path `threading.Lock` → cross-process `flock`) plus `atomic_write_text_no_follow` (`atomic_write.py`); `patch_file` adds a two-layer CAS (fingerprint at read, `expected_revision` re-asserted just before the `os.replace`). Its fuzzy ladder (`pub_base/text_matcher.py`, 11 strategies, first match wins) strips a copied snippet's line numbers (`N|` / `N: ` / `N<tab>`) and unescapes `\uXXXX` before the fuzzy ones, refuses `replace_all=True` for the lossy `BROAD_MATCHERS` (a global replace must not land on a lookalike), re-indents a fuzzy replacement to the file's own indentation and rewrites its straight quotes into the region's curly ones (only when the region uses that kind exclusively — mixed code/prose keeps straight quotes).
- `write_file` needs a READ-BEFORE-WRITE LICENSE to overwrite an existing file: a complete `read_file`, or this session's own earlier whole-file write (`agent/tools/pub_base/read_state.py` — process-local, keyed by session + resolved path, advanced by the session's own append/patch, lost on restart). `read_file` itself is the first context cut: the window is bounded by BOTH `TOOLS_TIMEOUTS["read_file_token_budget"]` (4 000, CJK-aware — the same budget means the same context cost in every language) and the caller's line `limit` (the stricter wins, truncation always at a line boundary), and a re-read of an unchanged file the session READ in full answers with a pointer instead of the content. A single line whose cost alone exceeds `TOOLS_TIMEOUTS["read_file_max_line_tokens"]` (4 000) is clipped inside itself (`...[N chars clipped]...`) and reported `truncated: true` (no write license off it) — WHEREVER the line sits in the page and even when the page budget is switched off, so one minified line can never ride the response whole. Because `offset` counts LINES there is no read_file address for the rest of a clipped line: the hint names the tools that reach it (`search_files` with context, `terminal` / `python_repl`). Its revision rides into the atomic write as `expected_revision`, so a file the session never read is refused and a change landing after the read is refused too; a partial read licenses nothing, `append` alone licenses nothing, a summarized-away read loses its license (`agent/middlewares/summarization/compression.py` drops the license of every read whose `read_file` result the compression discards — the preserved tail's later reads keep theirs, and the scan is fail-open), and a directory/symlink target skips the gate (its write fails with its own error).
- `terminal` / `python_repl` / ast-grep rewrites bypass all of it (subprocesses take no lock) — that boundary is deliberate, not an oversight.
- the skills index (`<available_skills>`, built by `skills/loader.py::get_skills_text`) renders each skill's `description` plus two frontmatter signals — `when_to_use` (**required** on every skill the nudge / curator create or patch, and now carried by every shipped built-in) and `when_not_to_use` (also present on the built-ins) — all three token-bounded CJK-aware (60/50/50 tokens, `truncate_by_tokens`) and XML-escaped; the client's skill tooltip shows the same fields. The curator writes a second, narrower level into the umbrella BODY: every `##` sub-capability section opens with its own `**When to use:**` line (plus `**When NOT to use:**` when a real exclusion exists), and its deterministic fallback extracts both from the merge reasons.
- a taskflow step may bind skills (`skills: ["browser"]`): `build_task_with_dep_results` (the one task-text builder every dispatch path calls) prepends each SKILL.md via `_skill_prepend.prepend_skills`, mirroring cron's `[IMPORTANT: …]` header + fail-open binding + `bump_use` + injection scan — so the child receives the skill directly and the orchestrator never relays it through its own context. `taskflow_plan`'s planner may emit the field and `taskflow_update_steps` may change it.
- `terminal` accepts a per-call `timeout` (seconds; default `TOOLS_TIMEOUTS["terminal_timeout_seconds"]` = 30, ceiling `terminal_max_timeout_seconds` = 600 via `_resolve_timeout` — 0/negative means 'unset') and a `run_in_background` flag: a backgrounded command returns immediately with a `bg-<hex>` task id, its output goes to a RETAINED file under the session's `evicted/` (`terminal-bg-<task-id>.log`, never a sweepable temp file), and a watcher on the server loop announces the exit (WS frame `terminal_bg_exit` + a `background_task` auto-turn). `terminal_bg_list` / `terminal_bg_output` (both `scope=main_only`, catalogue group `terminal`) are how the agent reads status and a bounded 8 KiB TAIL; the per-session live task count is capped by `TERMINAL_BG` (10), and `purge_session` SIGTERMs a deleted session's running tasks before its folder goes.
- terminal HITL cards can offer **本会话允许** (`decision: "session"`): it writes the command's two-word pattern to `HITL_SESSION_APPROVED_KEY`, while `approve` stays one-shot. The option is sent only for cacheable ordinary commands; dangerous commands, confirm-all and clawhub never write it, and `check_command` moves dangerous detection BEFORE Layer 6 so a stored pattern can never auto-approve a dangerous command (permanent Layer 5 ALWAYS still wins). The card labels are four-language.
- on Windows the unsandboxed spawn prefers a Git-for-Windows `bash.exe` (`pub_base/shell_resolver.py`, standard paths then inferred from `git.exe`, cached per process): the tool promises a POSIX shell, and `cmd.exe` would quietly change what `&` / `&&` / `$()` / `2>&1` mean. No Git Bash → the platform shell.
- the eviction PREVIEW marks a mid-line cut instead of letting it read as a line ending: the in-memory renderer appends `\u2026`, the file-on-disk reader detects the cut by peeking past `_PREVIEW_LINE_MAX_BYTES` (and, for the tail window, by checking whether the byte before the walk's stop is a newline), and either path appends one note naming the same fallback tools — a normal preview is byte-identical to before (the note is absent, never empty).
- `terminal`'s stdout is fd-direct-written: BOTH spawn sites (`_execute_sync` and `_arun`) hand the child a FILE for `stdout` (stderr merged into it via `STDOUT`, unchanged) opened as `.sherry-tmp-stdout-*.log` in the session directory, then wait on the process instead of draining a pipe. Under `TOOLS_TIMEOUTS["fd_write_threshold_bytes"]` (100 000 bytes) the file is read whole and unlinked; at or above it, it is renamed to `SESSIONS_DIR/<session_id>/evicted/terminal-<sha256(first 64 KiB)[:16]>.log` and only a head/tail preview comes back (`pub/func/message/eviction.py::build_file_preview` → `_read_head_tail`, byte-budgeted: 8 KiB per side and 2 KiB per line, so a newline-free 500 MB output is still a few KB — the total is `stat().st_size` BYTES, lines are never counted). A timed-out/cancelled/failed run discards its temp file; leftovers from a hard kill are swept by the shared `.sherry-tmp-` + `.swp`/`.log` contract in `atomic_write.py` (sweeping runs on the way into the next `_open_stdout_tmp`).
- the sandbox IS the write barrier for `terminal` / `python_repl` / `execute_code`
  (they never call `resolve_external_path`): `SandboxBackend.wrap(cmd, env,
writable_roots)` takes the caller's write surface, which each tool resolves from
  the SESSION's project directory (so a project bound outside the checkout is
  writable and the checkout itself is not), `TEMP_DIR` is appended centrally, and
  an empty list raises. The tracked `.env` ships `SANDBOX_POLICY=auto` (the same
  as the unset fallback: the packaged desktop app has no OS sandbox backend on
  macOS/Windows, where `required` would refuse every command — a server
  deployment that wants the hard guarantee sets `required` itself) — boot logs
  the effective policy +
  backend, an unusable `required` logs an ERROR with fix commands,
  `SANDBOX_STRICT_BOOT=1` refuses to start, and `GET /sandbox/status` reports the
  same state. Every bare run is recorded: the evidence-ledger row carries
  `sandboxed: false` and the bypass approval card is marked `⚠ UNSANDBOXED`.
  `PathGuard` additionally screens the command-string arguments (`commands` /
  `query`) against the hard-deny floor only. The read-surface whitelist
  (`SANDBOX_READ_ALLOWLIST`, system trees + `SANDBOX_READ_EXTRA`, else the whole
  fs is readable) is OFF until `scripts/sandbox_smoke.py` passes on a host whose
  sandbox actually runs — neither backend has ever been verified on one.
- An opt-in `sessions_spawn(isolation=True)` gives a child its own GIT WORKTREE of the project (`agent/tools/subagent/isolation/`), cut from a dirty baseline (`git stash create`, so uncommitted edits are visible; untracked files are copied in, ignored paths materialized as links/copies per `SUBAGENT_ISOLATION`, and a non-repository project is auto-initialized with a deny-listed baseline commit) whose changed files merge back at announce time under a per-root `flock` with the same revision CAS — conflicts are reported by path and left untouched, symlinks/ignored caches never merge, and every merged code file is symbol-diffed before it is written so the completion reply carries an **Interface changes** section (renamed/added/removed functions, methods, classes — `agent/tools/code_intel/symbol_diff.py`, gated by `SUBAGENT_ISOLATION["interface_diff_enabled"]`, fail-open and capped); it is ONE LEVEL: the runtime `sessions_spawn` tool carries `isolation`, and a spawn whose cwd already sits inside an isolated worktree reuses that tree (`isolation=True` is ignored there, info-logged, never refused — a child of the isolated ORCHESTRATOR works in the same tree, and its changes ride that one merge back; the check is a path test against `isolated_workspaces_dir()`, not a git probe); the docs page is `wiki/file-safety/README.md`. A workspace no live run owns is cleaned by the stale-workspace sweep (`agent/tools/subagent/isolation/sweep.py`, every sweeper cycle and once at boot; conflict keeps expire after `SUBAGENT_ISOLATION` `stale_workspace_ttl_days`).
- a revert never touches the conversation: it appends one `<revert>user had revert editing …</revert>` AIMessage per revert (an `aupdate_state` write the persistence layer mirrors into the store), and the agent reads what was undone on its next turn.
- the todo-continuation middleware is named `PlanContinuationEnforcer` (it supervises the plan = todos + TaskFlow flows), and TWO strings are deliberately frozen: `message.metadata["origin"]` stays `"todo_continuation"` (historical rows keep resolving) and `agent_switch._RENAMED_MIDDLEWARES` maps `PlanContinuationEnforcer → {TodoContinuationEnforcer}` so a preset payload written before the rename still switches it off. Its turn-end hook also counts turns without a `todowrite` (`stagnation_tracker.record_turn` / `record_todowrite`, threshold `TODOLIST_INFRA["todo_write_nudge_turns"]` = 10) and APPENDS a `[REMINDER: TODO TRACKING]` nudge to the continuation directive once it is crossed — the one shape the snapshot-based stagnation check cannot see (an agent advancing TaskFlow clears the streak while the todo list rots). Recovery mode carries its own todowrite guidance, so the nudge stays out of that branch. The same blind spot (a plan edited just enough to keep the snapshot moving) is bounded by the continuation budget: `TODOLIST_INFRA["max_continuation_turns"]` (15) confirmed deliveries → one stop directive + a session latch that only a turn the gate did NOT inject clears.
- a middleware row in 预设角色-中间件 must never let its label squeeze the switch: PrimeVue's `ToggleSwitch` cannot take a fall-through class from a fragment root, so each row wraps it in a `span.shrink-0` (the label column keeps `min-w-0 truncate`) and the row's help affordance is a real `?` button that toggles the explanation inline (hover tooltips are unreachable in an embedded webview).
- the `<system-reminder>` layer's contract lives in `pub/func/message/system_reminder.py` (`provenance="reminder_catalog"` + `internal=True` + the source id as `origin`), mirroring the workspace-notice leaf so the scanners that walk human messages can skip both: `split_into_turns` (a reminder is not a turn boundary), HITL's headless-turn check and the summarizer's serializer (`[System notice]` / `system`, never `[User]`). Wrapping escapes nested tags — todo text is user-influenced.
- user-configurable lifecycle hooks (`agent/middlewares/user_hooks/`, registered
  after `ToolResourceLockMiddleware`, which itself sits right after
  `ToolGuardrails` — a guardrail-BLOCKED call never reaches a hook):
  the `sherry.jsonc` `hooks` section names commands per event and the middleware
  dispatches them as SUBPROCESSES with the turn's identity. Step 1 only —
  `PostToolUse` / `PostToolUseFailure` (result/error previews bounded by
  `USER_HOOKS["*_preview_chars"]`, `durationMs` measured right here) and `Stop`
  (`responsePreview` + `toolCallCount`); `PreToolUse`, `UserPromptSubmit`,
  `SessionStart` and the `updatedInput` rewrite are NOT dispatched (the config
  parser still validates them, and the constructor logs a warning naming each
  ignored event once) because blocking/rewriting hooks need a trust gate this
  build does not have. The dispatcher NEVER blocks a turn: `shlex.split` and no
  shell, a scrubbed env (`scrub_env` + `SHERRY_SESSION_ID`/`SHERRY_HOOK_EVENT`),
  a per-hook timeout that kills the process, stdout capped at
  `USER_HOOKS["max_output_bytes"]` and parsed as OPTIONAL outcome JSON (non-JSON
  = success, and `decision`/`updatedInput` are parsed but ignored), stderr
  logged (capped) and never shown to the model, and every failure is a log line
  — the tool result rides through untouched. `hooks.enabled` defaults OFF
  (`USER_HOOKS["enabled"]`): the config file is git-tracked, so executing its
  commands must be a deliberate act. Added to `MIDDLEWARE_ORDER` +
  `GATEABLE_MIDDLEWARES` (the 预设-中间件 switch calls `middleware_enabled`), and
  the config is read once at construction (change = restart).
- the TaskFlow loop closes itself: a child spawned by a flow resumes it
  automatically (`agent/tools/subagent/announce/delivery.py::_try_auto_resume_taskflow`
  finds the owning flow by scanning active flows' steps for the child key), and
  `taskflow_resume` then dispatches the wave it unlocked
  (`auto_dispatch=True` default; `auto_dispatch=False` keeps the "unlock only"
  shape). The AI still supervises the exceptions — judge BLOCK, failures,
  planning and the final finish — instead of driving every step. `taskflow_replan`
  regenerates a flow's REMAINING steps from the goal, the done results and the
  failure reason (transitively invalidated steps go back to `blocked`; bounded by
  `REPLAN_MAX` replans per flow), `taskflow_plan` decomposes a goal into a
  dependency-ordered flow without dispatching anything, and `taskflow_finish`
  gained **Gate E** (`_goal_gate`): with a flow description present, an auxiliary
  model judges whether the collected results actually satisfy the goal (fail-open;
  a rejection names `taskflow_replan`).
- TaskFlow dispatch is **serialized per flow** and planning is idempotent:
  `_dispatch.flow_dispatch_lock(flow_id)` (process-local, keyed by loop + flow)
  wraps the manual `taskflow_dispatch` read → validate → spawn → persist body
  and the resume auto-dispatch wave, which re-checks each step is still `ready`
  after acquiring it — two dispatchers cannot spawn the same step twice. A
  multi-process deployment would need a DB lease. `taskflow_plan` generates
  `plan-<sha256(session_id + goal)[:10]>` (stable across processes, session
  scoped because `flow_id` is a global PK); repeating a goal/session returns
  the existing non-terminal flow as an idempotent success, and a duplicate
  non-terminal `taskflow_create` does the same (terminal flows still error).
  A new signature-based tool-loop middleware was **not** added: 204 historical
  tool calls showed zero consecutive same-name + same-args successful mutating
  runs; `ToolGuardrails` remains the detector.
- TaskFlow orphan collection is a **tri-color GC plus liveness** (v3), replacing
  the per-scenario fixes: every `taskflow_create`/`taskflow_plan` binds a
  flow-level `pending` todo (the GC root; `step_id` empty, so it stays active
  until the whole flow is terminal) — a missing `todo_content` falls back to the
  description/flow id rather than creating an unrooted flow. The sweeper marks
  active-todo `flow_id`s as the reachable/black set and fails non-terminal,
  non-superseded flows with no root and an `updated_at` older than
  `TASKFLOW_INFRA["tri_color_grace_seconds"]` (300 s — the grace covers the
  create/bind race between the two SQLite files) as `orphan_unreferenced`.
  Liveness then handles stuck-but-referenced flows: a child-session flow with
  no live run is collected after the short grace, a main-session flow is
  collected after `stale_action_hours` (12 h) of inactivity
  (`session_dead` when no socket/turn/run exists, `stale_swept` when the
  session is live), a live child vetoes, and active todos referencing a
  failed/cancelled flow are cancelled. Both passes run at boot (before the
  first 60 s tick) and every tick; `task_flows` gains additive `updated_at`
  (bumped by every mutation) and `terminal_reason` columns. A superseded
  predecessor becomes `cancelled`/`superseded`; `taskflow_finish` has three
  independent budgets (nudge / consecutive failures / protocol violations)
  that auto-fail and record detached children; terminal rows are pruned to
  `max_total_flows_per_session` (200) on a debounced post-mutation pass.
- a dispatch wave spawns CONCURRENTLY, bounded by
  `TASKFLOW_INFRA["max_parallel_dispatch"]` (2; 1 restores the historical
  sequential behaviour) through `asyncio.gather` + a semaphore — the three
  dispatch loops (`taskflow_dispatch`'s batch, `taskflow_resume`'s
  auto-dispatch wave and `taskflow_wait_all`'s retry pass) all do. The SUBAGENT
  lane is the global backpressure; this cap only decides how many spawn calls
  are in flight at once. A partial wave failure persists every successful child
  (via `update_flow_with_conflict_retry`, so a concurrent writer cannot drop a
  spawned key) and names the failed step ids (`auto_dispatch_failed=…` on the
  resume answer). `taskflow_resume`'s normal path also writes through that
  CAS-retry: the `build_state` callback re-applies the ALREADY-DECIDED verdict +
  result onto the freshly-read state and re-derives the unlock set from what was
  actually persisted — two children completing at once both land, and the same
  step is spawned exactly once. A superseded flow releases its per-session slot.
- two nullable `task_flows` columns record flow lineage (`resumed_from` /
  `superseded_by`, additive migration): `taskflow_create(resumed_from=…)` —
  the CREATE path, since `taskflow_replan` rewrites in place and never forks —
  writes the successor's forward pointer and stamps the predecessor's
  back-pointer with a plain session-scoped UPDATE that deliberately does NOT bump
  `expected_revision` (lineage is metadata). `get_active_flows_sync` excludes a
  superseded flow even while it is still `running`; `get_all_flows_sync(all)`
  and `taskflow_summary` still show it.
- `_try_auto_resume_taskflow` only reports success when the resume answer is not
  an `Error:` (the resume genuinely failed → an error log, not an info log; the
  child's completion is still recorded, so nothing is lost).
- tool duration is measured ONCE, at tool-return time, by `message_persistence`'s `wrap_tool_call`/`awrap_tool_call`
  (`time.monotonic()` difference, rounded, clamped at 0 — never a wall-clock subtraction: an NTP step back would make it
  negative, which is the defect ZCode ships unguarded), stamped into `ToolMessage.additional_kwargs["tool_duration_ms"]`
  and persisted to `messages.tool_duration_ms` (old DBs heal by ADD COLUMN; old rows read NULL). The stream's
  `tool_result.duration_ms` is the same measurement for the live card (`stream_dispatch`'s `_tool_started_at`, per
  session + tool id, consumed once). Approval waits and queue time are OUTSIDE the window by construction — the clock
  starts when the tool actually executes. The client shows a live ticker while running and the measured value when
  settled (`formatToolDuration`: <1s → `850ms`, <10s → `1.2s`, else whole seconds), and nothing when unknown.
- the code-intelligence index follows the SESSION's project directory
  (`agent/tools/code_intel/roots.py::index_db_for_root` → `<root>/.sherry/codeintel/index.db`),
  so alternating between projects no longer rebuilds one shared index (the stored
  paths are project-relative); `code_intel_index_db_path` pins one and
  `SHERRY_CODE_INTEL_DB` overrides the path, with the shared `CODE_INTEL_DIR`
  as the no-root fallback. The index root itself is resolved PER CALL
  (env override > session project dir > process default), like ast-grep and LSP
- the code-intel semantic layer is OPTIONAL and CODE-EMBEDDER-ONLY: BGE remains
  the text embedder (`context_engine` / multimodal RAG) and is never used here.
  `CODE_INTEL_SEMANTIC["enabled"]` gates registration; without a valid profile
  (`jina-embeddings-v2-base-code`, ONNX, 768d, Apache-2.0, under
  `models/embed_model/code/`) the tool is absent and no model/index is touched.
  The Jina backend is lazy (`onnxruntime` + `RobertaTokenizerFast`, mean pooling);
  switching profiles purges the old `code_embeddings` rows, including legacy
  `bge-m3` rows, so no BGE vector can be scored. It is OFF by default: merely
  having the files is not enough — the 模型配置 panel downloads the model and
  writes the opt-in marker (`GET /code-embedder/status`, `POST /code-embedder/download`,
  `DELETE /code-embedder` disables; the download runs in a background thread).
- truncation is budgeted in TOKENS, CJK-aware (`pub/func/message/token_truncation.py`):
  tool outputs, tool args, the summary body, the aggressive pass and every
  SummaryDoc list item go through `truncate_by_tokens` (`evicted_refs` is exempt —
  a clipped path is useless), so the same budget costs the same in every language
  (the old character caps ÷ 4 keep English byte-identical)
- two boot sweeps reconcile crash leftovers, both conservative and both run from
  the server assembly (`server/trigger/channels/core.py::_schedule_sweeper`, next
  to the sweeper start): `agent/tools/subagent/isolation/sweep.py` removes
  isolated workspaces no REGISTRY run owns (orphaned / expired-conflict past
  `SUBAGENT_ISOLATION["stale_workspace_ttl_days"]` / corrupted) plus a
  `git worktree prune` on their parents, and
  `server/service/session_dir_sweep.py` removes `SESSIONS_DIR/<id>/` folders NO
  store references (messages / todos / taskflows / checkpoints all probed,
  fail-open per store; symlinks and folders younger than
  `SESSION_DIRS["min_dir_age_seconds"]` are skipped)
- `.gitignore` line `*.db` ignores all SQLite files — DB files are never committed
- pre-push hook runs basedpyright on the entire diff — must be 0 errors before push
