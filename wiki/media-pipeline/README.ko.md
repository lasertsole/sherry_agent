# 🖼️ 미디어 파이프라인: 압축, 네이티브 읽기, 히스토리 예산, Artifact, 이미지 생성

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 이미지와 PDF를 끝에서 끝까지 다루는 방식: 모든 업로드는 provider에 도달하기 전에 하나의 결정적 압축 캐스케이드를 거치고, `read_file`은 skill 우회 없이 모델에게 이미지나 PDF를 그대로 보여줄 수 있으며, 히스토리 미디어는 매 턴 벗겨지는 대신 바이트 예산 안에서 최신순으로 유지되고, 영속화된 업로드는 SHA-256 내용 주소 기반 artifact 저장소에 들어가 동일 바이트를 중복 제거하고 `sherry-artifact://` 참조로 제공됩니다. `image_generation`은 프롬프트에서 이미지를 생성해 같은 artifact 저장소에 보관합니다.

사실상의 기준(source of truth): `pub/func/media/image_compress.py`, `agent/middlewares/media_pipeline/core.py`, `agent/middlewares/media_pipeline/budget_projection.py`, `agent/middlewares/media_pipeline/media_handlers.py`, `agent/middlewares/media_pipeline/tool_media.py`, `agent/middlewares/media_pipeline/scrub.py`, `agent/tools/file_tools/read_file.py`, `agent/tools/pub_base/pdf_render.py`, `agent/tools/pub_base/artifact_store.py`, `agent/tools/image_gen.py`, `models/TTI_model/core.py`, `agent/middlewares/summarization/media_offload.py`, `server/trigger/http/artifacts.py`, `client/app/composables/use-chat-media.ts`, `config/features/agent_side/media_pipeline.py`, `config/features/agent_side/image_gen.py`. 이 문서의 모든 상수는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [압축: `prepare_for_model`](#-압축-prepare_for_model)
  - [게이트](#-게이트)
  - [캐스케이드](#-캐스케이드)
  - [연결 지점](#-연결-지점)
- [`read_file`: 네이티브 이미지와 PDF](#-read_file-네이티브-이미지와-pdf)
  - [이미지](#-이미지)
  - [PDF](#-pdf)
- [히스토리 미디어 예산](#-히스토리-미디어-예산)
  - [모두 벗기지 않는 이유](#-모두-벗기지-않는-이유)
  - [순서와 플레이스홀더](#-순서와-플레이스홀더)
- [내용 주소 기반 Artifact](#-내용-주소-기반-artifact)
  - [저장 레이아웃과 중복 제거](#-저장-레이아웃과-중복-제거)
  - [제공과 렌더링](#-제공과-렌더링)
  - [압축 시점 재사용](#-압축-시점-재사용)
- [이미지 생성](#-이미지-생성)
- [청크 업로드: `/upload/chunk`](#-청크-업로드-uploadchunk)
- [설정 참고](#-설정-참고)
- [테스트](#-테스트)
- [한계](#-한계)

## 🎯 개요

미디어는 네 개의 문으로 에이전트에 들어옵니다: 사용자 업로드(WebSocket / Tauri), 도구가 만든 스크린샷, 브라우저 모드의 HTTP 업로드, 그리고 모델이 `read_file`로 여는 파일입니다. 네 경로 모두 같은 압축과 능력 판정 체계를 공유합니다. 설계 목표는 ZCode와의 정합입니다: 이미지별 게이트로 압축하고, 시각 지원 모델에는 픽셀을 그대로 보여주며, 히스토리 미디어는 바이트 예산에 들어가는 동안 유지하고, 모델이 읽을 수 없는 내용을 provider에 넘기지 않는 것입니다.

세 가지 성질이 핵심입니다:

- 압축 게이트는 전송 판단이지 컨텍스트 윈도우 추정기가 아닙니다. 압축 시점의 권위는 여전히 provider가 보고하는 `usage_metadata.input_tokens`입니다.
- 능력 판정과 예산 판정은 별개입니다. 스크럽 계층은 "이 모델이 이 계열을 읽을 수 있는가"에 답하고, 예산 계층은 "히스토리 미디어가 얼마나 더 들어가는가"에 답합니다.
- 영속화된 바이트는 내용 주소 기반입니다. 동일 업로드는 하나의 파일로 매핑되고 참조는 재시작을 넘어 살아남으므로, 이후 압축 과정이 두 번째 사본을 쓰지 않습니다.

## 🗜️ 압축: `prepare_for_model`

`pub/func/media/image_compress.py::prepare_for_model`은 ZCode의 Jimp 캐스케이드를 PIL로 옮긴 것입니다. 바이트, 미디어 타입, 확장자, 크기, 선택된 전략 라벨을 담은 `CompressedImage`를 반환합니다.

### 🚦 게이트

후보는 다음 세 조건을 모두 만족할 때만 채택됩니다:

- 가장 긴 변이 `image_compress_max_dimension`(기본 `2000` 픽셀) 이하;
- 원시 바이트 길이가 `max_base64_bytes * 3 / 4`(5 MiB base64 → 3.75 MiB 원시) 이하;
- base64 토큰 추정 `ceil(ceil(raw_len / 3) * 4 * 0.125)`가 `image_compress_max_tokens`(기본 `25000`) 이하.

`0.125` 비율은 ZCode의 `Cde`입니다. 의도적으로 보수적이어서 알려진 모든 provider 공식을 과대평가하므로, 게이트를 통과한 후보는 provider가 실제로 요구하는 것보다 작습니다. 게이트는 이미지별이며 누적되지 않습니다. 이미지가 많은 세션은 여전히 넘칠 수 있고, `ContextLimitGuard`가 provider가 보고한 토큰 수로 사후에 잡아냅니다.

### 🪜 캐스케이드

1. **WebP 통과** (이미 들어가면); 아니면 PIL WebP quality 80 → 60 → 40 → 20, 마지막으로 JPEG 폴백. (ZCode의 Jimp는 WebP를 쓸 수 없지만 PIL은 가능합니다.)
2. **원본 통과** (들이가고 가장 긴 변도 크기 상한 이내).
3. **형식 보존 재인코딩**: PNG `optimize=True`, JPEG quality 80 → 60 → 40 → 20, GIF 유지.
4. **리사이즈** (LANCZOS)로 크기 상한에 맞춘 뒤 형식 보존과 JPEG 단계 재시도.
5. **다운스케일 단계**: 가장 긴 변의 75%, 50%, 25%에서 같은 후보 재시도.
6. **최종 JPEG 폴백**: quality 20, 크기 1000, 800, 600, 400, 300, 200 픽셀.
7. 전부 실패하면 `ImageTooLargeError`를 던지고 호출자는 텍스트 / skill 경로를 유지합니다.

PNG 후보가 들어가는 한 JPEG로 바꾸지 않으므로 일반 경로에서 투명도가 보존됩니다. 결과의 전략 라벨(`original`, `png-optimized`, `jpeg-q60`, `resized`, `hard-jpeg-q20` 등)은 테스트와 로그에 나타납니다.

### 🔌 연결 지점

- `server/service/messages.py::_get_content_list`는 `data:` URL을 만들기 전에 `image_base64_list`와 `image_bytes_list`를 압축합니다.
- `agent/middlewares/media_pipeline/tool_media.py::_image_block`은 스크린샷을 삽입하기 전에 압축합니다.
- `server/trigger/http/image.py::upload_image`는 HTTP 업로드를 디스크에 쓰기 전에 압축합니다.
- `pub/func/media/image.py::download_and_convert_to_base64`는 image-to-text skill이 내려받은 바이트를 압축합니다.

`image_compress_enabled`(기본 `true`)를 끄면 예전의 바이트 그대로 통과가 가능합니다. 예산에 못 들어가는 이미지는 도구 경로에서 생략되고 HTTP 업로드 경로에서 `413`을 반환합니다. 사용자 업로드 경로는 원본을 유지하고 첨부를 조용히 잃는 대신 provider의 거부에 맡깁니다.

## 📖 `read_file`: 네이티브 이미지와 PDF

`read_file`은 더 이상 이미지를 읽을 수 없는 바이너리 자원으로 취급하거나 `image_to_text` skill에 고정하지 않습니다. PDF도 이해합니다. 두 분기 모두 도구 메시지의 CONTENT 리스트를 반환하며 JSON으로 감싼 문자열을 반환하지 않습니다. 리스트만이 `image_url` / `document` 블록을 실제 멀티모달 입력으로 만들기 때문입니다.

### 🖼️ 이미지

이 분기는 텍스트와 전체 파일 로직보다 먼저 실행되며 `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`, `.tif`를 다룹니다. 파일은 `read_image_max_bytes`(20 MiB)로 제한되고, 캐스케이드로 압축되어 `TEMP_DIR/read_images/<sha256[:16]><ext>`에 쓰인 뒤 `native_image_content(session_id, text, saved_path)`를 통해 반환됩니다:

- 시각 지원 세션은 `[{"type": "text", …}, {"type": "image_url", …}]`를 받습니다. `browser_screenshot`과 완전히 같은 메커니즘입니다.
- 시각 미지원으로 알려진 세션은 텍스트만 받고 원본 경로에서 `image_to_text` skill을 쓰라는 안내를 함께 받습니다.
- 디코딩할 수 없는 파일은 `read_image_failed`, 상한 초과는 `read_image_input_too_large`를 반환합니다.

텍스트에는 원본 경로와 압축 후 크기가 들어가므로 skill 폴백에도 읽을 수 있는 파일이 있습니다. `ContextEviction`의 `read_file` 슬라이서는 블록 리스트 결과를 의도적으로 건너뜁니다. 텍스트는 이미 도구가 예산화했고, `str(content)`를 자르면 base64 전체를 넣은 뒤 미디어 블록을 잘라내기 때문입니다.

### 📄 PDF

PDF 분기는 제공 모델의 선언된 능력으로 경로를 고릅니다(세션 재정의가 환경 모델보다 우선):

- `pdf` 지원, 페이지 범위 없음 → 원본 문서를 네이티브 블록으로: Anthropic은 `{"type": "document"}`, OpenAI 호환 provider는 `{"type": "file"}`.
- `pdf` 미지원이지만 `vision` 지원 → `agent/tools/pub_base/pdf_render.py`로 JPEG 페이지를 렌더링하고 페이지마다 압축해 `image_url`로 첨부.
- `pdf` 지원, 페이지 범위 요청, `vision` 지원 → 같은 페이지 렌더링.
- `pdf` 지원, 페이지 범위 요청, 시각 미지원 → `pdf_pages_images_unsupported`.
- 두 계열 모두 미지원 → `pdf_not_supported`.

`render_pdf_pages`는 PyMuPDF가 있으면 우선 사용하고 없으면 poppler의 `pdftoppm`을 씁니다. 둘 다 필수 의존성은 아닙니다. `pdf_render_dpi`(100)로 렌더링하고 요청당 `pdf_max_pages_per_request`(20)페이지, `pdf_render_timeout_s`(120초)로 제한하며, 백엔드가 없거나 외부 렌더러가 실패하면 `pdf_render_unavailable`을 보고합니다. `pdf_max_file_bytes`는 입력을 20 MiB로 제한합니다.

## 🧮 히스토리 미디어 예산

예전 동작은 `before_agent`에서 마지막 메시지를 제외한 모든 메시지의 `image_url` 블록을 벗겼습니다. 그래서 이미지 턴 다음의 텍스트 턴은 이미지를 잊었고, "모델에게 픽셀을 보여준다"는 방향과도 모순되었습니다. 그 벗기기는 삭제되었습니다.

### 🧭 모두 벗기지 않는 이유

이제 요청은 `history_media_budget_bytes`(75 MiB, ZCode의 `Mui`)에 들어가는 만큼 히스토리 미디어를 싣고, 최신순으로 고릅니다. 업로드가 수백 KB로 압축되므로 일반 세션은 모든 이미지를 유지합니다. 예산이 의미를 갖는 경우는 압축되지 않은 옛 파일, 압축 비활성화, 또는 매우 긴 이미지 중심 세션입니다.

### 🛡️ 순서와 플레이스홀더

`MediaBudgetProjectionMiddleware`(`agent/middlewares/media_pipeline/budget_projection.py`)가 `wrap_model_call`(및 비동기 쌍둥이)에서 `_project_media_budget`을 적용합니다. `MultimodalProcessor`는 업로드 / 영속화 / 능력 스크럽 절반을 유지하고 `_scrub_request`를 먼저 적용합니다. 스크럽 계층은 모델이 읽을 수 없는 계열을 텍스트 플레이스홀더로 바꾸고, 예산 미들웨어는 남은 각 `image_url` / `audio_url` / `video_url` 블록을 디코딩된 data URL 바이트 수로 측정합니다(원격 URL은 provider가 가져오므로 0바이트). 그런 다음 `model_copy` 요청 사본을 만듭니다. 체크포인트는 절대 변경되지 않습니다. 이 미들웨어는 main과 child 양쪽 체인에 등록되므로 이미지를 많이 읽는 서브에이전트도 메인 에이전트와 같은 예산을 씁니다.

최신 실제 사용자 메시지(`metadata.origin == "user"`)는 보호되어 혼자서 예산을 넘어도 미디어가 유지됩니다. 그 표시가 없는 기록은 최신 미디어 보유 `HumanMessage`로 폴백합니다. 보호 바이트를 먼저 예산에서 빼고, 나머지를 메시지 위치 기준 최신순으로 받아들입니다. 들어가지 않는 블록은 다음 텍스트가 됩니다:

```
[Attached image]
[Media omitted from provider request to keep the request body under the configured media budget.]
```

## 🗃️ 내용 주소 기반 Artifact

### 📦 저장 레이아웃과 중복 제거

업로드 이미지는 `agent/tools/pub_base/artifact_store.py`가 영속화합니다. 이름은 `<family>-<sha256[:16]><ext>`, 디렉터리는 `SRC_DIR/<session_id>/artifacts/`, 참조는 `sherry-artifact://<session_id>/<name>`입니다:

```
src/<session_id>/artifacts/
└── image-3c1bdb09e4e6b674.png
```

같은 바이트와 계열의 두 번째 쓰기는 프로세스 잠금 아래 존재 확인으로 생략되므로 재시도와 중복 업로드가 저장소를 키우지 않습니다. session id는 경로와 URI 양쪽에 들어갑니다. 세션을 삭제하면 그 artifact도 사라지고, 한 세션이 다른 세션의 이름을 해석할 수 없습니다. 쓰기는 원자적이며 저장소 실패 시 `None`을 반환하고 호출자는 기존 경로를 유지합니다.

### 🌐 제공과 렌더링

`GET /artifacts/:session_id/:name`(`server/trigger/http/artifacts.py`)은 두 세그먼트를 검증하고, 해당 세션의 artifact 디렉터리 안에서 파일을 해석하며 확장자에 맞는 Content-Type으로 응답합니다. 잘못된 session이나 이름은 `400`, 없거나 다른 세션의 이름은 `404`입니다.

클라이언트는 `use-chat-media.ts`에서 `sherry-artifact://`를 `/artifacts`로 해석합니다(`resolveImageSrc`와 `resolveMediaSrc` 모두). 이름이 `.png`로 끝나도 경로가 아니므로 이 분기는 파일 시스템 확장자 판정보다 앞에 있습니다. Markdown 이미지도 같습니다. `ChatBox.vue`가 Markdown 렌더링 전에 `resolveArtifactRefsInMarkdown`을 적용하므로 AI 답변이 `![](sherry-artifact://<sid>/<name>)`를 쓸 수 있습니다.

### 🔁 압축 시점 재사용

압축이 인라인 미디어를 오프로드할 때 `media_offload`는 먼저 디코딩한 바이트와 미디어 계열로 `find_artifact`를 호출합니다. 히트하면 업로드 경로가 이미 저장했다는 뜻이므로 블록은 두 번째 쓰기 없이 `[evicted to: <artifact path>]` 포인터가 됩니다. 미스하면 기존 디코딩 + 쓰기 경로(`SESSIONS_DIR/<session_id>/media/`)를 유지하며, 오디오와 비디오 바이트 같은 파이프라인 밖 미디어도 포함합니다.

## 🎨 이미지 생성

`image_generation`은 텍스트 프롬프트를 이미지로 바꾸는 메인 에이전트 전용 도구입니다. `TTI_*` 계열이 설정된 경우(`TTI_API_NAME` + `TTI_API_KEY`, `TTI_MODEL_PROVIDER`와 `TTI_API_BASE`로 OpenAI 호환 Images API 지정)에만 등록되며 서브에이전트에는 전달되지 않습니다——생성은 과금되고 어떤 모델을 쓸지는 운영자의 선택이기 때문입니다.

provider 쪽은 `models/TTI_model/core.py::TTIModel`입니다. `POST /v1/images/generations`에 `model`, `prompt`, `n`, `size`를 보냅니다. `quality`는 적용될 때만 보내고(gpt-image 계열에서는 옛 `standard`/`hd` 값을 버림), 오래된 `dall-e-*`에는 `response_format="b64_json"`을 강제하며 `gpt-image-*` / `chatgpt-image-latest`에서는 생략합니다. 새 모델 전용 옵션(`background`, `output_format`, `output_compression`, `moderation`)은 새 모델에만, DALL·E `style`은 옛 모델에만 전달합니다. URL 응답은 다운로드(50 MiB 상한)해 `b64_json`과 똑같이 처리합니다.

이미지 수는 두 번 제한됩니다. 모델별 `IMAGE_GEN_MAX_OUTPUT`(`dall-e-3` = 1, `dall-e-2`와 gpt-image 계열 = 10, 미지의 모델 = 1)과 `IMAGE_GEN["max_n"]`(10) 하드 상한입니다. 반환된 각 이미지는 `write_artifact(session_id, data, media_type="image", ext=…)`로 저장되므로 동일 바이트는 중복 제거되고 세션별 URI는 안정적입니다. 도구는 저장된 업로드와 똑같은 Markdown 참조를 반환합니다:

```
Generated 2 image(s) with gpt-image-1:

![generated image 1](sherry-artifact://<session>/image-<sha256[:16]>.png)

![generated image 2](sherry-artifact://<session>/image-<sha256[:16]>.png)
```

채팅의 기존 `resolveArtifactRefsInMarkdown`과 갤러리 / lightbox 경로가 최종 답변의 참조를 렌더링합니다. **도구 결과**도 같은 경로입니다: `ChatToolCard`가 임의의 도구 결과에서 `sherry-artifact://` 참조를 추출해 `ArtifactImageGallery` 썸네일 그리드를 평문 `<pre>` 대신 표시합니다(접힌 카드에서도 그리드를 보여 주므로 생성 이미지가 접힘 뒤에 숨지 않습니다. 클릭하면 공유 `useImagePreview` 갤러리가 열립니다). 접힌 턴도 숨은 그림을 알립니다: `classifyTurnFold`가 도구 행의 참조를 모으고 `ChatTurnHistoryStatus`가 펼치기 트리거 옆에 최대 4개의 썸네일을 보여 줍니다. 생성 전용 렌더러는 없습니다——분기는 도구 이름이 아니라 URI로 결정됩니다.

같은 provider가 `text_to_image` skill의 terminal 실행 스크립트(`skills/builtin/text_to_image/scripts/core.py`)도 뒷받침합니다. terminal 도구는 모든 자식 프로세스 환경(동기·비동기·백그라운드 spawn)에 `SHERRY_SESSION_ID`를 주입하고, 스크립트는 `TTIModel`로 생성해 `write_artifact`로 저장하며 각 URI를 **stdout에 print**하고 실패 시 `[error] …`를 출력합니다. 예전 스크립트는 아무것도 print하지 않고 `STATIC_DIR/images`에 저장해서 agent가 이미지 위치를 알 수 없었습니다. skill의 순수 URI 줄과 전용 도구의 markdown 참조는 같은 썸네일 분기를 탑니다. provider 오류, 키 누락, artifact 쓰기 실패는 `Error: …` 텍스트(도구) 또는 `[error] …` stdout 줄(스크립트)로 돌아가며 예외가 도구 밖으로 새지 않습니다. 도구 쪽 provider 호출은 워커 스레드에서 실행되어 이벤트 루프를 막지 않습니다.

1단계는 생성만 합니다. `inputImageMask` / `inputFidelity`(inpaint)와 스트리밍 partial image 이벤트는 연결하지 않았습니다. 모델 능력 카탈로그(`config/model_capabilities.json`)는 입력 미디어를 설명하므로 이미지 생성 모델의 상한은 가짜 입력 능력으로 넣지 않고 `models/TTI_model/core.py::IMAGE_GEN_MAX_OUTPUT`에 둡니다.

## 📤 청크 업로드: `/upload/chunk`

브라우저와 Tauri 셸은 `bridge/upload-chunked.ts`를 통해 첨부를 전송합니다. `CHUNK_SIZE`(512 KiB) 이하는 기존 전체 `/images|/audio|/video/upload` 경로를 씁니다. 더 큰 페이로드는 `POST /upload/begin`(session, filename, content type, `total_chunks`, `total_bytes`)으로 세션을 열고 각 청크를 `POST /upload/chunk?upload_id&chunk_index`로 보낸 뒤 `POST /upload/complete?upload_id`로 마칩니다.

한 파일의 청크는 엄격히 순서대로입니다. 서버는 건너뛴 인덱스(`code=chunkGap`)와 재전송(`code=chunkConflict`)을 거부해 ZCode와 같은 제약을 갖습니다. 병렬성은 파일 사이에 있으며 `uploadMultipleFiles`가 `runPool`로 최대 `max_concurrent_files`(8) 레인을 실행하고 각 레인은 순차 청크를 유지합니다. `GET /upload/status`는 연속 수신 청크 수를 반환해 `resumeUpload`가 이어갈 수 있고, `POST /upload/abort`는 스테이징을 삭제합니다.

서버는 `TEMP_DIR/upload_chunks/<upload_id>/`에 `meta.json`과 청크를 둡니다. `begin`은 종류별 바이트 상한과 청크 수/바이트 수 일치를 검증하고, `complete`가 연결·SHA-256·`<sha256[:16]><ext>` 쓰기를 수행합니다. 동일 바이트는 같은 파일과 URL을 재사용하고 완료된 업로드는 스테이징을 삭제하며 미완료는 `400`으로 청크를 남깁니다. `bridge/upload-cache.ts`는 페이로드를 SHA-256하고 Dexie `uploadCache`(schema v7, 진행 중 upload id/status 포함)에 `read_cache_ttl_ms`(30초) 동안 보관하므로 그 창 안의 재전송은 다시 업로드하지 않습니다. 캐시는 fail-open입니다.

## ⚙️ 설정 참고

| 설정 | 기본값 | 의미 |
|---|---|---|
| `image_compress_enabled` | `true` | 압축 캐스케이드 실행. 끄면 원본 전송. |
| `image_compress_max_dimension` | `2000` | 가장 긴 변의 픽셀 상한. |
| `image_compress_max_tokens` | `25000` | 이미지별 base64 토큰 게이트. |
| `image_compress_token_ratio` | `0.125` | 추정 토큰당 base64 문자 수. |
| `read_image_max_bytes` | `20971520` | `read_file` 이미지 입력 상한(20 MiB). |
| `pdf_max_file_bytes` | `20971520` | `read_file` PDF 입력 상한(20 MiB). |
| `pdf_max_pages_per_request` | `20` | 호출당 페이지 렌더링 상한. |
| `pdf_render_dpi` | `100` | 렌더링 페이지 해상도. |
| `pdf_render_timeout_s` | `120.0` | 외부 렌더러 타임아웃. |
| `history_media_budget_bytes` | `78643200` | 유지되는 히스토리 이미지 예산(75 MiB). |
| `preserve_latest_user_media` | `true` | 최신 실제 사용자 메시지의 미디어 보호. |
| `artifact_store_enabled` | `true` | 내용 주소 기반 저장소 사용. 끄면 타임스탬프 `media/` 사본. |
| `chunk_size_bytes` | `524288` | `/upload/chunk` 본문 하나의 최대 바이트. |
| `max_concurrent_files` | `8` | 한 번의 다중 첨부 전송에서 병렬 파일 레인 수. |
| `read_cache_ttl_ms` | `30000` | 업로드된 URL의 클라이언트 중복 제거 창. |
| `IMAGE_GEN["max_n"]` | `10` | 한 번의 생성 요청 절대 상한(모델별 상한이 먼저 적용). |
| `IMAGE_GEN["default_size"]` | `"1024x1024"` | 호출자가 크기를 생략했을 때의 기본값. |
| `IMAGE_GEN["default_quality"]` | `""` | 품질을 생략했을 때의 기본값(빈 값 = provider 기본). |
| `IMAGE_GEN["timeout_s"]` | `120.0` | provider 및 이미지 다운로드 타임아웃. |

## 🧪 테스트

- `tests/pub/func/media/test_image_compress.py` — 통과, 품질 단계, 크기 상한, 토큰 게이트, WebP, `too_large`.
- `tests/agent/tools/file_tools/test_read_file_image.py`와 `test_read_file_pdf.py` — 네이티브 블록, skill 폴백, 상한, 페이지 범위 오류.
- `tests/agent/tools/pub_base/test_pdf_render.py` — PyMuPDF 경로, `pdftoppm` 폴백, 타임아웃, 렌더러 부재 오류.
- `tests/agent/middlewares/media_pipeline/test_budget_projection.py` — 전체 수용, 최신순 퇴출, 최신 사용자 보호, 원격 URL, 전체 폐기, 양쪽 체인.
- `tests/agent/tools/pub_base/test_artifact_store.py`와 `tests/server/trigger/http/test_artifacts.py` — 중복 제거, 동시성, URI 파싱, 제공, 세션 범위.
- `tests/agent/middlewares/test_compression_media_offload.py` — artifact 재사용과 기존 오프로드 경로.
- `tests/agent/middlewares/test_media_pipeline_e2e.py` — 실제 agent 그래프: 서버 압축 → artifact 저장소 + `/artifacts` → `read_file` 이미지/PDF 블록 → 히스토리 예산 투영.
- `tests/server/trigger/http/test_upload_chunked.py`와 클라이언트 `upload-chunked` / `upload-cache` 스위트 — 스테이징, 조립, 내용 해시 중복 제거, 상한, 짧은 TTL 캐시.
- `tests/models/TTI_model/test_core.py`와 `tests/agent/tools/test_image_gen.py` — 요청 형태, 모델별 상한, `response_format` 가드, URL 다운로드, artifact 중복 제거, 오류 텍스트, 등록 게이트.
- `client/app/composables/__tests__/use-chat-media.test.ts` — 첨부와 Markdown 참조의 URI 해석.

## ⚠️ 한계

- 페이지 렌더링 폴백에는 PyMuPDF 또는 poppler가 필요합니다. 둘 다 번들되지 않으므로 없는 배포에서는 `pdf_render_unavailable`을 보고하고 네이티브 PDF 지원에 의존합니다. 프로젝트는 PyMuPDF를 `pyproject.toml`에 추가하지 않았습니다. 라이선스(AGPL)가 구현 세부가 아니라 배포 결정이기 때문입니다.
- 비디오는 페이로드당 상한(`video_max_bytes`, 30 MiB)만 있고 압축도 누적 히스토리 버킷도 없습니다. ZCode도 같습니다.
- 75 MiB 예산은 요청 크기 휴리스틱이지 토큰 예측기가 아닙니다. 압축된 이미지가 많아도 provider 토큰은 비쌀 수 있고, 압축 미들웨어가 사후 안전망입니다.
- `artifact_store_enabled`를 끄기 전에 기록된 artifact URI는 계속 읽을 수 있습니다. 새 업로드는 타임스탬프 `media/` 파일과 `/media` 엔드포인트로 돌아갑니다.
- 이미지 생성은 운영자가 설정한 provider에 의존합니다. `TTI_API_NAME`과 `TTI_API_KEY`가 없으면 도구 자체가 없고 모델은 그릴 수 없습니다. 1단계에는 편집 / inpaint도 스트리밍 미리보기도 없습니다.
