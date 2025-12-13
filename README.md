# 🍊 EMA AI Agent - 橘雪莉 (Sherry)

![Python](https://img.shields.io/badge/Python-3.12-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.2-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**中文文档**](README.zh.md) | **English**

> **A deep role-playing AI Agent built on LangGraph and multimodal technology.**

## ✨ Introduction

EMA AI Agent is a highly anthropomorphic AI agent system with long-term memory and complex reasoning capabilities. It's more than just a chatbot — it's a virtual companion with an independent **Persona**, **Skills**, and a dynamic **Skill Memory Graph**.



---

## 🚀 Key Features

### 1. 🧠 Deep Memory System (Context Engine)
- **Skill Memory Graph**: Automatically extracts knowledge points from conversations to build a dynamic knowledge graph.
- **Community Detection & Summarization**: Periodically partitions the graph and generates summaries for efficient long-term memory retrieval.
- **Persistent Storage**: Stores conversation history and memory nodes in SQLite, supporting cross-session memory inheritance.

### 2. 🛠️ Dynamic Skill System
- **SKILL.md Standard**: Skills defined in standardized Markdown format — the Agent can autonomously read and learn new abilities.
- **Tool Calling**: Built-in Web search, file I/O, code execution (Python Repl), terminal commands, and more.
- **Subagents**: Supports running complex time-consuming tasks in parallel in the background, with async results via a message bus.

### 3. 🌐 Multi-Channel Access
- **Web UI**: Modern chat interface built with Streamlit, supporting multimodal input (images, voice).
- **QQ Bot**: Integrated with `qq-botpy` for direct interaction in QQ groups or private chats.
- **Message Bus**: Internal async message queue (MessageBus) decouples input/output channels.

### 4. 🔊 Multimodal Interaction
- **TTS Voice Synthesis**: Integrated with GPT-SoVITS for real-time voice replies that faithfully reproduce the character's voice.
- **Visual Understanding**: Supports Vision-Language (VL) models for recognizing and analyzing user-uploaded images.

---

## 🏗️ Tech Stack

Built on **Python 3.12**, with the following core technologies:

| Module | Technology |
| :----- | :--------- |
| **Agent Framework** | LangChain 1.2, LangGraph |
| **Vector & Retrieval** | FAISS, LightRAG, Sentence Transformers |
| **Database** | SQLite (FTS5 full-text search), LanceDB |
| **Web Server** | Robyn (high-performance async server) |
| **Frontend UI** | Streamlit, Nuxt.js + Tauri (client) |
| **LLM Support** | DeepSeek, OpenAI, Ollama (local models) |

---

## 📂 Project Structure

```text
EMA_AI_agent/
├── agent/              # Agent core logic & middleware (e.g. Summarization)
├── context_engine/     # Memory engine (MesMemory & Skill Memory Graph)
├── skills/             # Skill library (SKILL.md definition files)
├── tools/              # Tools (Web Search, Python Repl, Agentic RAG, etc.)
├── channels/           # Channel adapters (QQ, WebSocket)
├── client/             # Streamlit frontend entry
├── server/             # Robyn backend service & API routes
├── workspace/          # Character profile files (IDENTITY.md, SOUL.md, USER.md)
├── models/             # Model wrappers (Chat, Reasoner, VL, TTS)
└── pub_func/           # Common utility functions
```

---

## ⚡ Quick Start

### 1. Prerequisites
Make sure **Python 3.12+** is installed.

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
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

*   **IDENTITY.md**: Defines name, age, interests, relationships, etc.
*   **SOUL.md**: Defines personality contrasts, speech style (e.g. "~です"), and behavioral logic.
*   **AGENTS.md**: Defines tool usage priorities, safety boundaries, and ethical guidelines.

---

## 🤝 Contributing

Issues and Pull Requests are welcome! To add a new skill:
1. Create a folder under `skills/`.
2. Write a `SKILL.md` describing the skill's usage and steps.
3. Restart the Agent — it will auto-discover and load the new skill.

---

## 📄 License

This project is licensed under the MIT License.

---

> **💡 Tip**: This project is inspired by the exploration of advanced AI agents and deep role-playing.
