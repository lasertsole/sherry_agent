# 🗜️ コンテキスト圧縮：Summarization ミドルウェア

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが長い会話をモデルのコンテキストウィンドウの中に収め続ける仕組み: 5つのトリガーポイントがライフサイクル全体（ターン開始前、すべてのモデル呼び出し前、すべてのモデル応答後、プロバイダのオーバーフローエラー時）を監視し、純粋関数型の4ルートルーターが最も安価な修復手段を選び（まず大きいツール結果と過大なツール呼び出し引数を切り詰め、強制されたときだけ AI 圧縮）、アンチスラッシングガードが圧縮の暴走を構造的に防ぎます。

一次情報: `agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/compression.py`、`agent/middlewares/summarization/overflow.py`、`agent/middlewares/summarization/summary_generation.py`、`agent/middlewares/summarization/thrash.py`、`agent/middlewares/summarization/state_aliases.py`、`agent/middlewares/summarization/plan_context.py`、`pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/llm_error_classifier.py`、`pub/func/estimate_tokens.py`、`pub/func/message/tool_output_dedup.py`、`pub/func/message/tool_output_prune.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`pub/func/message/turn_utils.py`、`config/features/agent_side/summarization.py`、および 2 つの登録箇所 `agent/core.py` と `agent/tools/subagent/spawn/core.py`。本文書の行番号と定数はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [トリガー：ライフサイクルとオーバーフロー判定](triggers/README.ja.md)
  - [🧭 ライフサイクル：5つのトリガーポイント（T1–T5）](triggers/README.ja.md#-ライフサイクル5つのトリガーポイントt1t5)
  - [🚦 4ルート・オーバーフロー判定](triggers/README.ja.md#-4ルートオーバーフロー判定)
- [トークン推定（トークナイザなし）](#-トークン推定トークナイザなし)
- [圧縮の内部](internals/README.ja.md)
  - [✂️ 切り詰めトラック：予算切り詰めとTTLモジュール](internals/README.ja.md#-切り詰めトラック予算切り詰めとttlモジュール)
  - [🔁 圧縮トラック：`_apply_compression` の内部](internals/README.ja.md#-圧縮トラック_apply_compression-の内部)
  - [🖼️ 圧縮時メディア・オフロード（インライン・メディア → 参照）](internals/README.ja.md#-圧縮時メディアオフロードインラインメディア--参照)
  - [📝 LLM要約：プロンプト、チェイニング、フォールバック](internals/README.ja.md#-llm要約プロンプトチェイニングフォールバック)
  - [🧱 静的フォールバック（LLMを使わない要約）](internals/README.ja.md#-静的フォールバックllmを使わない要約)
  - [📦 出力：要約メッセージペア](internals/README.ja.md#-出力要約メッセージペア)
  - [🛡️ スラッシング防止マトリクスと劣化リカバリ](internals/README.ja.md#-スラッシング防止マトリクスと劣化リカバリ)
  - [🔄 システムプロンプト更新](internals/README.ja.md#-システムプロンプト更新)
  - [📌 登録箇所](internals/README.ja.md#-登録箇所)
- [設定リファレンス](#-設定リファレンス)
- [テスト](#-テスト)
- [⚠️ 正直な限界](#%EF%B8%8F-正直な限界)

## 🎯 概要

`Summarization`（`agent/middlewares/summarization/core.py`、クラスは `core.py:234`）は**ゼロから実装された** `AgentMiddleware` であり、LangChain 組み込みの `SummarizationMiddleware` を継承し**ません**。エージェントライフサイクルの正確に 2 箇所だけにフックします:

- `before_agent` / `abefore_agent`（`core.py:399` / `core.py:403`）—— **T1 事前点検**
- `wrap_model_call` / `awrap_model_call`（`core.py:413` / `core.py:495`）—— **T2 ディスパッチ、T3 応答後の再確認、T4/T5 エラー復帰リング**

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
| メインエージェント（`agent/core.py:204`） | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| ワーカー/サブエージェント（`agent/tools/subagent/spawn/core.py:909`） | `("messages", 40)` **または** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`（デフォルト） |

どちらも `main_llm_context_window=main_llm_max_tokens`（`MAIN_LLM_MAX_TOKEN` 由来）と `keep=("messages", 10)` を渡します。

## 🪙 トークン推定（トークナイザなし）

`pub/func/estimate_tokens.py`（230 行）は意図的にトークナイザを使わず、決定論的に動作し、3 段階のフォールバックを持ちます:

- **T1 — API 報告使用量:** 最後の `AIMessage` が `usage_metadata` を持つ場合（または呼び出し側が `reported_tokens` を明示した場合）、`estimate_messages_tokens` はその値をそのまま返します — プロバイダーの実測値がローカル推定をすべてショートカットします;
- **T2 — CJK 対応ヒューリスティック:** `estimate_text_tokens` はテキストを CJK 文字（`// CHARS_PER_TOKEN_CJK = 2`）とそれ以外（`// CHARS_PER_TOKEN = 4`）に分け、検出には `pub.func.cjk.count_cjk` を再利用します;
- **T3 — レガシー `len // 4`:** 独立したコード経路ではなく、`count_cjk(text) == 0` のときの T2 の退化ケースです — そのため純 ASCII の推定値は旧来の数値と完全に一致します。

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# メッセージ単位: str content → テキスト推定
#            + Σ tool_call name/args 文字 + tool_call_id 文字
# リスト content → ブロック単位: テキストブロックはテキスト、メディア
#   ブロックは型ごとの固定コスト、未知ブロックは保守的な未知コスト
```

content が**リスト**の場合はブロック単位で数え、リスト全体を JSON 直列化することは決してありません: テキストブロックはテキストとして推定し、メディアブロック（`image_url` / `audio_url` / `video_url` / `audio_bytes` / `video_bytes`、または `data:` ペイロードを含む任意のブロック）は `TOKEN_ESTIMATION` の固定コスト —— `tokens_per_image_block = 85`、`tokens_per_audio_block = 256`、`tokens_per_video_block = 1024` —— を、未知ブロックは `tokens_per_unknown_block = 85` を取ります。削除された JSON 経路は 5 MB の base64 を約 125 万トークンと数え、画像 1 枚で圧縮を発火させていました; これらの固定値は、モデルなしでは実際のトークン数を導出できないメディアの保守的な代替です。`str` content と `None` は変わらず、純テキストのリストは連結テキストと同じ推定になります。

`pub/func/message/estimate_msg_tokens.py` は同じヘルパー群の後方互換 re-export になりました。速く、実行間で安定し（同じ入力 → 同じ数値 → 再現可能なテスト）、意図的に保守的な近似です。トリガー/予算経路のどの部分もモデルのトークナイザに依存しません。

## ⚙️ 設定リファレンス

すべての閾値は `config/features/agent_side/summarization.py` (SUMMARIZATION TypedDict) にあります。◆ 印の定数は生きているコード経路が消費します; ○ 印の定数は定義またはインポートはされているものの、生きている経路では**消費されません**（「正直な限界」参照）。

| 定数 | 値 | 消費箇所 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route` のハードオーバーフローバンド; T3 圧力ゲート; 両トリガー節の構築 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route` のソフトオーバーフローバンド |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`（overflow.py:168）: ウィンドウ − 予備量 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 切り詰めトラック予算 = usable × 0.60（overflow.py:219） |
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
| `TOKENS_PER_IMAGE_BLOCK` / `TOKENS_PER_AUDIO_BLOCK` / `TOKENS_PER_VIDEO_BLOCK` / `TOKENS_PER_UNKNOWN_BLOCK`（推定器） | `85` / `256` / `1024` / `85` | マルチモーダル content リストのブロック単位固定コスト —— base64 は決してテキストとして数えない; `config/features/agent_side/token_estimation.py` で定義 |
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
| `tests/agent/middlewares/test_compression_media_offload.py` | 12 | 圧縮時のインライン・メディア・オフロード: 書き込み + ポインタ、内容ハッシュ重複排除、デコード/書き込み失敗のプレースホルダ、保持ウィンドウのメディア不変、同期/非同期パリティ、`evicted_refs` 収集 |
| `tests/pub/func/test_estimate_tokens_media.py` | 22 | ブロック単位のマルチモーダル推定: 固定型コスト、5 MB base64 回帰、メディアを隠す未知ブロック、`str` / `None` / 空リスト / 純テキスト境界 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 6 | チェイニング要約フィルタリング: 旧ペアを直列化会話から除去、通常/空/複数ペア入力、未マークの旧セッション human を保持、async `_acreate_summary` ミラー |
| `tests/agent/middlewares/test_summary_doc.py` + `test_summary_doc_middleware.py` | 41 | 構造化要約: schema 強制変換、レンダリング往復 + バイト安定 + セクション順、コード層 cap + 注記、latest request 逐字、json_mode/json_repair/free-form の 3 ティア、prior-doc JSON チェイニング、旧 MD 遷移、退避ポインタの収集と継承 |
| `tests/agent/middlewares/test_summary_active_plan.py` | 20 | Part 1: 計画アクティブ判定（state/todo ソース、全完了ゲート、fail-open）、初回/更新プロンプト両経路への注入、チェーン圧縮を跨ぐノートの継承/追記/cap/クリア、latest request 逐字 + 退避ポインタ、複数メッセージ連投 |
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
- **残存するが不活性なコード。** `_preemptive_check`（overflow.py:148）と `_preemptive_truncate`（compression.py:420）は参照専用です: これらが実装する 2 バンドの先取りに到達する本番呼び出し箇所はありません。
- **推定器はトークナイザではなく、3 段階のトークナイザフリー・ヒューリスティックです。** API 報告使用量があれば T1 がそれを返し、T2 が CJK 対応ヒューリスティック（CJK 文字は `CHARS_PER_TOKEN_CJK = 2`、それ以外は `CHARS_PER_TOKEN = 4`）、T3 のレガシー `len // 4` は T2 の純 ASCII 退化ケースです。意図的に決定論的（再現可能なテスト、安定した予算）です; `CHARS_PER_TOKEN_CJK = 2` は中国語が 4 ではなく 1–2 字/トークンに近いことを反映しています。
- **報告値が勝つ場所。** T3 だけが報告使用量駆動のトリガーです（`compute_pressure` は max を取る）。T1/T2 のルート判定は推定駆動です（推定値 + システムプロンプトのオーバーヘッドのみ）; レガシーの `_check_trigger` 節フォールバックは `max(ローカル推定値, 報告値)` を使います。
- **T3 は返される応答を決して変えません。** T3 ディスパッチの永続効果はツール結果のその場での切り詰め（メッセージオブジェクトはグラフ状態と共有）とアンチスラッシングの帳簿記録だけです; T3 の compact ルートの `request.override` はローカルであり、元の応答が常に返ります。T3 本体全体が fail-open です。
- **T4/T5 は設計上アンチスラッシングマトリクスを迂回します** —— それが「強制」の要点です。`MAX_OVERFLOW_RETRIES (3)`（T4/T5 共有の単一カウンタ、ターンごとにリセット）を超えるか、強制圧縮ステップ自体が失敗すると、元のプロバイダ例外が伝播します（決して飲み込まれず、圧縮エラーで置き換えられることもありません）。
- **圧縮は fail-open です。** `_apply_compression` 内のどんな例外もログに記録され、飲み込まれます; ターンは圧縮されていない履歴のまま進行します。
- **静的フォールバックはヒューリスティックです。** キーワードベースの決定/完了分類と生のツール引数からのパス抽出はベストエフォートです; セクション骨格は保証されますが、コンテンツ品質は保証されません。
- **構造化ティアが `json_mode` なのは設定済みエンドポイントがそれを要求するためです。** `glm-5.3-flash`（openai 互換）は `with_structured_output` の関数呼び出しを発行せず、デフォルトの function-calling ティアは Pydantic 解析エラーになります; `_structured_runnable` は `method` 引数を受け付けないプロバイダー向けに引数なし呼び出しへ退避し、残りは `json_repair` と free-form ティアがカバーします。コード層 cap とレンダラーが出力形状を保証します。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` タグ/`lc_source="summarization"` は荷重を支える正確な文字列です。** 後続ターンのチェイニング（`_extract_previous_summary`）、プルーン停止条件、全テストスイートがこれらを文字通り照合します —— 軽々しく言い換えないでください。
