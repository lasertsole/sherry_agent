# 🖼️ Media Pipeline: Compression, Native Reads, History Budget, Artifacts & Generation

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent handles images and PDFs end to end: every upload is compressed by one deterministic cascade before it reaches a provider, `read_file` can show the model an image or a PDF instead of a skill detour, history media is kept newest-first inside a byte budget instead of being stripped after every turn, persisted uploads live in a SHA-256 content-addressed artifact store that deduplicates identical bytes and serves them through `sherry-artifact://` references, and `image_generation` turns a prompt into generated images stored in that same artifact store.

Source of truth: `pub/func/media/image_compress.py`, `agent/middlewares/media_pipeline/core.py`, `agent/middlewares/media_pipeline/budget_projection.py`, `agent/middlewares/media_pipeline/media_handlers.py`, `agent/middlewares/media_pipeline/tool_media.py`, `agent/middlewares/media_pipeline/scrub.py`, `agent/tools/file_tools/read_file.py`, `agent/tools/pub_base/pdf_render.py`, `agent/tools/pub_base/artifact_store.py`, `agent/tools/image_gen.py`, `models/TTI_model/core.py`, `agent/middlewares/summarization/media_offload.py`, `server/trigger/http/artifacts.py`, `client/app/composables/use-chat-media.ts`, `config/features/agent_side/media_pipeline.py`, `config/features/agent_side/image_gen.py`. Every constant in this document was checked against that code.

## Table of Contents

- [Overview](#-overview)
- [Compression: `prepare_for_model`](#-compression-prepare_for_model)
  - [The gate](#-the-gate)
  - [The cascade](#-the-cascade)
  - [Entry points](#-entry-points)
- [`read_file`: Native Images and PDFs](#-read_file-native-images-and-pdfs)
  - [Images](#-images)
  - [PDFs](#-pdfs)
- [History Media Budget](#-history-media-budget)
  - [Why not strip everything](#-why-not-strip-everything)
  - [Order and placeholders](#-order-and-placeholders)
- [Content-Addressed Artifacts](#-content-addressed-artifacts)
  - [Store layout and dedupe](#-store-layout-and-dedupe)
  - [Serving and rendering](#-serving-and-rendering)
  - [Compression-time reuse](#-compression-time-reuse)
- [Image Generation](#-image-generation)
- [Chunked Upload: `/upload/chunk`](#-chunked-upload-uploadchunk)
- [Configuration Reference](#-configuration-reference)
- [Testing](#-testing)
- [Limitations](#-limitations)

## 🎯 Overview

Media enters the agent through four doors: a user upload (WebSocket / Tauri), a tool-produced screenshot, a browser-mode HTTP upload, and a file the model asks `read_file` to open. All four share the same compression and capability machinery. The design goal is parity with ZCode: compress with a per-image gate, let a vision-capable model see the pixels, keep history media while it fits a byte budget, and never hand the provider a payload the model cannot read.

Three properties are load-bearing:

- The compression gate is a SENDING decision. It is not a context-window estimator; provider-reported `usage_metadata.input_tokens` remains the authority for compaction.
- Capability gating is separate from budgeting. The scrub layer answers "can this model read this family?"; the budget layer answers "how much history media still fits?".
- Persisted bytes are content-addressed. Identical uploads map to one file, and a reference survives restarts, so a later compression pass never writes a second copy.

## 🗜️ Compression: `prepare_for_model`

`pub/func/media/image_compress.py::prepare_for_model` ports ZCode's Jimp cascade to PIL. It returns a `CompressedImage` carrying the bytes, the media type, the extension, the dimensions and the chosen strategy label.

### 🚦 The gate

A candidate is accepted only when all three conditions hold:

- the longest edge is at most `image_compress_max_dimension` (default `2000` pixels);
- the raw byte length is at most `max_base64_bytes * 3 / 4` (5 MiB of base64 → 3.75 MiB raw);
- the base64 token estimate `ceil(ceil(raw_len / 3) * 4 * 0.125)` is at most `image_compress_max_tokens` (default `25000`).

The `0.125` ratio is ZCode's `Cde`. It is deliberately conservative: it overestimates every known provider formula, so a candidate that passes the gate is smaller than the provider would require. The gate is per image, never cumulative; a session can still overflow through many images, and `ContextLimitGuard` catches that reactively from the provider's own token count.

### 🪜 The cascade

1. **WebP passthrough** when the file already fits; otherwise PIL WebP quality 80 → 60 → 40 → 20, then a JPEG fallback. (ZCode's Jimp cannot write WebP; PIL can.)
2. **Original passthrough** when the file fits and its longest edge is within the dimension cap.
3. **Format-preserving re-encode**: PNG `optimize=True`; JPEG quality 80 → 60 → 40 → 20; GIF kept.
4. **Resize** to the dimension cap with LANCZOS, then repeat the format-preserving and JPEG steps.
5. **Downscale ladder** at 75%, 50% and 25% of the longest edge, retrying the same steps.
6. **Hard JPEG fallback** at quality 20 and sizes 1000, 800, 600, 400, 300 and 200 pixels.
7. If nothing fits, raise `ImageTooLargeError`; the caller keeps its text/skill path.

PNG is never converted to JPEG while a PNG candidate fits, so transparency is preserved on the common path. The strategy label on the result (`original`, `png-optimized`, `jpeg-q60`, `resized`, `hard-jpeg-q20`, …) is what tests and logs report.

### 🔌 Entry points

- `server/service/messages.py::_get_content_list` compresses `image_base64_list` and `image_bytes_list` before building `data:` URLs.
- `agent/middlewares/media_pipeline/tool_media.py::_image_block` compresses a screenshot before embedding it.
- `server/trigger/http/image.py::upload_image` compresses before writing the upload to disk.
- `pub/func/media/image.py::download_and_convert_to_base64` compresses the image-to-text skill's downloaded bytes.

The `image_compress_enabled` switch (default `true`) keeps the old byte-exact passthrough available. An image that cannot fit the budget is omitted on the tool path and reported as `413` on the HTTP upload path; the model-side upload keeps the original and lets the provider reject it rather than losing the attachment silently.

## 📖 `read_file`: Native Images and PDFs

`read_file` no longer treats an image as an unreadable binary resource or a job for the `image_to_text` skill. It also understands PDFs. Both branches return a tool-message CONTENT LIST, never a JSON-wrapped string, because a list is what makes `image_url` / `document` blocks actual multimodal input.

### 🖼️ Images

The branch runs before the text and whole-file logic for `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff` and `.tif`. The file is bounded by `read_image_max_bytes` (20 MiB), compressed with the cascade, written to `TEMP_DIR/read_images/<sha256[:16]><ext>`, and returned through `native_image_content(session_id, text, saved_path)`:

- a vision-capable session gets `[{"type": "text", …}, {"type": "image_url", …}]` — the same mechanism `browser_screenshot` uses;
- a session whose model is known blind gets the text plus a line telling it to use the `image_to_text` skill on the original path;
- an undecodable file reports `read_image_failed`; an oversize file reports `read_image_input_too_large`.

The text names the original path and the compressed dimensions, so the skill fallback still has a readable file. `ContextEviction`'s `read_file` slicer deliberately skips a block-list result: its text is already budgeted, and slicing the string form would embed the whole base64 payload and cut the media block away.

### 📄 PDFs

The PDF branch chooses a path from the serving model's declared capabilities (the session override wins over the environment model):

- `pdf` supported and no page range → the raw document as a native block: `{"type": "document"}` for Anthropic, `{"type": "file"}` for OpenAI-compatible providers.
- `pdf` unsupported but `vision` supported → pages render to JPEG images through `agent/tools/pub_base/pdf_render.py`, each compressed and attached as `image_url`.
- `pdf` supported, page range requested, `vision` supported → the same page rendering.
- `pdf` supported, page range requested, no vision → `pdf_pages_images_unsupported`.
- neither family supported → `pdf_not_supported`.

`render_pdf_pages` prefers PyMuPDF when installed and otherwise poppler's `pdftoppm`; neither is a hard dependency. It renders at `pdf_render_dpi` (100), bounds a request at `pdf_max_pages_per_request` (20) pages and `pdf_render_timeout_s` (120 seconds), and reports `pdf_render_unavailable` when no backend exists or the external renderer fails. `pdf_max_file_bytes` caps the input at 20 MiB.

## 🧮 History Media Budget

The old behaviour stripped every `image_url` block from all messages except the last one during `before_agent`. That made a text turn after an image turn forget the image, and it contradicted the "let the model see pixels" direction. The strip is gone.

### 🧭 Why not strip everything

A request now carries as much history media as fits `history_media_budget_bytes` (75 MiB, ZCode's `Mui`), choosing newest-first. Since uploads are compressed to a few hundred kilobytes, a normal session keeps every image; the budget matters for uncompressed legacy files, disabled compression, or very long image-heavy sessions.

### 🛡️ Order and placeholders

`MediaBudgetProjectionMiddleware` (`agent/middlewares/media_pipeline/budget_projection.py`) applies `_project_media_budget` in `wrap_model_call` (and its async twin); `MultimodalProcessor` keeps the upload / persistence / capability-scrub half and applies `_scrub_request` first. The scrub replaces a family the model cannot read with a text placeholder; the budget middleware then measures every surviving `image_url` / `audio_url` / `video_url` block by its DECODED data-URL size (a remote URL costs zero because the provider fetches it) and builds a `model_copy` request. The checkpoint is never mutated. The middleware is registered in BOTH chains, so a subagent reading many images is budgeted exactly like the main agent.

The latest real user message (`metadata.origin == "user"`) is protected: its media is kept even when it alone exceeds the budget. A transcript without that marker falls back to the newest media-bearing `HumanMessage`. Protected bytes are deducted from the budget first; the remaining images are admitted newest-first by message index. A block that does not fit becomes:

```
[Attached image]
[Media omitted from provider request to keep the request body under the configured media budget.]
```

## 🗃️ Content-Addressed Artifacts

### 📦 Store layout and dedupe

Uploaded images are persisted by `agent/tools/pub_base/artifact_store.py`. The name is `<family>-<sha256[:16]><ext>`, the directory is `SRC_DIR/<session_id>/artifacts/`, and the reference is `sherry-artifact://<session_id>/<name>`:

```
src/<session_id>/artifacts/
└── image-3c1bdb09e4e6b674.png
```

A second write of the same bytes and family is skipped by an existence check under one process lock, so retries and repeated uploads do not grow the store. The session id is part of both the path and the URI: deleting a session removes its artifacts, and one session can never resolve another's name. Writes are atomic; a store failure returns `None` and the caller keeps the legacy path.

### 🌐 Serving and rendering

`GET /artifacts/:session_id/:name` (`server/trigger/http/artifacts.py`) validates both segments, resolves the file inside that session's artifact directory, and answers with the extension's Content-Type. An invalid session or name is `400`; a missing or cross-session name is `404`.

The client resolves a `sherry-artifact://` entry through `/artifacts` in `use-chat-media.ts` (both `resolveImageSrc` and `resolveMediaSrc`) and it is checked BEFORE the filesystem-extension branch, because the name ends in `.png` but is not a path. Markdown images are handled too: `ChatBox.vue` runs the answer through `resolveArtifactRefsInMarkdown` before markdown rendering, so an AI answer can write `![](sherry-artifact://<sid>/<name>)`.

### 🔁 Compression-time reuse

When compression offloads inline media, `media_offload` first calls `find_artifact` with the decoded bytes and the media family. A hit means the upload path already stored the payload, so the block becomes an `[evicted to: <artifact path>]` pointer without a second write. A miss keeps the legacy decode-and-store route under `SESSIONS_DIR/<session_id>/media/`, which also covers non-pipeline media such as audio and video bytes.

## 🎨 Image Generation

`image_generation` is the main-agent tool that turns a text prompt into images. It is registered only when the `TTI_*` family is configured (`TTI_API_NAME` + `TTI_API_KEY`; `TTI_MODEL_PROVIDER` and `TTI_API_BASE` point it at an OpenAI-compatible Images API), and it never reaches a subagent — generation costs money and is the operator's model choice.

The provider half is `models/TTI_model/core.py::TTIModel`. It sends `POST /v1/images/generations` with `model`, `prompt`, `n` and `size`; `quality` is sent only when it applies (a legacy `standard`/`hd` value is dropped for a gpt-image model); `response_format="b64_json"` is forced for the older `dall-e-*` models and omitted for `gpt-image-*` / `chatgpt-image-latest`; the newer-only options (`background`, `output_format`, `output_compression`, `moderation`) pass through only to those newer models, and DALL·E `style` only to the older ones. A URL response is downloaded (50 MiB cap) and treated exactly like `b64_json`.

The image count is clamped twice: `IMAGE_GEN_MAX_OUTPUT` per model (`dall-e-3` = 1, `dall-e-2` and the gpt-image family = 10, unknown = 1) and `IMAGE_GEN["max_n"]` (10) as a hard ceiling. Every returned image is written through `write_artifact(session_id, data, media_type="image", ext=…)`, so identical bytes deduplicate and the per-session URI is stable; the tool answers markdown references exactly like a stored upload:

```
Generated 2 image(s) with gpt-image-1:

![generated image 1](sherry-artifact://<session>/image-<sha256[:16]>.png)

![generated image 2](sherry-artifact://<session>/image-<sha256[:16]>.png)
```

The chat's gallery/lightbox path renders those references: final answers go through `resolveArtifactRefsInMarkdown`, and TOOL RESULTS now render as pictures too — `ChatToolCard` extracts every `sherry-artifact://` reference from a result and shows an `ArtifactImageGallery` thumbnail grid instead of the plain `<pre>` (the collapsed card shows the grid as well, so a generated image cannot hide behind the fold, and clicking opens the shared `useImagePreview` gallery). A collapsed TURN also advertises its hidden pictures: `classifyTurnFold` collects the tool rows' references and `ChatTurnHistoryStatus` peeks up to four thumbnails beside its expand trigger. No generation-specific renderer exists — the branch is keyed on the URI, not the tool name.

The same provider backs the `text_to_image` skill's terminal-run script (`skills/builtin/text_to_image/scripts/core.py`). The terminal tool injects `SHERRY_SESSION_ID` into every child environment (sync, async and background spawns), the script generates through `TTIModel`, stores each image with `write_artifact`, PRINTS every URI to stdout and reports failures as an `[error] …` line; the previous script printed nothing and saved under `STATIC_DIR/images`, so the agent never learned where the image went. The skill's plain URI lines and the tool's markdown references ride the same thumbnail branch. A provider error, a missing key or a failed artifact write comes back as `Error: …` text (tool) or an `[error] …` stdout line (script), never as an exception escaping the tool; the tool's provider call runs in a worker thread so the event loop is not blocked.

Phase 1 is generation only. `inputImageMask` / `inputFidelity` (inpainting) and streaming partial-image events are not wired; the model-capability catalogue (`config/model_capabilities.json`) describes INPUT media, so image-generation model limits live in `models/TTI_model/core.py::IMAGE_GEN_MAX_OUTPUT` rather than being added there as fake input capabilities.

## 📤 Chunked Upload: `/upload/chunk`

Browsers and the Tauri shell send attachments through `bridge/upload-chunked.ts`. A payload of at most `CHUNK_SIZE` (512 KiB) keeps the whole-body `/images|/audio|/video/upload` route. A larger one opens a session with `POST /upload/begin` (session, filename, content type, `total_chunks`, `total_bytes`), posts every chunk as `POST /upload/chunk?upload_id&chunk_index`, and finishes with `POST /upload/complete?upload_id`.

Chunks for ONE file are strictly sequential: the server rejects a skipped index (`code=chunkGap`) and a replayed one (`code=chunkConflict`), mirroring ZCode. Parallelism is between files — `uploadMultipleFiles` runs up to `max_concurrent_files` (8) lanes through `runPool`, each lane keeping its own sequential chunk stream. `GET /upload/status` reports the contiguous received count for `resumeUpload`, and `POST /upload/abort` discards the staging directory.

The server stages chunks under `TEMP_DIR/upload_chunks/<upload_id>/` with a `meta.json`; `begin` validates the per-kind byte cap and the chunk-count/byte-size agreement, then `complete` concatenates, hashes with SHA-256 and writes `<sha256[:16]><ext>` into the same per-kind directory the whole-body route uses. Identical bytes reuse one file and URL, a completed upload removes its staging, and an incomplete one answers `400` while keeping the chunks for a resume. `bridge/upload-cache.ts` hashes the payload and keeps a Dexie `uploadCache` row (`schema v7`, including the live upload id and status) for `read_cache_ttl_ms` (30 s), so a repeated send inside that window does not upload again; caching is fail-open.

## ⚙️ Configuration Reference

| Setting | Default | Meaning |
|---|---|---|
| `image_compress_enabled` | `true` | Run the compression cascade; off passes original bytes. |
| `image_compress_max_dimension` | `2000` | Longest-edge cap in pixels. |
| `image_compress_max_tokens` | `25000` | Per-image base64 token gate. |
| `image_compress_token_ratio` | `0.125` | Base64 characters per estimated token. |
| `read_image_max_bytes` | `20971520` | `read_file` image input cap (20 MiB). |
| `pdf_max_file_bytes` | `20971520` | `read_file` PDF input cap (20 MiB). |
| `pdf_max_pages_per_request` | `20` | Page-render limit per call. |
| `pdf_render_dpi` | `100` | Rendered page resolution. |
| `pdf_render_timeout_s` | `120.0` | External renderer timeout. |
| `history_media_budget_bytes` | `78643200` | Retained history image budget (75 MiB). |
| `preserve_latest_user_media` | `true` | Protect the latest real user message's media. |
| `artifact_store_enabled` | `true` | Use the content-addressed store; off keeps timestamped `media/` copies. |
| `chunk_size_bytes` | `524288` | Maximum bytes in one `/upload/chunk` body. |
| `max_concurrent_files` | `8` | Parallel file lanes for one multi-attachment send. |
| `read_cache_ttl_ms` | `30000` | Client dedupe window for an uploaded URL. |
| `IMAGE_GEN["max_n"]` | `10` | Hard ceiling on one generation request (per-model caps apply first). |
| `IMAGE_GEN["default_size"]` | `"1024x1024"` | Size when the caller omits one. |
| `IMAGE_GEN["default_quality"]` | `""` | Quality when the caller omits one (empty = provider default). |
| `IMAGE_GEN["timeout_s"]` | `120.0` | Provider + image-download timeout. |

## 🧪 Testing

- `tests/pub/func/media/test_image_compress.py` — passthrough, quality ladder, dimension cap, token gate, WebP, `too_large`.
- `tests/agent/tools/file_tools/test_read_file_image.py` and `test_read_file_pdf.py` — native blocks, skill fallback, caps, page-range errors.
- `tests/agent/tools/pub_base/test_pdf_render.py` — PyMuPDF path, `pdftoppm` fallback, timeout and missing-renderer errors.
- `tests/agent/middlewares/media_pipeline/test_budget_projection.py` — fit, newest-first eviction, protected latest user, remote URLs, all-dropped, both chains.
- `tests/agent/tools/pub_base/test_artifact_store.py` and `tests/server/trigger/http/test_artifacts.py` — dedupe, concurrency, URI parsing, serving, session scope.
- `tests/agent/middlewares/test_compression_media_offload.py` — artifact reuse versus the legacy offload path.
- `tests/agent/middlewares/test_media_pipeline_e2e.py` — real agent graph: server compression → artifact store + `/artifacts` → `read_file` image/PDF blocks → history budget projection.
- `tests/server/trigger/http/test_upload_chunked.py` and the client `upload-chunked` / `upload-cache` suites — staging, assembly, content-hash dedupe, caps and the short-TTL cache.
- `tests/models/TTI_model/test_core.py` and `tests/agent/tools/test_image_gen.py` — request shape, per-model caps, the `response_format` guard, URL download, artifact dedupe, error text and the registration gate.
- `client/app/composables/__tests__/use-chat-media.test.ts` — URI resolution in attachments and markdown references.

## ⚠️ Limitations

- The page-rendering fallback needs PyMuPDF or poppler. Neither is bundled; a deployment without one reports `pdf_render_unavailable` and relies on native PDF support. The project did not add PyMuPDF to `pyproject.toml` because its licence (AGPL) is a distribution decision, not an implementation detail.
- Video is capped per payload (`video_max_bytes`, 30 MiB) but is not compressed and has no cumulative history bucket. ZCode behaves the same way.
- The 75 MiB budget is a request-size heuristic, not a token predictor: many compressed images can still be expensive in provider tokens, and compaction remains the reactive backstop.
- An artifact URI written before `artifact_store_enabled` was turned off remains readable; new uploads fall back to timestamped `media/` files and the `/media` endpoint.
- Image generation depends on an operator-configured provider: without `TTI_API_NAME` and `TTI_API_KEY` the tool is absent and the model cannot draw. Phase 1 has no editing/inpainting and no streaming previews.
