# 🧭 Summarization トリガー — ライフサイクル T1–T5 とオーバーフロー判定

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Summarization](../README.ja.md) の一部：5 つのライフサイクル・トリガーポイント（T1–T5）と 4 ルートのオーバーフロー判定。

---

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

- `truncate_tool_results_only` → `_run_budget_truncation`（:659）—— ステップ 1 が過大なツール呼び出し引数を切り詰め（新しいメッセージを返す、[切り詰めトラック](../internals/README.ja.md#-切り詰めトラック予算切り詰めとttlモジュール) 参照）、ステップ 2 がツール結果をその場で切り詰める —— その後**再確認**: 解放されたトークンが足りなければ（`new_tokens ≥ usable × 0.80`、返されたリスト上で推定）`compact_then_truncate` に昇格; 足りていれば圧縮なしで通過;
- `compact_only` / `compact_then_truncate` → `_execute_compact`（:702 / 非同期 :731）→ `_apply_compression`（例外はログ記録、リクエストは元のまま）→ `_record_compaction_bookkeeping`（:694: クールダウンの武装、ターン試行 1 回の計上）→ `compact_then_truncate` はさらに圧縮結果に予算切り詰めをバックストップとして実行 → 旧/新トークンと圧力比とともにルートをログ記録。

**P1-2 高速パス —— LLM を使わないテールクリップ。** いずれのルートも実行される前に、`_fast_tail_clip` が `clip_overflow_tail`（`pub/func/message/overflow_clip.py`）を実行します: 末尾の連続する `ToolMessage` バッチがコンパクトなスタブに置き換えられ、その置換は `ToolMessage.model_copy` を通るため、メッセージの削除も注入も起こりません —— `id`、`tool_call_id`、`name`、`additional_kwargs` はすべて生存し、ペアリングのサニタイズと永続ウォーターマークは引き続き満たされます。予算: `target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`; `0` のターゲットは「最大の対象バッチを取る」ことを意味します（`overflow_clip_max_remove` が上限、`overflow_clip_min_keep` が下限）。`ratio` はルート経路では `COMPRESSION_TRIGGER_RATIO (0.80)`、T4/T5 の強制ステップでは `1.0`（使用可能予算未満）です。クリップ**単独**で推定値が線の下に落ちる場合のみ採用されます: リクエストはスタブ済みリストのまま返され、ルートは一切実行されません（予算切り詰めなし、補助 LLM 圧縮なし）。不十分なクリップは破棄され、既存ルートが元のリストそのままで実行されます。T4/T5 ではクリップは `request.messages` を読みます; 既にスタブ済みのリクエストでは no-op になるため、同一クリップで再試行予算が焼かれることはなく、次の試行は圧縮に劣化します。

**テール内容の破棄が安全な理由:** すべてのツール結果は返された瞬間に MesMemory へフラッシュ済みで（`MessagePersistenceMiddleware`）、`message_search` ツールを通じて引き続き取得できます。P0-2 で退避された結果はスタブ内に `[evicted to: …]` ポインタを保持し（`read_file` が引き続き機能します）、P2-4 でスライスされた `read_file` 結果はスライス通知をそのまま保持します; そしてスタブ済みメッセージはスキャン対象バッチを終了させるため、2 回目のクリップは no-op であり、これらのマーカーを決して壊しません。設定: `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`。

ウィンドウ算術（テスト契約）: ウィンドウ `41 600` → usable `25 600`、2 つの閾値線 `17 920` / `20 480`、切り詰め予算 `15 360`。テスト固定値 `MAIN_LLM_MAX_TOKEN = 65536` のとき（実行時の `.env` 値は 131072 / 128K 以上が必要）、登録済み T2 節は `52 428` に置かれます。
