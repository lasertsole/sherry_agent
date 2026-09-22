# 🗜️ 컨텍스트 압축: Summarization 미들웨어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 긴 대화를 모델의 컨텍스트 윈도우 안에 유지하는 방법: 다섯 개의 트리거 지점이 전체 라이프사이클(턴 시작 전, 모든 모델 호출 전, 모든 모델 응답 후, 프로바이더 오버플로 에러 시)을 감시하고, 순수 함수형 4-경로 라우터가 가장 저렴한 수리책을 고르며(큰 도구 결과와 과도하게 큰 도구 호출 인자를 먼저 잘라내고, 강제될 때만 AI 압축), 안티-스래싱 가드가 압축이 통제 없이 불어나는 일을 원천 차단합니다.

사실상의 기준(source of truth): `agent/middlewares/summarization/core.py`, `agent/middlewares/summarization/compression.py`, `agent/middlewares/summarization/overflow.py`, `agent/middlewares/summarization/summary_generation.py`, `agent/middlewares/summarization/thrash.py`, `agent/middlewares/summarization/state_aliases.py`, `agent/middlewares/summarization/plan_context.py`, `pub/func/message/overflow_router.py`, `pub/func/message/tool_result_ttl.py`, `pub/func/message/llm_error_classifier.py`, `pub/func/estimate_tokens.py`, `pub/func/message/tool_output_dedup.py`, `pub/func/message/tool_output_prune.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `pub/func/message/turn_utils.py`, `config/features/agent_side/summarization.py`, 그리고 두 등록 지점 `agent/core.py`와 `agent/tools/subagent/spawn/core.py`. 이 문서의 모든 줄 번호와 상수는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [트리거: 라이프사이클과 오버플로 라우팅](triggers/README.ko.md)
  - [🧭 라이프사이클: 다섯 개의 트리거 지점 (T1–T5)](triggers/README.ko.md#-라이프사이클-다섯-개의-트리거-지점-t1t5)
  - [🚦 4-경로 오버플로 라우팅 결정](triggers/README.ko.md#-4-경로-오버플로-라우팅-결정)
- [토큰 추정 (토크나이저 없음)](#-토큰-추정-토크나이저-없음)
- [압축 내부](internals/README.ko.md)
  - [✂️ 트렁케이트 트랙: 예산 트렁케이션과 TTL 모듈](internals/README.ko.md#-트렁케이트-트랙-예산-트렁케이션과-ttl-모듈)
  - [🔁 컴팩트 트랙: `_apply_compression` 내부](internals/README.ko.md#-컴팩트-트랙-_apply_compression-내부)
  - [🖼️ 압축 시점 미디어 오프로드 (인라인 미디어 → 참조)](internals/README.ko.md#-압축-시점-미디어-오프로드-인라인-미디어--참조)
  - [📝 LLM 요약: 프롬프트, 체이닝, 폴백](internals/README.ko.md#-llm-요약-프롬프트-체이닝-폴백)
  - [🧱 정적 폴백 (LLM 없는 요약)](internals/README.ko.md#-정적-폴백-llm-없는-요약)
  - [📦 출력: 요약 메시지 쌍](internals/README.ko.md#-출력-요약-메시지-쌍)
  - [🛡️ 안티-스래싱 가드 매트릭스와 성능 저하 복구](internals/README.ko.md#-안티-스래싱-가드-매트릭스와-성능-저하-복구)
  - [🔄 시스템 프롬프트 갱신](internals/README.ko.md#-시스템-프롬프트-갱신)
  - [📌 등록 지점](internals/README.ko.md#-등록-지점)
- [설정 참조](#-설정-참조)
- [테스트](#-테스트)
- [⚠️ 정직함과 한계](#%EF%B8%8F-정직함과-한계)

## 🎯 개요

`Summarization`(`agent/middlewares/summarization/core.py`, 클래스는 `core.py:234`)은 **처음부터 직접 구현한** `AgentMiddleware`입니다 — LangChain 내장 `SummarizationMiddleware`를 상속하지 **않습니다**. 에이전트 라이프사이클의 정확히 두 지점에만 훅을 겁니다:

- `before_agent` / `abefore_agent`(`core.py:399` / `core.py:403`) — **T1 사전 점검**
- `wrap_model_call` / `awrap_model_call`(`core.py:413` / `core.py:495`) — **T2 디스패치, T3 응답 후 재확인, T4/T5 에러 복구 링**

미들웨어 체인에서는 **가장 안쪽 — LLM에 가장 가까운** 위치에 놓입니다. 압축이 발동되면 히스토리는 항상 다음 모양이 됩니다:

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

교체물이 Human/AI 쌍이기 때문에 모델은 연속된 같은 역할의 메시지를 절대 보지 못하고, 페어링 수리가 필요 없습니다.

등록 지점은 두 곳입니다:

| 사이트 | 트리거 | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| 메인 에이전트(`agent/core.py:204`) | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| 워커/서브에이전트(`agent/tools/subagent/spawn/core.py:909`) | `("messages", 40)` **또는** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`(기본값) |

둘 다 `main_llm_context_window=main_llm_max_tokens`(`MAIN_LLM_MAX_TOKEN`에서 유래)와 `keep=("messages", 10)`을 전달합니다.

## 🪙 토큰 추정 (토크나이저 없음)

`pub/func/estimate_tokens.py`(230행)는 의도적으로 토크나이저 없이 결정론적으로 동작하며, 3단계 폴백을 가집니다:

- **T1 — API 보고 사용량:** 마지막 `AIMessage`가 `usage_metadata`를 가지면(또는 호출자가 `reported_tokens`를 명시하면) `estimate_messages_tokens`가 그 값을 그대로 반환합니다 — provider의 실측값이 모든 로컬 추정을 단축합니다;
- **T2 — CJK 인식 휴리스틱:** `estimate_text_tokens`는 텍스트를 CJK 문자(`// CHARS_PER_TOKEN_CJK = 2`)와 나머지(`// CHARS_PER_TOKEN = 4`)로 나누고, 감지에는 `pub.func.cjk.count_cjk`를 재사용합니다;
- **T3 — 레거시 `len // 4`:** 별도 코드 경로가 아니라 `count_cjk(text) == 0`일 때의 T2 퇴화 케이스입니다 — 따라서 순수 ASCII 추정치는 기존 숫자와 정확히 일치합니다.

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# 메시지 단위: str content → 텍스트 추정
#           + Σ tool_call name/args 문자 + tool_call_id 문자
# 리스트 content → 블록 단위: 텍스트 블록은 텍스트, 미디어 블록은
#   유형별 고정 비용, 알 수 없는 블록은 보수적 미지 비용
```

content가 **리스트**이면 블록 단위로 세고, 리스트 전체를 JSON 직렬화하지 않습니다: 텍스트 블록은 텍스트로 추정하고, 미디어 블록(`image_url` / `audio_url` / `video_url` / `audio_bytes` / `video_bytes`, 또는 `data:` 페이로드를 담은 임의 블록)은 `TOKEN_ESTIMATION`의 고정 비용 —— `tokens_per_image_block = 85`, `tokens_per_audio_block = 256`, `tokens_per_video_block = 1024` —— 을, 알 수 없는 블록은 `tokens_per_unknown_block = 85`를 받습니다. 제거된 JSON 경로는 5 MB base64를 약 125만 토큰으로 세어 이미지 한 장으로 압축을 발화시켰습니다; 이 고정값은 모델 없이는 실제 토큰 수를 도출할 수 없는 미디어의 보수적 대체값입니다. `str` content와 `None`은 그대로이고, 순수 텍스트 리스트는 연결된 텍스트와 같은 추정치를 냅니다.

`pub/func/message/estimate_msg_tokens.py`는 이제 같은 헬퍼들의 하위 호환 re-export입니다. 빠르고, 실행 간 안정적이며(같은 입력 → 같은 숫자 → 재현 가능한 테스트), 의도적으로 보수적으로 근사합니다. 트리거/예산 경로의 어떤 것도 모델 토크나이저에 의존하지 않습니다.

## ⚙️ 설정 참조

모든 임계값은 `config/features/agent_side/summarization.py`(SUMMARIZATION TypedDict)에 있습니다. ◆ 표시 상수는 살아있는 코드 경로가 소비합니다; ○ 표시 상수는 정의 또는 임포트는 되지만 살아있는 경로는 **소비하지 않습니다**("정직함과 한계" 참조).

| 상수 | 값 | 소비 위치 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route`의 하드 오버플로 밴드; T3 압력 게이트; 두 트리거 절을 구성 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route`의 소프트 오버플로 밴드 |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`(overflow.py:168): 윈도우 − 예비량 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 트렁케이트 트랙 예산 = usable × 0.60 (overflow.py:219) |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results`의 후보 하한 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 최신 메시지는 절대 트렁케이트 불가 (페어링 마진) |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | T4/T5 강제 복구 상한 (단일 공유 카운터) |
| `OVERFLOW_CLIP_ENABLED` ◆ | `True` | P1-2 LLM 없는 테일 클립 마스터 스위치 |
| `OVERFLOW_CLIP_MAX_REMOVE` ◆ | `10` | 클립 1회당 스텁 처리하는 꼬리 메시지 상한 |
| `OVERFLOW_CLIP_MIN_KEEP` ◆ | `5` | 트랜스크립트 하한: 이 수 이하면 클립하지 않음 |
| `MAX_COMPRESS_ATTEMPTS_PER_TURN` ◆ | `3` | 턴당 선제 압축 상한 |
| `COMPACTION_COOLDOWN_ROUNDS` ◆ | `3` | 실제 compact마다 무장되는 쿨다운 |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 보존 예산 하한; 윈도우가 없을 때의 예산 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 보존 예산 상한 |
| `PRESERVE_RATIO` ◆ | `0.25` | 보존 예산 = 윈도우의 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | 프루닝: 최신 도구 출력 토큰 보존량 |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | 프루닝: 적용 최소 수익 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | 타깃 트렁케이트: 현재 토큰의 50%로 수축 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | 타깃 트렁케이트: 자격 기준 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | 타깃 트렁케이트: 출력당 상한 |
| `MIN_ARGS_CHARS_TO_TRUNCATE` ◆ | `500` | 도구 인자 트렁케이트: 자격 기준 (JSON 직렬화된 인자 길이) |
| `MAX_TOOL_ARGS_CHARS` ◆ | `2_000` | 도구 인자 트렁케이트: 인자당 상한 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 공격적 백스톱 절단 길이 (도구 결과와 도구 호출 인자) |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 요약 메시지 문자 상한 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | 모든 머리/꼬리 보존 (요약과 TTL 트렁케이션) |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 강제 복구 전 빈 응답 수 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 성능 저하 복구 예산 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | 거버너: 세션 시도 상한 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | 거버너: 연속 무효 → LLM 스킵 |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | 거버너: 토큰 축소 유효성 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | 모든 축소 전략에서 면제 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 마지막 턴 압축 게이트 |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO 섹션 상한 |
| `ACTIVE_PLAN_NOTES_MAX_ITEMS` / `EVICTED_REFS_MAX_ITEMS` ◆ | `20` / `20` | 문서 배열 상한(계획 노트 / 퇴출 포인터) |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 파일 작업 래칫 목록 상한 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 복구 컨텍스트 요청 상한 |
| `CHARS_PER_TOKEN` / `CHARS_PER_TOKEN_CJK`(추정기) | `4` / `2` | 결정론적 토큰 추정 제수(비 CJK / CJK); `config/features/agent_side/token_estimation.py`에 정의 |
| `TOKENS_PER_IMAGE_BLOCK` / `TOKENS_PER_AUDIO_BLOCK` / `TOKENS_PER_VIDEO_BLOCK` / `TOKENS_PER_UNKNOWN_BLOCK`(추정기) | `85` / `256` / `1024` / `85` | 멀티모달 content 리스트의 블록별 고정 비용 —— base64는 절대 텍스트로 세지 않음; `config/features/agent_side/token_estimation.py`에 정의 |
| `PRUNE_TTL_SECONDS` | `300` | TTL 만료 지평 — TTL 트리오만 소비 (오늘날 테스트 전용) |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 최초 관찰 레지스트리 한계 (오늘날 테스트 전용) |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 미들웨어가 임포트, 절대 읽지 않음 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 미들웨어가 임포트, 절대 읽지 않음 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 정의됨, 임포트되지 않음 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 정의됨, 임포트되지 않음 (실제 사용되는 것은 900자 목록 상한) |

## 🧪 테스트

| 스위트 | 케이스 | 커버 |
| :---- | :---- | :----- |
| `tests/pub/func/message/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` 밴드, 후보 규칙, 안정적 라우트 문자열 |
| `tests/pub/func/message/test_tool_result_ttl.py` | 28 | 제자리 트렁케이션, 페어링 불변식, 비어 있지 않은 플레이스홀더, 레지스트리 한계, 예산 트렁케이션 |
| `tests/pub/func/message/test_llm_error_classifier.py` | 56 | 413 상태, 텍스트 힌트, 7개 오버플로 패턴, cause 체인 깊이, 읽기 전용 보장 |
| `tests/pub/func/message/test_pub_func_message_tools.py` | 29 | 중복 제거 / 프루닝 / 타깃 트렁케이트 / 턴 유틸리티에 도구 인자 트렁케이트 추가: 머리+꼬리 형식, 작은 인자 스킵, 확보량 클램프, 보호 도구, 최근 스킵, 페어링 및 무변경 |
| `tests/pub/func/message/test_read_file_slice.py` | 12 | read_file 복구 가능 슬라이스: 원본 경로 + 1-based 이어읽기 offset 안내, 행 스킵 없음, 절대 페이지 번호, 일반 마커 바이트 동일, 보호 / 예산 내 / 폴백 경로 |
| `tests/config/test_num_contract.py` | 46 | 상수 계약 (워치독 `CONTRACT_NAMES`가 문서화된 모든 노브 커버) |
| `tests/pub/func/message/test_overflow_clip.py` | 21 | P1-2 순수 클립: 꼬리 배치 감지, max_remove/min_keep/enabled 게이트, 토큰 목표, 마커 보존(P0-2 포인터, P2-4 안내), no-op 멱등성, 페어링 불변식 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | 9 | P1-2 미들웨어 통합: T1/T2 LLM 없는 클립, 불충분 클립 퇴화, 킬 스위치, T4/T5 클립 후 재시도와 클립→압축 퇴화, 동기/비동기 패리티, 새니타이저 불변 |
| `tests/agent/middlewares/test_compression_comprehensive.py` | 52 | 12개 클래스: T2 소프트 오버플로, T2 쿨다운, T2 음성/무작동, 동기/비동기 패리티, T1 사전 점검, 라우트 결정, T3 트리거/3형태/음성 이중, T4/T5 복구, 전체 안티-스래싱 매트릭스, 전체 분기 패리티, 체이닝 요약 필터링 |
| `tests/agent/middlewares/test_compression_media_offload.py` | 12 | 압축 시점 인라인 미디어 오프로드: 쓰기 + 포인터, 내용 해시 중복 제거, 디코드/쓰기 실패 플레이스홀더, 보존 윈도우 미디어 불변, 동기/비동기 패리티, `evicted_refs` 수집 |
| `tests/pub/func/test_estimate_tokens_media.py` | 22 | 블록 단위 멀티모달 추정: 유형별 고정 비용, 5 MB base64 회귀, 미디어를 숨긴 미지 블록, `str` / `None` / 빈 리스트 / 순수 텍스트 경계 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 6 | 체이닝 요약 필터링: 이전 쌍을 직렬화된 대화에서 제거, 일반/빈/다중 쌍 입력, 마커 없는 레거시 human 보존, async `_acreate_summary` 미러 |
| `tests/agent/middlewares/test_summary_doc.py` + `test_summary_doc_middleware.py` | 41 | 구조화 요약: schema 강제 변환, 렌더링 왕복 + 바이트 안정 + 섹션 순서, 코드층 cap + 주석, latest request 그대로, json_mode/json_repair/free-form 3티어, prior-doc JSON 체이닝, 레거시 MD 전환, 퇴출 포인터 수집·승계 |
| `tests/agent/middlewares/test_summary_active_plan.py` | 20 | Part 1: 계획 활성 판정(state/todo 소스, 전체 완료 게이트, fail-open), 최초/업데이트 프롬프트 양 경로 주입, 체인 압축을 넘는 노트 상속/추가/cap/클리어, latest request 그대로 + 퇴출 포인터, 다중 메시지 연속 발송 |
| `tests/agent/middlewares/test_compression_e2e_static.py` | 18 | 6개 엔드투엔드 시나리오 + 3개 오버플로 카운터 회귀 테스트 × 2 등록 순서, 정적 폴백 압축, 제로 네트워크 |
| `tests/agent/middlewares/test_summarization_trigger.py` | 3 | 등록 계약(테스트 고정 윈도우): `MAIN_LLM_MAX_TOKEN = 65 536` → 트리거 임계값 `52 428`; 저토큰 통과 |
| `tests/agent/middlewares/test_summarization_comprehensive.py` | 140 | 레거시 딥 스위트: 절단점/예산, FIFO 상한, 폴백, 프루닝/중복 제거/타깃 트렁케이트, 성능 저하 |
| `tests/agent/middlewares/test_e2e_summarization.py` | 7 | 전체 그래프 밀폐 e2e: 실제 `create_agent` 체인 (주 모델 캡처 스텁, 보조 모델 실패 스텁)이 정적 폴백 경로를 유도; 제로 네트워크, 윈도우 32 000 (축소), MAIN_LLM 설정 누락 시 스킵 |
| `tests/agent/middlewares/message_persistence/` | 31 | 경계 + 도구 반환 증분 플러시: 각 메시지 정확히 1회, 경계 넘어 중복 없음, 영속 워터마크로 재시작 리플레이 시 행 수 불변, 동기 + 비동기 훅, session_id 부재 시 건너뜀, HITL 거부 페어 재장착, 필터 의미론; 추가로 T1/T2/T3 압축 경로의 제로 쓰기 증명과 요약 쌍(양쪽 절반)의 제로 쓰기 증명 |
| `tests/agent/middlewares/test_compression_nudges.py` | 2 | 압축 시점 nudge 디스패치: memory review + plan extraction이 compact 경로에서 발화, 컷 없는 압축은 아무것도 디스패치하지 않음 |
| `tests/context_engine/store/test_persisted_message_ids.py` | 3 | 영속 워터마크 저장소: 멱등 마킹, 세션 격리, 세션 삭제 시 정리, 빈 입력 no-op |
| `tests/context_engine/store/test_interrupt_marker_approach.py` | 11 | 마커 의미론: 요약 쌍은 이후 압축에서도 생존; FACT C 픽스처 (윈도우 26 000 → usable 10 000, 트렁케이트 라인 7 000) |

전체 프로세스 격리 스위트(`uv run python tests/run_tests_split.py`) 통과: **4268 passed / 12 skipped / 0 failed** (GROUP A 3282P/1S + GROUP B 913P/11S + GROUP C 73P).

## ⚠️ 정직함과 한계

- **`keep=("messages", 10)`은 받아들여지지만 사용되지 않습니다.** 생성자는 API 호환성을 위해 저장할 뿐; 꼬리 보존은 예산 기반(`PRESERVE_RATIO` × 윈도우, [2 000, 15 000] 클램프)에 라우터의 `TRUNCATABLE_RECENT_SKIP` 마진을 더한 것입니다. `keep`을 바꿔도 효과가 없습니다.
- **문서 장식용 임포트.** `summarization/core.py` 상단의 `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, `AUTO_CONTINUE_PROMPT`는 임포트되지만 절대 읽히지 않습니다. `DEGRADATION_MONITOR_COUNT`와 `FILE_OPS_SECTION_MAX_CHARS`는 `config/features/agent_side/summarization.py`의 `SUMMARIZATION` TypedDict에 정의되지만 소비자가 없습니다.
- **TTL 레지스트리는 프로덕션에 연결되어 있지 않습니다.** `record_first_seen` / `select_expired` / `truncate_expired`(및 `PRUNE_TTL_SECONDS`, `TTL_REGISTRY_MAX_ENTRIES`)는 테스트만 소비합니다; 미들웨어는 오직 `truncate_to_budget`만 사용합니다. `agent/` 전역 grep에서 TTL 트리오의 프로덕션 호출 지점은 발견되지 않습니다. 레지스트리는 또한 휘발적입니다(인메모리, `tool_call_id` 키, 재시작 시 소실).
- **남아 있지만 비활성인 코드.** `_preemptive_check`(overflow.py:148)와 `_preemptive_truncate`(compression.py:420)는 참조 전용입니다: 이들이 구현하는 2-밴드 선점에 도달하는 프로덕션 호출 지점은 없습니다.
- **추정기는 토크나이저가 아니라 3단계 토크나이저프리 휴리스틱입니다.** API 보고 사용량이 있으면 T1이 반환하고, T2는 CJK 인식 휴리스틱(CJK 문자 `CHARS_PER_TOKEN_CJK = 2`, 나머지 `CHARS_PER_TOKEN = 4`), T3의 레거시 `len // 4`는 T2의 순수 ASCII 퇴화 케이스입니다. 의도적으로 결정론적(재현 가능한 테스트, 안정적 예산)입니다; `CHARS_PER_TOKEN_CJK = 2`는 중국어가 4가 아닌 1–2자/토큰에 가까움을 반영합니다.
- **보고된 사용량이 이기는 곳.** T3만이 보고된 사용량 기반 트리거입니다(`compute_pressure`는 max를 취함). T1/T2 라우트 결정은 추정 기반입니다(추정치 + 시스템 프롬프트 오버헤드만); 레거시 `_check_trigger` 절 폴백은 `max(로컬 추정치, 보고값)`을 사용합니다.
- **T3는 반환되는 응답을 절대 바꾸지 않습니다.** T3 디스패치의 지속 효과는 도구 결과의 제자리 트렁케이션(메시지 객체는 그래프 상태와 공유됨)과 안티-스래싱 장부 기록뿐입니다; T3 compact 라우트의 `request.override`는 로컬이며 원본 응답이 항상 반환됩니다. T3 본문 전체가 fail-open입니다.
- **T4/T5는 설계상 안티-스래싱 매트릭스를 우회합니다** — 그것이 "강제"의 요점입니다. `MAX_OVERFLOW_RETRIES (3)`(T4/T5 단일 공유 카운터, 턴마다 리셋) 초과, 또는 강제 압축 단계 자체의 실패 시, 원본 프로바이더 예외가 전파됩니다(절대 삼켜지지 않고, 절대 압축 에러로 대체되지 않음).
- **압축은 fail-open입니다.** `_apply_compression` 내부의 어떤 예외도 로그되고 삼켜집니다; 턴은 압축되지 않은 히스토리로 진행됩니다.
- **정적 폴백은 휴리스틱입니다.** 키워드 기반 결정/완료 분류와 원시 도구 인자에서의 경로 추출은 최선의 노력입니다; 섹션 골격은 보장되지만 콘텐츠 품질은 아닙니다.
- **구조화 티어가 `json_mode`인 이유는 설정된 엔드포인트가 그것을 요구하기 때문입니다.** `glm-5.3-flash`(openai 호환)는 `with_structured_output`의 함수 호출을 내보내지 않아 기본 function-calling 티어가 Pydantic 파싱 오류가 됩니다; `_structured_runnable`은 `method` 인자를 받지 않는 프로바이더를 위해 인자 없는 호출로 물러서고, 나머지는 `json_repair`와 free-form 티어가 덮습니다. 코드층 cap과 렌더러가 출력 형태를 보장합니다.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 태그/`lc_source="summarization"`은 하중을 지지는 정확한 문자열입니다.** 이후 턴의 체이닝(`_extract_previous_summary`), 프루닝 정지 조건, 테스트 스위트 전체가 이들을 문자 그대로 매칭합니다 — 함부로 다시 표현하지 마십시오.
