# EMA Agent ミドルウェアシステム

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent のミドルウェア層：モデル呼び出しとツール呼び出しのすべてに関わる `AgentMiddleware` コンポーネント — コンテキストエンジニアリング、マルチモーダル入力処理、反復予算、ツールガードレール、トランスクリプト修復、ハートビートスタイルネス検知、ヒューマンインザループ承認、コンテキスト要約、モデルフォールバック付きの分類済み LLM エラーリトライ（`LLMRetryMiddleware`）— に加え、出力繰り返しガードとストリームレベルのグラフラッパー（`RepetitionGuardWrapper`、`ContextLimitGuardWrapper`）。

> 本ドキュメントの記述はすべてソースコードに対して検証済みです（インストール済み `langchain 1.3.9`、`agent/core.py`、`agent/tools/subagent/spawn/core.py`、および `agent/middlewares/` 配下の各モジュール）。以下に登場するクラス名・ファイル名・デフォルト値・状態キーはすべて実在します。

---

## 目次

- [アーキテクチャ概観](#アーキテクチャ概観)
- [ミドルウェアチェーン](#ミドルウェアチェーン)
- [ミドルウェアリファレンス](#ミドルウェアリファレンス)
  - [ContextEngineHook](#contextenginehook)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [LLMRetryMiddleware](#llmretrymiddleware)
  - [Summarization](#summarization)
  - [MaxTokensBoostMiddleware](#maxtokensboostmiddleware)
  - [OutputRepetitionGuard と RepetitionGuardWrapper](#outputrepetitionguard-と-repetitionguardwrapper)
  - [ContextLimitGuardWrapper](#contextlimitguardwrapper)
- [共有状態システム](#共有状態システム)
- [設定](#設定)
- [ライフサイクルとデータフロー](#ライフサイクルとデータフロー)
- [カスタムミドルウェアの作成](#カスタムミドルウェアの作成)
- [付録](#付録)

---

## アーキテクチャ概観

### ミドルウェアとは？

ミドルウェアは `langchain.agents.middleware.AgentMiddleware` を継承し、エージェントループの明確に定義された地点でフックします。システムは 4 つのフックファミリーを使用します（いずれも同期・非同期の両形式あり）：

| フックファミリー | 同期 | 非同期 | 実行タイミング |
|---|---|---|---|
| エージェント前/後 | `before_agent` / `after_agent` | `abefore_agent` / `aafter_agent` | 会話ターンごとに 1 回、モデル–ツールループ全体を囲む |
| モデル前/後 | `before_model` / `after_model` | `abefore_model` / `aafter_model` | 個々のモデルリクエストを囲む |
| モデル呼び出しラップ | `wrap_model_call` | `awrap_model_call` | モデルリクエスト自体をインターセプト（メッセージ / システムプロンプトの改変、LLM のショートサーキット） |
| ツール呼び出しラップ | `wrap_tool_call` | `awrap_tool_call` | 各ツール実行をインターセプト |

### フックの順序セマンティクス

インストール済み `langchain 1.3.9` のソース（`agents/middleware/factory.py` および `agents/middleware/types.py`）に対して検証済み：

- `before_agent` フックは**リスト順**に実行されます — 最初に登録されたミドルウェアが先に走ります。
- `after_agent` フックは**リスト逆順**に実行されます — 最後に登録されたミドルウェアの `after_agent` が最初に走ります（コンパイル済みグラフの出口ノードチェーンです）。
- `wrap_model_call` / `wrap_tool_call` は**リストの先頭が最外層**、末尾が最内層（LLM / ツールに最も近い）として合成されます。

> ⚠️ 旧ミドルウェアフレームワークには `awrap_before_agent` 形式のフックがありましたが、LangChain 1.3 には存在しません。非同期形式は先頭に `a` を付けるだけです：`abefore_agent`、`abefore_model`、`aafter_model`、`aafter_agent`、`awrap_model_call`、`awrap_tool_call`。

### 状態の永続化

ミドルウェアの状態は LangGraph のグラフ状態には**置かれません**（フレームワーク管理の一部のキーを除く）。呼び出しをまたぐ状態はセッション単位のランタイムレジスタに保持されます：

- `state_register_mem`（`StateRegisterMeM`）— インメモリ辞書。揮発性（プロセス再起動でクリア）。
- `state_register_db`（`StateRegisterDB`）— SQLite バックエンド（`src/data/state_register.db`）。再起動後も保持。
- `timer_call_register`（`TimerCallRegister`）— バックグラウンドカウントダウンタイマー（1–60 分）。`HeartbeatStaleness` が使用。

詳細は[共有状態システム](#共有状態システム)を参照。

---

## ミドルウェアチェーン

### メインエージェント（`agent/core.py`）

```python
middleware = [
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    SubagentCompletionDrainMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    LLMRetryMiddleware(fallback_chain=fallback_chain),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# コンパイル済みグラフをさらにラップ（内側 → 外側）：
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
agent = ContextLimitGuardWrapper(agent, context_window=main_llm_max_tokens)
```

`main_llm_max_tokens` は環境変数 `MAIN_LLM_MAX_TOKEN` から読み込まれ（`models/LLMs/main_llm.py`）、メインエージェントの要約トリガーはメインモデルのコンテキストウィンドウの 80 % に置かれます（`COMPRESSION_TRIGGER_RATIO = 0.80`）。

> **注意：** `OutputRepetitionGuard` はメインエージェントのミドルウェアとして**登録されています**（呼び出しごとのインターセプト）。さらにコンパイル済みグラフはストリームレベル検知のため `RepetitionGuardWrapper` でもラップされています — [OutputRepetitionGuard と RepetitionGuardWrapper](#outputrepetitionguard-と-repetitionguardwrapper) を参照。

### ワーカー / サブエージェントパイプライン（`agent/tools/subagent/spawn/core.py`）

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[
            ("messages", 40),
            ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
        ],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# 子グラフも同様にラップ：
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

メインエージェントとの違い：

- 要約トリガーはトークンのみではなく、メッセージ数（40）**または**トークン数（コンテキストウィンドウの 80 %）。
- より厳しい反復予算（90 ではなく 60）。
- `ContextEngineHook`、`MultimodalProcessor`、`HumanInTheLoop`、`LLMRetryMiddleware` はなし（子エージェントには分類済みリトライ/フォールバックループがない）。
- `OutputRepetitionGuard` はここでは本物のミドルウェアとして動作。
- 子セッション終了時、spawn コードは `finally` ブロックで `state_register_mem` から `OutputRepetitionGuard` の 6 つの状態キー（`SESSION_STATE_KEYS`）を削除します。

### ターンごとの実効順序（メインエージェント）

| フェーズ | 順序 |
|---|---|
| `before_agent`（リスト順） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → LLMRetryMiddleware → Summarization |
| `wrap_model_call`（最外層 → 最内層） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → OutputRepetitionGuard → MaxTokensBoostMiddleware → HeartbeatStaleness → HumanInTheLoop → LLMRetryMiddleware → Summarization（Summarization が LLM に最も近い。LLMRetry は Summarization の T4/T5 リカバリを外側から包み、MaxTokensBoost の内側に位置するため、本当の切断だけを目にする） |
| `after_agent`（逆順） | Summarization → LLMRetryMiddleware → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

あるフックを実装しているミドルウェアだけがそのフェーズに参加します。表は「実装していた場合に走る位置」を示しています。

---

## ミドルウェアリファレンス

### ContextEngineHook

**モジュール：** `agent/middlewares/context_engine/core.py` · **クラス：** `ContextEngineHook(AgentMiddleware)`
**フック：** `wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`、`after_agent` / `aafter_agent`

リストの先頭、したがって最外層のラップ層です。

**`wrap_model_call` — システムプロンプト注入**

1. `state_register_mem` の `system_prompt` を参照します。
2. なければ `state_register_db` にフォールバックし、それでも無ければ `workspace.prompt_builder.build_system_prompt(session_id)` で再構築します。
3. `request.override(system_message=...)` で注入し、プロンプトを `state_register_mem` にキャッシュバックします。

**`wrap_tool_call` — スキルレビューの計上**

ツールメタデータが `nudge: true` を設定していない限り（nudge/limit ツール自身の免除）、すべてのツール呼び出しに対して `state_register_db` の `nudge_review_skill_count` をインクリメントします。

**`after_agent` / `aafter_agent` — ターンの仕上げ**

1. `state_register_db` の `nudge_review_memory_count` をインクリメントします。
2. カウンターが閾値に達した場合（`_NUDGE_MEMORY_THRESHOLD = 10` ターン、`_NUDGE_SKILL_THRESHOLD = 10` ツール呼び出し）、`state_register_mem` のセッション単位ロック `nudge_review_memory_lock` / `nudge_review_skill_lock` の下で対応する **nudge サブエージェント**（下記）を起動します。ロック保持中は `after_agent` が nudge 判定をスキップします（カウンターは引き続き増加）。
3. 最終ターンを MesMemory に永続化：`slice_last_turn` → `sanitize_tool_use_result_pairing` → `add_messages(session_id, messages)`（SQLite）。
4. 同期 `after_agent` は `run_async` でサブエージェントを実行し、`aafter_agent` は `asyncio.gather` で永続化と nudge を並行実行します。

**Nudge サブエージェント**（`context_engine/nudge.py`）：メイン LLM 上に構築された独立した `create_agent` インスタンスで、ミドルウェアは `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget()]`。`_NudgeLimitTool` はメタデータに `nudge: true` を持たないツールをすべて拒否するため、nudge エージェントはメモリ/スキル系ツールしか使えません。プロンプト：`_MEMORY_REVIEW_PROMPT`（メモリレビュー）、`_SKILL_REVIEW_PROMPT`（スキルライブラリレビュー）、`_COMBINED_REVIEW_PROMPT`（両方同時）。

> 本ドキュメントの旧版はナレッジグラフ保守（`after_turn`）と `MemoryCache` を主張していました。**現在のコードにはどちらも存在しません。** システムプロンプトは状態レジスタと `build_system_prompt()` から供給され、ミドルウェア層のどこにもナレッジグラフ呼び出しはありません。

### MultimodalProcessor

**モジュール：** `agent/middlewares/multimodal_processor.py` · **クラス：** `MultimodalProcessor(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`

`before_agent` は、内容がマルチモーダルリストである**最後の** `HumanMessage` を処理します：

- **テキスト**項目はそのまま通します（最大 1 件）。
- **`image_url`**：リモートの `http(s)` URL はそのまま保持。`data:` / base64 ペイロードはデコードされ、PIL で `src/<session_id>/mutil_temp/<タイムスタンプ><拡張子>` に保存されます（拡張子は `_IMAGE_MAGIC` のマジックバイトから推定）。永続コピーが `media/` にも作られます。
- **`audio_url`**：一時ファイルへダウンロード（タイムアウト 30 秒）。**`audio_bytes` / `video_url` / `video_bytes`**：同様にデコード・保存（`_AUDIO_MAGIC` / `_VIDEO_MAGIC`）。
- メッセージテキストの末尾に `"[Uploaded media]"` 命令ブロックを追加し、`skill_view` ツールの `image_to_text` / `speech_to_text` / `video_text_to_text` でファイルを確認するようモデルに指示します（モデルはネイティブの視覚能力を持ちません）。
- 永続化パスは `additional_kwargs["images"]` / `["audios"]` / `["videos"]` に格納され、後から MesMemory に書き込まれ履歴レンダリングに使われます。
- **より古い** `HumanMessage` からは `image_url` ブロックが剥ぎ取られ、古い base64 がコンテキストに残りません。

`after_agent` は `mutil_temp` を清掃します：ファイル名の本体が純粋な数値タイムスタンプでないもの、または 7 日より古いものを削除します。

### IterationBudget

**モジュール：** `agent/middlewares/iteration_budget.py` · **クラス：** `IterationBudget(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

1 ターン内の**モデル呼び出し + ツール呼び出しの合計**に対するハード上限。コンストラクタ：`__init__(max_iterations: int = 50)`。メインエージェントは `IterationBudget(90)`、ワーカーエージェントは `IterationBudget(60)` を登録します。

- `before_agent` は `state_register_mem` のカウンターをリセット：`iteration_budget = max_iterations`、`iteration_budget_used = 0`。
- `wrap_model_call` はモデル呼び出しごとに 1 消費。予算が尽きると**モデルを呼ばずに**終端 `AIMessage` を返します。
- `wrap_tool_call` はツール呼び出しごとに 1 消費。尽きると実行の代わりにエラー `ToolMessage`（"Tool [x] skipped — iteration budget exhausted"）を返します。

### ToolGuardrails

**モジュール：** `agent/middlewares/tool_guardrails.py` · **クラス：** `ToolGuardrails(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_tool_call` / `awrap_tool_call`

5 つの失敗病理を検出し、4 段階エスカレーション `ALLOW → WARN → BLOCK → HALT`（`GuardrailAction` 列挙型）で反応します：

| 病理 | トリガー | WARN 後 | BLOCK 後 | hard-stop モード |
|---|---|---|---|---|
| 完全な失敗の繰り返し | 同じツール + 同じ引数（引数 JSON を `sort_keys` した MD5）の失敗 | 2（`exact_failure_warn_after`） | 5（`exact_failure_block_after`） | 5 で HALT |
| 同一ツールの失敗蓄積 | 同じツールが**異なる**引数で失敗し続ける | 3（`same_tool_failure_warn_after`） | 8（`same_tool_failure_halt_after`） | 8 で HALT |
| 冪等な無進捗 | メタデータ `idempotent: true` のツールが同一の結果ハッシュを返す | 2（`no_progress_warn_after`） | 5（`no_progress_block_after`） | 5 で HALT |
| ピンポン | 2 つのツール間の途切れない読み取り専用 A → B → A → B の往復 | 4（`ping_pong_warn_after`） | 6（`ping_pong_block_after`） | 6 で HALT |
| 引数改変 | 同じ冪等ツールが引数バリアントを巡回 | 3 変種（`arg_churn_warn_after`） | 5 変種（`arg_churn_block_after`） | 5 で HALT |

- `before_agent` はターン単位のガード状態をリセットします（`state_register_mem` のキー `tool_guardrail_state`）— 厳密にターン範囲なので、新しいターンはクリーンに始まります。
- `wrap_tool_call` はブロック済みツールと停止状態を事前チェック（実行せずエラー `ToolMessage` を返す）し、ツールを実行してから結果を評価します：
  - `warn` は `ToolMessage` に警告を追記；
  - `block` はツールを `blocked_tools` に記録；
  - `halt` はターンの残りに対する粘着性の停止を設定（`halt_decision`）。
- **リカバリモード**（`recovery_mode_enabled=True` がデフォルト）: 最初の BLOCK でターンが死ぬことはありません。ターンはリカバリ状態に入り、*precheck* 経路がブロックされたツールを解放するので、再試行は新鮮に評価されます。それ以降の BLOCK ごとに違反カウンタが増え、カウンタが `recovery_max_violations`（デフォルト 1）を超えると HALT に格上げされます — 即席の壁ではなく管理された再試行ウィンドウです。
- **ピンポンペア**は隣接する 2 つの呼び出しのツール名をハッシュし、*連続する 2 つ*の呼び出しが両方とも成功した冪等呼び出しである間（両方の記録が結果ハッシュを持つ間）だけ累積します。エラーが一度でも出たり、成功した非冪等（変異）呼び出しが一度でもあると、累積済みのすべてのペア連続記録がゼロに戻ります。結果の内容は比較しません: 途切れない読み取り専用の往復は、それ自体がループ信号として扱われます。非冪等ツールの成功も同様に引数改変状態をリセットします。
- `ToolCallGuardrailConfig` の既定値：`warnings_enabled=True`、`hard_stop_enabled=False`、`recovery_mode_enabled=True`、`recovery_max_violations=1` — `hard_stop_enabled=True` にするとすべての *ブロック* しきい値が HALT に変わり（旧来の厳格な壁）、`recovery_mode_enabled=False` にすると即時ブロックの挙動に戻ります。

▶️ 詳細：[docs/harness/loop-prevention/README.md](../../docs/harness/loop-prevention/README.md) · [中文](../../docs/harness/loop-prevention/README.zh.md) · [한국어](../../docs/harness/loop-prevention/README.ko.md) · [日本語](../../docs/harness/loop-prevention/README.ja.md)

### ToolCallNormalize

**モジュール：** `agent/middlewares/tool_call_normalize.py` · **クラス：** `ToolCallNormalize(AgentMiddleware)`
**フック：** `before_model` / `abefore_model` のみ

コンテキストトリミング後の tool-call / tool-result ペアリングを修復し、プロバイダーの "Message ordering conflict" エラーを防ぎます。処理は `pub.func.sanitize_tool_use_result_pairing(state["messages"])`（`pub/func/transcript_repair.py` で定義）に委譲され、以下を行います：

- `tool_call_id` による `ToolMessage` の重複排除；
- 空の `ToolMessage` の除去；
- 欠落した結果に対するプレースホルダー `ToolMessage`（"tool result missing after context trim."）の挿入；
- エラー状態の `AIMessage` の `invalid_tool_calls` をクリアし、OpenAI tool_calls としてシリアライズされないようにする。

フックはメッセージ全体の置換を返します：`[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`。

### SubagentCompletionDrainMiddleware

**モジュール：** `agent/middlewares/subagent_completion_drain.py` · **クラス：** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**フック：** `before_model` / `abefore_model` のみ

メインエージェントには `ToolCallNormalize` の直後に登録されるため、注入されるメッセージは注入ターンではサニタイズ書き換えをバイパスします。`before_model` でセッションの `SteeringQueue`（親がビジーの間に announce パイプラインが積んだ完了キャリアメッセージ）をリハイドレートして排出（drain）し、`{"messages": [carrier, ...]}` を返すことで、次のモデル呼び出しの直前に再構築された完了キャリア `HumanMessage` を注入します。

- 排出された各キューエントリはキューの SQLite ストアで `CONSUMED` とマークされるため、キャリアは正確に 1 回だけ注入されます（チェックポイント永続化により HITL 再開リプレイも安全）。
- Fail-open：`session_id` の欠落/空、空のキュー、あらゆる例外は握りつぶされます（ログ + no-op）— drain が親ターンを壊すことはなく、キューは再試行のために保持されます。
- 注入されたキャリアは `origin='subagent_completion'` として MesMemory に永続化されます。

### HeartbeatStaleness

**モジュール：** `agent/middlewares/heartbeat_staleness.py` · **クラス：** `HeartbeatStaleness(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

スタックしたターン用のウォッチドッグ。**メインエージェントとワーカーエージェントの両方**に登録されています（本ドキュメントの旧版はワーカーのみと主張していました — 誤りです）。

- `before_agent` は状態キーをリセットし、`timer_call_register.register(..., execute_now=True)` でバックグラウンドタイマーを起動します（1 分間隔）。
- `wrap_model_call` は `heartbeat_iter` をインクリメントします — ただし、以前のチェックですでにターンが kill されていれば先に `HeartbeatTimeoutError` を送出します。`wrap_tool_call` はツール実行中に `heartbeat_tool` を設定し、返却後にクリアします。
- `skip_heartbeat` バイパス：メタデータに `skip_heartbeat: true` を設定したツール（interrupt ベースのツールは人間の入力を待ってグラフを駐車させるため、after-tool フックは再開まで走りません）は `heartbeat_tool` の代わりに `heartbeat_skip` を設定します。フラグが立っている間、タイマーのコールバックは進捗チェックをスキップします — 待機中の変化しない `(iter, tool)` ペアはスタイルではありません。フラグは after-tool フックでクリアされ、`before_agent` でリセットされます。
- タイマーのコールバックは `(heartbeat_iter, heartbeat_tool)` を `_last_heartbeat_iter` / `_last_heartbeat_tool` と比較します。進捗があればスタイルカウンターをリセット、なければインクリメント。アイドル中に `stale_cycles_idle = 7` 回、または同一ツール内に停滞して `stale_cycles_in_tool = 20` 回に達すると `heartbeat_killed = True` となり、次のモデル / ツール呼び出しは続行の代わりに `HeartbeatTimeoutError` を送出します。
- `after_agent` はタイマーを停止します。
- 状態キー：`heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`heartbeat_skip`、および `_last_heartbeat_iter` / `_last_heartbeat_tool`。

### HumanInTheLoop

**モジュール：** `agent/middlewares/humanInTheLoop/core.py` · **クラス：** `HumanInTheLoop(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`after_model` / `aafter_model`、`wrap_tool_call` / `awrap_tool_call`

メインエージェントには `HumanInTheLoop(HITLConfig())` として登録 — すべてデフォルト、つまりモード `ApprovalMode.SMART`。各モデル応答の後にツール呼び出しをインターセプトし、ポリシーが要求する場合は LangGraph ネイティブの `interrupt()` でグラフを一時停止して、フロントエンドに承認ダイアログを表示させます。拒否された呼び出しはエラー `ToolMessage`（`BLOCKED_MESSAGE`）に置き換えられ、`GraphInterrupt` は握りつぶされずに再スローされます。

`after_model` での呼び出しごとのパイプライン：

1. ハードライン / 危険コマンド検知（`detection.py`：`detect_hardline_command`、`detect_dangerous_command`、基盤は `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS`）を `ApprovalPipeline.check_command`（`approval.py`）経由で実施。
2. スマート承認（`ApprovalMode.SMART`、任意の `smart_approval_llm`）— 明らかに安全な呼び出しを自動承認。
3. `interrupt()` — 既定の決定タイムアウトは 60 秒。
4. `write_approval_memory=True` の場合、メモリツールの書き込みは `WriteApprovalGate` を通過。`interrupted_tools` に列挙されたツールは常に中断され、決定は `approve` / `edit` / `reject`（`edit` はツール呼び出しの引数/名称を書き換えます）。
5. `wrap_tool_call` は承認が拒否またはタイムアウトした呼び出しの実行を拒否します（ターン単位のフラグは `before_agent` でリセット）。

サブゲート（`gates.py` / `approval.py`）：`ApprovalPipeline`、`WriteApprovalGate`、`InterruptManager`、`MCPElicitationConsent`、`KanbanTriage`、`PairingStore`、`SlashConfirm`。状態は `state_register_mem` に `hitl:` 接頭辞キーで格納されます。

`HITLConfig` のデフォルト：

| パラメータ | 既定値 | 意味 |
|---|---|---|
| `mode` | `ApprovalMode.SMART` | `SMART` / `MANUAL` / `OFF` |
| `timeout` | `60` | 中断決定のタイムアウト |
| `deny_rules` | `[]` | 明示的な拒否パターン |
| `yolo_mode` | `False` | すべての承認をスキップ |
| `write_approval_memory` | `False` | メモリツール書き込みのゲート |
| `write_approval_skills` | `False` | スキル書き込みのゲート |
| `clarify_timeout` | `3600` | 澄清質問のタイムアウト |
| `kanban_recurrence_limit` | `3`（`BLOCK_RECURRENCE_LIMIT`） | カンバントリアージ前の反復ブロック上限 |
| `mcp_reload_confirm` | `True` | MCP サーバーリロードの確認 |
| `destructive_slash_confirm` | `True` | 破壊的スラッシュコマンドの確認 |
| `smart_approval_llm` | `None` | スマート自動承認に使う LLM |
| `interrupted_tools` | `{}` | 常に `interrupt()` を起こすツール |
| `description_prefix` | `"Action requires human approval"` | 承認ダイアログ見出しの接頭辞 |

▶️ 詳細：[humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### LLMRetryMiddleware

**モジュール：** `agent/middlewares/llm_retry.py` · **クラス：** `LLMRetryMiddleware(AgentMiddleware)`（他に `LLMRetryConfig`、`FallbackCandidate`、`ContentFilterError`）
**フック：** `wrap_model_call` / `awrap_model_call` のみ

メインエージェントには **`HumanInTheLoop` と `Summarization` の間**で登録されます：`MaxTokensBoostMiddleware` に対しては内側（ブースト再呼び出しを経ても残った本当の切断だけを目にする）、Summarization に対しては外側（リトライループが T4/T5 オーバーフローリカバリリングを外側から包む）。ワーカーパイプラインには登録されません。状態に `session_id` がない場合は素通しです。

各 handler 呼び出しは、`pub/func/message/llm_error_classifier.py` に基づく「分類 → 処置」ループを通ります — `FailoverReason` 列挙型（18 種）、`ClassifiedError` 判定（`retryable` / `should_compress` / `should_fallback` フラグ）、8 段階優先度パイプライン `classify_api_error` — そして `pub/func/retry_utils.py::jittered_backoff` でバックオフします。

**`FailoverReason` ごとのリトライセマンティクス**

| クラス | 理由 | 処置 |
|---|---|---|
| ジッター付きバックオフでリトライ（`retryable=True`） | `auth`、`rate_limit`、`overloaded`、`server_error`、`timeout`、`image_too_large`、`invalid_response`、`unknown` | 最大 `max_retries` 回の再呼び出し。遅延 = `base_delay × 2^(attempt−1)` を `max_delay` で上限切りし、±`jitter`、`[0.1, max_delay]` にクランプ |
| 圧縮委譲（`should_compress=True`） | `context_overflow`、`payload_too_large` | 即座に再スロー — オーバーフローエラーは Summarization の T4/T5 リカバリリングが担当 |
| フォールバック（`should_fallback=True`、非リトライ） | `auth_permanent`、`billing`、`upstream_rate_limit`、`ssl_cert_verification`、`model_not_found`、`provider_policy_blocked`、`content_policy_blocked` | 次のフォールバック候補へ切替。チェーン尽きで再スロー |
| ハードフェイル | `format_error` | 再スロー（リトライもフォールバックもしない） |

**スタイル連続ブレーカー（ターン間）：** timeout と分類された失敗ごとに — および timeout 分類の部分ストリームスタブ再試行ごとに — セッション単位の `llm_stale_streak`（`state_register_mem` 内）がインクリメントされ、成功した handler 呼び出しがあれば 0 にリセットされます。連続が `stale_giveup_threshold` に達すると、次のモデル呼び出しは LLM を呼ぶ**前に** `RuntimeError("Provider unresponsive — aborting to avoid indefinite stall.")` を送出し、プロバイダーが応答しない状態のスパイラルをターン間で断ち切ります。

**モデルフォールバックチェーン：** `FallbackCandidate(provider, model_name, model)` エントリは、`models/LLMs/main_llm.py::build_fallback_chain()` が環境変数 `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` から構築します（i = 1…、最初に `NAME` が欠けた時点で停止。`PROVIDER` のデフォルトは `openai`。クライアントを構築できない候補は警告付きでスキップ）。1 起点のアクティブインデックスはセッション単位で `llm_fallback_index` に粘着的に保持され、毎回のモデル呼び出しの開始時に `request.override(model=...)` で既に活性化済みの候補へリバインドされ、フォールバック分類の失敗（またはコンテンツフィルタフラグ）で次の候補が活性化します。`FALLBACK_LLM_*` が未設定（デフォルト）の場合、このミドルウェアは単なる有界リトライループです。

**コンテンツフィルタフラグの消費：** ストリーム層（`server/service/stream_dispatch.py`）は `llm_content_filter_blocked`（明示的な `finish_reason == "content_filter"`）または `llm_content_filter_terminated`（ストリーム途中の安全カット）を設定します。ミドルウェアは毎回の handler 呼び出し後 — 成功でも分類済み例外でも — 両フラグを確認してクリアし、フォールバックモデルへリバインドするか、候補が残っていなければ `ContentFilterError("Model declined to respond (safety refusal).")` を送出します。`content_policy_blocked` は決してリトライしません。

**部分ストリームスタブの消費：** ストリーム途中のネットワーク切断後、ストリーム層は `llm_partial_stream_stub` と `llm_partial_stream_cause`（保持された `FailoverReason` 値、デフォルトは `timeout`）を設定します。ミドルウェアは成功した handler 呼び出し後にフラグを消費します：切断された結果は破棄され、handler がバックオフ後に新しい試行として一度だけ再呼び出しされます — より大きい max_tokens でのブーストは決してありません。ストリーミングターン（`is_stream_turn` フラグ）では、再呼び出し前に `request.config["callbacks"]` を剥離し `finally` で復元します（MaxTokensBoost の strip → call → restore 契約）。すでにストリーミング済みのトークンが重複しません。timeout 分類の原因はスタイル連続を増やします。リトライ予算が尽きた場合、ミドルウェアは穏当に縮退し現在の（部分的な）結果を返します。

**状態キー（すべて `state_register_mem` 内）：** `llm_stale_streak`、`llm_fallback_index`（ここで所有）。`llm_content_filter_blocked`、`llm_content_filter_terminated`、`llm_partial_stream_stub`、`llm_partial_stream_cause`（ストリーム層が書き込み、ここで消費）。

### Summarization

**モジュール：** `agent/middlewares/summarization.py` · **クラス：** `Summarization(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`（カウンターリセット）、`wrap_model_call` / `awrap_model_call`

最内層のミドルウェア — LLM に最も近い位置。スクラッチで実装された `AgentMiddleware` です（LangChain の `SummarizationMiddleware` **ではありません**）：トリガーが発火すると、予算ベースのカットオフで履歴を圧縮します — 非 LLM 戦略を優先し、テキスト劣化が安全な場合にのみ補助 LLM による要約を使用。`keep` パラメータは受け付けますが未使用で、末尾保持は予算ベースです：`clamp(context_window × 0.25, 2 000, 15 000)` トークン（`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`）。

- **ライフサイクルとルーティング：** ミドルウェアは 5つのトリガーポイント（T1–T5）を網羅します — T1 事前点検（`before_agent` / `abefore_agent`）、T2 呼び出し前ディスパッチ（`wrap_model_call` / `awrap_model_call`）、T3 応答後の再確認（実際の報告トークン）、T4（413 Payload Too Large）/ T5（コンテキストオーバーフロー）エラー復帰リング — どのトリガーも 4ルート・オーバーフロー判定（truncate / compact / both / pass）を実行し、`pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/tool_args_truncate.py`（ツール呼び出し引数の切り詰め）、`pub/func/message/llm_error_classifier.py` に委譲します。状態はセッション単位の `summarization_*` キー（計 14 個、ターンごとに 10 個をリセット）に保持されます。詳細は下記のリンクを参照。
- **トリガーセマンティクス**：節は `("messages", N)` または `("tokens", N)` で、節リスト間は **OR** — いずれかの節が発火すると圧縮が始まります。メインエージェント：`[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。ワーカー：`[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。`COMPRESSION_TRIGGER_RATIO = 0.80`。
- **カットオフの安全性：** `_determine_cutoff` がカットオフ位置を選び、続いて `_adjust_for_orphan_pairs` が `ToolMessage` が自身の `AIMessage` ツール呼び出しから分離されなくなるまで位置を手前に戻します。最後のユーザーターンが推定トークンの ≥ 50 % を占める場合（`LAST_TURN_RATIO_THRESHOLD = 0.5`）、そのターンを要約で消すのではなく、ターン自体を圧縮します（`self._compress_last_turn` フラグ）。
- **アンチスラッシング：** 1 セッションあたり最大 `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` 回の圧縮（ターンごとではない）。連続 `INEFFECTIVE_THRESHOLD = 2` 回の無効な圧縮で（有効 = メッセージ数の減少、またはトークン削減 ≥ `MIN_EFFECTIVENESS_PCT = 0.05`）、LLM ステップを無効化（`summarization_skip_llm`）し非 LLM 戦略のみを実行します。カウンターはセッション単位の `summarization_*` キーとして `state_register_mem` に保持されます（圧縮回数、無効連続回数、直近トークン、直近戦略、スキップフラグ、リカバリ状態など）。
- **切り詰め：** 既存の要約メッセージ（`additional_kwargs["lc_source"] == "summarization"` で識別）が `SUMMARY_TOTAL_MAX_CHARS = 16 000` 文字を超えると再切り詰めされ、先頭 30 % / 末尾 30 %（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）を保持し省略マーカーが入ります。
- **出力：** 置換後のメッセージは `HumanMessage` / `AIMessage` の**ペア**です — 中立的な `"What did we do so far?"` に続き、`additional_kwargs={"lc_source": "summarization"}` を持つ `AIMessage` が続きます — モデルが連続した同役割メッセージを見ることはなく、事後のペア修復も不要です。
- `need_update_system_prompt=True`（メインエージェントのみ）：圧縮後にシステムプロンプトを再構築 — メモリストアを再読み込みして `build_system_prompt()` を呼び — `system_prompt` キーで両方の状態レジスタに書き戻します。

▶️ 詳細：[docs/harness/summarization/README.md](../../docs/harness/summarization/README.md) · [中文](../../docs/harness/summarization/README.zh.md) · [한국어](../../docs/harness/summarization/README.ko.md) · [日本語](../../docs/harness/summarization/README.ja.md)

> 予算ミドルウェアのクラス既定値 `max_iterations` は 50 です。*登録されている* 値は 90（メイン）と 60（ワーカー）。本ドキュメントの旧版は予算 10 と主張していました — 誤りです。

### MaxTokensBoostMiddleware

**モジュール:** `agent/middlewares/max_tokens_boost.py` · **クラス:** `MaxTokensBoostMiddleware(AgentMiddleware)`
**フック:** `wrap_model_call` / `awrap_model_call`

**ツール呼び出し切断からの復旧**：モデル呼び出しが `finish_reason == "length"`
（OpenAI）/ `stop_reason == "max_tokens"`（Anthropic）で返り、レスポンスにツール
呼び出しが含まれる場合、ツール呼び出し JSON 自体が切断されています。ミドルウェアは
`wrap_model_call` / `awrap_model_call` 内で `max_tokens = base × 2^attempt`
（上限 32768、最大 3 リトライ）に増やして handler を再呼び出しし、完全なツール
呼び出しペイロードを生成させます。base は 3 層で解決されます（最初の正の値を採用）：

1. 呼び出し自身の `request.model_settings["max_tokens"]` —— 現在の呼び出しの実際の
   上限。呼び出しがより高い値で設定されていても、再呼び出しが低いデフォルトから
   やり直すことはありません；
2. 環境変数 `MAIN_LLM_OUTPUT_MAX_TOKEN`（デフォルト 8192）；
3. ハードコードされたデフォルト 8192。

切断された中間結果は破棄され——agent ループには最終結果だけが見えるため、切断内容が
checkpointer に書き込まれることはなく、IterationBudget は外側のモデル呼び出し毎に
1 回だけ加算されます。

- **テキストのみの切断**（ツール呼び出しなし）はここでは扱いません——サービス層の
  `StreamTurn` 外側ループ（継続 HumanMessage の注入）が担当します。
- **ストリーミング再呼び出しは callbacks を剥離**：最初の呼び出しの切断トークンは
  すでにクライアントへ送信済みのため、再呼び出し前に
  `request.config["callbacks"]` を取り除いて重複出力を防ぎ、`finally` で元の
  callbacks を復元します（例外時も含む）。ストリーミング/非ストリーミングの判定は
  `StreamTurn.run()` がセッション毎に設定する `is_stream_turn` フラグを読み——
  子エージェント（ainvoke）はこのフラグを持たず、常に非ストリーミング経路を通ります。
- `_extract_ai_message` は素の `AIMessage` と `.messages` を持つ `ModelRequest`
  形式レスポンスの両方を処理します。
- **思考予算との相互作用：** `MAIN_LLM_ENABLE_THINKING=true` の場合、
  `models/LLMs/main_llm.py::apply_thinking_budget` がリクエストの `max_tokens` を
  事前に `OUTPUT_MAX_TOKEN + 思考予算` まで膨らませます（本ミドルウェアと
  `MAIN_LLM_OUTPUT_MAX_TOKEN` 環境変数 / 8192 デフォルトを共有しているため）、
  レイヤー 1 のブースト base は既に思考膨張後の出力上限から始まります。
- **サービス層の診断：** ストリーム失敗時、`server/service/stream_diag.py` が
  チャンク数/バイト数と初回チャンクまでの時間を計上し、再スローされる例外に
  サマリを添付します — 上記のミドルウェアレベル復旧を補う可観測性です。

### OutputRepetitionGuard と RepetitionGuardWrapper

**モジュール：** `agent/middlewares/output_repetition_guard.py` · **クラス：** `OutputRepetitionGuard(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`

事後型の出力繰り返し検知器で、`WARN → HALT` エスカレーションを持ちます。`agent.middlewares.output_repetition_guard` からエクスポートされ、`agent/middlewares/__init__.py` からも再エクスポートされています。メインエージェント（呼び出しごとのインターセプト、下記ラッパーの補完）とワーカーパイプラインの**両方**に登録されています。

メインエージェントでは同じ検知が **`RepetitionGuardWrapper`**（`agent/stream_repetition_guard_wrapper.py`）を通じて実行されます。これはコンパイル済みグラフをラップし、ストリームレベルでインターセプトし（`ainvoke` の事後バックストップ付き）、同じ状態キーとデフォルトを再利用します。どちらの登録も `phantom_stream_guard=True` を渡します。

**検知レイヤー**

- **呼び出し間の繰り返し** — 可視出力の末尾 `_TAIL_CHARS = 500` 文字の MD5 を、ローリング履歴（`_MAX_HISTORY = 30`）と比較。`warn_after = 2` 件の同一出力で WARN（`AIMessage` で注意喚起）、`max_identical_outputs = 3` で HALT — 終端 `AIMessage` と粘着性の停止フラグを返します。
- **単一出力内の繰り返し**：
  - 文/行の重複率 > `internal_repeat_ratio = 0.6`（セグメント数 ≥ `internal_min_lines = 6`）；
  - ≥ `char_run_min = 8` 個の同一の空白以外の文字の連なり；
  - 2–10 文字の短いフレーズが ≥ 5 回の繰り返し。

  内部警告はラベルごとにセッションで 1 回だけ発火します。
- `_MIN_CONTENT_LENGTH = 20` 文字未満の内容はスキップ。ツール呼び出しを含むモデル応答は丸ごとスキップします（ツールループの後に再チェックされます）。
- **推論内容は別個に追跡**されます（`additional_kwargs` の `reasoning_content` / `reasoning` / `reasoning_text`、および可視内容から抽出・剥ぎ取られるインラインの `<think>` / `<thinking>` / `<reasoning>` ブロック）。

**ストリーム層ヘルパー** `check_stream_repetition(session_id, accumulated_text)` — 共有の `_STREAM_GUARD` シングルトンで、`server/service/messages.py::async_generate` が繰り返し検知時にストリーミング応答を途中で切断するために使用します。同じ状態キーと内部警告の重複排除ゲートを共有します。

**ワーカーのクリーンアップ：** 子セッション終了時、`SESSION_STATE_KEYS`（6 つのキー）が `state_register_mem` から削除されます。

### ContextLimitGuardWrapper

**モジュール：** `agent/context_limit_guard_wrapper.py` · **クラス：** `ContextLimitGuardWrapper`

`RepetitionGuardWrapper` と同種のグラフラッパーであり、ミドルウェア**ではありません**。`agent/core.py` では `RepetitionGuardWrapper` の**外側**からコンパイル済みエージェントをラップし（`agent → RepetitionGuardWrapper → ContextLimitGuardWrapper`）、繰り返しフィルタリングの前にストリームチャンクを目にします。ミドルウェアのストリーミング盲点を塞ぎます：ミドルウェアはストリーム途中のチャンクを見ず、応答後のオーバーフロー信号で後からコンテキストを圧縮することもできません。

**防御 1 — モデル呼び出し境界での強制圧縮：** 実際の `usage_metadata` 入力/出力トークンを `messages` チャンクから捕捉し、モデル呼び出し境界ごと（`updates` モード）とストリーム終端で、現在の呼び出し（`input_tokens` のみ）と予測ビュー（`input + output`、出力は次の呼び出しの入力になるため）の両方をコンテキストウィンドウの `COMPRESSION_TRIGGER_RATIO`（80 %）に対してチェックします。しきい値に達したら、Summarization の強制リカバリキー（`summarization_force_recovery`）を `state_register_mem` に設定し、次の呼び出し前チェックがクールダウン/試行上限のアンチスラッシングゲートに阻まれずに圧縮を実行するようにします。

**防御 2 — ストリーム途中の出力予算：** 蓄積されたモデルテキストを `_CHARS_PER_TOKEN = 4` 文字/トークンで推定し、`check_interval = 20` チャンクごとにチェックします。推定値がウィンドウの `output_cut_ratio = 0.20` を超えると、以降のテキストチャンクはクライアントへ転送されなくなり（ツール呼び出しチャンクは通過）、代わりに 1 回限りの切り詰めマーカー（`"[System notice: the response exceeded the mid-stream output budget and was truncated.]"`）が出力されます。グラフ内部では完全な `AIMessage` が蓄積され続けます — 切り詰められた末尾こそ、次の圧縮パスが除去する対象です。

`ainvoke` はそのまま委譲します（非ストリーミング経路は Summarization の T1–T3 トリガーが既にカバー）。未知の属性は内部グラフに委譲されます。コンストラクタ：`(inner, context_window, output_cut_ratio=0.20, check_interval=20)` — `context_window` は `MAIN_LLM_MAX_TOKEN` で、Summarization のトリガーと同じソースです。

---

## 共有状態システム

呼び出しをまたぐすべてのミドルウェア状態はセッション単位で、2 つのレジスタとタイマーレジストリに保持されます：

| レジスタ | バックエンド | 備考 |
|---|---|---|
| `state_register_mem`（`StateRegisterMeM`） | インメモリ辞書 | 揮発性。`_initialized` ガードによりプロセス開始時に 1 回だけリセット |
| `state_register_db`（`StateRegisterDB`） | SQLite（`src/data/state_register.db`） | 再起動後も保持。`clear_session` は非対応（`False` を返す）。`get_all_session_ids` を提供 |
| `timer_call_register`（`TimerCallRegister`） | asyncio タイマー | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

共通インターフェース（`runtime/state_register.py`）：`set_state`、`get_state`、`get_all_states`、`delete_state`、`clear_session`、`has_session`、`has_key`、`update_states`。

### 名前空間の規約

| キー | 所有者 | レジスタ |
|---|---|---|
| `system_prompt` | ContextEngineHook / Summarization | mem + db |
| `nudge_review_memory_count`、`nudge_review_skill_count` | ContextEngineHook | db |
| `nudge_review_memory_lock`、`nudge_review_skill_lock` | ContextEngineHook | mem |
| `iteration_budget`、`iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_*` キー（圧縮カウンター、無効連続、直近トークン/戦略、スキップ LLM フラグ、リカバリ状態、直近ユーザー質問） | Summarization | mem |
| `heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`heartbeat_skip`、`_last_heartbeat_iter`、`_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard のキー（`SESSION_STATE_KEYS`、6 つ） | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `llm_stale_streak`、`llm_fallback_index` | LLMRetryMiddleware | mem |
| `llm_content_filter_blocked`、`llm_content_filter_terminated` | ストリーム層（書き込み）→ LLMRetryMiddleware（消費） | mem |
| `llm_partial_stream_stub`、`llm_partial_stream_cause` | ストリーム層（書き込み）→ LLMRetryMiddleware（消費） | mem |
| `summarization_force_recovery` | ContextLimitGuardWrapper（書き込み）→ Summarization（消費） | mem |
| `hitl:` 接頭辞キー（`_STATE_PREFIX = "hitl"`） | HumanInTheLoop | mem |

---

## 設定

### 環境変数と設定ノブ

| ノブ | 場所 | 効果 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | メインエージェントの要約トリガー = この値の 80 %。`main_llm_context_window` としても `ContextLimitGuardWrapper.context_window` としても渡す |
| `MAIN_LLM_OUTPUT_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 出力トークン予算（デフォルト 8192）：MaxTokensBoost ブースト base のレイヤー 2、思考予算膨張が加算されるベース |
| `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` | `.env` → `build_fallback_chain()` | `LLMRetryMiddleware` のモデルフォールバックチェーン候補（i = 1…、最初に `NAME` が欠けた時点で停止） |

> **関連だが独立：** ツールごとのタイムアウトはハードコードされたモジュール定数です — `WEB_SEARCH_TIMEOUT = 15`（`agent/tools/web_search.py`）、`TERMINAL_TIMEOUT = 30`（`agent/tools/terminal.py`）、`PYTHON_REPL_TIMEOUT = 30`（`agent/tools/python_repl.py`。期限切れで子プロセスは kill されます）。`.env.example` の `TOOL_CALL_TIMEOUT_MINUTES = 5` は**これを消費するコードが存在しません** — 有効なノブではありません。これらのレガシー定数は以前 `config/num.py` にありました（現在は削除済み）。要約パイプラインは `config/features/agent_side/summarization.py` からそれらを読み取ります。

### ビルド例

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook,
    MultimodalProcessor,
    IterationBudget,
    ToolGuardrails,
    ToolCallNormalize,
    HeartbeatStaleness,
    HumanInTheLoop,
    HITLConfig,
    MaxTokensBoostMiddleware,
    Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),  # システムプロンプト + nudge + 永続化
        MultimodalProcessor(),  # マルチモーダル入力の正規化
        IterationBudget(90),  # ターン単位の呼び出し予算
        ToolGuardrails(),  # 失敗病理の検知
        ToolCallNormalize(),  # tool_use/tool_result の修復
        HeartbeatStaleness(),  # スタックターンのウォッチドッグ
        HumanInTheLoop(HITLConfig()),  # 承認ゲート
        Summarization(  # コンテキスト圧縮（最内層）
            need_update_system_prompt=True,
            model=auxiliary_llm,
            main_llm_context_window=main_llm_max_tokens,
            trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
            keep=("messages", 10),
        ),
    ],
)
```

### ミドルウェアごとのパラメータ

| ミドルウェア | パラメータ | 既定値 | 登録値 |
|---|---|---|---|
| `IterationBudget` | `max_iterations` | `50` | `90`（メイン）/ `60`（ワーカー） |
| `Summarization` | `need_update_system_prompt` | `False` | `True`（メイン） |
| `Summarization` | `model` | 必須 | `auxiliary_llm` |
| `Summarization` | `main_llm_context_window` | 必須 | `main_llm_max_tokens` |
| `Summarization` | `trigger` | 必須 | [ミドルウェアチェーン](#ミドルウェアチェーン)を参照 |
| `Summarization` | `keep` | 必須 | `("messages", 10)`（受け付けるが未使用） |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | 上記の既定値 | 既定値 |
| `HumanInTheLoop` | `config: HITLConfig` | 上記の既定値 | 既定値 |
| `HeartbeatStaleness` | （既定） | 間隔 1 分、アイドル 7 / ツール内 20 | 既定値 |
| `OutputRepetitionGuard` | （既定） | 3 / 2 / 0.6 / 6 / 8 | 既定値 |
| `MaxTokensBoostMiddleware` | （環境変数） | base：リクエストの `max_tokens` → `MAIN_LLM_OUTPUT_MAX_TOKEN`（8192）→ 8192、上限 32768、3 リトライ | 既定値 |
| `LLMRetryMiddleware` | `config: LLMRetryConfig` | `max_retries=3`、`base_delay=2.0`、`max_delay=60.0`、`jitter=0.3`、`stale_giveup_threshold=5` | 既定値（＋ `FALLBACK_LLM_*` 由来の `fallback_chain`） |

---

## ライフサイクルとデータフロー

### 単一ターン（詳細）

```
ユーザーターン到着
│
├─ before_agent（リスト順）
│   ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails
│   → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · ContextEngineHook   ここでは何もしない（永続化は after_agent で実施）
│   · MultimodalProcessor  最後の HumanMessage を正規化、古い image_url ブロックを剥離
│   · IterationBudget  予算カウンターをリセット
│   · ToolGuardrails  ターン単位のガード状態をリセット
│   · HeartbeatStaleness  状態キーをリセット + 1 分間隔のハートビートタイマーを起動
│   · HumanInTheLoop  ターン単位の中断フラグをリセット
│   · Summarization  圧縮カウンターをリセット
│
├─ ループ：モデル呼び出し
│   ├─ before_model
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage 書き換え
│   ├─ wrap_model_call（最外層 → 最内層）
│   │   · ContextEngineHook  システムプロンプトを注入（request.override）
│   │   · IterationBudget  1 消費。尽きたら終端 AIMessage
│   │   · HeartbeatStaleness  kill 済みなら HeartbeatTimeoutError、さもなくば heartbeat_iter += 1
│   │   · LLMRetryMiddleware  ブレーカー確認。分類済みリトライ + バックオフ。フォールバック /
│   │                        コンテンツフィルタ / 部分ストリームスタブフラグの消費
│   │   · Summarization  必要なら履歴を圧縮（非 LLM 戦略 + 補助 LLM）、アンチスラッシングカウンター
│   ├─ LLM が応答
│   └─ after_model
│       · HumanInTheLoop  ポリシーチェック。必要なら interrupt()。ブロック → エラー ToolMessage
│
├─ ループ：ツール呼び出し（呼び出しごと）
│   └─ wrap_tool_call
│       · IterationBudget  1 消費。尽きたらエラー ToolMessage
│       · ToolGuardrails  block/halt を事前チェック → 実行 → 評価 → warn/block/halt
│       · ContextEngineHook  スキルレビューカウンター（ツールメタデータ nudge: true を除く）
│       · HeartbeatStaleness  kill 済みなら送出。heartbeat_tool を設定し、返却後にクリア
│       · HumanInTheLoop  承認が拒否/タイムアウトした呼び出しを拒否
│
└─ after_agent（逆順）
    Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize
    → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook
    · HeartbeatStaleness  ハートビートタイマーを停止
    · MultimodalProcessor  mutil_temp を清掃（7 日超 / 非数値ファイル名）
    · ContextEngineHook  メモリレビューカウンター → 必要なら nudge サブエージェント（ロック）
                        → 最終ターンを MesMemory に永続化（slice → sanitize → add_messages）
```

---

## カスタムミドルウェアの作成

`AgentMiddleware` を継承し、必要なフックだけをオーバーライドします（シグネチャはインストール済み `langchain 1.3.9` より — 状態フックは `(state, runtime)`、ラップフックは `(request, handler)` を受け取ります）：

```python
from langchain.agents.middleware import AgentMiddleware


class MyMiddleware(AgentMiddleware):
    """ターンごとに、ループの前後に 1 回ずつ実行。"""

    def before_agent(self, state, runtime):
        # 状態更新辞書を返すか、None
        return None

    def after_agent(self, state, runtime):
        return None

    def wrap_model_call(self, request, handler):
        # `request` を検査/改変し、`handler(request)` に委譲
        return handler(request)

    def wrap_tool_call(self, request, handler):
        return handler(request)
```

非同期バリアントは `a` 接頭辞の規約に従います：`abefore_agent`、`aafter_agent`、`awrap_model_call`、`awrap_tool_call` など。ラップフックは軽量で副作用の少ない実装に保ってください — **すべての**モデル/ツール呼び出しで実行され、このコードベースでは最初に登録されたミドルウェアが最外層のラップになります。

---

## 付録

### ファイルレイアウト

```
agent/middlewares/
├── __init__.py                  # 公開エクスポート
├── base.py                      # require_session_id / args_hash ヘルパー
├── mixins.py                    # BeforeAgentHooksMixin / AfterAgentHooksMixin
├── context_engine/              # ContextEngineHook + nudge サブエージェント
│   ├── __init__.py              # ContextEngineHook のみエクスポート
│   ├── core.py                  # ContextEngineHook
│   └── nudge.py                 # nudge プロンプト + サブエージェントビルダー
├── heartbeat_staleness.py       # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig（独自の README を持つ）
│   ├── __init__.py              # HumanInTheLoop、HITLConfig をエクスポート
│   ├── types.py                 # 列挙型 + 設定データクラス（_STATE_PREFIX = "hitl"）
│   ├── detection.py             # ハードライン / 危険コマンドパターン
│   ├── approval.py              # ApprovalPipeline
│   ├── gates.py                 # WriteApprovalGate、InterruptManager、MCPElicitationConsent、
│   │                            # KanbanTriage、PairingStore、SlashConfirm
│   └── core.py                  # HumanInTheLoop
├── iteration_budget.py          # IterationBudget
├── llm_retry.py                 # LLMRetryMiddleware（LLMRetryConfig、FallbackCandidate、ContentFilterError を含む）
├── max_tokens_boost.py          # MaxTokensBoostMiddleware（ツール呼び出し切断の再呼び出し）
├── media_handlers.py            # MultimodalProcessor のメディアタイプ別戦略
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard（__init__.py が再エクスポート）
├── repetition_detectors.py      # 純粋な繰り返し検知プリミティブ
├── repetition_state.py          # セッション単位の繰り返し状態ヘルパー
├── subagent_completion_drain.py # SubagentCompletionDrainMiddleware
├── summarization.py             # Summarization
├── summarization_components.py  # Summarization 共有コンポーネント（_FORCE_RECOVERY_KEY など）
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # このファイル（+ .zh / .ja / .ko 版）

agent/stream_repetition_guard_wrapper.py   # RepetitionGuardWrapper（本パッケージの外に存在）
agent/context_limit_guard_wrapper.py       # ContextLimitGuardWrapper（本パッケージの外に存在）
```

### エクスポート（`__init__.py`）

```python
from agent.middlewares import (
    Summarization,
    LLMRetryMiddleware,
    LLMRetryConfig,
    FallbackCandidate,
    ContentFilterError,
    OutputRepetitionGuard,
    MaxTokensBoostMiddleware,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# 共有ヘルパーもエクスポートされています：BeforeAgentHooksMixin、
# AfterAgentHooksMixin、require_session_id、args_hash。
```
