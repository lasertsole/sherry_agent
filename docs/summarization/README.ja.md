# 🗜️ コンテキスト圧縮：Summarization ミドルウェア

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが長い会話をモデルのコンテキストウィンドウの中に収め続ける仕組み: 5つのトリガーポイントがライフサイクル全体（ターン開始前、すべてのモデル呼び出し前、すべてのモデル応答後、プロバイダのオーバーフローエラー時）を監視し、純粋関数型の4ルートルーターが最も安価な修復手段を選び（まず大きいツール結果と過大なツール呼び出し引数を切り詰め、強制されたときだけ AI 圧縮）、アンチスラッシングガードが圧縮の暴走を構造的に防ぎます。

一次情報: `agent/middlewares/summarization/core.py`、`pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/llm_error_classifier.py`、`pub/func/estimate_tokens.py`、`pub/func/message/tool_output_dedup.py`、`pub/func/message/tool_output_prune.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`pub/func/message/turn_utils.py`、`config/features/agent_side/summarization.py`、および 2 つの登録箇所 `agent/core.py` と `agent/tools/subagent/spawn/core.py`。本文書の行番号と定数はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [ライフサイクル：5つのトリガーポイント（T1–T5）](#-ライフサイクル5つのトリガーポイントt1t5)
- [4ルート・オーバーフロー判定](#-4ルートオーバーフロー判定)
- [トークン推定（トークナイザなし）](#-トークン推定トークナイザなし)
- [切り詰めトラック：予算切り詰めとTTLモジュール](#-切り詰めトラック予算切り詰めとttlモジュール)
- [圧縮トラック：`_apply_compression` の内部](#-圧縮トラック_apply_compression-の内部)
- [LLM要約：プロンプト、チェイニング、フォールバック](#-llm要約プロンプトチェイニングフォールバック)
- [静的フォールバック（LLMを使わない要約）](#-静的フォールバックllmを使わない要約)
- [出力：要約メッセージペア](#-出力要約メッセージペア)
- [スラッシング防止マトリクスと劣化リカバリ](#-スラッシング防止マトリクスと劣化リカバリ)
- [システムプロンプト更新](#-システムプロンプト更新)
- [登録箇所](#-登録箇所)
- [設定リファレンス](#-設定リファレンス)
- [テスト](#-テスト)
- [⚠️ 正直な限界](#%EF%B8%8F-正直な限界)

## 🎯 概要

`Summarization`（`agent/middlewares/summarization/core.py`、クラスは 500 行）は**ゼロから実装された** `AgentMiddleware` であり、LangChain 組み込みの `SummarizationMiddleware` を継承し**ません**。エージェントライフサイクルの正確に 2 箇所だけにフックします:

- `before_agent` / `abefore_agent`（1946 / 1950 行）—— **T1 事前点検**
- `wrap_model_call` / `awrap_model_call`（1960 / 2046 行）—— **T2 ディスパッチ、T3 応答後の再確認、T4/T5 エラー復帰リング**

ミドルウェアチェーンでは**最も内側 —— LLM に最も近い**位置に置かれます。圧縮が発動すると、履歴は常に次の形になります:

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

置き換え後のものが Human/AI ペアであるため、モデルは連続する同役割のメッセージを決して見ず、ペアリング修復も不要です。

登録箇所は 2 つあります:

| 箇所 | トリガー | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| メインエージェント（`agent/core.py:152`） | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| ワーカー/サブエージェント（`agent/tools/subagent/spawn/core.py:755`） | `("messages", 40)` **または** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`（デフォルト） |

どちらも `main_llm_context_window=main_llm_max_tokens`（`MAIN_LLM_MAX_TOKEN` 由来）と `keep=("messages", 10)` を渡します。

## 🧭 ライフサイクル：5つのトリガーポイント（T1–T5）

```
ターン開始
│
├─ T1  before_agent 事前点検  (_t1_preflight :1886 / _at1_preflight :1917)
│      ├─ _reset_turn_state (:1849) が 11 個のターン単位カウンタをリセット
│      ├─ _decide_overflow_route (:632) → None / "fits" → 通過
│      ├─ クールダウン > 0 なら COMPACT ルートを封鎖; 切り詰めトラックは
│      │  それでも実行（それ自体が最安の復帰メカニズム）
│      └─ ディスパッチ(trigger="T1") + _t1_state_update (:1862) が結果を
│         グラフにコミット:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         （add_messages リデューサは自前ではメッセージを消さない ——
│         RemoveMessage センチネルが、圧縮された接頭部が状態から実際に
│         出て行く唯一の経路）
│
├─ T2  wrap_model_call、ハンドラ以前（:1960 同期 / :2046 非同期）
│      ├─ force フラグ（:1973）をスキップゲートより先に読む —— スキップ
│      │  ゲート（_should_skip_compression :1255）がフラグを消費するため
│      ├─ _tick_cooldown（:832）: すべての呼び出しがクールダウンを減算
│      ├─ アンチスラッシングゲート（:1984–1987）:
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      通過（直前に compact が起きていればシステムプロンプトを
│      │      再構築、:1993–2005）→ ハンドラ → モニタ → T3
│      ├─ さもなくば: 4ルート判定（:2019）→ _dispatch_overflow_route;
│      │  レガシートリガー節が発火すれば（_check_trigger :576、例:
│      │  ("messages", 40)）→ ROUTE_COMPACT_ONLY（:2027）
│      └─ 3 箇所のハンドラ呼び出し（:1979、:2005、:2030）はすべて
│         _execute_with_recovery（:1057）の中で実行 —— すなわち T4/T5 リング
│
├─ T3  応答後の再確認  (_post_response_check :849 / 非同期 :922)
│      ├─ T2 が今回の wrap 呼び出しで既に圧縮していればスキップ
│      │  （t2_compressed フラグ、:2032–2035）—— モデル呼び出しにつき
│      │  圧縮は正確に一度
│      ├─ extract_reported_input_tokens(response)（:130）; None → 返却
│      ├─ ゲート: ターン試行上限、クールダウン、使用可能予算
│      ├─ pressure = max(推定値 + システムプロンプト, 報告値) ——
│      │  プロバイダが報告した入力トークンが優先（compute_pressure）
│      ├─ pressure < usable × 0.80 → 返却; ルートが "fits" → 返却
│      └─ ディスパッチ(trigger="T3")し、常に元の応答を返す; 関数全体が
│         fail-open（例外 → ログ、元の応答はそのまま保持）
│
└─ T4/T5  プロバイダエラー復帰リング
       (_execute_with_recovery :1057 / _aexecute_with_recovery :1115)
       ├─ ハンドラが例外を投げる → classify_provider_error
       │  （pub/func/message/llm_error_classifier.py）:
       │  payload_too_large → T4、context_overflow → T5
       │  （_TRIGGER_BY_ERROR_CLASS :115、_RETRY_KEY_BY_ERROR_CLASS :119）
       ├─ 対象外 / 未分類 → 元の例外をそのまま再スロー（再試行 0 回、
       │  状態書き込み 0 件、決して飲み込まない）
       ├─ 再試行 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  （:985 / 非同期 :1030）: LLM を使わないテールクリップが最初に走る ——
       │  クリップ単独で推定値が使用可能予算を下回った場合、スタブ済みの
       │  テール結果でハンドラが再試行され、compact は一切行われない;
       │  既にスタブ済みのリクエストではクリップは no-op になるため、次の
       │  試行は「compact + 予算切り詰め」ステップに劣化する。このステップは
       │  構造上すべてのアンチスラッシングゲートを迂回（クールダウン、
       │  ターンごとの上限、_should_skip_compression はいずれも参照されない）;
       │  クールダウンを武装せずターン試行も数えないが、セッション統計の
       │  真実性のため _record_compression は通る; クラス別再試行カウンタは
       │  成功後にのみ増える（:1021）
       ├─ 再試行の枯渇 → 元の例外を再スロー（エラーフレームは messages.py
       │  → turn_runner.py へ伝播 —— 空の応答で置き換えられることはない）
       └─ 強制圧縮ステップ自体の失敗 → 元の例外を再スロー
          （raise exc from compression_exc）。_monitor_degradation は
          リングが返った後の最終成功応答に対して一度だけ実行される。
```

レガシートリガー節は T2 のフォールバックとして今も存在します（`_check_trigger`、:576）: `("messages", N)` は履歴長で、`("tokens", N)` は `max(ローカル推定値, 最後の AIMessage が報告した usage_metadata.total_tokens)` ≥ N で発火します。節リストは OR です。

## 🚦 4ルート・オーバーフロー判定

`pub/func/message/overflow_router.py` は**純粋な判定レイヤ**です —— 切り詰めも、圧縮も、I/O も、状態もありません。ミドルウェアはここから 3 つの関数をインポートします:

- `compute_pressure`（:50）= `max(estimated_tokens + system_prompt_tokens, reported_tokens)` —— API が報告した値があればそれが優先;
- `find_truncatable_tool_results`（:68）—— 後備資格を持つのは **`ToolMessage` のみ**（ツール結果は再生成可能）; 直近 `TRUNCATABLE_RECENT_SKIP (6)` 件のメッセージは常に除外され、最新の tool/ai ペアリングが完全に保たれる; 候補は推定トークン ≥ `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200)` である必要がある; 結果はトークン降順ソートなので、実行者は最大の取り分から切る;
- `decide_route`（:103）—— ディスパッチ契約（文字列は固定）:

| 圧力（`p`）と `usable` の関係 | 切り詰め候補なし | 候補あり | 候補トークン合計 vs オーバーフロー量（`p − usable`） |
| :------------------------- | :------------------------ | :--------------- | :--------------------------------------------- |
| `p < 0.70 × usable` | `fits` | `fits` | — |
| ソフトオーバーフロー `0.70 × usable ≤ p < 0.80 × usable` | `fits` | `truncate_tool_results_only` | —（ソフトオーバーフロー**だけ**で圧縮が発火することはない） |
| ハードオーバーフロー `p ≥ 0.80 × usable` | `compact_only` | 合計 ≥ オーバーフロー → `truncate_tool_results_only`; 合計 < オーバーフロー → `compact_then_truncate` | オーバーフロー量 = `p − usable` |

3 つの閾値入力はすべて、生のウィンドウではなく**使用可能予算**から導出されます:

```
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :615
system_est     = estimate_text_tokens(system_prompt)   # _estimate_system_prompt_tokens :770
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

唯一の実行者 `_dispatch_overflow_route`（:760 同期 / :800 非同期）が T1、T2、T3 の**すべて**に仕えます —— 2 番目の複製は決して作りません:

- `truncate_tool_results_only` → `_run_budget_truncation`（:659）—— ステップ 1 が過大なツール呼び出し引数を切り詰め（新しいメッセージを返す、下記の切り詰めトラック参照）、ステップ 2 がツール結果をその場で切り詰める —— その後**再確認**: 解放されたトークンが足りなければ（`new_tokens ≥ usable × 0.80`、返されたリスト上で推定）`compact_then_truncate` に昇格; 足りていれば圧縮なしで通過;
- `compact_only` / `compact_then_truncate` → `_execute_compact`（:702 / 非同期 :731）→ `_apply_compression`（例外はログ記録、リクエストは元のまま）→ `_record_compaction_bookkeeping`（:694: クールダウンの武装、ターン試行 1 回の計上）→ `compact_then_truncate` はさらに圧縮結果に予算切り詰めをバックストップとして実行 → 旧/新トークンと圧力比とともにルートをログ記録。

**P1-2 高速パス —— LLM を使わないテールクリップ。** いずれのルートも実行される前に、`_fast_tail_clip` が `clip_overflow_tail`（`pub/func/message/overflow_clip.py`）を実行します: 末尾の連続する `ToolMessage` バッチがコンパクトなスタブに置き換えられ、その置換は `ToolMessage.model_copy` を通るため、メッセージの削除も注入も起こりません —— `id`、`tool_call_id`、`name`、`additional_kwargs` はすべて生存し、ペアリングのサニタイズと永続ウォーターマークは引き続き満たされます。予算: `target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`; `0` のターゲットは「最大の対象バッチを取る」ことを意味します（`overflow_clip_max_remove` が上限、`overflow_clip_min_keep` が下限）。`ratio` はルート経路では `COMPRESSION_TRIGGER_RATIO (0.80)`、T4/T5 の強制ステップでは `1.0`（使用可能予算未満）です。クリップ**単独**で推定値が線の下に落ちる場合のみ採用されます: リクエストはスタブ済みリストのまま返され、ルートは一切実行されません（予算切り詰めなし、補助 LLM 圧縮なし）。不十分なクリップは破棄され、既存ルートが元のリストそのままで実行されます。T4/T5 ではクリップは `request.messages` を読みます; 既にスタブ済みのリクエストでは no-op になるため、同一クリップで再試行予算が焼かれることはなく、次の試行は圧縮に劣化します。

**テール内容の破棄が安全な理由:** すべてのツール結果は返された瞬間に MesMemory へフラッシュ済みで（`MessagePersistenceMiddleware`）、`message_search` ツールを通じて引き続き取得できます。P0-2 で退避された結果はスタブ内に `[evicted to: …]` ポインタを保持し（`read_file` が引き続き機能します）、P2-4 でスライスされた `read_file` 結果はスライス通知をそのまま保持します; そしてスタブ済みメッセージはスキャン対象バッチを終了させるため、2 回目のクリップは no-op であり、これらのマーカーを決して壊しません。設定: `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`。

ウィンドウ算術（テスト契約）: ウィンドウ `41 600` → usable `25 600`、2 つの閾値線 `17 920` / `20 480`、切り詰め予算 `15 360`。テスト固定値 `MAIN_LLM_MAX_TOKEN = 65536` のとき（実行時の `.env` 値は 131072 / 128K 以上が必要）、登録済み T2 節は `52 428` に置かれます。

## 🪙 トークン推定（トークナイザなし）

`pub/func/estimate_tokens.py`（109 行）は意図的にトークナイザを使わず、決定論的に動作し、3 段階のフォールバックを持ちます:

- **T1 — API 報告使用量:** 最後の `AIMessage` が `usage_metadata` を持つ場合（または呼び出し側が `reported_tokens` を明示した場合）、`estimate_messages_tokens` はその値をそのまま返します — プロバイダーの実測値がローカル推定をすべてショートカットします;
- **T2 — CJK 対応ヒューリスティック:** `estimate_text_tokens` はテキストを CJK 文字（`// CHARS_PER_TOKEN_CJK = 2`）とそれ以外（`// CHARS_PER_TOKEN = 4`）に分け、検出には `pub.func.cjk.count_cjk` を再利用します;
- **T3 — レガシー `len // 4`:** 独立したコード経路ではなく、`count_cjk(text) == 0` のときの T2 の退化ケースです — そのため純 ASCII の推定値は旧来の数値と完全に一致します。

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# メッセージ単位: str content（または len(json.dumps(content))）
#            + Σ tool_call name/args 文字 + tool_call_id 文字
```

`pub/func/message/estimate_msg_tokens.py` は同じヘルパー群の後方互換 re-export になりました。速く、実行間で安定し（同じ入力 → 同じ数値 → 再現可能なテスト）、意図的に保守的な近似です。トリガー/予算経路のどの部分もモデルのトークナイザに依存しません。

## ✂️ 切り詰めトラック：予算切り詰めとTTLモジュール

`_run_budget_truncation`（:659）の中では 2 つの切り詰めレイヤがこの順で走ります:

**ステップ 1 —— ツール呼び出し引数**（`pub/func/message/tool_args_truncate.py`）: JSON 直列化が `MIN_ARGS_CHARS_TO_TRUNCATE (500)` 文字を超えるすべての `AIMessage.tool_calls[].args` —— そのツールが `PROTECTED_TOOLS` に含まれない場合 —— は、`MAX_TOOL_ARGS_CHARS (2_000)` 文字で封印される `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}` に置き換えられます（先頭 30% / 末尾 30%、ツール結果と同じ比率）。これで `args` は dict のまま（LangChain の `ToolCall.args` 型）で、すべてのプロバイダアダプタに対して JSON 直列化可能を保ち、モデルは引数が切られたことを見て取れます。直近 `TRUNCATABLE_RECENT_SKIP (6)` 件のメッセージはスキップされ、置き換えられた `AIMessage` は `model_copy` クローンです —— tool_call_ids は決して触られないため、AIMessage↔ToolMessage ペアリングは完全に保たれます。

**ステップ 2 —— ツール結果**: `pub/func/message/tool_result_ttl.py` は、切り詰めトラックが使うその場での切り詰めを提供します。設計不変量（構造を支えるもの）:

- **その場でのみ** —— このモジュールはメッセージを削除・並べ替え・pop しません; `msg.content`（または content リストブロック）を変更してインデックスを返すだけです。これがプロバイダ API と `ToolCallNormalize` が依存する tool-call/`ToolMessage` ペアリングを守ります。
- **プレースホルダは空にしない** —— 切り詰められた結果は常に空でない内容を保持します: `ToolCallNormalize.before_model` は**空の `ToolMessage` をドロップ**してトランスクリプトを浄化するため、空のプレースホルダは静かにペアリングを壊します。
- **先頭 30% / 末尾 30% 保持**（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）に省略マーカーを付けます。

ミドルウェアが実際に消費するもの: `truncate_tool_args`（ステップ 1、引数）と **`truncate_to_budget`**（ステップ 2、ツール結果）。ルーターの候補リストに駆動され、`_run_budget_truncation`（:659）が予算（`usable × TRUNCATE_BUDGET_RATIO`）を満たすまで候補を切り詰めます。ステップ 1 はミューテートせず新しい `AIMessage` を返すため、この関数は最終リストを返し、すべての呼び出し元はそのリストを `request.override` に**必ず**渡さなければなりません。

**read_file の結果は復元可能なまま**: `pub/func/message/target_truncation.py` の先頭+末尾クリップ（非 LLM 戦略 `_run_non_llm_strategies` が実行）は、各 `ToolMessage` を `tool_call_id` で AIMessage のツール呼び出しに引き戻します; ツールが `read_file` で `args.file_path` がある場合、切り落とされた中間は匿名マーカーではなく復元通知に置き換わります。通知は同じ先頭 30% / 末尾 30% の比率を保ち、元の `file_path` を明記し、`Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)` を示します。`N` は**先頭に完全には残っていない最初の行の絶対（1-based）ファイル行番号** —— したがって `offset=100` で読んだページは先頭が実際に止まった位置から再開し、途中で切れた行は再読され、決してスキップされません。offset を導出できない場合（ペイロードが read_file の JSON 結果でない場合）、通知は `offset=1` からの再読を求めます —— 推測した offset は決して出しません。その他のツールは匿名の `...[truncated N chars]...` マーカーをバイト単位で保ちます。

TTL レジストリ本体（`record_first_seen` / `select_expired` / `truncate_expired`、`PRUNE_TTL_SECONDS = 300`、`TTL_REGISTRY_MAX_ENTRIES = 512`、`tool_call_id` キー、再起動で揮発）は、現在**テストスイートだけが使用**しています —— ミドルウェアには年齢ベースの有効期限ロジックは接続されていません（「正直な限界」参照）。

## 🔁 圧縮トラック：`_apply_compression` の内部

`_apply_compression`（:1688; 非同期ツイン :1760）は次の順で実行されます:

1. **復帰コンテキストの捕捉**（`_capture_recovery_context`、:1577）: 最後のユーザー要求（≤ 800 文字）とファイル操作ラチェット —— `read`/`write` 系ツール呼び出しからパスを抽出し（:415）、前回分の集合とマージ（読んだものは記憶され、変更されたファイルが読み取り専用に格下げされることはない）。
2. **非 LLM 戦略**（`_run_non_llm_strategies`、:1493）: `重複排除 → プルーン → ターゲット切り詰め → ツール引数切り詰め`（詳細は下記）。これらはタダです —— モデル呼び出しなし。
3. **LLM を使うかの判定**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM での縮小が最初の機会を得ます; 履歴がまだ保持予算の 2 倍を超えるとき（またはガバナが LLM 要約を無効化しているとき、または非 LLM 戦略が何も減らせなかったとき）にのみ、補助 LLM に金を払います。
4. **アグレッシブ・バックストップ**（`_aggressive_truncate`、:1539）: 結果が*それでも*大きすぎる場合、`AGGRESSIVE_TRUNCATE_CHARS (1 000)` 文字を超えるすべての `ToolMessage` がマーカー付きでハードカットされます —— 同じ上限を超えるすべてのツール呼び出し引数 JSON も同様です（先頭のみ、`PROTECTED_TOOLS` は免除、`{"_truncated_args": ...}` に置き換え）。
5. **要約の自己切り詰め**（`_truncate_summary_messages`、:1630）: `SUMMARY_TOTAL_MAX_CHARS (16 000)` 文字を超える既存の要約メッセージ（`lc_source == "summarization"`）は、先頭 30% / 末尾 30% に再切り詰めされます（`_truncate_content`、:1622）。
6. **復帰コンテキストの注入**（`_inject_recovery_context`、:1595）: 捕捉したファイル操作ラチェットが要約の `## Relevant Files` セクションに書き込まれ、チェックポイントが常に最新の読み取り/変更ファイルマップを運ぶようにします。
7. **帳簿記録**（`_record_compression`、:1277）し、最後に `request.override(messages=..., system_message=...)`。

### 💾 圧縮時 nudge

メッセージ永続化は圧縮パスとは別に動作します: `MessagePersistenceMiddleware`（`agent/middlewares/message_persistence/`）が新しい human/AI メッセージをモデル呼び出しの各境界で、ツール結果を返却時に MesMemory へフラッシュし、永続ウォーターマーク `persisted_message_ids` で write-once を保証します。compact は圧縮と下記 nudge のスケジュールだけを行います。トリガー意味論は `agent/middlewares/README.md` を参照してください。

**圧縮時 nudge**（`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`）: メモリレビュー（`_nudge_memory`）は圧縮のたびにディスパッチされます; プラン抽出は同じ時点で `_detect_todo_all_complete` を評価します。どちらも NUDGE レーン上で fire-and-forget でディスパッチされ、モデル呼び出しをブロックしません。nudge ロックが保持されている間、圧縮はディスパッチを完全にスキップします（キューイングなし）。単発の `nudge_plan_extraction_fired` フラグは完了サイクルごとに 1 回の抽出を保証します —— そのため、一度も圧縮しないセッションはプラン抽出を発火しません。

**カットポイント選択**（`_determine_cutoff`、:1310）: 履歴をターンに分割し、**最新から逆方向**に歩きながら保持予算 `clamp(window × 0.25, 2 000, 15 000)`（`_calculate_preserve_budget`、:565）に照らして累積します; 丸ごと入らないターンはターン途中で割られることがあります。`_adjust_for_orphan_pairs`（:1340）がカットポイントを逆に歩き、`ToolMessage` が `AIMessage` のツール呼び出しから分離する状態がなくなるまで調整します。最終ターン比率ゲートが発火しない限り（最後のユーザーターン ≥ 全トークンの `LAST_TURN_RATIO_THRESHOLD (0.5)` —— `_check_last_turn_ratio`、wrap 入口 :1968/:2054 で呼び出し）、カットポイントが最後の `HumanMessage` を超えることはありません。

すべての失敗モードは fail-open です: `_apply_compression` が例外を投げてもログに記録されるだけで、元のリクエストがそのまま進行します —— 壊れた圧縮がターンを壊すことはありません。

## 📝 LLM要約：プロンプト、チェイニング、フォールバック

`_create_summary` / `_acreate_summary`（:1410 / :1435）:

1. **直列化**（`_serialize_for_summary`、:258）: 各メッセージがタグ付きの 1 行になります —— `[User]:`（≤ 2 000 文字）、`[Assistant]:`（≤ 2 000 文字）、`[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`、`[Tool result|Tool error] (id):`（> 2 000 文字 → 1 800 文字保持 + 省略マーカー）。
2. **前のチェックポイントのチェイニング**（`_extract_previous_doc` / `_extract_previous_summary`）: `additional_kwargs["lc_source"] == "summarization"` を持つ最新の `AIMessage` を探し、まず構造化 `summary_doc` ペイロードとして読み取ります（`<prior-summary>` 用に Markdown へ再レンダリング）。ペイロードのないメッセージ —— レガシーセッション、または free-form にフォールバックした回 —— は従来どおり `<summary>…</summary>` 本体から解析します。前回の Doc が存在すれば、プロンプトは `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED` になります。レガシー Markdown は `<prior-summary>` 経由でそのまま注入され、アップグレード後の最初の圧縮ラウンドで新しい `SummaryDoc` が出力されます（移行不要）。
3. **構造化出力**（`summary_doc.py::SummaryDoc`）: 補助モデルは `with_structured_output(SummaryDoc, method="json_mode")` でラップされます。設定済みの `glm-5.3-flash` エンドポイントは関数呼び出しスキーマを無視するため（デフォルト method は自由テキストを返し Pydantic パーサーに拒否されます —— 実測済み）、`json_mode` が主ティアです。解析/検証の失敗は生呼び出し + `json_repair`（`_sync_json_repair_doc` / `_async_json_repair_doc`）へ降格し、それも失敗すればレガシー free-form Markdown プロンプトが最後の LLM ティア、静的フォールバックが最終ガードです。
4. **呼び出し**は補助モデルに対し `config={"metadata": {"lc_source": "summarization"}}` 付きで行われ、下流のツールチェーンが要約呼び出しを識別できるようにします。
5. **ガードレール:** free-form 応答が空または極端に短い場合は決定論的要約へフォールバックし、例外も同様です。失敗時に LLM が最後の言葉を持つことはありません。

**レンダリング（Doc → Markdown）。** `render_summary_markdown` は純粋かつ決定論的（同じ Doc → 同じバイト、プレフィックスキャッシュ安全）です。従来のセクション骨格を出力し、配列が非空の場合のみ *Active Plan Notes* / *Evicted References* を追加します:

| `SummaryDoc` フィールド | レンダリングされるセクション | コード層 cap |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | 逐字、上限なし |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes`（非空時のみ） | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References`（非空時のみ） | `evicted_refs[-20:]` |

cap は `cap_summary_doc` の配列スライス（チェーンに保存される形）で、レンダラーは表示時に再度スライスし `"(N earlier items omitted for brevity)"` を追記します。`evicted_refs` はコードが維持します: `_collect_evicted_refs` が圧縮範囲内の `[evicted to: <path>]` マーカーと人間メッセージの `lc_evicted_to` タグを走査し、`_finalize_summary_doc` が前回 Doc のエントリと新規エントリを順序どおり重複排除してマージします。`_inject_recovery_context` はファイル操作ラチェットを、レンダリング済み `## Relevant Files` セクションと保存 Doc の `relevant_files` フィールドの両方へ書き戻します。

**チェイニング要約のフィルタリング**（`_filter_summary_messages`）: 前のチェックポイントが存在する場合、その Human/AI ペアは直列化された `<conversation>` 入力から除去されます —— 抽出済みの前回要約は `<prior-summary>`（または `<prior-summary-json>`）経由でのみ注入され、古い要約テキストはプロンプト内にちょうど 1 回だけ現れます。ペアは**両方**が `additional_kwargs={"lc_source": "summarization"}` を持ち、これにより両方とも MesMemory に入りません（`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`）: このペアは圧縮の内部成果物であり、会話履歴ではありません。フィルタは `_extract_previous_doc` / `_extract_previous_summary` の**後**に走り（チェイニングは旧要約を引き続き参照できます）、フィルタ後のリストが直列化・プロンプト構築・2 つの静的フォールバック分岐（LLM 失敗/短すぎる応答）に使われます —— `_apply_compression_under_lock` / `_aapply_compression_under_lock` の `skip_llm` パスも同様です。opencode-dev の `hidden` セットと deepagents の `_filter_summary_messages` に整合します。

レガシープロンプトテンプレート（`_SUMMARY_TEMPLATE`）は free-form フォールバックの骨格を引き続き固定します —— *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress（Completed ≤ 5 · In Progress · Blocked）/ Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* —— 「空でもすべてのセクションを保持する」ことと秘密保持ルール（"NEVER include API keys, tokens, passwords, secrets"）を要求します。構造化パスは Markdown 骨格を `_SUMMARY_JSON_RULES`（JSON フィールド一覧 + 同じ秘密保持ルール）に置き換えます。フィールドの意味は Pydantic `SummaryDoc` モデル自体に定義されています。

**ユーザー要求の送信元の正同定。** 永続化された各 `human` 行は `origin` を持ちます（トランスポート入口で刻印）: WS/チャネルのユーザー入力は `"user"`、オーケストレーターのステアリング注入は `"task_intent"`、完了キャリアは `"subagent_completion"`、Cron 配信ターンは `"cron"`（`ai`/`tool` 行は `NULL` のまま；origin タグ導入前の行はレガシー user メッセージとして読み取られます）。*Latest Unresolved User Request* の送信元はこの列で正同定されます: ユーザー送信元のみ（`origin = 'user'`、またはレガシー `NULL`）がユーザー要求であり、内部注入（`task_intent` / `subagent_completion` / `cron`）が要求として引用されることはありません。routing plan の複数形 `unresolved_user_requests[]` リストも同じ正フィルタを使います。

## 🧱 静的フォールバック（LLMを使わない要約）

`_build_static_fallback_summary`（:296）はモデル呼び出しゼロで同じセクション骨格を生成します:

- 最後のユーザー要求 → *Latest Unresolved User Request*; 最初の要求 → *Goal*;
- 決定キーワード（`decided`、`choosing`、`because`、`therefore`）を含む AI テキスト → *Key Decisions*、なければ *Completed*;
- すべてのツール呼び出し → *Completed*; パス風トークン（`/` または `\` を含む、または `.py`/`.md`/`.js`/`.ts`/`.json` で終わる）→ *Relevant Files*（≤ 10、`http` リンク除外）;
- エラーの `ToolMessage` → *Blocked* と *Critical Context*。

`skip_llm` が有効なときはそのまま使用され、短い/失敗した LLM 要約のセーフティネットにもなります。

## 📦 出力：要約メッセージペア

`_build_new_messages`（:1464）は要約テキストを包み、正確に 2 つのメッセージを出力します:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` —— 役割交代を維持する中立的な質問; AI 半分と同じ `lc_source` マーカーを持ちます。
- **AIMessage**、`additional_kwargs={"lc_source": "summarization"}` 付き —— このマーカーを後続のターンは (a) 前のチェックポイントを発見してチェーンし、(b) チェイニング再要約の入力からペアを除去し、プルーンがチェックポイントで停止するようにし、(c) テストが置き換え後の要約がモデル視点から丸ごと呑み込めることを検証するために使います。
- このペアが MesMemory に入ることはありません: `MessagePersistenceMiddleware._is_persistable` が `lc_source="summarization"` の両半分をスキップします（human 行ビルダーも同じゲートを持ちます）。
- AIMessage は構造化ドキュメント本体も保持します: `additional_kwargs["summary_doc"]`（cap 後の `SummaryDoc`、JSON 直列化可能な素の dict —— チェーンキャリア。`<prior-summary-json>` として再入力されます）。free-form フォールバックの回はこのペイロードを持たず、`_extract_previous_summary` が `<summary>` 本体の解析へフォールバックします。
- 合計コンテンツは `SUMMARY_TOTAL_MAX_CHARS (16 000)` で封印され、先頭/末尾 30/30 保持。

## 🛡️ スラッシング防止マトリクスと劣化リカバリ

状態はセッションスコープの `state_register_mem` の **13 個**の `summarization_*` キー（:92–107）にあります。`_reset_turn_state`（:1849）はターン開始時にそのうち **11 個**をリセットします; `summarization_last_user_question`、`summarization_cooldown_rounds` は意図的にターンごとにはリセットされ**ません**。

| ガード | キー | 閾値 | 効果 |
| :---- | :-- | :-------- | :----- |
| ターンクールダウン | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 実際の compact のたびに武装（:694）; **すべての**モデル呼び出しが減算（:832）; T1 compact ルート、T2 主動、T3 を封鎖 —— T4/T5 強制リングは決して封鎖しない |
| ターンあたりの圧縮数 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | :694 が増加; T2 主動 + T3 を抑圧（強制リングは免除） |
| オーバーフロー再試行（T4/T5 共有） | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | 両エラークラスが共有しターンごとにリセット; 成功した強制ステップのたびに増加; 枯渇 → 元のプロバイダエラーが伝播 |
| セッション総圧縮数 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`（:1255）が True を返す —— 主動圧縮は完全停止 |
| 連続無効回数 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` を設定 —— 非 LLM 戦略のみ |
| 有効性判定 | （`_record_compression`、:1277） | メッセージ数減少**または**トークン削減 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功した非 LLM 戦略（`dedup`/`prune`/`truncate`/`fallback`/`aggressive`）が `skip_llm` を再び解除 |
| 劣化リカバリ予算 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 劣化モニタが発動する強制リカバリの上限 |

**劣化モニタ**（`_monitor_degradation`、:1661）: この呼び出しで実際に圧縮が起きたときのみ参照されます（`_compaction_just_happened` フラグ）。モデルの応答にテキストがなければカウンタが増え、`DEGRADATION_NO_TEXT_THRESHOLD (3)` 回連続の空応答 —— かつ `summarization_recovery_attempts < 2` の間 —— で `force_recovery` を設定し、無効連続記録とセッション圧縮カウントをクリアします。空でない応答はすべてカウンタをリセットします。これは「圧縮 → モデルが混乱 → 空の出力 → 再圧縮」の病的ループを捉えます。相互作用に注意: force フラグは wrap 入口（:1973）で `_should_skip_compression` **より先に**読まれ、スキップゲートはカウンタをリセットして進むことでそのフラグを消費します（:1256–1261）—— リカバリ圧縮は正確に一度だけ実行されます。

## 🔄 システムプロンプト更新

メインエージェントのみ（`need_update_system_prompt=True`）: 圧縮後、ミドルウェアはシステムプロンプトを再構築して `system_prompt` 状態キーに書き込み、次のモデル呼び出しがペルソナファイル / 長期記憶を現時点のまま見るようにします。2 つの配送経路: 圧縮直後の `request.override(system_message=SystemMessage(...))`、および —— T1 の compact が既に起きたがアンチスラッシングゲートが 2 回目を封鎖したとき —— 再構築されたプロンプトはゲート経路でも配送されます（:1993–2005）。`@dynamic_prompt` システムプロンプトミドルウェアを持たないチェーン（サブエージェント / nudge パイプライン）はこのミドルウェアの配送に依存するためです。ゲート経路は、リクエストの現在の system message と**内容が異なる場合にのみ**注入します: 内容が一致していれば override も新しい `SystemMessage` も作らず（再注入しません）。

## 📌 登録箇所

```python
# agent/core.py:152 — メインエージェント（Summarization は最後のミドルウェア:
# 最も内側の wrap レイヤ、LLM に最も近い）
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — ワーカーエージェント（最初のミドルウェア）
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```

## ⚙️ 設定リファレンス

すべての閾値は `config/features/agent_side/summarization.py` (SUMMARIZATION TypedDict) にあります。◆ 印の定数は生きているコード経路が消費します; ○ 印の定数は定義またはインポートはされているものの、生きている経路では**消費されません**（「正直な限界」参照）。

| 定数 | 値 | 消費箇所 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route` のハードオーバーフローバンド; T3 圧力ゲート; 両トリガー節の構築 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route` のソフトオーバーフローバンド |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`（:615）: ウィンドウ − 予備量 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 切り詰めトラック予算 = usable × 0.60（:680） |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results` の候補下限 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 最新メッセージは切り詰め不可（ペアリングのマージン） |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | T4/T5 強制リカバリ上限（単一の共有カウンタ） |
| `OVERFLOW_CLIP_ENABLED` ◆ | `True` | P1-2 LLM なしテールクリップのマスタースイッチ |
| `OVERFLOW_CLIP_MAX_REMOVE` ◆ | `10` | 1 回のクリップでスタブ化する末尾メッセージの上限 |
| `OVERFLOW_CLIP_MIN_KEEP` ◆ | `5` | トランスクリプト下限: これ以下ならクリップしない |
| `MAX_COMPRESS_ATTEMPTS_PER_TURN` ◆ | `3` | ターンあたりの主動圧縮上限 |
| `COMPACTION_COOLDOWN_ROUNDS` ◆ | `3` | 実際の compact のたびに武装されるクールダウン |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 保持予算の下限; ウィンドウがないときの予算 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 保持予算の上限 |
| `PRESERVE_RATIO` ◆ | `0.25` | 保持予算 = ウィンドウの 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | プルーン: 最新ツール出力トークンの保持量 |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | プルーン: 適用の最小利益 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | ターゲット切り詰め: 現在トークンの 50% へ収縮 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | ターゲット切り詰め: 資格基準 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | ターゲット切り詰め: 出力ごとの上限 |
| `MIN_ARGS_CHARS_TO_TRUNCATE` ◆ | `500` | ツール引数切り詰め: 資格基準（JSON 直列化した引数の長さ） |
| `MAX_TOOL_ARGS_CHARS` ◆ | `2_000` | ツール引数切り詰め: 引数ごとの上限 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | アグレッシブ・バックストップのカット長（ツール結果とツール呼び出し引数） |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 要約メッセージ文字上限 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | すべての先頭/末尾保持（要約と TTL 切り詰め） |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 強制リカバリ前の空応答数 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 劣化リカバリ予算 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | ガバナ: セッション試行上限 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | ガバナ: 連続無効 → LLM スキップ |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | ガバナ: トークン削減の有効性 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | すべての縮小戦略から免除 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 最終ターン圧縮ゲート |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO セクション上限 |
| `ACTIVE_PLAN_NOTES_MAX_ITEMS` / `EVICTED_REFS_MAX_ITEMS` ◆ | `20` / `20` | ドキュメント配列上限（計画ノート / 退避ポインタ） |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | ファイル操作ラチェットのリスト上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 復帰コンテキストの要求上限 |
| `CHARS_PER_TOKEN` / `CHARS_PER_TOKEN_CJK`（推定器） | `4` / `2` | 決定論的トークン推定の除数（非 CJK / CJK）; `config/features/agent_side/token_estimation.py` で定義 |
| `PRUNE_TTL_SECONDS` | `300` | TTL 有効期限の地平 —— TTL トリオのみ消費（現在はテスト専用） |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 初回観測レジストリの上限（現在はテスト専用） |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | ミドルウェアがインポート、一度も読まれない |
| `AUTO_CONTINUE_PROMPT` ○ | — | ミドルウェアがインポート、一度も読まれない |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 定義あり、未インポート |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 定義あり、未インポート（実際に使われるのは 900 文字のリスト上限） |

## 🧪 テスト

| スイート | ケース | カバレッジ |
| :---- | :---- | :----- |
| `tests/pub/func/message/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` の各バンド、候補規則、安定したルート文字列 |
| `tests/pub/func/message/test_tool_result_ttl.py` | 28 | その場での切り詰め、ペアリング不変量、空でないプレースホルダ、レジストリ上限、予算切り詰め |
| `tests/pub/func/message/test_llm_error_classifier.py` | 56 | 413 ステータス、テキストヒント、7 つのオーバーフローパターン、cause チェーン深さ、読み取り専用保証 |
| `tests/pub/func/message/test_pub_func_message_tools.py` | 29 | 重複排除 / プルーン / ターゲット切り詰め / ターンユーティリティ、さらにツール引数切り詰め: 先頭+末尾形式、小さい引数のスキップ、解放量のクランプ、保護対象ツール、直近スキップ、ペアリングと無ミューテーション |
| `tests/pub/func/message/test_read_file_slice.py` | 12 | read_file の復元可能スライス: 元パス + 1-based 継続 offset 通知、行スキップなし、ページ番号の絶対性、汎用マーカーのバイト一致、保護 / 予算内 / フォールバック経路 |
| `tests/config/test_num_contract.py` | 46 | 定数契約（ウォッチドッグ `CONTRACT_NAMES` が文書化済みの全ノブをカバー） |
| `tests/pub/func/message/test_overflow_clip.py` | 21 | P1-2 純クリップ: 末尾バッチ検出、max_remove/min_keep/enabled ゲート、トークン目標、マーカー保持（P0-2 ポインタ、P2-4 通知）、no-op 冪等性、ペアリング不変量 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | 9 | P1-2 ミドルウェア統合: T1/T2 の LLM なしクリップ、不十分クリップの劣化、キルスイッチ、T4/T5 のクリップ→再試行とクリップ→圧縮劣化、同期/非同期パリティ、サニタイザ不変 |
| `tests/agent/middlewares/test_compression_comprehensive.py` | 52 | 12 クラス: T2 ソフトオーバーフロー、T2 クールダウン、T2 負/無操作、同期/非同期パリティ、T1 事前点検、ルート判定、T3 トリガー/3 形態/負の二重実行、T4/T5 リカバリ、全アンチスラッシングマトリクス、全分岐パリティ、チェイニング要約フィルタリング |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 6 | チェイニング要約フィルタリング: 旧ペアを直列化会話から除去、通常/空/複数ペア入力、未マークの旧セッション human を保持、async `_acreate_summary` ミラー |
| `tests/agent/middlewares/test_summary_doc.py` + `test_summary_doc_middleware.py` | 41 | 構造化要約: schema 強制変換、レンダリング往復 + バイト安定 + セクション順、コード層 cap + 注記、latest request 逐字、json_mode/json_repair/free-form の 3 ティア、prior-doc JSON チェイニング、旧 MD 遷移、退避ポインタの収集と継承 |
| `tests/agent/middlewares/test_compression_e2e_static.py` | 18 | 6 つのエンドツーエンドシナリオ + 3 つのオーバーフローカウンタ回帰テスト × 2 登録順、静的フォールバック圧縮、ゼロネットワーク |
| `tests/agent/middlewares/test_summarization_trigger.py` | 3 | 登録契約（テスト固定ウィンドウ）: `MAIN_LLM_MAX_TOKEN = 65 536` → トリガー閾値 `52 428`; 低トークン通過 |
| `tests/agent/middlewares/test_summarization_comprehensive.py` | 140 | レガシー深層スイート: カットポイント/予算、FIFO 上限、フォールバック、プルーン/重複排除/ターゲット切り詰め、劣化 |
| `tests/agent/middlewares/test_e2e_summarization.py` | 7 | フルグラフ密閉 e2e: 実 `create_agent` チェーン（主モデルはキャプチャスタブ、補助モデルは失敗スタブ）が静的フォールバック経路を駆動; ゼロネットワーク、ウィンドウ 32 000（縮小）、MAIN_LLM 設定欠落時はスキップ |
| `tests/agent/middlewares/message_persistence/` | 31 | 境界 + ツール返却の増分フラッシュ: 各メッセージちょうど 1 回、境界をまたいで重複なし、永続ウォーターマークによる再起動リプレイで行数不増、同期 + 非同期フック、session_id 欠落スキップ、HITL 拒否ペアの再装着、フィルタ意味論; さらに T1/T2/T3 圧縮経路のゼロ書き込み証明と、要約ペア（両半分）のゼロ書き込み証明 |
| `tests/agent/middlewares/test_compression_nudges.py` | 2 | 圧縮時 nudge ディスパッチ: memory review + plan extraction が compact 経路から発火、カットなし圧縮は何もディスパッチしない |
| `tests/context_engine/store/test_persisted_message_ids.py` | 3 | 永続ウォーターマークストア: 冪等なマーキング、セッション分離、セッション削除時のクリーンアップ、空入力の no-op |
| `tests/context_engine/store/test_interrupt_marker_approach.py` | 11 | マーカー意味論: 要約ペアは後続の圧縮でも生存; FACT C フィクスチャ（ウィンドウ 26 000 → usable 10 000、切り詰め線 7 000） |

プロセス分離フルスイート（`uv run python tests/run_tests_split.py`）は **4268 passed / 12 skipped / 0 failed** で合格（GROUP A 3282P/1S + GROUP B 913P/11S + GROUP C 73P）。

## ⚠️ 正直な限界

- **`keep=("messages", 10)` は受け取られるが使用されません。** コンストラクタは API 互換のために保存するだけ; 末尾保持は予算ベース（`PRESERVE_RATIO` × ウィンドウ、[2 000, 15 000] にクランプ）にルーターの `TRUNCATABLE_RECENT_SKIP` マージンを加えたものです。`keep` を変えても効果はありません。
- **飾りインポート。** `summarization/core.py` 先頭の `json`、`hashlib`、`SUMMARY_TRIM_TOKENS`、`AUTO_CONTINUE_PROMPT` はインポートされるが一度も読まれません。`DEGRADATION_MONITOR_COUNT` と `FILE_OPS_SECTION_MAX_CHARS` は `config/features/agent_side/summarization.py` の `SUMMARIZATION` TypedDict に定義があるが消費者はいません。
- **TTL レジストリは本番に接続されていません。** `record_first_seen` / `select_expired` / `truncate_expired`（および `PRUNE_TTL_SECONDS`、`TTL_REGISTRY_MAX_ENTRIES`）を消費するのはテストだけです; ミドルウェアはもっぱら `truncate_to_budget` を使います。`agent/` 全域の grep でも TTL トリオの本番呼び出し箇所は見つかりません。レジストリは揮発性でもあります（インメモリ、`tool_call_id` キー、再起動で喪失）。
- **残存するが不活性なコード。** `_preemptive_check`（:589）と `_preemptive_truncate`（:1159）は参照専用です: これらが実装する 2 バンドの先取りに到達する本番呼び出し箇所はありません。
- **推定器はトークナイザではなく、3 段階のトークナイザフリー・ヒューリスティックです。** API 報告使用量があれば T1 がそれを返し、T2 が CJK 対応ヒューリスティック（CJK 文字は `CHARS_PER_TOKEN_CJK = 2`、それ以外は `CHARS_PER_TOKEN = 4`）、T3 のレガシー `len // 4` は T2 の純 ASCII 退化ケースです。意図的に決定論的（再現可能なテスト、安定した予算）です; `CHARS_PER_TOKEN_CJK = 2` は中国語が 4 ではなく 1–2 字/トークンに近いことを反映しています。
- **報告値が勝つ場所。** T3 だけが報告使用量駆動のトリガーです（`compute_pressure` は max を取る）。T1/T2 のルート判定は推定駆動です（推定値 + システムプロンプトのオーバーヘッドのみ）; レガシーの `_check_trigger` 節フォールバックは `max(ローカル推定値, 報告値)` を使います。
- **T3 は返される応答を決して変えません。** T3 ディスパッチの永続効果はツール結果のその場での切り詰め（メッセージオブジェクトはグラフ状態と共有）とアンチスラッシングの帳簿記録だけです; T3 の compact ルートの `request.override` はローカルであり、元の応答が常に返ります。T3 本体全体が fail-open です。
- **T4/T5 は設計上アンチスラッシングマトリクスを迂回します** —— それが「強制」の要点です。`MAX_OVERFLOW_RETRIES (3)`（T4/T5 共有の単一カウンタ、ターンごとにリセット）を超えるか、強制圧縮ステップ自体が失敗すると、元のプロバイダ例外が伝播します（決して飲み込まれず、圧縮エラーで置き換えられることもありません）。
- **圧縮は fail-open です。** `_apply_compression` 内のどんな例外もログに記録され、飲み込まれます; ターンは圧縮されていない履歴のまま進行します。
- **静的フォールバックはヒューリスティックです。** キーワードベースの決定/完了分類と生のツール引数からのパス抽出はベストエフォートです; セクション骨格は保証されますが、コンテンツ品質は保証されません。
- **構造化ティアが `json_mode` なのは設定済みエンドポイントがそれを要求するためです。** `glm-5.3-flash`（openai 互換）は `with_structured_output` の関数呼び出しを発行せず、デフォルトの function-calling ティアは Pydantic 解析エラーになります; `_structured_runnable` は `method` 引数を受け付けないプロバイダー向けに引数なし呼び出しへ退避し、残りは `json_repair` と free-form ティアがカバーします。コード層 cap とレンダラーが出力形状を保証します。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` タグ/`lc_source="summarization"` は荷重を支える正確な文字列です。** 後続ターンのチェイニング（`_extract_previous_summary`）、プルーン停止条件、全テストスイートがこれらを文字通り照合します —— 軽々しく言い換えないでください。
