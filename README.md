# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**中文文档**](README.zh.md) | **English**

> **A deep role-playing AI Agent built on LangGraph and multimodal technology.**

## ✨ Introduction

EMA AI Agent is a highly anthropomorphic AI agent system with long-term memory and complex reasoning capabilities. It's more than just a chatbot — it's a virtual companion with an independent **Persona**, a dynamic **Skill Memory Graph**, and proactive behavior through scheduled tasks and background subagents.

The Agent's character, **Sherry**, is a detective girl with a dual personality contrast (gentle/cold) that shifts based on intimacy level. The entire system is designed to support immersive, persistent role-playing with memory that accumulates across sessions.

---

## 🚀 Key Features

### 1. 🧠 Deep Memory System (Context Engine)
- **Dual Memory Architecture**: Short-term session memory ([MesMemory](context_engine/README.md)) + long-term knowledge graph ([Skill Memory](context_engine/skill_memory/README.md))
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
- ▶️ _See the [Subagent System README](agent/tools/subagent/README.md) for lifecycle, Commander architecture, and API docs_

### 3. 🌐 Multi-Channel Access
- **Web UI**: Modern chat interface built with Streamlit, supporting multimodal input (images, voice)
- **Next-Generation Client** ([client_future](client_future/)): A Tauri 2 + Nuxt 4 desktop/mobile SPA client, currently in development
- **QQ Bot**: QQ channel adapter via plugin system (`plugins/channels/`)
- **Message Bus**: Internal async message queue ([MessageBus](bus/core.py)) decouples input/output channels

### 4. 👁️ Multimodal Interaction
- **Visual Understanding**: Supports Image-to-Text (VL) models for recognizing and analyzing user-uploaded images

### 5. ⏰ Scheduled & Proactive Behavior
- **Cron Service** ([skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)): Schedule periodic, one-shot, or cron-expression-based agent tasks
- **Heartbeat Service** ([skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)): Periodic wake-up that checks HEARTBEAT.md for pending tasks and executes them automatically during idle time

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
│   ├── middlewares/        # Middlewares: Summarization, ToolCallNormalize,
│   │                       #   ToolGuardrails, ToolTimeout, IterationBudget,
│   │                       #   MultimodalProcessor, ContextEngineHook
│   └── checkpointer/       # Session state checkpointing
│
├── bus/                    # Message bus (async queue)
│   └── core.py             # MessageBus — inbound/outbound queues & events
│
├── channels/               # Channel interface definitions
│   ├── base.py             # Abstract channel base
│   ├── manager.py          # Channel lifecycle manager
│   └── registry.py         # Channel registration
│
├── client/                 # Streamlit frontend entry
│   ├── api/                # API client layer
│   └── core.py             # Streamlit app entry
│
├── client_future/          # Next-gen client (Tauri 2 + Nuxt 4)
│   ├── app/                # Nuxt 4 SPA source
│   │   ├── app.vue         # Root component entry
│   │   ├── pages/          # Page components
│   │   ├── layouts/        # Layout components
│   │   ├── composables/    # Vue 3 composable logic
│   │   ├── assets/         # CSS & config assets
│   │   ├── nuxt.config.ts  # Nuxt 4 configuration
│   │   └── package.json    # Dependency manifest
│   ├── src-tauri/          # Tauri 2 native shell (Rust)
│   │   ├── src/            # Rust source
│   │   ├── Cargo.toml      # Rust dependencies
│   │   └── tauri.conf.json # Tauri 2 config
│   └── README.md           # English documentation
│
├── config/                 # Centralized configuration
│   ├── path.py             # File path configuration
│   ├── schema.py           # Configuration schema models
│   ├── character.py        # Character profile configuration
│   └── num.py              # Numeric/tuning parameters
│
├── context_engine/         # Memory engine
│   ├── pre_builder.py      # Unified pre-build API (query rewrite + memory assembly)
│   ├── mes_memory/         # Short-term session message memory (SQLite + FTS5)
│   └── skill_memory/       # Long-term knowledge graph
│       ├── core.py         # Orchestrator: assemble, ingest, after_turn
│       ├── extractor/      # LLM-based node/edge extraction from dialogue
│       ├── recaller/       # Dual-path recall (precise + generalized)
│       ├── graph/          # Community detection (Leiden), PageRank, dedup
│       └── store/          # SQLite + vectors + FTS5 storage
│
├── future/                 # Experimental / upcoming features
│
├── logs/                   # Logging system
│   ├── logger.py           # Log configuration
│   └── output/             # Log output directory
│
├── models/                 # Model wrappers
│   ├── chat_model.py       # Chat model (LangChain BaseChatModel)
│   ├── LLMs/               # LLM model configs
│   │   ├── auxiliary_llm.py    # Lightweight chat model
│   │   ├── main_llm.py         # Primary chat model
│   │   └── reasoner_llm.py     # Chain-of-thought reasoning model
│   ├── VTTT_model.py       # Video-Text-to-Text model
│   ├── ITTT_model.py        # Image-to-Text model
│   ├── STT_model/          # Speech-to-Text model
│   ├── embed_model/        # Text embedding model
│   ├── reranker_model/     # Cross-encoder reranker
│   └── extract_model/      # Entity extraction model
│
├── plugins/                # Plugin system
│   ├── channels/           # Channel plugins (QQ bot, etc.)
│   └── mcp_server/         # MCP server configurations
│
├── pub_func/               # Common utility functions
│   ├── format/             # Text formatting utilities
│   ├── media/              # Media processing utilities
│   ├── message/            # Message processing utilities
│   └── validator/          # Input validation utilities
│
├── runtime/                # Runtime state & utilities
│   ├── core.py             # Core runtime lifecycle
│   ├── _callback_executor.py   # Async callback executor
│   ├── count_call_register.py   # Usage/statistics counters
│   ├── relation_register.py    # Relationship/intimacy tracking
│   ├── state_register.py   # State registry
│   └── timer_call_register.py   # Timer registry
│
├── server/                 # Robyn backend service & API routes
│   ├── DAO/                # Data access objects
│   ├── service/            # Business logic services
│   └── trigger/            # Trigger managers
│       ├── channels/       # Incoming channel triggers
│       ├── http/           # HTTP endpoint triggers
│       └── subagent/       # Subagent result triggers
│
├── sessions/               # Session management
│   ├── main/               # Active session store
│   └── store.py            # Session CRUD operations
│
├── skills/                 # Skill library (SKILL.md definition files)
│   ├── loader.py           # Skill autodiscovery & registration
│   ├── skills_snapshot.py  # Builds skill prompt snapshot
│   └── builtin/            # Built-in skill implementations
│       └── core/           # Core built-in skills
│           ├── web_search/     # Web search & scrape
│           ├── cron/           # Cron scheduled task skill
│           ├── heartbeat/      # Heartbeat periodic check skill
│           ├── image_to_text/  # Image understanding
│           ├── speech_to_text/ # Speech recognition
│           ├── video_text_to_text/ # Video understanding
│           ├── multimodal_rag/ # RAG-based knowledge retrieval
│           ├── clawhub/        # GitHub repository cloner
│           └── skill_creator/  # Auto-generate new skills
│
├── src/                    # Runtime data directories
│   ├── checkpoints/        # Session checkpoints
│   ├── data/               # Data storage
│   ├── gallery/            # Image gallery
│   ├── rag/                # RAG index data
│   ├── sessions/           # Session runtime stores
│   └── store/              # Data stores
│
├── static/                 # Static assets
│   ├── avatar/             # Character avatar images
│   └── images/             # Other images
│
├── temp/                   # Temporary files
│
├── tests/                  # Test suite
│
├── tools/                  # Agent-accessible tools
│   ├── subagent/           # Subagent system
│   │   ├── core.py         # SubagentManager (singleton orchestrator)
│   │   ├── commander/      # LangGraph-based Commander agent
│   │   ├── templates/      # Result announcement templates
│   │   └── type.py         # SubAgentOutput data model
│   ├── mcp_plugin.py       # MCP plugin tool
│   ├── web_search.py       # Web search tool
│   ├── python_repl.py      # Python code execution
│   ├── terminal.py         # Terminal command execution
│   ├── read_file.py        # File reading
│   ├── write_file.py       # File writing
│   ├── memory.py           # Memory inspection tool
│   ├── message_search.py   # Conversation search tool
│   └── cron.py             # Cron management tool (deprecated)
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
│   ├── USER.md             # User-specific preferences & facts
│   ├── HEARTBEAT.md        # Pending tasks for heartbeat service
│   ├── character.json      # Character configuration
│   ├── prompt_builder.py   # Profile-to-prompt builder
│   ├── template/           # Prompt templates
│   └── memory/             # Long-term memory storage
│
├── .env                    # Environment variables (API keys, model paths)
├── .env.example            # Environment variable template
├── pyproject.toml          # Python dependencies (uv managed)
├── start.sh                # One-click startup script
└── TODOList.md             # Development roadmap
```

---

## 📚 Submodule Documentation

Each major subsystem has its own detailed README:

| Submodule | Description | Documentation |
|-----------|-------------|---------------|
| **Context Engine** | Dual memory system (MesMemory + Skill Memory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **MesMemory** | Short-term session message memory | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **Skill Memory** | Long-term knowledge graph memory | [EN](context_engine/skill_memory/README.md) · [ZH](context_engine/skill_memory/README.zh.md) |
| **Subagent System** | Hierarchical task decomposition & parallel execution | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **Cron Service** | Scheduled/periodic agent task execution | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **Heartbeat Service** | Periodic wake-up task check | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |
| **Next-gen Client** | Tauri 2 + Nuxt 4 desktop/mobile SPA client | [EN](client_future/README.md) · [ZH](client_future/README.zh.md) |
| **Channels** | Channel interface & adapter system | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **Middlewares** | Agent lifecycle middleware pipeline | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |

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
Copy the `.env` example and fill in your API Keys (DeepSeek, OpenAI, etc.) and model paths.

```bash
cp .env.example .env
# Edit .env to configure MAIN_LLM_API_KEY, model paths, etc.
```

### 4. Start Services
Use the provided `start.sh` script to launch local Ollama models, the backend, and the frontend UI all at once.

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
- **SOUL.md**: Defines personality contrasts, speech style, and behavioral logic.
- **AGENTS.md**: Defines tool usage priorities, safety boundaries, and ethical guidelines.
- **USER.md**: Stores user-specific interaction preferences and known facts.
- **HEARTBEAT.md**: Lists pending tasks for the heartbeat scheduled service.
- **character.json**: Structured character configuration (JSON).

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
