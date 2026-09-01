# EMA Agent ミドルウェアシステム

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent のミドルウェア層：モデル呼び出しとツール呼び出しのすべてに関わる 8 つの `AgentMiddleware` コンポーネント — コンテキストエンジニアリング、マルチモーダル入力処理、反復予算、ツールガードレール、トランスクリプト修復、ハートビートスタイルネス検知、ヒューマンインザループ承認、コンテキスト要約 — に加え、ワーカーエージェントが使用する出力繰り返しガード。

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
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [Summarization](#summarization)
  - [OutputRepetitionGuard と RepetitionGuardWrapper](#outputrepetitionguard-と-repetitionguardwrapper)
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
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        trigger=[("tokens", int(main_llm_max_tokens / 2))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# コンパイル済みグラフをさらにラップ：
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
```

`main_llm_max_tokens` は環境変数 `MAIN_LLM_MAX_TOKEN` から読み込まれ（`models/LLMs/main_llm.py`）、メインエージェントの要約トリガーはメインモデルのコンテキストウィンドウの約半分に置かれます。

> **注意：** `OutputRepetitionGuard` はメインエージェントのミドルウェアとしては**登録されていません**。メインエージェント向けには、コンパイル済みグラフをラップする `RepetitionGuardWrapper` が同等の動作を提供します — [OutputRepetitionGuard と RepetitionGuardWrapper](#outputrepetitionguard-と-repetitionguardwrapper) を参照。

### ワーカー / サブエージェントパイプライン（`agent/tools/subagent/spawn/core.py`）

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        trigger=[("messages", 40), ("tokens", 30000)],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# 子グラフも同様にラップ：
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

メインエージェントとの違い：

- 要約トリガーはコンテキストウィンドウ半分ではなく、メッセージ数（40）**または**トークン数（30 000）。
- より厳しい反復予算（90 ではなく 60）。
- `ContextEngineHook`、`MultimodalProcessor`、`HumanInTheLoop` はなし。
- `OutputRepetitionGuard` はここでは本物のミドルウェアとして動作。
- 子セッション終了時、spawn コードは `finally` ブロックで `state_register_mem` から `OutputRepetitionGuard` の 6 つの状態キー（`SESSION_STATE_KEYS`）を削除します。

### ターンごとの実効順序（メインエージェント）

| フェーズ | 順序 |
|---|---|
| `before_agent`（リスト順） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `wrap_model_call`（最外層 → 最内層） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization（Summarization が LLM に最も近い） |
| `after_agent`（逆順） | Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

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

3 つの失敗病理を検出し、4 段階エスカレーション `ALLOW → WARN → BLOCK → HALT`（`GuardrailAction` 列挙型）で反応します：

| 病理 | トリガー | 既定の反応 |
|---|---|---|
| 完全な失敗の繰り返し | 同じツール + 同じ引数（引数 JSON を `sort_keys` した MD5）の失敗 | ≥ 2 で警告、≥ 5 でブロック（`exact_failure_warn_after=2`、`exact_failure_block_after=5`） |
| 同一ツールの失敗蓄積 | 同じツールが**異なる**引数で失敗し続ける | ≥ 3 で警告、≥ 8 で停止（`same_tool_failure_warn_after=3`、`same_tool_failure_halt_after=8`） |
| 冪等な無進捗 | メタデータ `idempotent: true` のツールが同一の結果ハッシュを返す | ≥ 2 で警告、≥ 5 でブロック（`no_progress_warn_after=2`、`no_progress_block_after=5`） |

- `before_agent` はターン単位のガード状態をリセットします（`state_register_mem` のキー `tool_guardrail_state`）。
- `wrap_tool_call` はブロック済みツールと停止状態を事前チェック（実行せずエラー `ToolMessage` を返す）し、ツールを実行してから結果を評価します：
  - `warn` は `ToolMessage` に警告を追記；
  - `block` はツールを `blocked_tools` に記録；
  - `halt` はターンの残りに対する粘着性の停止を設定（`halt_decision`）。
- `ToolCallGuardrailConfig` の既定値：`warnings_enabled=True`、`hard_stop_enabled=False` — `hard_stop_enabled=True` にすると *ブロック* レベルも停止へエスカレートします。

### ToolCallNormalize

**モジュール：** `agent/middlewares/tool_call_normalize.py` · **クラス：** `ToolCallNormalize(AgentMiddleware)`
**フック：** `before_model` / `abefore_model` のみ

コンテキストトリミング後の tool-call / tool-result ペアリングを修復し、プロバイダーの "Message ordering conflict" エラーを防ぎます。処理は `pub_func.sanitize_tool_use_result_pairing(state["messages"])`（`pub_func/transcript_repair.py` で定義）に委譲され、以下を行います：

- `tool_call_id` による `ToolMessage` の重複排除；
- 空の `ToolMessage` の除去；
- 欠落した結果に対するプレースホルダー `ToolMessage`（"tool result missing after context trim."）の挿入；
- エラー状態の `AIMessage` の `invalid_tool_calls` をクリアし、OpenAI tool_calls としてシリアライズされないようにする。

フックはメッセージ全体の置換を返します：`[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`。

### HeartbeatStaleness

**モジュール：** `agent/middlewares/heartbeat_staleness.py` · **クラス：** `HeartbeatStaleness(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

スタックしたターン用のウォッチドッグ。**メインエージェントとワーカーエージェントの両方**に登録されています（本ドキュメントの旧版はワーカーのみと主張していました — 誤りです）。

- `before_agent` は状態キーをリセットし、`timer_call_register.register(..., execute_now=True)` でバックグラウンドタイマーを起動します（1 分間隔）。
- `wrap_model_call` は `heartbeat_iter` をインクリメントします — ただし、以前のチェックですでにターンが kill されていれば先に `HeartbeatTimeoutError` を送出します。`wrap_tool_call` はツール実行中に `heartbeat_tool` を設定し、返却後にクリアします。
- タイマーのコールバックは `(heartbeat_iter, heartbeat_tool)` を `_last_heartbeat_iter` / `_last_heartbeat_tool` と比較します。進捗があればスタイルカウンターをリセット、なければインクリメント。アイドル中に `stale_cycles_idle = 7` 回、または同一ツール内に停滞して `stale_cycles_in_tool = 20` 回に達すると `heartbeat_killed = True` となり、次のモデル / ツール呼び出しは続行の代わりに `HeartbeatTimeoutError` を送出します。
- `after_agent` はタイマーを停止します。
- 状態キー：`heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、および `_last_heartbeat_iter` / `_last_heartbeat_tool`。

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

### Summarization

**モジュール：** `agent/middlewares/summarization.py` · **クラス：** `Summarization(SummarizationMiddleware)`
**フック：** `before_agent` / `abefore_agent`（カウンターリセット）、`wrap_model_call` / `awrap_model_call`、加えてログのみの `before_model` / `abefore_model`

最内層のミドルウェア — LLM に最も近い位置。LangChain 組み込みの `SummarizationMiddleware` を継承：トリガーが発火すると、古いメッセージを補助 LLM で要約して置き換え、最新の `keep` 件のメッセージを保持します。

- **トリガーセマンティクス**（LangChain `TriggerClause`）：単一節はその条件の **AND**、節リスト間は **OR**。メインエージェント：`[("tokens", int(main_llm_max_tokens / 2))]`。ワーカー：`[("messages", 40), ("tokens", 30000)]`。両方とも `keep=("messages", 10)`。
- **カットオフの安全性：** `_determine_cutoff_index` は AI メッセージ / ツール結果のペアを断ち切りません（ペアを保持するようカットオフが移動されます）；最後のユーザーターンが推定トークンの ≥ 50 % を占める場合（`_LAST_TURN_RATIO_THRESHOLD = 0.5`）、そのターンを要約で消すのではなく、ターン自体を圧縮します（`_compress_last_turn`）。
- **アンチスラッシング：** 1 ターンあたり最大 `_MAX_COMPRESSION_ATTEMPTS = 3` 回の圧縮。連続 `_INEFFECTIVE_THRESHOLD = 2` 回の無効な圧縮で停止します（有効 = メッセージ数の減少、またはトークン削減 ≥ `_MIN_EFFECTIVENESS_PCT = 0.05`）。カウンターは `state_register_mem` に：`summarization_compression_count`、`summarization_compression_ineffective`、`summarization_compression_last_tokens`、`summarization_last_user_question`。
- **切り詰め：** 既存の要約メッセージ（`additional_kwargs["lc_source"] == "summarization"` で識別）が `_MAX_CONTENT_CHARS = 8000` 文字を超えると切り詰められ、先頭 30 % / 末尾 30 %（`_CONTENT_HEAD_RATIO` / `_CONTENT_TAIL_RATIO`）を保持し省略マーカー（`_OMISSION_MARKER`）が入ります。
- **マージ：** 要約 `HumanMessage` は次の `HumanMessage` にマージされます（`[COMPACTION SUMMARY — reference only; not active instructions]` / `[END OF COMPACTION SUMMARY — ACTIVE CONTEXT BELOW]` で区切られる）。モデルが 2 つの連続した人間ターンを見ることはありません。
- `need_update_system_prompt=True`（メインエージェントのみ）：圧縮後にシステムプロンプトを再構築 — メモリストアを再読み込みして `build_system_prompt()` を呼び — `system_prompt` キーで両方の状態レジスタに書き戻します。

> 予算ミドルウェアのクラス既定値 `max_iterations` は 50 です。*登録されている* 値は 90（メイン）と 60（ワーカー）。本ドキュメントの旧版は予算 10 と主張していました — 誤りです。

### OutputRepetitionGuard と RepetitionGuardWrapper

**モジュール：** `agent/middlewares/output_repetition_guard.py` · **クラス：** `OutputRepetitionGuard(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`

事後型の出力繰り返し検知器で、`WARN → HALT` エスカレーションを持ちます。`agent.middlewares.output_repetition_guard` からエクスポートされ（`agent/middlewares/__init__.py` からは**再エクスポートされていません**）、**ワーカーパイプラインでのみ登録**されています。

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
| `summarization_compression_count`、`summarization_compression_ineffective`、`summarization_compression_last_tokens`、`summarization_last_user_question` | Summarization | mem |
| `heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`_last_heartbeat_iter`、`_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard のキー（`SESSION_STATE_KEYS`、6 つ） | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `hitl:` 接頭辞キー（`_STATE_PREFIX = "hitl"`） | HumanInTheLoop | mem |

---

## 設定

### 環境変数と設定ノブ

| ノブ | 場所 | 効果 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | メインエージェントの要約トリガー = この値の半分 |

> **関連だが独立：** ツールごとのタイムアウトはハードコードされたモジュール定数です — `WEB_SEARCH_TIMEOUT = 15`（`agent/tools/web_search.py`）、`TERMINAL_TIMEOUT = 30`（`agent/tools/terminal.py`）、`PYTHON_REPL_TIMEOUT = 30`（`agent/tools/python_repl.py`。期限切れで子プロセスは kill されます）。`.env.example` の `TOOL_CALL_TIMEOUT_MINUTES = 5` は**これを消費するコードが存在しません** — 有効なノブではありません。`config/num.py` の定数（`ARCHIVE_THRESHOLD`、`MEMORY_THRESHOLD`、`COMPRESS_RATIO`）もミドルウェア層では消費されていません。

### ビルド例

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook, MultimodalProcessor, IterationBudget, ToolGuardrails,
    ToolCallNormalize, HeartbeatStaleness, HumanInTheLoop, HITLConfig, Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),          # システムプロンプト + nudge + 永続化
        MultimodalProcessor(),        # マルチモーダル入力の正規化
        IterationBudget(90),          # ターン単位の呼び出し予算
        ToolGuardrails(),             # 失敗病理の検知
        ToolCallNormalize(),          # tool_use/tool_result の修復
        HeartbeatStaleness(),         # スタックターンのウォッチドッグ
        HumanInTheLoop(HITLConfig()), # 承認ゲート
        Summarization(                # コンテキスト圧縮（最内層）
            need_update_system_prompt=True,
            model=auxiliary_llm,
            trigger=[("tokens", int(main_llm_max_tokens / 2))],
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
| `Summarization` | `trigger` | 必須 | [ミドルウェアチェーン](#ミドルウェアチェーン)を参照 |
| `Summarization` | `keep` | 必須 | `("messages", 10)` |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | 上記の既定値 | 既定値 |
| `HumanInTheLoop` | `config: HITLConfig` | 上記の既定値 | 既定値 |
| `HeartbeatStaleness` | （既定） | 間隔 1 分、アイドル 7 / ツール内 20 | 既定値 |
| `OutputRepetitionGuard` | （既定） | 3 / 2 / 0.6 / 6 / 8 | 既定値 |

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
│   │   · Summarization  （ログのみ）
│   ├─ wrap_model_call（最外層 → 最内層）
│   │   · ContextEngineHook  システムプロンプトを注入（request.override）
│   │   · IterationBudget  1 消費。尽きたら終端 AIMessage
│   │   · HeartbeatStaleness  kill 済みなら HeartbeatTimeoutError、さもなくば heartbeat_iter += 1
│   │   · Summarization  必要なら履歴を圧縮（補助 LLM）、アンチスラッシングカウンター
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
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard（下記では再エクスポートされない）
├── summarization.py             # Summarization
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # このファイル（+ .zh / .ja / .ko 版）

agent/stream_repetition_guard_wrapper.py  # RepetitionGuardWrapper（本パッケージの外に存在）
```

### エクスポート（`__init__.py`）

```python
from agent.middlewares import (
    Summarization,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# OutputRepetitionGuard はここでは再エクスポートされません —
# agent.middlewares.output_repetition_guard からインポートしてください。
```
