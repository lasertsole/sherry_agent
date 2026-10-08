# 🗜️ 退避、メディア、オーバーフロー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Context Governance](../README.ja.md) の一部：境界ごとの永続化の土台、3 つの退避経路、`read_file` スライス、メディア・ガバナンス、オーバーフロー・テールクリップ、チェーン要約のフィルタリング。

---

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

メディアは入口と出口の両側で統治されます：入口側の規則が処理不能なペイロードをリクエストから締め出し、使えないモデルを二度探らないようにし、圧縮とトークン推定は base64 を決してテキストとして扱いません。

**入力サイズ上限。** すべての受信メディア・ペイロード —— インラインの base64 / `data:` ブロックまたはリモート URL のダウンロード —— は、**ディスクへ書き込む前に** `MEDIA_PIPELINE["max_media_bytes"]`（20 MiB）と比較されます。上限を超えたペイロードはスキップされます：書き込みは行われず、パスは `MediaPaths` に入らず、警告がバイト数を記録し、メッセージには添付が保存されずモデルにも送られなかったことを示すモデル可視の `[Uploaded media]` 行が付きます。リモート URL はサーバーが申告した `Content-Length` があればそれで、なければ `limit + 1` バイトに制限した読み取りで判定するため、誤ったヘッダーが過大な書き込みを強制することはありません；画像・音声・動画のハンドラが同じゲートを共有します（`media_handlers.py::_exceeds_media_limit` / `_record_oversize`）。上限ちょうどのペイロードは許可されます。

**リクエストごとのケイパビリティ・スクラブ。** `MultimodalProcessor.wrap_model_call`（auto モード）はリクエストのコピーだけを再構築し —— `request.override(messages=...)` —— サービングモデルが `"unsupported"` とキャッシュされているファミリーのメディアブロックを、ブロック種別・記録済みのディスクパス・対応する組み込みスキル（`image_to_text` / `speech_to_text` / `video_text_to_text`）を記したテキスト・プレースホルダに置き換えます。supported と未プローブのブロックはそのまま通り、混合メッセージは対応ファミリーのネイティブ・メディアを保ち、非対応のものだけがブロック単位で剥がされます。state・checkpointer・MesMemory には決して書き込まれず、置換が不要なときは元のリクエストオブジェクトが返ります。スクラブは `"auto"` のときだけ動きます：`"true"` は全ブロックをモデルに残し、`"false"` はリクエストが組まれる前にスキルパスを取ります。

**サイレント劣化の検出。** モデルはメディアブロックを受け取りながら、見なかったかのように答えることがあります。auto モードのネイティブ試行として始まり成功した呼び出しでは、`LLMRetryMiddleware` が返答を `detect_media_blindness()`（`media_pipeline/degradation.py`）で評価します —— 純正規表現、en / zh / ja / ko、精度優先：盲目の言い回しの ±40 文字以内にメディア語があるときだけヒット —— さらに明示的な「メディアを説明して」要求パターンも対象です。ヒットすると、リクエストに実際に現れたすべてのメディアファミリーが `"unsupported"` としてキャッシュされ、以降のターンはスキルパスへ直行します；ターンごとのネイティブ・フラグはどちらでもクリアされます。`main_llm_silent_degradation_detection`（True）がマスタースイッチです。帰属はサービングモデルに従います：`LLMRetryMiddleware` がリクエストをスティッキーなフォールバック候補に再バインドするとき、まずターンごとのネイティブモデル・キーを `{candidate.provider}/{candidate.model_name}` に書き換えるため、エラー拒否もサイレント拒否も実際にその呼び出しを処理したモデルに対してキャッシュされます。

**ケイパビリティ・キャッシュ。** 三つの入口側挙動は一つのプロセスレベル・キャッシュ（`agent/middlewares/llm_capability_cache.py`）を共有します。キーは `"{provider}/{model_name}"`、ファミリーごとの値は `"auto"`（未テスト）/ `"supported"` / `"unsupported"`。キャッシュはプロセス内にのみ存在します：再起動でクリーンになり（無駄なネイティブ試行は最大 1 回）、モデル切り替え（env 変更 + 再起動）は自然に新しいキーになります。

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
