# 🧭 컨텍스트 거버넌스: 영속화, 축출, 슬라이스와 오버플로 클립

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 원본 기록을 어떻게 영속시키고 모델 가시 컨텍스트를 어떻게 작게 유지하는가: 모델 경계마다·도구 반환마다의 write-once 영속화, 복구 가능한 프리뷰를 남기는 도구 결과 디스크 축출, 실행 시점 `read_file` 슬라이스, state에 전문을 남기는 인간 메시지 축출, 미디어 거버넌스(입력 크기 상한, 요청별 능력 스크럽, 조용한 퇴화 감지, 압축 시점 오프로드, 블록 단위 토큰 분류), 어떤 압축 라우트보다 먼저 도는 LLM 없는 테일 클립, 그리고 오래된 요약을 대화 페이로드에서 차단하는 체인 요약 필터링.

에이전트가 만드는 모든 메시지는 두 번 가치 있다: **원본 기록**(실제로 일어난 일 — 검색과 압축을 위해)과 **모델 컨텍스트**(지금 윈도에 들어가는 것)다. 이 페이지는 그 두 요구를 화해시키는 각 메커니즘을 기록한다 — 모두 하나의 규칙을 공유한다: **파이프라인에 들어온 페이로드는 결코 잃지 않고, 줄이는 것은 모델 뷰뿐이며, 모든 축소는 전문을 가리키는 포인터를 남긴다.**

**사실상의 기준(source of truth):** `agent/middlewares/context_eviction/core.py`, `agent/middlewares/message_persistence/core.py`, `agent/middlewares/message_persistence/prepare.py`, `pub/func/message/eviction.py`, `pub/func/message/overflow_clip.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `agent/middlewares/summarization/core.py`, `agent/middlewares/summarization/media_offload.py`, `agent/middlewares/media_pipeline/scrub.py`, `agent/middlewares/media_pipeline/degradation.py`, `agent/middlewares/media_pipeline/media_handlers.py`, `agent/middlewares/llm_capability_cache.py`, `agent/middlewares/llm_retry/core.py`, `pub/func/estimate_tokens.py`, `context_engine/store/core.py`, `config/features/agent_side/tool_result_eviction.py`, `config/features/agent_side/summarization.py`, `config/features/agent_side/media_pipeline.py`, `config/features/agent_side/token_estimation.py`. 아래의 모든 주장은 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요와 파이프라인](#-개요와-파이프라인)
- [정보 출처](#%EF%B8%8F-정보-출처)
- [축출, 미디어, 오버플로](eviction/README.ko.md)
  - [💾 경계마다의 영속화](eviction/README.ko.md#-경계마다의-영속화)
  - [🗜️ 도구 결과 축출 (P0-2)](eviction/README.ko.md#%EF%B8%8F-도구-결과-축출-p0-2)
  - [✂️ `read_file` 슬라이스 (P2-4)](eviction/README.ko.md#%EF%B8%8F-read_file-슬라이스-p2-4)
  - [📥 인간 메시지 축출 (P1-9)](eviction/README.ko.md#-인간-메시지-축출-p1-9)
  - [🖼️ 미디어 거버넌스 (오프로드, 참조, 토큰 분류)](eviction/README.ko.md#%EF%B8%8F-미디어-거버넌스-오프로드-참조-토큰-분류)
  - [⚡ 오버플로 테일 클립 (P1-2)](eviction/README.ko.md#-오버플로-테일-클립-p1-2)
  - [🧵 체인 요약 필터링](eviction/README.ko.md#-체인-요약-필터링)
- [상호작용과 순서 보장](#-상호작용과-순서-보장)
- [설정](#%EF%B8%8F-설정)
- [테스트 맵](#-테스트-맵)
- [정리 의미론과 한계](#-정리-의미론과-한계)

## 🎯 개요와 파이프라인

이 메커니즘들은 하나의 파이프라인을 이룬다. 각 단계는 모델 뷰를 조금 더 줄이며, 그 어느 것도 원본 기록을 파괴하지 않는다:

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

| 단계 | 메커니즘 | LLM 비용 | 모델 뷰에 미치는 영향 |
|---|---|---|---|
| **도구 반환** | `ContextEvictionMiddleware` (P0-2 / P2-4) | 없음 | 일반 결과 > 20 000자 → head/tail 프리뷰 + 파일 포인터; `read_file` → 4 000자 슬라이스 + 안내 |
| **모델 경계** | `MessagePersistenceMiddleware`의 `after_model` | 없음 | 전문을 MesMemory에 기록(state 불변) |
| **인간 메시지** | `ContextEvictionMiddleware` (P1-9) | 없음 | 요청 뷰만 프리뷰로 절단; state/MesMemory는 전문 유지 |
| **오버플로(첫 수)** | `clip_overflow_tail` (P1-2) | 없음 | 꼬리 도구 결과를 스텁화; 메시지 정체성과 페어링 불변 |
| **오버플로(기존 라우트)** | `summarization` 4-라우트 디스패치 | compact 계열 라우트는 보조 LLM 1회 | 트렁케이션 및/또는 이력 압축 |
| **오버플로(프로바이더 오류)** | T4/T5 강제 복구 | compact 단계마다 1회 | 클립 → 압축 + 예산 트렁케이션, 최대 3회 재시도 |
| **미디어 오프로드(압축)** | `offload_inline_media` | 없음 | 요약될 프리픽스의 인라인 미디어 → 디스크 사본 + `[evicted to: …]` 포인터; 보존 윈도우 불변 |

## 🗂️ 정보 출처

그래프 state나 MesMemory에 도달하는 모든 정보는 아래 출처 중 하나로 들어온다. `origin` 열은 전량 출처 마커로 승격 중이다: `NULL`은 태깅 이전에 기록된 기존 사용자 메시지(읽기 측에서 `user`로 취급), `internal=True`를 가진 메시지는 **사용자 요청이 아니다** —— 요약의 Unresolved 목록은 사용자가 직접 보낸 메시지만 받는다(긍정 식별 / positive identification). 비메시지 출처(축출 파일, 계획 지식)도 함께 기재한다: `messages` 행이 되지는 않지만 주입 가능한 컨텍스트다. `planned` / `reserved`로 표시된 행은 아직 구현되지 않았다.

| 정보 출처 | origin / 마커 | internal | 발생 상황 | 영속화 | 주입 동작 |
|---|---|---|---|---|---|
| 프런트엔드 WS 사용자 메시지 | `origin='user'` | — | 사용자가 클라이언트에서 메시지 전송 | `messages` 행 `origin='user'` + 전문(경계마다 영속화) | state/MesMemory에 상주; 모델 뷰는 프리뷰로 축출될 수 있음; 요약은 최신 사용자 요청을 축자로 보존(`latest_user_request`; 다중 요청 목록 + 요청별 축출 포인터: `planned`) |
| 채널 사용자 메시지(QQ 등) | `origin='user'` | — | 사용자가 채널 어댑터로 전송 | 위와 같음 | 위와 같음 |
| TaskIntent 스티어링 / 리마인더 | `origin='task_intent'` | `True` | 계획 활성 유도 / 작업 의도 무장(`task_intent/core.py::_task_intent_message`) | `messages` 행 | **사용자 요청이 아님** —— Unresolved 목록에 들어가지 않음 |
| 서브에이전트 완료 캐리어 | `origin='subagent_completion'` | `True` | 백그라운드 서브에이전트가 완료 후 결과 통지 | `messages` 행(origin은 영속화 이음매에서 각인, `context_engine/store/core.py`) | 사용자 요청이 아님; 모델 뷰에 가시 |
| 하트비트로 트리거된 턴 | `origin='heartbeat'` | — | 하트비트 서비스의 턴(**현재 이런 경로 없음 —— `reserved`**) | — | 사용자 요청이 아님 |
| cron으로 트리거된 턴 | `origin='cron'` | `True` | 예약 작업의 세션 턴(`origin_for_source`) | `messages` 행 | 사용자 요청이 아님 |
| 압축 요약 쌍 | `lc_source='summarization'`(`additional_kwargs` 내, origin 열 아님) | — | 압축 산출물(`_build_new_messages`) | **MesMemory에 영속화되지 않음**; state 요약 쌍 | `<summary>`가 모델 뷰에 상주; `<prior-summary>`로 체인 연속 |
| 축출 파일 | 비메시지 —— 디스크 파일 | — | P0-2 / P1-9 축출 | `SESSIONS_DIR/<session_id>/evicted/`(바이트 단위 전문) | 필요 시 `read_file`; 요약 체인이 구조화 요약 문서에 `evicted_refs[]` 포인터를 운반 |
| 미디어 파일 | 비메시지 —— 디스크 파일 | — | 업로드 처리(`MultimodalProcessor`), 이력 메시지의 네이티브 블록 제거, 또는 압축 시점 오프로드(`offload_inline_media`) | `SESSIONS_DIR/<session_id>/media/`(영속 사본; 압축 시점 오프로드는 `sha256[:16]` 이름으로 중복 제거); `max_media_bytes` 초과 페이로드는 결코 기록되지 않음 | 네이티브일 때 미디어 블록; 스킬 경로 힌트와 요청별 스크럽 플레이스홀더가 경로를 전달; 요약 체인은 오프로드된 경로를 `evicted_refs[]`에 유지 |
| 계획 지식 | 비메시지 —— 디렉터리 | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | `plan_ref`로 `<knowledge>` 블록 주입 |
| FACTS.md | `workspace/memory/FACTS.md`(memory 도구 target `facts`) | — | 모듈 비의존적 광범위 함정과 규약: 압축 시 기억 검토 + 계획 완료 추출 | memory 파일(1 375자 상한; 초과 시 가장 오래된 항목부터 축출) | 상주 FACTS 메모리 블록으로 매 시스템 프롬프트에 주입 |

## 🔗 상호작용과 순서 보장

순서 보장(`agent/core.py`에서 검증, 리스트 순서 = 등록 순서):

| 훅 단계 | 여기서 중요한 순서 |
|---|---|
| `before_agent`(리스트 순) | `MultimodalProcessor`가 모델 루프 **전에** 돌므로 P1-9 태깅은 항상 미디어 힌트가 병합된 최종 텍스트를 본다 |
| `before_model`(리스트 순) | P1-9 태깅이 `ToolCallNormalize` / `SubagentCompletionDrainMiddleware`보다 먼저 |
| `wrap_model_call`(바깥→안) | ContextEviction(P1-9 뷰 대체) → … → Summarization(가장 안쪽, LLM에 가장 가까움) |
| `after_model`(역순) | `MessagePersistenceMiddleware`가 모델 이후 **첫** 훅 — HITL이 거부된 도구 호출을 벗겨내거나 `GraphInterrupt`를 올리기 전에 AI 메시지가 영속화된다 |
| `wrap_tool_call`(바깥→안) | IterationBudget → ToolGuardrails → ContextEviction → PathGuard → HeartbeatStaleness → HumanInTheLoop → **MessagePersistence(가장 안쪽)** — 원본 결과가 먼저 플러시되고 나가는 길에 프리뷰로 교체된다 |

상호작용 맵:

| 상호작용 메커니즘 | 무슨 일이 일어나는가 |
|---|---|
| **Summarization / 압축** | P1-9가 전문을 state에 남기므로 압축이 여전히 그것을 본다; 요약 쌍은 영속화와 다음 `<conversation>` 양쪽에서 제외된다 |
| **P1-2 오버플로 테일 클립** | `model_copy`로 `ToolMessage` 내용만 스텁화; `HumanMessage`는 절대 건드리지 않으므로 `lc_evicted_to` 태그와 state 전문이 모든 클립을 견딘다; 스텁은 P0-2 / P2-4 마커를 앞으로 운반한다 |
| **체인 요약 필터링** | 직렬화되는 대화에서만 `lc_source="summarization"` 메시지를 제거; state 트랜스크립트와 MesMemory 저장소는 불변 |
| **HITL** | 거부는 도구 반환 플러시를 우회하고(HITL이 바깥에서 영속화를 감쌈) 다음 경계에서 영속화된다; 영속화 배치는 거부된 도구 호출을 다시 붙여 거부가 짝지어진 AI 행을 유지하게 한다. HITL은 `HumanMessage`를 보지 않으므로 축출과 직교한다 |
| **`message_search`** | FTS5/SQLite 검색은 MesMemory 위에서 돌며, 거기에는 **완전한** 도구 결과와 **완전한** 인간 텍스트가 아카이브되어 있다 — 축출은 모델 뷰만 줄인다 |
| **프리픽스 캐싱** | P1-9는 id로 메시지를 제자리 갱신하고(내용 동일, id 동일) 다른 것은 다시 쓰지 않으므로, 모델 가시 프리픽스는 프리뷰 뷰가 실제로 다를 때만 무효화된다; 도구 축출은 메시지가 state에 들어가기 전에 일어나므로 모델이 보는 것은 처음부터 프리뷰판뿐이다 |
| **도구 페어링 / 새니타이저** | 모든 대체가 `id`와 `tool_call_id`를 보존한다; `sanitize_tool_use_result_pairing`이 축출 때문에 수리할 일은 결코 없고, 스텁은 계속 유효한 페어링 입력이다 |
| **서브에이전트 세션** | 자식 파이프라인은 `ContextEvictionMiddleware`와 `MessagePersistenceMiddleware`를 **등록하지 않는다**: 자식 트랜스크립트는 완전한 도구 결과를 유지하고 checkpoint에만 존재한다 |

## 🛠️ 설정

`TOOL_RESULT_EVICTION`(`config/features/agent_side/tool_result_eviction.py`):

| 키 | 기본값 | 의미 |
|---|---|---|
| `enabled` | `True` | 도구 결과 축출(P0-2) 마스터 스위치 |
| `evict_threshold_chars` | `20_000` | 이 문자 수를 넘는 텍스트를 축출 |
| `preview_head_lines` / `preview_tail_lines` | `5` / `5` | 프리뷰 head/tail 줄 수 |
| `eviction_subdir` | `"evicted"` | `SESSIONS_DIR/<session_id>/` 아래 하위 디렉터리 |
| `excluded_tools` | 8개 이름 | 절대 축출하지 않음(`read_file`은 슬라이스 경로로) |
| `human_evict_enabled` | `True` | 인간 메시지 축출(P1-9) 마스터 스위치 |
| `human_evict_threshold_chars` | `200_000` | 인간 메시지 트리거 임계값 |
| `human_preview_head_lines` / `human_preview_tail_lines` | `5` / `5` | 인간 메시지 프리뷰 head/tail 줄 수 |

`SUMMARIZATION`(`config/features/agent_side/summarization.py`) 중 이 페이지가 의존하는 키:

| 키 | 기본값 | 의미 |
|---|---|---|
| `overflow_clip_enabled` | `True` | P1-2 테일 클립 마스터 스위치 |
| `overflow_clip_max_remove` | `10` | 한 번의 클립이 스텁화하는 꼬리 메시지 상한 |
| `overflow_clip_min_keep` | `5` | 트랜스크립트 하한: 이 길이 이하면 결코 클립하지 않음 |
| `max_tool_output_chars` | `2_000` | 도구 결과의 압축 시점 클립 예산 |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | 압축 시점 클립의 head/tail 유지 비율 |

`TOKEN_ESTIMATION`(`config/features/agent_side/token_estimation.py`), 멀티모달 토큰 분류:

| 키 | 기본값 | 의미 |
|---|---|---|
| `chars_per_token` / `chars_per_token_cjk` | `4` / `2` | 텍스트 추정 제수(비 CJK / CJK) |
| `tokens_per_image_block` | `85` | 이미지 블록당 고정 비용(`langchain_core.count_tokens_approximately`와 정렬) |
| `tokens_per_audio_block` / `tokens_per_video_block` | `256` / `1024` | 보수적 고정 비용(추정 시 길이 메타데이터 없음) |
| `tokens_per_unknown_block` | `85` | 알 수 없는 블록의 고정 비용 —— 결코 그 base64가 아님 |

`MEDIA_PIPELINE`(`config/features/agent_side/media_pipeline.py`)의 입구 측 키:

| 키 | 기본값 | 의미 |
|---|---|---|
| `main_llm_native_multimodal` | `"auto"` | 3-상태 네이티브 스위치: `"true"`는 모델에 미디어 블록을 남기고, `"false"`는 항상 스킬 경로, `"auto"`는 능력 캐시로 패밀리별 결정; 그 밖의 값은 페일세이프로 스킬 경로 |
| `main_llm_silent_degradation_detection` | `True` | 네이티브 응답이 미디어 실명을 자술하면 요청에 나타난 미디어 패밀리를 `"unsupported"`로 캐시 |
| `max_media_bytes` | `20 * 1024 * 1024` | 페이로드당 하드 상한; 초과 페이로드는 쓰기 전에 건너뜀 |

이 노브들에는 환경 변수가 없다: 설계상 위 feature TypedDict들의 코드 기본값이다.

## 🧪 테스트 맵

| 스위트 | 커버 내용 |
|---|---|
| `tests/agent/middlewares/context_eviction/test_context_eviction.py` | P0-2/P2-4 미들웨어 동작: 축출, 제외, 슬라이스, 워터마크 커버, 페일오픈 |
| `tests/agent/middlewares/context_eviction/test_human_eviction.py` | P1-9 태깅, reducer 제자리 갱신, 모델 뷰 절단, 자가 치유, 미디어 보존 |
| `tests/agent/middlewares/message_persistence/test_message_persistence.py` | 경계 영속화, 워터마크 write-once, 거부 재페어링 |
| `tests/agent/middlewares/message_persistence/test_tool_result_persistence.py` | 도구 반환 플러시와 id 없는 핑거프린트 / 마커 상호작용 |
| `tests/agent/middlewares/message_persistence/test_compression_no_persistence.py` | 압축 경로가 MesMemory에 아무것도 쓰지 않음 |
| `tests/pub/func/message/test_eviction.py` | 순수 축출 원시 함수: 임계값, 프리뷰, 멱등성, 안전하지 않은 세션 id |
| `tests/pub/func/message/test_read_file_slice.py` | P2-4 실행 시점 슬라이스와 멱등성 |
| `tests/pub/func/message/test_overflow_clip.py` | P1-2 순수 클립: 꼬리 배치 감지, 게이트, 토큰 목표, 마커 보존 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | P1-2 미들웨어 통합: 제로 LLM 복구, 퇴화, T4/T5, 동기/비동기 패리티 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 체인 요약 필터링과 `<prior-summary>` 주입 |
| `tests/agent/middlewares/test_compression_media_offload.py` | 압축 시점 인라인 미디어 오프로드: 쓰기 + 포인터, 해시 중복 제거, 실패 플레이스홀더, 보존 윈도우 불변, 동기/비동기, `evicted_refs` |
| `tests/pub/func/test_estimate_tokens_media.py` | 블록 단위 멀티모달 추정: 고정 미디어 비용, 5 MB base64 회귀, 미디어를 숨긴 미지 블록, `str` / `None` / 빈 리스트 경계 |
| `tests/agent/middlewares/test_multimodal_processor.py` | 3-상태 네이티브 스위치와 요청별 스크럽: 혼합 패밀리는 지원되지 않는 블록만 제거, 지원 블록은 그대로, state/kwargs/디스크 불변, `"true"`는 결코 스크럽하지 않음, 이력 이미지 제거 |
| `tests/agent/middlewares/test_media_size_limit.py` | `max_media_bytes` 상한: 초과 페이로드는 쓰기 전에 건너뜀, 모델 가시 알림, 상한 정확히는 허용, 선언된 `Content-Length` 고속 경로, 제한된 읽기 |
| `tests/agent/middlewares/test_media_degradation.py` | 조용한 퇴화 감지: en / zh / ja / ko 실명 정규식 + 설명 요청 패턴, 유능하거나 무관한 응답은 오탐 없음, 실제 나타난 모든 패밀리 캐시, 어느 쪽이든 플래그 해제, 폴백 후보 귀속 |
| `tests/agent/middlewares/test_multimodal_native_fallback_e2e.py` | 거부 → 캐시 + 스킬 경로 재작성: 다음 세션 네이티브 건너뜀, 모델 키 격리, 명시 `"true"`는 폴백하지 않음 |
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` 워터마크 저장소 |
| `tests/full/test_context_governance_e2e.py` | 라이브 네트워크 e2e(실제 LLM + 실제 그래프): 모든 메커니즘 종단 간 — 명시적으로 실행 |

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

## 🧹 정리 의미론과 한계

- **`clear_session`은 한 번에 전부 지운다.** `server/DAO/messages.py::clear_session`은 세션의 MesMemory 행(`persisted_message_ids` 워터마크까지 함께 삭제), checkpointer 이력, 그리고 `SESSIONS_DIR/<session_id>/` 폴더 전체를 삭제한다 — 따라서 **`evicted/` 파일과 `plans/`가 세션과 함께 삭제된다**(`config/path.py::session_plans_dir`가 같은 전면 삭제 계약을 기록한다). 인메모리 레지스터는 마지막에 정리된다.
- **축출은 모델 뷰 축소이며 결코 삭제가 아니다.** 이 페이지가 줄이는 모든 페이로드는 MesMemory에 아카이브되거나, 그래프 state에 전문이 있거나(인간 메시지), 디스크 `evicted/` 아래에 있다 — 그리고 모든 프리뷰가 포인터를 운반한다.
- **`evicted/` 디렉터리는 세션 소유지만 가비지 컬렉션이 없다.** 파일은 `clear_session`까지 살아남는다; 메시지별 TTL은 없다. 거대한 도구 결과가 많은 장수 세션은 `workspace/sessions/<session_id>/evicted/`에 디스크 사용량을 쌓을 수 있다.
- **한 줄짜리 거대 행은 결코 축출되지 않는다.** head와 tail이 모두 전체 페이로드를 담게 되면 프리뷰가 원문보다 작아질 수 없어 메시지는 그대로 남는다(도구 경로와 인간 경로 모두).
- **`read_file` 슬라이스는 슬라이스 자체에서 복구할 수 없다 — 설계상.** 소스 파일이 복구 경로이며, 안내가 이어 읽는 방법을 정확히 알려준다. 파일이 없어지거나 삭제될 때만 무너진다.
- **인간 메시지 축출은 꼬리 메시지로 제한된다.** 거대 페이로드 뒤에 또 다른 사용자 턴이 이어져도 재검사되지 않는다; "마지막 메시지만"은 확정된 이력을 다시 들추지 않기 위한 의도적 설계다.
- **테일 클립은 꼬리 배치가 클 때만 도움이 된다.** 컨텍스트가 인간 턴이나 비도구 메시지에 먹혀 있으면 기존 라우트로 퇴화한다; P1-2는 최적화이지 보장이 아니다.
- **서브에이전트 트랜스크립트는 범위 밖이다.** 자식은 완전한 도구 결과를 유지한다(축출 없음, 영속화 없음) — 그 트랜스크립트는 checkpoint에만 존재하며 클라이언트 가시 MesMemory 이력에 결코 들어가지 않는다.
