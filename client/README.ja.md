# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 概要

`client` は EMA AI Agent のフロントエンドであり、**ストリーミング SPA デスクトップクライアント**として設計されています。

**Tauri 2 + Nuxt 4** で構築されており、以下を実現します:

- **より滑らかな操作** — ページ全体の再実行ではなく、Vue 3 のリアクティブな部分更新
- **オフライン優先** — Dexie.js(IndexedDB)で会話履歴をローカルにキャッシュ
- **ネイティブなデスクトップ機能** — Tauri 2 がシステムトレイ、グローバルショートカット(Alt+Space)、ファイルシステムアクセスなど、Streamlit では不可能な機能を提供
- **コンポーネント駆動アーキテクチャ** — Vue 3 Composition API + composables によるスケーラブルなチームコラボレーション

> **開発状況**: 活発に開発中。コアなチャット UI と Tauri IPC ブリッジが機能しています。

---

## アーキテクチャ

### ハイブリッドアーキテクチャ

```
+--------------------------------------------------------------------+
|                    Tauri 2 Desktop App (client/)              |
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

**Rust 実装**: Rust レイヤーは以下の機能を実装します:
1. フロントエンドからの Tauri IPC 呼び出しを受信
2. HTTP リクエストとして Python バックエンド(`http://127.0.0.1:8080`)へ転送
3. Python の SSE ストリームを Tauri イベントに変換し、フロントエンドをリアルタイム更新
4. Python バックエンドプロセスのライフサイクル管理(`EMA_AUTO_START_BACKEND` によるオプションの自動起動)
5. システムトレイ(表示/非表示/終了)とグローバルショートカット(Alt+Space によるウィンドウ切替)を提供

### データフロー

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
    -> Reactive state (composables + mitt event bus)
    -> Reactive UI update
```

---

## ディレクトリ構造

```
client/
├── .env.example                   # Environment variable template
├── .gitignore                     # Git ignore rules
├── eslint.config.mjs              # ESLint flat config
├── nuxt.config.ts                 # Nuxt 4 configuration (SSR=off, Vite, Tailwind, i18n, PrimeVue, color-mode)
├── package.json                   # Dependency manifest (pnpm workspace root)
├── pnpm-lock.yaml                 # pnpm lockfile
├── pnpm-workspace.yaml            # pnpm workspace definition
├── prettier.config.mjs            # Prettier code formatter config
├── tsconfig.json                  # TypeScript configuration
├── app/                           # Nuxt 4 SPA source
│   ├── app.vue                    # Root component entry
│   ├── common.scss                # Global SCSS mixin library (layout, shapes, scrollbar, etc.)
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css           # Tailwind v4 entry (@import 'tailwindcss') + @theme tokens + global reset
│   │   │   └── main.scss          # (reserved)
│   │   └── images/                # (reserved) Static images
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

## 技術スタック

| レイヤー | 技術 | 用途 |
|------|------|------|
| **クロスプラットフォームシェル** | [Tauri 2](https://v2.tauri.app/) | Webフロントエンドをシステム API アクセス可能なネイティブデスクトップアプリとしてパッケージ化 |
| **フロントエンドフレームワーク** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA モード(`ssr: false`)、Composition API + `<script setup lang="ts">` |
| **UI コンポーネント** | [PrimeVue 5](https://primevue.org/) + [PrimeIcons](https://primevue.org/icons) | 事前構築された UI コンポーネント(Button、Checkbox、Menu、ToggleSwitch など) |
| **状態管理** | [Vue 3 Composition API](https://vuejs.org/guide/extras/composition-api-faq)(composables + [mitt](https://github.com/developit/mitt) イベントバス) | グローバル状態 + リアクティブな UI 更新 |
| **スタイリング** | [Tailwind CSS](https://tailwindcss.com/)(v4、`@tailwindcss/vite` 経由) + SCSS | ユーティリティファースト CSS + カスタム mixin ライブラリ |
| **カラーモード** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | ダーク/ライトテーマ切替 |
| **国際化** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) | 中国語(既定)/英語 |
| **Markdown レンダリング** | [markdown-it](https://github.com/markdown-it/markdown-it) | チャットメッセージの Markdown → HTML |
| **XSS 保護** | [DOMPurify](https://github.com/cure53/DOMPurify) | HTML 出力のサニタイズ |
| **日付フォーマット** | [dayjs](https://day.js.org/) | コンパクトタイムスタンプ(YYYYMMDDHHmmss)の解析/整形 |
| **オフライン保存** | [Dexie.js](https://dexie.org/) | 会話履歴キャッシュのための IndexedDB ラッパー |
| **イベントバス** | [mitt](https://github.com/developit/mitt) | 軽量コンポーネント間通信 |
| **ユーティリティ** | [lodash-es](https://lodash.com/) | ディープクローン、重複排除など共通関数 |
| **ビルドツール** | [Vite](https://vitejs.dev/) | 開発サーバー + プロダクションビルド |
| **バックエンド言語** | [Rust](https://www.rust-lang.org/) 2021 edition(MSRV 1.94) | Tauri ネイティブロジック |
| **ロギング** | [tracing](https://docs.rs/tracing/) + [tauri-plugin-tracing](https://github.com/tauri-apps/tauri-plugin-tracing) | 構造化ロギング(Tauri バックエンド) |
| **型生成** | [ts-rs](https://github.com/Aleph-Alpha/ts-rs) | Rust 構造体から TypeScript 型を自動生成 |

### 主要な設定

- **Nuxt**: `ssr: false`(純 SPA); `pages/` ディレクトリ構造; `VITE_*` および `TAURI_*` 環境変数プレフィックスホワイトリスト付き Vite 設定; `/` ルートは `/home` へリダイレクト
- **Tauri**: アプリ識別子 `com.ema-ai.agent`、製品名 "EMA AI Agent"、開発 URL `http://localhost:3000`、インラインスタイル許可のため CSP を `null` に設定、ウィンドウ 800×600 リサイズ可能
- **Tailwind**(v4): `@tailwindcss/vite` Vite プラグインでロード、`app/assets/css/main.css`(`@import 'tailwindcss'`)にエントリ、カスタムトークン(色、ブレークポイント、z-index)は `@theme` ブロックに定義; 動的間隔(例: `h-15`)は `--spacing` で自動生成
- **i18n**: 既定ロケール `zh`、ストラテジ `prefix_except_default`
- **PrimeVue**: Noir プリセット(slate カラーパレット)、`.dark` CSS クラスセレクタでダークモード

---

## アーキテクチャ

### アーキテクチャ概要

```
client/ (Tauri+Nuxt+Vue)
                         │
                         ├── Streaming SPA, partial refresh
                         ├── Native desktop app (Tauri 2)
                         ├── System tray + global shortcut (Alt+Space)
                         ├── Offline cache (Dexie/IndexedDB)
                         ├── WebSocket real-time updates
                         ├── Markdown rendering + XSS sanitization
                         ├── Dark/Light mode + i18n
                         └── Module-based state via Vue 3 composables
```

### コンポーネント階層

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

### 通信ブリッジ (bridge.ts)

`bridge.ts` composable は、Tauri デスクトップとブラウザモードの両方で動作する統合 API を提供します:

| API | 説明 |
|-----|------|
| `sendChatMessage(request, onChunk)` | ストリーミング Agent チャット(Tauri イベント / SSE) |
| `stopChatMessage(sessionId)` | 進行中の生成を停止 |
| `clearSession(sessionId)` | セッション状態をクリア |
| `getHistory(sessionId, lastTurnCount)` | 会話履歴を取得 |
| `readSystemPrompt()` | すべてのシステムプロンプトファイルを読み取り |
| `writeSystemPrompt(fileToContent)` | システムプロンプトファイルを上書き |
| `updateSystemPrompt(fileToContent)` | システムプロンプトファイルをマージ更新 |
| `checkHealth()` | Python バックエンドへの接続可能性を確認 |

### WebSocket (ws.ts)

リアルタイムのサーバープッシュ通知のための WebSocket シングルトン:

- `{wsBase}/sessions/ws?session_id=default` に接続
- 切断時に自動再接続(5秒遅延)
- mitt 経由でイベントを発行: `ws:connected`、`ws:disconnected`、`ws:notification`、`ws:message`
- `VITE_API_BACK_URL` 環境変数から WS URL を解決

---

## コアモジュール詳細

### SCSS Mixin ライブラリ (`common.scss`)

レイアウト、形状、スクロールバー、テキストオーバーフローのユーティリティを提供する 300+ 行の SCSS mixin ライブラリ:
- サイズ制約: `minWidth` / `maxWidth` / `fixedWidth` / `fullWidth` など
- 形状: `fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule` など
- レイアウト: `flexCenter` / `scrollBar` / `wordEllipsis` など
- 画像: `imgFullInParent` / `fullImg` など

### Tauri バックエンド (`src-tauri/`)

- **lib.rs**: `tauri::Builder` 起動、システムトレイ(表示/非表示/終了)、グローバルショートカット(Alt+Space 切替)、Python プロセスマネージャー(`EMA_AUTO_START_BACKEND` で自動起動)
- **main.rs**: Windows サブシステムエントリ、`#![windows_subsystem = "windows"]` でリリースビルドのコンソールウィンドウを非表示
- **tauri.conf.json**: アプリ識別子 `com.ema-ai.agent`、製品名 "EMA AI Agent"、ビルドコマンド `pnpm build`、開発 URL `http://localhost:3000`
- **Cargo.toml**: Rust 依存関係 — tauri 2.x(tray-icon feature)、serde + serde_json、reqwest(rustls-tls、streaming)、tracing + tauri-plugin-tracing、ts-rs、thiserror、anyhow、tokio、uuid、プラグイン: shell、notification、global-shortcut、single-instance、window-state

### Rust モジュール構造

```
src-tauri/src/
├── commands/          # IPC command handlers
│   ├── agent.rs       # agent_chat, agent_stop
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

## 開発ガイド

### 前提条件

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/)(推奨)または npm
- [Rust](https://www.rust-lang.org/) >= 1.94(MSRV)
- [Tauri CLI v2](https://v2.tauri.app/start/cli/)

### 一般的なコマンド

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

### 環境変数

```bash
# .env.example
VITE_API_BACK_URL=http://localhost:8080  # Python backend URL
VITE_APP_NAME=EMA AI Agent               # App display name
EMA_PROJECT_ROOT=..                       # Project root for Python backend auto-spawn
EMA_AUTO_START_BACKEND=true               # Auto-start Python backend with Tauri app
```

### 新しいページ / コンポーネントの追加

1. `app/pages/` の下に `.vue` ファイルを作成 — Nuxt 4 がルートを自動登録
2. `app/components/` の下にコンポーネントを作成 — グローバルで自動利用可能
3. `app/composables/` の下に composable ロジックを作成
4. `app/assets/css/main.css` の `@theme` ブロックにカスタムトークンを追加
5. `app/i18n/locales/zh.json` と `en.json` に i18n キーを追加

### Python バックエンドの起動

```bash
# From the project root (EMA_AI_agent/)
python -m server
```

バックエンドは既定で `http://127.0.0.1:8080` で起動します(`VITE_API_BACK_URL` で設定可能)。

### 新しい IPC コマンドの追加

1. `src-tauri/src/commands/<module>.rs` で `#[derive(TS)]` 付きでリクエスト/レスポンス型を定義
2. `PythonBridge` メソッドを使って `#[tauri::command]` 関数を実装
3. `lib.rs` の `.invoke_handler(tauri::generate_handler![...])` にコマンドを登録
4. `cargo test` を実行して `app/types/backend/` に TypeScript 型を再生成
5. `app/composables/bridge.ts` に対応するラッパーを追加

---

## ライセンス

EMA AI Agent メインプロジェクトのライセンスと同じです。
