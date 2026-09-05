# 🗜️ コンテキスト圧縮：Summarization ミドルウェア

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが長い会話をモデルのコンテキストウィンドウの中に収め続ける仕組み: 5つのトリガーポイントがライフサイクル全体（ターン開始前、すべてのモデル呼び出し前、すべてのモデル応答後、プロバイダのオーバーフローエラー時）を監視し、純粋関数型の4ルートルーターが最も安価な修復手段を選び（まず大きいツール結果を切り詰め、強制されたときだけ AI 圧縮）、アンチスラッシングガードが圧縮の暴走を構造的に防ぎます。

一次情報: `agent/middlewares/summarization.py`、`pub_func/message/overflow_router.py`、`pub_func/message/tool_result_ttl.py`、`pub_func/message/llm_error_classifier.py`、`pub_func/message/estimate_msg_tokens.py`、`pub_func/message/tool_output_dedup.py`、`pub_func/message/tool_output_prune.py`、`pub_func/message/target_truncation.py`、`pub_func/message/turn_utils.py`、`config/num.py`、および 2 つの登録箇所 `agent/core.py` と `agent/tools/subagent/spawn/core.py`。本文書の行番号と定数はすべてこのコードと突き合わせて検証済みです。

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

`Summarization`（`agent/middlewares/summarization.py`、クラスは 490 行）は**ゼロから実装された** `AgentMiddleware` であり、LangChain 組み込みの `SummarizationMiddleware` を継承し**ません**。エージェントライフサイクルの正確に 2 箇所だけにフックします:

- `before_agent` / `abefore_agent`（1894 / 1898 行）—— **T1 事前点検**
- `wrap_model_call` / `awrap_model_call`（1908 / 1994 行）—— **T2 ディスパッチ、T3 応答後の再確認、T4/T5 エラー復帰リング**

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
├─ T1  before_agent 事前点検  (_t1_preflight :1834 / _at1_preflight :1865)
│      ├─ _reset_turn_state (:1797) が 10 個のターン単位カウンタをリセット
│      ├─ _decide_overflow_route (:622) → None / "fits" → 通過
│      ├─ クールダウン > 0 なら COMPACT ルートを封鎖; 切り詰めトラックは
│      │  それでも実行（それ自体が最安の復帰メカニズム）
│      └─ ディスパッチ(trigger="T1") + _t1_state_update (:1810) が結果を
│         グラフにコミット:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         （add_messages リデューサは自前ではメッセージを消さない ——
│         RemoveMessage センチネルが、圧縮された接頭部が状態から実際に
│         出て行く唯一の経路）
│
├─ T2  wrap_model_call、ハンドラ以前（:1908 同期 / :1994 非同期）
│      ├─ force フラグ（:1921）をスキップゲートより先に読む —— スキップ
│      │  ゲート（_should_skip_compression :1234）がフラグを消費するため
│      ├─ _tick_cooldown（:811）: すべての呼び出しがクールダウンを減算
│      ├─ アンチスラッシングゲート（:1931–1934）:
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      通過（直前に compact が起きていればシステムプロンプトを
│      │      再構築、:1938–1952）→ ハンドラ → モニタ → T3
│      ├─ さもなくば: 4ルート判定（:1967）→ _dispatch_overflow_route;
│      │  レガシートリガー節が発火すれば（_check_trigger :566、例:
│      │  ("messages", 40)）→ ROUTE_COMPACT_ONLY（:1972）
│      └─ 3 箇所のハンドラ呼び出し（:1927、:1953、:1978）はすべて
│         _execute_with_recovery（:1036）の中で実行 —— すなわち T4/T5 リング
│
├─ T3  応答後の再確認  (_post_response_check :828 / 非同期 :901)
│      ├─ T2 が今回の wrap 呼び出しで既に圧縮していればスキップ
│      │  （t2_compressed フラグ、:1980–1983）—— モデル呼び出しにつき
│      │  圧縮は正確に一度
│      ├─ extract_reported_input_tokens(response)（:127）; None → 返却
│      ├─ ゲート: ターン試行上限、クールダウン、使用可能予算
│      ├─ pressure = max(推定値 + システムプロンプト, 報告値) ——
│      │  プロバイダが報告した入力トークンが優先（compute_pressure）
│      ├─ pressure < usable × 0.80 → 返却; ルートが "fits" → 返却
│      └─ ディスパッチ(trigger="T3")し、常に元の応答を返す; 関数全体が
│         fail-open（例外 → ログ、元の応答はそのまま保持）
│
└─ T4/T5  プロバイダエラー復帰リング
       (_execute_with_recovery :1036 / _aexecute_with_recovery :1094)
       ├─ ハンドラが例外を投げる → classify_provider_error
       │  （pub_func/message/llm_error_classifier.py）:
       │  payload_too_large → T4、context_overflow → T5
       │  （_TRIGGER_BY_ERROR_CLASS :112、_RETRY_KEY_BY_ERROR_CLASS :116）
       ├─ 対象外 / 未分類 → 元の例外をそのまま再スロー（再試行 0 回、
       │  状態書き込み 0 件、決して飲み込まない）
       ├─ 再試行 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  （:964 / 非同期 :1009）: 強制圧縮 + 予算切り詰め。構造上すべての
       │  アンチスラッシングゲートを迂回（クールダウン、ターンごとの上限、
       │  _should_skip_compression はいずれも参照されない）; クールダウンを
       │  武装せずターン試行も数えないが、セッション統計の真実性のため
       │  _record_compression は通る; クラス別再試行カウンタは成功後にのみ
       │  増える（:1000）
       ├─ 再試行の枯渇 → 元の例外を再スロー（エラーフレームは messages.py
       │  → turn_runner.py へ伝播 —— 空の応答で置き換えられることはない）
       └─ 強制圧縮ステップ自体の失敗 → 元の例外を再スロー
          （raise exc from compression_exc）。_monitor_degradation は
          リングが返った後の最終成功応答に対して一度だけ実行される。
```

レガシートリガー節は T2 のフォールバックとして今も存在します（`_check_trigger`、:566）: `("messages", N)` は履歴長で、`("tokens", N)` は `max(ローカル推定値, 最後の AIMessage が報告した usage_metadata.total_tokens)` ≥ N で発火します。節リストは OR です。

## 🚦 4ルート・オーバーフロー判定

`pub_func/message/overflow_router.py` は**純粋な判定レイヤ**です —— 切り詰めも、圧縮も、I/O も、状態もありません。ミドルウェアはここから 3 つの関数をインポートします:

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
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :605
system_est     = len(state_register_mem["system_prompt"]) // 4                  # :616–620
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

唯一の実行者 `_dispatch_overflow_route`（:739 同期 / :779 非同期）が T1、T2、T3 の**すべて**に仕えます —— 2 番目の複製は決して作りません:

- `truncate_tool_results_only` → `_run_budget_truncation`（:649）がその場で切り詰め、その後**再確認**: 解放されたトークンが足りなければ（`new_tokens ≥ usable × 0.80`）`compact_then_truncate` に昇格; 足りていれば圧縮なしで通過;
- `compact_only` / `compact_then_truncate` → `_execute_compact`（:681 / 非同期 :710）→ `_apply_compression`（例外はログ記録、リクエストは元のまま）→ `_record_compaction_bookkeeping`（:673: クールダウンの武装、ターン試行 1 回の計上）→ `compact_then_truncate` はさらに圧縮結果に予算切り詰めをバックストップとして実行 → 旧/新トークンと圧力比とともにルートをログ記録。

ウィンドウ算術（テスト契約）: ウィンドウ `41 600` → usable `25 600`、2 つの閾値線 `17 920` / `20 480`、切り詰め予算 `15 360`。`MAIN_LLM_MAX_TOKEN = 65536` のとき、登録済み T2 節は `52 428` に置かれます。

## 🪙 トークン推定（トークナイザなし）

`pub_func/message/estimate_msg_tokens.py`（29 行）は意図的にトークナイザを使わず、決定論的に動作します:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

速く、実行間で安定し（同じ入力 → 同じ数値 → 再現可能なテスト）、意図的に保守的な近似です。トリガー/予算経路のどの部分もモデルのトークナイザに依存しません。

## ✂️ 切り詰めトラック：予算切り詰めとTTLモジュール

`pub_func/message/tool_result_ttl.py` は、切り詰めトラックが使うその場での切り詰めを提供します。設計不変量（構造を支えるもの）:

- **その場でのみ** —— このモジュールはメッセージを削除・並べ替え・pop しません; `msg.content`（または content リストブロック）を変更してインデックスを返すだけです。これがプロバイダ API と `ToolCallNormalize` が依存する tool-call/`ToolMessage` ペアリングを守ります。
- **プレースホルダは空にしない** —— 切り詰められた結果は常に空でない内容を保持します: `ToolCallNormalize.before_model` は**空の `ToolMessage` をドロップ**してトランスクリプトを浄化するため、空のプレースホルダは静かにペアリングを壊します。
- **先頭 30% / 末尾 30% 保持**（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）に省略マーカーを付けます。

ミドルウェアが実際に消費するもの: **`truncate_to_budget` のみ**。ルーターの候補リストに駆動され、`_run_budget_truncation`（:649）が予算（`usable × TRUNCATE_BUDGET_RATIO`）を満たすまで候補を切り詰めます。

TTL レジストリ本体（`record_first_seen` / `select_expired` / `truncate_expired`、`PRUNE_TTL_SECONDS = 300`、`TTL_REGISTRY_MAX_ENTRIES = 512`、`tool_call_id` キー、再起動で揮発）は、現在**テストスイートだけが使用**しています —— ミドルウェアには年齢ベースの有効期限ロジックは接続されていません（「正直な限界」参照）。

## 🔁 圧縮トラック：`_apply_compression` の内部

`_apply_compression`（:1636; 非同期ツイン :1708）は次の順で実行されます:

1. **復帰コンテキストの捕捉**（`_capture_recovery_context`、:1525）: 最後のユーザー要求（≤ 800 文字）とファイル操作ラチェット —— `read`/`write` 系ツール呼び出しからパスを抽出し（:405）、前回分の集合とマージ（読んだものは記憶され、変更されたファイルが読み取り専用に格下げされることはない）。
2. **非 LLM 戦略**（`_run_non_llm_strategies`、:1472）: `重複排除 → プルーン → ターゲット切り詰め`（詳細は下記）。これらはタダです —— モデル呼び出しなし。
3. **LLM を使うかの判定**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM での縮小が最初の機会を得ます; 履歴がまだ保持予算の 2 倍を超えるとき（またはガバナが LLM 要約を無効化しているとき、または非 LLM 戦略が何も減らせなかったとき）にのみ、補助 LLM に金を払います。
4. **アグレッシブ・バックストップ**（`_aggressive_truncate`、:1508）: 結果が*それでも*大きすぎる場合、`AGGRESSIVE_TRUNCATE_CHARS (1 000)` 文字を超えるすべての `ToolMessage` がマーカー付きでハードカットされます。
5. **要約の自己切り詰め**（`_truncate_summary_messages`、:1578）: `SUMMARY_TOTAL_MAX_CHARS (16 000)` 文字を超える既存の要約メッセージ（`lc_source == "summarization"`）は、先頭 30% / 末尾 30% に再切り詰めされます（`_truncate_content`、:1570）。
6. **復帰コンテキストの注入**（`_inject_recovery_context`、:1543）: 捕捉したファイル操作ラチェットが要約の `## Relevant Files` セクションに書き込まれ、チェックポイントが常に最新の読み取り/変更ファイルマップを運ぶようにします。
7. **帳簿記録**（`_record_compression`、:1256）し、最後に `request.override(messages=..., system_message=...)`。

**カットポイント選択**（`_determine_cutoff`、:1289）: 履歴をターンに分割し、**最新から逆方向**に歩きながら保持予算 `clamp(window × 0.25, 2 000, 15 000)`（`_calculate_preserve_budget`、:555）に照らして累積します; 丸ごと入らないターンはターン途中で割られることがあります。`_adjust_for_orphan_pairs`（:1319）がカットポイントを逆に歩き、`ToolMessage` が `AIMessage` のツール呼び出しから分離する状態がなくなるまで調整します。最終ターン比率ゲートが発火しない限り（最後のユーザーターン ≥ 全トークンの `LAST_TURN_RATIO_THRESHOLD (0.5)` —— `_check_last_turn_ratio`、wrap 入口 :1916/:2002 で呼び出し）、カットポイントが最後の `HumanMessage` を超えることはありません。

すべての失敗モードは fail-open です: `_apply_compression` が例外を投げてもログに記録されるだけで、元のリクエストがそのまま進行します —— 壊れた圧縮がターンを壊すことはありません。

## 📝 LLM要約：プロンプト、チェイニング、フォールバック

`_create_summary` / `_acreate_summary`（:1389 / :1414）:

1. **直列化**（`_serialize_for_summary`、:255）: 各メッセージがタグ付きの 1 行になります —— `[User]:`（≤ 2 000 文字）、`[Assistant]:`（≤ 2 000 文字）、`[Assistant tool call]: name(args ≤ 500 chars)`、`[Tool result|Tool error] (id):`（> 2 000 文字 → 1 800 文字保持 + 省略マーカー）。
2. **前のチェックポイントのチェイニング**（`_extract_previous_summary`、:1355）: `additional_kwargs["lc_source"] == "summarization"` を持つ最新の `AIMessage` を見つけ、`<summary>…</summary>` 本体を抽出します。存在すれば、プロンプトは `_SUMMARY_PROMPT_FIRST`（:234）ではなく `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE`（:242）になります —— 目標/制約/決定を前へ運び、競合時は最新を優先し、FIFO 上限を守ります。
3. **呼び出し**は補助モデルに対し `config={"metadata": {"lc_source": "summarization"}}` 付きで行われ、下流のツールチェーンが要約呼び出しを識別できるようにします。
4. **ガードレール:** 空または極端に短い応答は決定論的要約へフォールバックし、例外も同様です。失敗時に LLM が最後の言葉を持つことはありません。

プロンプトテンプレート（`_SUMMARY_TEMPLATE`、:187）は Markdown 骨格を固定します —— *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress（Completed ≤ 5 · In Progress · Blocked）/ Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* —— 「空でもすべてのセクションを保持する」ことと秘密保持ルール（"NEVER include API keys, tokens, passwords, secrets"）を要求します。`_enforce_fifo_limits`（:371）が返されたテキストに項目上限を決定論的に再適用し、`"(N earlier items omitted for brevity)"` を追記します。

## 🧱 静的フォールバック（LLMを使わない要約）

`_build_static_fallback_summary`（:286）はモデル呼び出しゼロで同じセクション骨格を生成します:

- 最後のユーザー要求 → *Latest Unresolved User Request*; 最初の要求 → *Goal*;
- 決定キーワード（`decided`、`choosing`、`because`、`therefore`）を含む AI テキスト → *Key Decisions*、なければ *Completed*;
- すべてのツール呼び出し → *Completed*; パス風トークン（`/` または `\` を含む、または `.py`/`.md`/`.js`/`.ts`/`.json` で終わる）→ *Relevant Files*（≤ 10、`http` リンク除外）;
- エラーの `ToolMessage` → *Blocked* と *Critical Context*。

`skip_llm` が有効なときはそのまま使用され、短い/失敗した LLM 要約のセーフティネットにもなります。

## 📦 出力：要約メッセージペア

`_build_new_messages`（:1443）は要約テキストを包み、正確に 2 つのメッセージを出力します:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` —— 役割交代を維持する中立的な質問。
- **AIMessage**、`additional_kwargs={"lc_source": "summarization"}` 付き —— このマーカーを後続のターンは (a) 前のチェックポイントを発見してチェーンし、(b) プルーンがチェックポイントで停止するようにし、(c) テストが置き換え後の要約がモデル視点から丸ごと呑み込めることを検証するために使います。
- 合計コンテンツは `SUMMARY_TOTAL_MAX_CHARS (16 000)` で封印され、先頭/末尾 30/30 保持。

## 🛡️ スラッシング防止マトリクスと劣化リカバリ

状態はセッションスコープの `state_register_mem` の **14 個**の `summarization_*` キー（:89–104）にあります。`_reset_turn_state`（:1797）はターン開始時にそのうち **10 個**をリセットします; `summarization_last_user_question`、`summarization_cooldown_rounds`、2 つの T4/T5 再試行カウンタは意図的にターンごとにはリセットされ**ません**。

| ガード | キー | 閾値 | 効果 |
| :---- | :-- | :-------- | :----- |
| ターンクールダウン | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 実際の compact のたびに武装（:673）; **すべての**モデル呼び出しが減算（:811）; T1 compact ルート、T2 主動、T3 を封鎖 —— T4/T5 強制リングは決して封鎖しない |
| ターンあたりの圧縮数 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | :673 が増加; T2 主動 + T3 を抑圧（強制リングは免除） |
| クラス別オーバーフロー再試行 | `summarization_overflow_retries_t4` / `_t5` | `MAX_OVERFLOW_RETRIES = 3` | 成功した強制ステップのたびに増加; 枯渇 → 元のプロバイダエラーが伝播 |
| セッション総圧縮数 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`（:1234）が True を返す —— 主動圧縮は完全停止 |
| 連続無効回数 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` を設定 —— 非 LLM 戦略のみ |
| 有効性判定 | （`_record_compression`、:1256） | メッセージ数減少**または**トークン削減 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功した非 LLM 戦略（`dedup`/`prune`/`truncate`/`fallback`/`aggressive`）が `skip_llm` を再び解除 |
| 劣化リカバリ予算 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 劣化モニタが発動する強制リカバリの上限 |

**劣化モニタ**（`_monitor_degradation`、:1609）: この呼び出しで実際に圧縮が起きたときのみ参照されます（`_compaction_just_happened` フラグ）。モデルの応答にテキストがなければカウンタが増え、`DEGRADATION_NO_TEXT_THRESHOLD (3)` 回連続の空応答 —— かつ `summarization_recovery_attempts < 2` の間 —— で `force_recovery` を設定し、無効連続記録とセッション圧縮カウントをクリアします。空でない応答はすべてカウンタをリセットします。これは「圧縮 → モデルが混乱 → 空の出力 → 再圧縮」の病的ループを捉えます。相互作用に注意: force フラグは wrap 入口（:1921）で `_should_skip_compression` **より先に**読まれ、スキップゲートはカウンタをリセットして進むことでそのフラグを消費します（:1235–1240）—— リカバリ圧縮は正確に一度だけ実行されます。

## 🔄 システムプロンプト更新

メインエージェントのみ（`need_update_system_prompt=True`）: 圧縮後、ミドルウェアはシステムプロンプトを再構築して `system_prompt` 状態キーに書き込み、次のモデル呼び出しがペルソナファイル / 長期記憶を現時点のまま見るようにします。2 つの配送経路: 圧縮直後の `request.override(system_message=SystemMessage(...))`、および —— T1 の compact が既に起きたがアンチスラッシングゲートが 2 回目を封鎖したとき —— 再構築されたプロンプトはゲート経路でも注入されます（:1938–1952）。`ContextEngineHook` を持たないチェーンはこのミドルウェアの配送に依存するためです。

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

すべての閾値は `config/num.py` にあります。◆ 印の定数は生きているコード経路が消費します; ○ 印の定数は定義またはインポートはされているものの、生きている経路では**消費されません**（「正直な限界」参照）。

| 定数 | 値 | 消費箇所 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route` のハードオーバーフローバンド; T3 圧力ゲート; 両トリガー節の構築 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route` のソフトオーバーフローバンド（旧 `_preemptive_check` の 2 バンドゲートは引退） |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`（:605）: ウィンドウ − 予備量 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 切り詰めトラック予算 = usable × 0.60（:660） |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results` の候補下限 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 最新メッセージは切り詰め不可（ペアリングのマージン） |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | エラークラスごとの T4/T5 強制リカバリ上限 |
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
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | アグレッシブ・バックストップのカット長 |
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
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | ファイル操作ラチェットのリスト上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 復帰コンテキストの要求上限 |
| `CHARS_PER_TOKEN`（推定器） | `4` | 決定論的トークン推定の除数 |
| `PRUNE_TTL_SECONDS` | `300` | TTL 有効期限の地平 —— TTL トリオのみ消費（現在はテスト専用） |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 初回観測レジストリの上限（現在はテスト専用） |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | ミドルウェアがインポート、一度も読まれない |
| `AUTO_CONTINUE_PROMPT` ○ | — | ミドルウェアがインポート、一度も読まれない |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 定義あり、未インポート |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 定義あり、未インポート（実際に使われるのは 900 文字のリスト上限） |

## 🧪 テスト

| スイート | ケース | カバレッジ |
| :---- | :---- | :----- |
| `tests/unit/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` の各バンド、候補規則、安定したルート文字列 |
| `tests/unit/test_tool_result_ttl.py` | 28 | その場での切り詰め、ペアリング不変量、空でないプレースホルダ、レジストリ上限、予算切り詰め |
| `tests/unit/test_llm_error_classifier.py` | 20 | 413 ステータス、テキストヒント、7 つのオーバーフローパターン、cause チェーン深さ、読み取り専用保証 |
| `tests/unit/test_config_num.py` | 43 | 定数契約（ウォッチドッグ `CONTRACT_NAMES` が文書化済みの全ノブをカバー） |
| `tests/module/test_compression_comprehensive.py` | 48 | 12 クラス: T2 ソフトオーバーフロー、T2 クールダウン、T2 負/無操作、同期/非同期パリティ、T1 事前点検、ルート判定、T3 トリガー/3 形態/負の二重実行、T4/T5 リカバリ、全アンチスラッシングマトリクス、全分岐パリティ |
| `tests/module/test_compression_e2e_static.py` | 12 | 6 つのエンドツーエンドシナリオ × 2 登録順、静的フォールバック圧縮、ゼロネットワーク |
| `tests/module/test_summarization_trigger.py` | 3 | 本番登録契約: `MAIN_LLM_MAX_TOKEN = 65 536` → トリガー閾値 `52 428`; 低トークン通過 |
| `tests/module/test_summarization_comprehensive.py` | 140 | レガシー深層スイート: カットポイント/予算、FIFO 上限、フォールバック、プルーン/重複排除/ターゲット切り詰め、劣化 |
| `tests/module/test_e2e_summarization.py` | 7 | フルグラフ密閉 e2e: 実 `create_agent` チェーン（主モデルはキャプチャスタブ、補助モデルは失敗スタブ）が静的フォールバック経路を駆動; ゼロネットワーク、ウィンドウ 32 000（縮小）、MAIN_LLM 設定欠落時はスキップ |
| `tests/integration/test_interrupt_marker_approach.py` | 11 | マーカー意味論: 要約ペアは後続の圧縮でも生存; FACT C フィクスチャ（ウィンドウ 26 000 → usable 10 000、切り詰め線 7 000） |

プロセス分離フルスイート（`uv run python tests/run_tests_split.py`）は **2219 passed / 0 failed** で合格（GROUP A 1469P/2S + GROUP B 750P/5D）。

## ⚠️ 正直な限界

- **`keep=("messages", 10)` は受け取られるが使用されません。** コンストラクタは API 互換のために保存するだけ; 末尾保持は予算ベース（`PRESERVE_RATIO` × ウィンドウ、[2 000, 15 000] にクランプ）にルーターの `TRUNCATABLE_RECENT_SKIP` マージンを加えたものです。`keep` を変えても効果はありません。
- **飾りインポート。** `summarization.py` 先頭の `json`、`hashlib`、`SUMMARY_TRIM_TOKENS`、`AUTO_CONTINUE_PROMPT` はインポートされるが一度も読まれません。`DEGRADATION_MONITOR_COUNT` と `FILE_OPS_SECTION_MAX_CHARS` は `config/num.py` に定義があるが消費者はいません。
- **TTL レジストリは本番に接続されていません。** `record_first_seen` / `select_expired` / `truncate_expired`（および `PRUNE_TTL_SECONDS`、`TTL_REGISTRY_MAX_ENTRIES`）を消費するのはテストだけです; ミドルウェアはもっぱら `truncate_to_budget` を使います。`agent/` 全域の grep でも TTL トリオの本番呼び出し箇所は見つかりません。レジストリは揮発性でもあります（インメモリ、`tool_call_id` キー、再起動で喪失）。
- **残存するが不活性なコード。** `_preemptive_check`（:579）と `_preemptive_truncate`（:1138）にはもう呼び出し箇所がありません —— これらが実装していた 2 バンドの先取りは 4 ルート判定に置き換えられました。参考のため保持されています。
- **推定器はトークナイザではなく `chars // 4` です。** 意図的に決定論的（再現可能なテスト、安定した予算）で、英語/コード混在コンテンツで較正されています; CJK 多めのコンテンツは過少計数されます（中国語は 4 ではなく 1–2 字/トークンに近い）。
- **報告値が勝つ場所。** T3 だけが報告使用量駆動のトリガーです（`compute_pressure` は max を取る）。T1/T2 のルート判定は推定駆動です（推定値 + システムプロンプトのオーバーヘッドのみ）; レガシーの `_check_trigger` 節フォールバックは `max(ローカル推定値, 報告値)` を使います。
- **T3 は返される応答を決して変えません。** T3 ディスパッチの永続効果はツール結果のその場での切り詰め（メッセージオブジェクトはグラフ状態と共有）とアンチスラッシングの帳簿記録だけです; T3 の compact ルートの `request.override` はローカルであり、元の応答が常に返ります。T3 本体全体が fail-open です。
- **T4/T5 は設計上アンチスラッシングマトリクスを迂回します** —— それが「強制」の要点です。クラスあたり `MAX_OVERFLOW_RETRIES (3)` を超えるか、強制圧縮ステップ自体が失敗すると、元のプロバイダ例外が伝播します（決して飲み込まれず、圧縮エラーで置き換えられることもありません）。
- **圧縮は fail-open です。** `_apply_compression` 内のどんな例外もログに記録され、飲み込まれます; ターンは圧縮されていない履歴のまま進行します。
- **静的フォールバックはヒューリスティックです。** キーワードベースの決定/完了分類と生のツール引数からのパス抽出はベストエフォートです; セクション骨格は保証されますが、コンテンツ品質は保証されません。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` タグ/`lc_source="summarization"` は荷重を支える正確な文字列です。** 後続ターンのチェイニング（`_extract_previous_summary`）、プルーン停止条件、全テストスイートがこれらを文字通り照合します —— 軽々しく言い換えないでください。
