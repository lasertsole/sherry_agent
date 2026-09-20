# 🧭 コンテキスト統治：永続化、退避、スライス、オーバーフロー・クリップ

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 生の履歴をいかに永続させ、モデル可視コンテキストをいかに小さく保つか：モデル境界ごと・ツール返却ごとの write-once 永続化、回収可能なプレビューを残すツール結果のディスク退避、実行時 `read_file` スライス、state に全文を残す人間メッセージ退避、メディア・ガバナンス（入力サイズ上限、リクエストごとのケイパビリティ・スクラブ、サイレント劣化の検出、圧縮時オフロード、ブロック単位トークン分類）、あらゆる圧縮ルートより先に走る LLM なしテールクリップ、そして古い要約を会話ペイロードから締め出すチェーン要約フィルタリング。

エージェントが生むメッセージには二つの価値がある：**生の履歴**（実際に起きたこと。検索と圧縮のため）と、**モデルコンテキスト**（今ウィンドウに収まるもの）である。本ページはこの二つを両立させる各機構を記録する —— すべてが一つの規則を共有する：**パイプラインに入ったペイロードは決して失わず、縮めるのはモデルビューだけ、そしてあらゆる縮小は全文へのポインタを残す。**

**一次情報:** `agent/middlewares/context_eviction/core.py`、`agent/middlewares/message_persistence/core.py`、`agent/middlewares/message_persistence/prepare.py`、`pub/func/message/eviction.py`、`pub/func/message/overflow_clip.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/media_offload.py`、`agent/middlewares/media_pipeline/scrub.py`、`agent/middlewares/media_pipeline/degradation.py`、`agent/middlewares/media_pipeline/media_handlers.py`、`agent/middlewares/llm_capability_cache.py`、`agent/middlewares/llm_retry/core.py`、`pub/func/estimate_tokens.py`、`context_engine/store/core.py`、`config/features/agent_side/tool_result_eviction.py`、`config/features/agent_side/summarization.py`、`config/features/agent_side/media_pipeline.py`、`config/features/agent_side/token_estimation.py`。以下の主張はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要とパイプライン](#-概要とパイプライン)
- [情報源](#%EF%B8%8F-情報源)
- [退避、メディア、オーバーフロー](eviction/README.ja.md)
  - [💾 境界ごとの永続化](eviction/README.ja.md#-境界ごとの永続化)
  - [🗜️ ツール結果の退避（P0-2）](eviction/README.ja.md#%EF%B8%8F-ツール結果の退避p0-2)
  - [✂️ `read_file` スライス（P2-4）](eviction/README.ja.md#%EF%B8%8F-read_file-スライスp2-4)
  - [📥 人間メッセージの退避（P1-9）](eviction/README.ja.md#-人間メッセージの退避p1-9)
  - [🖼️ メディア・ガバナンス（オフロード、参照、トークン分類）](eviction/README.ja.md#%EF%B8%8F-メディアガバナンスオフロード参照トークン分類)
  - [⚡ オーバーフロー・テールクリップ（P1-2）](eviction/README.ja.md#-オーバーフローテールクリップp1-2)
  - [🧵 チェーン要約のフィルタリング](eviction/README.ja.md#-チェーン要約のフィルタリング)
- [相互作用と順序保証](#-相互作用と順序保証)
- [設定](#%EF%B8%8F-設定)
- [テストマップ](#-テストマップ)
- [クリアの意味論と限界](#-クリアの意味論と限界)

## 🎯 概要とパイプライン

機構は一本のパイプラインを成す。各段はモデルビューをさらに縮め、そのどれも生の記録を破壊しない：

```text
tool returns
  │  ContextEvictionMiddleware.wrap_tool_call
  │    generic tool, text > 20 000 chars → full text to evicted/, head+tail preview in state (P0-2)
  │    read_file                         → execution-time 4 000-char head slice, no file written (P2-4)
  ▼
graph state (tool results: preview only · human messages: full text + lc_evicted_to tag)
  │
  │  MessagePersistenceMiddleware
  │    wrap_tool_call  → flush the RAW result the moment the handler returns
  │    after_model     → flush new human/ai/tool messages at every model boundary
  ▼
MesMemory (full text; persisted_message_ids watermark = write-once)
  │
  │  context pressure triggers Summarization (T1–T5)
  │    1. tail clip      — no LLM: stub the trailing contiguous ToolMessage batch (P1-2)
  │    2. existing route — truncate_tool_results_only / compact_only / compact_then_truncate
  │    3. forced recovery (T4/T5 provider errors) — clip first, then compact + budget truncate, then retry
  ▼
session end → clear_session() removes the session folder (evicted/ + plans) and the watermark
```

| 段階 | 機構 | LLM コスト | モデルビューへの影響 |
|---|---|---|---|
| **ツール返却** | `ContextEvictionMiddleware`（P0-2 / P2-4） | なし | 汎用結果 > 20 000 文字 → head/tail プレビュー + ファイルポインタ；`read_file` → 4 000 文字スライス + 通知 |
| **モデル境界** | `MessagePersistenceMiddleware` の `after_model` | なし | 全文を MesMemory へ書き込み（state は不変） |
| **人間メッセージ** | `ContextEvictionMiddleware`（P1-9） | なし | リクエストビューのみプレビューに切り詰め；state/MesMemory は全文保持 |
| **オーバーフロー（第一手）** | `clip_overflow_tail`（P1-2） | なし | 末尾のツール結果をスタブ化；メッセージ同一性とペアリングは不変 |
| **オーバーフロー（既存ルート）** | `summarization` 4 ルート分岐 | compact 系ルートは補助 LLM 1 回 | 切り詰めおよび／または履歴圧縮 |
| **オーバーフロー（プロバイダエラー）** | T4/T5 強制リカバリ | compact ステップごとに 1 回 | クリップ → 圧縮 + 予算切り詰め、最大 3 回再試行 |
| **メディア・オフロード（圧縮）** | `offload_inline_media` | なし | 要約されるプレフィックスのインライン・メディア → ディスクコピー + `[evicted to: …]` ポインタ；保持ウィンドウは不変 |

## 🗂️ 情報源

グラフ state や MesMemory に到達するすべての情報は、以下のいずれかの情報源から入る。`origin` 列は全量の情報源マーカーへと昇格中である：`NULL` はタグ付け以前に書かれた既存のユーザーメッセージ（読み側では `user` と同義）、`internal=True` を持つメッセージは**ユーザーリクエストではない** —— 要約の Unresolved リストはユーザーが直接送ったメッセージのみを受け付ける（正の識別 / positive identification）。非メッセージ情報源（退避ファイル、計画知識）も併記する：`messages` 行にはならないが、注入可能なコンテキストである。`planned` / `reserved` の行は未実装。

| 情報源 | origin / マーカー | internal | 発生場面 | 永続化 | 注入挙動 |
|---|---|---|---|---|---|
| フロントエンド WS ユーザーメッセージ | `origin='user'` | — | ユーザーがクライアントで送信 | `messages` 行 `origin='user'` + 全文（境界ごとに永続化） | state/MesMemory に常駐；モデルビューはプレビューへ退避され得る；要約は最新のユーザー要求を逐語で保持（`latest_user_request`；複数要求リスト + 要求ごとの退避ポインタ：`planned`） |
| チャネルユーザーメッセージ（QQ 等） | `origin='user'` | — | ユーザーがチャネルアダプタ経由で送信 | 上記と同じ | 上記と同じ |
| TaskIntent ステアリング / リマインダ | `origin='task_intent'` | `True` | 計画アクティブ誘導 / タスク意図アーミング（`task_intent/core.py::_task_intent_message`） | `messages` 行 | **ユーザーリクエストではない** —— Unresolved リストに入らない |
| サブエージェント完了キャリア | `origin='subagent_completion'` | `True` | バックグラウンドのサブエージェントが完了し結果を通知 | `messages` 行（origin は永続化の継ぎ目で刻印、`context_engine/store/core.py`） | ユーザーリクエストではない；モデルビューには可視 |
| ハートビート起動のターン | `origin='heartbeat'` | — | ハートビートサービスのターン（**現時点でこの経路は存在しない —— `reserved`**） | — | ユーザーリクエストではない |
| cron 起動のターン | `origin='cron'` | `True` | 定期ジョブのセッションターン（`origin_for_source`） | `messages` 行 | ユーザーリクエストではない |
| 圧縮要約ペア | `lc_source='summarization'`（`additional_kwargs` 内、origin 列ではない） | — | 圧縮成果物（`_build_new_messages`） | **MesMemory には永続化されない**；state の要約ペア | `<summary>` がモデルビューに常駐；`<prior-summary>` として連鎖継続 |
| 退避ファイル | 非メッセージ —— ディスクファイル | — | P0-2 / P1-9 退避 | `SESSIONS_DIR/<session_id>/evicted/`（バイト単位の全文） | オンデマンド `read_file`；要約チェーンは構造化要約ドキュメント内で `evicted_refs[]` ポインタを運ぶ |
| メディアファイル | 非メッセージ —— ディスクファイル | — | アップロード処理（`MultimodalProcessor`）、履歴メッセージのネイティブブロック除去、または圧縮時オフロード（`offload_inline_media`） | `SESSIONS_DIR/<session_id>/media/`（永続コピー；圧縮時オフロードは `sha256[:16]` 命名で重複排除）；`max_media_bytes` 超のペイロードは決して書き込まれない | ネイティブ時のメディアブロック；スキルパスのヒントとリクエストごとのスクラブ・プレースホルダがパスを運ぶ；要約チェーンはオフロード済みパスを `evicted_refs[]` に保つ |
| 計画知識 | 非メッセージ —— ディレクトリ | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | `plan_ref` により `<knowledge>` ブロックを注入 |
| FACTS.md | `workspace/memory/FACTS.md`（memory ツール target `facts`） | — | モジュール非依存の広範な落とし穴・規約：圧縮時の記憶レビュー + 計画完了時の抽出 | memory ファイル（1 375 文字上限；超過時は最古のエントリから淘汰） | 常駐 FACTS メモリブロックとして毎回のシステムプロンプトに注入 |

## 🔗 相互作用と順序保証

順序保証（`agent/core.py` で検証済み、リスト順 = 登録順）：

| フック段階 | ここで重要な順序 |
|---|---|
| `before_agent`（リスト順） | `MultimodalProcessor` はモデルループの**前**に走るので、P1-9 のタグ付けは常にメディアヒントが統合済みの最終テキストを見る |
| `before_model`（リスト順） | P1-9 のタグ付けは `ToolCallNormalize` / `SubagentCompletionDrainMiddleware` より先 |
| `wrap_model_call`（外→内） | ContextEviction（P1-9 ビュー置換）→ … → Summarization（最内層、LLM に最も近い） |
| `after_model`（逆順） | `MessagePersistenceMiddleware` がモデル後の**最初**のフック —— HITL が拒否ツール呼び出しを剥がすか `GraphInterrupt` を上げる前に AI メッセージが永続化される |
| `wrap_tool_call`（外→内） | IterationBudget → ToolGuardrails → ContextEviction → PathGuard → HeartbeatStaleness → HumanInTheLoop → **MessagePersistence（最内層）** —— 生の結果が先に書き込まれ、戻る途中でプレビューに差し替わる |

相互作用マップ：

| 相互作用する機構 | 何が起こるか |
|---|---|
| **Summarization / 圧縮** | P1-9 が全文を state に残すので圧縮は依然としてそれを見る；要約ペアは永続化と次の `<conversation>` の両方から除外される |
| **P1-2 オーバーフロー・テールクリップ** | `model_copy` で `ToolMessage` 内容のみをスタブ化；`HumanMessage` には決して触れないので `lc_evicted_to` タグと state 全文はあらゆるクリップを生き延びる；スタブは P0-2 / P2-4 マーカーを先へ運ぶ |
| **チェーン要約フィルタリング** | シリアライズされる会話からのみ `lc_source="summarization"` メッセージを除去；state トランスクリプトと MesMemory ストアは不変 |
| **HITL** | 拒否はツール返却の書き込みを迂回し（HITL が外側から永続化を包む）、次の境界で永続化される；永続化バッチは拒否ツール呼び出しを再付着させ、拒否がペアの AI 行を保つ。HITL は `HumanMessage` を見ないので退避とは直交する |
| **`message_search`** | FTS5/SQLite 検索は MesMemory 上で走り、そこには**完全な**ツール結果と**完全な**人間テキストがアーカイブされている —— 退避はモデルビューを縮めるだけ |
| **プレフィックスキャッシュ** | P1-9 は id によりその場でメッセージを更新し（内容同一・id 同一）、他は何も書き換えないので、モデル可視プレフィックスが無効化されるのはプレビュービューが実際に異なるときだけ；ツール退避はメッセージが state に入る前に起きるので、モデルが見るのは最初からプレビュー版だけ |
| **ツールペアリング / サニタイザ** | すべての置換が `id` と `tool_call_id` を保持する；`sanitize_tool_use_result_pairing` が退避のために修復することは決してなく、スタブは有効なペアリング入力であり続ける |
| **サブエージェントセッション** | 子パイプラインは `ContextEvictionMiddleware` と `MessagePersistenceMiddleware` を**登録しない**：子トランスクリプトは完全なツール結果を保ち、checkpoint のみに存在する |

## 🛠️ 設定

`TOOL_RESULT_EVICTION`（`config/features/agent_side/tool_result_eviction.py`）：

| キー | デフォルト | 意味 |
|---|---|---|
| `enabled` | `True` | ツール結果退避（P0-2）のマスタースイッチ |
| `evict_threshold_chars` | `20_000` | この文字数を超えるテキストを退避 |
| `preview_head_lines` / `preview_tail_lines` | `5` / `5` | プレビューの head/tail 行数 |
| `eviction_subdir` | `"evicted"` | `SESSIONS_DIR/<session_id>/` 下のサブディレクトリ |
| `excluded_tools` | 8 名 | 決して退避しない（`read_file` はスライス経路へ） |
| `human_evict_enabled` | `True` | 人間メッセージ退避（P1-9）のマスタースイッチ |
| `human_evict_threshold_chars` | `200_000` | 人間メッセージのトリガ閾値 |
| `human_preview_head_lines` / `human_preview_tail_lines` | `5` / `5` | 人間メッセージプレビューの head/tail 行数 |

`SUMMARIZATION`（`config/features/agent_side/summarization.py`）のうち本ページが依存するキー：

| キー | デフォルト | 意味 |
|---|---|---|
| `overflow_clip_enabled` | `True` | P1-2 テールクリップのマスタースイッチ |
| `overflow_clip_max_remove` | `10` | 一回のクリップでスタブ化する末尾メッセージの上限 |
| `overflow_clip_min_keep` | `5` | トランスクリプト下限：これ以下なら決してクリップしない |
| `max_tool_output_chars` | `2_000` | ツール結果の圧縮時クリップ予算 |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | 圧縮時クリップの head/tail 保持比率 |

`TOKEN_ESTIMATION`（`config/features/agent_side/token_estimation.py`）、マルチモーダル・トークン分類：

| キー | デフォルト | 意味 |
|---|---|---|
| `chars_per_token` / `chars_per_token_cjk` | `4` / `2` | テキスト推定の除数（非 CJK / CJK） |
| `tokens_per_image_block` | `85` | 画像ブロックあたりの固定コスト（`langchain_core.count_tokens_approximately` と整合） |
| `tokens_per_audio_block` / `tokens_per_video_block` | `256` / `1024` | 保守的な固定コスト（推定時に長さメタデータなし） |
| `tokens_per_unknown_block` | `85` | 未知ブロックの固定コスト —— その base64 では決してない |

`MEDIA_PIPELINE`（`config/features/agent_side/media_pipeline.py`）の入口側キー：

| キー | デフォルト | 意味 |
|---|---|---|
| `main_llm_native_multimodal` | `"auto"` | 三態ネイティブ・スイッチ：`"true"` はモデルにメディアブロックを残し、`"false"` は常にスキルパス、`"auto"` はケイパビリティ・キャッシュでファミリーごとに決定。それ以外の値はフェイルセーフでスキルパス |
| `main_llm_silent_degradation_detection` | `True` | ネイティブ返答がメディア盲目を自述したとき、リクエストに現れたメディアファミリーを `"unsupported"` としてキャッシュ |
| `max_media_bytes` | `20 * 1024 * 1024` | ペイロード単位のハード上限。超過ペイロードは書き込み前にスキップ |

これらのノブに環境変数はない：設計上、上記 feature TypedDict のコードデフォルトである。

## 🧪 テストマップ

| スイート | カバー内容 |
|---|---|
| `tests/agent/middlewares/context_eviction/test_context_eviction.py` | P0-2/P2-4 ミドルウェア挙動：退避、除外、スライス、ウォーターマーク被覆、フェイルオープン |
| `tests/agent/middlewares/context_eviction/test_human_eviction.py` | P1-9 タグ付け、reducer のその場更新、モデルビュー切り詰め、自己修復、メディア保持 |
| `tests/agent/middlewares/message_persistence/test_message_persistence.py` | 境界永続化、ウォーターマーク write-once、拒否の再ペアリング |
| `tests/agent/middlewares/message_persistence/test_tool_result_persistence.py` | ツール返却時の書き込みと、id なしフィンガープリント／マーカーとの相互作用 |
| `tests/agent/middlewares/message_persistence/test_compression_no_persistence.py` | 圧縮経路が MesMemory へ何も書かないこと |
| `tests/pub/func/message/test_eviction.py` | 純粋な退避プリミティブ：閾値、プレビュー、冪等性、安全でないセッション id |
| `tests/pub/func/message/test_read_file_slice.py` | P2-4 実行時スライスとその冪等性 |
| `tests/pub/func/message/test_overflow_clip.py` | P1-2 純粋クリップ：末尾バッチ検出、ゲート、トークン目標、マーカー保持 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | P1-2 ミドルウェア統合：ゼロ LLM 回復、劣化、T4/T5、同期／非同期パリティ |
| `tests/agent/middlewares/test_summary_message_filtering.py` | チェーン要約フィルタリングと `<prior-summary>` 注入 |
| `tests/agent/middlewares/test_compression_media_offload.py` | 圧縮時インライン・メディア・オフロード：書き込み + ポインタ、ハッシュ重複排除、失敗プレースホルダ、保持ウィンドウ不変、同期／非同期、`evicted_refs` |
| `tests/pub/func/test_estimate_tokens_media.py` | ブロック単位マルチモーダル推定：固定メディアコスト、5 MB base64 回帰、メディアを隠す未知ブロック、`str` / `None` / 空リスト境界 |
| `tests/agent/middlewares/test_multimodal_processor.py` | 三態ネイティブ・スイッチとリクエストごとのスクラブ：混合ファミリーは非対応ブロックだけを剥がし、対応ブロックはそのまま、state/kwargs/ディスク不変、`"true"` は決してスクラブしない、履歴画像の除去 |
| `tests/agent/middlewares/test_media_size_limit.py` | `max_media_bytes` 上限：超過ペイロードは書き込み前にスキップ、モデル可視の通知、上限ちょうどは許可、申告 `Content-Length` の高速パス、制限付き読み取り |
| `tests/agent/middlewares/test_media_degradation.py` | サイレント劣化の検出：en / zh / ja / ko の盲目正規表現 + 説明要求パターン、有能・無関係な返答は誤検出しない、実際の全ファミリーをキャッシュ、どちらでもフラグ解除、フォールバック候補への帰属 |
| `tests/agent/middlewares/test_multimodal_native_fallback_e2e.py` | 拒否 → キャッシュ + スキルパス書き換え：次セッションのネイティブ・スキップ、モデルキー分離、明示 `"true"` はフォールバックしない |
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` ウォーターマークストア |
| `tests/full/test_context_governance_e2e.py` | ライブネットワーク e2e（実 LLM + 実グラフ）：各機構の端から端まで —— 明示的に実行 |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/agent/middlewares/test_multimodal_processor.py \
    tests/agent/middlewares/test_media_size_limit.py \
    tests/agent/middlewares/test_media_degradation.py \
    tests/agent/middlewares/test_multimodal_native_fallback_e2e.py \
    tests/agent/middlewares/test_compression_media_offload.py \
    tests/pub/func/test_estimate_tokens_media.py \
    tests/context_engine/store/test_persisted_message_ids.py -q

# Live-network e2e — RUN EXPLICITLY, never part of the CI gate
uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v
```

## 🧹 クリアの意味論と限界

- **`clear_session` はすべてを一度に消す。** `server/DAO/messages.py::clear_session` はセッションの MesMemory 行（`persisted_message_ids` ウォーターマークごと削除）、checkpointer 履歴、そして `SESSIONS_DIR/<session_id>/` フォルダ全体を削除する —— したがって **`evicted/` ファイルと `plans/` はセッションとともに削除される**（`config/path.py::session_plans_dir` が同じ丸ごと削除の契約を記している）。インメモリレジスタは最後にクリアされる。
- **退避はモデルビューの縮小であり、削除では決してない。** 本ページが縮めるすべてのペイロードは、MesMemory にアーカイブされるか、グラフ state に全文があるか（人間メッセージ）、ディスクの `evicted/` 下にある —— そしてすべてのプレビューがポインタを運ぶ。
- **`evicted/` ディレクトリはセッション所有だがガベージコレクションされない。** ファイルは `clear_session` まで生きる；メッセージ単位の TTL はない。巨大なツール結果の多い長寿セッションは `workspace/sessions/<session_id>/evicted/` にディスク使用を蓄積し得る。
- **一行の巨大行は決して退避されない。** head と tail の両方がペイロード全体を含む場合、プレビューは原文より小さくなれず、メッセージはそのまま残る（ツール経路・人間経路とも）。
- **`read_file` スライスはスライス自体からは復元できない —— 設計上。** ソースファイルが回復経路であり、通知が続きの読み方を正確に告げる。ファイルの欠落／削除だけがこれを破る。
- **人間メッセージ退避は末尾メッセージに限られる。** 巨大ペイロードの後にさらに別のユーザーターンが続いても再検査されない；「最後のメッセージのみ」は確定済み履歴を再訪しないための意図的な設計である。
- **テールクリップは末尾バッチが大きいときにのみ効く。** コンテキストが人間ターンや非ツールメッセージに食われているトランスクリプトは既存ルートへ劣化する；P1-2 は最適化であり、保証ではない。
- **サブエージェントのトランスクリプトは対象外。** 子は完全なツール結果を保つ（退避なし・永続化なし）—— そのトランスクリプトは checkpoint のみに存在し、クライアント可視の MesMemory 履歴には決して入らない。
