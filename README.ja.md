# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **LangChain/LangGraph とマルチモーダル技術で構築されたディープ・ロールプレイング AI エージェント。**

## ✨ はじめに

EMA AI Agent は、長期記憶と複雑な推論能力を備えた、高度に擬人化された AI エージェントシステムです。単なるチャットボットではなく、独立した **ペルソナ**、動的な **スキルシステム**、そして定時タスクとバックグラウンドサブエージェントによる能動的な行動を備えたバーチャルコンパニオンです。

エージェントのキャラクター **Sherry（橘シェリー）** は自称少女探偵。外見は常に明るく元気いっぱい、その内面は冷静で切れ者です。システム全体は、記憶がセッションを越えて蓄積していく没入型の持続的ロールプレイをサポートするよう設計されています。

---

## 🚀 主な機能

### 1. 🧠 階層型メモリシステム（Context Engine）
- **短期セッションメモリ**（[MesMemory](context_engine/README.md)）：会話履歴を SQLite（WAL モード）に永続化し、FTS5 インデックスを自動作成——中国語全文検索用の trigram トークナイザーテーブルも含みます; 永続化は二段タイミングです（`MessagePersistenceMiddleware`）: ツール結果は返った瞬間にフラッシュされ、残りの新しい human/ai/tool メッセージはモデル呼び出しの各境界で増分フラッシュされます（`persisted_message_ids` ウォーターマークで write-once）。生ストアは圧縮の発生に依存しません
- **履歴取得**：直近 N ターン、ページング履歴、ターン範囲指定のクエリをプロンプトコンテキストとして整形
- **セッションチェックポイント**：スレッドセーフな非同期 SQLite チェックポインター（`langgraph-checkpoint-sqlite`）がエージェント状態を再起動をまたいで永続化し、古いチェックポイントは自動クリーンアップ
- **会話要約**：Summarization ミドルウェアが auxiliary LLM で長い履歴を会話中に圧縮
- **コンテキスト統治**：過大なツール結果と人間メッセージは回収可能なディスクコピーを残してプロンプトから退避され、`read_file` 結果はスライスされ、あらゆる圧縮の前に LLM なしのテールクリップが走り、チェーン要約は会話ペイロードから除外されます
- **プライベートナレッジグラフ RAG**：`multimodal_rag` スキルがドキュメント/フォルダをエンティティ関係グラフにインデックス化（ベンダード LightRAG + RAG-Anything、`snkv` ベクトルストレージ）し、マルチホップグラフ検索で回答
- **経験抽出（Experience Extraction）**：4 つのライフサイクル経路が会話履歴を再利用可能な経験として蓄積します。圧縮時の memory review（圧縮のたびに）、圧縮時に todo が全完了したときの plan 抽出、圧縮前の memory flush、圧縮後の todo fork です。それぞれ MEMORY.md / USER.md、plan ナレッジベース（`agent/tools/todolist/knowledge/`）、`skills/auto/`、`todos.db` に書き込みます
- ▶️ _アーキテクチャ・データモデル・API の詳細は [Context Engine README](context_engine/README.md) を参照_
- ▶️ _4 本の抽出経路とスキルキュレーションは [Experience README](docs/experience/README.ja.md) を参照_

### 2. 🛠️ 動的スキルシステム
- **SKILL.md 標準**：スキルは YAML フロントマター（`name`、`description`、オプションで `scope: all | main_only | subagent_only`）を持つ Markdown ファイルで、ローダーが `skills/` 配下のすべての `SKILL.md` を自動検出します
- **内蔵スキル**（[skills/builtin/](skills/builtin/)）：`cron`、`heartbeat`、`clawhub`（GitHub スキルインストーラー）、`skill_creator`（新スキル自動生成）、`image_to_text`、`speech_to_text`、`video_text_to_text`、`text_to_image`、`multimodal_rag`、`code_wiki`、`llm_wiki`
- **スキル管理ツール**：エージェントは実行時にスキルの一覧表示・閲覧・管理が可能。サードパーティ製アップロードスキル（`skills/plugins/`）は明示的に有効化するまで非アクティブ
- **SkillSpector セキュリティスキャン**（[server/service/skill_scanner.py](server/service/skill_scanner.py)）：サードパーティスキルは有効化前に NVIDIA SkillSpector でスキャン（静的 YARA/ルール解析 + auxiliary LLM によるオプションの LLM 意味解析）。検出されたスキルはインストールがブロックされます
- **スキルキュレーター**：context engine の curator スレッドが `skills/auto/` 配下の自動学習スキルを管理 — 詳細は [Experience README](docs/experience/README.ja.md)
- **ツールタイムアウト**：実際のツール別制限は `TOOLS_TIMEOUTS` レジストリ（`WEB_SEARCH_TIMEOUT=15`、`TERMINAL_TIMEOUT=30`、`PYTHON_REPL_TIMEOUT=30`）から取得されます。`TOOL_CALL_TIMEOUT_MINUTES` は保存設定にすぎず、**どの実行パスも消費しません**
- ▶️ _ミドルウェアパイプライン（ガードレール、反復予算、HITL、正規化、要約、マルチモーダル処理）の詳細は [Middlewares README](agent/middlewares/README.md) を参照_

### 3. 🤖 マルチレベルサブエージェントシステム
- **7 つのランタイムツール**：`sessions_spawn`、`sessions_yield`、`sessions_send`、`sessions_kill`、`sessions_steer`、`agents_list`、`subagents_list`
- **階層ロール**：深度制限付きネスト（デフォルト最大 2 深度、ハード上限 2）、MAIN → ORCHESTRATOR → LEAF ロールと最小権限のツールスコープ
- **独立コンテキスト**：すべてのサブエージェントは新規の独立したコンテキストで実行 —— 親トランスクリプトは決して継承しない。ファイル添付に対応
- **信頼性の高い配信**：結果は冪等チェックと指数バックオフリトライを備えた EventBus announce パイプラインで返却
- **永続化レジストリ**：実行レコードは SQLite に永続化。sweeper が孤立タスクを復旧し、followup チェッカーはランタイムアウトが設定された場合のみ強制（デフォルト: なし）
- **Swarm モード**：FIFO スケジューリングと設定可能な同時実行数によるバッチサブタスク実行
- **検証された完了**：すべての spawn が子ターン間に補助 LLM の完了判定器を実行します；`continue` 判定は判定器のフォローアッププロンプトを次のターンとして注入し、設定された `COMPLETION_JUDGE["goal_max_turns"]` 予算（既定 5）で上限が決まります
- **機能ロール（オプトイン）**：`sessions_spawn(functional_role=...)` でワーカーを特化（general / researcher / executor / reviewer / librarian）。ロールが LLM 層、ツール allow-list、子のシステムプロンプトセクションを決定します
- ▶️ _完全なアーキテクチャは [Subagent System README](agent/tools/subagent/README.md) を参照_

### 4. 🌐 マルチチャンネルアクセス
- **Robyn バックエンド**（[server/](server/)）：非同期 HTTP API + WebSocket（`/sessions/ws`）、`127.0.0.1:8080` でリッスンし、アップロードされたメディアを `/static`、`/images`、`/audio`、`/video` で配信
- **デスクトップクライアント**（[client/](client/)）：Tauri 2 + Nuxt 4（Vue 3 + TypeScript）SPA。システムトレイ、グローバルショートカット、オフライン履歴キャッシュ（Dexie/IndexedDB）、ダーク/ライトモード、i18n に対応
- **QQ ボット**：プラグインシステムによる QQ チャンネルアダプター（[plugins/channels/qq/](plugins/channels/qq/)）
- **メッセージバス**（[bus/core.py](bus/core.py)）：内部非同期キューがチャンネルとエージェントコアを分離

### 5. 👁️ マルチモーダルインタラクション
- **画像理解（ITTT）**：Image-to-Text ビジョンモデルによるユーザー画像の認識・分析
- **動画理解（VTTT）**：Video-Text-to-Text モデルによる動画コンテンツの分析
- **音声認識（STT）**：FunASR ベースのローカル音声認識
- **Text-to-Image（TTI）**：`text_to_image` スキルによるテキストからの画像生成
- **ドキュメント解析**：MinerU ベースのマルチモーダルドキュメント取り込み（ナレッジグラフ RAG パイプライン向け）

### 6. ⏰ 定時実行と能動的行動
- **Cron サービス**（[skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)）：一回限り（`at`）、間隔（`every`）、cron 式（`cron`、croniter + タイムゾーン）の 3 種類のエージェントタスクをスケジュール。JSON ジョブストアに永続化し、実行履歴とチャンネル配信に対応
- **Heartbeat サービス**（[skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)）：定期的なウェイクアップ（デフォルト 30 分）で `HEARTBEAT.md` の未完了タスクを確認し、LLM が skip/run を判断、結果は通知ゲートを通過

---

## 🏗️ 技術スタック

**Python 3.13**（依存関係管理は [uv](https://docs.astral.sh/uv/)）上に構築され、以下のコア技術を使用しています：

| モジュール | 技術 |
| :----- | :--------- |
| **エージェントフレームワーク** | LangChain 1.3+（`create_agent` + ミドルウェア）、LangGraph コンパイル済みグラフ |
| **チェックポイント** | langgraph-checkpoint-sqlite（スレッドセーフ非同期 SQLite セーバー） |
| **Web サーバー** | Robyn（HTTP + WebSocket + 静的ホスティング） |
| **データベース** | SQLite（aiosqlite、FTS5 全文検索、WAL モード） |
| **グラフ RAG** | ベンダード LightRAG + RAG-Anything（multimodal_rag スキル）、`snkv[vector]` ストレージ |
| **ローカル推論** | llama-cpp-python（GGUF：bge-m3 embedding、bge-reranker-v2-m3 reranker、auxiliary/ITTT/VTTT モデル）、FunASR（STT） |
| **ドキュメント解析** | mineru-vl-utils |
| **Web 検索** | langchain-tavily（Tavily API） |
| **LLM プロバイダー** | langchain-openai、langchain-deepseek、langchain-community + 20 以上のプロバイダーレジストリ（OpenAI、Anthropic、DeepSeek、Zhipu GLM、DashScope Qwen、Gemini、Moonshot Kimi、MiniMax、Groq、OpenRouter、SiliconFlow、Volcengine、Azure OpenAI、Ollama、vLLM など） |
| **構造化出力** | instructor、json_repair |
| **評価（Evaluation）** | RAGAS（グラフ RAG 品質指標）+ 自製サンドボックス評価フレームワーク（`evals/`） |
| **MCP** | langchain-mcp-adapters（`plugins/mcp_server/` でサーバーを設定） |
| **タスクスケジューリング** | croniter、asyncio |
| **非同期メッセージング** | asyncio キュー（MessageBus、EventBus） |
| **メディア処理** | OpenCV（headless）、Pillow、websockets / websocket-client |
| **デスクトップクライアント** | Tauri 2 + Nuxt 4（Vue 3、TypeScript、pnpm） |
| **ロギング** | loguru（オプションで LangSmith トレーシング） |

---

## 📂 プロジェクト構成

```text
EMA_AI_agent/
├── agent/                  # エージェントコアロジック
│   ├── core.py             # メインエージェントループ（LangChain create_agent → LangGraph グラフ）
│   ├── wrapper/            # グラフレベルラッパー（繰り返しガード、コンテキスト上限）
│   ├── checkpointer/       # スレッドセーフ非同期 SQLite チェックポインター
│   ├── middlewares/        # ミドルウェアパイプライン（要約、ガードレール、HITL など）
│   └── tools/              # エージェント利用可能なツール
│       ├── subagent/       # マルチレベルサブエージェントシステム（spawn/registry/swarm など）
│       ├── todolist/       # セッションスコープの todo 計画レイヤー
│       │   └── knowledge/  # Plan ナレッジベース + `knowledge` ツール
│       ├── file_tools/     # ファイル I/O ツール（読み・書き・パッチ・検索）
│       ├── skill_tools/    # スキル管理ツール（一覧・閲覧・管理）
│       ├── pub_base/       # 共有ツールユーティリティと基盤（BaseSQLiteRepository、パスユーティリティ）
│       ├── mcp_plugin.py   # MCP ツール統合
│       ├── web_search.py   # Web 検索ツール（Tavily）
│       ├── python_repl.py  # Python コード実行
│       ├── terminal.py     # ターミナルコマンド実行
│       ├── memory.py       # メモリ閲覧ツール
│       ├── question.py     # HITL 多肢選択の質問ツール
│       └── message_search.py # 会話 FTS5 検索ツール
│
├── bus/                    # メッセージバス（非同期キュー）
│   └── core.py             # MessageBus —— インバウンド/アウトバウンドキュー
│
├── channels/               # チャンネルインターフェース定義
│   ├── base.py             # 抽象チャンネル基底クラス
│   ├── manager.py          # チャンネルライフサイクルマネージャー
│   └── registry.py         # チャンネル登録
│
├── client/                 # デスクトップクライアント（Tauri 2 + Nuxt 4、pnpm）
│   ├── app/                # Nuxt 4 SPA ソース（Vue 3）
│   ├── src-tauri/          # Tauri 2 ネイティブシェル（Rust）
│   └── README.md           # クライアントドキュメント
│
├── config/                 # 集中設定（paths、feature TypedDicts、schema、settings）
│   ├── __init__.py         # API ホスト/ポート（127.0.0.1:8080）
│   ├── path.py             # ファイルパス設定
│   ├── schema.py           # 設定スキーマモデル
│   ├── sherry_settings.py  # sherry.jsonc ローダー
│   └── features/           # オブジェクト単位の feature TypedDict と既定インスタンス
│
├── context_engine/         # メモリエンジン（MesMemory）
│   ├── core.py             # 履歴取得と FTS5 検索 API
│   ├── store/              # セッションメッセージストア（SQLite + FTS5、WAL）
│   ├── events/             # 追記型イベントログ + projector
│   ├── embeddings/         # ベクトルセマンティック検索（indexer / search）
│   └── curator/            # 自動スキルキュレーション
│
├── docs/                   # サブシステム設計ドキュメント（言語別 README）
│   ├── experience/         # 経験抽出経路 + スキルキュレーション
│   ├── session_memory/     # セッションメモリのケイパビリティ
│   ├── summarization/      # 圧縮トリガーとクールダウン
│   ├── loop-prevention/    # 暴走ループ防止ハーネス
│   ├── sandbox/            # 評価サンドボックスとツール分離
│   ├── token-guard/        # 128K コンテキストウィンドウ下限
│   ├── context-governance/ # 永続化、退避、テールクリップ、要約フィルタリング
│   └── long-running-tasks/ # TaskFlow オーケストレーション
│
├── evals/                  # 評価フレームワーク（dispatcher + 5 スイート）
│   ├── evals.py            # スイートランナー: uv run python evals/evals.py [suite]
│   ├── sandbox.py          # 実行中のリポジトリ書き込みをリダイレクトするサンドボックス
│   ├── graph_rag/          # グラフ RAG パイプラインの RAGAS 指標
│   ├── subagent/           # サブエージェント spawn パイプラインのベンチマーク
│   ├── long_running_task/  # TaskFlow DAG 評価
│   ├── session_memory/     # セッションメモリスタックのチェック
│   ├── nudge_extraction/   # AI 判定の plan 抽出
│   └── results/            # 実行ごとのレポート（gitignore 済み）
│
├── logs/                   # ロギングシステム
│   ├── logger.py           # ログ設定（loguru）
│   └── output/             # ログ出力ディレクトリ
│
├── models/                 # モデルラッパーと重み
│   ├── LLMs/               # LLM 設定（main_llm.py、reasoner_llm.py、auxiliary_llm/、reasoning_* プロバイダー対応）
│   ├── ITTT_model/         # Image-to-Text モデル（クラウド API またはローカル GGUF）
│   ├── VTTT_model/         # Video-Text-to-Text モデル（クラウド API またはローカル GGUF）
│   ├── STT_model/          # Speech-to-Text モデル（FunASR）
│   ├── embed_model/        # 埋め込みモデル（ローカル bge-m3 GGUF またはクラウド API）
│   ├── reranker_model/     # リランカーモデル（ローカル GGUF またはクラウド API）
│   ├── extract_model/      # エンティティ抽出モデル（サードパーティ重み）
│   └── providers/          # LLM プロバイダー仕様とレジストリ
│       └── registry.py    # 20 以上のプロバイダーの ProviderSpec
│
├── plugins/                # プラグインシステム
│   ├── channels/           # チャンネルプラグイン（QQ ボットアダプター）
│   └── mcp_server/         # MCP サーバー設定
│
├── pub/                    # 共有ユーティリティとデータモデル
│   ├── func/               # 共通ユーティリティ関数
│   │   ├── format/         # テキストフォーマットユーティリティ
│   │   ├── media/          # メディア処理ユーティリティ
│   │   ├── message/        # メッセージ処理ユーティリティ
│   │   └── validator/      # 入力バリデーションユーティリティ
│   └── types/              # 共有データモデル
│       ├── message.py      # MultiModalMessage、Chat など
│       ├── bus.py          # メッセージバスデータモデル
│       └── client.py       # クライアントデータモデル
│
├── runtime/                # ランタイム状態とユーティリティ
│   ├── session/            # セッション単位レジストリ
│   │   ├── core.py         # シングルトン SessionRegister 基底 + セッション単位のクリーンアップ
│   │   ├── relation_register.py # セッション/socket 関係レジストリ
│   │   ├── state_register.py   # ステートレジストリ
│   │   ├── state_keys.py       # 型付き StateKey レジストリ + TypedState ファサード
│   │   ├── count_call_register.py # 使用量/統計カウンター
│   │   ├── timer_call_register.py # タイマーレジストリ
│   │   └── _callback_executor.py # 非同期コールバック実行器
│   └── process/            # プロセス単位サービス
│       ├── crash_loop_breaker.py # 起動クラッシュループ検出
│       └── periodic_backoff.py   # 周期バックオフ状態
│
├── server/                 # Robyn バックエンドサービス
│   ├── __main__.py         # サーバーエントリーポイント（python -m server）
│   ├── DAO/                # データアクセスオブジェクト
│   ├── service/            # ビジネスロジックサービス（skill_scanner.py を含む）
│   └── trigger/            # ルートとハンドラーの登録
│       ├── http/           # HTTP エンドポイントトリガー
│       ├── ws/             # WebSocket トリガー
│       ├── channels/       # チャンネル受信トリガー
│       └── subagent/       # サブエージェント結果トリガー
│
├── skills/                 # スキルライブラリ（SKILL.md 定義ファイル）
│   ├── loader.py           # スキル自動検出と登録
│   ├── skills_snapshot.py  # スキルプロンプトスナップショットの構築
│   ├── auto/               # 自動学習スキル（curator が管理）
│   ├── plugins/            # サードパーティアップロードスキル（デフォルトで非アクティブ）
│   └── builtin/            # 内蔵スキル
│       ├── core/           # cron、heartbeat、clawhub、skill_creator、image_to_text、
│       │                   # speech_to_text、video_text_to_text、multimodal_rag
│       ├── text_to_image/  # Text-to-Image スキル
│       ├── code_wiki/      # コードベース wiki 生成スキル
│       └── llm_wiki/       # Markdown ナレッジベーススキル
│
├── src/                    # ランタイムデータディレクトリ
│   ├── checkpoints/        # セッションチェックポイント
│   ├── data/               # データストレージ
│   ├── store/              # データストア
│   ├── rag/                # RAG インデックス出力
│   └── images/ audio/ video/ # アップロードメディア（静的配信）
│
├── temp/                   # 一時ファイル
│
├── tests/                  # テストスイート（pytest）
│
├── workspace/              # キャラクタープロファイルと行動定義
│   ├── SOUL.md             # 性格の対比、話し方
│   ├── AGENTS.md           # ツール使用の優先順位、安全境界
│   ├── USER.md             # ユーザー固有の対話設定
│   ├── HEARTBEAT.md        # Heartbeat サービスの未完了タスク
│   ├── prompt_builder.py   # プロファイルからプロンプトを構築
│   ├── file_sync.py        # ワークスペーステンプレートの遅延同期（言語別）
│   ├── template/           # ペルソナテンプレート（en / zh / ja / ko）
│   └── memory/             # 長期記憶ストレージ
│
├── .env.example            # 環境変数テンプレート
├── pyproject.toml          # Python 依存関係（uv 管理）
├── uv.lock                 # uv ロックファイル
├── start.sh                # バックエンド起動スクリプト
└── cron_jobs.json          # Cron ジョブスケジュールデータ
```

---

## 📚 サブモジュールドキュメント

各主要サブシステムには詳細な README があります：

| サブモジュール | 説明 | ドキュメント |
|-----------|-------------|---------------|
| **Context Engine** | 短期セッションメッセージメモリ（MesMemory） | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) · [JA](context_engine/README.ja.md) · [KO](context_engine/README.ko.md) |
| **経験体系** | 会話履歴を再利用可能な経験として蓄積する 4 つの抽出経路と、`skills/auto/` を保守する Curator | [EN](docs/experience/README.md) · [ZH](docs/experience/README.zh.md) · [JA](docs/experience/README.ja.md) · [KO](docs/experience/README.ko.md) |
| **セッションメモリ** | SESSION 計画のケイパビリティ: memory flush、圧縮クールダウン、compaction lock、イベントログ、セマンティック検索 | [EN](docs/session_memory/README.md) · [ZH](docs/session_memory/README.zh.md) · [JA](docs/session_memory/README.ja.md) · [KO](docs/session_memory/README.ko.md) |
| **サブエージェントシステム** | マルチレベルサブエージェントのスポーン、並列実行と結果配信 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) · [JA](agent/tools/subagent/README.ja.md) · [KO](agent/tools/subagent/README.ko.md) |
| **サブエージェント設計** | 設計不変条件：二軸ロールモデル、spawn 権限ガード、四層の完了ゲート | [EN](docs/subagent/README.md) · [ZH](docs/subagent/README.zh.md) · [JA](docs/subagent/README.ja.md) · [KO](docs/subagent/README.ko.md) |
| **Code Intel** | code-intel ロール向けの 4 層コード検索：tree-sitter シンボル索引、ast-grep 構造検索、LSP 精密検索、セマンティック検索 | [EN](docs/code-intel/README.md) · [ZH](docs/code-intel/README.zh.md) · [JA](docs/code-intel/README.ja.md) · [KO](docs/code-intel/README.ko.md) |
| **プログラム的ツール呼び出し** | EXECUTOR 専用の `execute_code`：1 本の Python スクリプトを別子プロセスで実行し、loopback TCP RPC ブリッジで実ツールを呼び出す。制限付き builtins とスクリプト単位の予算を併用 | [EN](docs/ptc/README.md) · [ZH](docs/ptc/README.zh.md) · [JA](docs/ptc/README.ja.md) · [KO](docs/ptc/README.ko.md) |
| **ミドルウェア** | エージェントライフサイクルミドルウェアパイプライン | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) · [JA](agent/middlewares/README.ja.md) · [KO](agent/middlewares/README.ko.md) |
| **チャンネル** | チャンネルインターフェースとアダプターシステム | [EN](channels/README.md) · [ZH](channels/README.zh.md) · [JA](channels/README.ja.md) · [KO](channels/README.ko.md) |
| **デスクトップクライアント** | Tauri 2 + Nuxt 4 デスクトップ/モバイル SPA クライアント | [EN](client/README.md) · [ZH](client/README.zh.md) · [JA](client/README.ja.md) · [KO](client/README.ko.md) |
| **Cron サービス** | 定時/周期的エージェントタスク実行 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) · [JA](skills/builtin/core/cron/scripts/README.ja.md) · [KO](skills/builtin/core/cron/scripts/README.ko.md) |
| **Heartbeat サービス** | 定期ウェイクアップタスクチェック | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) · [JA](skills/builtin/core/heartbeat/README.ja.md) · [KO](skills/builtin/core/heartbeat/README.ko.md) |
| **要約圧縮** | コンテキスト圧縮ミドルウェア: 5 つのトリガーポイント、4 経路オーバーフロールーター、スラッシング防止ガード | [EN](docs/summarization/README.md) · [ZH](docs/summarization/README.zh.md) · [JA](docs/summarization/README.ja.md) · [KO](docs/summarization/README.ko.md) |
| **ループ防止** | 暴走ループのガード、指数バックオフブレーカー、プロセスクラッシュゲーティング | [EN](docs/loop-prevention/README.md) · [ZH](docs/loop-prevention/README.zh.md) · [JA](docs/loop-prevention/README.ja.md) · [KO](docs/loop-prevention/README.ko.md) |
| **サンドボックス** | ターミナルと Python REPL の隔離: 環境変数スクラビング、OS ネイティブ分離、承認ゲート | [EN](docs/sandbox/README.md) · [ZH](docs/sandbox/README.zh.md) · [JA](docs/sandbox/README.ja.md) · [KO](docs/sandbox/README.ko.md) |
| **長時間タスク** | TaskFlow DAG エンジン、ステップ判定器、トークン予算、デッドライン、検証済み完了ゲート、ターンをまたぐメモリ継続性 | [EN](docs/long-running-tasks/README.md) · [ZH](docs/long-running-tasks/README.zh.md) · [JA](docs/long-running-tasks/README.ja.md) · [KO](docs/long-running-tasks/README.ko.md) |
| **Token Guard** | 両 LLM の 128K コンテキストウィンドウ下限（起動・ビルド・スポーン・env 書き込み） | [EN](docs/token-guard/README.md) · [ZH](docs/token-guard/README.zh.md) · [JA](docs/token-guard/README.ja.md) · [KO](docs/token-guard/README.ko.md) |
| **Context Governance** | 境界ごとの永続化、ツール結果と人間メッセージの退避、`read_file` スライス、オーバーフロー・テールクリップ、要約フィルタリング | [EN](docs/context-governance/README.md) · [ZH](docs/context-governance/README.zh.md) · [JA](docs/context-governance/README.ja.md) · [KO](docs/context-governance/README.ko.md) |

## ⚡ クイックスタート

### 1. 前提条件
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** —— 依存関係マネージャー。`.venv` は uv が自動的に作成・管理するため、仮想環境を手動で作成する必要はありません。

```bash
git clone <your-repo-url>
cd EMA_AI_agent
uv sync   # .venv を作成し、uv.lock の依存関係を正確にインストール
```

### 2. 環境変数の設定
`.env` テンプレートをコピーし、少なくともメインチャットモデルと Tavily キーを設定してください：

```bash
cp .env.example .env
```

| 変数 | 必須 | 説明 |
| :------- | :------- | :---------- |
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | メインチャットモデル（JSON 出力とツール呼び出しに対応必須）。`MAIN_LLM_MAX_TOKEN` は 131072 (128K) 以上が必要 |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | 汎用推論スイッチ。プロバイダーごとにマッピング（DeepSeek / OpenAI / GLM / Anthropic） |
| `TAVILY_API_KEY` | Web 検索利用時は必須 | Web 検索ツールを有効化 |
| `REASONER_LLM_*` | — | 思考連鎖（Chain-of-thought）推論モデル |
| `AUXILIARY_LLM_*` | — | 要約/単純タスク向けの軽量モデル（テンプレートのデフォルトはクラウド API。`AUXILIARY_LLM_MODEL_LOCAL=true` でローカル GGUF モデルに切替）。`AUXILIARY_LLM_MAX_TOKEN` は 131072 (128K) 以上が必要 |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | 画像 / 動画 / 画像生成 / 音声モデルの設定 |
| `RERANKER_*` / `EMBEDDING_*` | — | 検索用リランカーと埋め込みモデル（下記モデル注記を参照） |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector セキュリティスキャンのスイッチ（デフォルトで有効） |
| `TOOL_CALL_TIMEOUT_MINUTES`（sherry.jsonc）/ `LOG_LEVEL` | — | 保存設定のみ（デフォルト 5）— ツール実行パスは消費しません。ログレベル（INFO） |
| `WORKSPACE_TEMPLATE_LANG` | — | ペルソナテンプレートの言語：`en` / `zh` / `ja` / `ko`（初回使用時に遅延コピー） |
| `"LANGSMITH"`（sherry.jsonc） | — | オプションの LangSmith トレーシング |

### 3. モデルに関する注意（HuggingFace 自動ダウンロード）
**ローカル GGUF** モードに設定されたモデルは、初回使用時に Hugging Face から `models/<model>/model_weight/` へ自動ダウンロードされます。手動ダウンロードは不要です：

- **埋め込みモデル**：`EMBEDDING_MODEL_LOCAL=true`（デフォルト）の場合、ローカルの `bge-m3` Q8_0 GGUF を初回実行時に自動ダウンロードします。
- **リランカーモデル**：`.env` テンプレートのデフォルトは**クラウド API**（`RERANKER_MODEL_LOCAL=false`、OpenAI 互換の `bge-reranker-v2-m3`）。`true` に設定するとローカル GGUF リランカー（約 636 MB、自動ダウンロード）に切り替わります。
- **ITTT / VTTT / Auxiliary LLM**：テンプレートのデフォルトはクラウド API。`*_MODEL_LOCAL=true` でローカル GGUF モデルに切り替え可能（同様に自動ダウンロード）。

> 初回ダウンロードには huggingface.co へのアクセスが必要です（中国本土のユーザーはプロキシやミラーが必要な場合があります）。ダウンロードが中断された場合は次回起動時に再開されます。`models/<model>/model_weight/` を削除すると再ダウンロードを強制できます。

### 4. バックエンドの起動
`start.sh` は uv 管理の `.venv` をアクティブ化し、Robyn バックエンドを起動します（Ollama やフロントエンドは起動しません）：

```bash
chmod +x start.sh
./start.sh          # .venv のインタプリタで python -m server --fast --disable-openapi を実行
```

手動起動（同等）：

```bash
uv run python -m server
```

バックエンドは **http://127.0.0.1:8080** でリッスンし、WebSocket エンドポイントは `/sessions/ws` です。

### 5. （オプション）デスクトップクライアント
Tauri 2 + Nuxt 4 クライアントは [client/](client/) にあります。Node.js 18+、pnpm、Rust が必要です：

```bash
cd client
pnpm install
pnpm dev          # ブラウザモード、開発サーバー http://localhost:3000
pnpm tauri dev    # ネイティブデスクトップモード
```

クライアントはデフォルトで `http://127.0.0.1:8080` の Python バックエンドに接続します（`client/.env` の `VITE_API_BACK_URL` で変更可能）。詳細は[クライアント README](client/README.md) を参照。

---

## 🧪 テスト

テストは `tests/` 配下にあり、**ソースツリーをミラー**しています（`tests/agent/...`、`tests/server/...`、`tests/context_engine/...`）。uv 経由で **pytest** で実行します（単一ファイルや小規模な選択は `uv run pytest`）。すべてのテストファイルはモジュールレベルの `pytestmark`（`unit` / `integration` / `module` / `system` / `regression`）を持ち、どの runner グループで実行されるかを決定します。

### 推奨：プロセス分離 runner

フルスイート（および CI）には split runner を使います。これは **3 つの逐次 pytest プロセス**（並列には決してしない）で MARKER（ディレクトリではなく）によりテストを選択し、終了コードを集約して、グループごとのサマリーと最終判定（全グループ通過時のみ終了コード 0）を出力します：

```bash
uv run python tests/run_tests_split.py                  # ハーメティックスイート（既定、llm_e2e 除外）
uv run python tests/run_tests_split.py --with-llm-e2e   # 実 LLM e2e テストのみ（専用 job モード）
uv run python tests/run_tests_split.py -- -k spawn -q   # `--` 以降の引数は pytest に転送
```

| グループ | Marker | 内容 |
| :---- | :----- | :------- |
| **A** | `unit` | 純ロジック、完全 mock のテスト |
| **B** | `integration or module or system` | ハーメティックな統合 / モジュール / システムテスト |
| **C** | `regression` | クロスモジュール回帰テスト |

**なぜプロセスを分けるのか？** `tests/agent/tools/subagent/conftest.py` は conftest の *インポート* 時に stub callable をプロセスグローバルの `sys.modules` にインストールします。単一プロセスのフルスイート実行では、pytest は収集段階（テスト実行前）にすべての conftest とテストモジュールをインポートするため、それらの stub はプロセス全体で生き続け、スイートを越えて漏れます。遅延（呼び出し時）インポートは stub に解決されますが、より早く実オブジェクトを束縛したモジュールは古い束縛を保持します。結果として、subagent テストから遠く離れたスイートで不可解で順序依存の失敗が起きます（例：スキルスコープのアサーションが stub の固定スキルリストを見る、`TypeError` のトレースバックが conftest の lambda を指す）。グループを別プロセスで実行すれば、このクロススイート汚染は構造的に不可能になります。（stub 自体は `c730a46` 以降リストア安全です。runner は多層防御の運用レイヤーです。）

**Windows の注意：** 子 pytest プロセスの環境には `PYTHONIOENCODING=utf-8` が注入され、runner は `errors="replace"` で出力を取得するため、GBK コンソールコードページが出力を壊したり実行をクラッシュさせたりすることはありません。

### 実 LLM e2e テスト（`llm_e2e` marker）

`tests/agent/tools/subagent/` の 4 つのテストファイル（`test_real_e2e.py`、`test_spawn_direct_e2e.py`、`test_code_intel_researcher_e2e.py`、`test_ptc_executor_e2e.py`）にある 7 つのテストが**実 LLM API** を呼び出します。これらは：

- **既定で選択解除**され（`-m "not llm_e2e"`、`pyproject.toml` の addopts と runner の両方で設定）、
- `@pytest.mark.timeout` の予算（pytest-timeout）で制限され：単純テスト 300 秒、同時テスト 600 秒、
- 明示的に、**専用 job** で実行します：`uv run python tests/run_tests_split.py --with-llm-e2e`（`-m llm_e2e` を選択）または `uv run pytest -m llm_e2e`。

**想定実行時間**（単独、実バックエンド）：単純タスク約 30–60 秒、複雑な最悪ケース約 10 分、同時タスク約 2–9 分。この予算を超える場合は通常の遅さではなく実際のハングであり、テストごとの timeout が制限します（単純 300 秒 / 同時 600 秒）。

**CI：** `.github/workflows/ci.yml` は `main` への push/PR ごとに `uv run python tests/run_tests_split.py` を実行し、スイートを **3 つの逐次 pytest プロセス**（並列には決してしない）で走らせます：A = `unit`、B = `integration` + `module` + `system`、C = `regression`。`--with-llm-e2e` は別のより遅い job として維持してください（API トークンを消費するため、他のスイートと並列に実行しないでください）。

> **注意：** `tests/full/` は上記の標準グループ外の補助/実験ディレクトリです。実 LLM を駆動するファイルはすべて `llm_e2e` タグが付いているため、既定の addopts で選択解除され、split runner も収集しません（さらに `tests/full/` を `--ignore` します）。手動実行：`uv run --no-sync pytest -m llm_e2e tests/full/<file>`。hermetic なテストはここに置きません — real-graph HITL テストは `tests/agent/middlewares/humanInTheLoop/test_hitl_real_graph.py` に移動し、標準グループで実行されます。

### Evals

`evals/` は pytest と並ぶ自製のサンドボックス評価フレームワークです。登録済みの全スイート、または名前で 1 つを実行できます：

```bash
uv run python evals/evals.py                # 登録済みの全スイート
uv run python evals/evals.py graph_rag      # 名前で単一スイートを実行
```

| スイート | 評価内容 |
| :---- | :---------------- |
| `graph_rag` | multimodal_rag パイプラインを RAGAS で採点（faithfulness、answer relevancy、context recall、context precision） |
| `subagent` | 実 `spawn_subagent_direct` パイプラインを決定論的タスクのベンチで評価（タスク成功率 + レイテンシ） |
| `long_running_task` | 依存 DAG 上の TaskFlow オーケストレーションループ（ステップ成功率、フロー完了、wall time） |
| `session_memory` | セッションメモリスタックの 6 チェック：クールダウン、compaction lock、チェックポイント復元、冪等リプレイ、コンテキスト適格性、セマンティック検索ランキング |
| `nudge_extraction` | plan 抽出パスを auxiliary LLM が grounded・再利用可能・非汎用か判定 |

すべてのスイートは `evals/sandbox.py` 内で実行され（リポジトリへの書き込みは一時サンドボックスにリダイレクトされます）、レポートを `evals/results/<suite>/<run_id>/` に書き込みます。このディレクトリは **gitignore** 済みで、実行ごとのレポートがコミットされることはありません。

---

## 📝 キャラクタープロファイルの例

エージェントの動作は `workspace/` 配下のファイルによって駆動されます：

- **SOUL.md**：性格の対比、話し方、行動ロジックを定義。
- **AGENTS.md**：ツール使用の優先順位、安全境界、倫理ガイドラインを定義。
- **USER.md**：ユーザー固有の対話設定や既知情報を保存。
- **HEARTBEAT.md**：Heartbeat 定時サービスの未完了タスクを列挙。
- **prompt_builder.py**：プロファイルファイルからシステムプロンプトを構築。
- **file_sync.py**：不足しているペルソナファイルを `workspace/template/<lang>/`（`WORKSPACE_TEMPLATE_LANG` で選択）から遅延コピー。ユーザーの編集を上書きすることはありません。

---

## 🤝 コントリビューション

Issue と Pull Request を歓迎します！新しいスキルの追加方法：

1. `skills/` 配下にフォルダを作成（サードパーティスキルは `skills/plugins/`）。
2. YAML フロントマター（`name`、`description`、オプションの `scope`）を持つ `SKILL.md` に、スキルの使い方と手順を記述。
3. エージェントを再起動 —— ローダーがすべての `SKILL.md` を自動検出し、モデルに公開します。（実行中のエージェントに内蔵の `skill_creator` スキルで生成させることもできます。）

`skills/plugins/` 配下のサードパーティスキルは SkillSpector でスキャンされ、明示的に有効化されるまで非アクティブのままです。

---

連絡先：QQ 3132225629

## 📄 ライセンス

このプロジェクトは MIT ライセンスの下で公開されています。

---

> **💡 Tips**：このプロジェクトは、高度な AI エージェントとディープ・ロールプレイングの探求からインスピレーションを得ています。
