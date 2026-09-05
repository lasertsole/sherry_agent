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
- **短期セッションメモリ**（[MesMemory](context_engine/README.md)）：human/ai/tool のすべてのメッセージを SQLite（WAL モード）に永続化し、FTS5 インデックスを自動作成——中国語全文検索用の trigram トークナイザーテーブルも含みます
- **履歴取得**：直近 N ターン、ページング履歴、ターン範囲指定のクエリをプロンプトコンテキストとして整形
- **セッションチェックポイント**：スレッドセーフな非同期 SQLite チェックポインター（`langgraph-checkpoint-sqlite`）がエージェント状態を再起動をまたいで永続化し、古いチェックポイントは自動クリーンアップ
- **会話要約**：Summarization ミドルウェアが auxiliary LLM で長い履歴を会話中に圧縮
- **プライベートナレッジグラフ RAG**：`multimodal_rag` スキルがドキュメント/フォルダをエンティティ関係グラフにインデックス化（ベンダード LightRAG + RAG-Anything、`snkv` ベクトルストレージ）し、マルチホップグラフ検索で回答
- ▶️ _アーキテクチャ・データモデル・API の詳細は [Context Engine README](context_engine/README.md) を参照_

### 2. 🛠️ 動的スキルシステム
- **SKILL.md 標準**：スキルは YAML フロントマター（`name`、`description`、オプションで `scope: all | main_only | subagent_only`）を持つ Markdown ファイルで、ローダーが `skills/` 配下のすべての `SKILL.md` を自動検出します
- **内蔵スキル**（[skills/builtin/](skills/builtin/)）：`cron`、`heartbeat`、`clawhub`（GitHub スキルインストーラー）、`skill_creator`（新スキル自動生成）、`image_to_text`、`speech_to_text`、`video_text_to_text`、`text_to_image`、`multimodal_rag`、`code_wiki`、`llm_wiki`
- **スキル管理ツール**：エージェントは実行時にスキルの一覧表示・閲覧・管理が可能。サードパーティ製アップロードスキル（`skills/plugins/`）は明示的に有効化するまで非アクティブ
- **SkillSpector セキュリティスキャン**（[server/service/skill_scanner.py](server/service/skill_scanner.py)）：サードパーティスキルは有効化前に NVIDIA SkillSpector でスキャン（静的 YARA/ルール解析 + auxiliary LLM によるオプションの LLM 意味解析）。検出されたスキルはインストールがブロックされます
- **スキルキュレーター**：context engine の curator スレッドが `skills/auto/` 配下の自動学習スキルを管理
- **ツールタイムアウト**：ツール呼び出しは `TOOL_CALL_TIMEOUT_MINUTES`（デフォルト 5）で制限され、デッドロックを防止
- ▶️ _ミドルウェアパイプライン（ガードレール、反復予算、HITL、正規化、要約、マルチモーダル処理）の詳細は [Middlewares README](agent/middlewares/README.md) を参照_

### 3. 🤖 マルチレベルサブエージェントシステム
- **7 つのランタイムツール**：`sessions_spawn`、`sessions_yield`、`sessions_send`、`sessions_kill`、`sessions_steer`、`agents_list`、`subagents_list`
- **階層ロール**：深度制限付きネスト（デフォルト最大 2 深度、ハード上限 2）、MAIN → ORCHESTRATOR → LEAF ロールと最小権限のツールスコープ
- **コンテキストモード**：ISOLATED（新しいコンテキスト）または FORK（親トランスクリプトのコピー）、ファイル添付にも対応
- **信頼性の高い配信**：結果は冪等チェックと指数バックオフリトライを備えた EventBus announce パイプラインで返却
- **永続化レジストリ**：実行レコードは SQLite に永続化。sweeper が孤立タスクを復旧し、followup チェッカーはランタイムアウトが設定された場合のみ強制（デフォルト: なし）
- **Swarm モード**：FIFO スケジューリングと設定可能な同時実行数によるバッチサブタスク実行
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
│   ├── smart_tool_node.py  # ツールノードパッチ（冪等ツールの並列実行）
│   ├── stream_repetition_guard_wrapper.py # ストリーム出力の繰り返しガード
│   ├── checkpointer/       # スレッドセーフ非同期 SQLite チェックポインター
│   ├── middlewares/        # ミドルウェアパイプライン（要約、ガードレール、HITL など）
│   └── tools/              # エージェント利用可能なツール
│       ├── subagent/       # マルチレベルサブエージェントシステム（spawn/registry/swarm など）
│       ├── file_tools/     # ファイル I/O ツール（読み・書き・パッチ・検索）
│       ├── skill_tools/    # スキル管理ツール（一覧・閲覧・管理）
│       ├── pub_base/       # 共有ツールユーティリティと基盤
│       ├── mcp_plugin.py   # MCP ツール統合
│       ├── web_search.py   # Web 検索ツール（Tavily）
│       ├── python_repl.py  # Python コード実行
│       ├── terminal.py     # ターミナルコマンド実行
│       ├── memory.py       # メモリ閲覧ツール
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
├── config/                 # 集中設定
│   ├── __init__.py         # API ホスト/ポート（127.0.0.1:8080）
│   ├── path.py             # ファイルパス設定
│   ├── schema.py           # 設定スキーマモデル
│   └── num.py              # 数値/チューニングパラメータ
│
├── context_engine/         # メモリエンジン（MesMemory）
│   ├── core.py             # 履歴取得と FTS5 検索 API
│   ├── store/              # セッションメッセージストア（SQLite + FTS5、WAL）
│   └── curator/            # 自動スキルキュレーション
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
│   └── extract_model/      # エンティティ抽出モデル（サードパーティ重み）
│   └── providers/          # LLM プロバイダー仕様とレジストリ
│       └── registry.py    # 20 以上のプロバイダーの ProviderSpec
│
├── plugins/                # プラグインシステム
│   ├── channels/           # チャンネルプラグイン（QQ ボットアダプター）
│   └── mcp_server/         # MCP サーバー設定
│
├── pub_func/               # 共通ユーティリティ関数
│   ├── format/             # テキストフォーマットユーティリティ
│   ├── media/              # メディア処理ユーティリティ
│   ├── message/            # メッセージ処理ユーティリティ
│   └── validator/          # 入力バリデーションユーティリティ
│
├── runtime/                # ランタイム状態とユーティリティ
│   ├── core.py             # シングルトン Register 基底 + セッション単位のクリーンアップ
│   ├── relation_register.py # セッション/socket 関係レジストリ
│   ├── state_register.py   # ステートレジストリ
│   ├── count_call_register.py # 使用量/統計カウンター
│   ├── timer_call_register.py # タイマーレジストリ
│   └── _callback_executor.py # 非同期コールバック実行器
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
├── type/                   # 共有データモデル
│   ├── message.py          # MultiModalMessage、Chat など
│   ├── bus.py              # メッセージバスデータモデル
│   └── client.py           # クライアントデータモデル
│
├── workspace/              # キャラクタープロファイルと行動定義
│   ├── IDENTITY.md         # 名前、年齢、興味、人間関係
│   ├── SOUL.md             # 性格の対比、話し方
│   ├── AGENTS.md           # ツール使用の優先順位、安全境界
│   ├── USER.md             # ユーザー固有の対話設定
│   ├── HEARTBEAT.md        # Heartbeat サービスの未完了タスク
│   ├── character.json      # キャラクター設定（JSON）
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
| **Context Engine** | 短期セッションメッセージメモリ（MesMemory） | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **サブエージェントシステム** | マルチレベルサブエージェントのスポーン、並列実行と結果配信 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **ミドルウェア** | エージェントライフサイクルミドルウェアパイプライン | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |
| **チャンネル** | チャンネルインターフェースとアダプターシステム | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **デスクトップクライアント** | Tauri 2 + Nuxt 4 デスクトップ/モバイル SPA クライアント | [EN](client/README.md) · [ZH](client/README.zh.md) |
| **Cron サービス** | 定時/周期的エージェントタスク実行 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **Heartbeat サービス** | 定期ウェイクアップタスクチェック | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |

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
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | メインチャットモデル（JSON 出力とツール呼び出しに対応必須） |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | 汎用推論スイッチ。プロバイダーごとにマッピング（DeepSeek / OpenAI / GLM / Anthropic） |
| `TAVILY_API_KEY` | Web 検索利用時は必須 | Web 検索ツールを有効化 |
| `REASONER_LLM_*` | — | 思考連鎖（Chain-of-thought）推論モデル |
| `AUXILIARY_LLM_*` | — | 要約/単純タスク向けの軽量モデル（テンプレートのデフォルトはクラウド API。`AUXILIARY_LLM_MODEL_LOCAL=true` でローカル GGUF モデルに切替） |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | 画像 / 動画 / 画像生成 / 音声モデルの設定 |
| `RERANKER_*` / `EMBEDDING_*` | — | 検索用リランカーと埋め込みモデル（下記モデル注記を参照） |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector セキュリティスキャンのスイッチ（デフォルトで有効） |
| `TOOL_CALL_TIMEOUT_MINUTES` / `LOG_LEVEL` | — | ツールタイムアウト（5 分）とログレベル（INFO） |
| `WORKSPACE_TEMPLATE_LANG` | — | ペルソナテンプレートの言語：`en` / `zh` / `ja` / `ko`（初回使用時に遅延コピー） |
| `LANGSMITH_*` | — | オプションの LangSmith トレーシング |

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

## 📝 キャラクタープロファイルの例

エージェントの動作は `workspace/` 配下のファイルによって駆動されます：

- **IDENTITY.md**：名前、年齢、興味、人間関係などを定義。
- **SOUL.md**：性格の対比、話し方、行動ロジックを定義。
- **AGENTS.md**：ツール使用の優先順位、安全境界、倫理ガイドラインを定義。
- **USER.md**：ユーザー固有の対話設定や既知情報を保存。
- **HEARTBEAT.md**：Heartbeat 定時サービスの未完了タスクを列挙。
- **character.json**：構造化されたキャラクター設定（JSON）。
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
