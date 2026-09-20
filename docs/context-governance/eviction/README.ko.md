# 🗜️ 축출, 미디어, 오버플로

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Context Governance](../README.ko.md)의 일부: 경계마다의 영속화 토대, 세 가지 축출 경로, `read_file` 슬라이스, 미디어 거버넌스, 오버플로 테일 클립, 체인 요약 필터링.

---

## 💾 경계마다의 영속화

`agent/middlewares/message_persistence/core.py`(`MessagePersistenceMiddleware`)는 이 페이지 전체가 기대는 영속화 토대다. **두 시점**에 기록한다:

| 메시지 | 영속화 시점 |
|---|---|
| `ToolMessage` | **도구 핸들러가 반환하는 순간** (`wrap_tool_call` / `awrap_tool_call`) |
| `HumanMessage` | 해당 턴의 첫 모델 경계 (`after_model` / `aafter_model`) |
| `AIMessage` (그 `tool_calls` 포함) | 모델이 생성한 직후의 경계 |
| HITL 거부 (`status="error"` ToolMessage) | 다음 모델 경계 — HITL이 이 미들웨어를 바깥에서 감싸므로 그 단락은 플러시에 도달하지 않는다 |

두 시점은 하나의 배치 파이프라인을 공유한다: 역할/마커 필터 → 워터마크 → HITL 거부 재페어링 + 도구 결과 중복 제거 → 기록 → 마킹. 중요한 세부:

- **`persisted_message_ids` 워터마크(write-once).** 기록 전에 `filter_persisted_message_ids`가 세션의 묘비에 이미 있는 조회 키를 가진 후보를 버리고, 기록 후에 `mark_message_ids_persisted`가 라이터에 전달된 모든 후보에 묘비를 남긴다. 워터마크는 SQLite 테이블(`context_engine/store/db.py`)이므로 재시작을 견딘다.
- **id + 핑거프린트 이중 조회.** 도구 결과는 반환 시점에 영속화되며 **그때는 그래프 reducer가 아직 id를 부여하지 않았다**; 따라서 워터마크 조회는 LangGraph 메시지 id와 `sha1:` 콘텐츠 핑거프린트(role + content + `tool_call_id`)를 모두 확인한다. 다음 경계와 재시작 후 재생 모두 두 키 중 하나로 같은 논리 메시지에 매칭된다(`prepare.py`의 `_watermark_lookup_keys`).
- **요약 쌍은 절대 영속화되지 않는다.** `_is_persistable`은 `additional_kwargs["lc_source"] == "summarization"` 태그가 붙은 모든 메시지(쌍의 AI 절반)를 버리고, `HumanMessageRowBuilder.build`(`context_engine/store/core.py`)는 인간 절반에 `None`을 반환한다. 압축 산출물은 프롬프트 발판이지 대화 이력이 아니다.
- **페일오픈.** `session_id`가 없으면 조용히 건너뛰고, 라이터 오류는 로그만 남기고 묘비를 남기지 않아 같은 배치가 다음 경계에서 재시도된다. 영속화가 턴이나 도구 결과를 깨뜨리는 일은 결코 없다.

이것이 이 페이지의 이후 메커니즘들이 그토록 적극적일 수 있는 이유다: **어떤 후속 단계가 줄이기 전에 모든 페이로드는 이미 영속화되어 있다.**

## 🗜️ 도구 결과 축출 (P0-2)

`ContextEvictionMiddleware.wrap_tool_call`은 도구 응답을 **그래프 state에 들어가기 전에** 가로챈다(`agent/middlewares/context_eviction/core.py`; 원시 함수는 `pub/func/message/eviction.py`):

- 추출 텍스트가 **`evict_threshold_chars`(20 000자)를 초과하는** 일반 결과는 `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt`에 기록되고, 메시지 내용은 파일 경로를 담은 head/tail 프리뷰로 대체된다.
- `excluded_tools`(8개 이름 — `read_file`, `write_file`, `patch_file`, `search_files`, `list_files`, `memory`, `skill_view`, `skill_list`)는 그대로 통과한다: 페이로드가 이미 백엔드 파일시스템에 있거나 회수 비용이 낮다. `read_file`은 추가로 아래의 슬라이스 경로를 탄다.
- 대체는 `ToolMessage.model_copy`로 만들어지므로 메시지 `id`, `tool_call_id`, `name`, `status`, `additional_kwargs`가 모두 살아남는다 — 페어링, 새니타이저, 워터마크가 계속 같은 논리 메시지에 매칭된다.
- 멀티모달 비텍스트 블록(이미지/오디오/비디오)은 원문 그대로 유지되고 텍스트 블록만 교체된다.
- 안전 가드: 안전하지 않은 `session_id`(빈 값 / `.` / `..` / 구분자 포함)는 디스크를 건드리지 않고 건너뛴다; `[evicted to: …]` 마커가 있는 메시지는 다시 축출되지 않는다; 프리뷰가 원문보다 작아지지 않는 경우(한 줄짜리 거대 행)는 건너뛴다.

프리뷰 형식(`pub/func/message/eviction.py`, `preview_head_lines = preview_tail_lines = 5`):

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

**세 개의 보관소** — 축출 후 각 사본이 있는 곳:

| 위치 | 보관 내용 |
|---|---|
| 그래프 state / checkpointer / 다음 모델 호출 | **프리뷰만** |
| MesMemory(`messages` 테이블) | **전문** — 도구 반환 순간 내부 영속화 계층이 기록 |
| `SESSIONS_DIR/<session_id>/evicted/` | 바이트 단위로 동일한 사본; `load_evicted()` / `read_file`로 회수 |

이 분할을 성립시키는 순서: `wrap_tool_call` 체인에서 `MessagePersistenceMiddleware`는 **가장 안쪽**에 있고 `ContextEvictionMiddleware`는 그 **바깥**에 있다. 따라서 원본 결과가 먼저 플러시되고 프리뷰만 state로 넘어간다. 워터마크는 양쪽 모두에서 덮인다: `model_copy`가 내부 플러시의 프로세스 내 `_db_persisted` 마커를 운반하고, 원본 기록이 성공한 경우 대체 메시지의 워터마크 키에 추가 묘비가 남는다(`_cover_with_watermark`) — 마커가 사라지고 프리뷰 핑거프린트가 원본과 더 이상 일치하지 않는 재시작을 이것이 덮는다. 원본 기록이 실패하면 아무 묘비도 남기지 않고 다음 경계가 프리뷰 내용으로 재시도한다.

## ✂️ `read_file` 슬라이스 (P2-4)

`read_file` 결과는 **축출되지 않는다** — 파일이 이미 디스크에 있으니 두 번째 사본을 쓰는 것은 순수한 중복이다. 대신 `slice_read_file_result`(`pub/func/message/eviction.py`)가 내용을 **앞 `_READ_FILE_SLICE_CHARS`(4 000)자**와 복구 안내(`"...[Output was truncated due to eviction threshold. Use read_file with offset and limit to retrieve specific portions.]"`)로 대체한다. 축출 파일은 쓰지 않으며, 헬퍼는 멱등하다: 이미 슬라이스된(안내가 있는) 결과는 그대로 반환된다.

이것은 2단계 축소의 **실행 시점** 절반이다. **압축 시점** 절반은 `pub/func/message/target_truncation.py::_truncate_read_file_content`에 있다: 압축이 컨텍스트를 자를 때 각 `ToolMessage`를 `tool_call_id`로 `read_file` 호출에 되돌려 매핑하고, `max_tool_output_chars`(2 000)의 head 30% + tail 30%를 남기며, 중간을 **파서가 도출한 1-based 연속 오프셋**을 담은 복구 안내로 바꾼다(`Use offset=<N> to continue reading…`; 페이로드가 파싱되지 않으면 "처음부터 다시 읽기"로 폴백).

두 단계는 구조적으로 상호 보완적이다: 압축 시점이 실행 시점에 슬라이스된 페이로드를 나중에 자르면 잘린 JSON이 더 이상 파싱되지 않으므로 압축 안내는 결정론적으로 재시작 형식으로 폴백한다 — 잘못된 오프셋이 방출되는 일은 결코 없고, 실행 시점 슬라이스 헬퍼가 자기 출력을 다시 슬라이스하는 일도 없다.

## 📥 인간 메시지 축출 (P1-9)

사용자는 어떤 도구도 만들지 않은 페이로드를 붙여넣을 수 있다: 로그, 문서, 전사록, 코드베이스 전체. `ContextEvictionMiddleware`는 DeepAgents의 인간 메시지 축출을 Sherry 고유의 분할로 이식했다.

- **트리거**(`before_model` / `abefore_model`): `human_evict_enabled`가 참이고 **마지막** 메시지가 `HumanMessage`이며 `lc_evicted_to`가 없고 추출 텍스트가 **`human_evict_threshold_chars`(200 000자)를 초과**할 때. 마지막 메시지만 검사하므로 과거 사용자 턴은 다시 들여다보지 않는다.
- **태깅 + 오프로드**(`evict_human_message`): 전문이 `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|timestamp>.md`에 기록된 뒤, 훅이 부분 state 업데이트 `{"messages": [tagged]}`를 반환한다. `tagged`는 **id와 내용이 그대로**이고 `additional_kwargs["lc_evicted_to"]`만 추가된다. 표준 `add_messages` reducer가 메시지를 **id로 제자리 갱신**한다 — 메시지 리스트 재작성 없음, `DeltaChannel` 의존 없음, state 측에서의 프리픽스 캐시 무효화 없음. 파일이 태그보다 먼저 기록되므로 쓰기 실패가 매달린 포인터를 남기는 일은 없다.
- **모델 뷰**(`wrap_model_call` / `awrap_model_call`): `lc_evicted_to`를 가진 모든 `HumanMessage`는 **요청 안에서만**(`request.override(messages=...)`) state 텍스트로 만든 프리뷰로 대체된다: 축출 경로, head/tail 각 5줄, `read_file` 복구 안내. 비텍스트 블록(이미지/오디오/비디오)은 원문 그대로 유지된다(`_build_evicted_content`) — 미디어가 텍스트 파일로 오프로드되는 일은 없다.
- **자가 치유**: 축출 파일이 없으면(세션 디렉터리 정리, 디스크 문제) `_heal_eviction_file`이 다음 모델 호출에서 state 텍스트로 다시 쓴다 — 단 대상이 해당 세션 자신의 `evicted/` 디렉터리일 때만. 태그 안의 외부 경로는 경고와 함께 거부된다.

### 도구 결과와 반대인 3-상태 분할인 이유

| 위치 | 도구 결과(P0-2) | 인간 메시지(P1-9) |
|---|---|---|
| 그래프 state / checkpointer | 프리뷰만 | **전문** + `lc_evicted_to` 태그 |
| MesMemory(`messages` 테이블) | 전문 | **전문**(태그는 영속화를 필터하지 않는다) |
| 다음 모델 호출(요청 뷰만) | 프리뷰 | 프리뷰(경로 + `read_file` 힌트) |
| `SESSIONS_DIR/<session_id>/evicted/` | 바이트 단위 동일 사본 | 바이트 단위 동일 사본 |

이 비대칭은 메시지가 **언제** 영속화되는지에서 나온다. 도구 결과는 도구 반환 시점에 내부 영속화 계층이 플러시하므로 state가 안전하게 프리뷰만 가질 수 있다. 인간 메시지는 턴의 첫 `after_model` 경계에서 영속화된다 — state가 프리뷰만 가지면 MesMemory가 프리뷰를 아카이브하고 `message_search`와 압축 모두 진짜 텍스트를 잃는다. 그래서 state는 전문을 유지하고 요청 뷰만 절단된다. 메시지 id가 결코 바뀌지 않으므로 워터마크는 그대로이고 두 번째 행도 기록되지 않는다.

## 🖼️ 미디어 거버넌스 (오프로드, 참조, 토큰 분류)

미디어는 입구와 출구 양쪽에서 통치됩니다: 입구 규칙은 처리할 수 없는 페이로드를 요청 밖으로 막고 쓸 수 없는 모델을 두 번 탐색하지 않게 하며, 압축과 토큰 추정은 base64를 결코 텍스트로 취급하지 않습니다.

**입력 크기 상한.** 모든 인바운드 미디어 페이로드 —— 인라인 base64 / `data:` 블록 또는 원격 URL 다운로드 —— 는 **어떤 디스크 쓰기보다 먼저** `MEDIA_PIPELINE["max_media_bytes"]`(20 MiB)와 비교됩니다. 상한을 넘는 페이로드는 건너뜁니다: 아무것도 기록되지 않고, 경로가 `MediaPaths`에 들어가지 않으며, 경고가 바이트 수를 기록하고, 메시지에는 첨부가 저장되지 않았고 모델로 보내지지 않았다는 모델 가시 `[Uploaded media]` 줄이 붙습니다. 원격 URL은 서버가 선언한 `Content-Length`가 있으면 그것으로, 없으면 `limit + 1` 바이트로 제한한 읽기로 판정하므로 잘못된 헤더가 초과 쓰기를 강제할 수 없습니다; 이미지·오디오·비디오 핸들러가 같은 게이트를 공유합니다(`media_handlers.py::_exceeds_media_limit` / `_record_oversize`). 상한과 정확히 같은 페이로드는 허용됩니다.

**요청별 능력 스크럽.** `MultimodalProcessor.wrap_model_call`(auto 모드)은 요청 사본만 재구성합니다 —— `request.override(messages=...)` —— 서빙 모델이 `"unsupported"`로 캐시된 패밀리의 미디어 블록마다 블록 유형, 기록된 디스크 경로, 대응 내장 스킬(`image_to_text` / `speech_to_text` / `video_text_to_text`)을 담은 텍스트 플레이스홀더로 바꿉니다. supported와 미탐색 블록은 그대로 통과하므로 혼합 메시지는 지원 패밀리의 네이티브 미디어를 유지하고 지원되지 않는 것만 블록 단위로 벗겨집니다. state·checkpointer·MesMemory에는 결코 쓰지 않고, 교체할 블록이 없으면 원래 요청 객체를 반환합니다. 스크럽은 `"auto"`에서만 동작합니다: `"true"`는 모든 블록을 모델에 남기고, `"false"`는 요청이 조립되기 전에 스킬 경로를 탑니다.

**조용한 퇴화 감지.** 모델은 미디어 블록을 받아 놓고 보지 못한 것처럼 답할 수 있습니다. auto 모드 네이티브 시도로 시작해 성공한 호출에서 `LLMRetryMiddleware`는 응답을 `detect_media_blindness()`(`media_pipeline/degradation.py`)로 평가합니다 —— 순수 정규식, en / zh / ja / ko, 정밀도 우선: 실명 표현의 ±40자 안에 미디어 단어가 있을 때만 히트 —— 그리고 명시적 "미디어를 설명해 달라" 요청 패턴도 포함합니다. 히트하면 요청에 실제로 나타난 모든 미디어 패밀리가 `"unsupported"`로 캐시되어 이후 턴은 스킬 경로로 직행합니다; 턴별 네이티브 플래그는 어느 쪽이든 지웁니다. `main_llm_silent_degradation_detection`(True)이 마스터 스위치입니다. 귀속은 서빙 모델을 따릅니다: `LLMRetryMiddleware`가 요청을 스티키 폴백 후보로 재바인딩할 때 먼저 턴별 네이티브 모델 키를 `{candidate.provider}/{candidate.model_name}`으로 다시 쓰므로, 오류 거부와 조용한 거부 모두 실제로 그 호출을 처리한 모델에 캐시됩니다.

**능력 캐시.** 세 입구 동작은 하나의 프로세스 수준 캐시(`agent/middlewares/llm_capability_cache.py`)를 공유합니다. 키는 `"{provider}/{model_name}"`, 패밀리별 값은 `"auto"`(미테스트) / `"supported"` / `"unsupported"`. 캐시는 프로세스 안에만 존재합니다: 재시작하면 깨끗해지고(최대 한 번의 헛된 네이티브 탐색), 모델 전환(env 변경 + 재시작)은 자연히 새 키가 됩니다.

**압축 시점 오프로드.** `_apply_compression`이 요약될 프리픽스(`current_messages[:cutoff]`)를 직렬화하기 전에 `offload_inline_media`(`agent/middlewares/summarization/media_offload.py`)가 그 범위만의 모든 인라인 미디어 블록을 다시 씁니다:

- `data:` URL / base64 페이로드(`image_url` / `audio_url` / `video_url`, 맨 `base64` 필드, `audio_bytes` / `video_bytes`, 또는 Anthropic 형식 `source.data`)를 디코드해 `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`에 씁니다; 확장자는 매직 바이트에서 `media_handlers._infer_extension`으로 도출합니다;
- 동일 바이트는 한 번만 씁니다 —— 내용 해시 파일명이 단일 및 여러 압축에 걸쳐 중복을 제거합니다;
- 블록은 텍스트 포인터 `[evicted to: <path>]`가 됩니다. P0-2 / P1-9 축출 경로가 내는 것과 같은 마커라서 `_collect_evicted_refs`가 줍고 `SummaryDoc.evicted_refs`가 경로를 요약 체인으로 실어 나릅니다(*Evicted References*로 렌더링);
- 디코드나 쓰기에 실패한 블록은 `<media error="failed_to_offload" />`가 됩니다 —— fail-open, 결코 크래시하지 않습니다.

보존 윈도우는 미디어를 그대로 유지합니다. 두 압축 경로 모두 프리픽스 슬라이스에 오프로드를 호출합니다 —— `_apply_compression_under_lock`(동기)와 `_aapply_compression_under_lock`(비동기). 요약 프롬프트는 미디어 참조 규칙을 싣습니다: 포인터를 그대로 보존하고, 시각/오디오/비디오 세부를 지어내지 말고, 경로에서 페이로드를 가져옵니다. 미디어 파일은 세션 트리 안에 있으므로 `clear_session()`이 `evicted/` 및 `plans/`와 함께 삭제합니다.

**토큰 분류.** `pub/func/estimate_tokens.py`는 content **리스트**를 블록 단위로 셉니다: 텍스트 블록은 텍스트, 미디어 블록은 `TOKEN_ESTIMATION`의 유형별 고정 비용(`tokens_per_image_block = 85` —— `langchain_core.count_tokens_approximately`와 정렬; `tokens_per_audio_block = 256`; `tokens_per_video_block = 1024`; 알 수 없는 블록은 `tokens_per_unknown_block = 85`, `data:` 페이로드를 숨긴 블록 포함). base64는 결코 직렬화되지 않고 결코 텍스트로 세어지지 않습니다: 제거된 JSON 경로는 5 MB base64를 약 125만 토큰으로 읽어 이미지 한 장으로 압축을 발화시켰습니다. `str` content, `None`, 순수 텍스트 리스트는 텍스트 추정치를 유지합니다.

## ⚡ 오버플로 테일 클립 (P1-2)

최신 도구 출력은 대개 가장 큰 컨텍스트 소비자이자 가장 희생 가능한 부분이다 — 그리고 그 모두가 이미 영속화되어 있다. `pub/func/message/overflow_clip.py::clip_overflow_tail`은 이것을 **모든 오버플로 경로의 첫 번째, 제로 LLM 수**로 만든다:

- **라우트보다 먼저.** `Summarization._dispatch_overflow_route`(T1/T2/T3)는 모든 비-`fits` 라우트에 대해 실행 전에 클립을 돌리고, `_forced_recovery_request`(T4/T5)는 강제 압축 단계 전에 돌린다. 클립 **단독**으로 추정치가 경계선 아래로 떨어지면 요청은 스텁된 리스트 그대로 반환되고 라우트는 전혀 실행되지 않는다 — 예산 트렁케이션도, 보조 LLM 호출도 없다.
- **클립 방식.** 꼬리에서 **연속된 `ToolMessage` 배치**를 훑으며(첫 비-`ToolMessage` 또는 이미 스텁된 메시지에서 중단) 각 내용을 `model_copy`로 압축 스텁으로 바꾼다. 삭제·재정렬·주입되는 메시지는 없다: `id`, `tool_call_id`, `name`, `additional_kwargs`가 모두 살아남아 도구 호출/결과 페어링과 영속 워터마크가 그대로 유지된다.
- **마커 생존.** 스텁은 P0-2의 `[evicted to: …]` 포인터(+ `read_file` 힌트)와 P2-4 슬라이스 안내를 그대로 다시 실어, 클립 후에도 복구 경로가 계속 작동한다.
- **멱등.** 스텁은 스캔 대상 배치를 종료시키므로 두 번째 순회는 no-op — T4/T5 재시도 예산이 동일 클립으로 소모되는 일은 없고, 다음 시도는 압축으로 퇴화한다.
- **유계.** `overflow_clip_max_remove`(10)가 한 번의 클립이 스텁화할 수 있는 메시지 수 상한; `overflow_clip_min_keep`(5)이 짧은 트랜스크립트에서 클립을 비활성화; `overflow_clip_enabled`가 마스터 스위치다.

### 제로 LLM 고속 경로 vs. 퇴화 경로

| 상황 | 실행되는 것 | LLM 호출 |
|---|---|---|
| 클립 단독으로 추정치가 임계값 아래로 | 스텁 리스트 반환; 라우트 미실행 | **0** |
| 클립 불충분(또는 비활성 / 대상 배치 없음) | 클립 결과 폐기; 기존 라우트가 **원본 리스트 그대로** 실행 | 라우트 의존(compact 계열은 보조 LLM 호출) |
| T4/T5 프로바이더 오류, 첫 복구 시도 | 먼저 클립; 충분하면 스텁 리스트로 프로바이더 호출 재시도 | **0** |
| T4/T5 프로바이더 오류, 클립 불충분 | 강제 압축 + 예산 트렁케이션 후 재시도 | compact 단계마다 보조 LLM 1회(≤ `MAX_OVERFLOW_RETRIES = 3`) |

수용 조건은 엄격하다: 클립 **단독**이 순수 로컬 추정치를 경계선 아래로 내릴 때만 적용된다(`estimate_messages_tokens(messages, reported_tokens=0)` — 오래된 `usage_metadata`가 복구를 주도하는 일은 결코 없다). 불충분한 클립은 폐기되어 기존 라우트가 원본 리스트에서 동작한다.

## 🧵 체인 요약 필터링

압축은 이력을 `lc_source="summarization"` 태그가 붙은 `HumanMessage` / `AIMessage` 쌍(AI 절반이 `<summary>` 태그 안에 요약을 담는다)으로 대체한다. 다음 압축에서 그 오래된 요약을 직렬화된 `<conversation>`에 다시 넣으면 토큰 낭비와 요약기 혼란을 부른다.

`agent/middlewares/summarization/core.py`는 두 단계로 처리한다:

1. `_extract_previous_summary`가 트랜스크립트에서 이전 요약 텍스트를 꺼낸다(가장 최신 태그된 `AIMessage`, 그다음 태그된 `HumanMessage`를 찾는다).
2. `_filter_summary_messages`가 새 프롬프트로 직렬화될 리스트에서 `lc_source="summarization"` 메시지를 **전부** 제거한다. `_build_summary_prompt`는 추출된 텍스트를 `<prior-summary>`로 별도 주입하며 갱신 지시문과 나란히 놓는다; 모델은 하나의 통합 요약을 만들 것, 이전 요약은 이후 폐기된다는 것을 듣는다.

요약 쌍 자체도 MesMemory에 결코 들어가지 않는다([경계마다의 영속화](#-경계마다의-영속화) 참조) — 원본 이력은 원본 그대로다.
