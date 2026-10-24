# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md) · [**Wiki**](https://github.com/lasertsole/sherry_agent/wiki)

> **A deep role-playing AI Agent built on LangChain/LangGraph and multimodal technology.**

## ✨ Introduction

EMA AI Agent is a highly anthropomorphic AI agent system with long-term memory and complex reasoning capabilities. It's more than just a chatbot — it's a virtual companion with an independent **Persona**, a dynamic **Skill System**, and proactive behavior through scheduled tasks and background subagents.

The Agent's character, **Sherry** (Tachibana Sherry), is a self-proclaimed girl detective: ever-cheerful and energetic on the outside, calm and razor-sharp at the core. The entire system is designed to support immersive, persistent role-playing with memory that accumulates across sessions.

---

## 🚀 Key Features

### 1. 🧠 Layered Memory System (Context Engine)
- **Short-term Session Memory** ([MesMemory](context_engine/README.md)): conversation history persisted to SQLite (WAL mode) with automatic FTS5 indexing — including a trigram tokenizer table for Chinese full-text search; persistence runs at two timings (`MessagePersistenceMiddleware`): tool results are flushed the moment they return, and every model-call boundary incrementally flushes the remaining new human/ai/tool messages — write-once via the `persisted_message_ids` watermark — so the raw store does not depend on a compression ever firing
- **History Retrieval**: last-N-turns, paginated history, or turn-range queries formatted as prompt context
- **Session Checkpointing**: thread-safe async SQLite checkpointer (`langgraph-checkpoint-sqlite`) persists agent state across restarts; stale checkpoints are cleaned automatically
- **Conversation Summarization**: an auxiliary LLM compresses long histories mid-conversation via the Summarization middleware
- **Context Governance**: oversized tool results and human messages are evicted off the prompt with a recoverable on-disk copy, `read_file` results are sliced, a no-LLM tail clip runs before any compression, and chained summaries are filtered out of the conversation payload
- **Private Knowledge Graph RAG**: the `multimodal_rag` skill indexes documents/folders into an entity–relationship graph (vendored LightRAG + RAG-Anything on `snkv` vector storage) and answers via multi-hop graph retrieval
- **Experience Extraction**: three lifecycle paths turn conversation history into durable experience: the compression-time memory review (on every compression), plan extraction when the todo list is all-complete at a compression, and the post-compression todo fork. They write to MEMORY.md / USER.md, the plan knowledge base (`agent/tools/todolist/knowledge/`), `skills/auto/`, and `todos.db`
- ▶️ _See the [Context Engine README](context_engine/README.md) for architecture, data models, and API details_
- ▶️ _See the [Experience README](wiki/experience/README.md) for the extraction paths and the skill Curator_

### 2. 🛠️ Dynamic Skill System
- **SKILL.md Standard**: skills are Markdown files with YAML frontmatter (`name`, `description`, optional `scope: all | main_only | subagent_only`) — the loader auto-discovers every `SKILL.md` under `skills/`
- **Built-in Skills** ([skills/builtin/](skills/builtin/)): `cron`, `heartbeat`, `clawhub` (GitHub skill installer), `skill_creator` (generates new skills), `image_to_text`, `speech_to_text`, `video_text_to_text`, `text_to_image`, `multimodal_rag`, `taskflow`, `todolist`, `ulw-execute`, `code_wiki`, `llm_wiki`
- **Skill Management Tools**: the agent can list, view, and manage skills at runtime; third-party uploads (`skills/plugins/`) stay inactive until explicitly enabled
- **SkillSpector Security Scanning** ([server/service/skill_scanner.py](server/service/skill_scanner.py)): third-party skills are scanned by NVIDIA SkillSpector (static YARA/rule analysis + optional LLM semantic analysis via the auxiliary LLM) before activation; flagged skills are blocked from installation
- **Skill Curator**: the context-engine curator thread maintains auto-learned skills under `skills/auto/` — see the [Experience README](wiki/experience/README.md)
- **Tool Timeouts**: real per-tool limits come from the `TOOLS_TIMEOUTS` registry (`WEB_SEARCH_TIMEOUT=15`, `TERMINAL_TIMEOUT=30`, `PYTHON_REPL_TIMEOUT=30`); `TOOL_CALL_TIMEOUT_MINUTES` is a stored setting that **no execution path consumes**
- ▶️ _See the [Middlewares README](agent/middlewares/README.md) for the middleware pipeline (guardrails, iteration budget, HITL, normalization, summarization, multimodal processing)_

### 3. 🤖 Multi-level Subagent System
- **7 Runtime Tools**: `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list`
- **Hierarchical Roles**: depth-limited nesting (default max depth 2, hard cap 2) with MAIN → ORCHESTRATOR → LEAF roles and least-privilege tool scoping
- **Isolated Context**: every subagent runs with a fresh, independent context — the parent transcript is never inherited; file attachments are supported
- **Reliable Delivery**: results return through an EventBus announce pipeline with idempotency checks and exponential-backoff retries
- **Durable Registry**: run records persisted to SQLite; a sweeper recovers orphaned runs and a followup checker enforces run timeouts when configured (default: none)
- **Swarm Mode**: batch sub-task execution with FIFO scheduling and configurable concurrency
- **Verified completion**: every spawn runs an auxiliary-LLM completion judge between child turns; a `continue` verdict injects the judge's follow-up prompt as the next turn, bounded by the configured `COMPLETION_JUDGE["goal_max_turns"]` budget (default 5)
- **Functional Roles (opt-in)**: `sessions_spawn(functional_role=...)` specializes a worker (general / researcher / executor / reviewer / librarian); the role drives the LLM tier, the tool allow-list, and the child's system-prompt sections
- **Isolated Worktrees (opt-in)**: `sessions_spawn(isolation=True)` gives the child its own git worktree of the project (`agent/tools/subagent/isolation/`), cut from the **dirty** baseline (`git stash create`, so uncommitted edits are visible to the child) and merged back at announce time under a per-root lock — see the [File Safety README](wiki/file-safety/README.md)
- ▶️ _See the [Subagent System README](agent/tools/subagent/README.md) for the full architecture_

### 4. 🌐 Multi-Channel Access
- **Robyn Backend** ([server/](server/)): async HTTP API + WebSocket (`/sessions/ws`) on `127.0.0.1:8080`, serving uploaded media under `/static`, `/images`, `/audio`, `/video`
- **Desktop Client** ([client/](client/)): Tauri 2 + Nuxt 4 (Vue 3 + TypeScript) SPA with system tray, global shortcut, offline history cache (Dexie/IndexedDB), dark/light mode, and i18n
- **QQ Bot**: QQ channel adapter via the plugin system ([plugins/channels/qq/](plugins/channels/qq/))
- **Message Bus** ([bus/core.py](bus/core.py)): internal async queues decouple channels from the agent core

### 5. 👁️ Multimodal Interaction
- **Image Understanding (ITTT)**: Image-to-Text vision models for analyzing user-uploaded images
- **Video Understanding (VTTT)**: Video-Text-to-Text models for video content analysis
- **Speech Recognition (STT)**: FunASR-based local speech-to-text
- **Text-to-Image (TTI)**: image generation from text descriptions via the `text_to_image` skill
- **Document Parsing**: MinerU-based multimodal document ingestion for the knowledge-graph RAG pipeline

### 6. ⏰ Scheduled & Proactive Behavior
- **Cron Service** ([skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)): one-shot (`at`), interval (`every`), or cron-expression (`cron`, via croniter + timezone) agent tasks, persisted to a JSON job store with per-job run history and delivery to channels
- **Heartbeat Service** ([skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)): periodic wake-up (default 30 min) that checks `HEARTBEAT.md` for pending tasks, lets an LLM decide skip/run, and passes results through a notification gate

### 7. ⚙️ Runtime, Concurrency & Session Control
- **Project Directory Binding** ([runtime/session/project_dir.py](runtime/session/project_dir.py)): every session has a project directory that its tools resolve relative paths against — session binding → `SHERRY_PROJECT_DIR` → `project_dir` in `sherry.jsonc` → `ROOT_DIR`. `PUT /sessions/project` binds or clears it (`null`), and a choice made mid-turn is parked and promoted at the turn boundary. The root is resolved **per call**, never cached, so a process-level tool singleton never freezes a session onto the first caller's directory; a subagent inherits the parent's binding at spawn and stays frozen to the root it started with. The prompt gains a `## Current Working Directory` block naming the effective root. The root's `AGENTS.md` (fallback `CLAUDE.md`) is injected as a `project_instructions` reminder whenever its content changes, and an explicitly bound project gets one `.sherry/` shell — code index and shadow git — on the first message, never at binding time
- **Read-only File Browsing**: `GET /project/tree` and `GET /project/file` serve a lazy tree and a viewer hard-refused outside the session's root (they deliberately bypass the agent's external-file approval flow), with `FILE_BROWSER` bounding read size, encoding, depth and entries per level. The in-app directory picker (`GET /system/dirs`, a deliberate exception that lists one absolute path's **subdirectories only**) backs the browser build, while the desktop build opens the OS folder dialog. A tree row's context menu offers open / open with / open in the built-in browser (HTML) / reveal in the file manager / copy the absolute or relative path / add to chat, and a row (or a file-viewer tab) can be dragged into the composer — either way the message carries an `@./path` mention chip, never the file's content: the agent reads the file itself
- **Selection Quote & Side Session**: selecting text inside ONE chat message and right-clicking opens a small menu — 添加到当前会话 inserts an atomic quote chip into the composer (sent as a markdown blockquote), while 在辅助会话中提问 creates an ordinary child session that is a **complete fork** of the parent: its first turn is seeded with the parent's checkpoint transcript (model-only, so the side chat's visible history stays empty) and the parent preset, agent config, per-session model settings and project directory are copied, with the fork cut at the parent's last completed answer so an unfinished tool call is never replayed. It opens in a session-scoped right-sidebar tab and pre-fills the quote; closing that tab only closes the panel — the child and all of its state survive, it stays in the session list so it can be reopened, and it is purged only when the parent session is deleted. Selections spanning rows, starting in controls/approval cards, or over 8 000 chars leave the native context menu alone
- **Agent-Controllable Browser (CDP)** ([config/features/infra_side/browser_agent.py](config/features/infra_side/browser_agent.py)): opt-in and off by default (`SHERRY_BROWSER_AGENT_ENABLED=1` plus a restart) — while off, no browser tool is registered, every `/browser/*` route answers 404, and no Chromium is spawned. When on, the main agent drives a real loopback Chromium through the merged main-only `browser` tool (navigate / snapshot / click / type / press / scroll / screenshot) plus `browser_evaluate`, the last only with `SHERRY_BROWSER_ALLOW_EVALUATE=1`, and the toolbox's browser panel watches the same page as a second CDP client (`/browser/ws` screencast + input); the debug port never leaves the backend process
- **User Lifecycle Hooks** ([agent/middlewares/user_hooks/](agent/middlewares/user_hooks/)): the `sherry.jsonc` `hooks` section runs operator-supplied commands against the turn — `PostToolUse` / `PostToolUseFailure` (each carries `toolName`, `toolCallId`, a bounded result/error preview and `durationMs`) and `Stop` (`responsePreview`, `toolCallCount`). Read-only by design: a hook audits, notifies and meters, and cannot block a tool, rewrite its arguments or inject context. Off by default (a tracked config must not make the agent execute commands on its own); commands are `shlex`-split with no shell, run with a scrubbed environment, bounded by a per-hook timeout that kills the process, and their stdout is capped and parsed as optional JSON (non-JSON output = success). Registered after the tool guardrails, gateable per session
- **Concurrency Lanes** ([runtime/lane/core.py](runtime/lane/core.py)): four process-level lanes, each an `asyncio.Semaphore` with active/queued counters, queue over-limit work FIFO instead of rejecting it — `MAIN` (main-agent turns, CPU-scaled 12–16), `SUBAGENT` (spawns and steers, 8), `NUDGE` (nudge/persistence calls, 4), `NESTED` (`sessions_send` reply turns, serial, 1). Startup validation enforces `main >= subagent + nudge`; `GET /lane-status` reports each lane's `{name, max_concurrent, active, queued}`
- **Queued User Input**: a message sent while a turn is running is persisted as a `QUEUED` row (dedup by `client_msg_id`, capped by `INPUT_QUEUE["max_active_per_session"]`) and answered with its FIFO position. The turn runner drains **one row per turn**, so N queued messages produce N turns and N replies — never one merged answer — and a HITL-pending session drains nothing until the resume completes. The client can `cancel_queued`, `edit_queued`, or `send_now` a queued row
- **Parallel Step Dispatch & Flow Lineage** ([agent/tools/taskflow/tools/_shared.py](agent/tools/taskflow/tools/_shared.py)): a dispatch wave spawns concurrently, bounded by `TASKFLOW_INFRA["max_parallel_dispatch"]` (2; 1 restores the sequential behaviour) — a partial wave failure persists every spawned child and names the failed step ids, and two children completing at once both land (the resume write re-applies its decided verdict through `update_flow_with_conflict_retry`, so no result is lost to a conflict and a step is never spawned twice). `taskflow_flow(action="create", resumed_from=…)` records flow lineage (`resumed_from` / `superseded_by`, additive columns); a superseded flow drops out of the active reads while staying on the board
- **TaskFlow Progress** ([agent/tools/taskflow/progress_push.py](agent/tools/taskflow/progress_push.py)): each flow's flat step list (id, task, status) and per-status counts feed the floating progress panel; the scheduler still unlocks each step on its own `depends_on`. Every flow mutation pushes a `taskflow_updated` frame, best-effort, and a reconnecting client re-asks with `taskflow_refresh` for the identical payload
- **Stream Recovery** ([server/service/stream_dispatch.py](server/service/stream_dispatch.py)): a viewer that drops mid-turn reconnects with ONE history request (`include_active_turn=1`) that returns both the turns completed while it was away and the running turn's snapshot (`turn_id`, `seq`, text). The socket holds inbound frames during the request; the client merges history, applies the snapshot, and drops only same-turn frames at or below the watermark — nothing is replayed or duplicated. Committed tool calls stay in the trajectory ledger (`GET /sessions/:session_id/trajectory`).
- **Tool Duration**: every tool call is timed once, at tool-return time (`time.monotonic()`, clamped at 0) by the message-persistence wrapper, stamped into `ToolMessage.additional_kwargs["tool_duration_ms"]` and persisted to `messages.tool_duration_ms`; the live card shows a running ticker and the measured value once settled. Approval waits and queue time are outside the window by construction
- **Per-Session Model & Thinking**: `PUT /sessions/thinking` and `PUT /sessions/model` override the reasoning toggle and the main model for one session (the client prefetches a single-use `GET /auth/ws-ticket` when login is enabled), and the thinking-control middleware swaps the request model for the `build_main_llm(thinking=...)` variant built from it — falling back to the env default when the session carries no explicit choice

---

## 🏗️ Tech Stack

Built on **Python 3.13** (dependency management via [uv](https://docs.astral.sh/uv/)), with the following core technologies:

| Module | Technology |
| :----- | :--------- |
| **Agent Framework** | LangChain 1.3+ (`create_agent` + middlewares), LangGraph compiled graphs |
| **Checkpointing** | langgraph-checkpoint-sqlite (thread-safe async SQLite saver) |
| **Web Server** | Robyn (HTTP + WebSocket + static hosting) |
| **Database** | SQLite via aiosqlite (FTS5 full-text search, WAL mode) |
| **Graph RAG** | Vendored LightRAG + RAG-Anything (multimodal_rag skill), `snkv[vector]` storage |
| **Local Inference** | llama-cpp-python (GGUF: bge-m3 embedding, bge-reranker-v2-m3 reranker, auxiliary/ITTT/VTTT models), FunASR (STT) |
| **Document Parsing** | mineru-vl-utils |
| **Web Search** | langchain-tavily (Tavily API) + a self-hosted SearXNG instance as the alternative backend, with automatic Tavily→SearXNG fallback |
| **Web Fetch** | `web_fetch` reads ONE http(s) URL as markdown: SSRF-guarded, DNS-pinned and per-hop-redirect-checked for public pages, with an opt-in switch (`SHERRY_WEB_FETCH_ALLOW_LOCAL=1`) for local/dev-server addresses |
| **Code Search** | ripgrep, installed with the dependencies (`ripgrep-bin`), with the pure-Python scan as the always-available fallback |
| **LLM Providers** | langchain-openai, langchain-deepseek, langchain-classic + a 20+ provider registry (OpenAI, Anthropic, DeepSeek, Zhipu GLM, DashScope Qwen, Gemini, Moonshot Kimi, MiniMax, Groq, OpenRouter, SiliconFlow, Volcengine, Azure OpenAI, Ollama, vLLM, and more) |
| **Structured Output** | instructor, json_repair |
| **Evaluation** | RAGAS (graph-RAG quality metrics) + a homegrown sandboxed suite runner (`evals/`) |
| **MCP** | langchain-mcp-adapters (servers configured in `plugins/mcp_server/`) |
| **Task Scheduling** | croniter, asyncio |
| **Async Messaging** | asyncio queues (MessageBus, EventBus) |
| **Media Processing** | OpenCV (headless), Pillow, websockets / websocket-client |
| **Desktop Client** | Tauri 2 + Nuxt 4 (Vue 3, TypeScript, pnpm) |
| **Logging** | loguru (optional LangSmith tracing) |

---

## 📂 Project Structure

```text
sherry_agent/
├── agent/                  # Agent core logic
│   ├── core.py             # Main agent loop (LangChain create_agent → LangGraph graph)
│   ├── security/           # Redaction, PII, threat patterns, untrusted-output wrapping
│   ├── wrapper/            # Graph-level wrappers (repetition guard, context limit)
│   ├── checkpointer/       # Thread-safe async SQLite checkpointers
│   ├── middlewares/        # Middleware pipeline (summarization, guardrails, HITL, ...)
│   ├── prompt_data_provider.py # Runtime PromptDataProvider implementation (owner side)
│   ├── skill_write_provider.py  # Runtime SkillWriteProvider implementation (owner side)
│   └── tools/              # Agent-accessible tools
│       ├── taskflow/       # Task orchestration engine (DAG, budget, deadline, step judge)
│       ├── subagent/       # Multi-level subagent system (spawn/registry/swarm/...)
│       │   └── isolation/  # Opt-in git-worktree isolation backend for a spawned child
│       ├── todolist/       # Session-scoped todo planning layer
│       │   └── knowledge/  # Plan knowledge base + `knowledge` tool
│       ├── file_tools/     # File I/O tools (read, write, patch, search)
│       ├── skill_tools/    # Skill management tools (list, view, manage)
│       ├── code_intel/     # Code retrieval (ripgrep, tree-sitter, ast-grep, LSP, semantic)
│       ├── ptc/            # Programmatic tool calling (`execute_code` child-process bridge)
│       ├── pub_base/       # Shared tool utilities & infrastructure (BaseSQLiteRepository, path utils)
│       ├── mcp_plugin.py   # MCP tool integration
│       ├── web_search.py   # Web search tool (Tavily)
│       ├── web_fetch.py    # URL-reading tool (markdown; public + opt-in local)
│       ├── python_repl.py  # Python code execution
│       ├── terminal.py     # Terminal command execution
│       ├── memory.py       # Memory inspection tool
│       ├── question.py     # HITL multi-choice question prompt
│       └── message_search.py # Conversation FTS5 search tool
│
├── bus/                    # Message bus (async queues)
│   └── core.py             # MessageBus — inbound/outbound queues
│
├── channels/               # Channel interface definitions
│   ├── base.py             # Abstract channel base
│   ├── deps.py             # Dependency-injection seams for channel wiring
│   ├── manager.py          # Channel lifecycle manager
│   └── registry.py         # Channel registration
│
├── client/                 # Desktop client (Tauri 2 + Nuxt 4, pnpm)
│   ├── app/                # Nuxt 4 SPA source (Vue 3)
│   ├── src-tauri/          # Tauri 2 native shell (Rust)
│   └── README.md           # Client documentation
│
├── config/                 # Centralized configuration (paths, feature TypedDicts, schema, settings)
│   ├── __init__.py         # API host/port (127.0.0.1:8080)
│   ├── path.py             # File path configuration
│   ├── schema.py           # Configuration schema models
│   ├── sherry_settings.py  # sherry.jsonc loader
│   └── features/           # Per-object feature TypedDicts + default instances
│
├── context_engine/         # Memory engine (MesMemory)
│   ├── core.py             # History retrieval & FTS5 search APIs
│   ├── store/              # Session message store (SQLite + FTS5, WAL)
│   ├── events/             # Append-only event log + projector
│   ├── embeddings/         # Vector semantic search (indexer / search)
│   └── curator/            # Auto-skill curation
│
├── wiki/                   # Subsystem design docs (per-language READMEs)
│   ├── experience/         # Experience extraction paths + skill curation
│   ├── session_memory/     # Session-memory capabilities
│   ├── summarization/      # Compression triggers & cooldown
│   ├── loop-prevention/    # Runaway-loop prevention harness
│   ├── sandbox/            # Eval sandbox & tool isolation
│   ├── token-guard/        # 128K context-window floor
│   ├── context-governance/ # Persistence, eviction, tail clip, summary filtering
│   ├── long-running-tasks/ # TaskFlow orchestration
│   ├── subagent/           # Subagent design invariants & completion gates
│   ├── code-intel/         # Code retrieval layers
│   ├── graph-rag/          # Multi-hop path retrieval + recall tuning
│   ├── llm-wiki/          # Markdown KB + derived graph queries
│   ├── ptc/                # Programmatic tool calling
│   ├── file-safety/        # Atomic writes, CAS, locks, worktree isolation
│   ├── threat-model/       # Trust boundaries & prompt-injection scanner
│   └── auth/               # Opt-in login protection (account + session)
│
├── evals/                  # Evaluation framework (dispatcher + 11 suites)
│   ├── evals.py            # Suite runner: uv run python evals/evals.py [suite]
│   ├── sandbox.py          # Sandbox that redirects repo writes during a run
│   ├── graph_rag/          # RAGAS metrics for the graph-RAG pipeline
│   ├── subagent/           # Subagent spawn-pipeline benchmark
│   ├── long_running_task/  # TaskFlow DAG eval
│   ├── session_memory/     # Session-memory stack checks
│   ├── nudge_extraction/   # AI-judged plan extraction
│   └── results/            # Per-run reports (gitignored)
│
├── logs/                   # Logging system
│   ├── logger.py           # Log configuration (loguru)
│   ├── curator/            # Curator run logs
│   └── output/             # Log output directory
│
├── models/                 # Model wrappers & weights
│   ├── LLMs/               # LLM configs (main_llm.py, reasoner_llm.py, auxiliary_llm/, reasoning_* providers)
│   ├── ITTT_model/         # Image-to-Text model (cloud API or local GGUF)
│   ├── VTTT_model/         # Video-Text-to-Text model (cloud API or local GGUF)
│   ├── STT_model/          # Speech-to-Text model (FunASR)
│   ├── embed_model/        # Embedding model (local bge-m3 GGUF or cloud API)
│   ├── reranker_model/     # Cross-encoder reranker (local GGUF or cloud API)
│   ├── extract_model/      # Entity extraction model (third-party weights)
│   └── providers/          # LLM provider specifications & registry
│       └── registry.py    # ProviderSpec entries for 20+ providers
│
├── plugins/                # Plugin system
│   ├── channels/           # Channel plugins (QQ bot adapter)
│   └── mcp_server/         # MCP server configuration
│
├── pub/                    # Shared utilities & data models
│   ├── func/               # Common utility functions
│   │   ├── format/         # Text formatting utilities
│   │   ├── media/          # Media processing utilities
│   │   ├── message/        # Message processing utilities
│   │   └── validator/      # Input validation utilities
│   └── types/              # Shared data models
│       ├── message.py      # MultiModalMessage, Chat, etc.
│       ├── bus.py          # Message bus data models
│       └── client.py       # Client data models
│
├── runtime/                # Runtime state, lanes & cross-boundary seams
│   ├── hooks.py            # Process-level callback registry (server-owned hooks resolved by agent/skills)
│   ├── data_provider.py    # Prompt/skill-write provider registries (agent-owned, read by workspace/context_engine)
│   ├── lane/               # Process-level concurrency lanes (MAIN / SUBAGENT / NUDGE / NESTED)
│   ├── session/            # Session-scoped registers
│   │   ├── core.py         # Singleton SessionRegister base + per-session cleanup
│   │   ├── relation_register.py # Session/socket relation registry
│   │   ├── state_register.py   # State registry
│   │   ├── state_keys.py       # Typed StateKey registry + TypedState facade
│   │   ├── project_dir.py  # Per-session project-directory binding resolution
│   │   ├── count_call_register.py # Usage/statistics counters
│   │   ├── timer_call_register.py # Timer registry
│   │   └── _callback_executor.py # Async callback executor
│   └── process/            # Process-scoped services
│       ├── crash_loop_breaker.py # Boot crash-loop detection
│       └── periodic_backoff.py   # Periodic backoff state
│
├── server/                 # Robyn backend service
│   ├── __main__.py         # Server entry point (python -m server)
│   ├── DAO/                # Data access objects
│   ├── queue/              # Persisted user-input queue store
│   ├── utils/              # Shared backend helpers (JWT, password hashing, atomic I/O, WS)
│   ├── service/            # Business logic services (incl. skill_scanner.py)
│   └── trigger/            # Route & handler registration
│       ├── http/           # HTTP endpoint triggers
│       ├── ws/             # WebSocket triggers
│       ├── channels/       # Incoming channel triggers
│       └── subagent/       # Subagent result triggers
│
├── skills/                 # Skill library (SKILL.md definition files)
│   ├── loader.py           # Skill autodiscovery & registration
│   ├── skills_snapshot.py  # Builds the skill prompt snapshot
│   ├── auto/               # Auto-learned skills (maintained by curator)
│   ├── plugins/            # Third-party uploaded skills (inactive by default)
│   └── builtin/            # Built-in skills
│       ├── core/           # cron, heartbeat, clawhub, skill_creator, image_to_text,
│       │                   # speech_to_text, video_text_to_text, multimodal_rag,
│       │                   # taskflow, todolist, ulw-execute
│       ├── text_to_image/  # Text-to-image skill
│       ├── code_wiki/      # Codebase wiki generation skill
│       └── llm_wiki/       # Markdown knowledge-base skill
│
├── src/                    # Runtime data directories
│   ├── checkpoints/        # Session checkpoints
│   ├── data/               # Data storage
│   ├── store/              # Data stores
│   ├── rag/                # RAG index output
│   └── images/ audio/ video/ # Uploaded media (served statically)
│
├── temp/                   # Temporary files
│
├── tests/                  # Mirror-structured pytest suite (tests/<source>/...) + run_tests_split.py (marker-based runner)
│
├── workspace/              # Character profile & behavior definition
│   ├── SOUL.md             # Personality contrasts, speech style
│   ├── IDENTITY.md         # Core identity card
│   ├── AGENTS.md           # Tool usage priorities, safety boundaries
│   ├── USER.md             # User-specific interaction preferences
│   ├── ROLE.md             # Role statement: who the AI plays, who the user plays
│   ├── HEARTBEAT.md        # Pending tasks for heartbeat service
│   ├── prompt_builder.py   # Profile-to-prompt builder
│   ├── file_sync.py        # Lazy workspace template sync (per language)
│   ├── template/           # Persona templates (en / zh / ja / ko)
│   ├── sessions/           # Per-session runtime workspace data
│   └── memory/             # Long-term memory storage
│
├── .env                    # App config, tracked (sandbox / gateway / login — never a key)
├── .models.example         # Model-config template (seeds .models on first boot)
├── .network.example        # Network-config template (seeds .network on first boot)
├── pyproject.toml          # Python dependencies (uv managed)
├── uv.lock                 # Lockfile for uv
├── start.sh                # Backend startup script
└── cron_jobs.json          # Cron job schedule data
```

---

## 📚 Submodule Documentation

Each major subsystem has its own detailed README:

| Submodule | Description | Documentation |
|-----------|-------------|---------------|
| **Context Engine** | Short-term session message memory (MesMemory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) · [JA](context_engine/README.ja.md) · [KO](context_engine/README.ko.md) |
| **Experience** | Three extraction lifecycle paths plus the background Curator that maintains `skills/auto/` | [EN](wiki/experience/README.md) · [ZH](wiki/experience/README.zh.md) · [JA](wiki/experience/README.ja.md) · [KO](wiki/experience/README.ko.md) |
| **Session Memory** | SESSION-plan capabilities: memory review, compression cooldown, compaction lock, event log, semantic search | [EN](wiki/session_memory/README.md) · [ZH](wiki/session_memory/README.zh.md) · [JA](wiki/session_memory/README.ja.md) · [KO](wiki/session_memory/README.ko.md) |
| **Subagent System** | Multi-level subagent spawn, parallel execution & result delivery | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) · [JA](agent/tools/subagent/README.ja.md) · [KO](agent/tools/subagent/README.ko.md) |
| **Subagent Design** | Design invariants: two-axis role model, spawn-privilege guards, four-layer completion gates | [EN](wiki/subagent/README.md) · [ZH](wiki/subagent/README.zh.md) · [JA](wiki/subagent/README.ja.md) · [KO](wiki/subagent/README.ko.md) |
| **Code Intel** | Code retrieval: ripgrep keyword search for every agent, plus four role-gated layers — tree-sitter symbol index, ast-grep structural search, LSP precision, semantic search — with imports/inheritance/mixin edges and a bidirectional `path` tool | [EN](wiki/code-intel/README.md) · [ZH](wiki/code-intel/README.zh.md) · [JA](wiki/code-intel/README.ja.md) · [KO](wiki/code-intel/README.ko.md) |
| **LLM Wiki Graph** | Markdown-first knowledge base with an explicit derived graph: wikilink/mention edges with EXTRACTED/INFERRED confidence, multimodal ingest, and path/god-node/community queries | [EN](wiki/llm-wiki/README.md) · [ZH](wiki/llm-wiki/README.zh.md) · [JA](wiki/llm-wiki/README.ja.md) · [KO](wiki/llm-wiki/README.ko.md) |
| **GraphRAG Path Query** | Explicit multi-hop path retrieval: BFS for 2-3 hops, an embedding-guided beam fallback, and tuned recall thresholds over the dense-only LightRAG engine | [EN](wiki/graph-rag/README.md) · [ZH](wiki/graph-rag/README.zh.md) · [JA](wiki/graph-rag/README.ja.md) · [KO](wiki/graph-rag/README.ko.md) |
| **Programmatic Tool Calling** | EXECUTOR-only `execute_code`: one Python script in a separate child process, real tools over a loopback TCP RPC bridge, restricted builtins, per-script budgets | [EN](wiki/ptc/README.md) · [ZH](wiki/ptc/README.zh.md) · [JA](wiki/ptc/README.ja.md) · [KO](wiki/ptc/README.ko.md) |
| **Middlewares** | Agent lifecycle middleware pipeline | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) · [JA](agent/middlewares/README.ja.md) · [KO](agent/middlewares/README.ko.md) |
| **Channels** | Channel interface & adapter system | [EN](channels/README.md) · [ZH](channels/README.zh.md) · [JA](channels/README.ja.md) · [KO](channels/README.ko.md) |
| **Desktop Client** | Tauri 2 + Nuxt 4 desktop/mobile SPA client | [EN](client/README.md) · [ZH](client/README.zh.md) · [JA](client/README.ja.md) · [KO](client/README.ko.md) |
| **Cron Service** | Scheduled/periodic agent task execution | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) · [JA](skills/builtin/core/cron/scripts/README.ja.md) · [KO](skills/builtin/core/cron/scripts/README.ko.md) |
| **Heartbeat Service** | Periodic wake-up task check | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) · [JA](skills/builtin/core/heartbeat/README.ja.md) · [KO](skills/builtin/core/heartbeat/README.ko.md) |
| **Summarization** | Context compaction middleware: five trigger points, a 4-route overflow router, anti-thrash guards | [EN](wiki/summarization/README.md) · [ZH](wiki/summarization/README.zh.md) · [JA](wiki/summarization/README.ja.md) · [KO](wiki/summarization/README.ko.md) |
| **Loop Prevention** | Runaway-loop guards, exponential-backoff breakers, and process crash gating | [EN](wiki/loop-prevention/README.md) · [ZH](wiki/loop-prevention/README.zh.md) · [JA](wiki/loop-prevention/README.ja.md) · [KO](wiki/loop-prevention/README.ko.md) |
| **Sandbox** | Terminal & Python REPL confinement: env scrubbing, OS-native isolation, approval gate | [EN](wiki/sandbox/README.md) · [ZH](wiki/sandbox/README.zh.md) · [JA](wiki/sandbox/README.ja.md) · [KO](wiki/sandbox/README.ko.md) |
| **Threat Model** | Trust boundaries, data classification, per-threat protection status, and the prompt-injection scanner | [EN](wiki/threat-model/README.md) · [ZH](wiki/threat-model/README.zh.md) · [JA](wiki/threat-model/README.ja.md) · [KO](wiki/threat-model/README.ko.md) |
| **User Login** | Opt-in account protection: loopback clients stay exempt, remote access is gated by a scrypt password and HttpOnly JWT session cookies | [EN](wiki/auth/README.md) · [ZH](wiki/auth/README.zh.md) · [JA](wiki/auth/README.ja.md) · [KO](wiki/auth/README.ko.md) |
| **Long-Running Tasks** | TaskFlow DAG engine, step judge, budgets, deadlines, verified completion gates, tri-color orphan GC, and cross-turn memory continuity | [EN](wiki/long-running-tasks/README.md) · [ZH](wiki/long-running-tasks/README.zh.md) · [JA](wiki/long-running-tasks/README.ja.md) · [KO](wiki/long-running-tasks/README.ko.md) |
| **Token Guard** | Hard 128K context-window floor on both LLMs (boot, build, spawn, env write) | [EN](wiki/token-guard/README.md) · [ZH](wiki/token-guard/README.zh.md) · [JA](wiki/token-guard/README.ja.md) · [KO](wiki/token-guard/README.ko.md) |
| **Plan Mode** | Plan-Then-Execute: read-only design, an approval gate on the plan, a revision-bound resubmit loop and a mutation budget | [EN](wiki/plan-mode/README.md) · [ZH](wiki/plan-mode/README.zh.md) · [JA](wiki/plan-mode/README.ja.md) · [KO](wiki/plan-mode/README.ko.md) |
| **Context Governance** | Per-boundary persistence, tool-result & human-message eviction, `read_file` slice, overflow tail clip, summary filtering, conversation-turn identity on message rows | [EN](wiki/context-governance/README.md) · [ZH](wiki/context-governance/README.zh.md) · [JA](wiki/context-governance/README.ja.md) · [KO](wiki/context-governance/README.ko.md) |
| **Stream Recovery** | Mid-turn WebSocket recovery: snapshot + per-turn `seq` watermark, no replay, no duplication, tool calls stay in the trajectory ledger | [EN](wiki/stream-recovery/README.md) · [ZH](wiki/stream-recovery/README.zh.md) · [JA](wiki/stream-recovery/README.ja.md) · [KO](wiki/stream-recovery/README.ko.md) |
| **Turn History Folding** | Settled turns collapse tool/reasoning history behind one summary while the final assistant answer stays visible; running or interrupted turns stay linear | [EN](wiki/turn-fold/README.md) · [ZH](wiki/turn-fold/README.zh.md) · [JA](wiki/turn-fold/README.ja.md) · [KO](wiki/turn-fold/README.ko.md) |
| **File Safety** | Atomic writes, two-layer CAS, per-path and cross-process locks, the read-before-write license for `write_file` (dropped again the moment a compression discards the read it came from), and isolated subagent workspaces whose merges report interface-level changes | [EN](wiki/file-safety/README.md) · [ZH](wiki/file-safety/README.zh.md) · [JA](wiki/file-safety/README.ja.md) · [KO](wiki/file-safety/README.ko.md) |
| **Tool Parallel Safety** | Batch-aware ToolNode: same-file reads/writes ordered, different paths concurrent, process/session resource locks | [EN](wiki/tool-parallel-safety/README.md) · [ZH](wiki/tool-parallel-safety/README.zh.md) · [JA](wiki/tool-parallel-safety/README.ja.md) · [KO](wiki/tool-parallel-safety/README.ko.md) |

## ⚡ Quick Start

### 1. Prerequisites
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — the dependency manager. It creates and manages `.venv` automatically; there is no need to create a virtual environment manually.

```bash
git clone <your-repo-url>
cd sherry_agent
uv sync   # creates .venv and installs the exact dependencies from uv.lock
```

### 2. Configure the Model & Network Files
Three files carry the configuration, split by sensitivity:

| File | Tracked? | Contents |
| :--- | :--- | :---------- |
| `.env` | ✅ committed | Non-secret application configuration (sandbox policy, gateway, login). Never put a key here. |
| `.models` | gitignored | Every model family's provider / endpoint / key (seeded from `.models.example` on first boot) |
| `.network` | gitignored | Web search (Tavily / SearXNG), the browser agent's switches, LangSmith tracing (seeded from `.network.example` on first boot) |

Fill in at least the main chat model in `.models` and, for web search, a Tavily key or a SearXNG instance in `.network` — the 模型配置 / 模型联网 panels edit both for you:

| Variable | Required | Description |
| :------- | :------- | :---------- |
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | Primary chat model (must support JSON output and tool calling); `MAIN_LLM_MAX_TOKEN` must be >= 131072 (128K) |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | Universal reasoning switch, mapped per provider (DeepSeek / OpenAI / GLM / Anthropic) |
| `TAVILY_API_KEY` | ✅ for web search | Enables the web search tool |
| `SEARXNG_URL` / `SEARXNG_PROXY` / `SEARXNG_ENGINES` / `SEARXNG_CATEGORIES` / `SEARXNG_VERIFY_TLS` | — | Self-hosted SearXNG as the (or an alternative) web-search backend; without `TAVILY_API_KEY` it is the only one. Configure it in the 模型联网 panel; Tavily failures fall back to it automatically |
| `AUXILIARY_LLM_*` | — | Lightweight model for summarization / simple tasks (cloud API by default; set `AUXILIARY_LLM_MODEL_LOCAL=true` for a local GGUF model); `AUXILIARY_LLM_MAX_TOKEN` must be >= 131072 (128K) |
| `TASKFLOW_LLM_*` | — | TaskFlow decision model (plan / replan / step judge / goal gate). An empty provider + API base follows `AUXILIARY_LLM_*`; local mode reuses the auxiliary GGUF. `TASKFLOW_LLM_MAX_TOKEN` must be >= 131072 (128K) |
| `COMPRESSION_LLM_*` | — | Compression model (LLM summary). Memory review uses `MAIN_LLM` and plan extraction uses `AUXILIARY_LLM_*`; an empty provider + API base follows `AUXILIARY_LLM_*` |
| `REASONER_LLM_*` | — | Chain-of-thought reasoning model |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | Image / video / text-to-image / speech model configuration |
| `RERANKER_*` / `EMBEDDING_*` | — | Reranker & embedding for retrieval (see model notes below) |
| `MODEL_PRICING_TABLE` | — | USD per 1M-token price table for session-budget cost estimates (a user-editable JSON object stored as a JSON string; provider-returned cost and the local litellm cache are fallbacks) |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector security scanner switch (on by default); LLM semantic analysis is opt-in (off by default) and requires a provider supporting json_schema structured output |
| `SHERRY_BROWSER_AGENT_ENABLED` / `SHERRY_BROWSER_HEADLESS` | — | Opt-in browser agent (off by default); needs a restart to register its tools |
| `TOOL_CALL_TIMEOUT_MINUTES` (sherry.jsonc) / `LOG_LEVEL` | — | Stored setting only (default 5) — no tool-execution path consumes it; log level (INFO) |
| `WORKSPACE_TEMPLATE_LANG` | — | Persona template language: `en` / `zh` / `ja` / `ko` (lazy-copied on first use) |
| `LANGSMITH_TRACING_V2` / `LANGSMITH_API_KEY` (`.network`) | — | Optional LangSmith tracing |

### 3. Model Notes (HuggingFace Auto-Download)
Models configured for **local GGUF** mode are downloaded automatically from Hugging Face into `models/<model>/model_weight/` on first use — no manual download is required:

- **Embedding**: `EMBEDDING_MODEL_LOCAL=true` (the default) uses the local `bge-m3` Q8_0 GGUF, auto-downloaded on first run.
- **Reranker**: the `.models` template defaults to a **cloud API** (`RERANKER_MODEL_LOCAL=false`, OpenAI-compatible `bge-reranker-v2-m3`). Set it to `true` to run the local GGUF reranker instead, which is then auto-downloaded (~636 MB).
- **ITTT / VTTT / Auxiliary LLM**: default to cloud APIs in the template; set `*_MODEL_LOCAL=true` to switch to local GGUF models (also auto-downloaded).
- **Prefetch with progress**: the 模型配置 panel's built-in local entry (embedding, reranker, ITTT / VTTT, auxiliary) offers a download button and a live progress bar; it receives throttled `model_download_updated` WebSocket frames and takes one `GET /model-downloads/status` snapshot when the panel opens or reconnects, then resumes an interrupted transfer from its `.part` file.

> Network access to huggingface.co is required for first-run downloads (users in China may need a proxy or a mirror). Interrupted downloads are resumed on the next start; delete `models/<model>/model_weight/` to force a re-download.

### 4. Start the Backend
`start.sh` activates the uv-managed `.venv` and launches the Robyn backend (it does not start Ollama or any frontend):

```bash
chmod +x start.sh
./start.sh          # runs the .venv interpreter: python -m server --fast --disable-openapi
```

Manual start (equivalent):

```bash
uv run python -m server
```

The backend listens on **http://127.0.0.1:8080** with a WebSocket endpoint at `/sessions/ws`.

### 5. (Optional) Desktop Client
The Tauri 2 + Nuxt 4 client lives in [client/](client/) and requires Node.js 20+, pnpm, and Rust (CI pins Node 22):

```bash
cd client
pnpm install
pnpm dev          # browser mode, dev server at http://localhost:3000
pnpm tauri dev    # native desktop mode
```

The client connects to the Python backend at `http://127.0.0.1:8080` by default (configurable via `VITE_API_BACK_URL` in `client/.env`). See the [client README](client/README.md) for details.

**Packaging the desktop app** produces TWO artifacts: the installer (front end + Rust shell — no Python, no venv and no checkout on the target machine) and the backend archive the app downloads on its first launch, because GitHub caps a release asset at 2 GiB and the backend is well past that:

```bash
uv run --with pyinstaller python packaging/build_backend.py   # backend → client/src-tauri/sherry-backend/
cd client && pnpm tauri build                                 # → deb / rpm / AppImage / msi / dmg
```

`.github/workflows/desktop-build.yml` builds both on all three platforms. Both steps are per-OS (PyInstaller cannot cross-compile). The app writes everything it owns — sessions, memory, logs, learned skills, `.env` / `.models` / `.network` — into the OS app-data directory (`SHERRY_HOME`, set by the shell), never into the read-only install tree. First run has no model configured yet: the window opens, 模型配置 writes the keys, and the next launch picks them up.

---

## 🧪 Testing

Tests live under `tests/`, **mirroring the source tree** (`tests/agent/...`, `tests/server/...`, `tests/context_engine/...`), and run with **pytest** via uv (`uv run pytest` for a single test file or a small selection). Every test file carries a module-level `pytestmark` (`unit` / `integration` / `module` / `system` / `regression`) that selects which runner group executes it.

### Recommended: the process-isolated runner

For the full suite (and for CI), use the split runner — it executes the suite in **three sequential pytest processes** (never parallel), selecting tests by MARKER (not by directory), aggregates their exit codes, and prints a per-group summary plus a final verdict (exit code 0 only if all groups pass):

```bash
uv run python tests/run_tests_split.py                  # hermetic suite (default, llm_e2e excluded)
uv run python tests/run_tests_split.py --with-llm-e2e   # ONLY the real-LLM e2e tests (dedicated-job mode)
uv run python tests/run_tests_split.py -- -k spawn -q   # args after `--` are forwarded to pytest
```

| Group | Marker | Contents |
| :---- | :----- | :------- |
| **A** | `unit` | pure-logic, fully mocked tests |
| **B** | `integration or module or system` | hermetic integration / module / system tests |
| **C** | `regression` | cross-module regression tests |

**Why separate processes?** `tests/agent/tools/subagent/conftest.py` installs stub callables into process-global `sys.modules` at conftest *import* time. In a single-process full-suite run, pytest imports every conftest and test module during collection — before any test executes — so those stubs are live for the whole process and leak across suites: lazy (call-time) imports resolve the stub, while modules that bound the real object earlier keep stale bindings. The result is confusing, order-dependent failures in suites far away from the subagent tests (e.g. skill-scope assertions seeing a stub's fixed skill list, `TypeError` tracebacks naming conftest lambdas). Running the groups in separate processes makes this cross-suite pollution structurally impossible. (The stubs themselves are restore-safe since `c730a46`; the runner is the defense-in-depth operational layer.)

**Windows note:** child pytest processes get `PYTHONIOENCODING=utf-8` in their environment and the runner captures their output with `errors="replace"`, so GBK console codepages can neither corrupt the output nor crash the run.

### Real-LLM e2e tests (`llm_e2e` marker)

Eight tests across five files call **real LLM APIs**: seven in `tests/agent/tools/subagent/` (`test_real_e2e.py`, `test_spawn_direct_e2e.py`, `test_code_intel_researcher_e2e.py`, `test_ptc_executor_e2e.py`) plus the multimodal-RAG pipeline test in `tests/skills/builtin/core/multimodal_rag/test_rag_e2e.py`. They are:

- **deselected by default** (`-m "not llm_e2e"` — set both in `pyproject.toml` addopts and by the runner),
- bounded by `@pytest.mark.timeout` budgets (pytest-timeout): 300 s per simple test, 600 s for the concurrent test,
- run explicitly, in a **dedicated job**: `uv run python tests/run_tests_split.py --with-llm-e2e` (selects `-m llm_e2e`) or `uv run pytest -m llm_e2e`.

**Expected runtimes** (solo, real backend): simple task ≈ 30–60 s; complex worst case ≈ 10 min; concurrent tasks ≈ 2–9 min. A run that exceeds these budgets is a real hang, not normal slowness — the per-test timeout bounds it (300 s simple / 600 s concurrent).

**CI:** `.github/workflows/ci.yml` runs `uv run python tests/run_tests_split.py` on every push/PR to `main` or `dev`, executing the suite as **three sequential pytest processes** (never parallel): A = `unit`, B = `integration` + `module` + `system`, C = `regression`. The `--with-llm-e2e` suite stays a separate, slower job (it costs API tokens; never run it in parallel with other suites).

> **Note:** `tests/full/` is an auxiliary/experimental directory outside the standard groups above. Every file there that drives a live LLM is tagged `llm_e2e`, so the default addopts deselect it and the split runner never collects it (it also `--ignore`s `tests/full/`). Run one explicitly with `uv run --no-sync pytest -m llm_e2e tests/full/<file>`. Hermetic tests do not belong there — the real-graph HITL test now lives at `tests/agent/middlewares/humanInTheLoop/test_hitl_real_graph.py` and runs in the standard groups.

### Evals

`evals/` is a homegrown, sandboxed evaluation framework that sits beside pytest. Run every registered suite, or one by name:

```bash
uv run python evals/evals.py                # all registered suites
uv run python evals/evals.py graph_rag      # a single suite by name
```

| Suite | What it evaluates |
| :---- | :---------------- |
| `graph_rag` | The multimodal_rag pipeline, scored with RAGAS (faithfulness, answer relevancy, context recall, context precision) |
| `subagent` | The real `spawn_subagent_direct` pipeline on a bench of deterministic tasks (task success + latency) |
| `long_running_task` | The TaskFlow orchestration loop over a dependent DAG (step success, flow completion, wall time) |
| `session_memory` | The session-memory stack over 6 checks: cooldown, compaction lock, checkpoint restore, idempotent replay, context eligibility, semantic search ranking |
| `nudge_extraction` | The plan-extraction pass, judged by an auxiliary LLM for grounded, reusable, non-generic skills |
| `coding_capability` | The real main agent on a small self-designed coding task set, each task judged by its own repository's pytest run — a baseline, not an external benchmark |
| `swebench` | External SWE-bench_Lite instances through the same harness (pinned base commit + per-instance venv + the instance's own FAIL_TO_PASS / PASS_TO_PASS verdict; the agent sees the problem statement only) |
| `deepswe` | External DeepSWE tasks in the benchmark's own Docker image: the agent edits a bind-mounted checkout, `terminal` runs in the container, and the task's separate verifier image grades the agent's diff (gold patch checked first) |
| `terminal_bench` | External Terminal-Bench tasks: the task image is built, the agent works inside it, and its own `run-tests.sh` pytest verdict judges the run (oracle checked first; multi-service tasks are reported unsupported) |
| `agentbench` | External [AgentBench](https://github.com/THUDM/AgentBench) DBBench test set: official questions/tables from a pinned checkout, materialised in SQLite, graded by AgentBench's own comparison rules (INSERT/DELETE/UPDATE rows are reported unsupported; MySQL→SQLite dialect is documented) |
| `toolbench` | External [ToolBench](https://github.com/OpenBMB/ToolBench) test instructions: official `test_query_ids`, tool calls through the official RapidAPI proxy, official answer format, and the official ToolEval evaluator imported from the pinned repository (data mirror: [Adorg/ToolBench](https://huggingface.co/datasets/Adorg/ToolBench)) |

Every suite runs inside `evals/sandbox.py`, which redirects repo writes into a temp sandbox, and writes its reports under `evals/results/<suite>/<run_id>/`. That directory is **gitignored**; per-run reports are never committed.

`deepswe` and `terminal_bench` are container-backed and need a working Docker daemon; `toolbench` needs `TOOLBENCH_KEY`, `MAIN_LLM_API_KEY` and an OpenAI-compatible ToolEval judge key; `agentbench` needs `MAIN_LLM_API_KEY`. Without their prerequisites all four SKIP loudly and exit non-zero (a skipped suite is not a pass). Task trees and benchmark data are cloned/downloaded into `~/.cache/sherry_evals` (override with `DEEPSWE_TASKS_DIR` / `TERMINAL_BENCH_DIR` / `SHERRY_AGENTBENCH_REPO` / `SHERRY_TOOLBENCH_DATA` / `SHERRY_EVAL_CACHE`), never inside the repository; `--dry-run` validates a task selection without Docker, a model or the large ToolBench download.

`coding_capability`, `swebench`, `deepswe`, `terminal_bench`, `agentbench` and `toolbench` additionally keep a version-controlled baseline (in their suite directories), point the workspace at a throwaway directory (`SHERRY_WORKSPACE_DIR`) so a run never touches the live persona, and exit non-zero when they drop below the baseline threshold or a task errors; provider-side failures are reported as invalid runs and excluded from the rate.

---

## 📝 Character Profile Examples

The Agent's behavior is driven by the files under `workspace/`:

- **SOUL.md**: Defines personality contrasts, speech style, and behavioral logic.
- **AGENTS.md**: Defines tool usage priorities, safety boundaries, and ethical guidelines.
- **USER.md**: Stores user-specific interaction preferences and known facts.
- **ROLE.md**: States who the AI plays and who the user plays ("用户将扮演…" style); composed by the 预设角色 (Presets & Role) panel in the active UI language and saved inside persona presets.
- **HEARTBEAT.md**: Lists pending tasks for the heartbeat scheduled service.
- **prompt_builder.py**: Builds the system prompt from the profile files.
- **file_sync.py**: Lazily copies any missing persona files from `workspace/template/<lang>/` (selected via `WORKSPACE_TEMPLATE_LANG`) without ever overwriting user edits.

---

## 🤝 Contributing

Issues and Pull Requests are welcome! To add a new skill:

1. Create a folder under `skills/` (or `skills/plugins/` for third-party skills).
2. Write a `SKILL.md` with YAML frontmatter (`name`, `description`, optional `scope`) describing the skill's usage and steps.
3. Restart the Agent — the loader auto-discovers every `SKILL.md` and exposes it to the model. (You can also ask the running Agent to use the built-in `skill_creator` skill to generate one.)

Third-party skills under `skills/plugins/` are scanned by SkillSpector and stay inactive until explicitly enabled.

---

Contact Information: QQ 3132225629

## 📄 License

This project is licensed under the MIT License.

---

> **💡 Tip**: This project is inspired by the exploration of advanced AI agents and deep role-playing.
