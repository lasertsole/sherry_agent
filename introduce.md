[中文版](introduce.zh.md)

# 🚀 EMA AI Agent — Technical Deep Dive

This is a heavyweight AI Agent project that combines **cutting-edge research (GraphRAG, community detection)** with **production-grade engineering (multi-channel access, asynchronous message bus)**.

---

## 🏗️ 1. Project Overview

### 🛠️ Tech Stack

- **AI Core**: `LangChain 1.2` + `LangGraph` — multi-state, cyclic agent state machines.
- **Storage & Retrieval**: `SQLite (FTS5)` for relational data and full-text search, `LanceDB / FAISS` for vector search, `LightRAG` for knowledge graph retrieval.
- **Graph Algorithms**: `igraph` + `Leiden algorithm` — community detection and multi-level summarization of memory graphs.
- **Network & Frontend**: `Robyn` (high-performance async Python web server) + `Streamlit` / `Tauri 2 + Nuxt 4` (cross-platform desktop and mobile).
- **Runtime**: `Python 3.13`, fully async with `asyncio`.

### 🎯 Capabilities

- **Long-term Evolving Memory**: Automatically distills conversation content into knowledge points, weaves them into a graph, and periodically runs community detection to self-summarize and resolve ambiguous pronouns.
- **Proactive & Scheduled Tasks**: A heartbeat mechanism lets the agent read `HEARTBEAT.md` todos during idle time and execute them autonomously, with Cron scheduling support.
- **Self-Learning Skills**: Feed it a `SKILL.md` spec sheet, and it learns to invoke new tools (search, code execution, terminal commands, etc.).
- **Multi-Channel Interaction**: Chat via web UI, send images, speak (GPT-SoVITS integration), or run as a QQ bot.

### 📈 Current Status

- **Backend Core Complete**: Dual memory engine, async message bus, Robyn server, and scheduled tasks are all operational or have core implementations.
- **Multi-Modal & Multi-Channel**: Streamlit web UI, QQ bot, voice/vision capabilities are connected.
- **[In Progress] Next-Gen Client**: A modern Tauri 2 + Nuxt 4 client (`client_future`) is under active development.

---

## 💡 2. What You'll Learn

1. **Production-Grade Agent Architecture**: Go beyond simple prompt engineering — master LangGraph for decision flow, conditional branching, and state rollback.
2. **GraphRAG (Graph-Enhanced Retrieval)**: Combine vector search, full-text search (FTS5), and graph algorithms (Leiden / PageRank) to solve the "lost in the middle" problem and memory decay in long-context LLMs.
3. **High-Concurrency Async Programming**: Deep hands-on with Python `asyncio` — learn how `asyncio.Queue` enables non-blocking background memory extraction and decoupled message buses.
4. **Multi-Platform Fusion**: Build one AI brain that adapts to Web, desktop (Tauri), and instant messaging (QQ), while handling audio and image streams.

---

## 🛠️ 3. Development Roadmap

### 🏁 Phase 1: Setup & Validation

- **Environment**: Set up **Python 3.13**, install dependencies, configure local models (Ollama) or cloud APIs (DeepSeek / OpenAI).
- **Smoke Test**: Run the `Streamlit` UI, send a few messages, verify `MesMemory` records in `SQLite`, and check the background `Skill Memory` extraction logs for errors.

### 🚀 Phase 2: Core Development & Optimization

- **Task 1: `client_future` Frontend**
  - If you know Vue / Nuxt or cross-platform development (Tauri / Rust), dive into `client_future/` to build the next-gen desktop client and connect it to the Robyn backend API.
- **Task 2: Memory Graph (Skill Memory) Tuning**
  - Audit the prompt accuracy of graph extraction. With `igraph + Leiden`, write test scripts to verify that community detection and summarization produce high-quality project summaries as conversations accumulate.
- **Task 3: Custom Skills (`SKILL.md`)**
  - Write a new `SKILL.md` (e.g., "Auto-analyze local Git repos and generate weekly reports") and test whether the agent can autonomously read and invoke it.
- **Task 4: Heartbeat & Cron Mechanism**
  - Manually write long-running tasks into `HEARTBEAT.md` (e.g., "Summarize today's conversations every hour and output a Markdown report"), then verify that the heartbeat service and subagents handle background parallel execution and async communication reliably.

---
