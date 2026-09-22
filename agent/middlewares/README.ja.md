# EMA Agent ミドルウェアシステム

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent のミドルウェア層：モデル呼び出しとツール呼び出しのすべてに関わる `AgentMiddleware` コンポーネント — コンテキストエンジニアリング、マルチモーダル入力処理、反復予算、ツールガードレール、トランスクリプト修復、ハートビートスタイルネス検知、ヒューマンインザループ承認、モデル境界とツール復帰の二段タイミングでのメッセージ永続化（`MessagePersistenceMiddleware`）、コンテキスト要約、モデルフォールバック付きの分類済み LLM エラーリトライ（`LLMRetryMiddleware`）— に加え、出力繰り返しガードとストリームレベルのグラフラッパー（`RepetitionGuardWrapper`、`ContextLimitGuardWrapper`）。

> 本ドキュメントの記述はすべてソースコードに対して検証済みです（インストール済み `langchain 1.3.9`、`agent/core.py`、`agent/tools/subagent/spawn/core.py`、および `agent/middlewares/` 配下の各モジュール）。以下に登場するクラス名・ファイル名・デフォルト値・状態キーはすべて実在します。

---

## 目次

- [アーキテクチャ概観](#アーキテクチャ概観)
- [ミドルウェアチェーン](#ミドルウェアチェーン)
- [ミドルウェアリファレンス](#ミドルウェアリファレンス)
  - [system_prompt_injection](#system_prompt_injection)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [PathGuard](#pathguard)
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [TaskIntentMiddleware](#taskintentmiddleware)
  - [TodoContinuationEnforcer](#todocontinuationenforcer)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [MessagePersistenceMiddleware](#messagepersistencemiddleware)
  - [ContextEvictionMiddleware](#contextevictionmiddleware)
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

インストール済み `langchain 1.3.9` のソース（`langchain/agents/factory.py` および `langchain/agents/middleware/types.py`）に対して検証済み：

- `before_agent` フックは**リスト順**に実行されます — 最初に登録されたミドルウェアが先に走ります。
- `after_agent` フックは**リスト逆順**に実行されます — 最後に登録されたミドルウェアの `after_agent` が最初に走ります（コンパイル済みグラフの出口ノードチェーンです）。
- `after_model` フックはミドルウェアごとに 1 つのグラフノードへコンパイルされ、**リスト逆順**に連結されます — `model` → `after_model[last]` → … → `after_model[first]`。したがって、このフックを実装する最後のミドルウェアが、モデル応答後に最初に走るフックになります。
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
    # after_agent フックはリスト逆順に走る: 最初に登録された enforcer が
    # ターン終了時に最後に実行され、本当に終わったターンを観測する。
    TodoContinuationEnforcer(),
    system_prompt_injection,  # @dynamic_prompt：システムプロンプト注入
    MultimodalProcessor(),
    IterationBudget(ITERATION_BUDGET["main_agent_max_iterations"]),
    ToolGuardrails(),
    ContextEvictionMiddleware(),
    ToolCallNormalize(),
    PathGuard(),
    SubagentCompletionDrainMiddleware(),
    TaskIntentMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    # after_model ノードは登録逆順に走る: HITL の直後に登録することで、
    # モデル応答後に最初に実行されるフックとなり、HITL が拒否呼び出しを
    # 剥がす / interrupt する前に AI メッセージが永続化される。
    MessagePersistenceMiddleware(),
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
# コンパイル済みグラフは apply_graph_wrappers() がラップする（内側 → 外側）：
# RepetitionGuardWrapper、次に ContextLimitGuardWrapper。
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
- `system_prompt_injection`（`@dynamic_prompt`）、`MultimodalProcessor`、`HumanInTheLoop`、`LLMRetryMiddleware` はなし（子エージェントには分類済みリトライ/フォールバックループがない）。`PathGuard`、`TaskIntentMiddleware`、`TodoContinuationEnforcer` もなし。
- `MessagePersistenceMiddleware` なし：子セッションはクライアント可視の MesMemory 履歴には含まれません —— トランスクリプトはチェックポイントにのみ存在し、親から見える完了キャリアだけが `origin='subagent_completion'` で永続化されます。
- `ContextEvictionMiddleware` なし：子トランスクリプトは完全なツール結果を保持し（退避ファイルも read_file スライスもなし）、巨大な人間メッセージもタグ付け・ビュー切り詰めの対象になりません。
- `OutputRepetitionGuard` はここでは本物のミドルウェアとして動作。
- `MaxTokensBoostMiddleware` は非ストリーミング経路を取ります。子は
  `ainvoke` で実行されるため、子セッション id に `is_stream_turn` フラグが立つことはありません。
- 子セッション終了時、spawn コードは `finally` ブロックで `state_register_mem` から `OutputRepetitionGuard` の 6 つの状態キー（`SESSION_STATE_KEYS`）を削除します。

### ターンごとの実効順序（メインエージェント）

| フェーズ | 順序 |
|---|---|
| `before_agent`（リスト順） | MultimodalProcessor → IterationBudget → ToolGuardrails → OutputRepetitionGuard → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `before_model`（リスト順） | ContextEvictionMiddleware（P1-9：末尾の巨大な HumanMessage にタグ付け。`before_agent` チェーンが先に走るため、メディアヒントは既にテキストへ統合済み） → ToolCallNormalize → SubagentCompletionDrainMiddleware → TaskIntentMiddleware（2 つのインジェクタはいずれもサニタイズ書き換えの後に位置するため、注入ターンでもメッセージが剥がされない） |
| `wrap_model_call`（最外層 → 最内層） | system_prompt_injection → MultimodalProcessor → IterationBudget → ContextEvictionMiddleware → OutputRepetitionGuard → MaxTokensBoostMiddleware → HeartbeatStaleness → LLMRetryMiddleware → Summarization（Summarization が LLM に最も近い。LLMRetry は Summarization の T4/T5 リカバリを外側から包み、MaxTokensBoost の内側に位置するため、本当の切断だけを目にする） |
| `after_model`（逆順） | MessagePersistenceMiddleware → HumanInTheLoop（永続化が先：このフックを実装する最後のミドルウェアであり、自身は fail-open なので、HITL の拒否書き換えも `GraphInterrupt` もフラッシュを飛ばせない） |
| `wrap_tool_call`（最外層 → 最内層） | IterationBudget → ToolGuardrails → ContextEvictionMiddleware → PathGuard → HeartbeatStaleness → HumanInTheLoop → MessagePersistenceMiddleware（最内層でツールに最も近い：返された `ToolMessage` をフラッシュ。HITL の interrupt/拒否短絡はこれを迂回し、それらの拒否は次のモデル境界で永続化される。退避は永続化の外側にあり、原文が先にフラッシュされ、プレビューだけが state へ進む） |
| `after_agent`（逆順） | HeartbeatStaleness → MultimodalProcessor → TodoContinuationEnforcer（HeartbeatStaleness が最後に登録された `after_agent` 実装者なので最初に走る。最初に登録された enforcer は最後に走り、終了したターンを観測する） |

そのフェーズに該当するフックを実装しているミドルウェアだけを行に記載しています。

---

## ミドルウェアリファレンス

### system_prompt_injection

**モジュール：** `agent/middlewares/system_prompt/core.py` · **ミドルウェア：** `system_prompt_injection`（LangChain `@dynamic_prompt` が生成する `AgentMiddleware` インスタンス）
**フック：** `wrap_model_call` / `awrap_model_call`

リストの 2 番目、`TodoContinuationEnforcer`（モデル呼び出しラップを実装しない）の直後に位置し、したがって最外層の **ラップ** 層です。デコレータが生成するクラスは `wrap_model_call` と `awrap_model_call` の両方を登録します（被装飾関数は同期で、非同期ラッパーからも呼ばれます）。

**システムプロンプト注入**

1. `require_session_id` で `session_id` を解決します —— 欠落/空白は `RuntimeError("Not pass session_id")`。
2. `state_register_mem` の `system_prompt` を参照します。
3. なければ `state_register_db` にフォールバックし、それでも無ければ `workspace.prompt_builder.build_system_prompt(session_id)` で再構築し、**db + mem に二重書き込み**します。
4. same-content skip：`@dynamic_prompt` のラッパーは*無条件に* `request.override(system_message=...)` を呼びます。リクエストが既に同一内容の `SystemMessage` を持つ場合、被装飾関数は**その同じ message オブジェクト**を返すため、override は同一バイトを再適用します —— モデル可視プレフィックスはバイト単位で同一に保たれ（プロバイダープレフィックスキャッシュに有効）、実際に変化したとき（または初回で `request.system_message is None` のとき）だけ新しい `SystemMessage` を返します。

> `system_prompt` の mem キーは圧縮パイプラインとの契約です：`Summarization` がトークン見積もり（`_estimate_system_prompt_tokens`）に読み、圧縮後に（mem + db へ）書き戻します —— そのためキャッシュミス時は必ず二重書き込みします。

**永続化は圧縮パイプラインから出ました。** `MessagePersistenceMiddleware` が各モデル境界で新規メッセージを MesMemory へフラッシュします（下のセクション参照）；メモリレビュー / プラン抽出の nudge は引き続き `Summarization` が compact 接縫からスケジュールします。`system_prompt_injection` はどのライフサイクルフック（`before_agent` / `after_agent` / `before_model` / `after_model`）もオーバーライドしません；役割はシステムプロンプトのラップだけです。

> 本ドキュメントの旧版はナレッジグラフ保守（`after_turn`）と `MemoryCache` を主張していました。**現在のコードにはどちらも存在しません。** システムプロンプトは状態レジスタと `build_system_prompt()` から供給され、ミドルウェア層のどこにもナレッジグラフ呼び出しはありません。

### MultimodalProcessor

**モジュール：** `agent/middlewares/media_pipeline/core.py` · **クラス：** `MultimodalProcessor(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`after_agent` / `aafter_agent`

`before_agent` は、内容がマルチモーダルリストである**最後の** `HumanMessage` を処理します：

- **テキスト**項目はそのまま通します（最大 1 件）。
- **`image_url`**：リモートの `http(s)` URL はそのまま保持。`data:` / base64 ペイロードはデコードされ、PIL で `src/<session_id>/mutil_temp/<タイムスタンプ><拡張子>` に保存されます（拡張子は `_IMAGE_MAGIC` のマジックバイトから推定）。永続コピーが `media/` にも作られます。
- **`audio_url`**：一時ファイルへダウンロード（タイムアウト 30 秒）。**`audio_bytes` / `video_url` / `video_bytes`**：同様にデコード・保存（`_AUDIO_MAGIC` / `_VIDEO_MAGIC`）。
- **サイズ上限**：書き込みの前にサイズを検証し、`max_media_bytes`（20 MiB、DeepAgents の CLI ハードリミットに一致）を超えるペイロードはスキップされます — ディスクへ書かれず、パスとしても記録されず、warning に実際のバイト数を記録し、通知は `MediaPaths.skipped` に入り、プロセッサが `[Uploaded media] ... was skipped` の 1 行としてメッセージのテキストブロックに追記します。リモート URL は `Content-Length` があればそれで、無ければ上限付きストリーミング読み取りで判定するため、誤ったヘッダーでも過大な書き込みは発生しません。ちょうど上限のペイロードは許可され、0 バイト / 不正なペイロードは既存の失敗経路を維持します。
- `main_llm_native_multimodal` 設定が経路を決めます：`"true"` はメディアブロックをモデルにそのまま渡し、`"false"` は常にスキルパス、`"auto"` はメディア種別（vision / audio / video）ごとにプロセス単位の能力キャッシュ（キーは `"{provider}/{model_name}"`）で判定します。
- `"auto"` では：**すべて**の在途種別が `"unsupported"` キャッシュならメッセージ全体がスキルパスへ。**混合**メッセージ（一部 supported、一部 unsupported）はネイティブブロックを保持し、リクエスト単位スクラブが未対応ブロックだけを置換します。未検証（`"auto"`）の種別はブロックを保持し、ターン単位のネイティブ試行フラグ（`_multimodal_trying_native` / `_multimodal_native_model`）を記録します。`"[Uploaded media]"` 命令ブロックはスキルパス時のみ追加され、`skill_view` ツールの `image_to_text` / `speech_to_text` / `video_text_to_text` でファイルを確認するようモデルに指示します。
- モデルが `multimodal_not_supported` として分類される拒否を返すと、`LLMRetryMiddleware` がメッセージに実際に含まれるメディア種別を `"unsupported"` として記録し、リクエストをスキルパスへ書き換えます。同じプロセス内の以降のセッションとターンはネイティブ試行を省略します。拒否は**実際に処理した**モデルに記録されます：呼び出しがフォールバック候補へ再バインドされていた場合、`LLMRetryMiddleware` はキャッシュ書き込み前に `_multimodal_native_model` をその候補へ書き換えます（同セクション参照）。
- **サイレント劣化の検知**（`main_llm_silent_degradation_detection`、既定で有効）：ネイティブ試行が**成功**したのに、返信がメディアを認識できないと自ら述べたり、添付の説明をユーザーに求めたりした場合、`LLMRetryMiddleware` はリクエストに含まれる全メディア種別を実際に処理したモデルに対して `"unsupported"` としてキャッシュし、以降のターンは失敗必至のネイティブ試行を行いません。検出器（`media_pipeline/degradation.py::detect_media_blindness`）は EN/ZH/JA/KO 対応の高精度正規表現で、失明/説明要求フレーズの近くにメディア語がある場合のみ一致します。「返信がメディアに言及しない」という字面のヒューリスティックは意図的に採用していません — 有能なモデルはメディア語を一切使わずに画像を説明でき、誤検知は遅いスキルパスを恒久化してしまうためです。
- 永続化パスは `additional_kwargs["images"]` / `["audios"]` / `["videos"]` に格納され、後から MesMemory に書き込まれ履歴レンダリングに使われます。
- **より古い** `HumanMessage` からは `image_url` ブロックが剥ぎ取られ、古い base64 がコンテキストに残りません。ただし、そのようなブロックが実際に存在する場合のみ実行され（剥ぎ取るものが無いメッセージは安価な事前チェックでスキップ）、剥ぎ取り後のテキストが空でない場合のみ書き戻します。

`wrap_model_call` / `awrap_model_call` は**すべてのモデルリクエスト**で（`"auto"` モードのみ）実行され、リクエストのコピーをスクラブします：種別が `"unsupported"` キャッシュのメディアブロックは、剥ぎ取られたメディア・そのディスク上のパス・対応スキル（`image_to_text` / `speech_to_text` / `video_text_to_text`）を示すテキストプレースホルダーに置換されます。supported と未検証のブロックはそのまま通ります。書き換えは `request.override(messages=...)` で行い、state・checkpointer・MesMemory には一切触れず、スクラブ不要なら元のリクエストオブジェクトをそのまま返します。能力参照には環境のメインモデルキー（`get_model_key()`）を使います：本層は `LLMRetryMiddleware` の外側を包むため、チェーン内層で再バインドされたスティッキー・フォールバック候補を観測できません。その候補の拒否は `multimodal_not_supported` → スキルパス書き換えが引き続き担い、キャッシュ書き込み前に実際に処理した候補を `_multimodal_native_model` へ記録します。

`after_agent` は `mutil_temp` を清掃します：ファイル名の本体が純粋な数値タイムスタンプでないもの、または 7 日より古いものを削除します。

### IterationBudget

**モジュール：** `agent/middlewares/iteration_budget/core.py` · **クラス：** `IterationBudget(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

1 ターン内の**モデル呼び出し + ツール呼び出しの合計**に対するハード上限。コンストラクタ：`__init__(max_iterations: int = 50)`。メインエージェントは `IterationBudget(90)`、ワーカーエージェントは `IterationBudget(60)` を登録します。

- `before_agent` は `state_register_mem` のカウンターをリセット：`iteration_budget = max_iterations`、`iteration_budget_used = 0`。
- `wrap_model_call` はモデル呼び出しごとに 1 消費。予算が尽きると**モデルを呼ばずに**終端 `AIMessage` を返します。
- `wrap_tool_call` はツール呼び出しごとに 1 消費。尽きると実行の代わりにエラー `ToolMessage`（"Tool [x] skipped — iteration budget exhausted"）を返します。

### ToolGuardrails

**モジュール：** `agent/middlewares/tool_guardrails/core.py` · **クラス：** `ToolGuardrails(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_tool_call` / `awrap_tool_call`

5 つの失敗病理を検出し、4 段階エスカレーション `ALLOW → WARN → BLOCK → HALT`（`GuardrailAction` 列挙型）で反応します：

| 病理 | トリガー | WARN 後 | BLOCK 後 | hard-stop モード |
|---|---|---|---|---|
| 完全な失敗の繰り返し | 同じツール + 同じ引数（引数 JSON を `sort_keys` した MD5）の失敗 | 2（`exact_failure_warn_after`） | 5（`exact_failure_block_after`） | 5 で HALT |
| 同一ツールの失敗蓄積 | 同じツールが**異なる**引数で失敗し続ける | 3（`same_tool_failure_warn_after`） | 8（`same_tool_failure_halt_after`） | 8 で HALT |
| 冪等な無進捗 | 同一の冪等ツール（メタデータ `idempotent: true`）が、そのターン内で既に返したことのある結果ハッシュを返す | 2（`no_progress_warn_after`） | 5（`no_progress_block_after`） | 5 で HALT |
| ピンポン | 2 つのツール間の途切れない A → B → A → B の往復で、連続する 2 呼び出しがともに無進捗 | 4（`ping_pong_warn_after`） | 6（`ping_pong_block_after`） | 6 で HALT |
| 引数改変 | 同じ冪等ツールが、結果の変わらない引数バリアントを巡回 | 3 変種（`arg_churn_warn_after`） | 5 変種（`arg_churn_block_after`） | 5 で HALT |

- `before_agent` はターン単位のガード状態をリセットします（`state_register_mem` のキー `tool_guardrail_state`）— 厳密にターン範囲なので、新しいターンはクリーンに始まります。
- `wrap_tool_call` はブロック済みツールと停止状態を事前チェック（実行せずエラー `ToolMessage` を返す）し、ツールを実行してから結果を評価します：
  - `warn` は `ToolMessage` に警告を追記；
  - `block` はツールを `blocked_tools` に記録；
  - `halt` はターンの残りに対する粘着性の停止を設定（`halt_decision`）。
- **リカバリモード**（`recovery_mode_enabled=True` がデフォルト）: 最初の BLOCK でターンが死ぬことはありません。ターンはリカバリ状態に入り、*precheck* 経路がブロックされたツールを解放するので、再試行は新鮮に評価されます。それ以降の BLOCK ごとに違反カウンタが増え、カウンタが `recovery_max_violations`（デフォルト 1）を超えると HALT に格上げされます — 即席の壁ではなく管理された再試行ウィンドウです。
- **無進捗とは停滞（stagnation、`is_stagnant`）のこと**：成功した冪等呼び出しは、**同一ツール**がそのターン内で既に同じ `result_hash` を返していた場合にのみ無進捗と判定されます。結果が変われば進捗です。**ピンポンペア**は隣接する 2 つの呼び出しのツール名をハッシュし、*連続する 2 つ*の呼び出しが両方とも無進捗である間だけ累積します。エラーが一度でも出たり、結果が変化（非停滞）したり、成功した非冪等（変異）呼び出しが一度でもあると、累積済みのすべてのペア連続記録がゼロに戻ります。冪等ツールの結果変化も同様に引数改変として数えられなくなり、非冪等ツールの成功は引数改変状態を完全にクリアします。
- `ToolCallGuardrailConfig` の既定値：`warnings_enabled=True`、`hard_stop_enabled=False`、`recovery_mode_enabled=True`、`recovery_max_violations=1` — `hard_stop_enabled=True` にするとすべての *ブロック* しきい値が HALT に変わり（旧来の厳格な壁）、`recovery_mode_enabled=False` にすると即時ブロックの挙動に戻ります。

▶️ 詳細：[docs/loop-prevention/README.md](../../docs/loop-prevention/README.md) · [中文](../../docs/loop-prevention/README.zh.md) · [한국어](../../docs/loop-prevention/README.ko.md) · [日本語](../../docs/loop-prevention/README.ja.md)

### ToolCallNormalize

**モジュール：** `agent/middlewares/tool_call_normalize/core.py` · **クラス：** `ToolCallNormalize(AgentMiddleware)`
**フック：** `before_model` / `abefore_model` のみ

コンテキストトリミング後の tool-call / tool-result ペアリングを修復し、プロバイダーの "Message ordering conflict" エラーを防ぎます。処理は `pub.func.sanitize_tool_use_result_pairing(state["messages"])`（`pub/func/transcript_repair.py` で定義）に委譲され、以下を行います：

- `tool_call_id` による `ToolMessage` の重複排除；
- 空の `ToolMessage` の除去；
- 欠落した結果に対するプレースホルダー `ToolMessage`（"tool result missing after context trim."）の挿入；
- エラー状態の `AIMessage` の `invalid_tool_calls` をクリアし、OpenAI tool_calls としてシリアライズされないようにする。

変更がないときフックは `None` を返します —— 状態書き込みもメッセージ再構築も行わず、モデル可視のプレフィックスはそのままです。実際に修復したときだけメッセージ全体の置換を返します：`[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`。なお、プレフィックスキャッシュの基準は**モデルに送られるシリアライズ済み内容**であり、Python オブジェクトの同一性ではありません。変更なしの再構築をスキップすることで、無意味な checkpointer 状態書き込みを排除し、再構築経路による内容ドリフトを完全に取り除けます。

### PathGuard

**モジュール：** `agent/middlewares/path_guard/core.py` · **クラス：** `PathGuard(AgentMiddleware)`
**フック：** `wrap_tool_call` / `awrap_tool_call` のみ

各ツール自身の `resolve_project_path()` / `resolve_external_path()` パターンに対する多層防御です。パス検査を忘れたツールでも、トラバーサルやハード拒否パスを読ませることはできません。メインエージェントでは `ToolCallNormalize` の直後に登録され、リスト順が wrap フックの外側順になるため `ToolGuardrails` の**内側**で実行されます（`IterationBudget` → `ToolGuardrails` → `ContextEvictionMiddleware` → `PathGuard` → ツール）。拒否は通常のエラー `ToolMessage` として ToolGuardrails に評価され、他のツール失敗と同じ扱いになります。worker パイプラインには登録しません：子ツールは自前のゲートを保ち、サブエージェントの外部アクセスはそもそも強制拒否です。

スクリーニングは意図的に保守的です：

- 引数名 `file_path` / `path` / `directory` / `dir` の文字列値のみを対象にし、`scheme://` 形式の URL はスキップするため、非パス意味論を誤読しません；
- `..` トラバーサル成分（URL デコード・バックスラッシュ正規化済み——共有の `has_traversal_component`）は拒否します；
- `resolve_project_path()` が受け入れる値はそのまま通します；
- `ROOT_DIR` の外に解決される値は、ハード拒否フロア（YOLO 拒否リスト / `/etc/passwd`、`/etc/shadow`、`/etc/sudoers`）に当たらない限り通します；
- それ以外の外部パスはツール自身の `resolve_external_path()` HITL フローに委ねます——ミドルウェアは承認・引数書き換え・インタラプトのいずれも行いません。ツールは実行時に同じゲートを再実行するため、ここで介入すると決定が二重になります。

拒否時はツールを実行せず、構造化エラー `ToolMessage`（`status="error"`、元の `tool_call_id` / ツール名を保持）を返します。外部パスの詳細：[docs/sandbox/isolation/README.ja.md §5](../../docs/sandbox/isolation/README.ja.md#5-外部ファイルパスゲートファイルツール)。

### SubagentCompletionDrainMiddleware

**モジュール：** `agent/middlewares/subagent_completion_drain/core.py` · **クラス：** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**フック：** `before_model` / `abefore_model` のみ

メインエージェントには `ToolCallNormalize` の直後に登録されるため、注入されるメッセージは注入ターンではサニタイズ書き換えをバイパスします。`before_model` でセッションの `SteeringQueue`（親がビジーの間に announce パイプラインが積んだ完了キャリアメッセージ）をリハイドレートして排出（drain）し、`{"messages": [carrier, ...]}` を返すことで、次のモデル呼び出しの直前に再構築された完了キャリア `HumanMessage` を注入します。

- 排出された各キューエントリはキューの SQLite ストアで `CONSUMED` とマークされるため、キャリアは正確に 1 回だけ注入されます（チェックポイント永続化により HITL 再開リプレイも安全）。
- Fail-open：`session_id` の欠落/空、空のキュー、あらゆる例外は握りつぶされます（ログ + no-op）— drain が親ターンを壊すことはなく、キューは再試行のために保持されます。
- **プログラムゲート（常時有効）**：排出された各バッチはセッションの検証証跡と照合されます（`agent/tools/taskflow/evidence_collector.py`）；合格した証跡がない場合は、キャリアの後ろに必須検証メッセージが追記されます——キャリア自体はそのまま注入されます。参照はフェイルオープン：証跡コレクタが利用できなくてもターンを止めません。
- 注入されたキャリアは、それが注入されたまさにそのモデル呼び出しの `after_model` 境界で `origin='subagent_completion'` として MesMemory に書き込まれます（`MessagePersistenceMiddleware`）; その境界まではチェックポイントにのみ存在し、messages テーブルからは見えません。

### TaskIntentMiddleware

**モジュール：** `agent/middlewares/task_intent/core.py` · **クラス：** `TaskIntentMiddleware(AgentMiddleware)`
**フック：** `before_model` / `abefore_model`（本番経路は非同期フック。イベントループが無いときだけ同期版が委譲します）

メインエージェントでは `SubagentCompletionDrainMiddleware` の直後に登録されるため、注入されるメッセージは注入ターンのサニタイズ書き換えを迂回します。1 つのフックに 2 つの挙動が統合されています：

- **アーミング：** アクティブな計画が無い状態で仕事の依頼らしい最初のユーザーターンに、オーケストレーター向けの完全なステアリングプロンプトを注入し、以降の該当ターンには短いリマインダーを注入します。アーミング台帳（`_armed_sessions`）はプロセス単位で、圧縮成功後に `rearm_after_compact(session_id)` がエントリを消去します（`Summarization` が呼び出し）。そのため compact 後は完全なプロンプトが再注入されます。候補判定はキーワード / 疑問 / 雑談パターン（`_TASK_KEYWORDS`、`_QUESTION_PATTERNS`、`_CHAT_PATTERNS`）を使います。
- **計画アクティブ時のステアリング：** boulder ファイル（`config.path.resolve_boulder_path`）にアクティブ/一時停止中の作業があり、その計画ファイルが存在してチェックボックスを含む場合、代わりに計画アクティブ・リマインダーを追記し、アーミングを省略します — 計画アクティブのステアリングが優先です。

注入はターンの最初のモデル呼び出しでのみ発生します（最後の非ディレクティブ `HumanMessage` が末尾メッセージであること）。ドレイン済みの完了キャリア（`metadata.internal` + `provenance == "subagent_completion"`）、`[SYSTEM DIRECTIVE` / `<sherry-ulw-execute>` 指令、`metadata.internal` メッセージはステアリングを発火させないため、注入された指令が再アーミングを起こすことはありません。fail-open：内部例外はログに記録され、フックは `None` を返します。

### TodoContinuationEnforcer

**モジュール：** `agent/middlewares/todo_continuation/core.py` · **クラス：** `TodoContinuationEnforcer(AgentMiddleware)`
**フック：** `aafter_agent` のみ（非同期ターン終了フック）

メインエージェントのリストで**最初**に登録されるため、`after_agent` は**最後**に実行されます — `after_agent` フックはリスト逆順に走り、enforcer は本当に終わったターンを観測する必要があるためです。セッションの todo リストにまだ `pending` / `in_progress` 項目がある場合、サーバー側の自動ターンフック（`runtime.hooks.MAYBE_TRIGGER_AUTO_TURN`、呼び出し時に解決。未登録なら no-op に劣化しセッションは再試行可能なまま）を通じて継続プロンプトを fire-and-forget で注入します。

- 中断クラスのターンエラー（ユーザーキャンセル / タイムアウト、`stagnation_tracker.is_abort_error`）では決して継続しません。空または全完了のリストは停滞トラッカーをリセットします。
- 停滞処理（`agent/tools/todolist/stagnation_tracker.py`）：複数回の試行後もリストが変わらない場合は復帰モードに入り `_RECOVERY_PROMPT` を注入します。`is_in_cooldown` が繰り返し注入を抑制し、配信成功後にのみ `mark_injected` で記録します。
- fail-open：例外はログに記録され、ターンは通常どおり終了します。

### HeartbeatStaleness

**モジュール：** `agent/middlewares/heartbeat_staleness/core.py` · **クラス：** `HeartbeatStaleness(AgentMiddleware)`
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
4. `write_approval_memory=True` の場合、メモリツールの書き込みは `WriteApprovalGate` を通過。`interrupted_tools` に列挙されたツールは常に中断され、決定は `approve` / `edit` / `reject`（`edit` はツール呼び出しの引数/名称を書き換えます）。別途、外部パスゲートは独自の割り込みを起こし、決定セットは `approve` / `approve_dir` / `yolo` / `reject` です（詳細: [docs/sandbox/isolation/README.ja.md](../../docs/sandbox/isolation/README.ja.md#5-外部ファイルパスゲートファイルツール)）。
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

ツール呼び出しの承認決定は `ToolApprovalStore`(`approval_store.py`)によって `SRC_DIR/data/approvals.json` にも永続化されます: オペレーター単位で分離された JSON ストアをバイト改訂 CAS で更新するため、承認済みの `interrupted_tools` 呼び出しは再起動後に再確認されません。スコープ内にオペレーターがいない場合(またはヘッドレスなシステム注入ターン)は、承認が必要なゲートは割り込みの代わりに自動拒否します。

▶️ 詳細：[humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### MessagePersistenceMiddleware

**モジュール：** `agent/middlewares/message_persistence/core.py` · **クラス：** `MessagePersistenceMiddleware(AgentMiddleware)`
**フック：** `after_model` / `aafter_model`、`wrap_tool_call` / `awrap_tool_call`

メインエージェントでは **`HumanInTheLoop` の直後**に登録されます。`after_model` ノードは登録逆順に連結されるため、これは `model` の後に**最初に実行される**フックです — AI メッセージは、HITL が拒否されたツール呼び出しを剥がしたり `GraphInterrupt` を起こす前に永続化され、他のフックの例外もこのフラッシュを飛ばせません。ツール呼び出しラップチェーン（リスト先頭が最外層）では**最内層**に位置し、実際のツール結果を見られます（HITL の短絡はこれを迂回します）。（ワーカーパイプラインには登録されません。）

**レイテンシ — メッセージ種別ごとの `messages` テーブル到達時点：**

| メッセージ | 永続化のタイミング |
|---|---|
| human | そのターン最初のモデル境界 |
| AI（`tool_calls` を含む） | モデルが生成した直後のモデル境界 |
| ツール結果 | **ツール handler が返った瞬間**（`wrap_tool_call` / `awrap_tool_call`）。次のモデル呼び出しを待ちません |
| HITL 拒否（`HumanInTheLoop` 短絡による `status="error"` ToolMessage） | 次のモデル境界 — HITL はこのミドルウェアを外側から包むため、短絡は内層を呼びません |

**ツール復帰時のフラッシュ：** wrap フックはまず `handler(request)` を実行し、応答から `ToolMessage` を取り出します — 応答は裸の `ToolMessage`、`ToolMessage` / `Command` が混在するリスト、`Command.update["messages"]` にメッセージを載せた `Command` のいずれか（`_iter_tool_messages` がすべて走査）— を同一バッチパイプラインで永続化し、応答を**そのまま**返します。`ToolMessage` を含まない応答（純粋なナビゲーション `Command`）は何も書き込みません。

共有バッチパイプライン（両フック共通）：

1. `require_session_id` で `session_id` を解決。欠落/空白（または非 dict のツール呼び出し state）は静かにスキップし、決して送出しません。
2. 候補の収集：`_is_persistable` が `human` / `ai` / `tool` のみを残し、プロセス内 `_db_persisted` マーカー付きメッセージと `lc_source == "summarization"` の圧縮成果物をスキップします。
3. 永続ウォーターマークでのフィルタ：`filter_persisted_message_ids` が参照キーが `persisted_message_ids` に登録済みの候補を除外します。参照は **LangGraph メッセージ `id`**（チェックポイント直列化をまたいで安定）と `sha1:` 内容フィンガープリントの**両方**を確認します — ツール結果は復帰時にまだ id を持たない（グラフ reducer が後で付与する）ため、後続の境界や再起動リプレイはフィンガープリントで一致し、再永続化しません。
4. バッチの補完：`_reconcile_denials_for_persistence` が HITL 拒否のツール呼び出しを直前の `AIMessage` に再装着し（拒否はペアのまま）、`_dedup_tool_results` が空・重複 id のツール結果を捨てます。
5. `await add_messages(...)`（非同期経路）/ `add_messages_sync(...)`（同期経路）で書き込み、その後 `mark_message_ids_persisted` が書き込み器へ渡した全候補（重複排除されたコピーを含む — 再浮上防止）をトゥームストーン化します。
6. 書き込み件数とソースを debug ログに記録（`message persistence: wrote N messages at model boundary|tool return for <session>`）。

**2 経路は 1 つのウォーターマークを共有し、書き込みは冪等です。** 復帰時に書かれたツール結果は後続の各境界で再び見え、境界で書かれた AI メッセージは次の境界で再び見えます。グラフ状態は蓄積しますが、ウォーターマークが各メッセージを 1 行に保ちます — プロセス再起動をまたいでも成立します（メッセージ id と内容フィンガープリントはチェックポイント直列化を生き延びます）。書き込みエラーはログのみで**トゥームストーンされず**、次の境界で同じバッチが再試行されます（fail-open — 永続化がターンやツール結果を壊すことはありません）。

> 圧縮パスはもう何も永続化しません：`compaction_persistence.py` モジュールと `_persist_discarded_messages_sync` / `_apersist_discarded_messages` 呼び出し地点は削除されました。compact は圧縮と圧縮時 nudge のスケジュールだけを行います（Summarization セクション参照）。

### ContextEvictionMiddleware

**モジュール：** `agent/middlewares/context_eviction/core.py` · **クラス：** `ContextEvictionMiddleware(AgentMiddleware)`
**フック：** `wrap_tool_call` / `awrap_tool_call`（P0-2/P2-4）と `before_model` / `abefore_model` + `wrap_model_call` / `awrap_model_call`（P1-9）

メインエージェントでは **`ToolGuardrails` の直後** に登録されるため、wrap チェーンでは `PathGuard` / `HumanInTheLoop` / `MessagePersistenceMiddleware` の**外側**に位置します（先に登録されたものが最外層）。`MessagePersistenceMiddleware` は最内層のままなので、ツール結果については内側の層がツール復帰の瞬間に**生の結果**を MesMemory へフラッシュし、その後でこの層がプレビューに差し替えます。state（したがってチェックポインターと次回のモデル呼び出し）に入るのは常にプレビューだけで、大きな内容がプレフィックスに入ることはありません。ワーカーパイプラインには登録されません（子トランスクリプトは完全なツール結果を保持）。

同じミドルウェアが**人間メッセージ**経路（P1-9）も持ち、その三状態分割はツール側とは逆です。以下を参照：[人間メッセージの退避](#人間メッセージの退避p1-9)。

**ツール結果の 2 つの縮小パス**

| 結果 | 処理 |
|---|---|
| 一般ツール、テキスト > `evict_threshold_chars`（20 000 文字） | 全文を `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt` に書き込み、内容を head/tail プレビューに置換 |
| `read_file`（P2-4） | ファイルは既にディスク上——先頭 4 000 文字 + 復旧通知にスライス、**ファイルは書かない** |
| `write_file` / `patch_file` / `search_files` / `list_files` / `memory` / `skill_view` / `skill_list` | 決して退避しない（`excluded_tools`）。`read_file` もこの集合にあるが、上記のスライス経路を通る |

**プレビュー形式**（`pub/func/message/eviction.py::build_preview`、`preview_head_lines=5`、`preview_tail_lines=5`）：

```text
[evicted to: <path>]
--- head (5 lines) ---
<先頭 5 行>
...
--- tail (5 lines) ---
<末尾 5 行>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

（`offset=0` は `read_file` の `max(1, …)` により 1 にクランプされるため、ポインターは常に有効です。）置換は `model_copy` で作られ、メッセージの `id`・`status`・`name`・`additional_kwargs` は保持されます。マルチモーダルの非テキストブロックはそのまま残り、テキスト部分だけが置換されます。

**3 つのストア**

| 場所 | 内容 |
|---|---|
| graph state / チェックポインター / 次回モデル呼び出し | プレビューのみ |
| MesMemory（`messages` テーブル） | 完全な原文（内側の層がツール復帰時に書き込み） |
| `SESSIONS_DIR/<session_id>/evicted/` | バイト単位で同一のコピー（`load_evicted()` / `read_file` で取得可能） |

**ウォーターマークの安全性。** `model_copy` は内側のフラッシュが付けたプロセス内 `_db_persisted` マーカーも引き継ぐため、次のモデル境界はプレビューをスキップします。原文の書き込みが成功した場合は、置換メッセージのウォーターマークキーにもトゥームストーンを書き込みます——再起動でマーカーが失われ、プレビューの指紋が原文と一致しなくなった場合をカバーします。どちらの場合も同じ論理メッセージに 2 行目は書かれません。原文の書き込みが失敗した場合はトゥームストーンを書かず、境界がプレビュー内容で再試行します。

**パス安全性とライフサイクル。** `session_id` は単一の安全なパスセグメントとして検証されます（`config/path.py::is_safe_session_segment`）。空 / `.` / `..` / 区切り文字を含む id はディスクに触れずスキップされます。`clear_session()` は `SESSIONS_DIR/<session_id>/` フォルダー全体を rmtree するため、退避ファイルはセッションと共に削除されます。退避済みメッセージが再度退避されることはありません（冪等マーカー検査）。

**圧縮時の read_file スライスとの相補性**（`pub/func/message/target_truncation.py::_truncate_read_file_content`）：このミドルウェアはツール実行時を、圧縮経路はコンテキスト逼迫時を担当します（head 30 % + tail 30 %、`max_tool_output_chars = 2 000`、パーサー由来の 1-based 継続 offset 付き）。スライス済みペイロードを圧縮が再度クリップしても、切り詰められた JSON はパースできないため、決定論的に「先頭から読み直し」通知へフォールバックします。実行時スライス自体も冪等です。2 つの通知は衝突せず、同一メッセージの段階的縮小です。

#### 人間メッセージの退避（P1-9）

ユーザーが巨大なプレーンテキスト（ログ、文書、会話エクスポート、コード）を貼り付けることがありますが、`MultimodalProcessor` はメディア添付のみを扱い、この種のテキストは統治しません。DeepAgents の `FilesystemMiddleware` には専用機構（`human_message_token_limit_before_evict`）があり、本ミドルウェアはそれを Sherry 固有の三状態分割で移植します。

**トリガー（`before_model` / `abefore_model`）。** ゲート：`human_evict_enabled` かつ**最後の**メッセージが `HumanMessage`、`lc_evicted_to` を持たない、抽出テキスト長が `human_evict_threshold_chars`（200 000 文字 ≒ DeepAgents 既定 50 000 トークン × 4 文字/トークン）を**超える**。最後の 1 件のみを検査し、過去のユーザー発言は再検査しません。`before_agent` チェーン（MultimodalProcessor）は常にモデルループより先に走るため、タグ付け時のテキストにはメディアヒントが統合済みです。本ミドルウェアの `before_model` ノードはリスト順で `ToolCallNormalize` / `SubagentCompletionDrainMiddleware` より先に実行されます。

**タグ付け + 退避。** 全文を `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|タイムスタンプ>.md` に書き込み（`evict_human_message`、`pub/func/message/eviction.py`）、フックは部分状態更新 `{"messages": [tagged]}` を返します。`tagged = msg.model_copy(update={"additional_kwargs": {..., "lc_evicted_to": str(path)}})` で **content と id は不変**。標準の `add_messages` reducer が id でその場置換します。`REMOVE_ALL_MESSAGES` センチネルもリスト全面書き換えも不要で、state 側でプレフィックスキャッシュを壊しません。`session_id` が安全でない場合（空 / `.` / `..` / 区切り文字入り）はディスクに触れずスキップします。書き込みはタグ付けより先なので、失敗しても宙に浮いたポインタは残りません。1 行が巨大でプレビューが元より小さくならない場合もスキップします（ツール側と同じガード）。

**モデルビュー（`wrap_model_call` / `awrap_model_call`）。** `lc_evicted_to` を持つ `HumanMessage` は、**そのリクエスト内でのみ** state の原文から作ったプレビュー（`build_human_preview`）に置換されます：退避パス + head/tail 各 5 行 + `read_file(file_path=..., offset=0, limit=100)` の続読ヒント。`_build_evicted_content` は非テキストブロック（画像 / 音声 / 動画）をそのまま保持し、テキストブロックだけを置換します。リクエストは `request.override(messages=...)` で再構築され（Summarization と同じイディオム）、state は決して書き換えられません。ファイルが欠落している場合（セッションディレクトリ削除、ディスク障害）は state の原文から**自己修復**しますが、書き込み先がそのセッション自身の `evicted/` ディレクトリでなければ拒否します。

**3 つのストア——ツール結果とは逆の分割**

| 場所 | ツール結果（P0-2） | 人間メッセージ（P1-9） |
|---|---|---|
| graph state / checkpointer | プレビューのみ | **全文** + `lc_evicted_to` タグ |
| MesMemory（`messages` テーブル） | 全文 | **全文**（タグは永続化をフィルタしない） |
| 次回のモデル呼び出し（リクエストビュー） | プレビュー | プレビュー（パス + `read_file` ヒント） |
| `SESSIONS_DIR/<session_id>/evicted/` | バイト単位で同一のコピー | バイト単位で同一のコピー |

違いの理由：人間メッセージはターン最初の `after_model` 境界で永続化されるため、state が全文を保持しないと MesMemory は全文をアーカイブできません（そうでなければ `message_search` と圧縮はプレビューしか見られません）。ツール結果は内側の永続化層がツール復帰時にフラッシュ済みなので、state はプレビューで済みます。人間メッセージの id は変わらないため永続化ウォーターマークは影響を受けず、2 行目は書かれません。HITL とツールペアリングは `HumanMessage` と相互作用せず、P1-2 のオーバーフロー・テールクリップは `ToolMessage` のみをスタブ化し、`_filter_summary_messages` は `lc_source='summarization'` の成果物のみを除外します——どちらもタグやポインタを破壊しません。

**設定**（`config/features/agent_side/tool_result_eviction.py`）：`enabled=True`、`evict_threshold_chars=20_000`、`preview_head_lines=5`、`preview_tail_lines=5`、`eviction_subdir="evicted"`、`excluded_tools`（上記 8 個）、`human_evict_enabled=True`、`human_evict_threshold_chars=200_000`、`human_preview_head_lines=5`、`human_preview_tail_lines=5`。

### LLMRetryMiddleware

**モジュール：** `agent/middlewares/llm_retry/core.py` · **クラス：** `LLMRetryMiddleware(AgentMiddleware)`（他に `LLMRetryConfig`、`FallbackCandidate`、`ContentFilterError`）
**フック：** `wrap_model_call` / `awrap_model_call` のみ

メインエージェントには **`HumanInTheLoop` と `Summarization` の間**で登録されます：`MaxTokensBoostMiddleware` に対しては内側（ブースト再呼び出しを経ても残った本当の切断だけを目にする）、Summarization に対しては外側（リトライループが T4/T5 オーバーフローリカバリリングを外側から包む）。ワーカーパイプラインには登録されません。状態に `session_id` がない場合は素通しです。

各 handler 呼び出しは、`pub/func/message/llm_error_classifier.py` に基づく「分類 → 処置」ループを通ります — `FailoverReason` 列挙型（19 種）、`ClassifiedError` 判定（`retryable` / `should_compress` / `should_fallback` フラグ）、8 段階優先度パイプライン `classify_api_error` — そして `pub/func/retry_utils.py::jittered_backoff` でバックオフします。

**`FailoverReason` ごとのリトライセマンティクス**

| クラス | 理由 | 処置 |
|---|---|---|
| ジッター付きバックオフでリトライ（`retryable=True`） | `auth`、`rate_limit`、`overloaded`、`server_error`、`timeout`、`image_too_large`、`invalid_response`、`unknown` | 最大 `max_retries` 回の再呼び出し。遅延 = `base_delay × 2^(attempt−1)` を `max_delay` で上限切りし、±`jitter`、`[0.1, max_delay]` にクランプ |
| 圧縮委譲（`should_compress=True`） | `context_overflow`、`payload_too_large` | 即座に再スロー — オーバーフローエラーは Summarization の T4/T5 リカバリリングが担当 |
| マルチモーダルのスキルフォールバック（`retryable=True`） | `multimodal_not_supported` | auto モードのネイティブ試行中のみ：メッセージに実際に含まれるメディア種別を `unsupported` として記録し、リクエストをスキルパスへ書き換え、リトライ予算をリセットして再呼び出し。それ以外は再スロー |
| フォールバック（`should_fallback=True`、非リトライ） | `auth_permanent`、`billing`、`upstream_rate_limit`、`ssl_cert_verification`、`model_not_found`、`provider_policy_blocked`、`content_policy_blocked` | 次のフォールバック候補へ切替。チェーン尽きで再スロー |
| ハードフェイル | `format_error` | 再スロー（リトライもフォールバックもしない） |

**スタイル連続ブレーカー（ターン間）：** timeout と分類された失敗ごとに — および timeout 分類の部分ストリームスタブ再試行ごとに — セッション単位の `llm_stale_streak`（`state_register_mem` 内）がインクリメントされ、成功した handler 呼び出しがあれば 0 にリセットされます。連続が `stale_giveup_threshold` に達すると、次のモデル呼び出しは LLM を呼ぶ**前に** `RuntimeError("Provider unresponsive — aborting to avoid indefinite stall.")` を送出し、プロバイダーが応答しない状態のスパイラルをターン間で断ち切ります。

**モデルフォールバックチェーン：** `FallbackCandidate(provider, model_name, model)` エントリは、`models/LLMs/main_llm.py::build_fallback_chain()` が環境変数 `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` から構築します（i = 1…、最初に `NAME` が欠けた時点で停止。`PROVIDER` のデフォルトは `openai`。クライアントを構築できない候補は警告付きでスキップ）。1 起点のアクティブインデックスはセッション単位で `llm_fallback_index` に粘着的に保持され、毎回のモデル呼び出しの開始時に `request.override(model=...)` で既に活性化済みの候補へリバインドされ、フォールバック分類の失敗（またはコンテンツフィルタフラグ）で次の候補が活性化します。`FALLBACK_LLM_*` が未設定（デフォルト）の場合、このミドルウェアは単なる有界リトライループです。

**ネイティブ試行の帰属は実際に処理したモデルに従う：** `MultimodalProcessor` は auto モードのネイティブ試行開始時に環境メインモデルのキーを `_multimodal_native_model` に記録します。リクエストがフォールバック候補へリバインドされるたび（呼び出し開始時の粘着バインド、または活性化時）、`_refresh_native_model_key` がそのキーを `{candidate.provider}/{candidate.model_name}` に書き換えます — これによりメディア拒否（またはサイレント劣化の検知）は実際に処理したモデルへキャッシュされ、劣る候補が環境メインモデルのキャッシュ項目を汚染しません。進行中のネイティブ試行の外では何も書き込みません。

**サイレント劣化の評価：** ハンドラ呼び出しが成功した後、ミドルウェアは結果に対して `_evaluate_silent_degradation` を実行します。適用されるのは auto モードのネイティブ試行中のみで、ターン単位フラグはどちらの場合もクリアされます（古いフラグが同一ターンの後続呼び出しにフォールバックを許可してはならないため）。`main_llm_silent_degradation_detection` が有効で、返信がメディアを知覚できないと自ら述べている場合（`media_pipeline/degradation.py::detect_media_blindness`）、リクエストに含まれるすべてのメディア種別が実際に処理したモデルに対して `"unsupported"` としてキャッシュされ、以降のターンはメディア抜きで黙って答える代わりにネイティブ試行を省略します。

**コンテンツフィルタフラグの消費：** ストリーム層（`server/service/stream_dispatch.py`）は `llm_content_filter_blocked`（明示的な `finish_reason == "content_filter"`）または `llm_content_filter_terminated`（ストリーム途中の安全カット）を設定します。ミドルウェアは毎回の handler 呼び出し後 — 成功でも分類済み例外でも — 両フラグを確認してクリアし、フォールバックモデルへリバインドするか、候補が残っていなければ `ContentFilterError("Model declined to respond (safety refusal).")` を送出します。`content_policy_blocked` は決してリトライしません。

**部分ストリームスタブの消費：** ストリーム途中のネットワーク切断後、ストリーム層は `llm_partial_stream_stub` と `llm_partial_stream_cause`（保持された `FailoverReason` 値、デフォルトは `timeout`）を設定します。ミドルウェアは成功した handler 呼び出し後にフラグを消費します：切断された結果は破棄され、handler がバックオフ後に新しい試行として一度だけ再呼び出しされます — より大きい max_tokens でのブーストは決してありません。ストリーミングターン（`is_stream_turn` フラグ）では、再呼び出し前に `request.config["callbacks"]` を剥離し `finally` で復元します（MaxTokensBoost の strip → call → restore 契約）。すでにストリーミング済みのトークンが重複しません。timeout 分類の原因はスタイル連続を増やします。リトライ予算が尽きた場合、ミドルウェアは穏当に縮退し現在の（部分的な）結果を返します。

**状態キー（すべて `state_register_mem` 内）：** `llm_stale_streak`、`llm_fallback_index`（ここで所有）。`llm_content_filter_blocked`、`llm_content_filter_terminated`、`llm_partial_stream_stub`、`llm_partial_stream_cause`（ストリーム層が書き込み、ここで消費）。`_multimodal_trying_native`、`_multimodal_native_model`（`MultimodalProcessor` が書き込み、スキルフォールバックのためにここで消費）。

### Summarization

**モジュール：** `agent/middlewares/summarization/core.py` · **クラス：** `Summarization(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`（カウンターリセット）、`wrap_model_call` / `awrap_model_call`

最内層のミドルウェア — LLM に最も近い位置。スクラッチで実装された `AgentMiddleware` です（LangChain の `SummarizationMiddleware` **ではありません**）：トリガーが発火すると、予算ベースのカットオフで履歴を圧縮します — 非 LLM 戦略を優先し、テキスト劣化が安全な場合にのみ補助 LLM による要約を使用。`keep` パラメータは受け付けますが未使用で、末尾保持は予算ベースです：`clamp(context_window × 0.25, 2 000, 15 000)` トークン（`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`）。

- **ライフサイクルとルーティング：** ミドルウェアは 5つのトリガーポイント（T1–T5）を網羅します — T1 事前点検（`before_agent` / `abefore_agent`）、T2 呼び出し前ディスパッチ（`wrap_model_call` / `awrap_model_call`）、T3 応答後の再確認（実際の報告トークン）、T4（413 Payload Too Large）/ T5（コンテキストオーバーフロー）エラー復帰リング — どのトリガーも 4ルート・オーバーフロー判定（truncate / compact / both / pass）を実行し、`pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/tool_args_truncate.py`（ツール呼び出し引数の切り詰め）、`pub/func/message/llm_error_classifier.py` に委譲します。状態はセッション単位の `summarization_*` キー（計 13 個、ターンごとに 11 個をリセット）に保持されます。詳細は下記のリンクを参照。
- **トリガーセマンティクス**：節は `("messages", N)` または `("tokens", N)` で、節リスト間は **OR** — いずれかの節が発火すると圧縮が始まります。メインエージェント：`[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。ワーカー：`[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。`COMPRESSION_TRIGGER_RATIO = 0.80`。
- **カットオフの安全性：** `_determine_cutoff` がカットオフ位置を選び、続いて `_adjust_for_orphan_pairs` が `ToolMessage` が自身の `AIMessage` ツール呼び出しから分離されなくなるまで位置を手前に戻します。最後のユーザーターンが推定トークンの ≥ 50 % を占める場合（`LAST_TURN_RATIO_THRESHOLD = 0.5`）、そのターンを要約で消すのではなく、ターン自体を圧縮します（`self._compress_last_turn` フラグ）。
- **アンチスラッシング：** 1 セッションあたり最大 `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` 回の圧縮（ターンごとではない）。連続 `INEFFECTIVE_THRESHOLD = 2` 回の無効な圧縮で（有効 = メッセージ数の減少、またはトークン削減 ≥ `MIN_EFFECTIVENESS_PCT = 0.05`）、LLM ステップを無効化（`summarization_skip_llm`）し非 LLM 戦略のみを実行します。カウンターはセッション単位の `summarization_*` キーとして `state_register_mem` に保持されます（圧縮回数、無効連続回数、直近トークン、直近戦略、スキップフラグ、リカバリ状態など）。
- **圧縮時のメディア退避：** 破棄されるプレフィックスは要約される前に `offload_inline_media`（`summarization/media_offload.py`）を通り、インラインメディア（`data:` URL、生の `base64` フィールド、生バイト）を `SESSIONS_DIR/<session_id>/media/{sha256[:16]}{ext}` へ書き出し（内容ハッシュで重複排除、このサブディレクトリは `clear_session` が丸ごと削除）、各ブロックを `[evicted to: <path>]` テキストポインタに置換します — `_collect_evicted_refs` が走査するこのマーカーにより、パスは `SummaryDoc.evicted_refs` に入り要約チェーン上で生き残ります。書き換えられるのは要約対象の範囲のみで、保持される末尾ウィンドウのメディアブロックはそのままです。すべての失敗は fail-open で、デコード / 書き込みできないブロックは `<media error="failed_to_offload" />` になります。
- **切り詰め：** 既存の要約メッセージ（`additional_kwargs["lc_source"] == "summarization"` で識別）が `SUMMARY_TOTAL_MAX_CHARS = 16 000` 文字を超えると再切り詰めされ、先頭 30 % / 末尾 30 %（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）を保持し省略マーカーが入ります。
- **出力：** 置換後のメッセージは `HumanMessage` / `AIMessage` の**ペア**です — 中立的な `"What did we do so far?"` に続き、`additional_kwargs={"lc_source": "summarization"}` を持つ `AIMessage` が続きます — モデルが連続した同役割メッセージを見ることはなく、事後のペア修復も不要です。
- **構造化要約：** 補助呼び出しは `with_structured_output(SummaryDoc, method="json_mode")`（`summarization/summary_doc.py`）を通ります。Markdown はドキュメントからコードでレンダリングされ、cap 後のドキュメント本体は AIMessage の `additional_kwargs["summary_doc"]` に保存されます（チェイニング再要約は `<prior-summary-json>` として再入力）。解析失敗は `json_repair`、次にレガシー free-form パスへ降格します。項目上限は配列スライス（`completed[-5:]`、`key_decisions[-5:]`、`active_plan_notes[-20:]`、`evicted_refs[-20:]`）で、Markdown の再解析ではありません。
- `need_update_system_prompt=True`（メインエージェントのみ）：圧縮後にシステムプロンプトを再構築 — メモリストアを再読み込みして `build_system_prompt()` を呼び — `system_prompt` キーで両方の状態レジスタに書き戻します。2 つの配送経路（圧縮直後とアンチスラッシングゲート経路）は、リクエストが既に同一内容の `SystemMessage` を持つ場合に注入をスキップし —— override も新しい `SystemMessage` も作らず —— モデル可視プレフィックスをバイト単位で同一に保ちます。
- **永続化は行いません：** 圧縮パスは MesMemory へ何も書き込みません。メッセージ永続化は各モデル境界で `MessagePersistenceMiddleware` が実行します；旧 `compaction_persistence.py` の破棄プレフィックスフラッシュと `_persist_discarded_messages_sync` / `_apersist_discarded_messages` 呼び出し地点は削除されました。
- **圧縮時 nudge：** `schedule_compression_nudges`（`summarization/nudges.py`）が圧縮のたびにメモリレビューを派遣します；プラン抽出は同じ時点で `_detect_todo_all_complete` により評価されます。どちらも NUDGE レーン上の fire-and-forget タスクとして走ります；nudge ロックが保持されている間、圧縮は派遣を完全にスキップします。after-agent フックはこれらを派遣しません。

**Nudge サブエージェント**（`summarization/nudges.py`、圧縮パイプラインが派遣）：メイン LLM 上に構築された独立した `create_agent` インスタンスで、ミドルウェアは `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`。`_NudgeLimitTool` はメタデータに `nudge: true` を持たないツールをすべて拒否するため、nudge エージェントは nudge フェーズで許可されたツールしか使えません。プロンプトは 2 つあります：

- `_MEMORY_REVIEW_PROMPT`（メモリレビュー）：ユーザーの持続的な好みや期待をメモリツールで保存する定期パス。
- `_PLAN_EXTRACTION_PROMPT`（プラン抽出）：全ての todo が完了したときに 1 回発火するパスで、2 つの成果物を生成します。**Part 1** は `knowledge` ツール（`action="write"`）で構造化 JSON ナレッジを `workspace/knowledge/plans/<plan-name>/` に書き込み、task・wave・plan の 3 層で `failure_set` / `success_path` / `method` を持ちます。**Part 2** は `skill_manage` でスキルライブラリを更新します（旧来の独立したスキルレビュー指針はここに統合）。そのコンテキストは `_build_plan_context` から取得します：プランファイル、todo リスト、およびこのセッションの subagent runs（`result_text` / `outcome` / タスクのみ）。
- **圧縮後の TODO 更新：** `compression_todo_update_enabled`（既定で有効）がオンで、かつ今回の圧縮が実際にメッセージを破棄した場合、非同期パスは専用 nudge エージェントを fire-and-forget で起動します（`_COMPRESSION_TODO_PROMPT`）。そのグラフは派生セッションキー（`<id>::compression-todo`。`IterationBudget` / `ToolGuardrails` の状態がメインセッションに触れることはありません）で動作し、ツールセットはメタデータ付き `todowrite` シム 1 つだけ（`todo_update: True`、`_NudgeLimitTool(allowed_metadata_key="todo_update")` が許可）で、メインセッションに束縛されます。破棄された会話スライスに基づいて TODO リストを突き合わせ、実際に完了した項目を `completed` / `cancelled` に、根拠のある新規作業を `pending` として追加し、`todowrite` で**完全な**リストを書き戻します。圧縮をブロックすることも失敗させることもなく、セッション単位の `compression_todo_update_lock` が重複起動を防ぎ、同期パスはイベントループが動作している場合のみスケジュールします。

▶️ 詳細：[docs/summarization/README.md](../../docs/summarization/README.md) · [中文](../../docs/summarization/README.zh.md) · [한국어](../../docs/summarization/README.ko.md) · [日本語](../../docs/summarization/README.ja.md)

> 予算ミドルウェアのクラス既定値 `max_iterations` は 50 です。*登録されている* 値は 90（メイン）と 60（ワーカー）。本ドキュメントの旧版は予算 10 と主張していました — 誤りです。

### MaxTokensBoostMiddleware

**モジュール:** `agent/middlewares/max_tokens_boost/core.py` · **クラス:** `MaxTokensBoostMiddleware(AgentMiddleware)`
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

**モジュール：** `agent/middlewares/output_repetition_guard/core.py` · **クラス：** `OutputRepetitionGuard(AgentMiddleware)`
**フック：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`

事後型の出力繰り返し検知器で、`WARN → HALT` エスカレーションを持ちます。`agent.middlewares.output_repetition_guard.core` からエクスポートされ、`agent/middlewares/__init__.py` からも再エクスポートされています。メインエージェント（呼び出しごとのインターセプト、下記ラッパーの補完）とワーカーパイプラインの**両方**に登録されています。

メインエージェントでは同じ検知が **`RepetitionGuardWrapper`**（`agent/wrapper/repetition_guard.py`）を通じて実行されます。これはコンパイル済みグラフをラップし、ストリームレベルでインターセプトし（`ainvoke` の事後バックストップ付き）、同じ状態キーとデフォルトを再利用します。どちらの登録も `phantom_stream_guard=True` を渡します。

**検知レイヤー**

- **呼び出し間の繰り返し** — 可視出力の末尾 `_TAIL_CHARS = 500` 文字の MD5 を、ローリング履歴（`_MAX_HISTORY = 30`）と比較。`warn_after = 2` 件の同一出力で WARN（`AIMessage` で注意喚起）、`max_identical_outputs = 3` で HALT — 終端 `AIMessage` と粘着性の停止フラグを返します。
- **単一出力内の繰り返し**：
  - 文/行の重複率 > `internal_repeat_ratio = 0.6`（セグメント数 ≥ `internal_min_lines = 6`）；
  - ≥ `char_run_min = 8` 個の同一の空白以外の文字の連なり；
  - 2–10 文字の短いフレーズが ≥ 5 回の繰り返し。

  内部警告はラベルごとにセッションで 1 回だけ発火します。
- `_MIN_CONTENT_LENGTH = 20` 文字未満の内容はスキップ。ツール呼び出しを含むモデル応答は丸ごとスキップします（ツールループの後に再チェックされます）。
- **推論内容は別個に追跡**されます（`additional_kwargs` の `reasoning_content` / `reasoning` / `reasoning_text`、および可視内容から抽出・剥ぎ取られるインラインの `<think>` / `<thinking>` / `<reasoning>` ブロック）。

**ストリーム途中の切断**は `RepetitionGuardWrapper`（`agent/wrapper/repetition_guard.py`）が担います: グラフの `astream` をインターセプトし、本モジュールの内部繰り返し検出器、同じセッション単位の内部警告重複排除ゲート、共有の `_STREAM_WARNING` 文言を再利用します。

**ワーカーのクリーンアップ：** 子セッション終了時、`SESSION_STATE_KEYS`（6 つのキー）が `state_register_mem` から削除されます。

### ContextLimitGuardWrapper

**モジュール：** `agent/wrapper/context_limit.py` · **クラス：** `ContextLimitGuardWrapper`

`RepetitionGuardWrapper` と同種のグラフラッパーであり、ミドルウェア**ではありません**。`agent/core.py` はプラガブル登録簿（`agent/wrapper/registry.py::apply_graph_wrappers`、内側から外側へ）の既定ラップチェーンを適用するため、コンパイル済みグラフは `ContextLimitGuardWrapper(RepetitionGuardWrapper(graph))` になります — ContextLimit は繰り返しガードの**外側**に位置し、繰り返しフィルタリングの前にストリームチャンクを目にします。ミドルウェアのストリーミング盲点を塞ぎます：ミドルウェアはストリーム途中のチャンクを見ず、応答後のオーバーフロー信号で後からコンテキストを圧縮することもできません。

**防御 1 — モデル呼び出し境界での強制圧縮：** 実際の `usage_metadata` 入力/出力トークンを `messages` チャンクから捕捉し、モデル呼び出し境界ごと（`updates` モード）とストリーム終端で、現在の呼び出し（`input_tokens` のみ）と予測ビュー（`input + output`、出力は次の呼び出しの入力になるため）の両方をコンテキストウィンドウの `COMPRESSION_TRIGGER_RATIO`（80 %）に対してチェックします。しきい値に達したら、Summarization の強制リカバリキー（`summarization_force_recovery`）を `state_register_mem` に設定し、次の呼び出し前チェックがクールダウン/試行上限のアンチスラッシングゲートに阻まれずに圧縮を実行するようにします。

**防御 2 — ストリーム途中の出力予算：** 蓄積されたモデルテキストはトークナイザ不要の CJK 対応 `estimate_text_tokens` で推定し（CJK 文字は `// 2`、それ以外は `// 4`、純 ASCII は従来の `len // 4` に退化）、`check_interval = 20` チャンクごとにチェックします。推定値がウィンドウの `output_cut_ratio = 0.20` を超えると、以降のテキストチャンクはクライアントへ転送されなくなり（ツール呼び出しチャンクは通過）、代わりに 1 回限りの切り詰めマーカー（`"[System notice: the response exceeded the mid-stream output budget and was truncated.]"`）が出力されます。グラフ内部では完全な `AIMessage` が蓄積され続けます — 切り詰められた末尾こそ、次の圧縮パスが除去する対象です。

`ainvoke` はそのまま委譲します（非ストリーミング経路は Summarization の T1–T3 トリガーが既にカバー）。未知の属性は内部グラフに委譲されます。コンストラクタ：`(inner, context_window, output_cut_ratio=0.20, check_interval=20)` — `context_window` は `MAIN_LLM_MAX_TOKEN` で、Summarization のトリガーと同じソースです。

---

## 共有状態システム

呼び出しをまたぐすべてのミドルウェア状態はセッション単位で、2 つのレジスタとタイマーレジストリに保持されます：

| レジスタ | バックエンド | 備考 |
|---|---|---|
| `state_register_mem`（`StateRegisterMeM`） | インメモリ辞書 | 揮発性。`_initialized` ガードによりプロセス開始時に 1 回だけリセット |
| `state_register_db`（`StateRegisterDB`） | SQLite（`src/data/state_register.db`） | 再起動後も保持。`clear_session` は非対応（`False` を返す）。`get_all_session_ids` を提供 |
| `timer_call_register`（`TimerCallRegister`） | asyncio タイマー | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

共通インターフェース（`runtime/session/state_register.py`）：`set_state`、`get_state`、`get_all_states`、`delete_state`、`clear_session`、`has_session`、`has_key`、`update_states`。

### 名前空間の規約

| キー | 所有者 | レジスタ |
|---|---|---|
| `system_prompt` | system_prompt_injection / Summarization | mem + db |
| `nudge_plan_extraction_fired` | 圧縮時 nudge スケジューラ（Summarization → `summarization/nudges.py`） | db |
| `nudge_review_memory_lock`、`nudge_plan_extraction_lock` | 圧縮時 nudge スケジューラ（Summarization → `summarization/nudges.py`） | mem |
| `iteration_budget`、`iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_*` キー（圧縮カウンター、無効連続、直近トークン/戦略、スキップ LLM フラグ、リカバリ状態、直近ユーザー質問） | Summarization | mem |
| `heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`heartbeat_skip`、`_last_heartbeat_iter`、`_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard のキー（`SESSION_STATE_KEYS`、6 つ） | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `llm_stale_streak`、`llm_fallback_index` | LLMRetryMiddleware | mem |
| `_multimodal_trying_native`、`_multimodal_native_model` | MultimodalProcessor（書き込み）→ LLMRetryMiddleware（消費） | mem |
| `llm_content_filter_blocked`、`llm_content_filter_terminated` | ストリーム層（書き込み）→ LLMRetryMiddleware（消費） | mem |
| `llm_partial_stream_stub`、`llm_partial_stream_cause` | ストリーム層（書き込み）→ LLMRetryMiddleware（消費） | mem |
| `summarization_force_recovery` | ContextLimitGuardWrapper（書き込み）→ Summarization（消費） | mem |
| `hitl:` 接頭辞キー（`_STATE_PREFIX = "hitl"`） | HumanInTheLoop | mem |

---

## 設定

### 環境変数と設定ノブ

| ノブ | 場所 | 効果 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | メインエージェントの要約トリガー = この値の 80 %。`main_llm_context_window` としても `ContextLimitGuardWrapper.context_window` としても渡す。131072 (128K) 以上が必要：token guard が起動時とグラフ構築時にブロックする |
| `MAIN_LLM_OUTPUT_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 出力トークン予算（デフォルト 8192）：MaxTokensBoost ブースト base のレイヤー 2、思考予算膨張が加算されるベース |
| `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` | `.env` → `build_fallback_chain()` | `LLMRetryMiddleware` のモデルフォールバックチェーン候補（i = 1…、最初に `NAME` が欠けた時点で停止） |

### フィーチャー設定（`config/features/agent_side/`）

ミドルウェアのチューニングはオブジェクト単位の TypedDict モジュールに置かれます — 環境変数が供給するのは上表の 2 つの LLM 予算だけです。本層が消費するノブ：

| モジュール | ノブ |
|---|---|
| `iteration_budget.py` | `main_agent_max_iterations=90`、`worker_max_iterations=60`、`default_max_iterations=50` |
| `media_pipeline.py` | `main_llm_native_multimodal="auto"`（`"true"` / `"false"` / `"auto"`。それ以外はフェイルセーフのスキルパス）、`main_llm_silent_degradation_detection=True`、`max_media_bytes=20 MiB`、`multimodal_temp_retention_days=7` |
| `summarization.py` | `compression_trigger_ratio=0.80` に加え、`Summarization` と `summarization/nudges.py` が読む保持/試行/クールダウン/しきい値の各定数 |
| `tool_result_eviction.py` | `enabled=True`、`evict_threshold_chars=20 000`、プレビュー先頭/末尾 5 行、`human_evict_enabled=True`、`human_evict_threshold_chars=200 000` |
| `tool_guardrails.py` | ToolGuardrails セクションに列挙した 5 病理のしきい値と復帰デフォルト |
| `heartbeat_staleness.py` | `heartbeat_interval_minutes=1`、`stale_cycles_idle=7`、`stale_cycles_in_tool=20` |
| `repetition_guard.py` | `max_identical_outputs=3`、`warn_after=2`、`internal_repeat_ratio=0.6`、`internal_min_lines=6`、`char_run_min=8`、`tail_chars=500`、`max_history=30` |
| `max_tokens_boost.py` | `base_max_tokens`（`MAIN_LLM_OUTPUT_MAX_TOKEN`、既定 8192）、`default_max_tokens=8192`、`max_cap=32 768`、`max_retries=3` |
| `llm_retry.py` | `max_retries=3`、`base_delay=2.0`、`max_delay=60.0`、`jitter=0.3`、`stale_giveup_threshold=5` |
| `context_guard.py` | `output_cut_ratio=0.20`、`check_interval=20` |

> **関連だが独立：** ツールごとのタイムアウトはフィーチャーレジストリに束縛されたモジュール定数です（`config/features/agent_side/tools_timeouts.py` の `TOOLS_TIMEOUTS`） — `WEB_SEARCH_TIMEOUT = 15`（`agent/tools/web_search.py`）、`TERMINAL_TIMEOUT = 30`（`agent/tools/terminal.py`）、`PYTHON_REPL_TIMEOUT = 30`（`agent/tools/python_repl.py`。期限切れで子プロセスは kill されます）。`TOOL_CALL_TIMEOUT_MINUTES`（既定 5）は `sherry.jsonc` の設定で、`config/sherry_settings.py` を通り設定サービスから公開されますが、**これを消費するツール実行経路はありません** — 有効なタイムアウトノブではありません。要約パイプラインは `config/features/agent_side/summarization.py` から定数を読み取ります。

### ビルド例（抜粋）

登録済みミドルウェアをすべて示しているわけではありません — 完全なメインエージェントのリストは[ミドルウェアチェーン](#ミドルウェアチェーン)を参照。

```python
from langchain.agents import create_agent
from agent.middlewares import (
    system_prompt_injection,
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
        system_prompt_injection,  # @dynamic_prompt：システムプロンプト注入
        MultimodalProcessor(),  # マルチモーダル入力の正規化
        IterationBudget(90),  # ターン単位の呼び出し予算
        ToolGuardrails(),  # 失敗病理の検知
        ToolCallNormalize(),  # tool_use/tool_result の修復
        PathGuard(),  # パス引数のスクリーニング
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
│   MultimodalProcessor → IterationBudget → ToolGuardrails → OutputRepetitionGuard
│   → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · MultimodalProcessor  メディアを永続化し、設定 / 能力キャッシュに従いネイティブブロックを保持するかスキルヒントを付与
│   · IterationBudget  予算カウンターをリセット
│   · ToolGuardrails  ターン単位のガード状態をリセット
│   · OutputRepetitionGuard  ターン単位の繰り返し状態をリセット
│   · HeartbeatStaleness  状態キーをリセット + 1 分間隔のハートビートタイマーを起動
│   · HumanInTheLoop  ターン単位の中断フラグをリセット
│   · Summarization  圧縮カウンターをリセット
│
├─ ループ：モデル呼び出し
│   ├─ before_model
│   │   · ContextEvictionMiddleware  末尾の巨大な HumanMessage にタグ付け（P1-9）
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage 書き換え
│   │   · SubagentCompletionDrainMiddleware  キューされた完了キャリアをドレイン
│   │   · TaskIntentMiddleware  タスク意図 / 計画アクティブのステアリング（ターンの最初の呼び出し）
│   ├─ wrap_model_call（最外層 → 最内層）
│   │   · system_prompt_injection  システムプロンプトを注入（デコレータが request.override を呼ぶ）
│   │   · MultimodalProcessor  auto モードのみ：未対応メディアブロックをテキストプレースホルダーへ置換（リクエストのコピーのみ）
│   │   · IterationBudget  1 消費。尽きたら終端 AIMessage
│   │   · ContextEvictionMiddleware  退避済み人間メッセージをプレビューへ置換（P1-9）
│   │   · OutputRepetitionGuard  呼び出しごとの繰り返しインターセプト
│   │   · MaxTokensBoostMiddleware  ツール呼び出しが切断されたときに再呼び出し
│   │   · HeartbeatStaleness  kill 済みなら HeartbeatTimeoutError、さもなくば heartbeat_iter += 1
│   │   · LLMRetryMiddleware  ブレーカー確認。分類済みリトライ + バックオフ。フォールバック /
│   │                        コンテンツフィルタ / 部分ストリームスタブフラグの消費
│   │   · Summarization  必要なら履歴を圧縮（非 LLM 戦略 + 補助 LLM）、アンチスラッシングカウンター
│   ├─ LLM が応答
│   └─ after_model（逆順；永続化が先）
│       · MessagePersistenceMiddleware  新しい human/ai/tool メッセージを MesMemory へ増分フラッシュ（ウォーターマークで write-once）
│       · HumanInTheLoop  ポリシーチェック。必要なら interrupt()。ブロック → エラー ToolMessage
│
├─ ループ：ツール呼び出し（呼び出しごと）
│   └─ wrap_tool_call
│       · IterationBudget  1 消費。尽きたらエラー ToolMessage
│       · ToolGuardrails  block/halt を事前チェック → 実行 → 評価 → warn/block/halt
│       · ContextEvictionMiddleware  生の結果を退避/スライス済みビューへ置換
│       · PathGuard  ツール実行前にトラバーサル / ハード拒否のパス引数を拒否
│       · HeartbeatStaleness  kill 済みなら送出。heartbeat_tool を設定し、返却後にクリア
│       · HumanInTheLoop  承認が拒否/タイムアウトした呼び出しを拒否
│       · MessagePersistenceMiddleware  返された ToolMessage を即永続化
│         （最内層 wrap。HITL 拒否は迂回し、次の境界で永続化）
│
└─ after_agent（逆順）
    HeartbeatStaleness → MultimodalProcessor → TodoContinuationEnforcer
    · HeartbeatStaleness  ハートビートタイマーを停止
    · MultimodalProcessor  mutil_temp を清掃（7 日超 / 非数値ファイル名）
    · TodoContinuationEnforcer  未完了の todo が残っていれば継続プロンプトを fire-and-forget で注入
    · （system_prompt_injection はライフサイクルフックを実装しません；メッセージ永続化は
       専用の after_model フックで走り、メモリレビュー / プラン抽出 nudge は代わりに
       Summarization の圧縮パス内で発火します。）
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
├── llm_capability_cache.py      # プロセス単位のネイティブ・マルチモーダル能力キャッシュ
├── context_eviction/            # ContextEvictionMiddleware（P0-2/P2-4 + P1-9）
│   ├── __init__.py              # ContextEvictionMiddleware をエクスポート
│   └── core.py                  # ContextEvictionMiddleware
├── system_prompt/               # @dynamic_prompt システムプロンプト注入
│   ├── __init__.py              # system_prompt_injection のみエクスポート
│   └── core.py                  # system_prompt_injection + _get_and_reload_system_prompt
├── heartbeat_staleness/         # HeartbeatStaleness
│   ├── __init__.py              # HeartbeatStaleness をエクスポート
│   └── core.py                  # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig（独自の README を持つ）
│   ├── __init__.py              # HumanInTheLoop、HITLConfig をエクスポート
│   ├── types.py                 # 列挙型 + 設定データクラス（_STATE_PREFIX = "hitl"）
│   ├── detection.py             # ハードライン / 危険コマンドパターン
│   ├── approval.py              # ApprovalPipeline
│   ├── approval_scope.py        # 永続承認のオペレータースコープ解決
│   ├── approval_store.py        # ToolApprovalStore（SRC_DIR/data/approvals.json、CAS）
│   ├── gates.py                 # WriteApprovalGate、InterruptManager、MCPElicitationConsent、
│   │                            # KanbanTriage、PairingStore、SlashConfirm
│   ├── strategies.py            # ToolApprovalHandler 分岐 + ApprovalHandlerRegistry
│   └── core.py                  # HumanInTheLoop
├── iteration_budget/            # IterationBudget
│   ├── __init__.py              # IterationBudget をエクスポート
│   └── core.py                  # IterationBudget
├── llm_retry/                   # LLMRetryMiddleware（LLMRetryConfig、FallbackCandidate、ContentFilterError を含む）
│   ├── __init__.py              # LLMRetryMiddleware のみエクスポート
│   └── core.py                  # LLMRetryMiddleware、LLMRetryConfig、FallbackCandidate、
│                                # ContentFilterError
├── max_tokens_boost/            # MaxTokensBoostMiddleware（ツール呼び出し切断の再呼び出し）
│   ├── __init__.py              # MaxTokensBoostMiddleware をエクスポート
│   └── core.py                  # MaxTokensBoostMiddleware
├── media_pipeline/              # MultimodalProcessor
│   ├── __init__.py              # MultimodalProcessor をエクスポート
│   ├── core.py                  # MultimodalProcessor
│   ├── degradation.py           # サイレント劣化検出器（自己申告の失明）
│   ├── fallback.py              # スキルパスへのフォールバック（メディアヒント + リクエスト書き換え）
│   ├── media_handlers.py        # メディアタイプ別戦略 + MediaPaths（サイズ上限付き）
│   ├── scrub.py                 # リクエスト単位で未対応メディアブロックをスクラブ
│   └── mixins.py                # BeforeAgentHooksMixin / AfterAgentHooksMixin（共有）
├── message_persistence/         # MessagePersistenceMiddleware
│   ├── __init__.py              # MessagePersistenceMiddleware をエクスポート
│   ├── core.py                  # MessagePersistenceMiddleware（after_model / aafter_model）
│   └── prepare.py               # 永続化バッチのフィルタ + HITL 拒否の再ペアリング
├── output_repetition_guard/     # OutputRepetitionGuard
│   ├── __init__.py              # OutputRepetitionGuard をエクスポート
│   ├── core.py                  # OutputRepetitionGuard（__init__.py が再エクスポート）
│   ├── repetition_detectors.py  # 純粋な繰り返し検知プリミティブ
│   └── repetition_state.py      # セッション単位の繰り返し状態ヘルパー
├── path_guard/                  # PathGuard（ツール呼び出しのパス引数スクリーニング）
│   ├── __init__.py              # PathGuard のみエクスポート
│   └── core.py                  # PathGuard
├── subagent_completion_drain/   # SubagentCompletionDrainMiddleware
│   ├── __init__.py              # SubagentCompletionDrainMiddleware をエクスポート
│   └── core.py                  # SubagentCompletionDrainMiddleware
├── summarization/               # Summarization
│   ├── __init__.py              # Summarization をエクスポート
│   ├── core.py                  # Summarization
│   ├── compression.py           # 圧縮の適用: カットオフ + 非 LLM 戦略パイプライン（ロック内実行）
│   ├── overflow.py              # T1–T5 オーバーフロー経路 + provider エラー復帰リング
│   ├── summary_generation.py    # LLM 要約プロンプト/チェーン/フォールバック + 復帰コンテキスト抽出
│   ├── thrash.py                # アンチスラッシングカウンタ、クールダウン管理、劣化モニタ
│   ├── state_aliases.py         # summarization 状態キーの短縮エイリアス（StateKey 値）
│   ├── summarization_components.py # Summarization 共有コンポーネント（_FORCE_RECOVERY_KEY など）
│   ├── compaction_lock.py       # SQLite 圧縮ロック（TTL、fail-open）
│   ├── media_offload.py         # 圧縮時のインラインメディア退避
│   ├── memory_flush.py          # 圧縮前メモリフラッシュ
│   ├── nudges.py                # 圧縮時 nudge のスケジューリング + プロンプト
│   ├── plan_context.py          # プラン抽出 nudge のアクティブプラン検出
│   └── summary_doc.py           # SummaryDoc スキーマ + 決定論的 Markdown レンダラー
├── task_intent/                 # TaskIntentMiddleware
│   ├── __init__.py              # TaskIntentMiddleware をエクスポート
│   └── core.py                  # TaskIntentMiddleware
├── todo_continuation/           # TodoContinuationEnforcer
│   ├── __init__.py              # TodoContinuationEnforcer をエクスポート
│   └── core.py                  # TodoContinuationEnforcer
├── tool_call_normalize/         # ToolCallNormalize
│   ├── __init__.py              # ToolCallNormalize をエクスポート
│   └── core.py                  # ToolCallNormalize
├── tool_guardrails/             # ToolGuardrails
│   ├── __init__.py              # ToolGuardrails をエクスポート
│   └── core.py                  # ToolGuardrails
└── README.md                    # このファイル（+ .zh / .ja / .ko 版）

agent/wrapper/registry.py             # 順序付き・プラガブルなグラフラップチェーン
agent/wrapper/repetition_guard.py     # RepetitionGuardWrapper（本パッケージの外に存在）
agent/wrapper/context_limit.py        # ContextLimitGuardWrapper（本パッケージの外に存在）
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
    ContextEvictionMiddleware,
    IterationBudget,
    system_prompt_injection,
    ToolCallNormalize,
    PathGuard,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
    MessagePersistenceMiddleware,
    llm_capability_cache,  # モジュール：プロセス単位のネイティブ・マルチモーダル能力キャッシュ
)
# 共有ヘルパーもエクスポートされています：BeforeAgentHooksMixin、
# AfterAgentHooksMixin、require_session_id、args_hash。
# SubagentCompletionDrainMiddleware / TaskIntentMiddleware / TodoContinuationEnforcer
# は agent/core.py が各サブモジュールからインポートします — パッケージは再エクスポートしません。
```
