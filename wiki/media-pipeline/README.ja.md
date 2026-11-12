# 🖼️ メディアパイプライン：圧縮、ネイティブ読み込み、履歴予算、Artifact、画像生成

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが画像と PDF をエンドツーエンドで扱う仕組み：すべてのアップロードは provider に届く前に 1 本の決定的な圧縮カスケードを通り、`read_file` は skill 経由ではなく画像や PDF をそのままモデルに見せることができ、履歴メディアは毎ターン剥がすのではなくバイト予算の中で新しい順に保持され、永続化されたアップロードは SHA-256 の内容アドレス型 artifact ストアに置かれ、同一バイトを重複排除して `sherry-artifact://` 参照で配信されます。`image_generation` はプロンプトから画像を生成し、同じ artifact ストアに保存します。

一次情報：`pub/func/media/image_compress.py`、`agent/middlewares/media_pipeline/core.py`、`agent/middlewares/media_pipeline/budget_projection.py`、`agent/middlewares/media_pipeline/media_handlers.py`、`agent/middlewares/media_pipeline/tool_media.py`、`agent/middlewares/media_pipeline/scrub.py`、`agent/tools/file_tools/read_file.py`、`agent/tools/pub_base/pdf_render.py`、`agent/tools/pub_base/artifact_store.py`、`agent/tools/image_gen.py`、`models/TTI_model/core.py`、`agent/middlewares/summarization/media_offload.py`、`server/trigger/http/artifacts.py`、`client/app/composables/use-chat-media.ts`、`config/features/agent_side/media_pipeline.py`、`config/features/agent_side/image_gen.py`。本文書のすべての定数はこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [圧縮：`prepare_for_model`](#-圧縮prepare_for_model)
  - [ゲート](#-ゲート)
  - [カスケード](#-カスケード)
  - [接続点](#-接続点)
- [`read_file`：ネイティブ画像と PDF](#-read_fileネイティブ画像と-pdf)
  - [画像](#-画像)
  - [PDF](#-pdf)
- [履歴メディア予算](#-履歴メディア予算)
  - [すべて剥がさない理由](#-すべて剥がさない理由)
  - [順序とプレースホルダ](#-順序とプレースホルダ)
- [内容アドレス型 Artifact](#-内容アドレス型-artifact)
  - [保存レイアウトと重複排除](#-保存レイアウトと重複排除)
  - [配信と描画](#-配信と描画)
  - [圧縮時の再利用](#-圧縮時の再利用)
- [画像生成](#-画像生成)
- [チャンクアップロード: `/upload/chunk`](#-チャンクアップロード-uploadchunk)
- [設定リファレンス](#-設定リファレンス)
- [テスト](#-テスト)
- [限界](#-限界)

## 🎯 概要

メディアは 4 つの入口からエージェントに入ります：ユーザーのアップロード（WebSocket / Tauri）、ツールが生成したスクリーンショット、ブラウザモードの HTTP アップロード、そしてモデルが `read_file` で開くファイルです。4 つとも同じ圧縮と能力判定の仕組みを共有します。設計目標は ZCode との整合です：画像ごとのゲートで圧縮し、視覚対応モデルにはピクセルをそのまま見せ、履歴メディアはバイト予算に収まる限り保持し、モデルが読めない内容を provider に渡さないことです。

重要な性質は 3 つあります：

- 圧縮ゲートは送信時の判断であり、コンテキストウィンドウの推定器ではありません。圧縮タイミングの権威は provider が報告する `usage_metadata.input_tokens` のままです。
- 能力判定と予算判定は別物です。スクラブ層は「このモデルはこのファミリを読めるか」に答え、予算層は「履歴メディアがあとどれだけ収まるか」に答えます。
- 永続化されたバイトは内容アドレス型です。同一アップロードは 1 ファイルに写像され、参照は再起動を越えて残るため、後続の圧縮処理が 2 つ目のコピーを書くことはありません。

## 🗜️ 圧縮：`prepare_for_model`

`pub/func/media/image_compress.py::prepare_for_model` は ZCode の Jimp カスケードを PIL に移植したものです。バイト、メディア型、拡張子、寸法、選択された戦略ラベルを持つ `CompressedImage` を返します。

### 🚦 ゲート

候補は次の 3 条件をすべて満たす場合だけ採用されます：

- 最長辺が `image_compress_max_dimension`（既定 `2000` ピクセル）以下；
- 生バイト長が `max_base64_bytes * 3 / 4`（5 MiB の base64 → 3.75 MiB の生バイト）以下；
- base64 トークン推定 `ceil(ceil(raw_len / 3) * 4 * 0.125)` が `image_compress_max_tokens`（既定 `25000`）以下。

`0.125` の比率は ZCode の `Cde` です。意図的に保守的で、既知の provider 式をすべて過大評価するため、ゲートを通った候補は provider の実際の要求より小さくなります。ゲートは画像ごとで累積しません。画像が多いセッションは依然として溢れ得ますが、`ContextLimitGuard` が provider 報告のトークン数で事後的に捕捉します。

### 🪜 カスケード

1. **WebP パススルー**（すでに収まる場合）。そうでなければ PIL の WebP quality 80 → 60 → 40 → 20、最後に JPEG フォールバック。（ZCode の Jimp は WebP を書けませんが PIL は書けます。）
2. **原本パススルー**（収まり、最長辺も寸法上限内の場合）。
3. **形式保持の再エンコード**：PNG は `optimize=True`、JPEG は quality 80 → 60 → 40 → 20、GIF は保持。
4. **リサイズ**（LANCZOS）で寸法上限に合わせ、形式保持と JPEG の候補を再試行。
5. **ダウンスケール段階**：最長辺の 75%、50%、25% で同じ候補を再試行。
6. **最終 JPEG フォールバック**：quality 20、サイズ 1000、800、600、400、300、200 ピクセル。
7. すべて失敗したら `ImageTooLargeError` を送出し、呼び出し側はテキスト / skill 経路を維持します。

PNG 候補が収まる限り JPEG に変換しないため、通常経路では透明度が保たれます。結果の戦略ラベル（`original`、`png-optimized`、`jpeg-q60`、`resized`、`hard-jpeg-q20` など）はテストとログに現れます。

### 🔌 接続点

- `server/service/messages.py::_get_content_list` は `data:` URL を組み立てる前に `image_base64_list` と `image_bytes_list` を圧縮します。
- `agent/middlewares/media_pipeline/tool_media.py::_image_block` はスクリーンショットを埋め込む前に圧縮します。
- `server/trigger/http/image.py::upload_image` は HTTP アップロードを書き込む前に圧縮します。
- `pub/func/media/image.py::download_and_convert_to_base64` は image-to-text skill がダウンロードしたバイトを圧縮します。

`image_compress_enabled`（既定 `true`）を切ると以前のバイトそのままのパススルーが使えます。予算に収まらない画像はツール経路では省略され、HTTP アップロード経路では `413` を返します。ユーザーアップロード経路は元画像を保持し、添付を黙って失う代わりに provider の拒否に委ねます。

## 📖 `read_file`：ネイティブ画像と PDF

`read_file` は画像を読めないバイナリ資源として扱わず、`image_to_text` skill に固定もしません。PDF も理解します。どちらの分岐もツールメッセージの CONTENT リストを返し、JSON で包んだ文字列にはしません。リストだけが `image_url` / `document` ブロックを実際のマルチモーダル入力にするからです。

### 🖼️ 画像

この分岐はテキストとファイル全体の処理より前に走り、`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.bmp`、`.tiff`、`.tif` を対象にします。ファイルは `read_image_max_bytes`（20 MiB）で制限され、カスケードで圧縮され、`TEMP_DIR/read_images/<sha256[:16]><ext>` に書かれ、`native_image_content(session_id, text, saved_path)` を通して返されます：

- 視覚対応セッションは `[{"type": "text", …}, {"type": "image_url", …}]` を受け取ります。`browser_screenshot` とまったく同じ仕組みです。
- 視覚非対応と分かっているセッションはテキストだけを受け取り、元のパスで `image_to_text` skill を使うよう案内されます。
- デコードできないファイルは `read_image_failed`、上限超過は `read_image_input_too_large` を返します。

テキストには元のパスと圧縮後の寸法が入るため、skill フォールバックでも読めるファイルがあります。`ContextEviction` の `read_file` スライサーはブロックリスト結果を意図的に素通りさせます。テキストはすでにツール側で予算化されており、`str(content)` を切ると base64 全体を埋め込んだうえでメディアブロックを切り落としてしまうからです。

### 📄 PDF

PDF 分岐は提供モデルの宣言能力で経路を選びます（セッション上書きが環境モデルより優先）：

- `pdf` 対応かつページ範囲なし → 生ドキュメントをネイティブブロックとして送信：Anthropic は `{"type": "document"}`、OpenAI 互換 provider は `{"type": "file"}`。
- `pdf` 非対応だが `vision` 対応 → `agent/tools/pub_base/pdf_render.py` で JPEG ページに描画し、ページごとに圧縮して `image_url` で添付。
- `pdf` 対応、ページ範囲あり、`vision` 対応 → 同じページ描画。
- `pdf` 対応、ページ範囲あり、視覚非対応 → `pdf_pages_images_unsupported`。
- どちらのファミリも非対応 → `pdf_not_supported`。

`render_pdf_pages` は PyMuPDF が入っていればそれを使い、なければ poppler の `pdftoppm` を使います。どちらも必須依存ではありません。`pdf_render_dpi`（100）で描画し、1 回の要求を `pdf_max_pages_per_request`（20）ページ、`pdf_render_timeout_s`（120 秒）に制限し、バックエンドがない場合や外部レンダラが失敗した場合は `pdf_render_unavailable` を報告します。`pdf_max_file_bytes` は入力を 20 MiB に制限します。

## 🧮 履歴メディア予算

以前の動作は `before_agent` で最後の 1 件を除くすべてのメッセージから `image_url` ブロックを剥がしていました。そのため画像のターンの次のテキストターンは画像を忘れ、しかも「モデルにピクセルを見せる」方向と矛盾しました。この剥離は削除されました。

### 🧭 すべて剥がさない理由

現在の要求は `history_media_budget_bytes`（75 MiB、ZCode の `Mui`）に収まる限り履歴メディアを運び、新しい順に選びます。アップロードは数百 KB に圧縮されるため、通常のセッションはすべての画像を保持します。予算が効くのは未圧縮の旧ファイル、圧縮無効、または非常に長い画像中心のセッションです。

### 🛡️ 順序とプレースホルダ

`MediaBudgetProjectionMiddleware`（`agent/middlewares/media_pipeline/budget_projection.py`）が `wrap_model_call`（および非同期版）で `_project_media_budget` を適用します。`MultimodalProcessor` はアップロード / 永続化 / 能力スクラブの半分を保持し、`_scrub_request` を先に適用します。スクラブ層はモデルが読めないファミリをテキストのプレースホルダに置換し、予算ミドルウェアは残った各 `image_url` / `audio_url` / `video_url` ブロックをデコード後の data URL バイト数で計量します（リモート URL は provider が取得するため 0 バイト）。そして `model_copy` の要求コピーを組み立てます。チェックポイントは決して書き換えません。このミドルウェアは main と child の両チェーンに登録されるため、多数の画像を読むサブエージェントもメインエージェントと同じ予算で扱われます。

最新の実ユーザーメッセージ（`metadata.origin == "user"`）は保護され、単独で予算を超えてもメディアが保持されます。そのマーカーがない記録では、最新のメディア付き `HumanMessage` にフォールバックします。保護バイトを先に予算から差し引き、残りをメッセージ位置の新しい順に受け入れます。収まらないブロックは次のテキストになります：

```
[Attached image]
[Media omitted from provider request to keep the request body under the configured media budget.]
```

## 🗃️ 内容アドレス型 Artifact

### 📦 保存レイアウトと重複排除

アップロード画像は `agent/tools/pub_base/artifact_store.py` が永続化します。名前は `<family>-<sha256[:16]><ext>`、ディレクトリは `SRC_DIR/<session_id>/artifacts/`、参照は `sherry-artifact://<session_id>/<name>` です：

```
src/<session_id>/artifacts/
└── image-3c1bdb09e4e6b674.png
```

同じバイトと同じファミリの 2 回目の書き込みは、プロセスロック下の存在確認で省略されるため、再試行や重複アップロードでストアは増えません。session id はパスと URI の両方に含まれます。セッションを削除すればその artifact も消え、あるセッションが別のセッションの名前を解決することはありません。書き込みはアトミックで、ストア失敗時は `None` を返し、呼び出し側は従来のパスを使います。

### 🌐 配信と描画

`GET /artifacts/:session_id/:name`（`server/trigger/http/artifacts.py`）は 2 つのセグメントを検証し、そのセッションの artifact ディレクトリ内でファイルを解決し、拡張子に応じた Content-Type で返します。不正な session または名前は `400`、存在しない名前や別セッションの名前は `404` です。

クライアントは `use-chat-media.ts` で `sherry-artifact://` を `/artifacts` に解決します（`resolveImageSrc` と `resolveMediaSrc` の両方）。名前が `.png` で終わっていてもパスではないため、この分岐はファイルシステム拡張子の判定より前に置かれます。Markdown 画像も同様です。`ChatBox.vue` が Markdown レンダリング前に `resolveArtifactRefsInMarkdown` を通すため、AI の回答は `![](sherry-artifact://<sid>/<name>)` と書けます。

### 🔁 圧縮時の再利用

圧縮がインラインメディアをオフロードするとき、`media_offload` はまずデコードしたバイトとメディアファミリで `find_artifact` を呼びます。ヒットすればアップロード経路がすでに保存済みなので、ブロックは 2 回目の書き込みなしで `[evicted to: <artifact path>]` ポインタになります。ミスした場合は従来のデコード + 書き込み経路（`SESSIONS_DIR/<session_id>/media/`）を維持し、音声や動画バイトなどパイプライン外のメディアもカバーします。

## 🎨 画像生成

`image_generation` はテキストプロンプトから画像を作るメインエージェント専用ツールです。`TTI_*` ファミリが設定済み（`TTI_API_NAME` + `TTI_API_KEY`、`TTI_MODEL_PROVIDER` と `TTI_API_BASE` で OpenAI 互換の Images API を指定）のときだけ登録され、サブエージェントには渡りません——生成は課金され、どのモデルを使うかは運用者の選択だからです。

provider 側は `models/TTI_model/core.py::TTIModel` です。`POST /v1/images/generations` に `model`、`prompt`、`n`、`size` を送ります。`quality` は適用されるときだけ送り（gpt-image 系では旧来の `standard`/`hd` を落とします）、古い `dall-e-*` には `response_format="b64_json"` を強制し、`gpt-image-*` / `chatgpt-image-latest` では省略します。新モデル専用オプション（`background`、`output_format`、`output_compression`、`moderation`）は新モデルにだけ、DALL·E の `style` は旧モデルにだけ渡します。URL 応答はダウンロードし（50 MiB 上限）、`b64_json` と同様に扱います。

画像枚数は二重に制限されます。モデル別の `IMAGE_GEN_MAX_OUTPUT`（`dall-e-3` = 1、`dall-e-2` と gpt-image ファミリ = 10、未知 = 1）と、`IMAGE_GEN["max_n"]`（10）の上限です。返された各画像は `write_artifact(session_id, data, media_type="image", ext=…)` で保存されるため、同一バイトは重複排除され、セッションごとの URI は安定します。ツールは保存済みアップロードと同じ Markdown 参照を返します：

```
Generated 2 image(s) with gpt-image-1:

![generated image 1](sherry-artifact://<session>/image-<sha256[:16]>.png)

![generated image 2](sherry-artifact://<session>/image-<sha256[:16]>.png)
```

チャットの既存の `resolveArtifactRefsInMarkdown` とギャラリー / lightbox 経路がこれらを描画します。生成専用のフロントエンドコードはありません。provider エラー、キー欠落、artifact 書き込み失敗はすべて `Error: …` テキストとして返り、例外がツールの外へ漏れません。provider 呼び出しはワーカースレッドで実行され、イベントループを塞ぎません。

フェーズ 1 は生成のみです。`inputImageMask` / `inputFidelity`（inpaint）とストリーミングの partial image イベントは配線していません。モデル能力カタログ（`config/model_capabilities.json`）は入力メディアを記述するものなので、画像生成モデルの上限は偽の入力能力として追加せず `models/TTI_model/core.py::IMAGE_GEN_MAX_OUTPUT` に置いています。

## 📤 チャンクアップロード: `/upload/chunk`

ブラウザと Tauri シェルは `bridge/upload-chunked.ts` 経由で添付を送ります。`CHUNK_SIZE`（512 KiB）以下は従来の一括 `/images|/audio|/video/upload` を使います。それより大きい場合は `POST /upload/begin`（session、filename、content type、`total_chunks`、`total_bytes`）でセッションを開き、各チャンクを `POST /upload/chunk?upload_id&chunk_index` で送り、最後に `POST /upload/complete?upload_id` を呼びます。

1 ファイル内のチャンクは厳密に順序どおりです。サーバーは欠番（`code=chunkGap`）と再送（`code=chunkConflict`）を拒否し、ZCode と同じ制約になります。並列性はファイル間で、`uploadMultipleFiles` が `runPool` で最大 `max_concurrent_files`（8）レーンを走らせ、各レーン内は順序どおりです。`GET /upload/status` は連続して受信したチャンク数を返し `resumeUpload` が再開でき、`POST /upload/abort` はステージを破棄します。

サーバーは `TEMP_DIR/upload_chunks/<upload_id>/` に `meta.json` とチャンクを置きます。`begin` は種別ごとのバイト上限とチャンク数/バイト数の整合性を検証し、`complete` が連結・SHA-256・`<sha256[:16]><ext>` の書き込みを行います。同一バイトは同じファイルと URL を再利用し、完了したアップロードはステージを削除、未完了は `400` でチャンクを残します。`bridge/upload-cache.ts` はペイロードを SHA-256 し、Dexie `uploadCache`（schema v7、実行中の upload id と status を含む）に `read_cache_ttl_ms`（30 秒）保持するため、その窓内の再送は再アップロードしません。キャッシュはフェイルオープンです。

## ⚙️ 設定リファレンス

| 設定 | 既定値 | 意味 |
|---|---|---|
| `image_compress_enabled` | `true` | 圧縮カスケードを実行。無効なら原本を送信。 |
| `image_compress_max_dimension` | `2000` | 最長辺のピクセル上限。 |
| `image_compress_max_tokens` | `25000` | 画像ごとの base64 トークンゲート。 |
| `image_compress_token_ratio` | `0.125` | 推定トークンあたりの base64 文字数。 |
| `read_image_max_bytes` | `20971520` | `read_file` の画像入力上限（20 MiB）。 |
| `pdf_max_file_bytes` | `20971520` | `read_file` の PDF 入力上限（20 MiB）。 |
| `pdf_max_pages_per_request` | `20` | 1 回の呼び出しで描画するページ上限。 |
| `pdf_render_dpi` | `100` | 描画ページの解像度。 |
| `pdf_render_timeout_s` | `120.0` | 外部レンダラのタイムアウト。 |
| `history_media_budget_bytes` | `78643200` | 保持する履歴画像の予算（75 MiB）。 |
| `preserve_latest_user_media` | `true` | 最新の実ユーザーメッセージのメディアを保護。 |
| `artifact_store_enabled` | `true` | 内容アドレス型ストアを使用。無効ならタイムスタンプ付き `media/` コピー。 |
| `chunk_size_bytes` | `524288` | 1 つの `/upload/chunk` body の最大バイト数。 |
| `max_concurrent_files` | `8` | 1 回の複数添付送信で並行するファイルレーン数。 |
| `read_cache_ttl_ms` | `30000` | アップロード済み URL のクライアント重複排除窓。 |
| `IMAGE_GEN["max_n"]` | `10` | 1 回の生成要求の絶対上限（モデル別上限が先に効く）。 |
| `IMAGE_GEN["default_size"]` | `"1024x1024"` | 呼び出し側がサイズを省略したときの既定値。 |
| `IMAGE_GEN["default_quality"]` | `""` | 品質を省略したときの既定値（空 = provider 既定）。 |
| `IMAGE_GEN["timeout_s"]` | `120.0` | provider と画像ダウンロードのタイムアウト。 |

## 🧪 テスト

- `tests/pub/func/media/test_image_compress.py` — パススルー、品質段階、寸法上限、トークンゲート、WebP、`too_large`。
- `tests/agent/tools/file_tools/test_read_file_image.py` と `test_read_file_pdf.py` — ネイティブブロック、skill フォールバック、上限、ページ範囲エラー。
- `tests/agent/tools/pub_base/test_pdf_render.py` — PyMuPDF 経路、`pdftoppm` フォールバック、タイムアウト、レンダラ不在エラー。
- `tests/agent/middlewares/media_pipeline/test_budget_projection.py` — 全収容、新しい順の淘汰、最新ユーザー保護、リモート URL、全破棄、両チェーン。
- `tests/agent/tools/pub_base/test_artifact_store.py` と `tests/server/trigger/http/test_artifacts.py` — 重複排除、並行性、URI 解析、配信、セッションスコープ。
- `tests/agent/middlewares/test_compression_media_offload.py` — artifact 再利用と従来のオフロード経路。
- `tests/agent/middlewares/test_media_pipeline_e2e.py` — 実際の agent グラフ: サーバー圧縮 → artifact ストア + `/artifacts` → `read_file` の画像/PDF ブロック → 履歴予算の投影。
- `tests/server/trigger/http/test_upload_chunked.py` とクライアントの `upload-chunked` / `upload-cache` スイート — ステージング、組み立て、内容ハッシュ重複排除、上限、短 TTL キャッシュ。
- `tests/models/TTI_model/test_core.py` と `tests/agent/tools/test_image_gen.py` — 要求形状、モデル別上限、`response_format` ガード、URL ダウンロード、artifact 重複排除、エラーテキスト、登録ゲート。
- `client/app/composables/__tests__/use-chat-media.test.ts` — 添付と Markdown 参照の URI 解決。

## ⚠️ 限界

- ページ描画フォールバックには PyMuPDF または poppler が必要です。どちらも同梱されないため、ない配備では `pdf_render_unavailable` を報告し、ネイティブ PDF 対応に依存します。プロジェクトは PyMuPDF を `pyproject.toml` に追加していません。ライセンス（AGPL）は実装の詳細ではなく配布上の判断だからです。
- 動画は 1 ペイロードあたりの上限（`video_max_bytes`、30 MiB）だけで、圧縮も累積履歴バケットもありません。ZCode も同じです。
- 75 MiB の予算は要求サイズのヒューリスティックであり、トークン予測器ではありません。圧縮済み画像が多くても provider トークンは高くつき得るため、圧縮ミドルウェアが事後的な最後の砦です。
- `artifact_store_enabled` を切る前に書かれた artifact URI は引き続き読めます。新しいアップロードはタイムスタンプ付き `media/` ファイルと `/media` エンドポイントに戻ります。
- 画像生成は運用者が設定した provider に依存します。`TTI_API_NAME` と `TTI_API_KEY` がなければツール自体が存在せず、モデルは描けません。フェーズ 1 には編集 / inpaint もストリーミングプレビューもありません。
