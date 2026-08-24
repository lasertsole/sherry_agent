# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **LangGraph とマルチモーダル技術で構築されたディープ・ロールプレイング AI エージェント。**

## ✨ はじめに

EMA AI Agent は、長期記憶と複雑な推論能力を備えた高度に擬人化された AI エージェントシステムです。単なるチャットボットではなく、独立した**ペルソナ**、動的な**スキル記憶グラフ（Skill Memory Graph）**、スケジュールタスクやバックグラウンドのサブエージェントによる能動的な行動を持つバーチャルコンパニオンです。

エージェントのキャラクター **Sherry** は、親密度に応じて切り替わる二重人格の対比（優しい/冷たい）を備えた探偵の少女です。システム全体は、セッションをまたいで蓄積される記憶を持つ、没入感のある持続的なロールプレイングをサポートするよう設計されています。

---

## 🚀 主な機能

### 1. 🧠 ディープメモリシステム（コンテキストエンジン＋経験グラフ）
- **二重メモリアーキテクチャ**: 短期セッションメモリ（[MesMemory](context_engine/README.md)）＋長期経験知識グラフ
- **経験グラフ**: タスク実行から高シグナルで再利用可能な経験を抽出し、構造化されたノードとエッジに変換する蒸留優先の知識グラフ
- **多役割知識ベース**: メインエージェント＋コマンダーが共有する戦略レベルグラフ（`default`）、ワーカー用の運用レベルグラフ（`worker`）
- **二重回顧**: 精密（ベクトル/FTS5 → コミュニティ拡張 → PPR）＋一般化（コミュニティベクトル → 代表者）による再ランキング付き検索
- **コミュニティ検出と要約**: Leiden アルゴリズムがグラフを分割し、効率的な長期検索のための要約を生成
- **永続ストレージ**: SQLite + FTS5 + ベクトル埋め込み、セッションをまたぐ知識継承をサポート
- ▶️ _アーキテクチャ、データモデル、API の詳細は[コンテキストエンジン README](context_engine/README.md)と経験グラフのドキュメントを参照_

### 2. 🛠️ 動的スキルシステム
- **SKILL.md 標準**: 標準化された Markdown 形式で定義されたスキル — エージェントが自律的に読み取り、新しい能力を学習可能
- **ツール呼び出し**: 組み込みのウェブ検索、ファイル I/O、コード実行（Python Repl）、ターミナルコマンド、メッセージ検索など
- **サブエージェント**: 複雑で時間のかかるタスクをバックグラウンドで並列実行し、メッセージバス経由で非同期結果を取得
- **経験フィードバックループ**: サブエージェントが draft ツールで発見を記録 → タスク完了後に経験を蒸留 → 経験グラフに取り込み → 将来のタスクへ回顧・注入
- ▶️ _ライフサイクル、コマンダーアーキテクチャ、蒸留パイプライン、API ドキュメントは[サブエージェントシステム README](agent/tools/subagent/README.md)を参照_
- **ツールタイムアウト**: Python REPL、ターミナル、ウェブ検索ツールはそれぞれ独立した設定可能なタイムアウトを持ち、期限切れ時に自動終了
- ▶️ _ミドルウェアパイプラインは[ミドルウェア README](agent/middlewares/README.md)を参照_

### 3. 🌐 マルチチャネルアクセス
- **ウェブ UI**: Streamlit で構築されたモダンなチャットインターフェース、マルチモーダル入力（画像、音声）をサポート
- **次世代クライアント**（[client](client/)）: Tauri 2 + Nuxt 4 デスクトップ/モバイル SPA クライアント
- **QQ ボット**: プラグインシステム（`plugins/channels/`）を介した QQ チャネルアダプタ
- **メッセージバス**: 内部非同期メッセージキュー（[MessageBus](bus/core.py)）が入出力チャネルを分離

### 4. 👁️ マルチモーダル対話
- **視覚理解**: ユーザーがアップロードした画像を認識・分析する Image-to-Text（VL）モデルをサポート

### 5. ⏰ スケジュール＆能動的行動
- **Cron サービス**（[skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)）: 定期・一回・cron 式ベースのエージェントタスクをスケジュール
- **ハートビートサービス**（[skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)）: HEARTBEAT.md の保留タスクを定期的にチェックし、アイドル時間に自動実行する周期的なウェイクアップ

---

## 🏗️ 技術スタック

**Python 3.13** をベースに、以下のコア技術を使用:

| モジュール | 技術 |
| :----- | :--------- |
| **エージェントフレームワーク** | LangChain 1.3+、langchain-classic、LangGraph |
| **ベクトル＆検索** | FAISS、LightRAG、Sentence Transformers、BGE/BAAI Embedding シリーズ |
| **データベース** | SQLite（FTS5 全文検索）、LanceDB |
| **グラフアルゴリズム** | igraph + Leiden Algorithm（コミュニティ検出）、PageRank |
| **ウェブサーバー** | Robyn + FastAPI（二重非同期サーバー） |
| **フロントエンド UI** | Streamlit、Tauri 2 + Nuxt 4（次世代クライアント） |
| **LLM サポート** | DeepSeek、OpenAI、Ollama（ローカルモデル）、langchain-deepseek |
| **タスクスケジューリング** | croniter、asyncio |
| **非同期メッセージング** | asyncio.Queue（MessageBus） |

---

## 📂 プロジェクト構成

```text
EMA_AI_agent/
├── agent/                  # エージェントコアロジック＆ミドルウェア
│   ├── core.py             # メインエージェントループ（LangGraph コンパイルグラフ）
│   ├── checkpointer/       # セッション状態チェックポイント
│   ├── codeact/            # CodeAct エージェント（コード対話実行）
│   │   ├── core.py         # CodeAct ループ＆ツールオーケストレーション
│   │   └── utils.py        # CodeAct ユーティリティ
│   ├── middlewares/        # ミドルウェアパイプライン
│   │   ├── summarization.py         # 会話要約
│   │   ├── tool_call_normalize.py   # ツール呼び出し正規化＆ルーティング
│   │   ├── tool_guardrails.py       # ツール安全ガードレール
│   │   ├── iteration_budget.py      # ターン予算制限
│   │   ├── multimodal_processor.py  # ビジョン入力処理
│   │   ├── heartbeat_staleness.py   # ハートビート鮮度チェック
│   │   └── context_engine/          # コンテキストエンジンフック
│   ├── tools/              # エージェントアクセス可能ツール
│   │   ├── subagent/       # サブエージェントシステム（階層的タスク分解）
│   │   │   ├── base.py     # SubagentManager（シングルトンオーケストレータ＋蒸留）
│   │   │   ├── core.py     # Subagent spawn ツール（@tool）
│   │   │   ├── draft.py    # draft ツール — 実行中に主要発見を記録
│   │   │   ├── distiller.py # タスク後の経験蒸留
│   │   │   ├── commander/  # LangGraph ベースのコマンダーエージェント
│   │   │   ├── templates/  # 結果発表テンプレート
│   │   │   └── type.py     # SubAgentOutput データモデル
│   │   ├── file_tools/     # ファイル I/O ツール（読み取り、書き込み、パッチ、検索）
│   │   ├── skill_tools/    # スキル管理ツール（一覧、表示、管理）
│   │   ├── pub_base/       # 共有ツールユーティリティ＆インフラ
│   │   ├── mcp_plugin.py   # MCP プラグインツール
│   │   ├── web_search.py   # ウェブ検索ツール
│   │   ├── python_repl.py  # Python コード実行（タイムアウト付きサブプロセス）
│   │   ├── terminal.py     # ターミナルコマンド実行（サンドボックス、タイムアウト付き）
│   │   ├── memory.py       # メモリ検査ツール
│   │   └── message_search.py # 会話検索ツール
│   └── utils/              # エージェント補助ユーティリティ
│
├── bus/                    # メッセージバス（非同期キュー）
│   └── core.py             # MessageBus — 受信/送信キュー＆イベント
│
├── channels/               # チャネルインターフェース定義
│   ├── base.py             # 抽象チャネルベース
│   ├── manager.py          # チャネルライフサイクルマネージャ
│   └── registry.py         # チャネル登録
│
├── client/                 # 次世代クライアント（Tauri 2 + Nuxt 4）
│   ├── app/                # Nuxt 4 SPA ソース
│   │   ├── app.vue         # ルートコンポーネントエントリ
│   │   ├── pages/          # ページコンポーネント
│   │   ├── layouts/        # レイアウトコンポーネント
│   │   ├── composables/    # Vue 3 コンポーザブルロジック
│   │   ├── assets/         # CSS＆設定アセット
│   │   ├── nuxt.config.ts  # Nuxt 4 設定
│   │   └── package.json    # 依存関係マニフェスト
│   ├── src-tauri/          # Tauri 2 ネイティブシェル（Rust）
│   │   ├── src/            # Rust ソース
│   │   ├── Cargo.toml      # Rust 依存関係
│   │   └── tauri.conf.json # Tauri 2 設定
│   └── README.md           # 英語ドキュメント
│
├── config/                 # 集中型設定
│   ├── path.py             # ファイルパス設定
│   ├── schema.py           # 設定スキーマモデル
│   └── num.py              # 数値/チューニングパラメータ
│
├── context_engine/         # メモリエンジン
│   ├── core.py             # メッセージ検索＆検索 API
│   └── store/              # 短期セッションメッセージメモリ（SQLite + FTS5）
│
├── logs/                   # ログシステム
│   ├── logger.py           # ログ設定
│   └── output/             # ログ出力ディレクトリ
│
├── models/                 # モデルラッパー
│   ├── LLMs/               # LLM モデル設定
│   │   ├── auxiliary_llm/       # 軽量チャットモデル
│   │   ├── main_llm.py         # プライマリチャットモデル
│   │   ├── reasoner_llm.py     # 思考連鎖推論モデル
│   │   └── reasoning_normalizer.py # プロバイダ間で reasoning_content を正規化
│   ├── VTTT_model.py       # ビデオ-テキスト-テキストモデル
│   ├── ITTT_model.py       # 画像-テキストモデル
│   ├── STT_model/          # 音声-テキストモデル
│   ├── embed_model/        # テキスト埋め込みモデル
│   ├── reranker_model/     # クロスエンコーダ再ランカー
│   └── extract_model/      # エンティティ抽出モデル
│
├── plugins/                # プラグインシステム
│   ├── channels/           # チャネルプラグイン（QQ ボットなど）
│   └── mcp_server/         # MCP サーバー設定
│
├── providers/              # LLM プロバイダ仕様＆レジストリ
│   ├── registry.py         # 対応プロバイダすべての ProviderSpec エントリ
│   └── __init__.py         # プロバイダレジストリのエクスポート
│
├── pub_func/               # 共通ユーティリティ関数
│   ├── format/             # テキスト整形ユーティリティ
│   ├── media/              # メディア処理ユーティリティ
│   ├── message/            # メッセージ処理ユーティリティ
│   └── validator/          # 入力検証ユーティリティ
│
├── runtime/                # ランタイム状態＆ユーティリティ
│   ├── core.py             # コアランタイムライフサイクル
│   ├── _callback_executor.py   # 非同期コールバック実行器
│   ├── count_call_register.py   # 使用量/統計カウンタ
│   ├── relation_register.py    # 関係/親密度トラッキング
│   ├── state_register.py   # 状態レジストリ
│   └── timer_call_register.py   # タイマーレジストリ
│
├── server/                 # Robyn バックエンドサービス＆API ルート
│   ├── __main__.py         # サーバーエントリポイント
│   ├── DAO/                # データアクセスオブジェクト
│   ├── service/            # ビジネスロジックサービス
│   └── trigger/            # トリガーマネージャ
│       ├── core.py         # トリガーマネージャ
│       ├── channels/       # 受信チャネルトリガー
│       ├── http/           # HTTP エンドポイントトリガー
│       └── subagent/       # サブエージェント結果トリガー
│
├── skills/                 # スキルライブラリ（SKILL.md 定義ファイル）
│   ├── loader.py           # スキル自動発見＆登録
│   ├── skills_snapshot.py  # スキルプロンプトスナップショット生成
│   ├── skills_snapshot.json # キャッシュされたスキルプロンプトスナップショット
│   ├── auto/               # 自動学習スキル
│   ├── plugins/            # プラグイン提供スキル
│   └── builtin/            # 組み込みスキル実装
│       └── core/           # コア組み込みスキル
│           ├── web_search/     # ウェブ検索＆スクレイプ
│           ├── cron/           # Cron スケジュールタスクスキル
│           ├── heartbeat/      # ハートビート定期チェックスキル
│           ├── image_to_text/  # 画像理解
│           ├── speech_to_text/ # 音声認識
│           ├── video_text_to_text/ # ビデオ理解
│           ├── multimodal_rag/ # RAG ベース知識検索
│           ├── clawhub/        # GitHub リポジトリクローナー
│           └── skill_creator/  # 新スキル自動生成
│
├── src/                    # ランタイムデータディレクトリ
│   ├── checkpoints/        # セッションチェックポイント
│   ├── data/               # データストレージ
│   ├── sessions/           # セッションランタイムストア
│   └── store/              # データストア
│
├── static/                 # 静的アセット
│   ├── avatar/             # キャラクターアバター画像
│   └── images/             # その他の画像
│
├── temp/                   # 一時ファイル
│
├── tests/                  # テストスイート
│
├── type/                   # 共有データモデル
│   ├── message.py          # MultiModalMessage、Chat など
│   ├── bus.py              # メッセージバスデータモデル
│   └── client.py           # クライアントデータモデル
│
├── workspace/              # キャラクタープロフィール＆動作定義
│   ├── IDENTITY.md         # 名前、年齢、興味、関係
│   ├── SOUL.md             # 性格の対比、話し方
│   ├── AGENTS.md           # ツール使用優先度、安全境界
│   ├── USER.md             # ユーザー固有の対話優先度＆既知の事実
│   ├── HEARTBEAT.md        # ハートビートサービス用の保留タスク
│   ├── character.json      # キャラクター設定
│   ├── prompt_builder.py   # プロフィール-プロンプトビルダー
│   ├── template/           # プロンプトテンプレート
│   └── memory/             # 長期記憶ストレージ
│
├── .env                    # 環境変数（API キー、モデルパス）
├── .env.example            # 環境変数テンプレート
├── pyproject.toml          # Python 依存関係（uv 管理）
├── uv.lock                 # uv 用ロックファイル
├── start.sh                # ワンクリック起動スクリプト
├── introduce.md            # プロジェクト紹介（EN）
├── introduce.zh.md         # プロジェクト紹介（ZH）
├── TODOList.md             # 開発ロードマップ（EN）
├── TODOList.zh.md          # 開発ロードマップ（ZH）
└── cron_jobs.json          # Cron ジョブスケジュールデータ
```

---

## 📚 サブモジュールドキュメント

各主要サブシステムには独自の詳細 README があります:

| サブモジュール | 説明 | ドキュメント |
|-----------|-------------|---------------|
| **コンテキストエンジン** | 短期セッションメッセージメモリ（MesMemory） | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **サブエージェントシステム** | 階層的タスク分解、並列実行＆経験蒸留 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **ミドルウェア** | エージェントライフサイクルミドルウェアパイプライン | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |
| **チャネル** | チャネルインターフェース＆アダプタシステム | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **次世代クライアント** | Tauri 2 + Nuxt 4 デスクトップ/モバイル SPA クライアント | [EN](client/README.md) · [ZH](client/README.zh.md) |
| **Cron サービス** | スケジュール/定期エージェントタスク実行 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **ハートビートサービス** | 定期的ウェイクアップタスクチェック | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |

---

## ⚡ クイックスタート

### 1. 前提条件
**Python 3.13+** がインストールされていることを確認してください。

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv sync
```

### 2. モデルのダウンロード
初回実行時、システムは**Hugging Face**から自動的に Embedding モデルと Reranker モデルを `models/embed_model` と `models/reranker_model` にダウンロードします。注意:

- **ネットワーク**: huggingface.co にアクセスできることを確認してください（中国のユーザーはプロキシまたはミラーが必要な場合があります）。
- **待ち時間**: モデルの重みは大きいです（数百 MB 〜数 GB）。ダウンロード時間は接続速度に依存します。
- **中断時の再開**: ダウンロードが中断された場合は、対応するディレクトリを削除して再起動すると再ダウンロードされます。

> モデルを手動でダウンロードしてディレクトリに配置すれば、自動ダウンロードをスキップできます。

### 3. 環境変数の設定
`.env` の例をコピーし、API キー（DeepSeek、OpenAI など）とモデルパスを記入します。

```bash
cp .env.example .env
# .env を編集して MAIN_LLM_API_KEY、モデルパスなどを設定
```

### 4. サービスの起動
提供された `start.sh` スクリプトを使用して、ローカル Ollama モデル、バックエンド、フロントエンド UI を一度に起動します。

```bash
chmod +x start.sh
./start.sh
```

### 5. （オプション）手動起動

各コンポーネントを個別に起動することもできます:

```bash
python -m server  # バックエンド起動
```

---

## 📝 キャラクタープロフィールの例

エージェントの動作は `workspace/` 配下の Markdown ファイルによって決定されます:

- **IDENTITY.md**: 名前、年齢、興味、関係などを定義
- **SOUL.md**: 性格の対比、話し方、行動ロジックを定義
- **AGENTS.md**: ツール使用優先度、安全境界、倫理ガイドラインを定義
- **USER.md**: ユーザー固有の対話優先度と既知の事実を保存
- **HEARTBEAT.md**: ハートビートスケジュールサービス用の保留タスク一覧
- **character.json**: 構造化されたキャラクター設定（JSON）

---

## 🤝 コントリビューション

Issues と Pull Requests を歓迎します！新しいスキルを追加するには:

1. `skills/` の下にフォルダを作成します。
2. スキルの使用方法と手順を説明する `SKILL.md` を書きます。
3. エージェントを再起動すると、新しいスキルを自動的に発見してロードします。

---

連絡先: QQ 3132225629

## 📄 ライセンス

このプロジェクトは MIT ライセンスの下でライセンスされています。

---

> **💡 ヒント**: このプロジェクトは、高度な AI エージェントとディープロールプレイングの探求に触発されました。
