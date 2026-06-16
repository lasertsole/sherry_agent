# TODO List

## 1. Rewrite Frontend

Replace the Streamlit frontend under `client/` with the Tauri 2 + Nuxt 4 frontend project under `client_future/`.

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

- [ ] Define Rust commands: `read_text_file`, `write_text_file`, `get_app_data_dir`, `show_notification`
- [ ] Update `tauri.conf.json`: productName → "EMA AI Agent", identifier → real bundle ID, add system tray config
- [ ] Add Tauri plugins: `tauri-plugin-notification`, `tauri-plugin-shell` (if needed)
- [ ] Implement system tray with menu (show/hide, quit)
- [ ] Implement global shortcuts (e.g. Alt+Space to toggle window)
- [ ] Error handling: return proper error types to frontend via Tauri `Result<T, E>`

#### 1.2 Home Page — Session Sidebar (`app/pages/home/`)

- [ ] Session list: fetch from backend API at mount, display in sidebar
- [ ] Session CRUD: create new session (via backend), delete/rename session
- [ ] Session switching: click session in sidebar → load its messages
- [ ] Responsive sidebar: mobile (overlay) vs desktop (fixed)
- [ ] Batch operations: multi-select sessions, batch delete
- [ ] Search/filter sessions (by title or date range)

#### 1.3 Chat View — Message Display (`app/pages/home/`)

- [ ] Message bubble component with Markdown rendering
- [ ] Differentiate user messages vs AI messages (left/right alignment + styling)
- [ ] Message metadata: timestamp, model name, token count
- [ ] Scroll-to-bottom on new messages
- [ ] Loading/streaming indicator during AI response
- [ ] Support multimodal messages: text + images
- [ ] Copy message text button

#### 1.4 Chat Input (`app/components/chat/inputBox.vue`)

- [ ] Multi-line text input with Enter (send) / Shift+Enter (newline)
- [ ] Send button with disabled state during streaming
- [ ] Stop generation button during streaming
- [ ] Image upload (multipart/form-data via `requestApi.ts`)
- [ ] File upload attachment
- [ ] Knowledge base toggle / context mode selector

#### 1.5 Streaming & SSE Integration

- [ ] Consume SSE / chunked JSON response from Python backend `/chat/stream` endpoint
- [ ] Incrementally update message bubble content as chunks arrive
- [ ] Handle stream cancellation (abort fetch + notify backend)
- [ ] Handle reconnection on network error

#### 1.6 Local State Management (`app/stores/`)

- [ ] Pinia store for session list (active session, CRUD operations)
- [ ] Pinia store for message list (messages of active session, streaming state)
- [ ] Pinia store for UI state (sidebar open, tools menu, theme)
- [ ] Dexie.js (IndexedDB) offline cache for session list and recent messages
- [ ] Sync strategy: load from IndexedDB first → fetch latest from backend → update cache

#### 1.7 Internationalization (`app/i18n/`)

- [ ] i18n JSON files: zh.json (done — skeleton only), en.json, ja.json
- [ ] Translate all UI text (sidebar, input, buttons, tooltips, empty states)
- [ ] Ensure i18n locale switching works seamlessly

#### 1.8 Tauri Desktop Integration

- [ ] System tray icon + context menu (show, hide, quit)
- [ ] Global shortcut Alt+Space to toggle window
- [ ] Single-instance lock (prevent multiple windows)
- [ ] Window state persistence (position, size, maximized)

#### 1.9 Polish & Testing

- [ ] Error handling UI: toast notifications for API errors, network offline
- [ ] Loading skeleton / spinner components
- [ ] Responsive layout tested on mobile, tablet, desktop
- [ ] Dark mode consistency (tailwind `dark:` classes across all components)
- [ ] Accessibility: keyboard navigation, focus management

---

## 2. Add Platform Integrations

Add more platform adapters under `channel/`, such as WeChat Bot and Feishu (Lark).

---

**[中文版](TODOList.zh.md)**
