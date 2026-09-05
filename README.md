# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **A deep role-playing AI Agent built on LangChain/LangGraph and multimodal technology.**

## ✨ Introduction

EMA AI Agent is a highly anthropomorphic AI agent system with long-term memory and complex reasoning capabilities. It's more than just a chatbot — it's a virtual companion with an independent **Persona**, a dynamic **Skill System**, and proactive behavior through scheduled tasks and background subagents.

The Agent's character, **Sherry** (Tachibana Sherry), is a self-proclaimed girl detective: ever-cheerful and energetic on the outside, calm and razor-sharp at the core. The entire system is designed to support immersive, persistent role-playing with memory that accumulates across sessions.

---

## 🚀 Key Features

### 1. 🧠 Layered Memory System (Context Engine)
- **Short-term Session Memory** ([MesMemory](context_engine/README.md)): every human/ai/tool message persisted to SQLite (WAL mode) with automatic FTS5 indexing — including a trigram tokenizer table for Chinese full-text search
- **History Retrieval**: last-N-turns, paginated history, or turn-range queries formatted as prompt context
- **Session Checkpointing**: thread-safe async SQLite checkpointer (`langgraph-checkpoint-sqlite`) persists agent state across restarts; stale checkpoints are cleaned automatically
- **Conversation Summarization**: an auxiliary LLM compresses long histories mid-conversation via the Summarization middleware
- **Private Knowledge Graph RAG**: the `multimodal_rag` skill indexes documents/folders into an entity–relationship graph (vendored LightRAG + RAG-Anything on `snkv` vector storage) and answers via multi-hop graph retrieval
- ▶️ _See the [Context Engine README](context_engine/README.md) for architecture, data models, and API details_

### 2. 🛠️ Dynamic Skill System
- **SKILL.md Standard**: skills are Markdown files with YAML frontmatter (`name`, `description`, optional `scope: all | main_only | subagent_only`) — the loader auto-discovers every `SKILL.md` under `skills/`
- **Built-in Skills** ([skills/builtin/](skills/builtin/)): `cron`, `heartbeat`, `clawhub` (GitHub skill installer), `skill_creator` (generates new skills), `image_to_text`, `speech_to_text`, `video_text_to_text`, `text_to_image`, `multimodal_rag`, `code_wiki`, `llm_wiki`
- **Skill Management Tools**: the agent can list, view, and manage skills at runtime; third-party uploads (`skills/plugins/`) stay inactive until explicitly enabled
- **SkillSpector Security Scanning** ([server/service/skill_scanner.py](server/service/skill_scanner.py)): third-party skills are scanned by NVIDIA SkillSpector (static YARA/rule analysis + optional LLM semantic analysis via the auxiliary LLM) before activation; flagged skills are blocked from installation
- **Skill Curator**: the context-engine curator thread maintains auto-learned skills under `skills/auto/`
- **Tool Timeouts**: tool calls are bounded by `TOOL_CALL_TIMEOUT_MINUTES` (default 5) to prevent deadlocks
- ▶️ _See the [Middlewares README](agent/middlewares/README.md) for the middleware pipeline (guardrails, iteration budget, HITL, normalization, summarization, multimodal processing)_

### 3. 🤖 Multi-level Subagent System
- **7 Runtime Tools**: `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list`
- **Hierarchical Roles**: depth-limited nesting (default max depth 2, hard cap 2) with MAIN → ORCHESTRATOR → LEAF roles and least-privilege tool scoping
- **Context Modes**: ISOLATED (fresh context) or FORK (copy of the parent transcript), plus file attachments
- **Reliable Delivery**: results return through an EventBus announce pipeline with idempotency checks and exponential-backoff retries
- **Durable Registry**: run records persisted to SQLite; a sweeper recovers orphaned runs and a followup checker enforces run timeouts when configured (default: none)
- **Swarm Mode**: batch sub-task execution with FIFO scheduling and configurable concurrency
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
| **Web Search** | langchain-tavily (Tavily API) |
| **LLM Providers** | langchain-openai, langchain-deepseek, langchain-community + a 20+ provider registry (OpenAI, Anthropic, DeepSeek, Zhipu GLM, DashScope Qwen, Gemini, Moonshot Kimi, MiniMax, Groq, OpenRouter, SiliconFlow, Volcengine, Azure OpenAI, Ollama, vLLM, and more) |
| **Structured Output** | instructor, json_repair |
| **MCP** | langchain-mcp-adapters (servers configured in `plugins/mcp_server/`) |
| **Task Scheduling** | croniter, asyncio |
| **Async Messaging** | asyncio queues (MessageBus, EventBus) |
| **Media Processing** | OpenCV (headless), Pillow, websockets / websocket-client |
| **Desktop Client** | Tauri 2 + Nuxt 4 (Vue 3, TypeScript, pnpm) |
| **Logging** | loguru (optional LangSmith tracing) |

---

## 📂 Project Structure

```text
EMA_AI_agent/
├── agent/                  # Agent core logic
│   ├── core.py             # Main agent loop (LangChain create_agent → LangGraph graph)
│   ├── smart_tool_node.py  # Tool-node patching (idempotent-tool parallelism)
│   ├── stream_repetition_guard_wrapper.py # Stream-level output repetition guard
│   ├── checkpointer/       # Thread-safe async SQLite checkpointers
│   ├── middlewares/        # Middleware pipeline (summarization, guardrails, HITL, ...)
│   └── tools/              # Agent-accessible tools
│       ├── subagent/       # Multi-level subagent system (spawn/registry/swarm/...)
│       ├── file_tools/     # File I/O tools (read, write, patch, search)
│       ├── skill_tools/    # Skill management tools (list, view, manage)
│       ├── pub_base/       # Shared tool utilities & infrastructure
│       ├── mcp_plugin.py   # MCP tool integration
│       ├── web_search.py   # Web search tool (Tavily)
│       ├── python_repl.py  # Python code execution
│       ├── terminal.py     # Terminal command execution
│       ├── memory.py       # Memory inspection tool
│       └── message_search.py # Conversation FTS5 search tool
│
├── bus/                    # Message bus (async queues)
│   └── core.py             # MessageBus — inbound/outbound queues
│
├── channels/               # Channel interface definitions
│   ├── base.py             # Abstract channel base
│   ├── manager.py          # Channel lifecycle manager
│   └── registry.py         # Channel registration
│
├── client/                 # Desktop client (Tauri 2 + Nuxt 4, pnpm)
│   ├── app/                # Nuxt 4 SPA source (Vue 3)
│   ├── src-tauri/          # Tauri 2 native shell (Rust)
│   └── README.md           # Client documentation
│
├── config/                 # Centralized configuration
│   ├── __init__.py         # API host/port (127.0.0.1:8080)
│   ├── path.py             # File path configuration
│   ├── schema.py           # Configuration schema models
│   └── num.py              # Numeric/tuning parameters
│
├── context_engine/         # Memory engine (MesMemory)
│   ├── core.py             # History retrieval & FTS5 search APIs
│   ├── store/              # Session message store (SQLite + FTS5, WAL)
│   └── curator/            # Auto-skill curation
│
├── logs/                   # Logging system
│   ├── logger.py           # Log configuration (loguru)
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
├── pub_func/               # Common utility functions
│   ├── format/             # Text formatting utilities
│   ├── media/              # Media processing utilities
│   ├── message/            # Message processing utilities
│   └── validator/          # Input validation utilities
│
├── runtime/                # Runtime state & utilities
│   ├── core.py             # Singleton Register base + per-session cleanup
│   ├── relation_register.py # Session/socket relation registry
│   ├── state_register.py   # State registry
│   ├── count_call_register.py # Usage/statistics counters
│   ├── timer_call_register.py # Timer registry
│   └── _callback_executor.py # Async callback executor
│
├── server/                 # Robyn backend service
│   ├── __main__.py         # Server entry point (python -m server)
│   ├── DAO/                # Data access objects
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
│       │                   # speech_to_text, video_text_to_text, multimodal_rag
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
├── tests/                  # Test suite (pytest) + run_tests_split.py (process-isolated test runner)
│
├── type/                   # Shared data models
│   ├── message.py          # MultiModalMessage, Chat, etc.
│   ├── bus.py              # Message bus data models
│   └── client.py           # Client data models
│
├── workspace/              # Character profile & behavior definition
│   ├── IDENTITY.md         # Name, age, interests, relationships
│   ├── SOUL.md             # Personality contrasts, speech style
│   ├── AGENTS.md           # Tool usage priorities, safety boundaries
│   ├── USER.md             # User-specific interaction preferences
│   ├── HEARTBEAT.md        # Pending tasks for heartbeat service
│   ├── character.json      # Character configuration
│   ├── prompt_builder.py   # Profile-to-prompt builder
│   ├── file_sync.py        # Lazy workspace template sync (per language)
│   ├── template/           # Persona templates (en / zh / ja / ko)
│   └── memory/             # Long-term memory storage
│
├── .env.example            # Environment variable template
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
| **Context Engine** | Short-term session message memory (MesMemory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **Subagent System** | Multi-level subagent spawn, parallel execution & result delivery | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **Middlewares** | Agent lifecycle middleware pipeline | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |
| **Channels** | Channel interface & adapter system | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **Desktop Client** | Tauri 2 + Nuxt 4 desktop/mobile SPA client | [EN](client/README.md) · [ZH](client/README.zh.md) |
| **Cron Service** | Scheduled/periodic agent task execution | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **Heartbeat Service** | Periodic wake-up task check | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |

## ⚡ Quick Start

### 1. Prerequisites
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — the dependency manager. It creates and manages `.venv` automatically; there is no need to create a virtual environment manually.

```bash
git clone <your-repo-url>
cd EMA_AI_agent
uv sync   # creates .venv and installs the exact dependencies from uv.lock
```

### 2. Configure Environment Variables
Copy the `.env` example and fill in at least the main chat model and Tavily key:

```bash
cp .env.example .env
```

| Variable | Required | Description |
| :------- | :------- | :---------- |
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | Primary chat model (must support JSON output and tool calling) |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | Universal reasoning switch, mapped per provider (DeepSeek / OpenAI / GLM / Anthropic) |
| `TAVILY_API_KEY` | ✅ for web search | Enables the web search tool |
| `REASONER_LLM_*` | — | Chain-of-thought reasoning model |
| `AUXILIARY_LLM_*` | — | Lightweight model for summarization / simple tasks (cloud API by default; set `AUXILIARY_LLM_MODEL_LOCAL=true` for a local GGUF model) |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | Image / video / text-to-image / speech model configuration |
| `RERANKER_*` / `EMBEDDING_*` | — | Reranker & embedding for retrieval (see model notes below) |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector security scanner switch (on by default); LLM semantic analysis is opt-in (off by default) and requires a provider supporting json_schema structured output |
| `TOOL_CALL_TIMEOUT_MINUTES` / `LOG_LEVEL` | — | Tool timeout (5) and log level (INFO) |
| `WORKSPACE_TEMPLATE_LANG` | — | Persona template language: `en` / `zh` / `ja` / `ko` (lazy-copied on first use) |
| `LANGSMITH_*` | — | Optional LangSmith tracing |

### 3. Model Notes (HuggingFace Auto-Download)
Models configured for **local GGUF** mode are downloaded automatically from Hugging Face into `models/<model>/model_weight/` on first use — no manual download is required:

- **Embedding**: `EMBEDDING_MODEL_LOCAL=true` (the default) uses the local `bge-m3` Q8_0 GGUF, auto-downloaded on first run.
- **Reranker**: the `.env` template defaults to a **cloud API** (`RERANKER_MODEL_LOCAL=false`, OpenAI-compatible `bge-reranker-v2-m3`). Set it to `true` to run the local GGUF reranker instead, which is then auto-downloaded (~636 MB).
- **ITTT / VTTT / Auxiliary LLM**: default to cloud APIs in the template; set `*_MODEL_LOCAL=true` to switch to local GGUF models (also auto-downloaded).

> Network access to huggingface.co is required for first-run downloads (users in China may need a proxy or a mirror). Interrupted downloads are resumed on the next start; delete `models/<model>/model_weight/` to force a re-download.

### 4. Start the Backend
`start.sh` activates the uv-managed `.venv` and launches the Robyn backend (it no longer starts Ollama or any frontend):

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
The Tauri 2 + Nuxt 4 client lives in [client/](client/) and requires Node.js 18+, pnpm, and Rust:

```bash
cd client
pnpm install
pnpm dev          # browser mode, dev server at http://localhost:3000
pnpm tauri dev    # native desktop mode
```

The client connects to the Python backend at `http://127.0.0.1:8080` by default (configurable via `VITE_API_BACK_URL` in `client/.env`). See the [client README](client/README.md) for details.

---

## 🧪 Testing

Tests live under `tests/{unit,integration,system,module}` and run with **pytest** via uv (`uv run pytest` for a single test file or a small selection).

### Recommended: the process-isolated runner

For the full suite (and for CI), use the split runner — it executes the suite in **two sequential pytest processes** (never parallel), aggregates their exit codes, and prints a per-group summary plus a final verdict (exit code 0 only if both groups pass):

```bash
uv run python tests/run_tests_split.py                  # hermetic suite (default, llm_e2e excluded)
uv run python tests/run_tests_split.py --with-llm-e2e   # ONLY the real-LLM e2e tests (dedicated-job mode)
uv run python tests/run_tests_split.py -- -k spawn -q   # args after `--` are forwarded to pytest
```

| Group | Directories | Contents |
| :---- | :---------- | :------- |
| **A** | `tests/unit` | unit tests (home of the `sys.modules` stubs described below) |
| **B** | `tests/integration`, `tests/system`, `tests/module` | hermetic integration / system / module tests |

**Why two processes?** `tests/unit/subagent/conftest.py` installs stub callables into process-global `sys.modules` at conftest *import* time. In a single-process full-suite run, pytest imports every conftest and test module during collection — before any test executes — so those stubs are live for the whole process and leak across directories: lazy (call-time) imports resolve the stub, while modules that bound the real object earlier keep stale bindings. The result is confusing, order-dependent failures in suites far away from `tests/unit` (e.g. skill-scope assertions seeing a stub's fixed skill list, `TypeError` tracebacks naming conftest lambdas). Running the groups in separate processes makes this cross-suite pollution structurally impossible. (The stubs themselves are restore-safe since `c730a46`; the runner is the defense-in-depth operational layer.)

**Windows note:** child pytest processes get `PYTHONIOENCODING=utf-8` in their environment and the runner captures their output with `errors="replace"`, so GBK console codepages can neither corrupt the output nor crash the run.

### Real-LLM e2e tests (`llm_e2e` marker)

Three tests in `tests/integration/` (`test_real_e2e.py`, `test_spawn_direct_e2e.py`) call **real LLM APIs**. They are:

- **deselected by default** (`-m "not llm_e2e"` — set both in `pyproject.toml` addopts and by the runner),
- bounded by `@pytest.mark.timeout` budgets (pytest-timeout): 300 s per simple test, 600 s for the concurrent test,
- run explicitly, in a **dedicated job**: `uv run python tests/run_tests_split.py --with-llm-e2e` (selects `-m llm_e2e`) or `uv run pytest -m llm_e2e`.

**Expected runtimes** (solo, real backend): simple task ≈ 30–60 s; complex worst case ≈ 10 min; concurrent tasks ≈ 2–9 min. A run that exceeds these budgets is a real hang, not normal slowness — the per-test timeout bounds it (300 s simple / 600 s concurrent).

**CI:** this repository currently has no CI configuration; `tests/run_tests_split.py` is the **CI-ready entry point** — wire `uv run python tests/run_tests_split.py` into the primary pipeline (hermetic; two processes ≈ 7 min total) and schedule `--with-llm-e2e` as a separate, slower job (it costs API tokens; never run it in parallel with other suites).

> **Note:** `tests/full/` is an auxiliary/experimental directory outside the standard groups above. In particular `tests/full/test_main_agent_e2e.py` is a live-network test that is **not** tagged `llm_e2e` — do not wire it into CI without tagging it first.

---

## 📝 Character Profile Examples

The Agent's behavior is driven by the files under `workspace/`:

- **IDENTITY.md**: Defines name, age, interests, relationships, etc.
- **SOUL.md**: Defines personality contrasts, speech style, and behavioral logic.
- **AGENTS.md**: Defines tool usage priorities, safety boundaries, and ethical guidelines.
- **USER.md**: Stores user-specific interaction preferences and known facts.
- **HEARTBEAT.md**: Lists pending tasks for the heartbeat scheduled service.
- **character.json**: Structured character configuration (JSON).
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
