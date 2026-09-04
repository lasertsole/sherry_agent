# 🗜️ コンテキスト圧縮: Summarization ミドルウェア

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが長い会話をモデルのコンテキストウィンドウの中に収め続ける仕組み: 決定論的なトークン推定器が警報を鳴らし、非 LLM 戦略がタダでツール出力のノイズを削り、それでも足りないときにだけ補助 LLM が古いターンを構造化されたチェックポイントへ書き直します。アンチスラッシングガードにより、圧縮が暴走することはありません。

一次情報: `agent/middlewares/summarization.py`、`pub_func/message/estimate_msg_tokens.py`、`pub_func/message/tool_output_dedup.py`、`pub_func/message/tool_output_prune.py`、`pub_func/message/target_truncation.py`、`pub_func/message/turn_utils.py`、`config/num.py`、および 2 つの登録箇所 `agent/core.py` と `agent/tools/subagent/spawn/core.py`。本文書の行番号と定数はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [実行場所: モデル呼び出しごとのフロー](#-実行場所-モデル呼び出しごとのフロー)
- [トリガー: 3つのゲート](#-トリガー-3つのゲート)
- [トークン推定 (トークナイザーなし)](#-トークン推定-トークナイザーなし)
- [保持予算とカットオフ](#-保持予算とカットオフ)
- [`_apply_compression` 内の圧縮パイプライン](#-_apply_compression-内の圧縮パイプライン)
- [LLM 要約: プロンプト、チェーン、フォールバック](#-llm-要約-プロンプトチェーンフォールバック)
- [静的フォールバック (LLM を使わない要約)](#-静的フォールバック-llm-を使わない要約)
- [非 LLM 戦略](#-非-llm-戦略)
- [出力: 要約メッセージペア](#-出力-要約メッセージペア)
- [アンチスラッシングと劣化リカバリー](#-アンチスラッシングと劣化リカバリー)
- [システムプロンプトの再構築](#-システムプロンプトの再構築)
- [登録箇所](#-登録箇所)
- [設定リファレンス](#-設定リファレンス)
- [テスト](#-テスト)
- [⚠️ 正直な制限事項](#%EF%B8%8F-正直な制限事項)

## 🎯 概要

`Summarization`（`agent/middlewares/summarization.py`、クラスは 402 行目）は**ゼロから実装された** `AgentMiddleware` であり、LangChain の組み込み `SummarizationMiddleware` を継承し**ません**。圧縮ロジックはすべて自己完結しています: トリガー判定、カットオフ決定、要約生成、多戦略シュリンクパイプライン、劣化モニタリング。

役割: 会話が閾値を超えて育つと、メッセージ履歴の古い接頭部をコンパクトなチェックポイントで置き換えつつ、直近のコンテキストは逐語的に保ちます。圧縮後、履歴は常に次の形になります:

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

置き換え後が Human/AI ペアであるため、モデルが同じロールのメッセージを連続で見ることはなく、ペア修復は不要です。

登録箇所は 2 か所あります:

| 登録箇所 | トリガー | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| メインエージェント（`agent/core.py:152`） | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| ワーカー/サブエージェント（`agent/tools/subagent/spawn/core.py:755`） | `("messages", 40)` **または** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`（デフォルト） |

両方とも `main_llm_context_window=main_llm_max_tokens`（`MAIN_LLM_MAX_TOKEN` 由来）と `keep=("messages", 10)` を渡します。

## 🧭 実行場所: モデル呼び出しごとのフロー

このミドルウェアは `before_agent`/`abefore_agent`（カウンターリセット）と `wrap_model_call`/`awrap_model_call`（1188–1262 行目）にフックします。ミドルウェアチェーンの中で**最も内側、つまり LLM に最も近い**位置にいるため、メッセージの書き換えはモデル呼び出し直前の最後のステップです。

```
wrap_model_call(request, handler)
│
├─ 1. _check_last_turn_ratio      last turn ≥ 50% of tokens? → flag it
├─ 2. _should_skip_compression    max attempts reached / LLM marked ineffective?
│        └─ yes → call handler directly, monitor response, return
├─ 3. _preemptive_check           pressure = est_tokens / context_window
│        ├─ ≥ 0.80            → "compact"
│        ├─ ≥ 0.70            → "truncate_only"
│        └─ else              → None
├─ 4. if truncate_only|compact:
│        _preemptive_truncate    shrink oversized ToolMessages (> 2000 chars),
│                                no LLM call, override request messages
├─ 5. need_compress = (action == "compact") OR configured trigger fires
├─ 6. if need_compress: _apply_compression(...)   exceptions logged, never fatal
├─ 7. response = handler(request)
└─ 8. _monitor_degradation(response)   count empty responses after compaction
```

パイプライン全体は非同期パス向けに `_aapply_compression`（1087–1153 行目）としてミラー実装されており、両者は意味的に同一です。

## 🚦 トリガー: 3つのゲート

**ゲート 1 — 設定されたトリガー**（`_check_trigger`、478 行目）。各節は `("messages", N)`（履歴長 ≥ N）か `("tokens", N)`（実効トークン数 ≥ N）です。節のリストは OR です。

**ゲート 2 — 先制的な圧力チェック**（`_preemptive_check`、491 行目）。`main_llm_context_window` の設定を要求し、`pressure = effective_tokens / context_window` を計算して返します:

- `pressure ≥ COMPRESSION_TRIGGER_RATIO (0.80)` なら `"compact"` — この呼び出しで完全圧縮;
- `pressure ≥ PREEMPTIVE_TRUNCATE_RATIO (0.70)` なら `"truncate_only"` — LLM 不要のツール出力シュリンクのみ。

**ゲート 3 — 最終ターン比率**（`_check_last_turn_ratio`、581 行目）。最後のユーザーターンが単独で全トークンの ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)` を占める場合、`_compress_last_turn` がセットされます: カットオフロジックは最終ターンを保護する代わりに、要約が最終ターン**へ**食い込むことを許可されます。最後のユーザー質問はリカバリーコンテキスト用にセッション状態へ退避されます。

「実効トークン」 = `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)`。API が報告した数値があれば、それが ground truth であるため優先されます（455–461、478–511 行目）。

## 🪙 トークン推定 (トークナイザーなし)

`pub_func/message/estimate_msg_tokens.py` は意図的にトークナイザーを使わず、決定論的です:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

高速で、実行間で安定し（同じ入力 → 同じ数字 → 再現可能なテスト）、意図的に保守的な近似です。トリガー/予算経路のどこにもモデルのトークナイザーへの依存はありません。

## 💰 保持予算とカットオフ

**予算**（`_calculate_preserve_budget`、467 行目）:

```
budget = clamp(context_window × PRESERVE_RATIO(0.25), MIN_PRESERVE_TOKENS(2000), MAX_PRESERVE_TOKENS(15000))
without a context window → MIN_PRESERVE_TOKENS (2000)
```

**カットオフ**（`_determine_cutoff`、668 行目）は、履歴のどの末尾が逐語的に生き残るかを選びます:

1. 履歴をターンに分割し（`split_into_turns`）、**最新から遡って**予算が満たされるまでターンサイズを積み上げます。
2. ターン全体が収まらない場合はターン途中で分割し（`split_turn`）、残り予算を無駄なく使い切れます。
3. `_adjust_for_orphan_pairs`（698 行目）が続いてカットオフを**後方へ**巻き戻し、どの `ToolMessage` も自分の `AIMessage` ツール呼び出しと分離しないようにします: 呼び出しが要約で消えたツール結果は API エラーになります。
4. `_compress_last_turn` がセットされていない限り、カットオフは最後の `HumanMessage` を越えないよう固定されます: 現在の質問は常に逐語的に保たれます。

⚠️ **noop の罠:** 履歴全体が予算内に収まる場合、遡りはカットオフを一切動かせず `0` のままになり、このラウンドでは要約が何も起こりません（`cutoff == 0 → "noop"`、1045–1047/1117–1119 行目）。デフォルトの下限 `MIN_PRESERVE_TOKENS = 2000` により、約 2000 推定トークン未満の履歴は決して LLM 要約されません。統合テストは要約経路を決定論的に通すため、小さな `main_llm_context_window`（例: 8 000）を注入します。

## 🔁 `_apply_compression` 内の圧縮パイプライン

`_apply_compression`（1015 行目; 非同期ツインは 1087 行目）は、次の順序で実行します:

1. **リカバリーコンテキストの捕捉**（`_capture_recovery_context`、904 行目）: 最後のユーザー要求（≤ 800 文字）とファイル操作ラチェット、すなわち `read`/`write` 系ツール呼び出しから抽出されたパスを、前ラウンドの集合とマージします（読み取りは記憶され、修正されたファイルが読み取り専用に格下げされることはありません）。
2. **非 LLM 戦略**（`_run_non_llm_strategies`、851 行目）: `dedup → prune → target truncate`（詳細は後述）。モデル呼び出しのないタダのステップです。
3. **LLM を呼ぶかの判定**（1030 行目）:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM シュリンクに最初のチャンスが与えられます; 補助 LLM が使われるのは、履歴がまだ保持予算の 2 倍を超えているとき（またはアンチスラッシングガバナーが LLM 要約を無効化したとき、非 LLM 戦略が何も減らせなかったとき）だけです。
4. **アグレッシブなバックストップ**（1052 行目）: 結果がそれでも `budget × 2` を超える場合、`AGGRESSIVE_TRUNCATE_CHARS (1000)` 文字を超えるすべての `ToolMessage` をハードカットします。
5. **要約の自己切り詰め**（`_truncate_summary_messages`、957 行目）: 既存の要約メッセージ（`lc_source == "summarization"`）が `SUMMARY_TOTAL_MAX_CHARS (16 000)` 文字を超えていれば、先頭 30% / 末尾 30% で再切り詰めします。
6. **リカバリー情報の注入**（`_inject_recovery_context`、922 行目）: 捕捉したファイル操作ラチェットを要約の `## Relevant Files` セクションへ書き込み、チェックポイントが常に最新の読み取り/修正ファイルマップを運ぶようにします。
7. **記帳**（`_record_compression`、635 行目）、最後に `request.override(messages=..., system_message=...)`。

すべての失敗モードは fail-open です: `_apply_compression` が例外を投げれば、例外はロギングされ（1217 行目）元のリクエストがそのまま進みます。圧縮が壊れてもターンは壊れません。

## 📝 LLM 要約: プロンプト、チェーン、フォールバック

`_create_summary` / `_acreate_summary`（768–816 行目）:

1. **シリアライズ**（`_serialize_for_summary`、167 行目）: 各メッセージがタグ付きの 1 行になります — `[User]:`（≤ 2000 文字）、`[Assistant]:`（≤ 2000 文字）、`[Assistant tool call]: name(args ≤ 500 chars)`、`[Tool result|Tool error] (id):`（≤ 1800 文字 + 省略マーカー）。
2. **前チェックポイントのチェーン**（`_extract_previous_summary`、734 行目）: `additional_kwargs["lc_source"] == "summarization"` を持つ最新の `AIMessage` を見つけ、`<summary>…</summary>` 本体を抽出します。存在すれば、プロンプトは `_SUMMARY_PROMPT_FIRST` の代わりに `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE` になります（目標/制約/決定を前へ運び、競合は最新優先、FIFO 上限）。
3. 補助モデルを `config={"metadata": {"lc_source": "summarization"}}` 付きで**呼び出し**、下流ツールが要約呼び出しを識別できるようにします。
4. **ガードレール:** 空または 50 文字未満の応答は決定論的要約へフォールバックし（785 行目）、例外も同様です（789 行目）。失敗時に LLM に最終決定権を与えません。

プロンプトテンプレート（`_SUMMARY_TEMPLATE`、99 行目）は Markdown 骨格を固定します — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — 「空でも全セクションを維持」と秘密保持ルール（"NEVER include API keys, tokens, passwords, secrets"）付き。`_enforce_fifo_limits`（283 行目）は返されたテキストに項目上限を決定論的に再適用し、`"(N earlier items omitted for brevity)"` を追記します。

## 🧱 静的フォールバック (LLM を使わない要約)

`_build_static_fallback_summary`（198 行目）はモデル呼び出しゼロで同じセクション骨格を作ります:

- 最後のユーザー要求 → *Latest Unresolved User Request*; 最初の要求 → *Goal*;
- 決定キーワード（`decided`、`choosing`、`because`、`therefore`）を含む AI テキスト → *Key Decisions*、なければ *Completed*;
- すべてのツール呼び出し → *Completed*; パス風トークン（`/` か `\` を含む、または `.py`/`.md`/… で終わる） → *Relevant Files*（≤ 10）;
- エラーの `ToolMessage` → *Blocked* と *Critical Context*。

`skip_llm` が有効なときはそのまま使われ、短い/失敗した LLM 要約のセーフティネットにもなります。

## 🧹 非 LLM 戦略

3 戦略とも 1 パスで実行され（851 行目）、`PROTECTED_TOOLS = {"memory", "skill_view", "skill_list"}` を尊重します:

| 戦略 | モジュール | メカニズム |
| :------- | :----- | :-------- |
| **Dedup** | `tool_output_dedup.py` | 繰り返される同一のツール出力を折りたたむ |
| **Prune** | `tool_output_prune.py` | `ToolMessage` を**新 → 旧**の順に歩き、要約メッセージか `status="compacted"` の結果で停止; 保護ツールはスキップ; サイズを累積 (chars // 4) — 最新の `PRUNE_PROTECT_TOKENS (40 000)` トークンを超えた出力の内容は `[Old tool result content cleared]` に置き換えられる。総削減量 ≥ `PRUNE_MIN_REDUCTION_TOKENS (5 000)` のときのみ適用 |
| **Target truncate** | `target_truncation.py` | 過大な出力を `current_tokens × TARGET_TRUNCATE_RATIO (0.5)` 方向へ縮小: `MIN_OUTPUT_CHARS_TO_TRUNCATE (500)` 文字以上の出力を `MAX_TOOL_OUTPUT_CHARS (2 000)` 文字へ切り詰め |

先制的切り詰め（パイプライン前、517 行目）はさらに、保護されていないすべての `ToolMessage` を 2000 文字に制限し、先頭 30%/末尾 30% を残して `...[omitted N chars]...` マーカーを付けます。

## 📦 出力: 要約メッセージペア

`_build_new_messages`（822 行目）は要約テキストを包み、正確に 2 つのメッセージを出力します:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — ロール交互を保つ中立的な質問。
- **AIMessage** は `additional_kwargs={"lc_source": "summarization"}` を持つ — 以降のターンが (a) 前チェックポイントを見つけてチェーンし、(b) prune をチェックポイントで止めさせ、(c) 総合テストが要約が置き換えられた後モデルビューから swallow 可能だと表明するためのマーカー。
- 総内容は `SUMMARY_TOTAL_MAX_CHARS (16 000)` 文字で上限付き、先頭/末尾 30/30 を維持。

## 🛡️ アンチスラッシングと劣化リカバリー

状態はセッションスコープの `state_register_mem` の下、9 つの `summarization_*` キーに置かれ、毎ターン `before_agent` がリセットします（1159 行目）。

**圧縮ガバナー**（`_should_skip_compression` / `_record_compression`、613–662 行目）:

| ガード | 閾値 | 効果 |
| :---- | :-------- | :----- |
| 総試行回数 | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | セッション全体で圧縮を完全に停止 |
| 連続無効回数 | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` をセット — 非 LLM 戦略のみ許可 |
| 有効性 | メッセージ数の減少 **または** トークン削減 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功した非 LLM 戦略は `skip_llm` を再び解除 |

**劣化モニター**（`_monitor_degradation`、988 行目）: 圧縮後、モデルの応答にテキストがなければカウンターが増え、`DEGRADATION_NO_TEXT_THRESHOLD (3)` 回連続の空応答に達するとミドルウェアが強制リカバリーを実行します — カウンターのリセット、`skip_llm` の解除、圧縮の再有効化 — 最大 `MAX_RECOVERY_ATTEMPTS (2)` 回まで。空でない応答はカウンターをリセットします。このガードは「圧縮 → モデル混乱 → 空出力 → 再圧縮」の病的なループを捉えます。

## 🔄 システムプロンプトの再構築

メインエージェントのみ（`need_update_system_prompt=True`、1068–1074 行目）: 圧縮後はペルソナファイル/長期記憶の関連性が変化している可能性があるため、ミドルウェアは `memory_store` をディスクから再読み込みし、`workspace.prompt_builder.build_system_prompt(session_id)` でシステムプロンプトを再構築し、その値を `state_register_mem` と `state_register_db` の**両方**の `system_prompt` キーへ書き込み、`request.override(system_message=SystemMessage(...))` で注入します。外側の `ContextEngineHook` が以降の呼び出しでレジスターからこの値を拾います。

## 📌 登録箇所

```python
# agent/core.py:152 — main agent (Summarization is the LAST middleware:
# innermost wrap layer, closest to the LLM)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — worker agent (first middleware)
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

すべての閾値は `config/num.py` にあります。◆ 印の値はミドルウェアが取り込む値、○ 印の値は定義はあるもののミドルウェアが**消費しない**値です（正直な制限事項を参照）。

| 定数 | 値 | 消費箇所 |
| :------- | :---- | :------------- |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | 先制ゲート — truncate-only 閾値 |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | 先制ゲート — compact 閾値; 両トリガー節の構築にも使用 |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 予算の下限; コンテキストウィンドウがないときの予算 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 予算の上限 |
| `PRESERVE_RATIO` ◆ | `0.25` | 予算 = コンテキストウィンドウの 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | prune: 最新側で保持されるツール出力トークン |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | prune: 適用に必要な最小削減量 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | target-truncate: 現在トークンの 50% 方向へ縮小 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | target-truncate: 適用資格 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | target-truncate: 出力あたり上限 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | アグレッシブバックストップの切断長 |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 要約メッセージの文字上限 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | すべての先頭/末尾保持率 |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 強制リカバリー前の空応答回数 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 強制リカバリーの予算 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | ガバナー: セッション試行上限 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | ガバナー: 連続無効 → LLM スキップ |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | ガバナー: トークン削減の有効性 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | すべてのシュリンク戦略から免除 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 最終ターン圧縮ゲート |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO セクション上限 |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | ファイル操作ラチェットのリスト上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | リカバリーコンテキストの要求上限 |
| `CHARS_PER_TOKEN`（推定器） | `4` | 決定論的トークン推定の除数 |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | ミドルウェアが取り込むが読まない |
| `AUTO_CONTINUE_PROMPT` ○ | — | ミドルウェアが取り込むが読まない |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 定義のみ、未取り込み |
| `COMPRESSION_RESERVE_TOKENS` ○ | `16_000` | 定義のみ、未取り込み |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 定義のみ、未取り込み（900 文字のリスト上限のみ使用） |

## 🧪 テスト

| スイート | カバー範囲 |
| :---- | :----- |
| `tests/module/test_summarization_comprehensive.py` | 140 ケースのモジュールスイート: トリガーゲート、予算/カットオフ、FIFO 上限、フォールバック、prune/dedup/target-truncate、劣化 |
| `tests/integration/test_interrupt_marker_approach.py` | マーカーの意味論: 要約ペアは後続の圧縮でも生存; `AIMessage` の `lc_source`; 最終ターン圧縮 |
| `tests/unit/test_pub_func_message_tools.py` | 推定器、prune（マーカー置換、保護ウィンドウ、最小削減ゲート） |
| `tests/module/test_summarization_trigger.py` | 本番登録契約（キャップなしウィンドウ、0.80 しきい値）+ 低トークン パススルー回帰 |
| `tests/integration/` 密閉 e2e | ネットワークアクセスゼロのフルグラフ静的フォールバック圧縮 |

プロセス分離のフルスイート（`uv run python tests/run_tests_split.py`）は **2071 passed / 0 failed** で通過します（GROUP A 1384P/2S + GROUP B 687P/5D）。

## ⚠️ 正直な制限事項

- **`keep=("messages", 10)` は受け取られるが使われない。** コンストラクターが API 互換のために保持するだけで、実際の末尾保持は純粋に予算ベースです（`PRESERVE_RATIO` × コンテキストウィンドウ、[2000, 15000] にクランプ）。`keep` を変えても効果はありません。
- **ドキュメントそのままの import。** `json`、`hashlib`、`SUMMARY_TRIM_TOKENS`、`AUTO_CONTINUE_PROMPT` は `summarization.py` の先頭で import されるが読まれません — このファイルが書かれた仕様と一緒に書き写されたもので、lint ゲートの免除でカバーされます。`DEGRADATION_MONITOR_COUNT`、`COMPRESSION_RESERVE_TOKENS`、`FILE_OPS_SECTION_MAX_CHARS` は `config/num.py` で定義されますが、消費者はいません。
- **推定器はトークナイザーではなく `chars // 4`。** 意図的に決定論的（再現可能なテスト、安定した予算）で、英語/コード混合コンテンツに合わせて校正されています; CJK が多いコンテンツは過小カウントされます（中国語は 4 よりトークンあたり 1–2 文字に近い）。
- **報告された usage が推定に勝る。** 最後の `AIMessage` が `usage_metadata.total_tokens` を持っていれば、その数値（API 側の完全なカウントを含む）がトリガリングを主導します — ローカル推定はあくまでフォールバックです。
- **圧縮は fail-open。** `_apply_compression` 内の例外はすべてロギングされ呑み込まれ、そのターンは未圧縮の履歴で進行します。したがって、系統的に壊れた補助 LLM は壊れたターンではなく、より頻繁な非 LLM シュリンクに帰着します。
- **静的フォールバックはヒューリスティック。** キーワードベースの決定/完了分類と生のツール引数からのパス抽出はベストエフォートです: セクション骨格は保証されますが、内容の質は保証されません。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` タグ/`lc_source="summarization"` は荷重を支える正確な文字列。** 以降ターンのチェーン（`_extract_previous_summary`）、prune の停止条件、テストスイートがすべて文字通り照合します — 安易に言い換えないでください。
