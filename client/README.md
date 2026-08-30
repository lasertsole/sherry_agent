# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## Overview

`client` is the frontend of the EMA AI Agent, a **streaming SPA chat client** that talks to the Python backend in [server/](../server/) (Robyn, `http://127.0.0.1:8080` by default).

Built with **Tauri 2 + Nuxt 4 (Vue 3 + TypeScript)**:

- **Streaming chat** — agent responses stream over WebSockets (`/sessions/agent/ws`) with typed chunks (`text` / `reasoning` / `tool_start` / `tool_end` / `tool_result`) and HITL (human-in-the-loop) approval cards
- **Offline-first history** — Dexie.js (IndexedDB) caches conversation history, subagent run records, character profiles, and the chat background image; history requests are cache-first and fetch only missing turns
- **Rich tooling UI** — skill manager, cron jobs, heartbeat editor, channel settings, log viewer (server + client logs), statistics charts, subagent flow graph, knowledge-graph viewer
- **Dark/Light mode + i18n** — `@nuxtjs/color-mode` theming and 4 locales (`zh` / `en` / `ja` / `ko`)
- **Dual-mode bridge** — `bridge.ts` auto-detects Tauri desktop vs plain browser and picks the transport accordingly

> **Development status**: Active development. The browser code path (direct HTTP/WS to the Python backend) is fully implemented; the Rust side of `src-tauri/` currently contains only placeholder modules (see [Architecture](#architecture)).

---

## Architecture

### Hybrid Architecture

```text
+--------------------------------------------------------------+
|                Frontend (Nuxt 4 SPA, app/)                   |
|  Vue 3 components + composables + Pinia + mitt event bus     |
+---------------------------+----------------------------------+
                            |  bridge.ts (runtime auto-detect)
              +-------------+--------------+
              |                            |
   [Browser mode]                [Tauri mode]
   ofetch HTTP REST +            invoke() IPC commands
   native WebSocket              + Tauri Events (agent:stream:*)
              |                            |
              |                 src-tauri/ (Rust shell, currently
              |                 placeholder modules only)
              |                            |
              +-------------+--------------+
                            |
              HTTP / WS  http://localhost:8080
              (VITE_API_BACK_URL, no dev proxy)
                            |
+---------------------------v----------------------------------+
|              Python Backend (server/, Robyn)                 |
|  REST endpoints + WebSocket endpoints                        |
|  Agent Core (LangGraph) | Memory | Skills | Cron | Channels  |
+--------------------------------------------------------------+
```

### Data Flow

```text
User interaction (Vue component)
    -> bridge.ts (auto-detect Tauri vs browser mode)
        |
        |--> [Browser mode] fetchApi() REST + WebSocket /sessions/agent/ws
        |        (base64 media uploaded first via POST /images|/audio|/video/upload)
        |
        |--> [Tauri mode] invoke() -> Rust IPC command -> Tauri Events
        |
    -> Reactive state (composables + Pinia + mitt event bus)
    -> Reactive UI update
```

**Note on the Tauri mode**: `bridge.ts` still contains the full Tauri IPC code path (`agent_chat`, `agent_stop`, `session_clear`, `session_history`, `system_prompt_*`, `memory_*`, `system_health`, `subagent_runs`, `subagent_run_delete`) mirroring `app/types/backend/*` (ts-rs generated). However, `src-tauri/src/` currently contains only **empty placeholder modules** (`config/`, `core/`, `database/`, `prompts/`, `rag/`, `runtime/`, `sessions/`, `skills/`, `tools/`, `types/` — each an empty `mod.rs`); the Rust entry point and IPC command implementations are not present in the current tree. Several flows (heartbeat, cron, skills, channels, curator, logs, `/env`, subagent steering) bypass Rust entirely and always go through `fetchApi` in both modes.

---

## Directory Structure

```text
client/
├── .env.example                   # Environment variable template (VITE_APP_NAME, VITE_API_BACK_URL)
├── eslint.config.mjs              # ESLint flat config
├── nuxt.config.ts                 # Nuxt 4 config (SPA, Tailwind v4, i18n, PrimeVue, Pinia, color-mode)
├── package.json                   # Dependency manifest (scripts: dev/build/generate/preview/typecheck/test/...)
├── playwright.config.ts           # Playwright repro config (./repros, Desktop Edge, localhost:3000)
├── pnpm-lock.yaml                 # pnpm lockfile
├── pnpm-workspace.yaml            # pnpm workspace config (allowBuilds)
├── prettier.config.mjs            # Prettier config
├── tsconfig.json                  # TypeScript configuration
├── vitest.config.ts               # Vitest unit-test config (composables, happy-dom, v8 coverage)
├── vitest.integration.config.ts   # Vitest integration-test config (real .vue SFC, mocked backend)
├── app/                           # Nuxt 4 SPA source
│   ├── app.vue                    # Root component — layout, toast layer, connection banner, locale restore
│   ├── common.scss                # 300+ line global SCSS mixin library (layout, shapes, scrollbar, ...)
│   ├── assets/css/
│   │   ├── main.css               # Tailwind v4 entry (@import 'tailwindcss') + @theme tokens + reset
│   │   └── main.scss              # Global SCSS entry (loaded in nuxt.config css)
│   ├── common/utils.ts            # Shared utilities (dayjs setup, formatCompactTimeString, ...)
│   ├── components/
│   │   ├── chat/inputBox.vue      # Chat input box component (i18n-aware)
│   │   └── ImagePreviewOverlay.vue# Full-screen image preview overlay (Teleport to body)
│   ├── composables/               # Vue 3 composable logic (+ unit tests under __tests__/)
│   │   ├── bridge.ts              # Unified Tauri/Browser bridge — chat streaming, sessions, prompts,
│   │   │                          #   memory, heartbeat, cron, skills, channels, curator, logs, env
│   │   ├── requestApi.ts          # fetchApi HTTP wrapper (ofetch, baseURL from VITE_API_BACK_URL)
│   │   ├── ws.ts                  # WebSocket singletons: /sessions/ws + /subagents/ws (5s auto-reconnect)
│   │   ├── db.ts                  # Dexie (IndexedDB) cache: messages, character profiles, background, runs
│   │   ├── messages.ts            # History API (cache-first /get_history_by_turn_page) + stream abort event
│   │   ├── connection.ts          # Backend health polling (5s) + online/offline toasts
│   │   ├── toast.ts               # Global toast layer (PrimeVue Toast registration)
│   │   ├── clientLog.ts           # Client-side console.* capture persisted to Dexie (all/log/error)
│   │   ├── env.ts                 # Read/write backend .env (GET/PUT /env)
│   │   ├── workspace.ts           # System prompt handlers (/system_prompt GET/POST/PATCH)
│   │   ├── defaultCharacter.ts    # Built-in default character (names + /avatar/*.jpg)
│   │   ├── sessionFilter.ts       # Client-side session list filter (keyword + date range)
│   │   ├── useChatBackground.ts   # Global chat background image (Dexie-persisted singleton)
│   │   ├── useImagePreview.ts     # Image preview overlay state
│   │   ├── useSubagentTasks.ts    # Background-task state singleton (fetch + WS + Dexie)
│   │   ├── utils.ts               # max/min + date/time utilities
│   │   ├── mitt.ts                # mitt event bus instance
│   │   └── system.ts              # (empty placeholder)
│   ├── declare/declarations.d.ts  # Type declarations
│   ├── i18n/locales/              # en.json / ja.json / ko.json / zh.json
│   ├── layouts/default.vue        # Default layout — full-view wrapper
│   ├── pages/
│   │   ├── index.vue              # Renders ChatInputBox (route / redirects to /home via routeRules)
│   │   ├── knowledge-graph/
│   │   │   └── index.vue          # Knowledge graph viewer (@antv/g6, doc upload, under development)
│   │   └── home/
│   │       ├── index.vue          # Main chat shell — SessionSidebar + toolbar + nested NuxtPage
│   │       ├── config.ts          # Toolbar (image/audio/video upload) & header tool definitions
│   │       ├── type.ts            # SessionRecord / Tool / MessageItem type definitions
│   │       ├── index/[sid].vue    # Per-session chat page (KeepAlive, HITL card, task jump bar)
│   │       ├── index/tasks/[sid].vue  # Standalone background-tasks page (/home/tasks/{sid})
│   │       └── components/        # 20 page components:
│   │           ├── ChatBox.vue            # Message list (markdown-it + DOMPurify, media via /media)
│   │           ├── SessionSidebar.vue     # Session list sidebar (create/rename/filter sessions)
│   │           ├── HistoryItem.vue        # Sidebar history session item
│   │           ├── ModeSwitch.vue         # Dark/Light toggle (PrimeVue ToggleSwitch)
│   │           ├── ExtendDialog.vue       # "Extend" dialog
│   │           ├── ConfigDialog.vue       # System config (.env editor, background, language, ...)
│   │           ├── PersonaDialog.vue      # System prompt / persona editor
│   │           ├── MemoryDialog.vue       # Long-term memory editor (workspace/memory/*)
│   │           ├── HeartbeatDialog.vue    # HEARTBEAT.md editor
│   │           ├── CronDialog.vue         # Cron job management (/cron)
│   │           ├── SkillsDialog.vue       # Skill manager (list/upload/toggle/pin/delete/curator)
│   │           ├── ChannelSettingsDialog.vue  # Channel toggles & per-channel config
│   │           ├── LogsDialog.vue         # Log viewer (server logs + client logs, live stream)
│   │           ├── NotificationDialog.vue # Server-push notification list
│   │           ├── StatsDialog.vue        # Usage statistics (@antv/g2 via GChart.vue)
│   │           ├── GChart.vue             # @antv/g2 chart wrapper
│   │           ├── SubagentTasksView.vue  # Background tasks view (list/detail/flow graph)
│   │           ├── SubagentRunDetail.vue  # Single subagent run detail
│   │           ├── SubagentFlowGraph.vue  # Subagent run tree graph (@antv/g6)
│   │           └── AvatarCropDialog.vue   # Avatar upload + crop (cropperjs)
│   ├── stores/ui.ts               # Pinia UI store (sidebarCollapsed persisted to localStorage)
│   └── types/
│       ├── message.ts             # BaseMessage / AiMessage / MultiModalMessage, ...
│       ├── response.d.ts          # API response type definitions
│       └── backend/               # ts-rs generated backend types (ChatRequest, HealthStatus, ...)
├── docs/                          # VitePress documentation site (guide/commands/events/types/zh)
├── public/                        # Static assets (favicon.svg, robots.txt, avatar/)
├── repros/                        # Playwright repro tests (run via playwright.config.ts)
├── src-tauri/                     # Tauri 2 native shell
│   ├── capabilities/default.json  # Permissions (core:default, shell:*, notification,
│   │                              #   global-shortcut:default, window-state:default)
│   ├── icons/                     # App icons
│   ├── resources/                 # Bundled resources placeholder (skills/, templates/)
│   ├── src/                       # Empty placeholder modules only: config/ core/ database/
│   │                              #   prompts/ rag/ runtime/ sessions/ skills/ tools/ types/
│   ├── tests/                     # Rust test placeholders (empty mod.rs + .gitkeep dirs)
│   ├── benches/                   # Benchmark placeholder
│   ├── .env.example               # Backend model config template (MAIN_LLM_*, TAVILY_API_KEY, ...)
│   ├── Cargo.toml                 # Rust manifest (tauri 2, reqwest, ts-rs, tracing, plugins, ...)
│   ├── Cargo.lock                 # Rust lockfile
│   ├── build.rs                   # Tauri build script
│   └── tauri.conf.json            # Tauri 2 config (beforeDev: pnpm dev, beforeBuild: pnpm build)
└── tests/integration/             # Vitest integration tests (real SFC, mocked backend)
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Cross-platform shell** | [Tauri 2](https://v2.tauri.app/) (`2.0.0-rc.17`, `@tauri-apps/api` ^2.11.1, `@tauri-apps/cli` 2.11.4) | Native desktop packaging + config/capabilities/icons |
| **Frontend framework** | [Nuxt 4](https://nuxt.com/) ^4.5.2 + [Vue 3](https://vuejs.org/) ^3.5.41 | SPA mode (`ssr: false`), Composition API + `<script setup lang="ts">` |
| **UI components** | [PrimeVue](https://primevue.org/) ^4.5.0 + PrimeIcons ^8.0.0 (`@primevue/nuxt-module`) | Dialog, Button, Select, ToggleSwitch, Toast, etc. |
| **State management** | [Pinia](https://pinia.vuejs.org/) ^4.0.3 (`@pinia/nuxt`) + `pinia-plugin-persistedstate` (localStorage) | Global UI state (sidebar, theme entry) |
| **Styling** | [Tailwind CSS](https://tailwindcss.com/) v4 via `@tailwindcss/vite` + SCSS (`sass`) | Utility-first CSS + `@theme` tokens + SCSS mixin library |
| **Color mode** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | Dark/Light theme switching (`.dark` class) |
| **Internationalization** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) 10.6.0 | zh / en / ja / ko, `no_prefix` strategy |
| **Markdown rendering** | [markdown-it](https://github.com/markdown-it/markdown-it) ^15 | Chat message markdown → HTML (ChatBox.vue) |
| **XSS protection** | [DOMPurify](https://github.com/cure53/DOMPurify) ^3.4 | Sanitize rendered HTML |
| **Date formatting** | [dayjs](https://day.js.org/) | Compact timestamps (`YYYYMMDDHHmmss`) parsing/formatting |
| **Offline storage** | [Dexie.js](https://dexie.org/) ^4.4.4 | IndexedDB wrapper: messages, characters, background, subagent runs, client logs |
| **Event bus** | [mitt](https://github.com/developit/mitt) ^3 | Lightweight cross-component communication |
| **Charts / graphs** | [@antv/g2](https://g2.antv.antgroup.com/) ^5, [@antv/g6](https://g6.antv.antgroup.com/) ^5 | Statistics charts, subagent flow graph, knowledge graph |
| **Image cropping** | [cropperjs](https://github.com/fengyuanchen/cropperjs) ^1.6 | Avatar upload & crop |
| **Utilities** | [lodash-es](https://lodash.com/) ^4.18 | Common utility functions |
| **Unit / integration tests** | [Vitest](https://vitest.dev/) ^4 + happy-dom + @vue/test-utils + @vitest/coverage-v8 | Composable unit tests + SFC integration tests |
| **E2E repros** | [@playwright/test](https://playwright.dev/) ^1.62 | Repro tests against the dev server (Desktop Edge) |
| **Lint / format** | ESLint ^10 (flat config) + Prettier | Code quality |
| **Type checking** | [vue-tsc](https://github.com/vuejs/language-tools) ^3.3.9 | `pnpm typecheck` |
| **Backend language** | [Rust](https://www.rust-lang.org/) 2021 edition (MSRV 1.94) | Tauri shell (src-tauri/, currently placeholder modules) |

### Key Configuration

- **Nuxt** (`nuxt.config.ts`): `ssr: false` (pure SPA); `devtools` disabled; Vite with `clearScreen: false`, `envPrefix: ['VITE_', 'TAURI_']`, `server.strictPort: true` (Tauri requires a consistent port); CSS entries `~/assets/css/main.css` + `~/assets/css/main.scss`; route rule `/` → 301 redirect to `/home`; `src-tauri/` excluded from scanning
- **Tauri** (`tauri.conf.json`): product name "EMA AI Agent", version 0.1.0, identifier `com.ema-ai.agent`, `beforeDevCommand: pnpm dev`, `beforeBuildCommand: pnpm build`, dev URL `http://localhost:3000`, frontendDist `../dist`, CSP `null`, main window 800×600 resizable
- **Tailwind** (v4): loaded via `@tailwindcss/vite`; entry `app/assets/css/main.css` imports `tailwindcss` + PrimeIcons; `@custom-variant dark` triggered by the `.dark` class; custom `@theme` tokens (breakpoints 480/768/976/1440, colors `gray-dark`/`gray-light`/`theme-main`, Merriweather serif, z-index 1–3)
- **i18n**: strategy `no_prefix`, `defaultLocale: 'en'`, locales `zh`/`en`/`ja`/`ko`, `detectBrowserLanguage: false`; `app.vue` restores the locale on mount (preference cookie `i18n_redirected` → browser language → fallback `en`)
- **PrimeVue**: custom `NoirPreset` derived from the Aura preset with a slate primary palette; dark mode via the `.dark` selector
- **Pinia persistence**: `pinia-plugin-persistedstate` configured globally with `storage: 'localStorage'`

---

## Frontend Architecture

### Component Hierarchy

```text
app.vue (root: toast layer, connection banner, locale restore)
  └─ NuxtLayout (layouts/default.vue)
       └─ NuxtPage
            ├─ /            → 301 redirect to /home (routeRules)
            ├─ /knowledge-graph  (knowledge-graph/index.vue, @antv/g6)
            └─ /home (home/index.vue: SessionSidebar + toolbar)
                 └─ NuxtPage (page-key = route.params.sid, KeepAlive)
                      ├─ /home/{sid}       (index/[sid].vue — chat + HITL card)
                      └─ /home/tasks/{sid} (index/tasks/[sid].vue — SubagentTasksView)
```

### Communication Bridge (bridge.ts)

`bridge.ts` provides a unified API that works in both Tauri desktop and browser modes. All backend access flows through it (or through `fetchApi` in `requestApi.ts`):

| API | Description |
|-----|-------------|
| `streamChatMessage(request, onChunk, onHitl?, onDone?)` | Streaming agent chat; returns `{ controller, promise }` (Tauri Events or `/sessions/agent/ws`) |
| `sendChatMessage(request, onChunk)` | Convenience wrapper around `streamChatMessage` |
| `stopChatMessage(sessionId)` | Stop ongoing generation (`agent_stop` IPC or WS `stop` frame) |
| `resumeHitl(sessionId, decision, ...)` | Resume a paused HITL agent over a fresh WebSocket |
| `clearSession(sessionId)` | Clear session state (`session_clear` IPC or `DELETE /sessions`) |
| `getHistory(sessionId, lastTurnCount)` | Retrieve history (`session_history` IPC or `GET /n_turns_history_messages`) |
| `fetchSubagentRuns` / `fetchSubagentRunSubtree` / `deleteSubagentRunSubtree` / `steerSubagentRun` | Background subagent task management |
| `readSystemPrompt` / `writeSystemPrompt` / `updateSystemPrompt` / `readSystemPromptTemplate` | System prompt files CRUD |
| `readMemory` / `writeMemory` | Long-term memory files (`workspace/memory/*`) |
| `readHeartbeat` / `writeHeartbeat` | `workspace/HEARTBEAT.md` (always direct HTTP) |
| `listCronJobs` / `addCronJob` / `updateCronJob` / `runCronJob` / `enableCronJob` / `deleteCronJob` | Cron job management |
| `listSkills` / `readSkill` / `uploadSkill` / `setSkillActive` / `deleteSkill` / `pinSkill` | Skill management |
| `listChannels` / `updateChannel` / `getChannelConfig` / `updateChannelConfig` | Channel settings |
| `runCuratorReview` / `getCuratorSettings` / `setCuratorSettings` | Auto-skill curator control |
| `listLogFiles` / `readLogFile` / `openLogStream` | Backend log reading + live `/logs/ws` stream |
| `readEnvConfig` / `writeEnvConfig` (`env.ts`) | Backend `.env` read/update (`GET/PUT /env`) |
| `checkHealth()` | Backend reachability (`system_health` IPC or `GET /system_prompt`) |

Browser-mode chat streaming details:

- Connects to `ws(s)://{VITE_API_BACK_URL}/sessions/agent/ws` and sends `{ session_id, multi_modal_message }`
- Base64 media is uploaded first via `POST /images/upload`, `/audio/upload`, `/video/upload` and referenced by URL
- Server frames: `{ event: "chunk" | "done" | "error" | "stopped" | "hitl_request", ... }`; chunks carry a `type` (`text`/`reasoning`/`tool_start`/`tool_end`/`tool_result`) and tool metadata
- Interrupted streams reconnect with exponential backoff (1s/2s/4s, max 3 attempts via `WS_RECONNECT_MAX_ATTEMPTS`); a mid-stream loss raises `StreamInterruptedError` and emits `ws:conn-loss` / `stream:reconnecting` / `stream:reconnected` / `stream:reconnect:failed` via mitt
- HITL interrupts carry `HitlInterruptData` (tool name/args/allowed decisions); decisions are sent as `hitl_response` frames (`approve` / `reject` / `edit`)

### WebSocket Singletons (ws.ts)

Two independent, module-level singleton connections (both auto-reconnect after 5 seconds):

| Connection | Endpoint | Events via mitt |
|-----------|----------|-----------------|
| Session push | `/sessions/ws?session_id=default` | `ws:connected`, `ws:notification`, `ws:message`, `ws:disconnected` |
| Subagent push | `/subagents/ws` | `ws:subagents:connected`, `ws:subagents:ready`, `ws:subagent_spawned`, `ws:subagent_ended`, `ws:subagents:message`, `ws:subagents:disconnected` |

Both resolve the base URL from `VITE_API_BACK_URL` (`http://` → `ws://`, `https://` → `wss://`).

### Backend Endpoints Used by the Client

REST (base URL `VITE_API_BACK_URL`, default `http://localhost:8080`):

| Endpoint | Method(s) | Purpose |
|----------|-----------|---------|
| `/sessions` | DELETE | Clear a session |
| `/n_turns_history_messages` | GET | Last-N-turns history |
| `/get_history_by_turn_page` | GET | Paginated history (cache-first via Dexie) |
| `/sessions/agent/ws` | WS | Chat streaming, stop, HITL resume |
| `/sessions/ws` | WS | Server push notifications |
| `/subagents/ws` | WS | Subagent spawn/end pushes |
| `/subagents/runs` | GET / DELETE | Subagent run records (list/subtree/delete) |
| `/subagents/steer` | POST | Steer/resume a subagent run |
| `/system_prompt` | GET / POST / PATCH / PUT | System prompt read/write/update |
| `/system_prompt/template` | GET | Persona template files |
| `/memory` | GET / PUT | Long-term memory files |
| `/heartbeat` | GET / PUT | HEARTBEAT.md |
| `/cron`, `/cron/trigger`, `/cron/enable` | GET/POST/PUT/DELETE | Cron job CRUD + trigger |
| `/skills`, `/skills/{path}`, `/skills/upload`, `/skills/toggle`, `/skills/delete`, `/skills/pin` | GET/POST | Skill management |
| `/curator/run`, `/curator/settings` | POST / GET / PUT | Curator review & settings |
| `/channels`, `/channels/{name}`, `/channels/{name}/config` | GET / PUT | Channel toggles & config |
| `/env` | GET / PUT | Backend `.env` read/update |
| `/logs/files`, `/logs` | GET | Log file list & tail read |
| `/logs/ws` | WS | Live log stream |
| `/images/upload`, `/audio/upload`, `/video/upload` | POST | Base64 media upload → URL |
| `/media` | GET | Render persisted media files |

---

## Core Module Details

### SCSS Mixin Library (`common.scss`)

A 300+ line SCSS mixin library providing utilities for layout, shapes, scrollbars, and text overflow (e.g. `fullViewWindow`, `flexCenter`, `scrollBar`, `wordEllipsis`), used across pages, components, and layouts.

### Offline Cache (db.ts, Dexie/IndexedDB)

- `CachedMessage` — mirrors the backend message table rows (turn_num, images/audios/videos, tool fields, token counts); history requests are cache-first and only fetch turns newer than the cached max
- `CachedCharacter` — per-session avatar/name snapshots (base64 data URL or `/avatar/*.jpg` from `public/avatar/`)
- Subagent run records cached from `/subagents/ws` pushes and REST fetches
- Chat background image config (global, module-level singleton via `useChatBackground`)
- `clientLog.ts` persists captured browser `console.*` output into an `all` / `log` / `error` bucket structure

### State & Events

- **Pinia** (`stores/ui.ts`): sidebar collapse (persisted), settings-menu flag, theme write entry
- **mitt event bus**: WS events, stream reconnection events, session stream abort (`session:abort-stream`), cross-component notifications
- **connection.ts**: polls `checkHealth()` every 5 s; exposes `isOnline` / `backendStatus` and drives the global connection banner in `app.vue`

### Type Generation (app/types/backend/)

TypeScript interfaces generated by [ts-rs](https://github.com/Aleph-Alpha/ts-rs) from the Rust backend structs (`ChatRequest`, `HistoryMessage`, `HealthStatus`, `AgentStreamChunk`, `PromptFileResponse`, etc.). Files carry a "Do not edit manually" header.

### Testing

- **Unit** (`pnpm test`): `app/**/*.{test,spec}.ts` — 20+ composable test suites under `app/composables/__tests__/` (happy-dom, `vue-i18n` stubbed)
- **Integration** (`pnpm test:integration`): `tests/integration/` — mounts real `.vue` SFCs (ChatBox, HistoryItem, home page, ModeSwitch, inputBox, image rendering) with the backend mocked
- **Repros** (`repros/`): Playwright specs against the Nuxt dev server on `localhost:3000` (installed MS Edge channel)

---

## Development Guide

### Prerequisites

- [Node.js](https://nodejs.org/) (LTS)
- [pnpm](https://pnpm.io/) — the project uses `pnpm-lock.yaml` / `pnpm-workspace.yaml`; use pnpm to keep the lockfile authoritative
- [Rust](https://www.rust-lang.org/) toolchain (MSRV 1.94) — only needed for building `src-tauri/`
- The Python backend from the project root (see the [root README](../README.md))

### Common Commands

```bash
# Install dependencies (pnpm is the package manager; postinstall runs `nuxt prepare`)
pnpm install

# Dev server (browser mode, Nuxt dev server on http://localhost:3000)
pnpm dev

# Production build (SPA output in dist/ — tauri.conf.json frontendDist)
pnpm build

# Static generation / preview
pnpm generate
pnpm preview

# Type checking (vue-tsc --noEmit)
pnpm typecheck

# Unit tests (Vitest, happy-dom)
pnpm test
pnpm test:watch

# Integration tests (real SFCs, mocked backend)
pnpm test:integration
pnpm test:integration:watch

# Tauri desktop dev mode (runs `pnpm dev` first via beforeDevCommand)
pnpm tauri dev

# Tauri desktop build (runs `pnpm build` first via beforeBuildCommand)
pnpm tauri build
```

`pnpm tauri dev` / `pnpm tauri build` invoke the Tauri CLI from the `@tauri-apps/cli` devDependency.

### Environment Variables

```bash
# client/.env.example
VITE_APP_NAME="sherry"                     # App display name (document <title>)
VITE_API_BACK_URL="http://localhost:8080"  # Python backend base URL (REST + WS)
```

All backend calls resolve `VITE_API_BACK_URL` with a hardcoded fallback of `http://localhost:8080`. There is no dev-server proxy — the frontend calls the backend directly, so the backend must allow cross-origin requests (it serves `Access-Control-Allow-Origin: *`).

`src-tauri/.env.example` separately holds a template of the **backend** model configuration (`MAIN_LLM_*`, `REASONER_*`, `SIMPLE_MAIN_LLM_*`, `ITTT_*`, `TTI_*`, `RERANKER_*`, `EMBEDDING_*`, `TAVILY_API_KEY`, `LANGSMITH_*`).

### Starting the Python Backend

From the project root (see the root README for full setup):

```bash
uv run python -m server
```

The backend listens on `http://127.0.0.1:8080` by default (change the client side via `VITE_API_BACK_URL`).

### Adding New Pages / Components

1. Create a `.vue` file under `app/pages/` — Nuxt 4 auto-registers the route (nested pages under `home/` are rendered by `home/index.vue`'s inner `<NuxtPage>`)
2. Create components under `app/components/` or `app/pages/home/components/`
3. Add composable logic under `app/composables/` (add unit tests under `app/composables/__tests__/`)
4. Add custom tokens to the `@theme` block in `app/assets/css/main.css`
5. Add i18n keys to all four locale files: `app/i18n/locales/{en,ja,ko,zh}.json`

### Adding a New Backend Call

1. Add the endpoint call in `app/composables/bridge.ts` (or `requestApi.ts` for plain REST) — decide whether it uses the Tauri IPC path, `fetchApi`, or both
2. If the backend sends new payload shapes, extend the corresponding type under `app/types/backend/` or `app/types/message.ts`
3. Cover the new logic with a unit test in `app/composables/__tests__/`

---

## License

MIT — same as the EMA AI Agent main project (see `src-tauri/Cargo.toml` `license` field).
