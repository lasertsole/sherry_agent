# client_future

## Overview

`client_future` is the next-generation frontend for the EMA AI Agent, designed as a **streaming SPA desktop client** that will gradually replace the existing Python/Streamlit frontend (`client/` directory).

The current `client/` is a conversational Web UI built with Python 3.10 + Streamlit. While functionally complete, it is constrained by Streamlit's script-re-run rendering model, which limits interactivity smoothness, state management, and cross-platform desktop capabilities. `client_future` rebuilds the frontend from the ground up with **Tauri 2 + Nuxt 4**, aiming to deliver:

- **Smoother interactions** — Vue 3 reactive partial updates instead of full-page re-runs
- **Offline-first** — Dexie.js (IndexedDB) for caching conversation history locally
- **Native desktop capabilities** — Tauri 2 provides system tray, global shortcuts (Alt+Space), file system access, and other features impossible in Streamlit
- **Component-driven architecture** — Vue 3 Composition API + Pinia for scalable team collaboration

> **Development status**: Active development; core chat UI and Tauri IPC bridge are functional.

---

## Architecture

### Hybrid Architecture

```
+--------------------------------------------------------------------+
|                    Tauri 2 Desktop App (client_future/)              |
|                                                                    |
|  +-------------------+       invoke()       +--------------------+ |
|  |  Nuxt 4 Frontend  | ===================> |  Rust Backend      | |
|  |  (app/)           |                      |  (src-tauri/src/)  | |
|  |                   | <=================== |                    | |
|  |  bridge.ts        |   Tauri Events       |  commands/         | |
|  |  (dual-mode)      |   (streaming)        |  services/         | |
|  +-------------------+                      |  core/              | |
|                                             |  utils/             | |
|                                             +---------+----------+ |
|                                                       |            |
+-------------------------------------------------------|------------+
                                                         |
                                           reqwest HTTP (localhost:8080)
                                           SSE stream / JSON REST
                                                         |
+-------------------------------------------------------|------------+
|                    Python Backend (server/)             |            |
|                                                        v           |
|  Robyn HTTP + SSE + WebSocket                                      |
|  Agent Core (LangGraph) | RAG | Multi-channel     |
+--------------------------------------------------------------------+
```

**Rust implementation**: The Rust layer implements the following functionality:
1. Receives Tauri IPC calls from the frontend
2. Forwards them as HTTP requests to the Python backend (`http://127.0.0.1:8080`)
3. Converts SSE streams from Python into Tauri Events for real-time frontend updates
4. Manages Python backend process lifecycle (optional auto-spawn via `EMA_AUTO_START_BACKEND`)
5. Provides system tray (Show/Hide/Quit) and global shortcut (Alt+Space toggle window)

### Data Flow

```
User interaction (Vue component)
    -> bridge.ts (auto-detect Tauri vs browser mode)
        |
        |--> [Tauri mode] invoke() -> Rust IPC command
        |        -> PythonBridge (reqwest HTTP) -> Python backend
        |        -> SSE stream -> Tauri Events -> frontend listener
        |
        |--> [Browser mode] fetchApi() -> Python backend (direct SSE/REST)
        |
    -> Pinia Store (state update)
    -> Reactive UI update
```

---

## Directory Structure

```
client_future/
├── .env.example                   # Environment variable template
├── .gitignore                     # Git ignore rules
├── eslint.config.mjs              # ESLint flat config
├── nuxt.config.ts                 # Nuxt 4 configuration (SSR=off, Vite, Tailwind, i18n, PrimeVue, color-mode)
├── package.json                   # Dependency manifest (pnpm workspace root)
├── pnpm-lock.yaml                 # pnpm lockfile
├── pnpm-workspace.yaml            # pnpm workspace definition
├── prettier.config.mjs            # Prettier code formatter config
├── tailwind.config.js             # Tailwind configuration
├── tsconfig.json                  # TypeScript configuration
├── app/                           # Nuxt 4 SPA source
│   ├── app.vue                    # Root component entry
│   ├── common.scss                # Global SCSS mixin library (layout, shapes, scrollbar, etc.)
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css           # Global CSS reset + CSS variables
│   │   │   ├── main.scss          # (reserved)
│   │   │   └── tailwind.scss      # Tailwind directives (@tailwind base/components/utilities)
│   │   ├── images/                # (reserved) Static images
│   │   └── ts/
│   │       └── tailwind.config.ts # Tailwind custom tokens (width, height, z-index utilities)
│   ├── common/
│   │   └── utils.ts               # Shared utilities (formatCompactTimeString via dayjs)
│   ├── components/
│   │   └── chat/
│   │       └── inputBox.vue       # Chat input box component (i18n-aware)
│   ├── composables/               # Vue 3 composable logic
│   │   ├── bridge.ts              # Unified Tauri/Browser communication bridge
│   │   ├── messages.ts            # Message API: history, clear session, SSE streaming
│   │   ├── requestApi.ts          # HTTP request wrapper (useFetch + retry logic)
│   │   ├── system.ts              # (reserved) System composable
│   │   ├── utils.ts               # Date/time utilities (comparison, formatting, UTC conversion)
│   │   ├── workspace.ts           # System prompt & character CRUD operations
│   │   ├── ws.ts                  # WebSocket singleton with auto-reconnect (5s)
│   │   └── mitt.ts                # mitt event bus instance
│   ├── declare/
│   │   └── declarations.d.ts      # Type declarations
│   ├── i18n/
│   │   └── locales/
│   │       ├── en.json            # English translations
│   │       └── zh.json            # Chinese translations
│   ├── layouts/
│   │   └── default.vue            # Default layout — Nuxt 4 layout entry
│   ├── pages/
│   │   ├── index.vue              # Root page (redirects to /home)
│   │   └── home/
│   │       ├── index.vue          # Main chat page — sidebar + chat area
│   │       ├── config.ts          # Toolbar & header tool configurations
│   │       ├── type.ts            # Session/Message/ChatRole type definitions
│   │       └── components/
│   │           ├── ChatBox.vue    # Message list with markdown rendering & XSS sanitization
│   │           ├── HistoryItem.vue# Sidebar history session item
│   │           └── ModeSwitch.vue # Dark/Light mode toggle (PrimeVue ToggleSwitch)
│   └── types/
│       ├── message.ts             # Message type definitions (BaseMessage, AiMessage, MultiModalMessage, etc.)
│       └── response.d.ts          # API response type definitions
├── src-tauri/                     # Tauri 2 native shell
│   ├── capabilities/
│   │   └── default.json           # Permission config (currently core:default only)
│   ├── icons/                     # App icons
│   ├── src/
│   │   ├── lib.rs                 # Tauri app entry — Builder setup, tray menu, global shortcut, Python process manager
│   │   ├── main.rs                # Windows subsystem entry + calls lib::run()
│   │   ├── commands/              # Tauri IPC command handlers
│   │   │   ├── mod.rs
│   │   │   ├── agent.rs           # agent_chat, agent_stop
│   │   │   ├── character.rs       # character_read/write/update
│   │   │   ├── events.rs          # Event type definitions
│   │   │   ├── session.rs         # session_clear, session_history
│   │   │   ├── system.rs          # system_info, system_health
│   │   │   └── system_prompt.rs   # system_prompt_read/write/update
│   │   ├── services/
│   │   │   ├── mod.rs
│   │   │   ├── python_bridge.rs   # HTTP bridge to Python backend (reqwest + SSE → Tauri Events)
│   │   │   └── python_process.rs  # Python backend process lifecycle manager
│   │   ├── core/                  # Core domain modules (stub/placeholder)
│   │   │   ├── mod.rs
│   │   │   ├── agent/
│   │   │   ├── bus/
│   │   │   ├── channel/
│   │   │   ├── cron/
│   │   │   ├── heartbeat/
│   │   │   ├── memory/
│   │   │   └── subagent/
│   │   ├── utils/
│   │   │   ├── mod.rs
│   │   │   ├── config.rs          # AppConfig (from environment variables)
│   │   │   ├── error.rs           # Error types
│   │   │   └── logger.rs          # Tracing setup
│   │   ├── config/
│   │   ├── database/
│   │   ├── models/
│   │   ├── prompts/
│   │   ├── rag/
│   │   ├── runtime/
│   │   ├── sessions/
│   │   ├── skills/
│   │   ├── tools/
│   │   └── types/
│   ├── Cargo.toml                 # Rust dependencies (tauri 2, serde, reqwest, tracing, ts-rs, etc.)
│   ├── tauri.conf.json            # Tauri 2 config — app name "EMA AI Agent", identifier "com.ema-ai.agent"
│   └── build.rs                   # Tauri build script
└── public/                        # Nuxt public static assets
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Cross-platform shell** | [Tauri 2](https://v2.tauri.app/) | Packages the web frontend as a native desktop app with system API access |
| **Frontend framework** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA mode (`ssr: false`), Composition API + `<script setup lang="ts">` |
| **UI components** | [PrimeVue 4](https://primevue.org/) + [PrimeIcons](https://primevue.org/icons) | Pre-built UI components (Button, Checkbox, Menu, ToggleSwitch, etc.) |
| **State management** | [Pinia](https://pinia.vuejs.org/) + [pinia-plugin-persistedstate](https://prazdevs.github.io/pinia-plugin-persistedstate/) | Global state + persistence |
| **Styling** | [Tailwind CSS](https://tailwindcss.com/) (via `@nuxtjs/tailwindcss`) + SCSS | Utility-first CSS + custom mixin library |
| **Color mode** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | Dark/Light theme switching |
| **Internationalization** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) | Chinese (default) / English |
| **Markdown rendering** | [markdown-it](https://github.com/markdown-it/markdown-it) | Chat message markdown → HTML |
| **XSS protection** | [DOMPurify](https://github.com/cure53/DOMPurify) | Sanitize HTML output |
| **Date formatting** | [dayjs](https://day.js.org/) | Parse/format compact timestamps (YYYYMMDDHHmmss) |
| **Offline storage** | [Dexie.js](https://dexie.org/) | IndexedDB wrapper for caching conversation history |
| **Event bus** | [mitt](https://github.com/developit/mitt) | Lightweight component communication |
| **Utilities** | [lodash-es](https://lodash.com/) | Deep clone, deduplication, and other common functions |
| **Build tool** | [Vite](https://vitejs.dev/) | Dev server + production builds |
| **Backend language** | [Rust](https://www.rust-lang.org/) 2021 edition (MSRV 1.94) | Tauri native logic |
| **Logging** | [tracing](https://docs.rs/tracing/) + [tauri-plugin-tracing](https://github.com/tauri-apps/tauri-plugin-tracing) | Structured logging (Tauri backend) |
| **Type generation** | [ts-rs](https://github.com/Aleph-Alpha/ts-rs) | Auto-generate TypeScript types from Rust structs |

### Key Configuration

- **Nuxt**: `ssr: false` (pure SPA); `pages/` directory structure; Vite config with `VITE_*` and `TAURI_*` environment variable prefix allowlist; route `/` redirects to `/home`
- **Tauri**: App identifier `com.ema-ai.agent`, product name "EMA AI Agent", dev URL `http://localhost:3000`, CSP set to `null` to allow inline styles, window 800×600 resizable
- **Tailwind**: Loaded via `@nuxtjs/tailwindcss` module, config at `app/assets/ts/tailwind.config.ts` and root `tailwind.config.js`, provides custom `w-*`/`h-*`/`z-*` utility classes
- **i18n**: Default locale `zh`, strategy `prefix_except_default`
- **PrimeVue**: Noir preset (slate color palette), dark mode via `.dark` CSS class selector

---

## Architecture

### Relationship with client/

```
Current state:  client/ (Python+Streamlit) — production
                    │
                    ├── Feature-complete but Streamlit-render-limited
                    ├── Web-only, no desktop capabilities
                    └── Weak state management
                         │
               client_future/ (Tauri+Nuxt+Vue) — in development
                         │
                         ├── Streaming SPA, partial refresh
                         ├── Native desktop app (Tauri 2)
                         ├── System tray + global shortcut (Alt+Space)
                         ├── Offline cache (Dexie/IndexedDB)
                         ├── WebSocket real-time updates
                         ├── Markdown rendering + XSS sanitization
                         ├── Dark/Light mode + i18n
                         └── Componentized Pinia state management
```

`client_future` will **not** replace `client/` overnight. Migration happens module by module, and eventually `client/` will be deprecated.

### Component Hierarchy

```
app.vue (root)
  └─ NuxtLayout (default.vue)
       └─ NuxtPage
            ├─ / (redirect → /home)
            └─ /home (home/index.vue)
                 ├─ HistoryItem (sidebar session list)
                 ├─ ModeSwitch (dark/light toggle)
                 └─ ChatBox (message area)
                      └─ Markdown rendering (markdown-it + DOMPurify)
```

### Communication Bridge (bridge.ts)

The `bridge.ts` composable provides a unified API that works in both Tauri desktop and browser modes:

| API | Description |
|-----|-------------|
| `sendChatMessage(request, onChunk)` | Streaming agent chat (Tauri Events / SSE) |
| `stopChatMessage(sessionId)` | Stop ongoing generation |
| `clearSession(sessionId)` | Clear session state |
| `getHistory(sessionId, lastTurnCount)` | Retrieve conversation history |
| `readSystemPrompt()` | Read all system prompt files |
| `writeSystemPrompt(fileToContent)` | Overwrite system prompt files |
| `updateSystemPrompt(fileToContent)` | Merge-update system prompt files |
| `readCharacter()` | Read character configuration |
| `writeCharacter(data)` | Overwrite character configuration |
| `updateCharacter(data)` | Merge-update character configuration |
| `checkHealth()` | Check Python backend reachability |

### WebSocket (ws.ts)

WebSocket singleton for real-time server push notifications:

- Connects to `{wsBase}/sessions/ws?session_id=main`
- Auto-reconnects on disconnect (5-second delay)
- Emits events via mitt: `ws:connected`, `ws:disconnected`, `ws:notification`, `ws:message`
- Resolves WS URL from `VITE_API_BACK_URL` environment variable

---

## Core Module Details

### SCSS Mixin Library (`common.scss`)

A 300+ line SCSS mixin library providing utilities for layout, shapes, scrollbars, and text overflow:
- Size constraints: `minWidth` / `maxWidth` / `fixedWidth` / `fullWidth`, etc.
- Shapes: `fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule`, etc.
- Layout: `flexCenter` / `scrollBar` / `wordEllipsis`, etc.
- Images: `imgFullInParent` / `fullImg`, etc.

### Tauri Backend (`src-tauri/`)

- **lib.rs**: `tauri::Builder` startup, system tray (Show/Hide/Quit), global shortcut (Alt+Space toggle), Python process manager (auto-spawn via `EMA_AUTO_START_BACKEND`)
- **main.rs**: Windows subsystem entry, `#![windows_subsystem = "windows"]` hides the console window in release builds
- **tauri.conf.json**: App identifier `com.ema-ai.agent`, product name "EMA AI Agent", build command `pnpm build`, dev URL `http://localhost:3000`
- **Cargo.toml**: Rust dependencies — tauri 2.x (tray-icon feature), serde + serde_json, reqwest (rustls-tls, streaming), tracing + tauri-plugin-tracing, ts-rs, thiserror, anyhow, tokio, uuid, plus plugins: shell, notification, global-shortcut, single-instance, window-state

### Rust Module Structure

```
src-tauri/src/
├── commands/          # IPC command handlers
│   ├── agent.rs       # agent_chat, agent_stop
│   ├── character.rs   # character_read/write/update
│   ├── session.rs     # session_clear, session_history
│   ├── system.rs      # system_info, system_health
│   └── system_prompt.rs
├── services/
│   ├── python_bridge.rs   # HTTP bridge (reqwest + SSE → Tauri Events)
│   └── python_process.rs  # Python backend process manager
├── core/              # Domain modules (stubs)
├── utils/
│   ├── config.rs      # AppConfig from env vars
│   ├── error.rs       # Error types
│   └── logger.rs      # Tracing setup
├── config/
├── database/
├── models/
├── prompts/
├── rag/
├── runtime/
├── sessions/
├── skills/
├── tools/
└── types/
```

---

## Comparison with client/

| Dimension | client/ | client_future |
|-----------|---------|---------------|
| Language | Python 3.10 | TypeScript / Rust |
| Framework | Streamlit | Nuxt 4 + Vue 3 |
| UI Library | Streamlit native | PrimeVue 4 + Tailwind CSS |
| Rendering | Full-page re-run | Reactive partial update |
| Desktop support | None (Web only) | Tauri 2 native desktop |
| Storage | Python in-memory (ChatStorage) | Dexie.js IndexedDB |
| State management | Streamlit Session State | Pinia + persistence plugin |
| Markdown | N/A | markdown-it + DOMPurify |
| Visualization | Streamlit native charts | D3.js SVG |
| Events | N/A | mitt event bus + WebSocket |
| i18n | N/A | @nuxtjs/i18n (zh/en) |
| Dark mode | N/A | @nuxtjs/color-mode |
| Backend language | Python (FastAPI/WebSocket) | — (calls same backend API) |

---

## Development Guide

### Prerequisites

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/) (recommended) or npm
- [Rust](https://www.rust-lang.org/) >= 1.94 (MSRV)
- [Tauri CLI v2](https://v2.tauri.app/start/cli/)

### Common Commands

```bash
# Install dependencies
pnpm install

# Dev mode (Web browser)
pnpm dev

# Tauri desktop dev mode
pnpm tauri dev

# Production build
pnpm build

# Tauri desktop build
pnpm tauri build

# Rust compilation check
cd src-tauri && cargo check

# Rust tests
cd src-tauri && cargo test
```

### Environment Variables

```bash
# .env.example
VITE_API_BACK_URL=http://localhost:8080  # Python backend URL
VITE_APP_NAME=EMA AI Agent               # App display name
EMA_PROJECT_ROOT=..                       # Project root for Python backend auto-spawn
EMA_AUTO_START_BACKEND=true               # Auto-start Python backend with Tauri app
```

### Adding New Pages / Components

1. Create a `.vue` file under `app/pages/` — Nuxt 4 auto-registers the route
2. Create components under `app/components/` — auto-available globally
3. Create composable logic under `app/composables/`
4. Add custom tokens to `app/assets/ts/tailwind.config.ts`
5. Add i18n keys to `app/i18n/locales/zh.json` and `en.json`

### Starting the Python Backend

```bash
# From the project root (EMA_AI_agent/)
python -m server
```

The backend starts on `http://127.0.0.1:8080` by default (configurable via `VITE_API_BACK_URL`).

### Adding New IPC Commands

1. Define request/response types in `src-tauri/src/commands/<module>.rs` with `#[derive(TS)]`
2. Implement the `#[tauri::command]` function using `PythonBridge` methods
3. Register the command in `lib.rs` under `.invoke_handler(tauri::generate_handler![...])`
4. Run `cargo test` to regenerate TypeScript types in `app/types/backend/`
5. Add the corresponding wrapper in `app/composables/bridge.ts`

---

## License

Same as the EMA AI Agent main project license.
