# TODO List

## 1. Rewrite Frontend

The Tauri 2 + Nuxt 4 frontend project under `client/` has replaced the legacy Streamlit frontend.

### Architecture

```
┌─────────────────┐     HTTP (localhost:XXX)     ┌──────────────────┐
│  Nuxt 4 (SPA)   │ ──────────────────────────▶  │ Python Backend   │
│  (app/)         │ ◀──────────────────────────  │ (FastAPI/Robyn)  │
│                 │     SSE / chunked JSON       │                  │
│  requestApi.ts  │                              │  server/         │
└────────┬────────┘                              └──────────────────┘
         │ Tauri Commands (IPC)
         ▼
┌─────────────────┐
│  Tauri Rust     │  ← ONLY system-level tasks:
│  (src-tauri/)   │     - File I/O (read/write text files)
│                 │     - System notifications
│                 │     - App data directory
│                 │     - System tray & global shortcuts
│                 │     - Window controls
└─────────────────┘
```

**Key constraint**: Tauri Rust does NOT do any network requests or business logic. All network communication (including SSE streaming) goes directly from Nuxt frontend to Python backend via `requestApi.ts`.

### Subtasks

#### 1.1 Tauri Rust Backend (`src-tauri/`)

- [x] Define Rust commands: `read_text_file`, `write_text_file`, `get_app_data_dir`, `show_notification`
- [x] Update `tauri.conf.json`: productName → "EMA AI Agent", identifier → real bundle ID, add system tray config
- [x] Add Tauri plugins: `tauri-plugin-notification`, `tauri-plugin-shell` (if needed)
- [x] Implement system tray with menu (show/hide, quit)
- [x] Implement global shortcuts (e.g. Alt+Space to toggle window)
- [x] Error handling: return proper error types to frontend via Tauri `Result<T, E>`

#### 1.2 Home Page — Session Sidebar (`app/pages/home/`)

- [x] Session list: fetch from backend API at mount, display in sidebar
- [x] Session CRUD: create new session (local placeholder, lands on backend after first message), delete/rename session
- [x] Session switching: click session in sidebar → load its messages
- [x] Responsive sidebar: mobile (overlay) vs desktop (fixed)
- [x] Batch operations: multi-select sessions, batch delete
- [x] Search/filter sessions (by title or date range)

#### 1.3 Chat View — Message Display (`app/pages/home/`)

- [x] Message bubble component with Markdown rendering
- [x] Differentiate user messages vs AI messages (left/right alignment + styling)
- [ ] Message metadata: timestamp, model name, token count
- [x] Scroll-to-bottom on new messages
- [x] Loading/streaming indicator during AI response
- [x] Support multimodal messages: text + images
- [x] Copy message text button

#### 1.4 Chat Input (`app/components/chat/inputBox.vue`)

- [x] Multi-line text input with Enter (send) / Shift+Enter (newline)
- [x] Send button with disabled state during streaming
- [x] Stop generation button during streaming
- [x] Image upload (multipart/form-data via `requestApi.ts`)
- [ ] File upload attachment
- [x] Knowledge base toggle

#### 1.5 Streaming & SSE Integration

- [x] Consume SSE / chunked JSON response from Python backend `/chat/stream` endpoint
- [x] Incrementally update message bubble content as chunks arrive
- [x] Handle stream cancellation (abort fetch + notify backend)
- [x] Handle reconnection on network error

#### 1.6 Local State Management (`app/stores/`)

- [x] Session list state (active session, CRUD operations) — composables + Dexie IndexedDB (Pinia not used)
- [x] Message list state (messages of active session, streaming state) — composables + Dexie (message cache + streaming draft persistence, Pinia not used)
- [x] Pinia store for UI state (sidebar open, tools menu, theme) — sidebar persisted via localStorage (pinia-plugin-persistedstate), theme via @nuxtjs/color-mode (cookie), menu transient
- [x] Dexie.js (IndexedDB) offline cache for session list and recent messages
- [x] Sync strategy: load from IndexedDB first → fetch latest from backend → update cache

#### 1.7 Internationalization (`app/i18n/`)

- [x] i18n JSON files: zh.json, en.json, ja.json
- [x] Translate all UI text (sidebar, input, buttons, tooltips, empty states)
- [x] Ensure i18n locale switching works seamlessly

#### 1.8 Tauri Desktop Integration

- [x] System tray icon + context menu (show, hide, quit)
- [x] Global shortcut Alt+Space to toggle window
- [x] Single-instance lock (prevent multiple windows)
- [x] Window state persistence (position, size, maximized)

#### 1.9 Polish & Testing

- [x] Error handling UI: toast notifications for API errors, network offline
- [x] Loading skeleton / spinner components
- [ ] Responsive layout tested on mobile, tablet, desktop
- [x] Dark mode consistency (tailwind `dark:` classes across all components)
- [ ] Accessibility: keyboard navigation, focus management

---

## 2. Add Platform Integrations

Add more platform adapters under `channel/`, such as WeChat Bot, Feishu (Lark), X (Twitter), and Telegram.

---

**[中文版](TODOList.zh.md)**
