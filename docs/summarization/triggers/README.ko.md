# 🧭 Summarization 트리거 — 라이프사이클 T1–T5와 오버플로 라우팅

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Summarization](../README.ko.md)의 일부: 다섯 개의 라이프사이클 트리거 지점(T1–T5)과 4경로 오버플로 결정.

---

## 🧭 라이프사이클: 다섯 개의 트리거 지점 (T1–T5)

```
턴 시작
│
├─ T1  before_agent 사전 점검  (_t1_preflight overflow.py:795 / _at1_preflight overflow.py:820)
│      ├─ _reset_turn_state (thrash.py:157)가 11개의 턴 단위 카운터를 리셋
│      ├─ _decide_overflow_route (overflow.py:185) → None / "fits" → 통과
│      ├─ 쿨다운 > 0이면 COMPACT 라우트 차단; 트렁케이트 트랙은 여전히
│      │  실행 (그 자체가 가장 저렴한 복구 메커니즘)
│      └─ 디스패치(trigger="T1") + _t1_state_update (overflow.py:773)가 결과를
│         그래프에 커밋:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         (add_messages 리듀서는 스스로 메시지를 지우지 않는다 —
│         RemoveMessage 센티널이 압축된 접두부가 상태를 실제로
│         떠나는 유일한 통로)
│
├─ T2  wrap_model_call, 핸들러 이전 (core.py:413 동기 / core.py:495 비동기)
│      ├─ force 플래그(core.py:426)를 스킵 게이트보다 먼저 읽음 — 스킵 게이트
│      │  (_should_skip_compression thrash.py:110)가 플래그를 소비함
│      ├─ _tick_cooldown(thrash.py:87): 모든 호출이 쿨다운을 감소
│      ├─ 안티-스래싱 게이트(core.py:437–439):
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      통과 (직전에 compact가 있었다면 시스템 프롬프트 재구축,
│      │      core.py:441–459) → 핸들러 → 모니터 → T3
│      ├─ 아니면: 4-경로 결정(core.py:472) → _dispatch_overflow_route;
│      │  레거시 트리거 절이 발동하면(_check_trigger overflow.py:135, 예:
│      │  ("messages", 40)) → ROUTE_COMPACT_ONLY(core.py:480)
│      └─ 핸들러 호출 자체는
│         _execute_with_recovery(overflow.py:703) 안쪽에서 실행 — 즉 T4/T5 링
│
├─ T3  응답 후 재확인  (_post_response_check overflow.py:511 / 비동기 overflow.py:543)
│      ├─ T2가 이번 wrap 호출에서 이미 압축했다면 건너뜀(t2_compressed
│      │  플래그, core.py:484–486) — 모델 호출당 압축은 정확히 한 번
│      ├─ extract_reported_input_tokens(response)(overflow.py:66); None → 반환
│      ├─ 게이트: 턴 시도 상한, 쿨다운, 사용 가능 예산
│      ├─ pressure = max(추정치 + 시스템 프롬프트, 보고값) —
│      │  프로바이더가 보고한 입력 토큰이 우선(compute_pressure)
│      ├─ pressure < usable × 0.80 → 반환; 라우트가 "fits" → 반환
│      └─ 디스패치(trigger="T3")하고 항상 원본 응답을 반환; 함수 전체가
│         fail-open(예외 발생 → 로그, 원본 응답 그대로 유지)
│
└─ T4/T5  프로바이더 에러 복구 링
       (_execute_with_recovery overflow.py:675 / _aexecute_with_recovery overflow.py:734)
       ├─ 핸들러가 예외를 던짐 → classify_provider_error
       │  (pub/func/message/llm_error_classifier.py):
       │  payload_too_large → T4, context_overflow → T5
       │  (_TRIGGER_BY_ERROR_CLASS overflow.py:105, _RETRY_KEY_BY_ERROR_CLASS overflow.py:111)
       ├─ 대상 외 / 미분류 → 원본 예외를 그대로 재던짐(재시도 0회,
       │  상태 기록 0건, 절대 삼키지 않음)
       ├─ 재시도 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  (overflow.py:625 / 비동기 overflow.py:662): LLM을 부르지 않는 테일 클립이 먼저 실행됨 —
       │  클립만으로 추정치가 사용 가능 예산 아래로 떨어지면 핸들러가
       │  스텁된 테일 결과로 재시도되고 compact는 전혀 일어나지 않음;
       │  이미 스텁된 요청에서는 클립이 no-op이 되므로 다음 시도는
       │  "compact + 예산 트렁케이션" 단계로 퇴화함. 이 단계는 구조상
       │  모든 안티-스래싱 게이트를 우회(쿨다운, 턴당 상한,
       │  _should_skip_compression 모두 consult하지 않음); 쿨다운을
       │  무장하지 않고 턴 시도도 세지 않지만, 세션 통계의 진실성을 위해
       │  _record_compression은 거침; 클래스별 재시도 카운터는 성공
       │  후에만 증가(overflow.py:614)
       ├─ 재시도 소진 → 원본 예외 재던짐(에러 프레임은 messages.py →
       │  turn_runner.py로 전파 — 절대 빈 응답으로 대체되지 않음)
       └─ 강제 압축 단계 자체가 실패 → 원본 예외 재던짐
          (raise exc from compression_exc). _monitor_degradation은
          링이 반환한 뒤 최종 성공 응답에 한 번만 실행.
```

레거시 트리거 절은 여전히 T2의 폴백으로 존재합니다(`_check_trigger`, overflow.py:135): `("messages", N)`은 히스토리 길이로, `("tokens", N)`은 `max(로컬 추정치, 마지막 AIMessage의 보고된 usage_metadata.total_tokens)` ≥ N으로 발동합니다. 절 목록은 OR 관계입니다.

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
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget overflow.py:168
system_est     = estimate_text_tokens(system_prompt)   # _estimate_system_prompt_tokens overflow.py:179
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

단일 실행자 `_dispatch_overflow_route`(overflow.py:408 동기 / overflow.py:430 비동기)가 T1, T2 **및** T3를 모두 서비스합니다 — 두 번째 복사본은 절대 없습니다:

- `truncate_tool_results_only` → `_run_budget_truncation`(overflow.py:219) — 1단계는 과도하게 큰 도구 호출 인자를 잘라내고(새 메시지 반환, [트렁케이트 트랙](../internals/README.ko.md#-트렁케이트-트랙-예산-트렁케이션과-ttl-모듈) 참조), 2단계는 도구 결과를 제자리에서 잘라냄 — 이후 **재확인**: 확보한 토큰이 부족하면(`new_tokens ≥ usable × 0.80`, 반환된 목록 기준으로 추정) `compact_then_truncate`로 승격; 아니면 압축 없이 통과;
- `compact_only` / `compact_then_truncate` → `_execute_compact`(overflow.py:326 / 비동기 overflow.py:344) → `_apply_compression`(예외는 로그, 요청은 그대로) → `_record_compaction_bookkeeping`(core.py:345: 쿨다운 무장, 턴 시도 1회 기록) → `compact_then_truncate`는 압축 결과에 예산 트렁케이션을 백스톱으로 한 번 더 실행 → 구/신 토큰과 압력 비율과 함께 라우트 로깅.

**P1-2 빠른 경로 — LLM을 부르지 않는 테일 클립.** 어떤 라우트가 실행되기 전에 `_fast_tail_clip`이 `clip_overflow_tail`(`pub/func/message/overflow_clip.py`)을 실행합니다: 꼬리에 연속된 `ToolMessage` 배치가 압축 스텁으로 대체되며, 대체는 `ToolMessage.model_copy`를 거치므로 메시지가 삭제되거나 주입되지 않습니다 — `id`, `tool_call_id`, `name`, `additional_kwargs`가 모두 살아남아 페어링 새니타이즈와 영속 워터마크가 계속 충족됩니다. 예산: `target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`; `0` 타깃은 "최대 적격 배치를 취함"을 뜻합니다(`overflow_clip_max_remove`가 상한, `overflow_clip_min_keep`이 하한). `ratio`는 라우트 경로에서 `COMPRESSION_TRIGGER_RATIO (0.80)`, T4/T5 강제 단계에서 `1.0`(사용 가능 예산 아래)입니다. 클립 **단독**으로 추정치가 경계선 아래로 떨어질 때만 채택됩니다: 요청은 스텁된 리스트 그대로 반환되고 라우트는 전혀 실행되지 않습니다(예산 트렁케이션 없음, 보조 LLM 압축 없음). 불충분한 클립은 버려지고 기존 라우트가 원본 리스트 그대로에서 실행됩니다. T4/T5에서는 클립이 `request.messages`를 읽습니다; 이미 스텁된 요청에서는 no-op이 되므로 동일 클립으로 재시도 예산이 소모되지 않고 다음 시도는 압축으로 퇴화합니다.

**꼬리 내용을 버려도 안전한 이유:** 모든 도구 결과는 반환되는 순간 MesMemory에 플러시되었고(`MessagePersistenceMiddleware`), `message_search` 도구로 계속 조회할 수 있습니다. P0-2로 축출된 결과는 스텁 안에 `[evicted to: …]` 포인터를 유지하며(`read_file`이 계속 동작), P2-4로 슬라이스된 `read_file` 결과는 슬라이스 안내를 그대로 유지합니다; 그리고 스텁된 메시지는 스캔 대상 배치를 종료시키므로 두 번째 클립은 no-op이며 이 마커들을 절대 파괴하지 않습니다. 설정: `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`.

윈도우 산술(테스트 계약): 윈도우 `41 600` → usable `25 600`, 두 경계선 `17 920` / `20 480`, 트렁케이트 예산 `15 360`. 테스트 고정값 `MAIN_LLM_MAX_TOKEN = 65536`일 때(런타임 `.env` 값은 131072 / 128K 이상 필요) 등록된 T2 절은 `52 428`에 놓입니다.
