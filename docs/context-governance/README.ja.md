# 🧭 コンテキスト統治：永続化、退避、スライス、オーバーフロー・クリップ

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 生の履歴をいかに永続させ、モデル可視コンテキストをいかに小さく保つか：モデル境界ごと・ツール返却ごとの write-once 永続化、回収可能なプレビューを残すツール結果のディスク退避、実行時 `read_file` スライス、state に全文を残す人間メッセージ退避、あらゆる圧縮ルートより先に走る LLM なしテールクリップ、そして古い要約を会話ペイロードから締め出すチェーン要約フィルタリング。

エージェントが生むメッセージには二つの価値がある：**生の履歴**（実際に起きたこと。検索と圧縮のため）と、**モデルコンテキスト**（今ウィンドウに収まるもの）である。本ページはこの二つを両立させる六つの機構を記録する —— すべてが一つの規則を共有する：**データは決して失わず、縮めるのはモデルビューだけ、そして全文へのポインタを必ず残す。**

**一次情報:** `agent/middlewares/context_eviction/core.py`、`agent/middlewares/message_persistence/core.py`、`agent/middlewares/message_persistence/prepare.py`、`pub/func/message/eviction.py`、`pub/func/message/overflow_clip.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/media_offload.py`、`pub/func/estimate_tokens.py`、`context_engine/store/core.py`、`config/features/agent_side/tool_result_eviction.py`、`config/features/agent_side/summarization.py`、`config/features/agent_side/token_estimation.py`。以下の主張はすべてこのコードと突き合わせて検証済みです。

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
| 計画知識 | 非メッセージ —— ディレクトリ | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | `plan_ref` により `<knowledge>` ブロックを注入 |
| FACTS.md | `workspace/memory/FACTS.md`（memory ツール target `facts`） | — | モジュール非依存の広範な落とし穴・規約：圧縮時の記憶レビュー + 計画完了時の抽出 | memory ファイル（1 375 文字上限；超過時は最古のエントリから淘汰） | 常駐 FACTS メモリブロックとして毎回のシステムプロンプトに注入 |

## 💾 境界ごとの永続化

`agent/middlewares/message_persistence/core.py`（`MessagePersistenceMiddleware`）は本ページ全体が立脚する永続化の床である。**二つのタイミング**で書き込む：

| メッセージ | 永続化のタイミング |
|---|---|
| `ToolMessage` | **ツールハンドラが返った瞬間**（`wrap_tool_call` / `awrap_tool_call`） |
| `HumanMessage` | そのターン最初のモデル境界（`after_model` / `aafter_model`） |
| `AIMessage`（`tool_calls` を含む） | モデルが生成した直後の境界 |
| HITL 拒否（`status="error"` の ToolMessage） | 次のモデル境界 —— HITL は本ミドルウェアを外側から包むため、その短絡は書き込み層に届かない |

両タイミングは一つのバッチパイプラインを共有する：役割／マーカーフィルタ → ウォーターマーク → HITL 拒否の再ペアリング + ツール結果重複排除 → 書き込み → マーク。重要な詳細：

- **`persisted_message_ids` ウォーターマーク（write-once）。** 書き込み前に `filter_persisted_message_ids` が、セッションの墓碑に既にある検索キーを持つ候補を落とす。書き込み後に `mark_message_ids_persisted` が、ライターへ渡した全候補に墓碑を打つ。ウォーターマークは SQLite テーブル（`context_engine/store/db.py`）なので再起動を生き延びる。
- **id + フィンガープリントの二重検索。** ツール結果は返却時に永続化され、**その時点ではグラフ reducer がまだ id を割り当てていない**。そのためウォーターマーク検索は LangGraph メッセージ id と `sha1:` 内容フィンガープリント（role + content + `tool_call_id`）の両方を確認する。次の境界も再起動後のリプレイも、どちらかのキーで同じ論理メッセージに一致する（`prepare.py` の `_watermark_lookup_keys`）。
- **要約ペアは決して永続化されない。** `_is_persistable` は `additional_kwargs["lc_source"] == "summarization"` の付いた全メッセージ（ペアの AI 側）を落とし、`HumanMessageRowBuilder.build`（`context_engine/store/core.py`）は人間側に `None` を返す。圧縮成果物はプロンプトの足場であり、会話履歴ではない。
- **フェイルオープン。** `session_id` が無ければ黙ってスキップ；ライターのエラーはログのみで墓碑を打たず、同じバッチが次の境界で再試行される。永続化がターンやツール結果を壊すことは決してない。

これが、本ページの以降の機構があれほど積極的でいられる理由である：**あらゆる後続段が縮める前に、すべてのペイロードはすでに永続化済みなのだ。**

## 🗜️ ツール結果の退避（P0-2）

`ContextEvictionMiddleware.wrap_tool_call` はツール応答を**グラフ state に入る前に**傍受する（`agent/middlewares/context_eviction/core.py`；プリミティブは `pub/func/message/eviction.py`）：

- 抽出テキストが **`evict_threshold_chars`（20 000 文字）を超える**汎用結果は `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt` に書き出され、メッセージ内容はファイルパスを運ぶ head/tail プレビューに置き換わる。
- `excluded_tools`（8 名 —— `read_file`、`write_file`、`patch_file`、`search_files`、`list_files`、`memory`、`skill_view`、`skill_list`）はそのまま通過する：ペイロードはすでにバックエンドのファイルシステム上にあるか、回収が安価である。`read_file` はさらに下のスライス経路を取る。
- 置換は `ToolMessage.model_copy` で構築されるため、メッセージ `id`、`tool_call_id`、`name`、`status`、`additional_kwargs` がすべて生存する —— ペアリング、サニタイザ、ウォーターマークは同じ論理メッセージに一致し続ける。
- マルチモーダルの非テキストブロック（画像／音声／動画）はそのまま保持され、テキストブロックのみ置換される。
- 安全ガード：安全でない `session_id`（空 / `.` / `..` / 区切り文字入り）はディスクに触れずスキップ；`[evicted to: …]` マーカー付きのメッセージは二度と退避されない；プレビューが原文より小さくならない場合（一行の巨大行）はスキップ。

プレビュー形式（`pub/func/message/eviction.py`、`preview_head_lines = preview_tail_lines = 5`）：

```text
[evicted to: <path>]
--- head (5 lines) ---
<first 5 lines>
...
--- tail (5 lines) ---
<last 5 lines>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

**三つの保管場所** —— 退避後、各コピーがどこにあるか：

| 場所 | 保持するもの |
|---|---|
| グラフ state / checkpointer / 次のモデル呼び出し | **プレビューのみ** |
| MesMemory（`messages` テーブル） | **全文**。ツール返却の瞬間に内側の永続化が書き込む |
| `SESSIONS_DIR/<session_id>/evicted/` | バイト単位で同一のコピー；`load_evicted()` / `read_file` で取り出せる |

これを成立させる順序：`wrap_tool_call` チェーンで `MessagePersistenceMiddleware` は**最内層**におり、`ContextEvictionMiddleware` はその**外側**に位置する。したがって生の結果が先に書き込まれ、プレビューだけが state へ進む。ウォーターマークはどちらでも覆われる：`model_copy` は内側の書き込みが付けたプロセス内 `_db_persisted` マーカーを運び、生の書き込みが成功した場合は置換メッセージのウォーターマークキーにも追加の墓碑が打たれる（`_cover_with_watermark`）—— マーカーが失われ、プレビューのフィンガープリントが生の内容と一致しなくなった再起動をこれが覆う。生の書き込みが失敗した場合は何も墓碑を打たず、次の境界がプレビュー内容で再試行する。

## ✂️ `read_file` スライス（P2-4）

`read_file` の結果は**退避されない** —— ファイルはすでにディスク上にあり、二つ目のコピーを書くのは純粋な重複である。代わりに `slice_read_file_result`（`pub/func/message/eviction.py`）が内容を**先頭 `_READ_FILE_SLICE_CHARS`（4 000）文字**と回復通知（`"...[Output was truncated due to eviction threshold. Use read_file with offset and limit to retrieve specific portions.]"`）に置き換える。退避ファイルは書かれず、ヘルパーは冪等である：すでにスライス済み（通知あり）の結果はそのまま返る。

これは二段階縮小の**実行時**側である。**圧縮時**側は `pub/func/message/target_truncation.py::_truncate_read_file_content` にある：圧縮がコンテキストを切る際、各 `ToolMessage` を `tool_call_id` で `read_file` 呼び出しに解決し、`max_tool_output_chars`（2 000）の head 30% + tail 30% を残し、中間を**パーサ由来の 1-based 継続オフセット**を運ぶ回復通知に置き換える（`Use offset=<N> to continue reading…`；ペイロードが解析できないときは「先頭から読み直し」にフォールバック）。

二段階は構造的に補完し合う：圧縮時が実行時スライス済みペイロードを後から切ると、切り詰められた JSON はもはや解析できず、圧縮通知は決定論的に再開形式へフォールバックする —— 誤ったオフセットが emit されることは決してなく、実行時スライスヘルパーが自らの出力を二度スライスすることもない。

## 📥 人間メッセージの退避（P1-9）

ユーザーはどのツールも生み出さないペイロードを貼り付け得る：ログ、文書、文字起こし、コードベース全体。`ContextEvictionMiddleware` は DeepAgents の人間メッセージ退避を、Sherry 固有の分割で移植している。

- **トリガ**（`before_model` / `abefore_model`）：`human_evict_enabled` が真、かつ**最後の**メッセージが `HumanMessage`、かつ `lc_evicted_to` を持たず、かつ抽出テキストが **`human_evict_threshold_chars`（200 000 文字）を超える**。検査されるのは最後のメッセージだけなので、過去のユーザーターンが再訪されることはない。
- **タグ付け + 退避**（`evict_human_message`）：全文が `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|timestamp>.md` に書かれ、フックは部分 state 更新 `{"messages": [tagged]}` を返す。`tagged` は **id と内容が不変**で、`additional_kwargs["lc_evicted_to"]` だけが追加される。標準 `add_messages` reducer がメッセージを **id によりその場で更新**する —— メッセージリストの書き換えなし、`DeltaChannel` 依存なし、state 側からのプレフィックスキャッシュ無効化なし。ファイルはタグより先に書かれるので、書き込み失敗が宙ぶらりんのポインタを残すことはない。
- **モデルビュー**（`wrap_model_call` / `awrap_model_call`）：`lc_evicted_to` を持つすべての `HumanMessage` は、**リクエスト内でのみ**（`request.override(messages=...)`）state テキストから構築されたプレビューへ置き換えられる：退避パス、head/tail 各 5 行、`read_file` 回復通知。非テキストブロック（画像／音声／動画）はそのまま保持される（`_build_evicted_content`）—— メディアがテキストファイルへ退避されることはない。
- **自己修復**：退避ファイルが欠けている場合（セッションディレクトリの削除、ディスク障害）、`_heal_eviction_file` が次のモデル呼び出しで state テキストから書き直す —— ただし対象がそのセッション自身の `evicted/` ディレクトリにある場合に限る。タグ内の外部パスは警告とともに拒否される。

### ツール結果と逆の三状態分割になる理由

| 場所 | ツール結果（P0-2） | 人間メッセージ（P1-9） |
|---|---|---|
| グラフ state / checkpointer | プレビューのみ | **全文** + `lc_evicted_to` タグ |
| MesMemory（`messages` テーブル） | 全文 | **全文**（タグは永続化をフィルタしない） |
| 次のモデル呼び出し（リクエストビューのみ） | プレビュー | プレビュー（パス + `read_file` ヒント） |
| `SESSIONS_DIR/<session_id>/evicted/` | バイト単位で同一のコピー | バイト単位で同一のコピー |

非対称性はメッセージが**いつ**永続化されるかに由来する。ツール結果はツール返却時に内側の永続化層が書き込むので、state は安全にプレビューだけを持てる。人間メッセージはターン最初の `after_model` 境界で永続化される —— state がプレビューしか持たなければ、MesMemory はプレビューをアーカイブし、`message_search` も圧縮も本当のテキストを失う。だから state は全文を保ち、リクエストビューだけが切り詰められる。メッセージ id は決して変わらないため、ウォーターマークは無傷で、二行目が書かれることもない。

## 🖼️ メディア・ガバナンス（オフロード、参照、トークン分類）

圧縮とトークン推定はどちらもマルチモーダル・ペイロードを扱わなければならず、どちらも base64 をテキストとして扱ってはなりません。

**圧縮時オフロード。** `_apply_compression` が要約されるプレフィックス（`current_messages[:cutoff]`）を直列化する前に、`offload_inline_media`（`agent/middlewares/summarization/media_offload.py`）がその範囲だけのすべてのインライン・メディアブロックを書き換えます：

- `data:` URL / base64 ペイロード（`image_url` / `audio_url` / `video_url`、裸の `base64` フィールド、`audio_bytes` / `video_bytes`、または Anthropic 形式の `source.data`）をデコードし、`SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}` に書き込みます；拡張子はマジックバイトから `media_handlers._infer_extension` で導出します；
- 同一バイトは一度だけ書き込みます —— 内容ハッシュのファイル名が単一回および複数回の圧縮をまたいで重複を排除します；
- ブロックはテキストポインタ `[evicted to: <path>]` になります。P0-2 / P1-9 の退避経路が出すのと同じマーカーなので、`_collect_evicted_refs` が拾い、`SummaryDoc.evicted_refs` がパスを要約チェーンに運びます（*Evicted References* として描画）；
- デコードまたは書き込みに失敗したブロックは `<media error="failed_to_offload" />` になります —— fail-open、決してクラッシュしません。

保持ウィンドウはメディアをそのまま保ちます。両方の圧縮経路がプレフィックススライスにオフロードを呼びます —— `_apply_compression_under_lock`（同期）と `_aapply_compression_under_lock`（非同期）。要約プロンプトはメディア参照ルールを運びます：ポインタを逐語で保持し、視覚/音声/動画の詳細を捏造せず、パスからペイロードを取得します。メディアファイルはセッションツリー内にあるため、`clear_session()` が `evicted/` と `plans/` とともに削除します。

**トークン分類。** `pub/func/estimate_tokens.py` は content **リスト**をブロック単位で数えます：テキストブロックはテキスト、メディアブロックは `TOKEN_ESTIMATION` の型ごとの固定コスト（`tokens_per_image_block = 85` —— `langchain_core.count_tokens_approximately` と整合；`tokens_per_audio_block = 256`；`tokens_per_video_block = 1024`；未知ブロックは `tokens_per_unknown_block = 85`、`data:` ペイロードを隠したブロックも含む）。base64 は決して直列化されず、決してテキストとして数えられません：削除された JSON 経路は 5 MB の base64 を約 125 万トークンと読み、画像 1 枚で圧縮を発火させていました。`str` content、`None`、純テキストのリストはテキスト推定を保ちます。

## ⚡ オーバーフロー・テールクリップ（P1-2）

最新のツール出力は通常、最大のコンテキスト消費者であり、最も犠牲にできる —— しかもそのすべてがすでに永続化済みである。`pub/func/message/overflow_clip.py::clip_overflow_tail` はこれを**あらゆるオーバーフロー経路の最初の、ゼロ LLM の一手**に変える：

- **ルートより先。** `Summarization._dispatch_overflow_route`（T1/T2/T3）は非 `fits` ルートごとに実行前にクリップを走らせ、`_forced_recovery_request`（T4/T5）は強制圧縮ステップの前に走らせる。クリップ**単独**で推定値が線の下に落ちれば、リクエストはスタブ済みリストのまま返り、ルートは一切実行されない —— 予算切り詰めも補助 LLM 呼び出しもない。
- **クリップの仕方。** 末尾から**連続する `ToolMessage` バッチ**を走査し（最初の非 `ToolMessage` か既スタブで停止）、各内容を `model_copy` でコンパクトなスタブに置き換える。何も削除・並べ替え・注入されない：`id`、`tool_call_id`、`name`、`additional_kwargs` がすべて生存し、ツール呼び出し／結果ペアリングと永続ウォーターマークは無傷のまま。
- **マーカーは生き残る。** スタブは P0-2 の `[evicted to: …]` ポインタ（+ `read_file` ヒント）と P2-4 スライス通知をそのまま再掲するので、クリップ後も回復経路が機能する。
- **冪等。** スタブはスキャン対象バッチを終わらせるため、二度目の走査は no-op —— T4/T5 の再試行予算が同一クリップで焼かれることはなく、次の試行は圧縮へ劣化する。
- **有界。** `overflow_clip_max_remove`（10）が一回のクリップでスタブ化できるメッセージ数の上限；`overflow_clip_min_keep`（5）が短いトランスクリプトでのクリップを無効化；`overflow_clip_enabled` がマスタースイッチ。

### ゼロ LLM 高速パス vs. 劣化パス

| 状況 | 実行されるもの | LLM 呼び出し |
|---|---|---|
| クリップ単独で推定値が閾値未満に落ちる | スタブ済みリストを返す；ルートは実行されない | **0** |
| クリップ不足（または無効 / 対象バッチなし） | クリップ結果を破棄；既存ルートが**元のリストそのまま**で実行 | ルート依存（compact 系は補助 LLM を呼ぶ） |
| T4/T5 プロバイダエラー、最初の回復試行 | まずクリップ；十分ならスタブ済みリストでプロバイダ呼び出しを再試行 | **0** |
| T4/T5 プロバイダエラー、クリップ不足 | 強制圧縮 + 予算切り詰め、その後再試行 | compact ステップごとに補助 LLM 1 回（≤ `MAX_OVERFLOW_RETRIES = 3`） |

受け入れは厳格：クリップ**単独**が純ローカル推定値を線の下に下げるときのみ適用される（`estimate_messages_tokens(messages, reported_tokens=0)` —— 古い `usage_metadata` が回復を駆動することは決してない）。不足クリップは破棄され、既存ルートは元のリストのまま動きます。

## 🧵 チェーン要約のフィルタリング

圧縮は履歴を `lc_source="summarization"` タグ付きの `HumanMessage` / `AIMessage` ペア（AI 側が `<summary>` タグ内に要約を運ぶ）に置き換える。次の圧縮でその古い要約をシリアライズ済み `<conversation>` に戻せば、トークンの浪費と要約器の混乱を招く。

`agent/middlewares/summarization/core.py` は二段階で処理する：

1. `_extract_previous_summary` がトランスクリプトから前回要約テキストを取り出す（最新のタグ付き `AIMessage`、次にタグ付き `HumanMessage` を探す）。
2. `_filter_summary_messages` が、新プロンプトへシリアライズされるリストから `lc_source="summarization"` の**全**メッセージを除去する。`_build_summary_prompt` は抽出テキストを `<prior-summary>` として別途注入し、更新指示と並べる；モデルは統合要約を一つ作ること、前回要約はこの後破棄されることを告げられる。

要約ペア自体も MesMemory には決して入らない（[境界ごとの永続化](#-境界ごとの永続化)参照）—— 生の履歴は生のまま。

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
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` ウォーターマークストア |
| `tests/full/test_context_governance_e2e.py` | ライブネットワーク e2e（実 LLM + 実グラフ）：六機構の端から端まで —— 明示的に実行 |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
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
