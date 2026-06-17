# 🍊 EMA AI Agent - 橘雪莉 (Sherry)

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**中文文档**](README.zh.md) | **English**

> **A deep role-playing AI Agent built on LangGraph and multimodal technology.**

## ✨ Introduction

EMA AI Agent is a highly anthropomorphic AI agent system with long-term memory and complex reasoning capabilities. It's more than just a chatbot — it's a virtual companion with an independent **Persona**, a dynamic **Skill Memory Graph**, and proactive behavior through scheduled tasks and background subagents.

The Agent's character, **橘雪莉 (Sherry)**, is a detective girl with a dual personality contrast (gentle/cold) that shifts based on intimacy level. The entire system is designed to support immersive, persistent role-playing with memory that accumulates across sessions.

---

## 🚀 Key Features

### 1. 🧠 Deep Memory System (Context Engine)
- **Dual Memory Architecture**: Short-term session memory ([MesMemory](context_engine/mes_memory/README.md)) + long-term knowledge graph ([Skill Memory](context_engine/skill_memory/README.md))
- **Skill Memory Graph**: Automatically extracts knowledge points from conversations to build a dynamic knowledge graph
- **Community Detection & Summarization**: Periodically partitions the graph and generates summaries for efficient long-term memory retrieval
- **Query Rewriting**: Disambiguates pronouns and references using conversation history before retrieval
- **Persistent Storage**: Stores conversation history and memory nodes in SQLite + FTS5, supporting cross-session memory inheritance
- **Async Non-blocking Extraction**: Skill Memory extraction runs in the background; MesMemory writes are synchronous and immediate
- ▶️ _See the [Context Engine README](context_engine/README.md) for architecture, data models, and API details_

### 2. 🛠️ Dynamic Skill System
- **SKILL.md Standard**: Skills defined in standardized Markdown format — the Agent can autonomously read and learn new abilities
- **Tool Calling**: Built-in Web search, file I/O, code execution (Python Repl), terminal commands, message search, and more
- **Subagents**: Supports running complex time-consuming tasks in parallel in the background, with async results via a message bus
- ▶️ _See the [Subagent System README](subagent/README.md) for lifecycle, Commander architecture, and API docs_

### 3. 🌐 Multi-Channel Access
- **Web UI**: Modern chat interface built with Streamlit, supporting multimodal input (images, voice)
- **Next-Generation Client** ([client_future](client_future/)): A Tauri 2 + Nuxt 4 desktop/mobile SPA client, currently in development
- **QQ Bot**: Integrated with `qq-botpy` for direct interaction in QQ groups or private chats
- **Message Bus**: Internal async message queue ([MessageBus](bus/queue.py)) decouples input/output channels

### 4. 🔊 Multimodal Interaction
- **TTS Voice Synthesis**: Foreign integrated with GPT-SoVITS for real-time voice replies that faithfully reproduce the character's voice
- **Visual Understanding**: Supports Image-to-Text (VL) models for recognizing and analyzing user-uploaded images

### 5. ⏰ Scheduled & Proactive Behavior
- **Cron Service** ([cron/](cron/README.md)): Schedule periodic, one-shot, or cron-expression-based agent tasks
- **Heartbeat Service** ([heartbeat/](heartbeat/README.md)): Periodic wake-up that checks HEARTBEAT.md for pending tasks and executes them automatically during idle time
---

## 🏗️ Tech Stack

Built on **Python 3.13**, with the following core technologies:

| Module | Technology |
| :----- | :--------- |
| **Agent Framework** | LangChain 1.3+, langchain-classic, LangGraph |
| **Vector & Retrieval** | FAISS, LightRAG, Sentence Transformers, BGE/BAAI Embedding series |
| **Database** | SQLite (FTS5 full-text search), LanceDB |
| **Graph Algorithms** | igraph + Leiden Algorithm (community detection), PageRank |
| **Web Server** | Robyn + FastAPI (dual async server) |
| **Frontend UI** | Streamlit, Tauri 2 + Nuxt 4 (next-gen client) |
| **LLM Support** | DeepSeek, OpenAI, Ollama (local models), langchain-deepseek |
| **Task Scheduling** | croniter, asyncio |
| **Async Messaging** | asyncio.Queue (MessageBus) |

---

## 📂 Project Structure

```text
EMA_AI_agent/
├── agent/                  # Agent core logic & middleware
│   ├── core.py             # Main agent loop (LangGraph compiled graph)
│   ├── middlewares/        # 6 middlewares: ContextEngineHook, Summarization,
│   │                       #   ToolLoopPrevention, ToolCallNormalize,
│   │                       #   ToolTimeout, MultimodalProcessor
│   └── checkpointer/       # Session state checkpointing
│
├── bus/                    # Message bus (async queue)
│   ├── queue.py            # MessageBus — inbound/outbound queues
│   └── events.py           # InboundMessage, OutboundMessage data models
│
├── channels/               # Channel adapters (QQ, WebSocket)
├── client/                 # Streamlit frontend entry
├── client_future/          # Next-gen client (Tauri 2 + Nuxt 4)
│   ├── app/                # Nuxt 4 SPA source
│   │   ├── app.vue                  # Root component entry
│   │   ├── common.scss              # Global SCSS mixin library
│   │   ├── assets/
│   │   │   ├── css/
│   │   │   │   ├── main.css         # Global CSS reset + CSS variables
│   │   │   │   ├── main.scss        # (reserved)
│   │   │   │   └── tailwind.scss    # Tailwind directives
│   │   │   └── ts/
│   │   │       └── tailwind.config.ts # Tailwind custom tokens
│   │   ├── composables/             # Vue 3 composable logic
│   │   │   └── mitt.ts              # mitt event bus instance
│   │   ├── declare/                 # (reserved) Type declarations
│   │   │   └── declarations.d.ts
│   │   ├── layouts/
│   │   │   └── default.vue          # Default layout entry
│   │   ├── pages/
│   │   │   └── index.vue            # Main page
│   │   ├── nuxt.config.ts           # Nuxt 4 configuration
│   │   ├── package.json             # Dependency manifest
│   │   └── tsconfig.json            # TypeScript configuration
│   ├── src-tauri/                   # Tauri 2 native shell
│   │   ├── capabilities/
│   │   │   └── default.json         # Permission config
│   │   ├── src/
│   │   │   ├── lib.rs               # Tauri app entry
│   │   │   └── main.rs              # Windows subsystem entry
│   │   ├── Cargo.toml               # Rust dependencies
│   │   ├── tauri.conf.json          # Tauri 2 config
│   │   └── build.rs                 # Tauri build script
│   ├── README.md                    # This file (English)
│   └── README.zh.md                 # Chinese version
│
├── config/                 # Centralized configuration
│   ├── path.py             # File path configuration
│   ├── schema.py           # Configuration schema models
│   ├── character.py        # Character profile configuration
│   └── num.py              # Numeric/tuning parameters
│
├── context_engine/         # Memory engine — see README for full docs
│   ├── pre_builder.py      # Unified pre-build API (query rewrite + memory assembly)
│   ├── mes_memory/         # Short-term session message memory (SQLite + FTS5)
│   │   ├── core.py         # Business logic: retrieval, search, nudge extraction
│   │   └── store/          # Data layer: SQLite CRUD, migrations
│   └── skill_memory/       # Long-term knowledge graph
│       ├── core.py         # Orchestrator: assemble, ingest, after_turn
│       ├── extractor/      # LLM-based node/edge extraction from dialogue
│       ├── recaller/       # Dual-path recall (precise + generalized)
│       ├── graph/          # Community detection (Leiden), PageRank, dedup
│       └── store/          # SQLite + vectors + FTS5 storage
│
├── cron/                   # Scheduled task service — see README
│   ├── core.py             # CronService: timer loop, job execution
│   ├── types.py            # CronSchedule, CronPayload, CronJob models
│   └── jobs.json           # Persistent job store
│
├── heartbeat/              # Periodic task check service — see README
│   ├── core.py             # HeartbeatService: loop, LLM decision, execution
│   └── evaluate.py         # Notification gate: decide if results are worth delivering
│
├── models/                 # Model wrappers
│   ├── chat_model.py       # Chat model (LangChain BaseChatModel)
│   ├── simple_chat_model.py # Lightweight chat model
│   ├── reasoner_model.py   # Reasoner model (chain-of-thought)
│   ├── VTTT_model.py       # Video-Text-to-Text model
│   ├── ITT_model.py        # Image-to-Text model
│   ├── SST_model/          # Speech-to-Text model
│   ├── embed_model/        # Text embedding model
│   ├── reranker_model/     # Cross-encoder reranker
│   ├── extract_model/      # Entity extraction model
│   └── sovits_model/       # TTS voice synthesis (GPT-SoVITS)
│
├── pub_func/               # Common utility functions
├── rag/                    # Retrieval-Augmented Generation modules
│   ├── agentic_rag/        # Agentic RAG with tool-calling orchestration
│   ├── mutil_hop_graphrag/ # Multi-hop reasoning over graph knowledge
│   └── rag_anything/       # General-purpose RAG adapter
│
├── runtime/                # Runtime utilities
│   ├── count_register.py   # Usage/statistics counters
│   └── relation_register.py# Relationship/intimacy tracking
│
├── server/                 # Robyn backend service & API routes
├── sessions/               # Session runtime data (per-session)
├── skills/                 # Skill library (SKILL.md definition files)
│   ├── loader.py           # Skill autodiscovery & registration
│   ├── skills_snapshot.py  # Builds skill prompt snapshot
│   └── builtin/            # Built-in skill implementations
│       ├── core/           # Core built-in skills
│       │   ├── web_search/     # Web search & scrape
│       │   ├── python_repl/    # Python code execution
│       │   ├── terminal/       # Terminal command execution
│       │   ├── image_to_text/  # Image understanding
│       │   ├── speech_to_text/ # Speech recognition
│       │   ├── video_text_to_text/ # Video understanding
│       │   ├── rag/            # RAG-based knowledge retrieval
│       │   ├── clawhub/        # GitHub repository cloner
│       │   └── skill_creator/  # Auto-generate new skills
│       └── text_to_image/  # Image generation
├── src/                    # Runtime data directories
│   ├── avatar/             # Character avatar images
│   ├── checkpoints/        # Session checkpoints
│   ├── gallery/            # Image gallery
│   ├── rag/                # RAG index data
│   ├── sessions/           # Session stores
│   ├── store/              # Data stores
│   └── temp/               # Temporary files
│
├── subagent/               # Subagent system — see README
│   ├── core.py             # SubagentManager (singleton orchestrator)
│   ├── commander/          # LangGraph-based Commander agent
│   │   ├── core.py         # build_commander() — graph construction
│   │   ├── tools/          # TodoWriter, Worker (parallel dispatch)
│   │   └── middlewares/    # TodoInjector, TodoCleaner, SummarizationMiddleware
│   ├── templates/          # Result announcement templates (Jinja2-style)
│   └── type.py             # SubAgentOutput data model
│
├── tests/                  # Test suite
├── future/                 # New functions that may be released in the future.
├── tools/                  # Agent-accessible tools
│   ├── web_search.py       # Web search tool
│   ├── python_repl.py      # Python code execution
│   ├── terminal.py         # Terminal command execution
│   ├── read_file.py        # File reading
│   ├── write_file.py       # File writing
│   ├── subagent.py         # Subagent spawn tool (SubagentTool)
│   ├── cron.py             # Cron management tool
│   ├── memory.py           # Memory inspection tool
│   └── message_search.py   # Conversation search tool
│
├── type/                   # Shared data models
│   ├── __init__.py         # MultiModalMessage, Chat, FileType, etc.
│   └── ...                 # Pydantic v2 models
│
├── workspace/              # Character profile files (IDENTITY.md, SOUL.md, USER.md)
├── output/                 # Output directory (generated files)
├── start.sh                # One-click startup script
└── .env                    # Environment variables (API keys, model paths)
```

---

## 📚 Submodule Documentation

Each major subsystem has its own detailed README:

| Submodule | Description | Documentation |
|-----------|-------------|---------------|
| **Context Engine** | Dual memory system (MesMemory + Skill Memory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **MesMemory** | Short-term session message memory | [EN](context_engine/mes_memory/README.md) · [ZH](context_engine/mes_memory/README.zh.md) |
| **Skill Memory** | Long-term knowledge graph memory | [EN](context_engine/skill_memory/README.md) · [ZH](context_engine/skill_memory/README.zh.md) |
| **Subagent System** | Hierarchical task decomposition & parallel execution | [EN](subagent/README.md) · [ZH](subagent/README.zh.md) |
| **Cron Service** | Scheduled/periodic agent task execution | [EN](cron/README.md) · [ZH](cron/README.zh.md) |
| **Heartbeat Service** | Periodic wake-up task check | [EN](heartbeat/README.md) · [ZH](heartbeat/README.zh.md) |
| **Next-gen Client** | Tauri 2 + Nuxt 4 desktop/mobile SPA client | [EN](client_future/README.md) · [ZH](client_future/README.zh.md) |

---

## ⚡ Quick Start

### 1. Prerequisites
Make sure **Python 3.13+** is installed.

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv sync
```

### 2. Model Download
On first run, the system will automatically download the Embedding model and Reranker model from **Hugging Face** to `models/embed_model` and `models/reranker_model`. Please note:

- **Network**: Ensure you can reach huggingface.co (users in China may need a proxy or mirror).
- **Be Patient**: Model weights are large (hundreds of MB to several GB); download time depends on your connection speed.
- **Resume on Interruption**: If the download is interrupted, delete the corresponding directory and restart to re-download.

> You can also manually download the models and place them in the directories to skip the auto-download.

### 3. Configure Environment Variables
Copy the `.env` example and fill in your API Keys (DeepSeek, OpenAI, etc.) and TTS model path.

```bash
cp .env.example .env
# Edit .env to configure CHAT_API_KEY, GPT_SOVITS_DIR, etc.
```

### 4. Start Services
Use the provided `start.sh` script to launch TTS, local Ollama models, the backend, and the frontend UI all at once.

```bash
chmod +x start.sh
./start.sh
```

### 5. (Optional) Manual Startup

You can also start each component manually:

```bash
python -m server  # Start backend
streamlit run client/core.py  # Start frontend
```

---

## 📝 Character Profile Examples

The Agent's behavior is driven by Markdown files under `workspace/`:

- **IDENTITY.md**: Defines name, age, interests, relationships, etc.
- **SOUL.md**: Defines personality contrasts, speech style (e.g. "~です"), and behavioral logic.
- **AGENTS.md**: Defines tool usage priorities, safety boundaries, and ethical guidelines.
- **USER.md**: Stores user-specific interaction preferences and known facts.

---

## 🤝 Contributing

Issues and Pull Requests are welcome! To add a new skill:

1. Create a folder under `skills/`.
2. Write a `SKILL.md` describing the skill's usage and steps.
3. Restart the Agent — it will auto-discover and load the new skill.

---

Contact Information: QQ 3132225629

## 📄 License

This project is licensed under the MIT License.

---

> **💡 Tip**: This project is inspired by the exploration of advanced AI agents and deep role-playing.
