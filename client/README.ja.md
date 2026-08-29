# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 概要

`client` は EMA AI Agent のフロントエンドです。[server/](../server/) の Python バックエンド（Robyn、デフォルト `http://127.0.0.1:8080`）と通信する**ストリーミング SPA チャットクライアント**です。

**Tauri 2 + Nuxt 4（Vue 3 + TypeScript）** で構築されています：

- **ストリーミングチャット** — エージェントの応答は WebSocket（`/sessions/agent/ws`）経由で型付きチャンク（`text` / `reasoning` / `tool_start` / `tool_end` / `tool_result`）としてストリーミングされ、HITL（human-in-the-loop）承認カードに対応
- **オフラインファーストの履歴** — Dexie.js（IndexedDB）が会話履歴、サブエージェント実行レコード、キャラクタープロファイル、チャット背景画像をキャッシュ。履歴リクエストはキャッシュ優先で、欠落しているターンのみ取得
- **充実したツール UI** — スキルマネージャー、cron ジョブ、ハートビートエディター、チャネル設定、ログビューアー（サーバー + クライアントログ）、統計チャート、サブエージェントフローグラフ、ナレッジグラフビューアー
- **ダーク/ライトモード + i18n** — `@nuxtjs/color-mode` によるテーマと 4 ロケール（`zh` / `en` / `ja` / `ko`）
- **デュアルモードブリッジ** — `bridge.ts` が Tauri デスクトップと通常のブラウザを自動判定し、適切なトランスポートを選択

> **開発ステータス**：活発に開発中。ブラウザコードパス（Python バックエンドへの直接 HTTP/WS）は完全に実装済み。`src-tauri/` の Rust 側は現時点でプレースホルダーモジュールのみです（[アーキテクチャ](#アーキテクチャ)を参照）。

---

## アーキテクチャ

### ハイブリッドアーキテクチャ

```
+--------------------------------------------------------------+
|                フロントエンド（Nuxt 4 SPA、app/）              |
|  Vue 3 コンポーネント + composables + Pinia + mitt バス        |
+---------------------------+----------------------------------+
                            |  bridge.ts（実行時自動判定）
              +-------------+--------------+
              |                            |
   [ブラウザモード]               [Tauri モード]
   ofetch HTTP REST +            invoke() IPC コマンド
   ネイティブ WebSocket           + Tauri Events（agent:stream:*）
              |                            |
              |                 src-tauri/（Rust シェル、現在は
              |                 プレースホルダーのみ）
              |                            |
              +-------------+--------------+
                            |
              HTTP / WS  http://localhost:8080
              （VITE_API_BACK_URL、開発プロキシなし）
                            |
+---------------------------v----------------------------------+
|              Python バックエンド（server/、Robyn）             |
|  REST エンドポイント + WebSocket エンドポイント                |
|  Agent Core (LangGraph) | Memory | Skills | Cron | Channels  |
+--------------------------------------------------------------+
```

### データフロー

```
ユーザー操作（Vue コンポーネント）
    -> bridge.ts（Tauri / ブラウザモードを自動判定）
        |
        |--> [ブラウザモード] fetchApi() REST + WebSocket /sessions/agent/ws
        |        （Base64 メディアは先に POST /images|/audio|/video/upload でアップロード）
        |
        |--> [Tauri モード] invoke() -> Rust IPC コマンド -> Tauri Events
        |
    -> リアクティブな状態（composables + Pinia + mitt イベントバス）
    -> リアクティブな UI 更新
```

**Tauri モードに関する注記**：`bridge.ts` には Tauri IPC コードパス（`agent_chat`、`agent_stop`、`session_clear`、`session_history`、`system_prompt_*`、`memory_*`、`system_health`、`subagent_runs`、`subagent_run_delete`）が `app/types/backend/*`（ts-rs 生成）と対応して完全に残っています。しかし `src-tauri/src/` は現時点で**空のプレースホルダーモジュールのみ**（`config/`、`core/`、`database/`、`prompts/`、`rag/`、`runtime/`、`sessions/`、`skills/`、`tools/`、`types/` — それぞれ空の `mod.rs`）であり、Rust エントリポイントと IPC コマンド実装は現在のコードツリーに存在しません。さらに、一部のフロー（ハートビート、cron、スキル、チャネル、curator、ログ、`/env`、サブエージェントのステア）は Rust を完全にバイパスし、両モードで常に `fetchApi` を経由します。

---

## ディレクトリ構成

```
client/
├── .env.example                   # 環境変数テンプレート（VITE_APP_NAME、VITE_API_BACK_URL）
├── eslint.config.mjs              # ESLint flat 設定
├── nuxt.config.ts                 # Nuxt 4 設定（SPA、Tailwind v4、i18n、PrimeVue、Pinia、color-mode）
├── package.json                   # 依存関係マニフェスト（スクリプト: dev/build/generate/preview/typecheck/test/...）
├── playwright.config.ts           # Playwright repro 設定（./repros、Desktop Edge、localhost:3000）
├── pnpm-lock.yaml                 # pnpm ロックファイル
├── pnpm-workspace.yaml            # pnpm ワークスペース設定（allowBuilds）
├── prettier.config.mjs            # Prettier 設定
├── tsconfig.json                  # TypeScript 設定
├── vitest.config.ts               # Vitest ユニットテスト設定（composables、happy-dom、v8 カバレッジ）
├── vitest.integration.config.ts   # Vitest 統合テスト設定（実 .vue SFC、モックバックエンド）
├── app/                           # Nuxt 4 SPA ソース
│   ├── app.vue                    # ルートコンポーネント — レイアウト、Toast レイヤー、接続バナー、ロケール復元
│   ├── common.scss                # 300 行超のグローバル SCSS ミックスインライブラリ（レイアウト、形状、スクロールバー、...）
│   ├── assets/css/
│   │   ├── main.css               # Tailwind v4 エントリ（@import 'tailwindcss'）+ @theme トークン + リセット
│   │   └── main.scss              # グローバル SCSS エントリ（nuxt.config の css で読み込み）
│   ├── common/utils.ts            # 共有ユーティリティ（dayjs セットアップ、formatCompactTimeString、...）
│   ├── components/
│   │   ├── chat/inputBox.vue      # チャット入力ボックスコンポーネント（i18n 対応）
│   │   └── ImagePreviewOverlay.vue# フルスクリーン画像プレビューオーバーレイ（body へ Teleport）
│   ├── composables/               # Vue 3 コンポーザブルロジック（ユニットテストは __tests__/ 配下）
│   │   ├── bridge.ts              # 統合 Tauri/Browser ブリッジ — チャットストリーミング、セッション、システムプロンプト、
│   │   │                          #   メモリ、ハートビート、cron、スキル、チャネル、curator、ログ、env
│   │   ├── requestApi.ts          # fetchApi HTTP ラッパー（ofetch、baseURL は VITE_API_BACK_URL）
│   │   ├── ws.ts                  # WebSocket シングルトン: /sessions/ws + /subagents/ws（5 秒自動再接続）
│   │   ├── db.ts                  # Dexie（IndexedDB）キャッシュ: メッセージ、キャラクタープロファイル、背景、実行レコード
│   │   ├── messages.ts            # 履歴 API（キャッシュ優先の /get_history_by_turn_page）+ ストリーム中断イベント
│   │   ├── connection.ts          # バックエンド健全性ポーリング（5 秒）+ オンライン/オフライン Toast
│   │   ├── toast.ts               # グローバル Toast レイヤー（PrimeVue Toast 登録）
│   │   ├── clientLog.ts           # クライアント側 console.* 取得を Dexie に永続化（all/log/error）
│   │   ├── env.ts                 # バックエンド .env の読み書き（GET/PUT /env）
│   │   ├── workspace.ts           # システムプロンプトハンドラ（/system_prompt GET/POST/PATCH）
│   │   ├── defaultCharacter.ts    # 内蔵デフォルトキャラクター（名前 + /avatar/*.jpg）
│   │   ├── sessionFilter.ts       # クライアント側セッションリストフィルタ（キーワード + 日付範囲）
│   │   ├── useChatBackground.ts   # グローバルチャット背景画像（Dexie 永続化シングルトン）
│   │   ├── useImagePreview.ts     # 画像プレビューオーバーレイ状態
│   │   ├── useSubagentTasks.ts    # バックグラウンドタスク状態シングルトン（fetch + WS + Dexie）
│   │   ├── utils.ts               # max/min + 日時ユーティリティ
│   │   ├── mitt.ts                # mitt イベントバスインスタンス
│   │   └── system.ts              # （空のプレースホルダー）
│   ├── declare/declarations.d.ts  # 型宣言
│   ├── i18n/locales/              # en.json / ja.json / ko.json / zh.json
│   ├── layouts/default.vue        # デフォルトレイアウト — フルビューのラッパー
│   ├── pages/
│   │   ├── index.vue              # ChatInputBox を描画（ルート / は routeRules により /home へ 301 リダイレクト）
│   │   ├── knowledge-graph/
│   │   │   └── index.vue          # ナレッジグラフビューアー（@antv/g6、ドキュメントアップロード、開発中）
│   │   └── home/
│   │       ├── index.vue          # メインチャットシェル — SessionSidebar + ツールバー + ネストされた NuxtPage
│   │       ├── config.ts          # ツールバー（画像/音声/動画アップロード）とヘッダーツール定義
│   │       ├── type.ts            # SessionRecord / Tool / MessageItem 型定義
│   │       ├── index/[sid].vue    # セッションごとのチャットページ（KeepAlive、HITL カード、タスクジャンプバー）
│   │       ├── index/tasks/[sid].vue  # スタンドアロンのバックグラウンドタスクページ（/home/tasks/{sid}）
│   │       └── components/        # 20 のページコンポーネント：
│   │           ├── ChatBox.vue            # メッセージリスト（markdown-it + DOMPurify、メディアは /media 経由）
│   │           ├── SessionSidebar.vue     # セッションリストサイドバー（作成/リネーム/フィルタ）
│   │           ├── HistoryItem.vue        # サイドバーの履歴セッション項目
│   │           ├── ModeSwitch.vue         # ダーク/ライト切替（PrimeVue ToggleSwitch）
│   │           ├── ExtendDialog.vue       # "Extend" ダイアログ
│   │           ├── ConfigDialog.vue       # システム設定（.env エディター、背景、言語、...）
│   │           ├── PersonaDialog.vue      # システムプロンプト / ペルソナエディター
│   │           ├── MemoryDialog.vue       # 長期メモリエディター（workspace/memory/*）
│   │           ├── HeartbeatDialog.vue    # HEARTBEAT.md エディター
│   │           ├── CronDialog.vue         # cron ジョブ管理（/cron）
│   │           ├── SkillsDialog.vue       # スキルマネージャー（一覧/アップロード/切替/ピン/削除/curator）
│   │           ├── ChannelSettingsDialog.vue  # チャネル切替とチャネルごとの設定
│   │           ├── LogsDialog.vue         # ログビューアー（サーバーログ + クライアントログ、ライブストリーム）
│   │           ├── NotificationDialog.vue # サーバー push 通知リスト
│   │           ├── StatsDialog.vue        # 利用統計（@antv/g2、GChart.vue 経由）
│   │           ├── GChart.vue             # @antv/g2 チャートラッパー
│   │           ├── SubagentTasksView.vue  # バックグラウンドタスクビュー（一覧/詳細/フローグラフ）
│   │           ├── SubagentRunDetail.vue  # 単一サブエージェント実行の詳細
│   │           ├── SubagentFlowGraph.vue  # サブエージェント実行ツリーグラフ（@antv/g6）
│   │           └── AvatarCropDialog.vue   # アバターアップロード + 切り抜き（cropperjs）
│   ├── stores/ui.ts               # Pinia UI ストア（sidebarCollapsed を localStorage に永続化）
│   └── types/
│       ├── message.ts             # BaseMessage / AiMessage / MultiModalMessage、...
│       ├── response.d.ts          # API レスポンス型定義
│       └── backend/               # ts-rs 生成のバックエンド型（ChatRequest、HealthStatus、...）
├── docs/                          # VitePress ドキュメントサイト（guide/commands/events/types/zh）
├── public/                        # 静的アセット（favicon.svg、robots.txt、avatar/）
├── repros/                        # Playwright repro テスト（playwright.config.ts で実行）
├── src-tauri/                     # Tauri 2 ネイティブシェル
│   ├── capabilities/default.json  # 権限（core:default、shell:*、notification、
│   │                              #   global-shortcut:default、window-state:default）
│   ├── icons/                     # アプリアイコン
│   ├── resources/                 # バンドルリソースのプレースホルダー（skills/、templates/）
│   ├── src/                       # 空のプレースホルダーモジュールのみ: config/ core/ database/
│   │                              #   prompts/ rag/ runtime/ sessions/ skills/ tools/ types/
│   ├── tests/                     # Rust テストプレースホルダー（空 mod.rs + .gitkeep ディレクトリ）
│   ├── benches/                   # ベンチマークプレースホルダー
│   ├── .env.example               # バックエンドモデル設定テンプレート（MAIN_LLM_*、TAVILY_API_KEY、...）
│   ├── Cargo.toml                 # Rust マニフェスト（tauri 2、reqwest、ts-rs、tracing、プラグイン、...）
│   ├── Cargo.lock                 # Rust ロックファイル
│   ├── build.rs                   # Tauri ビルドスクリプト
│   └── tauri.conf.json            # Tauri 2 設定（beforeDev: pnpm dev、beforeBuild: pnpm build）
└── tests/integration/             # Vitest 統合テスト（実 SFC、モックバックエンド）
```

## 技術スタック

| レイヤー | 技術 | 目的 |
|-------|-----------|---------|
| **クロスプラットフォームシェル** | [Tauri 2](https://v2.tauri.app/)（`2.0.0-rc.17`、`@tauri-apps/api` ^2.11.1、`@tauri-apps/cli` 2.11.4） | ネイティブデスクトップパッケージング + 設定/ケイパビリティ/アイコン |
| **フロントエンドフレームワーク** | [Nuxt 4](https://nuxt.com/) ^4.5.2 + [Vue 3](https://vuejs.org/) ^3.5.41 | SPA モード（`ssr: false`）、Composition API + `<script setup lang="ts">` |
| **UI コンポーネント** | [PrimeVue](https://primevue.org/) ^4.5.0 + PrimeIcons ^8.0.0（`@primevue/nuxt-module`） | Dialog、Button、Select、ToggleSwitch、Toast など |
| **状態管理** | [Pinia](https://pinia.vuejs.org/) ^4.0.3（`@pinia/nuxt`）+ `pinia-plugin-persistedstate`（localStorage） | グローバル UI 状態（サイドバー、テーマエントリ） |
| **スタイリング** | [Tailwind CSS](https://tailwindcss.com/) v4（`@tailwindcss/vite` 経由）+ SCSS（`sass`） | ユーティリティファースト CSS + `@theme` トークン + SCSS ミックスインライブラリ |
| **カラーモード** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | ダーク/ライトテーマ切替（`.dark` クラス） |
| **国際化** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) 10.6.0 | zh / en / ja / ko、`no_prefix` ストラテジー |
| **Markdown レンダリング** | [markdown-it](https://github.com/markdown-it/markdown-it) ^15 | チャットメッセージの markdown → HTML（ChatBox.vue） |
| **XSS 対策** | [DOMPurify](https://github.com/cure53/DOMPurify) ^3.4 | レンダリング HTML のサニタイズ |
| **日付フォーマット** | [dayjs](https://day.js.org/) | コンパクトタイムスタンプ（`YYYYMMDDHHmmss`）の解析/フォーマット |
| **オフラインストレージ** | [Dexie.js](https://dexie.org/) ^4.4.4 | IndexedDB ラッパー: メッセージ、キャラクター、背景、サブエージェント実行、クライアントログ |
| **イベントバス** | [mitt](https://github.com/developit/mitt) ^3 | 軽量なコンポーネント間通信 |
| **チャート / グラフ** | [@antv/g2](https://g2.antv.antgroup.com/) ^5、[@antv/g6](https://g6.antv.antgroup.com/) ^5 | 統計チャート、サブエージェントフローグラフ、ナレッジグラフ |
| **画像クロップ** | [cropperjs](https://github.com/fengyuanchen/cropperjs) ^1.6 | アバターのアップロードと切り抜き |
| **ユーティリティ** | [lodash-es](https://lodash.com/) ^4.18 | 汎用ユーティリティ関数 |
| **ユニット / 統合テスト** | [Vitest](https://vitest.dev/) ^4 + happy-dom + @vue/test-utils + @vitest/coverage-v8 | コンポーザブルのユニットテスト + SFC 統合テスト |
| **E2E repro** | [@playwright/test](https://playwright.dev/) ^1.62 | 開発サーバーに対する repro テスト（Desktop Edge） |
| **Lint / フォーマット** | ESLint ^10（flat config）+ Prettier | コード品質 |
| **型チェック** | [vue-tsc](https://github.com/vuejs/language-tools) ^3.3.9 | `pnpm typecheck` |
| **バックエンド言語** | [Rust](https://www.rust-lang.org/) 2021 edition（MSRV 1.94） | Tauri シェル（src-tauri/、現在はプレースホルダーモジュール） |

### 主要な設定

- **Nuxt**（`nuxt.config.ts`）：`ssr: false`（純 SPA）；`devtools` は無効；Vite は `clearScreen: false`、`envPrefix: ['VITE_', 'TAURI_']`、`server.strictPort: true`（Tauri はポート固定が必要）；CSS エントリ `~/assets/css/main.css` + `~/assets/css/main.scss`；ルートルール `/` → `/home` へ 301 リダイレクト；`src-tauri/` はスキャン対象外
- **Tauri**（`tauri.conf.json`）：製品名 "EMA AI Agent"、バージョン 0.1.0、識別子 `com.ema-ai.agent`、`beforeDevCommand: pnpm dev`、`beforeBuildCommand: pnpm build`、開発 URL `http://localhost:3000`、frontendDist `../dist`、CSP `null`、メインウィンドウ 800×600 リサイズ可
- **Tailwind**（v4）：`@tailwindcss/vite` 経由で読み込み；エントリ `app/assets/css/main.css` が `tailwindcss` と PrimeIcons をインポート；`@custom-variant dark` は `.dark` クラスで発火；カスタム `@theme` トークン（ブレークポイント 480/768/976/1440、カラー `gray-dark`/`gray-light`/`theme-main`、セリフフォント Merriweather、z-index 1–3）
- **i18n**：ストラテジー `no_prefix`、`defaultLocale: 'en'`、ロケール `zh`/`en`/`ja`/`ko`、`detectBrowserLanguage: false`；`app.vue` がマウント時にロケールを復元（設定 cookie `i18n_redirected` → ブラウザ言語 → フォールバック `en`）
- **PrimeVue**：Aura プリセット由来のカスタム `NoirPreset`（slate プライマリパレット）；ダークモードは `.dark` セレクター
- **Pinia 永続化**：`pinia-plugin-persistedstate` をグローバルに `storage: 'localStorage'` で設定

---

## フロントエンドアーキテクチャ

### コンポーネント階層

```
app.vue（ルート：Toast レイヤー、接続バナー、ロケール復元）
  └─ NuxtLayout（layouts/default.vue）
       └─ NuxtPage
            ├─ /            → 301 リダイレクトで /home へ（routeRules）
            ├─ /knowledge-graph  （knowledge-graph/index.vue、@antv/g6）
            └─ /home（home/index.vue：SessionSidebar + ツールバー）
                 └─ NuxtPage（page-key = route.params.sid、KeepAlive）
                      ├─ /home/{sid}       （index/[sid].vue — チャット + HITL カード）
                      └─ /home/tasks/{sid} （index/tasks/[sid].vue — SubagentTasksView）
```

### 通信ブリッジ（bridge.ts）

`bridge.ts` は Tauri デスクトップとブラウザの両モードで動作する統一 API を提供します。バックエンドへのアクセスはすべてこれ（または `requestApi.ts` の `fetchApi`）経由です：

| API | 説明 |
|-----|-------------|
| `streamChatMessage(request, onChunk, onHitl?, onDone?)` | ストリーミングエージェントチャット；`{ controller, promise }` を返す（Tauri Events または `/sessions/agent/ws`） |
| `sendChatMessage(request, onChunk)` | `streamChatMessage` の簡易ラッパー |
| `stopChatMessage(sessionId)` | 進行中の生成を停止（`agent_stop` IPC または WS `stop` フレーム） |
| `resumeHitl(sessionId, decision, ...)` | 新しい WebSocket で一時停止中の HITL エージェントを再開 |
| `clearSession(sessionId)` | セッション状態をクリア（`session_clear` IPC または `DELETE /sessions`） |
| `getHistory(sessionId, lastTurnCount)` | 履歴を取得（`session_history` IPC または `GET /n_turns_history_messages`） |
| `fetchSubagentRuns` / `fetchSubagentRunSubtree` / `deleteSubagentRunSubtree` / `steerSubagentRun` | バックグラウンドのサブエージェントタスク管理 |
| `readSystemPrompt` / `writeSystemPrompt` / `updateSystemPrompt` / `readSystemPromptTemplate` | システムプロンプトファイルの CRUD |
| `readMemory` / `writeMemory` | 長期メモリファイル（`workspace/memory/*`） |
| `readHeartbeat` / `writeHeartbeat` | `workspace/HEARTBEAT.md`（常に直接 HTTP） |
| `listCronJobs` / `addCronJob` / `updateCronJob` / `runCronJob` / `enableCronJob` / `deleteCronJob` | cron ジョブ管理 |
| `listSkills` / `readSkill` / `uploadSkill` / `setSkillActive` / `deleteSkill` / `pinSkill` | スキル管理 |
| `listChannels` / `updateChannel` / `getChannelConfig` / `updateChannelConfig` | チャネル設定 |
| `runCuratorReview` / `getCuratorSettings` / `setCuratorSettings` | 自動スキル curator の制御 |
| `listLogFiles` / `readLogFile` / `openLogStream` | バックエンドログの読み取り + ライブ `/logs/ws` ストリーム |
| `readEnvConfig` / `writeEnvConfig`（`env.ts`） | バックエンド `.env` の読み取り/更新（`GET/PUT /env`） |
| `checkHealth()` | バックエンド到達性（`system_health` IPC または `GET /system_prompt`） |

ブラウザモードのチャットストリーミング詳細：

- `ws(s)://{VITE_API_BACK_URL}/sessions/agent/ws` に接続し `{ session_id, multi_modal_message }` を送信
- Base64 メディアは先に `POST /images/upload`、`/audio/upload`、`/video/upload` でアップロードされ、URL で参照される
- サーバーフレーム：`{ event: "chunk" | "done" | "error" | "stopped" | "hitl_request", ... }`；チャンクは `type`（`text`/`reasoning`/`tool_start`/`tool_end`/`tool_result`）とツールメタデータを保持
- ストリーム切断時は指数バックオフで再接続（1s/2s/4s、`WS_RECONNECT_MAX_ATTEMPTS` により最大 3 回）；ストリーム中の損失は `StreamInterruptedError` を投げ、mitt 経由で `ws:conn-loss` / `stream:reconnecting` / `stream:reconnected` / `stream:reconnect:failed` を発行
- HITL 割り込みは `HitlInterruptData`（ツール名/引数/選択肢）を保持；判定は `hitl_response` フレーム（`approve` / `reject` / `edit`）として送信

### WebSocket シングルトン（ws.ts）

互いに独立した 2 つのモジュールレベル シングルトン接続（どちらも 5 秒後に自動再接続）：

| 接続 | エンドポイント | mitt 経由で発行されるイベント |
|-----------|----------|-----------------|
| セッション push | `/sessions/ws?session_id=default` | `ws:connected`、`ws:notification`、`ws:message`、`ws:disconnected` |
| サブエージェント push | `/subagents/ws` | `ws:subagents:connected`、`ws:subagents:ready`、`ws:subagent_spawned`、`ws:subagent_ended`、`ws:subagents:message`、`ws:subagents:disconnected` |

両方とも `VITE_API_BACK_URL` からベース URL を解決（`http://` → `ws://`、`https://` → `wss://`）。

### クライアントが使用するバックエンドエンドポイント

REST（ベース URL `VITE_API_BACK_URL`、デフォルト `http://localhost:8080`）：

| エンドポイント | メソッド | 目的 |
|----------|-----------|---------|
| `/sessions` | DELETE | セッションをクリア |
| `/n_turns_history_messages` | GET | 直近 N ターンの履歴 |
| `/get_history_by_turn_page` | GET | ページ分割された履歴（Dexie でキャッシュ優先） |
| `/sessions/agent/ws` | WS | チャットストリーミング、停止、HITL 再開 |
| `/sessions/ws` | WS | サーバー push 通知 |
| `/subagents/ws` | WS | サブエージェントの生成/終了 push |
| `/subagents/runs` | GET / DELETE | サブエージェント実行レコード（一覧/サブツリー/削除） |
| `/subagents/steer` | POST | サブエージェント実行のステア/再開 |
| `/system_prompt` | GET / POST / PATCH / PUT | システムプロンプトの読み取り/書き込み/更新 |
| `/system_prompt/template` | GET | ペルソナテンプレートファイル |
| `/memory` | GET / PUT | 長期メモリファイル |
| `/heartbeat` | GET / PUT | HEARTBEAT.md |
| `/cron`、`/cron/trigger`、`/cron/enable` | GET/POST/PUT/DELETE | cron ジョブ CRUD + トリガー |
| `/skills`、`/skills/{path}`、`/skills/upload`、`/skills/toggle`、`/skills/delete`、`/skills/pin` | GET/POST | スキル管理 |
| `/curator/run`、`/curator/settings` | POST / GET / PUT | curator レビューと設定 |
| `/channels`、`/channels/{name}`、`/channels/{name}/config` | GET / PUT | チャネル切替と設定 |
| `/env` | GET / PUT | バックエンド `.env` の読み取り/更新 |
| `/logs/files`、`/logs` | GET | ログファイル一覧と末尾読み取り |
| `/logs/ws` | WS | ライブログストリーム |
| `/images/upload`、`/audio/upload`、`/video/upload` | POST | Base64 メディアアップロード → URL |
| `/media` | GET | 保存済みメディアファイルの表示 |

---

## コアモジュールの詳細

### SCSS ミックスインライブラリ（`common.scss`）

300 行超の SCSS ミックスインライブラリ。レイアウト・形状・スクロールバー・テキストオーバーフロー向けユーティリティ（`fullViewWindow`、`flexCenter`、`scrollBar`、`wordEllipsis` など）を提供し、ページ・コンポーネント・レイアウト全体で使用されています。

### オフラインキャッシュ（db.ts、Dexie/IndexedDB）

- `CachedMessage` — バックエンドのメッセージテーブル行をミラー（turn_num、images/audios/videos、tool フィールド、トークン数）；履歴リクエストはキャッシュ優先で、キャッシュ済み最大ターンより新しいターンのみ取得
- `CachedCharacter` — セッションごとのアバター/名前スナップショット（base64 data URL または `public/avatar/` からの `/avatar/*.jpg`）
- `/subagents/ws` の push と REST 取得からキャッシュされたサブエージェント実行レコード
- チャット背景画像設定（グローバル、`useChatBackground` によるモジュールレベル シングルトン）
- `clientLog.ts` が取得したブラウザ `console.*` 出力を `all` / `log` / `error` のバケット構造に永続化

### 状態とイベント

- **Pinia**（`stores/ui.ts`）：サイドバー折りたたみ（永続化）、設定メニューフラグ、テーマ書き込みエントリ
- **mitt イベントバス**：WS イベント、ストリーム再接続イベント、セッションストリーム中断（`session:abort-stream`）、コンポーネント間通知
- **connection.ts**：5 秒ごとに `checkHealth()` をポーリング；`isOnline` / `backendStatus` を公開し、`app.vue` のグローバル接続バナーを駆動

### 型生成（app/types/backend/）

[ts-rs](https://github.com/Aleph-Alpha/ts-rs) により Rust バックエンド構造体から生成された TypeScript インターフェース（`ChatRequest`、`HistoryMessage`、`HealthStatus`、`AgentStreamChunk`、`PromptFileResponse` など）。ファイルには "Do not edit manually" ヘッダーがあります。

### テスト

- **ユニットテスト**（`pnpm test`）：`app/**/*.{test,spec}.ts` — `app/composables/__tests__/` 配下に 20+ の composable テストスイート（happy-dom、`vue-i18n` はスタブ）
- **統合テスト**（`pnpm test:integration`）：`tests/integration/` — 実際の `.vue` SFC をマウント（ChatBox、HistoryItem、ホームページ、ModeSwitch、inputBox、画像レンダリング）。バックエンドはモック
- **Repro**（`repros/`）：`localhost:3000` の Nuxt 開発サーバーに対する Playwright スペック（MS Edge チャネルをインストール済み）

---

## 開発ガイド

### 前提条件

- [Node.js](https://nodejs.org/)（LTS）
- [pnpm](https://pnpm.io/) — プロジェクトは `pnpm-lock.yaml` / `pnpm-workspace.yaml` を使用。ロックファイルを正とするため pnpm を使用してください
- [Rust](https://www.rust-lang.org/) ツールチェーン（MSRV 1.94）— `src-tauri/` のビルドにのみ必要
- プロジェクトルートの Python バックエンド（[ルート README](../README.md) を参照）

### よく使うコマンド

```bash
# 依存関係のインストール（パッケージマネージャーは pnpm。postinstall で `nuxt prepare` を実行）
pnpm install

# 開発サーバー（ブラウザモード、Nuxt 開発サーバー http://localhost:3000）
pnpm dev

# 本番ビルド（SPA 出力は dist/ — tauri.conf.json の frontendDist）
pnpm build

# 静的生成 / プレビュー
pnpm generate
pnpm preview

# 型チェック（vue-tsc --noEmit）
pnpm typecheck

# ユニットテスト（Vitest、happy-dom）
pnpm test
pnpm test:watch

# 統合テスト（実 SFC、モックバックエンド）
pnpm test:integration
pnpm test:integration:watch

# Tauri デスクトップ開発モード（beforeDevCommand で先に `pnpm dev` を実行）
pnpm tauri dev

# Tauri デスクトップビルド（beforeBuildCommand で先に `pnpm build` を実行）
pnpm tauri build
```

`pnpm tauri dev` / `pnpm tauri build` は `@tauri-apps/cli` 開発依存の Tauri CLI を呼び出します。

### 環境変数

```bash
# client/.env.example
VITE_APP_NAME="sherry"                     # アプリ表示名（document <title>）
VITE_API_BACK_URL="http://localhost:8080"  # Python バックエンドのベース URL（REST + WS）
```

すべてのバックエンド呼び出しは `VITE_API_BACK_URL` を解決し、ハードコードされたフォールバック `http://localhost:8080` を持ちます。開発サーバーのプロキシはありません — フロントエンドはバックエンドに直接アクセスするため、バックエンドがクロスオリジンリクエストを許可している必要があります（バックエンドは `Access-Control-Allow-Origin: *` を返します）。

`src-tauri/.env.example` には別途**バックエンド**のモデル設定テンプレート（`MAIN_LLM_*`、`REASONER_*`、`SIMPLE_MAIN_LLM_*`、`ITTT_*`、`TTI_*`、`RERANKER_*`、`EMBEDDING_*`、`TAVILY_API_KEY`、`LANGSMITH_*`）があります。

### Python バックエンドの起動

プロジェクトルートから（セットアップの詳細はルート README を参照）：

```bash
uv run python -m server
```

バックエンドはデフォルトで `http://127.0.0.1:8080` でリッスンします（クライアント側は `VITE_API_BACK_URL` で変更可能）。

### 新しいページ / コンポーネントの追加

1. `app/pages/` に `.vue` ファイルを作成 — Nuxt 4 がルートを自動登録（`home/` 配下のネストされたページは `home/index.vue` の内側 `<NuxtPage>` でレンダリング）
2. `app/components/` または `app/pages/home/components/` にコンポーネントを作成
3. `app/composables/` に composable ロジックを追加（ユニットテストは `app/composables/__tests__/` に）
4. `app/assets/css/main.css` の `@theme` ブロックにカスタムトークンを追加
5. 4 つのロケールファイルすべてに i18n キーを追加：`app/i18n/locales/{en,ja,ko,zh}.json`

### 新しいバックエンド呼び出しの追加

1. `app/composables/bridge.ts` にエンドポイント呼び出しを追加（純粋な REST なら `requestApi.ts`）— Tauri IPC パス、`fetchApi`、その両方のどれを使うか決定
2. バックエンドが新しいペイロード形状を送る場合、`app/types/backend/` または `app/types/message.ts` の対応する型を拡張
3. `app/composables/__tests__/` にユニットテストを追加

---

## ライセンス

MIT — EMA AI Agent 本体と同じ（`src-tauri/Cargo.toml` の `license` フィールドを参照）。
