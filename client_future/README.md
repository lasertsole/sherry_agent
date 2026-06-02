# client_future

## Overview

`client_future` is the next-generation frontend for the EMA AI Agent, designed as a **streaming SPA desktop client** that will gradually replace the existing Python/Streamlit frontend (`client/` directory).

The current `client/` is a conversational Web UI built with Python 3.10 + Streamlit. While functionally complete, it is constrained by Streamlit's script-re-run rendering model, which limits interactivity smoothness, state management, and cross-platform desktop capabilities. `client_future` rebuilds the frontend from the ground up with **Tauri 2 + Nuxt 4**, aiming to deliver:

- **Smoother interactions** — Vue 3 reactive partial updates instead of full-page re-runs
- **Offline-first** — Dexie.js (IndexedDB) for caching conversation history locally
- **Native desktop capabilities** — Tauri 2 provides system tray, file system access, keyboard shortcuts, and other features impossible in Streamlit
- **Component-driven architecture** — Vue 3 Composition API + Pinia for scalable team collaboration

> **Development status**: Early stage; some components are placeholders or demonstrations.

---

## Directory Structure

```
client_future/
├── .gitignore                    # Git ignore rules
├── .vscode/                      # VS Code workspace settings
│   └── settings.json
├── app/                          # Nuxt 4 SPA source
│   ├── app.vue                   # Root component entry
│   ├── common.scss               # Global SCSS mixin library (layout, shapes, scrollbar, etc.)
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css          # Global CSS reset + CSS variables
│   │   │   ├── main.scss         # (reserved)
│   │   │   └── tailwind.scss     # Tailwind directives (@tailwind base/components/utilities)
│   │   ├── images/               # (reserved) Static images
│   │   └── ts/
│   │       └── tailwind.config.ts # Tailwind custom tokens (width, height, z-index utilities)
│   ├── components/
│   │   ├── dom/                  # DOM-based UI components
│   │   │   └── drawer.vue        # Drawer panel component
│   │   ├── icon/                 # (reserved) Icon components
│   │   └── svg/                  # SVG graphic components
│   │       ├── staff.vue         # Single musical staff (5 lines, parameterized by lineGap/lineBold)
│   │       └── staffPaper.vue    # Multi-staff sheet (row count/spacing computed from StaffConfig)
│   ├── composables/              # Vue 3 composable logic
│   │   ├── mitt.ts               # mitt event bus instance
│   │   └── staffConfig.ts        # Staff configuration singleton — reactive refs for paddingY/staffNum/heightPercent/gapPerStaff
│   ├── declare/                  # (reserved) Type declarations
│   │   └── declarations.d.ts
│   ├── layouts/
│   │   └── default.vue           # Default layout — Nuxt 4 layout entry
│   └── pages/
│       └── index.vue             # Main page — composes LazySvgStaffPaper + DomDrawer
├── eslint.config.mjs             # ESLint flat config
├── nodemodules/                  # pnpm dependencies (gitignored)
├── nuxt.config.ts                # Nuxt 4 configuration (SSR=off, Vite, Tailwind CSS module)
├── package.json                  # Dependency manifest (pnpm workspace root)
├── pnpm-lock.yaml                # pnpm lockfile
├── pnpm-workspace.yaml           # pnpm workspace definition
├── prettier.config.mjs           # Prettier code formatter config
├── public/                       # Nuxt public static assets
├── README.md                     # This file (English)
├── README.zh.md                  # Chinese version
├── src-tauri/                    # Tauri 2 native shell
│   ├── capabilities/
│   │   └── default.json          # Permission config (currently core:default only)
│   ├── icons/                    # App icons
│   ├── src/
│   │   ├── lib.rs                # Tauri app entry — Builder setup + tauri_plugin_log (debug mode)
│   │   └── main.rs               # Windows subsystem entry + calls lib::run()
│   ├── Cargo.toml                # Rust dependencies (tauri 2, serde, serde_json, log, tauri-plugin-log)
│   ├── tauri.conf.json           # Tauri 2 config — app name "anon", build commands, dev URL localhost:3000, window config, CSP
│   └── build.rs                  # Tauri build script
└── tsconfig.json                 # TypeScript configuration
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Cross-platform shell** | [Tauri 2](https://v2.tauri.app/) | Packages the web frontend as a native desktop app with system API access |
| **Frontend framework** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA mode (`ssr: false`), Composition API + `<script setup lang="ts">` |
| **State management** | [Pinia](https://pinia.vuejs.org/) + [pinia-plugin-persistedstate](https://prazdevs.github.io/pinia-plugin-persistedstate/) | Global state + persistence |
| **Styling** | [Tailwind CSS](https://tailwindcss.com/) (via `@nuxtjs/tailwindcss`) + SCSS | Utility-first CSS + custom mixin library |
| **Visualization** | [D3.js v7](https://d3js.org/) | SVG musical score rendering, knowledge graphs, etc. |
| **Offline storage** | [Dexie.js](https://dexie.org/) | IndexedDB wrapper for caching conversation history |
| **Event bus** | [mitt](https://github.com/developit/mitt) | Lightweight component communication |
| **Utilities** | [lodash-es](https://lodash.com/) | Deep clone, deduplication, and other common functions |
| **Build tool** | [Vite](https://vitejs.dev/) | Dev server + production builds |
| **Backend language** | [Rust](https://www.rust-lang.org/) 2021 edition | Tauri native logic |
| **Logging** | [log](https://docs.rs/log/) + [tauri-plugin-log](https://github.com/tauri-apps/tauri-plugin-log) | Tauri backend logging (enabled in debug mode) |

### Key Configuration

- **Nuxt**: `ssr: false` (pure SPA); `pages/` directory structure; Vite config with `process.env.TAURI_*` environment variable prefix allowlist
- **Tauri**: App identifier `com.anon.dev`, dev URL `http://localhost:3000`, window title `anon`, CSP set to `null` to allow inline styles
- **Tailwind**: Loaded via `@nuxtjs/tailwindcss` module, config at `app/assets/ts/tailwind.config.ts`, provides custom `w-*`/`h-*`/`z-*` utility classes

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
                         ├── Offline cache (Dexie/IndexedDB)
                         └── Componentized Pinia state management
```

`client_future` will **not** replace `client/` overnight. Migration happens module by module, and eventually `client/` will be deprecated.

### Component Hierarchy

```
app.vue (root)
  └─ NuxtLayout (default.vue)
       └─ NuxtPage (index.vue)
            ├─ LazySvgStaffPaper  (SVG staff paper)
            │    └─ SvgStaff × N  (single staff)
            └─ DomDrawer          (drawer panel)
```

- **SVG layer** (`components/svg/`) — Vector rendering for musical scores and knowledge graphs. `staff.vue` renders a single 5-line staff, `staffPaper.vue` composes multiple staves based on `StaffConfig` singleton parameters.
- **DOM layer** (`components/dom/`) — Traditional HTML components (drawers, buttons, panels, etc.).
- **Composables** — `staffConfig.ts` uses a **singleton pattern** to keep global staff parameters (paddingY, staffNum, heightPercent, gapPerStaff) reactive via `ref()`. `mitt.ts` exports a global `emitter` instance.

### Data Flow

```
User interaction (Vue component)
    → Pinia Store (state change)
    → Reactive UI update
    → (optional) Tauri API (desktop capabilities)
    → (optional) Dexie.js (IndexedDB persistence)
    → (future) Backend API communication
```

---

## Core Module Details

### StaffConfig Singleton (`composables/staffConfig.ts`)

Central configuration manager for musical staff rendering. Uses a **singleton pattern** to ensure a single global instance. All parameters are exposed via `ref()` for reactivity — when any component modifies a parameter, all dependents update automatically.

Key parameters:
- `paddingY` — page top padding
- `staffNum` — staves per page
- `heightPercent` — single-staff height percentage
- `gapPerStaff` — gap between staves
- `baisPerStaff` — per-staff Y offset (for scrolling/pagination)

### SvgStaff / SvgStaffPaper (`components/svg/`)

- **SvgStaff** — Renders a single 5-line staff. `lineGap = heightPercent / 5`, `lineBold = heightPercent / 50`, positioned via `translate(x, y)`.
- **SvgStaffPaper** — Composes multiple `SvgStaff` rows. Row count controlled by `staffNumOfcurrentPage`, row spacing by `baisPerStaff`. `viewBox="0 0 100 100"` enables responsive scaling.

### DomDrawer (`components/dom/drawer.vue`)

Drawer panel component for sidebar/overlay UI patterns.

### SCSS Mixin Library (`common.scss`)

A 300+ line SCSS mixin library providing utilities for layout, shapes, scrollbars, and text overflow:
- Size constraints: `minWidth` / `maxWidth` / `fixedWidth` / `fullWidth`, etc.
- Shapes: `fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule`, etc.
- Layout: `flexCenter` / `scrollBar` / `wordEllipsis`, etc.
- Images: `imgFullInParent` / `fullImg`, etc.

### Tauri Backend (`src-tauri/`)

- **lib.rs**: `tauri::Builder` startup, loads `tauri_plugin_log` in debug mode
- **main.rs**: Windows subsystem entry, `#![windows_subsystem = "windows"]` hides the console window in release builds
- **tauri.conf.json**: App identifier `com.anon.dev`, build command `npm run build`, dev URL `http://localhost:3000`
- **Cargo.toml**: Rust dependencies — tauri 2.x (with devtools feature), serde + serde_json (serialization), log + tauri-plugin-log (logging)

---

## Comparison with client/

| Dimension | client/ | client_future |
|-----------|---------|---------------|
| Language | Python 3.10 | TypeScript / Rust |
| Framework | Streamlit | Nuxt 4 + Vue 3 |
| Rendering | Full-page re-run | Reactive partial update |
| Desktop support | None (Web only) | Tauri 2 native desktop |
| Storage | Python in-memory (ChatStorage) | Dexie.js IndexedDB |
| State management | Streamlit Session State | Pinia + persistence plugin |
| Visualization | Streamlit native charts | D3.js SVG |
| Events | N/A | mitt event bus |
| Backend language | Python (FastAPI/WebSocket) | — (calls same backend API) |

---

## Development Guide

### Prerequisites

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/) (recommended) or npm
- [Rust](https://www.rust-lang.org/) (latest stable)
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
```

### Adding New Pages / Components

1. Create a `.vue` file under `app/pages/` — Nuxt 4 auto-registers the route
2. Create components under `app/components/` — auto-available globally
3. Create composable logic under `app/composables/`
4. Add custom tokens to `app/assets/ts/tailwind.config.ts`

---

## License

Same as the EMA AI Agent main project license.
