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

| Directory | Purpose | Key entry point |
|---|---|---|
| `agent/` | Agent core: middleware chain, tools, subagent system, checkpointer | `agent/core.py::built_agent()` |
| `agent/middlewares/` | Middleware pipeline (summarization, guardrails, HITL, intent, continuation, memory_flush, message_persistence) | `agent/middlewares/__init__.py` |
| `agent/tools/` | LLM-callable tools (taskflow, todolist, memory, subagent, file, search, ...) | `agent/tools/__init__.py::build_main_tools()` |
| `agent/tools/taskflow/` | Task orchestration engine (DAG, budget, deadline, progress, board, step judge, expectation→actual closure, evidence collector) | `agent/tools/taskflow/config.py` |
| `agent/tools/todolist/` | Session-scoped todo planning layer + append-only evidence ledger/recorder | `agent/tools/todolist/service.py` |
| `agent/tools/subagent/` | Multi-level subagent system (spawn/registry/announce/sweeper, completion judge, functional roles) | `agent/tools/subagent/spawn/core.py` |
| `agent/tools/pub_base/` | Shared tool infrastructure (`BaseSQLiteRepository` for the three SQLite stores, path utils, skill usage) | `agent/tools/pub_base/sqlite_store.py` |
| `agent/wrapper/` | Graph-level wrappers (repetition guard, context limit) + pluggable registry | `agent/wrapper/registry.py` |
| `agent/state_port.py` | The transcript port: read the live message list / pending HITL interrupt, heal an interrupted turn (server calls this, never the checkpoint API) | `agent/state_port.py` |
| `agent/trajectory_events.py` | Agent-side producers of trajectory events (reaches the server-side ledger through `runtime.hooks`) | `agent/trajectory_events.py` |
| `server/service/trajectory_store.py` | The per-turn trajectory ledger: one row per tool call (start/end merged), plus turn/compact/error events | `server/service/trajectory_store.py` |
| `config/` | Centralized configuration (paths, features TypedDicts, schema, settings) | `config/__init__.py` |
| `config/features/` | Per-object feature config (71 TypedDicts) | `config/features/__init__.py` |
| `server/` | Robyn HTTP/WS backend (trigger → service → queue/DAO → utils) | `server/__main__.py` |
| `context_engine/` | Memory engine (MesMemory SQLite + curator) | `context_engine/store/db.py` |
| `workspace/` | Live persona files (gitignored; templates in `workspace/template/`) | `workspace/prompt_builder.py::build_system_prompt()` |
| `pub/` | Shared utilities (message pipeline, retry, validators) | `pub/func/message/` |
| `models/` | LLM/model wrappers (main, reasoner, auxiliary, vision, embed, reranker) | `models/LLMs/main_llm.py` |
| `runtime/` | Runtime state registers (`session/`; canonical keys in `state_keys.py`) + process-level services (`process/`, `lane/`) + dependency-inversion seams (`hooks.py`, `data_provider.py`) | `runtime/session/state_register.py` |
| `runtime/hooks.py` | Process-level callback registry — `agent`/`skills` resolve server-owned callables (auto-turn trigger, WS task table, skill scan) without importing `server` | `runtime/hooks.py` |
| `runtime/data_provider.py` | Prompt/skill-write provider registries (`PromptDataProvider`/`SkillWriteProvider`) — `workspace`/`context_engine` obtain agent-owned data without importing `agent` | `runtime/data_provider.py` |
| `runtime/lane/` | Process-level concurrency lanes (`Lane`/`LaneManager`/`lane_slot`/`LaneType`) | `runtime/lane/core.py` |
| `tests/` | Mirror-structured pytest suite (markers: unit/integration/module/system/regression/perf) | `tests/run_tests_split.py` |
| `skills/` | SKILL.md skill system (builtin/auto/plugins) | `skills/loader.py::scan_skills()` |
| `docs/` | VitePress documentation site | `docs/long-running-tasks/README.md` |
| `docs/context-governance/` | Context governance docs (persistence, eviction, slices, overflow clip, summary filtering) | `docs/context-governance/README.md` |
| `scripts/` | Dev tooling (git hooks, maintenance scripts) | `scripts/hooks/` |

## Architecture

```
User message → Robyn WS → agent.core.built_agent() graph
  │
  ├─ middleware chain (before_agent → before_model → LLM → tools → after_model → after_agent)
  │    system_prompt_injection (@dynamic_prompt) → WorkspaceNotice → ToolSelection → MultimodalProcessor → IterationBudget
  │    → ToolGuardrails → ContextEviction(P0-2/P2-4) → ToolCallNormalize → PathGuard → SubagentCompletionDrain
  │    → TaskIntent(E7) → DynamicReminderMiddleware → OutputRepetitionGuard → MaxTokensBoost → ThinkingControl → HeartbeatStaleness → HITL
  │    (TaskIntent's intent detection is TWO layers: four-language rules — EN/ZH/JA/KO
  │     keywords plus question/chat patterns, then a length fallback over 100 chars —
  │     and an auxiliary-LLM classification that runs ONLY for first arming, ONLY in the
  │     20-100 char window with request phrasing (してください / 해줘 / please / 帮我) and no
  │     keyword; armed sessions and session-less calls stay rule-only, and every failure
  │     path is fail-open to "not a task")
  │    → MessagePersistence → LLMRetry → Summarization → PlanContinuationEnforcer(E3; gates the plan = todos + open TaskFlow flows; delivered through the auto-turn trigger, which DEFERS past the ending turn's own `ws_task`/`answering` busy signals — up to `TODOLIST_INFRA["continuation_self_idle_wait_s"]` (30 s) — because the enforcer's hook runs while that stream is still draining; a directive still refused at the cap means a real user turn took over and is dropped. The client cannot stream that turn (it sends no `turn_started`), so `use-chat-stream.ts` refetches the newest turn page on its `done` — without that, the continuation ran but stayed invisible until a manual reload)
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
  │     plan mode, working directory, todo list, boulder, TaskFlow board,
  │     continuity, knowledge index — the catalogue in
  │     `agent/middlewares/reminder_catalog/` decides what, at which priority, with
  │     which cooldown and under a 30 000-char total budget (lowest priority
  │     dropped first). They used to be REBUILT INTO THE CACHED SYSTEM PROMPT,
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
  │     reaches state; read_file results are sliced, never offloaded. Its
  │     before_model tags an oversized trailing HumanMessage (full text stays in
  │     state/MesMemory) and wrap_model_call truncates only the model view)
  │    ThinkingControl: per-session thinking toggle (client switch →
  │     PUT /sessions/thinking → state register); swaps request.model for the
  │     thinking on/off variant built by build_main_llm(thinking=...) when the
  │     session carries an explicit LLM_THINKING_ENABLED flag, else env default
  │
  ├─ tools: build_main_tools() → taskflow(14) + todolist(2) + memory + subagent(7)
  │         + file_tools + web_search + terminal + python_repl + question + ...
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

  completion gates (all always-on, all fail-open):
    · Tier 1 schema gate — a step's response_schema is validated on
      taskflow_resume (json parse + validate_structured_output); a miss is a
      retry signal ("schema_error") that never spends a judge call, and an
      unretried miss marks the step failed
    · StepJudge — step_judge.py, on taskflow_resume for criteria-bearing steps
      (judge_criteria or validation_criteria; pass / retry within
      STEP_JUDGE["max_retries"] / block); a Tier 1 pass never skips it, and a
      caller-declared step_outcome outranks both
    · CompletionJudge goal loop — every spawn, bounded by
      COMPLETION_JUDGE["goal_max_turns"]; continue injects a follow-up turn
    · Evidence ledger — always-on terminal/python_repl auto-record + file-edit
      stale events; read side derives staleness (evidence_collector.py)
    · taskflow_finish gates A–D — DAG completeness, no unresolved steps
      (blocked/failed/skipped/cancelled are reported by id with their reason),
      no FAIL/[stale] evidence, SisyphusVerifier (only with todo + plan_path)
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
the summarizer / memory-flush serializers (labelled `[System notice]` /
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
to that session, so it opens under 当前会话 like the commit diff). The sidebar's 工作目录 body stacks TWO collapsible sections —
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
human's own shell, loopback-only via the gateway). Both panels keep their state in
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
  flow — `<app_home>/sherry-backend/sherry-backend` present *and* its `VERSION`
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
  provider / endpoint / key / local-model switch for main, auxiliary, reasoner,
  ITTT, VTTT, TTI, reranker, embedding and STT, plus the scanner's LLM
  selection. Loaded by `config/models_settings.py`.
- **`.network`** (gitignored; template `.network.example`) — network services:
  `TAVILY_API_KEY` + the `SEARXNG_*` backends, the `SHERRY_BROWSER_*` switches
  and `LANGSMITH_*` tracing (moved out of `sherry.jsonc`, which is tracked and
  must not hold a key). Loaded by `config/network_settings.py`.

Both files are JSON5 (flat ``"KEY": value,`` lines; comments and trailing commas
allowed). The loaders SEED a missing file from its template, overlaying the
values still found in a legacy `.env` (and, for the tracing keys, in
sherry.jsonc's old `LANGSMITH` object), then `hydrate` every key into
`os.environ` so the existing readers (`os.getenv`, the import-time feature
snapshots, subprocess scrubbing) are unchanged. Precedence is **explicit process
variable → `.models` / `.network` → legacy `.env` → registry default**: the
hydration skips an already-set key, and `config.path` hydrates BEFORE it
(re)loads the environment file — `config.path.load_env()` is the single reload
seam (bare `load_dotenv(..., override=True)` calls were replaced by it).

Drift is refused, not merged: `server/service/env.py` treats the moved keys as
`SPLIT_OUT_KEYS` (the two registries are the source of truth) — a legacy `.env`
line is neither shown by `GET /env` nor accepted by `PUT /env` (the error names
the file to edit). `GET/PUT /models-config` and `GET/PUT /network-config`
(mirroring `/sherry-config`) are the writers; the 模型配置 tab merges the three
sources and routes each group's save back to its own endpoint, while the
模型联网 tab writes Tavily / SearXNG through `PUT /network-config`.
`.env` no longer gets a `.env.bak` (it is tracked — git is the backup);
`.models` / `.network` keep one.

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
  + `scaffolding.MAIN_REQUIRED_NAMES`, `roles/loader.py`, `skills/loader.py::skills_catalog`);
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
  (`schedule_compression_nudges`) and the plan-completion extraction fork
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
  build now fails without them (`scaffolding._MAIN_REQUIRED`, 14 entries) and the service
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
``auth_account`` row (``CHECK (id = 1)`` — the schema cannot hold a second) is
identified by nothing, login takes just the password, and a username-era
``auth_users`` table is copied into it at init and dropped
(``_MIGRATION_DDLS`` in ``server/DAO/auth_store.py``). The JWT carries only
``sub``/``jti``/``typ`` — no personal data.

**Brute force is throttled, injection has no surface.** ``_LoginThrottle``
(``server/service/auth_service.py``) counts consecutive failures per CLIENT
SOCKET ADDRESS and escalates: ``AUTH["login_max_attempts"]`` free tries, then
``login_lockout_base_seconds`` doubling to ``login_lockout_max_seconds``
(a locked client is refused BEFORE any hashing, 429 + ``retry_after``), and a
success clears the streak. Every query in the store is parameterized
(``?`` placeholders only — never string-built SQL), which the test suite pins
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
switch; `SHERRY_AUTH_ENABLED` only *seeds* a fresh row) and
`auth_jwt_blacklist` (`expires_at` in JWT seconds). Server tests isolate it with
the autouse `isolated_auth_store` fixture — no test may touch the real file.

**Client**: `pages/login.vue`, `middleware/auth.global.ts` (per-navigation
`/auth/status` decision), `stores/auth.ts`, `pages/home/components/AccountSettingsPanel.vue`
(menu → 账户, a right-sidebar tab), `composables/bridge/auth.ts` (raw transport:
codes in, no generic toasts) and the transport's own 401 → refresh-once → replay
(`requestApi.refreshSessionOnce`, single-flight).

## TaskFlow Progress (`agent/tools/taskflow/waves.py`)

Two reporting views, both pushed (never polled) to the chat's floating progress
panel — top-right overlay in `ChatBox.vue`, collapsed to a pill by default:

* **Todos** — the existing `todo_updated` push from
  `agent/tools/todolist/service.py::_push_todo_update`.
* **Waves** — `waves.py` groups a flow's steps into longest-path DAG levels
  (reporting only: the scheduler still unlocks each step on its own
  `depends_on`; an unknown dep id is ignored and a cycle lands in a trailing wave
  flagged `cyclic` instead of looping). Every flow mutation calls
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

## Agent-Controllable Browser (`config/features/infra_side/browser_agent.py`)

Opt-in and OFF by default (`SHERRY_BROWSER_AGENT_ENABLED=1` + restart; the
「模型配置」 panel grows a `SHERRY_BROWSER` group for its switches (they live in
`.network`)). With the feature off no `browser_*` tool is registered, every
`/browser/*` route answers 404 and no Chromium is ever spawned.

**Server side** — `server/service/browser_cdp.py` is a thin CDP transport (one
WebSocket, JSON-RPC ids, flat sessions, *droppable* screencast events) plus the
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

| Lane | Constrains | Default | Config key |
|---|---|---|---|
| `MAIN` | main-agent turn (`_run_executor`) | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` (12) → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | child-agent executions (spawn + steer) | 8 | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | nudge/persistence calls (`summarization/nudges.py`, 3 sites) | 4 | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` reply turns (serial) | 1 | `LANE_SYSTEM["nested_max_concurrent"]` |

- Config: `config/features/infra_side/lane_system.py`; `validate_lane_config()` runs at server startup and enforces `main >= subagent + nudge` (all limits ≥ 1); `install_lane_lifecycle()` in `server/service/lane_lifecycle.py` validates, prewarms the manager, registers `set_drain_check(is_gateway_draining)`, and installs a bounded exit drain (`atexit`, `drain_all(timeout=0)` — never blocks exit).
- Queueing: a spawn that passes per-parent admission but exceeds the SUBAGENT lane is registered `PENDING` (no `forbidden`); `PENDING → RUNNING` happens inside the lane slot and only then is `started_at` stamped (queue wait is not run time). `PENDING` counts as active in registry queries and is killable; restore finalizes restart-leftover PENDING runs the same way at startup (silently — no announce), and orphan recovery covers same-lifetime lost tasks with `ended_reason="pending_orphaned"`.
- Drain mode: `set_drain_check(fn)` makes `Lane.acquire()` refuse (without consuming a permit) while the subagent gateway reports draining.
- Observability: `GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`.
- Hot updates (`LaneManager.set_concurrency`) only affect new acquires; in-flight slots keep their permits.

## Key Configuration

| File | Contents |
|---|---|
| `config/features/agent_side/` | 44 per-object TypedDicts (summarization, guardrails, tool_result_eviction, iteration, memory_flush, taskflow_infra, todolist_infra, tools_timeouts, step_judge, completion_judge, evidence_ledger, subagent_isolation, ...) |
| `config/features/infra_side/` | 27 per-object TypedDicts (gateway, auth, bus, http_upload, retry_backoff, server_http, ws_stream, input_queue, heartbeat, cron, skill_scanner, mes_memory, curator, web_search, model_pricing, ...) |
| `config/features/__init__.py` | Aggregator — all 71 TypedDicts + instances re-exported |
| `config/path.py` | All filesystem paths (ROOT_DIR, SKILLS_DIR, WORKSPACE_DIR, ...) |
| `config/schema.py` | Pydantic Config (SHERRY_ env prefix, mostly unused at runtime) |
| `config/sherry_settings.py` | sherry.jsonc loader (TOOL_CALL_TIMEOUT_MINUTES, LOG_LEVEL, curator.*, LANGSMITH.*) |

## Code Graph

CodeGraph MCP (`@colbymchenry/codegraph`, wired in `opencode.json`) indexes the repo into `.codegraph/codegraph.db` (gitignored) — a 20-language AST knowledge graph (Python/TS/Rust full; `.vue` gets no AST). One MCP tool, `codegraph_explore`, returns verbatim source + call paths (dynamic dispatch included) + blast radius. Auto-syncs on file events (2s debounce). CLI: `codegraph query|explore|callers|impact|status`. Rebuild index after big restructures: `codegraph init`. It is a snapshot — verify against source when editing.

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

`evals/` sits beside the pytest tree: a homegrown, sandboxed framework with seven
registered suites (`uv run python evals/evals.py [suite]`). `swebench` is the
EXTERNAL benchmark — SWE-bench_Lite instances run through the same harness with
the instance's own verdict (pinned base commit + a per-instance `uv venv
--python 3.11` with the checkout installed; the agent sees only the problem
statement, the test patch is applied by the JUDGE, and an instance whose gold
patch does not resolve it fails a harness check before any model call).
`coding_capability`
is the coding baseline — it drives the REAL main agent
(`server.service.messages.async_generate`) over a self-designed task set, judges
every task with that repository's own `python -m pytest`, fails a task whose
diff touched the tests, pins the thinking switch, and reports provider failures
as invalid runs (excluded from the pass rate) instead of capability. It is the
one suite that must run in its own process: it repoints `config.path.WORKSPACE_DIR`
through `SHERRY_WORKSPACE_DIR` — read when `config.path` is first imported, and
unset in every other launch — at a throwaway directory, so a measurement run
never reads or writes the live persona, sessions or memory. `baseline.json` next
to the suite is the version-controlled reference (threshold + model id).

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
- file writes go through `file_write_lock` (`agent/tools/pub_base/file_lock.py`: in-process per-path `threading.Lock` → cross-process `flock`) plus `atomic_write_text_no_follow` (`atomic_write.py`); `patch_file` adds a two-layer CAS (fingerprint at read, `expected_revision` re-asserted just before the `os.replace`). Its fuzzy ladder (`pub_base/text_matcher.py`, 11 strategies, first match wins) strips a copied snippet's line numbers (`N|` / `N: ` / `N<tab>`) and unescapes `\uXXXX` before the fuzzy ones, refuses `replace_all=True` for the lossy `BROAD_MATCHERS` (a global replace must not land on a lookalike), re-indents a fuzzy replacement to the file's own indentation and rewrites its straight quotes into the region's curly ones (only when the region uses that kind exclusively — mixed code/prose keeps straight quotes).
- `write_file` needs a READ-BEFORE-WRITE LICENSE to overwrite an existing file: a complete `read_file`, or this session's own earlier whole-file write (`agent/tools/pub_base/read_state.py` — process-local, keyed by session + resolved path, advanced by the session's own append/patch, lost on restart). `read_file` itself is the first context cut: the window is bounded by BOTH `TOOLS_TIMEOUTS["read_file_token_budget"]` (4 000, CJK-aware — the same budget means the same context cost in every language) and the caller's line `limit` (the stricter wins, truncation always at a line boundary; a single line longer than the whole budget is clipped inside itself and reported `truncated: true`, so it grants no license), and a re-read of an unchanged file the session READ in full answers with a pointer instead of the content. Its revision rides into the atomic write as `expected_revision`, so a file the session never read is refused and a change landing after the read is refused too; a partial read licenses nothing, `append` alone licenses nothing, a summarized-away read loses its license (`agent/middlewares/summarization/compression.py` drops the license of every read whose `read_file` result the compression discards — the preserved tail's later reads keep theirs, and the scan is fail-open), and a directory/symlink target skips the gate (its write fails with its own error).
- `terminal` / `python_repl` / ast-grep rewrites bypass all of it (subprocesses take no lock) — that boundary is deliberate, not an oversight.
- the skills index (`<available_skills>`, built by `skills/loader.py::get_skills_text`) renders each skill's `description` plus two optional frontmatter signals — `when_to_use` (**required** on every skill the nudge / curator create or patch) and `when_not_to_use` (optional) — all three token-bounded CJK-aware (60/50/50 tokens, `truncate_by_tokens`) and XML-escaped; the client's skill tooltip shows the same fields. The curator writes a second, narrower level into the umbrella BODY: every `##` sub-capability section opens with its own `**When to use:**` line (plus `**When NOT to use:**` when a real exclusion exists), and its deterministic fallback extracts both from the merge reasons.
- a taskflow step may bind skills (`skills: ["browser"]`): `build_task_with_dep_results` (the one task-text builder every dispatch path calls) prepends each SKILL.md via `_skill_prepend.prepend_skills`, mirroring cron's `[IMPORTANT: …]` header + fail-open binding + `bump_use` + injection scan — so the child receives the skill directly and the orchestrator never relays it through its own context. `taskflow_plan`'s planner may emit the field and `taskflow_update_steps` may change it.
- `terminal` accepts a per-call `timeout` (seconds; default `TOOLS_TIMEOUTS["terminal_timeout_seconds"]` = 30, ceiling `terminal_max_timeout_seconds` = 600 via `_resolve_timeout` — 0/negative means 'unset') and a `run_in_background` flag: a backgrounded command returns immediately with a `bg-<hex>` task id, its output goes to a RETAINED file under the session's `evicted/` (`terminal-bg-<task-id>.log`, never a sweepable temp file), and a watcher on the server loop announces the exit (WS frame `terminal_bg_exit` + a `background_task` auto-turn). `terminal_bg_list` / `terminal_bg_output` (both `scope=main_only`, catalogue group `terminal`) are how the agent reads status and a bounded 8 KiB TAIL; the per-session live task count is capped by `TERMINAL_BG` (10), and `purge_session` SIGTERMs a deleted session's running tasks before its folder goes.
- on Windows the unsandboxed spawn prefers a Git-for-Windows `bash.exe` (`pub_base/shell_resolver.py`, standard paths then inferred from `git.exe`, cached per process): the tool promises a POSIX shell, and `cmd.exe` would quietly change what `&` / `&&` / `$()` / `2>&1` mean. No Git Bash → the platform shell.
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
- An opt-in `sessions_spawn(isolation=True)` gives a child its own GIT WORKTREE of the project (`agent/tools/subagent/isolation/`), cut from a dirty baseline (`git stash create`, so uncommitted edits are visible; untracked files are copied in, ignored paths materialized as links/copies per `SUBAGENT_ISOLATION`, and a non-repository project is auto-initialized with a deny-listed baseline commit) whose changed files merge back at announce time under a per-root `flock` with the same revision CAS — conflicts are reported by path and left untouched, symlinks/ignored caches never merge, and every merged code file is symbol-diffed before it is written so the completion reply carries an **Interface changes** section (renamed/added/removed functions, methods, classes — `agent/tools/code_intel/symbol_diff.py`, gated by `SUBAGENT_ISOLATION["interface_diff_enabled"]`, fail-open and capped); the docs page is `docs/file-safety/README.md`. A workspace no live run owns is cleaned by the stale-workspace sweep (`agent/tools/subagent/isolation/sweep.py`, every sweeper cycle and once at boot; conflict keeps expire after `SUBAGENT_ISOLATION` `stale_workspace_ttl_days`).
- a revert never touches the conversation: it appends one `<revert>user had revert editing …</revert>` AIMessage per revert (an `aupdate_state` write the persistence layer mirrors into the store), and the agent reads what was undone on its next turn.
- the todo-continuation middleware is named `PlanContinuationEnforcer` (it supervises the plan = todos + TaskFlow flows), and TWO strings are deliberately frozen: `message.metadata["origin"]` stays `"todo_continuation"` (historical rows keep resolving) and `agent_switch._RENAMED_MIDDLEWARES` maps `PlanContinuationEnforcer → {TodoContinuationEnforcer}` so a preset payload written before the rename still switches it off. Its turn-end hook also counts turns without a `todowrite` (`stagnation_tracker.record_turn` / `record_todowrite`, threshold `TODOLIST_INFRA["todo_write_nudge_turns"]` = 10) and APPENDS a `[REMINDER: TODO TRACKING]` nudge to the continuation directive once it is crossed — the one shape the snapshot-based stagnation check cannot see (an agent advancing TaskFlow clears the streak while the todo list rots). Recovery mode carries its own todowrite guidance, so the nudge stays out of that branch.
- a middleware row in 预设角色-中间件 must never let its label squeeze the switch: PrimeVue's `ToggleSwitch` cannot take a fall-through class from a fragment root, so each row wraps it in a `span.shrink-0` (the label column keeps `min-w-0 truncate`) and the row's help affordance is a real `?` button that toggles the explanation inline (hover tooltips are unreachable in an embedded webview).
- the `<system-reminder>` layer's contract lives in `pub/func/message/system_reminder.py` (`provenance="reminder_catalog"` + `internal=True` + the source id as `origin`), mirroring the workspace-notice leaf so the scanners that walk human messages can skip both: `split_into_turns` (a reminder is not a turn boundary), HITL's headless-turn check and the summarizer / memory-flush serializers (`[System notice]` / `system`, never `[User]`). Wrapping escapes nested tags — todo text is user-influenced.
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
- tool duration is measured ONCE, at tool-return time, by `message_persistence`'s `wrap_tool_call`/`awrap_tool_call`
  (`time.monotonic()` difference, rounded, clamped at 0 — never a wall-clock subtraction: an NTP step back would make it
  negative, which is the defect ZCode ships unguarded), stamped into `ToolMessage.additional_kwargs["tool_duration_ms"]`
  and persisted to `messages.tool_duration_ms` (old DBs heal by ADD COLUMN; old rows read NULL). The stream's
  `tool_result.duration_ms` is the same measurement for the live card (`stream_dispatch`'s `_tool_started_at`, per
  session + tool id, consumed once). Approval waits and queue time are OUTSIDE the window by construction — the clock
  starts when the tool actually executes. The client shows a live ticker while running and the measured value when
  settled (`formatToolDuration`: <1s → `850ms`, <10s → `1.2s`, else whole seconds), and nothing when unknown.
- the code-intelligence index follows the SESSION's project directory
  (`agent/tools/code_intel/roots.py::index_db_for_root` → `<root>/.codeintel/index.db`),
  so alternating between projects no longer rebuilds one shared index (the stored
  paths are project-relative); `code_intel_index_db_path` pins one and
  `SHERRY_CODE_INTEL_DB` overrides the path, with the shared `CODE_INTEL_DIR`
  as the no-root fallback. The index root itself is resolved PER CALL
  (env override > session project dir > process default), like ast-grep and LSP
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
