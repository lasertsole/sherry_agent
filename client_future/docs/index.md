---
layout: home
hero:
  name: "EMA AI Agent"
  text: "Backend API Documentation"
  tagline: Tauri IPC command reference for frontend developers
  actions:
    - theme: brand
      text: Getting Started
      link: /guide/getting-started
    - theme: alt
      text: Commands
      link: /commands/agent

features:
  - title: 12 IPC Commands
    details: Agent chat/stop, session management, system prompts, character config, and system utilities. All proxied to the Python backend via HTTP bridge.
  - title: Streaming Events
    details: Real-time agent responses via Tauri events. SSE from Python backend is forwarded as 4 Tauri event types.
  - title: TypeScript Types
    details: Auto-generated .d.ts files from Rust types via ts-rs for compile-time safety.
  - title: Hybrid Architecture
    details: Tauri/Rust HTTP bridge proxies all requests to the Python backend (LangGraph, RAG, TTS). Zero business logic in Rust.
  - title: Desktop Features
    details: System tray, Alt+Space global shortcut, single-instance lock, window state persistence.
  - title: Error Handling
    details: 12 structured error codes (including BACKEND_ERROR) with retryability hints and frontend-friendly messages.
---
