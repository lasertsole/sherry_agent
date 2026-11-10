# 🍊 EMA AI Agent - Sherry

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md) · [**Wiki**](https://github.com/lasertsole/sherry_agent/wiki) · [**Releases**](https://github.com/lasertsole/sherry_agent/releases/latest)

> A role-playing AI companion with a persistent persona, long-term memory, and a desktop app that can work with your files, browser and terminal.

## ✨ Meet Sherry

Sherry is not a one-off chatbot. She keeps a consistent character across conversations, remembers what matters to you, and can act on your computer when a task needs more than a reply.

The default character, **Sherry (Tachibana Sherry)**, is a self-proclaimed girl detective: cheerful and energetic on the outside, calm and sharp at the core. You can keep her as she is, rewrite her persona, or create your own character with the **Presets & Role** panel.

The app runs on your machine. Conversations, memory and settings stay in your local app-data directory; only the model and web services you configure are contacted over the network.

## 🚀 What you can do

- **Talk in character.** Long, in-world conversations with a persona that stays consistent — voice, habits, boundaries and backstory included.
- **Be remembered.** Sherry keeps long-term memory about you and your projects, recalls it when relevant, and carries it across sessions and restarts.
- **Give her skills.** Built-in skills cover coding, research, documents, images, audio, video, scheduling and more. She can also learn reusable skills from completed work, or create new ones on request.
- **Delegate real work.** For larger jobs she plans a task list, splits work into steps, and runs subagents in parallel where it helps.
- **Work with your desktop.** Read and edit project files, browse the web, run terminal commands, inspect a Git history, or sketch on a whiteboard — from the same window.
- **See, hear and speak.** Send images, audio, video or documents; Sherry can understand them, transcribe speech, and generate images.
- **Stay proactive.** Scheduled tasks and a heartbeat check let her follow up on long-running work or a daily routine without being prompted.
- **Use the chat where you are.** The desktop app is the main home; a QQ channel adapter is also available.

## 🖥️ Install

### Desktop app (recommended)

Download the latest installer for Windows, macOS or Linux from [Releases](https://github.com/lasertsole/sherry_agent/releases/latest), install it, and launch Sherry.

On first launch:

1. Choose a persona preset (or start with the default Sherry).
2. Open **Settings → Model Config**, enter your model provider, model name, API key and a context window of at least **200,000 tokens**, then save.
3. Start chatting.

The **Model Config** panel can also download local models (embedding, reranker, vision and auxiliary models) with a progress bar. Web search is optional — configure Tavily or a SearXNG instance in **Settings → Web Search** if you want it.

### Run from source

For development or a custom build you need **Python 3.13+**, **[uv](https://docs.astral.sh/uv/)** and, for the desktop client, **Node.js 20+**, **pnpm** and **Rust**.

```bash
git clone https://github.com/lasertsole/sherry_agent.git
cd sherry_agent
uv sync
./start.sh          # starts the backend on http://127.0.0.1:8080
```

To use the desktop client in development:

```bash
cd client
pnpm install
pnpm tauri dev
```

Build and packaging instructions live in the [developer guide](AGENTS.md) and the [Wiki](https://github.com/lasertsole/sherry_agent/wiki).

## 🧭 A quick tour

- **Chat**: the main conversation window. Switch sessions from the left sidebar; each session can use its own model and thinking level.
- **Memory**: open **Menu → Memory** to browse, search, edit or archive what Sherry remembers, and to tune memory limits and semantic recall.
- **Presets & Role**: edit the character's identity, personality, speech style and the role you play.
- **Task list**: for multi-step work, Sherry keeps a visible plan with progress, so you can see what is done and what is next.
- **Right sidebar**: files, Shadow Git Graph, a browser, a terminal, a whiteboard, task trajectory and other tools open as tabs beside the chat.
- **Side chat / branch**: quote a message in a side conversation, or fork a new session from an earlier answer to explore a different direction without disturbing the original.
- **Scheduled tasks**: create one-off, interval or cron tasks, and let the heartbeat check pending work on a schedule.
- **Settings**: model and network configuration, shortcuts, safety filter, desktop behavior, language and appearance.

## 🔐 Your data

Sherry runs locally. Sessions, memory, learned skills, logs and configuration are stored in the OS app-data directory, not inside the installation folder. API keys are only sent to the providers you configure. There is no Sherry account or cloud service you must sign up for.

## 📚 Documentation

This README is the user guide. The [Wiki](https://github.com/lasertsole/sherry_agent/wiki) holds the technical documentation — architecture, memory engine, task orchestration, sandboxing, security model and more. Developers and contributors can start with [AGENTS.md](AGENTS.md) and the [client README](client/README.md).

## 🤝 Contributing

Issues and pull requests are welcome. If you want to add a skill, create a folder with a `SKILL.md` under `skills/builtin/`, `skills/auto/` or `skills/plugins/`; the running agent can also generate one for you with its built-in `skill_creator` skill. Third-party skills stay inactive until you explicitly enable them.

## 📄 License

MIT License.

---

Contact: QQ 3132225629

> **💡 Tip**: This project is inspired by the exploration of advanced AI agents and deep role-playing.
