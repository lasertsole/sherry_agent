# 🗜️ 컨텍스트 압축: Summarization 미들웨어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 긴 대화를 모델의 컨텍스트 윈도우 안에 유지하는 방법: 다섯 개의 트리거 지점이 전체 라이프사이클(턴 시작 전, 모든 모델 호출 전, 모든 모델 응답 후, 프로바이더 오버플로 에러 시)을 감시하고, 순수 함수형 4-경로 라우터가 가장 저렴한 수리책을 고르며(큰 도구 결과와 과도하게 큰 도구 호출 인자를 먼저 잘라내고, 강제될 때만 AI 압축), 안티-스래싱 가드가 압축이 통제 없이 불어나는 일을 원천 차단합니다.

사실상의 기준(source of truth): `agent/middlewares/summarization/core.py`, `agent/middlewares/summarization/plan_context.py`, `pub/func/message/overflow_router.py`, `pub/func/message/tool_result_ttl.py`, `pub/func/message/llm_error_classifier.py`, `pub/func/estimate_tokens.py`, `pub/func/message/tool_output_dedup.py`, `pub/func/message/tool_output_prune.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `pub/func/message/turn_utils.py`, `config/features/agent_side/summarization.py`, 그리고 두 등록 지점 `agent/core.py`와 `agent/tools/subagent/spawn/core.py`. 이 문서의 모든 줄 번호와 상수는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [라이프사이클: 다섯 개의 트리거 지점 (T1–T5)](#-라이프사이클-다섯-개의-트리거-지점-t1t5)
- [4-경로 오버플로 라우팅 결정](#-4-경로-오버플로-라우팅-결정)
- [토큰 추정 (토크나이저 없음)](#-토큰-추정-토크나이저-없음)
- [트렁케이트 트랙: 예산 트렁케이션과 TTL 모듈](#-트렁케이트-트랙-예산-트렁케이션과-ttl-모듈)
- [컴팩트 트랙: `_apply_compression` 내부](#-컴팩트-트랙-_apply_compression-내부)
- [LLM 요약: 프롬프트, 체이닝, 폴백](#-llm-요약-프롬프트-체이닝-폴백)
- [정적 폴백 (LLM 없는 요약)](#-정적-폴백-llm-없는-요약)
- [출력: 요약 메시지 쌍](#-출력-요약-메시지-쌍)
- [안티-스래싱 가드 매트릭스와 성능 저하 복구](#-안티-스래싱-가드-매트릭스와-성능-저하-복구)
- [시스템 프롬프트 갱신](#-시스템-프롬프트-갱신)
- [등록 지점](#-등록-지점)
- [설정 참조](#-설정-참조)
- [테스트](#-테스트)
- [⚠️ 정직함과 한계](#%EF%B8%8F-정직함과-한계)

## 🎯 개요

`Summarization`(`agent/middlewares/summarization/core.py`, 클래스는 500행)은 **처음부터 직접 구현한** `AgentMiddleware`입니다 — LangChain 내장 `SummarizationMiddleware`를 상속하지 **않습니다**. 에이전트 라이프사이클의 정확히 두 지점에만 훅을 겁니다:

- `before_agent` / `abefore_agent`(1946 / 1950행) — **T1 사전 점검**
- `wrap_model_call` / `awrap_model_call`(1960 / 2046행) — **T2 디스패치, T3 응답 후 재확인, T4/T5 에러 복구 링**

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
| 메인 에이전트(`agent/core.py:152`) | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| 워커/서브에이전트(`agent/tools/subagent/spawn/core.py:755`) | `("messages", 40)` **또는** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`(기본값) |

둘 다 `main_llm_context_window=main_llm_max_tokens`(`MAIN_LLM_MAX_TOKEN`에서 유래)와 `keep=("messages", 10)`을 전달합니다.

## 🧭 라이프사이클: 다섯 개의 트리거 지점 (T1–T5)

```
턴 시작
│
├─ T1  before_agent 사전 점검  (_t1_preflight :1886 / _at1_preflight :1917)
│      ├─ _reset_turn_state (:1849)가 11개의 턴 단위 카운터를 리셋
│      ├─ _decide_overflow_route (:632) → None / "fits" → 통과
│      ├─ 쿨다운 > 0이면 COMPACT 라우트 차단; 트렁케이트 트랙은 여전히
│      │  실행 (그 자체가 가장 저렴한 복구 메커니즘)
│      └─ 디스패치(trigger="T1") + _t1_state_update (:1862)가 결과를
│         그래프에 커밋:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         (add_messages 리듀서는 스스로 메시지를 지우지 않는다 —
│         RemoveMessage 센티널이 압축된 접두부가 상태를 실제로
│         떠나는 유일한 통로)
│
├─ T2  wrap_model_call, 핸들러 이전 (:1960 동기 / :2046 비동기)
│      ├─ force 플래그(:1973)를 스킵 게이트보다 먼저 읽음 — 스킵 게이트
│      │  (_should_skip_compression :1255)가 플래그를 소비함
│      ├─ _tick_cooldown(:832): 모든 호출이 쿨다운을 감소
│      ├─ 안티-스래싱 게이트(:1984–1987):
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      통과 (직전에 compact가 있었다면 시스템 프롬프트 재구축,
│      │      :1993–2005) → 핸들러 → 모니터 → T3
│      ├─ 아니면: 4-경로 결정(:2019) → _dispatch_overflow_route;
│      │  레거시 트리거 절이 발동하면(_check_trigger :576, 예:
│      │  ("messages", 40)) → ROUTE_COMPACT_ONLY(:2027)
│      └─ 세 곳의 핸들러 호출부(:1979, :2005, :2030)는 모두
│         _execute_with_recovery(:1057) 안쪽에서 실행 — 즉 T4/T5 링
│
├─ T3  응답 후 재확인  (_post_response_check :849 / 비동기 :922)
│      ├─ T2가 이번 wrap 호출에서 이미 압축했다면 건너뜀(t2_compressed
│      │  플래그, :2032–2035) — 모델 호출당 압축은 정확히 한 번
│      ├─ extract_reported_input_tokens(response)(:130); None → 반환
│      ├─ 게이트: 턴 시도 상한, 쿨다운, 사용 가능 예산
│      ├─ pressure = max(추정치 + 시스템 프롬프트, 보고값) —
│      │  프로바이더가 보고한 입력 토큰이 우선(compute_pressure)
│      ├─ pressure < usable × 0.80 → 반환; 라우트가 "fits" → 반환
│      └─ 디스패치(trigger="T3")하고 항상 원본 응답을 반환; 함수 전체가
│         fail-open(예외 발생 → 로그, 원본 응답 그대로 유지)
│
└─ T4/T5  프로바이더 에러 복구 링
       (_execute_with_recovery :1057 / _aexecute_with_recovery :1115)
       ├─ 핸들러가 예외를 던짐 → classify_provider_error
       │  (pub/func/message/llm_error_classifier.py):
       │  payload_too_large → T4, context_overflow → T5
       │  (_TRIGGER_BY_ERROR_CLASS :115, _RETRY_KEY_BY_ERROR_CLASS :119)
       ├─ 대상 외 / 미분류 → 원본 예외를 그대로 재던짐(재시도 0회,
       │  상태 기록 0건, 절대 삼키지 않음)
       ├─ 재시도 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  (:985 / 비동기 :1030): LLM을 부르지 않는 테일 클립이 먼저 실행됨 —
       │  클립만으로 추정치가 사용 가능 예산 아래로 떨어지면 핸들러가
       │  스텁된 테일 결과로 재시도되고 compact는 전혀 일어나지 않음;
       │  이미 스텁된 요청에서는 클립이 no-op이 되므로 다음 시도는
       │  "compact + 예산 트렁케이션" 단계로 퇴화함. 이 단계는 구조상
       │  모든 안티-스래싱 게이트를 우회(쿨다운, 턴당 상한,
       │  _should_skip_compression 모두 consult하지 않음); 쿨다운을
       │  무장하지 않고 턴 시도도 세지 않지만, 세션 통계의 진실성을 위해
       │  _record_compression은 거침; 클래스별 재시도 카운터는 성공
       │  후에만 증가(:1021)
       ├─ 재시도 소진 → 원본 예외 재던짐(에러 프레임은 messages.py →
       │  turn_runner.py로 전파 — 절대 빈 응답으로 대체되지 않음)
       └─ 강제 압축 단계 자체가 실패 → 원본 예외 재던짐
          (raise exc from compression_exc). _monitor_degradation은
          링이 반환한 뒤 최종 성공 응답에 한 번만 실행.
```

레거시 트리거 절은 여전히 T2의 폴백으로 존재합니다(`_check_trigger`, :576): `("messages", N)`은 히스토리 길이로, `("tokens", N)`은 `max(로컬 추정치, 마지막 AIMessage의 보고된 usage_metadata.total_tokens)` ≥ N으로 발동합니다. 절 목록은 OR 관계입니다.

## 🚦 4-경로 오버플로 라우팅 결정

`pub/func/message/overflow_router.py`는 **순수 결정 레이어**입니다 — 잘라내기도, 압축도, I/O도, 상태도 없습니다. 미들웨어는 여기서 세 함수를 임포트합니다:

- `compute_pressure`(:50) = `max(estimated_tokens + system_prompt_tokens, reported_tokens)` — API가 보고한 수치가 있으면 그것이 우선;
- `find_truncatable_tool_results`(:68) — **`ToolMessage`만** 후보 자격이 있음(도구 결과는 재생성 가능); 최근 `TRUNCATABLE_RECENT_SKIP (6)`개 메시지는 항상 제외되어 최신 tool/ai 페어링이 온전히 유지됨; 후보는 최소 `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200)` 추정 토큰 이상이어야 함; 결과는 토큰 내림차순 정렬이라 실행자가 가장 큰 이득부터 자름;
- `decide_route`(:103) — 디스패치 계약(문자열 고정):

| 압력(`p`) vs `usable` | 트렁케이트 후보 없음 | 후보 존재 | 후보 토큰 합 vs 오버플로(`p − usable`) |
| :------------------------- | :------------------------ | :--------------- | :--------------------------------------------- |
| `p < 0.70 × usable` | `fits` | `fits` | — |
| 소프트 오버플로 `0.70 × usable ≤ p < 0.80 × usable` | `fits` | `truncate_tool_results_only` | — (소프트 오버플로만으로는 압축이 **절대** 트리거되지 않음) |
| 하드 오버플로 `p ≥ 0.80 × usable` | `compact_only` | 합 ≥ 오버플로 → `truncate_tool_results_only`; 합 < 오버플로 → `compact_then_truncate` | 오버플로 = `p − usable` |

세 임계값 입력은 모두 원시 윈도우가 아니라 **사용 가능 예산**에서 파생됩니다:

```
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :615
system_est     = estimate_text_tokens(system_prompt)   # _estimate_system_prompt_tokens :770
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

단일 실행자 `_dispatch_overflow_route`(:760 동기 / :800 비동기)가 T1, T2 **및** T3를 모두 서비스합니다 — 두 번째 복사본은 절대 없습니다:

- `truncate_tool_results_only` → `_run_budget_truncation`(:659) — 1단계는 과도하게 큰 도구 호출 인자를 잘라내고(새 메시지 반환, 아래 트렁케이트 트랙 참조), 2단계는 도구 결과를 제자리에서 잘라냄 — 이후 **재확인**: 확보한 토큰이 부족하면(`new_tokens ≥ usable × 0.80`, 반환된 목록 기준으로 추정) `compact_then_truncate`로 승격; 아니면 압축 없이 통과;
- `compact_only` / `compact_then_truncate` → `_execute_compact`(:702 / 비동기 :731) → `_apply_compression`(예외는 로그, 요청은 그대로) → `_record_compaction_bookkeeping`(:694: 쿨다운 무장, 턴 시도 1회 기록) → `compact_then_truncate`는 압축 결과에 예산 트렁케이션을 백스톱으로 한 번 더 실행 → 구/신 토큰과 압력 비율과 함께 라우트 로깅.

**P1-2 빠른 경로 — LLM을 부르지 않는 테일 클립.** 어떤 라우트가 실행되기 전에 `_fast_tail_clip`이 `clip_overflow_tail`(`pub/func/message/overflow_clip.py`)을 실행합니다: 꼬리에 연속된 `ToolMessage` 배치가 압축 스텁으로 대체되며, 대체는 `ToolMessage.model_copy`를 거치므로 메시지가 삭제되거나 주입되지 않습니다 — `id`, `tool_call_id`, `name`, `additional_kwargs`가 모두 살아남아 페어링 새니타이즈와 영속 워터마크가 계속 충족됩니다. 예산: `target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`; `0` 타깃은 "최대 적격 배치를 취함"을 뜻합니다(`overflow_clip_max_remove`가 상한, `overflow_clip_min_keep`이 하한). `ratio`는 라우트 경로에서 `COMPRESSION_TRIGGER_RATIO (0.80)`, T4/T5 강제 단계에서 `1.0`(사용 가능 예산 아래)입니다. 클립 **단독**으로 추정치가 경계선 아래로 떨어질 때만 채택됩니다: 요청은 스텁된 리스트 그대로 반환되고 라우트는 전혀 실행되지 않습니다(예산 트렁케이션 없음, 보조 LLM 압축 없음). 불충분한 클립은 버려지고 기존 라우트가 원본 리스트 그대로에서 실행됩니다. T4/T5에서는 클립이 `request.messages`를 읽습니다; 이미 스텁된 요청에서는 no-op이 되므로 동일 클립으로 재시도 예산이 소모되지 않고 다음 시도는 압축으로 퇴화합니다.

**꼬리 내용을 버려도 안전한 이유:** 모든 도구 결과는 반환되는 순간 MesMemory에 플러시되었고(`MessagePersistenceMiddleware`), `message_search` 도구로 계속 조회할 수 있습니다. P0-2로 축출된 결과는 스텁 안에 `[evicted to: …]` 포인터를 유지하며(`read_file`이 계속 동작), P2-4로 슬라이스된 `read_file` 결과는 슬라이스 안내를 그대로 유지합니다; 그리고 스텁된 메시지는 스캔 대상 배치를 종료시키므로 두 번째 클립은 no-op이며 이 마커들을 절대 파괴하지 않습니다. 설정: `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`.

윈도우 산술(테스트 계약): 윈도우 `41 600` → usable `25 600`, 두 경계선 `17 920` / `20 480`, 트렁케이트 예산 `15 360`. 테스트 고정값 `MAIN_LLM_MAX_TOKEN = 65536`일 때(런타임 `.env` 값은 131072 / 128K 이상 필요) 등록된 T2 절은 `52 428`에 놓입니다.

## 🪙 토큰 추정 (토크나이저 없음)

`pub/func/estimate_tokens.py`(109행)는 의도적으로 토크나이저 없이 결정론적으로 동작하며, 3단계 폴백을 가집니다:

- **T1 — API 보고 사용량:** 마지막 `AIMessage`가 `usage_metadata`를 가지면(또는 호출자가 `reported_tokens`를 명시하면) `estimate_messages_tokens`가 그 값을 그대로 반환합니다 — provider의 실측값이 모든 로컬 추정을 단축합니다;
- **T2 — CJK 인식 휴리스틱:** `estimate_text_tokens`는 텍스트를 CJK 문자(`// CHARS_PER_TOKEN_CJK = 2`)와 나머지(`// CHARS_PER_TOKEN = 4`)로 나누고, 감지에는 `pub.func.cjk.count_cjk`를 재사용합니다;
- **T3 — 레거시 `len // 4`:** 별도 코드 경로가 아니라 `count_cjk(text) == 0`일 때의 T2 퇴화 케이스입니다 — 따라서 순수 ASCII 추정치는 기존 숫자와 정확히 일치합니다.

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# 메시지 단위: str content(또는 len(json.dumps(content)))
#           + Σ tool_call name/args 문자 + tool_call_id 문자
```

`pub/func/message/estimate_msg_tokens.py`는 이제 같은 헬퍼들의 하위 호환 re-export입니다. 빠르고, 실행 간 안정적이며(같은 입력 → 같은 숫자 → 재현 가능한 테스트), 의도적으로 보수적으로 근사합니다. 트리거/예산 경로의 어떤 것도 모델 토크나이저에 의존하지 않습니다.

## ✂️ 트렁케이트 트랙: 예산 트렁케이션과 TTL 모듈

`_run_budget_truncation`(:659) 안에서는 두 개의 트렁케이션 레이어가 순서대로 돕니다:

**1단계 — 도구 호출 인자**(`pub/func/message/tool_args_truncate.py`): JSON 직렬화가 `MIN_ARGS_CHARS_TO_TRUNCATE (500)`자를 넘는 모든 `AIMessage.tool_calls[].args` — 해당 도구가 `PROTECTED_TOOLS`에 없는 경우 — 는 `MAX_TOOL_ARGS_CHARS (2_000)`자로 봉인되는 `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}`로 대체됩니다(머리 30% / 꼬리 30%, 도구 결과와 같은 비율). 덕분에 `args`는 dict로 남고(LangChain의 `ToolCall.args` 타입), 모든 프로바이더 어댑터에서 JSON 직렬화 가능하며, 모델은 인자가 잘렸음을 볼 수 있습니다. 최근 `TRUNCATABLE_RECENT_SKIP (6)`개 메시지는 건너뛰고, 대체된 `AIMessage`는 `model_copy` 클론입니다 — tool_call_ids는 절대 건드리지 않으므로 AIMessage↔ToolMessage 페어링은 온전히 유지됩니다.

**2단계 — 도구 결과**: `pub/func/message/tool_result_ttl.py`는 트렁케이트 트랙이 사용하는 제자리 절단을 제공합니다. 설계 불변식(하중 지지):

- **제자리만** — 이 모듈은 메시지를 절대 삭제, 재정렬, pop하지 않습니다; `msg.content`(또는 content 리스트 블록)만 수정하고 인덱스를 반환합니다. 이것이 프로바이더 API와 `ToolCallNormalize`가 의존하는 tool-call/`ToolMessage` 페어링을 보존합니다.
- **비어 있지 않은 플레이스홀더** — 잘려나간 결과는 항상 비어 있지 않은 내용을 유지합니다: `ToolCallNormalize.before_model`은 **빈 `ToolMessage`를 드롭**해서 트랜스크립트를 정화하므로, 빈 플레이스홀더는 조용히 페어링을 깨뜨립니다.
- **머리 30% / 꼬리 30% 보존**(`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`)에 생략 마커를 붙입니다.

미들웨어가 실제로 소비하는 것: `truncate_tool_args`(1단계, 인자)와 **`truncate_to_budget`**(2단계, 도구 결과) — 라우터의 후보 목록으로 구동되며, `_run_budget_truncation`(:659)이 예산(`usable × TRUNCATE_BUDGET_RATIO`)에 맞을 때까지 후보를 자릅니다. 1단계는 변경하지 않고 새 `AIMessage`를 반환하므로, 이 함수는 최종 목록을 반환하고 모든 호출자는 그 목록을 `request.override`에 **반드시** 넣어야 합니다.

**read_file 결과는 복구 가능하게 유지됩니다**: `pub/func/message/target_truncation.py`의 머리+꼬리 클립(비 LLM 전략 `_run_non_llm_strategies`가 실행)은 각 `ToolMessage`를 `tool_call_id`로 AIMessage 도구 호출에 되짚습니다; 도구가 `read_file`이고 `args.file_path`가 있으면 잘린 중간은 익명 마커 대신 복구 안내로 대체됩니다. 안내는 같은 머리 30% / 꼬리 30% 비율을 유지하고 원본 `file_path`를 명시하며 `Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)`를 제시합니다. `N`은 **머리에 완전히 남지 않은 첫 행의 절대(1-based) 파일 행 번호** — 따라서 `offset=100`으로 읽은 페이지는 머리가 실제로 멈춘 지점부터 이어지고, 중간에 잘린 행은 다시 읽히며 절대 건너뛰지 않습니다. offset을 도출할 수 없으면(페이로드가 read_file JSON 결과가 아니면) 안내는 `offset=1`부터 다시 읽기를 요청합니다 — 추측 offset은 내지 않습니다. 다른 모든 도구는 익명 `...[truncated N chars]...` 마커를 바이트 단위로 유지합니다.

TTL 레지스트리 자체(`record_first_seen` / `select_expired` / `truncate_expired`, `PRUNE_TTL_SECONDS = 300`, `TTL_REGISTRY_MAX_ENTRIES = 512`, `tool_call_id` 키, 재시작 시 휘발)는 오늘날 **테스트 스위트만 사용**합니다 — 미들웨어에는 나이 기반 만료 로직이 연결되어 있지 않습니다("정직함과 한계" 참조).

## 🔁 컴팩트 트랙: `_apply_compression` 내부

`_apply_compression`(:1688; 비동기 쌍둥이 :1760)은 다음 순서로 실행됩니다:

1. **복구 컨텍스트 캡처**(`_capture_recovery_context`, :1577): 마지막 사용자 요청(≤ 800자)과 파일 작업 래칫 — `read`/`write` 계열 도구 호출에서 경로를 추출하고(:415), 이전 라운드의 집합과 병합(읽은 것은 기억되고, 수정된 파일이 읽기 전용으로 강등되는 일은 없음).
2. **비 LLM 전략**(`_run_non_llm_strategies`, :1493): `중복 제거 → 프루닝 → 타깃 트렁케이트 → 도구 인자 트렁케이트`(상세는 아래). 이것들은 공짜입니다 — 모델 호출 없음.
3. **LLM 사용 여부 결정**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   비 LLM 축소가 첫 기회를 얻습니다; 히스토리가 여전히 보존 예산의 두 배를 넘을 때(또는 거버너가 LLM 요약을 비활성했거나, 비 LLM 전략이 아무것도 줄이지 못했을 때)에만 보조 LLM을 씁니다.
4. **공격적 백스톱**(`_aggressive_truncate`, :1539): 결과가 *그래도* 너무 크면, `AGGRESSIVE_TRUNCATE_CHARS (1 000)`자를 넘는 모든 `ToolMessage`가 마커와 함께 하드 컷됩니다 — 같은 상한을 넘는 모든 도구 호출 인자 JSON도 마찬가지입니다(머리만, `PROTECTED_TOOLS` 면제, `{"_truncated_args": ...}`로 대체).
5. **요약 자체 절단**(`_truncate_summary_messages`, :1630): `SUMMARY_TOTAL_MAX_CHARS (16 000)`자를 넘는 기존 요약 메시지(`lc_source == "summarization"`)는 머리 30% / 꼬리 30%로 재절단됩니다(`_truncate_content`, :1622).
6. **복구 주입**(`_inject_recovery_context`, :1595): 캡처한 파일 작업 래칫이 요약의 `## Relevant Files` 섹션으로 재작성되어, 체크포인트가 항상 최신 읽기/수정 파일 맵을 품도록 합니다.
7. **장부 기록**(`_record_compression`, :1277), 마지막으로 `request.override(messages=..., system_message=...)`.

### 💾 압축 시점 nudge

메시지 영속화는 압축 경로 밖에서 동작합니다: `MessagePersistenceMiddleware`(`agent/middlewares/message_persistence/`)가 새 human/AI 메시지를 각 모델 호출 경계에서, 도구 결과를 반환 시 MesMemory로 플러시하고, 영속 워터마크 `persisted_message_ids`로 write-once를 보장합니다. compact는 압축과 아래 nudge 스케줄만 담당합니다. 트리거 의미론은 `agent/middlewares/README.md`를 참조하세요.

**압축 시점 nudge**(`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`): 메모리 리뷰(`_nudge_memory`)는 압축마다 디스패치됩니다; 플랜 추출은 같은 시점에 `_detect_todo_all_complete`를 평가합니다. 둘 다 NUDGE 레인에서 fire-and-forget으로 디스패치되어 모델 호출을 막지 않습니다. nudge 락이 잡혀 있는 동안 압축은 디스패치를 완전히 건너뜁니다(큐잉 없음). 단발 `nudge_plan_extraction_fired` 플래그는 완료 사이클당 1회 추출을 보장하며, 한 번도 압축하지 않는 세션은 플랜 추출을 발화하지 않습니다.

**절단점 선택**(`_determine_cutoff`, :1310): 히스토리를 턴으로 쪼개고, **최신에서 거꾸로** 걸으며 보존 예산 `clamp(window × 0.25, 2 000, 15 000)`(`_calculate_preserve_budget`, :565)에 맞춰 누적합니다; 통째로 안 들어가는 턴은 턴 중간에서 쪼개질 수 있습니다. `_adjust_for_orphan_pairs`(:1340)가 절단점을 거꾸로 걸어 `ToolMessage`가 `AIMessage` 도구 호출과 떨어지는 경우가 없도록 합니다. 마지막 턴 비율 게이트가 발동하지 않는 한(마지막 사용자 턴 ≥ 전체 토큰의 `LAST_TURN_RATIO_THRESHOLD (0.5)` — `_check_last_turn_ratio`, wrap 진입 :1968/:2054에서 호출), 절단점은 마지막 `HumanMessage`를 넘지 않습니다.

모든 실패 모드는 fail-open입니다: `_apply_compression`이 예외를 던지면 로그만 남기고 원본 요청이 그대로 진행됩니다 — 깨진 압축이 턴을 망치는 일은 없습니다.

## 📝 LLM 요약: 프롬프트, 체이닝, 폴백

`_create_summary` / `_acreate_summary`(:1410 / :1435):

1. **직렬화**(`_serialize_for_summary`, :258): 각 메시지가 태그 붙은 한 줄로 변합니다 — `[User]:`(≤ 2 000자), `[Assistant]:`(≤ 2 000자), `[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`, `[Tool result|Tool error] (id):`(> 2 000자 → 1 800자 보존 + 생략 마커).
2. **이전 체크포인트 체이닝**(`_extract_previous_doc` / `_extract_previous_summary`): `additional_kwargs["lc_source"] == "summarization"`인 가장 최신 `AIMessage`를 찾아 먼저 구조화 `summary_doc` 페이로드로 읽습니다(`<prior-summary>`용으로 Markdown 재렌더링). 페이로드가 없는 메시지 — 레거시 세션, 또는 free-form으로 폴백한 회차 — 는 기존대로 `<summary>…</summary>` 본문에서 파싱합니다. 이전 Doc이 있으면 프롬프트가 `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED`가 됩니다. 레거시 Markdown은 `<prior-summary>`로 그대로 주입되며, 업그레이드 후 첫 압축 라운드가 곧바로 새 `SummaryDoc`을 출력합니다(마이그레이션 불필요).
3. **구조화 출력**(`summary_doc.py::SummaryDoc`): 보조 모델은 `with_structured_output(SummaryDoc, method="json_mode")`로 래핑됩니다. 설정된 `glm-5.3-flash` 엔드포인트는 함수 호출 스키마를 무시하므로(기본 method는 자유 텍스트를 반환해 Pydantic 파서가 거부 — 실측 완료) `json_mode`가 주 티어입니다. 파싱/검증 실패는 원시 호출 + `json_repair`(`_sync_json_repair_doc` / `_async_json_repair_doc`)로 강등되고, 그것도 실패하면 레거시 free-form Markdown 프롬프트가 마지막 LLM 티어이며 정적 폴백이 최종 가드입니다.
4. **호출**은 보조 모델에 `config={"metadata": {"lc_source": "summarization"}}`로 수행되어, 다운스트림 도구 체인이 요약 호출을 식별할 수 있게 합니다.
5. **가드 레일:** free-form 응답이 비어 있거나 지나치게 짧으면 결정론적 요약으로 폴백하고, 예외도 마찬가지입니다. 실패 시 LLM이 최후의 발언권을 가지는 일은 없습니다.

**렌더링(Doc → Markdown).** `render_summary_markdown`은 순수·결정론적입니다(같은 Doc → 같은 바이트, 프리픽스 캐시 안전). 기존 섹션 골격을 출력하고, 배열이 비어 있지 않을 때만 *Active Plan Notes* / *Evicted References*를 덧붙입니다:

| `SummaryDoc` 필드 | 렌더링되는 섹션 | 코드층 cap |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | 그대로, 상한 없음 |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes`(비어 있지 않을 때만) | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References`(비어 있지 않을 때만) | `evicted_refs[-20:]` |

cap은 `cap_summary_doc`의 배열 슬라이스(체인에 저장되는 형태)이고, 렌더러는 표시 시 다시 슬라이스해 `"(N earlier items omitted for brevity)"`를 덧붙입니다. `evicted_refs`는 코드가 유지합니다: `_collect_evicted_refs`가 압축 범위의 `[evicted to: <path>]` 마커와 인간 메시지의 `lc_evicted_to` 태그를 스캔하고, `_finalize_summary_doc`이 이전 Doc 항목과 새 항목을 순서대로 중복 제거해 병합합니다. `_inject_recovery_context`는 파일 작업 래칫을 렌더링된 `## Relevant Files` 섹션과 저장 Doc의 `relevant_files` 필드 양쪽에 되씁니다.

### 🗂️ Active Plan Notes와 계획 컨텍스트 주입

압축은 계획을 인식합니다. 보조 모델 호출 전에 `_get_plan_context_sync(session_id)`(`core.py`, TaskFlow 블록 옆)가 이 세션이 실행 중인 계획을 권위 블록으로 렌더링합니다:

- 계획 활성 판정은 `agent/tools/todolist/knowledge/ownership.py`의 두 원시 연관 소스를 재사용합니다 — 세션의 `plan_ref` state 키와 이 세션의 todo 중 하나에 있는 `plan_ref`(`plan_context.py::resolve_active_plan`); **boulder 소스는 의도적으로 제외**됩니다.
- 주입 내용은 **계획 파일 상대 경로 + 계획 이름 + 미완료 todo 요약**이며, 계획 본문은 절대 주입하지 않습니다(모델이 경로를 `read_file`할 수 있음). todos가 모두 `completed` / `cancelled`인 세션은 **활성 계획이 아닙니다**.
- 이 블록은 두 프롬프트 경로(최초 요약과 체인 업데이트, 구조화와 레거시 free-form) 모두에 주입됩니다.

`SummaryDoc.active_plan_notes`는 모델이 아니라 파이프라인이 소유하므로, 계획 범위의 교훈은 계획이 활성인 동안 계속 유지됩니다:

- 체인 업데이트에서 이전 Doc의 항목을 **그대로, 순서대로 상속**하며, 모델은 새 한 줄 교훈(`증상 -> 회피책`)을 덧붙이는 것만 할 수 있습니다.
- 배열 상한은 `active_plan_notes[-20:]`; 체인 렌더는 `(N earlier items omitted for brevity)`를 덧붙이고 저장 페이로드는 잘라낸 꼬리를 유지합니다.
- 계획이 완료되면(todos가 모두 `completed` / `cancelled`) 리졸버가 "활성 계획 없음"을 보고하여 렌더에서 `## Active Plan Notes` 섹션이 사라지고 다음 Doc의 배열은 비워집니다. `plan_ref`가 전혀 없는 세션도 마찬가지로 배열을 갖지 않습니다.

**최신 사용자 요청 그대로 + 퇴출 포인터.** *Latest Unresolved User Request* 섹션은 결코 잘리지 않습니다: `latest_user_request`는 그대로 제시되며(레거시 템플릿의 `max 800 chars` 지시도 제거됨), 직렬화되는 `<conversation>`은 **state**에서 옵니다 — 퇴출된 인간 메시지는 state에 전체 텍스트와 `lc_evicted_to` 태그를 유지하고 모델 뷰만 미리보기입니다. 최신 사용자 요청 자체가 퇴출된 경우, 파이프라인이 그 `[evicted to: <path>]` 포인터를 필드에 덧붙입니다(코드 소유, `_latest_human_eviction_ref`). 따라서 요약 체인은 항상 디스크의 전체 텍스트로 돌아가는 길을 보존합니다. 내부 주입(`metadata.internal`)은 이 앵커를 빼앗지 않습니다.

**체이닝 요약 필터링**(`_filter_summary_messages`): 이전 체크포인트가 있으면 그 Human/AI 쌍을 직렬화된 `<conversation>` 입력에서 제거합니다 — 추출된 이전 요약은 `<prior-summary>`(또는 `<prior-summary-json>`)로만 주입되므로, 오래된 요약 텍스트는 프롬프트에 정확히 한 번만 나타납니다. 이 쌍은 **양쪽 모두** `additional_kwargs={"lc_source": "summarization"}`을 가지며, 이는 양쪽 모두 MesMemory에 들어가지 않게 합니다(`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`): 이 쌍은 압축의 내부 산출물이지 대화 기록이 아닙니다. 필터는 `_extract_previous_doc` / `_extract_previous_summary` **이후**에 실행되어(체이닝은 여전히 이전 요약을 봅니다), 필터된 목록이 직렬화·프롬프트 구성·두 정적 폴백 분기(LLM 실패/너무 짧은 응답)에 쓰입니다 — `_apply_compression_under_lock` / `_aapply_compression_under_lock`의 `skip_llm` 경로도 마찬가지입니다. opencode-dev의 `hidden` 집합, deepagents의 `_filter_summary_messages`와 정렬됩니다.

레거시 프롬프트 템플릿(`_SUMMARY_TEMPLATE`)은 free-form 폴백 골격을 계속 고정합니다 — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress(Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — "비어 있어도 모든 섹션을 유지"와 기밀 규칙("NEVER include API keys, tokens, passwords, secrets")을 요구합니다. 구조화 경로는 Markdown 골격을 `_SUMMARY_JSON_RULES`(JSON 필드 목록 + 동일 기밀 규칙)로 대체합니다. 필드 의미는 Pydantic `SummaryDoc` 모델 자체에 정의됩니다.

**사용자 요청 출처의 정식 식별.** 영속화된 각 `human` 행은 `origin`을 가집니다(전송 진입점에서 각인): WS/채널 사용자 입력은 `"user"`, 오케스트레이터 스티어링 주입은 `"task_intent"`, 완료 캐리어는 `"subagent_completion"`, Cron 전달 턴은 `"cron"`(`ai`/`tool` 행은 `NULL` 유지; origin 태깅 이전 행은 레거시 user 메시지로 읽음). *Latest Unresolved User Request*의 출처는 이 열로 정식 식별됩니다: 사용자 출처만(`origin = 'user'` 또는 레거시 `NULL`) 사용자 요청이며, 내부 주입(`task_intent` / `subagent_completion` / `cron`)은 요청으로 인용되지 않습니다. routing plan의 복수형 `unresolved_user_requests[]` 목록도 동일한 정식 필터를 사용합니다.

## 🧱 정적 폴백 (LLM 없는 요약)

`_build_static_fallback_summary`(:296)는 모델 호출 0회로 같은 섹션 골격을 만듭니다:

- 마지막 사용자 요청 → *Latest Unresolved User Request*; 첫 요청 → *Goal*;
- 결정 키워드(`decided`, `choosing`, `because`, `therefore`)를 포함한 AI 텍스트 → *Key Decisions*, 아니면 *Completed*;
- 모든 도구 호출 → *Completed*; 경로 같은 토큰(`/` 또는 `\` 포함, 또는 `.py`/`.md`/`.js`/`.ts`/`.json`으로 끝) → *Relevant Files*(≤ 10, `http` 링크 제외);
- 에러 `ToolMessage` → *Blocked*와 *Critical Context*.

`skip_llm`이 활성화되면 그대로 사용되고, 짧거나 실패한 LLM 요약의 안전망이기도 합니다.

## 📦 출력: 요약 메시지 쌍

`_build_new_messages`(:1464)는 요약 텍스트를 감싸 정확히 두 메시지를 내보냅니다:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — 역할 교대를 유지하는 중립적 질문; AI 절반과 같은 `lc_source` 마커를 가집니다.
- **AIMessage**, `additional_kwargs={"lc_source": "summarization"}` 포함 — 이후 턴들이 (a) 이전 체크포인트를 찾아 체이닝하고, (b) 체이닝 재요약 입력에서 이 쌍을 제거하고 프루닝이 체크포인트에서 멈추게 하며, (c) 테스트가 대체된 후 요약이 모델 뷰에서 삼켜질 수 있음을 단증하는 마커.
- 이 쌍은 MesMemory에 절대 들어가지 않습니다: `MessagePersistenceMiddleware._is_persistable`이 `lc_source="summarization"` 양쪽 절반을 건너뜁니다(human 행 빌더도 같은 게이트를 가집니다).
- AIMessage는 구조화 문서 본체도 보관합니다: `additional_kwargs["summary_doc"]`(cap 후 `SummaryDoc`, JSON 직렬화 가능한 순수 dict — 체인 캐리어, `<prior-summary-json>`으로 재투입). free-form 폴백 회차는 이 페이로드가 없고 `_extract_previous_summary`가 `<summary>` 본문 파싱으로 폴백합니다.
- 전체 콘텐츠는 `SUMMARY_TOTAL_MAX_CHARS (16 000)`으로 봉인되고, 머리/꼬리 30/30 보존.

## 🛡️ 안티-스래싱 가드 매트릭스와 성능 저하 복구

상태는 세션 범위 `state_register_mem`의 **열세 개** `summarization_*` 키(:92–107)에 살습니다. `_reset_turn_state`(:1849)는 매 턴 시작 시 그중 **열한 개**를 리셋합니다; `summarization_last_user_question`, `summarization_cooldown_rounds`는 의도적으로 턴마다 리셋되지 **않습니다**.

| 가드 | 키 | 임계값 | 효과 |
| :---- | :-- | :-------- | :----- |
| 턴 쿨다운 | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 실제 compact마다 무장(:694); **모든** 모델 호출이 감소(:832); T1 compact 라우트, T2 선제, T3를 차단 — T4/T5 강제 링은 절대 차단하지 않음 |
| 턴당 압축 수 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | :694이 증가; T2 선제 + T3 억제(강제 링은 면제) |
| 오버플로 재시도 (T4/T5 공유) | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | 두 에러 클래스가 공유하며 턴마다 리셋; 성공한 강제 단계마다 증가; 소진 → 원본 프로바이더 에러 전파 |
| 세션 총 압축 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`(:1255)이 True 반환 — 선제 압축 완전 정지 |
| 연속 무효 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` 설정 — 비 LLM 전략만 |
| 유효성 판정 | (`_record_compression`, :1277) | 메시지 수 감소 **또는** 토큰 축소 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 성공한 비 LLM 전략(`dedup`/`prune`/`truncate`/`fallback`/`aggressive`)이 `skip_llm`을 다시 해제 |
| 성능 저하 복구 예산 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 성능 저하 모니터가 시작하는 강제 복구의 상한 |

**성능 저하 모니터**(`_monitor_degradation`, :1661): 이번 호출에서 실제로 압축이 일어났을 때만 consult됩니다(`_compaction_just_happened` 플래그). 모델 응답에 텍스트가 없으면 카운터가 증가하고; `DEGRADATION_NO_TEXT_THRESHOLD (3)`회 연속 빈 응답 — 그리고 `summarization_recovery_attempts < 2`인 동안 — `force_recovery`를 설정하고 무효 연속 기록과 세션 압축 카운트를 지웁니다. 비어 있지 않은 응답은 카운터를 리셋합니다. 이것은 "압축 → 모델 혼란 → 빈 출력 → 재압축"의 병적 루프를 잡습니다. 상호작용에 주의: force 플래그는 wrap 진입(:1973)에서 `_should_skip_compression` **보다 먼저** 읽히고, 스킵 게이트는 카운터를 리셋하고 진행하며 그 플래그를 소비합니다(:1256–1261) — 복구 압축은 정확히 한 번 실행됩니다.

## 🔄 시스템 프롬프트 갱신

메인 에이전트 전용(`need_update_system_prompt=True`): 압축 후 미들웨어가 시스템 프롬프트를 재구축해 `system_prompt` 상태 키에 기록하므로, 다음 모델 호출은 페르소나 파일 / 장기 기억을 지금 현재 상태 그대로 봅니다. 두 전달 경로: 압축 직후의 `request.override(system_message=SystemMessage(...))`, 그리고 — T1 compact가 이미 일어났지만 안티-스래싱 게이트가 두 번째 압축을 막을 때 — 재구축된 프롬프트는 게이트 경로에서 여전히 전달됩니다(:1993–2005). `@dynamic_prompt` 시스템 프롬프트 미들웨어가 없는 체인(서브에이전트 / nudge 파이프라인)은 이 미들웨어가 전달해 주기 때문입니다. 게이트 경로는 요청의 현재 system message와 **내용이 다를 때만** 주입합니다: 내용이 같으면 override도 새 `SystemMessage`도 만들지 않습니다(재주입하지 않음).

## 📌 등록 지점

```python
# agent/core.py:152 — 메인 에이전트 (Summarization은 마지막 미들웨어:
# 가장 안쪽 wrap 레이어, LLM에 가장 가까움)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — 워커 에이전트 (첫 미들웨어)
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```

## ⚙️ 설정 참조

모든 임계값은 `config/features/agent_side/summarization.py`(SUMMARIZATION TypedDict)에 있습니다. ◆ 표시 상수는 살아있는 코드 경로가 소비합니다; ○ 표시 상수는 정의 또는 임포트는 되지만 살아있는 경로는 **소비하지 않습니다**("정직함과 한계" 참조).

| 상수 | 값 | 소비 위치 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route`의 하드 오버플로 밴드; T3 압력 게이트; 두 트리거 절을 구성 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route`의 소프트 오버플로 밴드 |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`(:615): 윈도우 − 예비량 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 트렁케이트 트랙 예산 = usable × 0.60 (:680) |
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
- **남아 있지만 비활성인 코드.** `_preemptive_check`(:589)와 `_preemptive_truncate`(:1159)는 참조 전용입니다: 이들이 구현하는 2-밴드 선점에 도달하는 프로덕션 호출 지점은 없습니다.
- **추정기는 토크나이저가 아니라 3단계 토크나이저프리 휴리스틱입니다.** API 보고 사용량이 있으면 T1이 반환하고, T2는 CJK 인식 휴리스틱(CJK 문자 `CHARS_PER_TOKEN_CJK = 2`, 나머지 `CHARS_PER_TOKEN = 4`), T3의 레거시 `len // 4`는 T2의 순수 ASCII 퇴화 케이스입니다. 의도적으로 결정론적(재현 가능한 테스트, 안정적 예산)입니다; `CHARS_PER_TOKEN_CJK = 2`는 중국어가 4가 아닌 1–2자/토큰에 가까움을 반영합니다.
- **보고된 사용량이 이기는 곳.** T3만이 보고된 사용량 기반 트리거입니다(`compute_pressure`는 max를 취함). T1/T2 라우트 결정은 추정 기반입니다(추정치 + 시스템 프롬프트 오버헤드만); 레거시 `_check_trigger` 절 폴백은 `max(로컬 추정치, 보고값)`을 사용합니다.
- **T3는 반환되는 응답을 절대 바꾸지 않습니다.** T3 디스패치의 지속 효과는 도구 결과의 제자리 트렁케이션(메시지 객체는 그래프 상태와 공유됨)과 안티-스래싱 장부 기록뿐입니다; T3 compact 라우트의 `request.override`는 로컬이며 원본 응답이 항상 반환됩니다. T3 본문 전체가 fail-open입니다.
- **T4/T5는 설계상 안티-스래싱 매트릭스를 우회합니다** — 그것이 "강제"의 요점입니다. `MAX_OVERFLOW_RETRIES (3)`(T4/T5 단일 공유 카운터, 턴마다 리셋) 초과, 또는 강제 압축 단계 자체의 실패 시, 원본 프로바이더 예외가 전파됩니다(절대 삼켜지지 않고, 절대 압축 에러로 대체되지 않음).
- **압축은 fail-open입니다.** `_apply_compression` 내부의 어떤 예외도 로그되고 삼켜집니다; 턴은 압축되지 않은 히스토리로 진행됩니다.
- **정적 폴백은 휴리스틱입니다.** 키워드 기반 결정/완료 분류와 원시 도구 인자에서의 경로 추출은 최선의 노력입니다; 섹션 골격은 보장되지만 콘텐츠 품질은 아닙니다.
- **구조화 티어가 `json_mode`인 이유는 설정된 엔드포인트가 그것을 요구하기 때문입니다.** `glm-5.3-flash`(openai 호환)는 `with_structured_output`의 함수 호출을 내보내지 않아 기본 function-calling 티어가 Pydantic 파싱 오류가 됩니다; `_structured_runnable`은 `method` 인자를 받지 않는 프로바이더를 위해 인자 없는 호출로 물러서고, 나머지는 `json_repair`와 free-form 티어가 덮습니다. 코드층 cap과 렌더러가 출력 형태를 보장합니다.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 태그/`lc_source="summarization"`은 하중을 지지는 정확한 문자열입니다.** 이후 턴의 체이닝(`_extract_previous_summary`), 프루닝 정지 조건, 테스트 스위트 전체가 이들을 문자 그대로 매칭합니다 — 함부로 다시 표현하지 마십시오.
