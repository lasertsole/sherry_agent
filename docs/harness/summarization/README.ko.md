# 🗜️ 컨텍스트 압축: Summarization 미들웨어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 긴 대화를 모델의 컨텍스트 윈도우 안에 유지하는 방법: 다섯 개의 트리거 지점이 전체 라이프사이클(턴 시작 전, 모든 모델 호출 전, 모든 모델 응답 후, 프로바이더 오버플로 에러 시)을 감시하고, 순수 함수형 4-경로 라우터가 가장 저렴한 수리책을 고르며(큰 도구 결과를 먼저 잘라내고, 강제될 때만 AI 압축), 안티-스래싱 가드가 압축이 통제 없이 불어나는 일을 원천 차단합니다.

사실상의 기준(source of truth): `agent/middlewares/summarization.py`, `pub_func/message/overflow_router.py`, `pub_func/message/tool_result_ttl.py`, `pub_func/message/llm_error_classifier.py`, `pub_func/message/estimate_msg_tokens.py`, `pub_func/message/tool_output_dedup.py`, `pub_func/message/tool_output_prune.py`, `pub_func/message/target_truncation.py`, `pub_func/message/turn_utils.py`, `config/num.py`, 그리고 두 등록 지점 `agent/core.py`와 `agent/tools/subagent/spawn/core.py`. 이 문서의 모든 줄 번호와 상수는 해당 코드와 대조하여 검증했습니다.

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

`Summarization`(`agent/middlewares/summarization.py`, 클래스는 490행)은 **처음부터 직접 구현한** `AgentMiddleware`입니다 — LangChain 내장 `SummarizationMiddleware`를 상속하지 **않습니다**. 에이전트 라이프사이클의 정확히 두 지점에만 훅을 겁니다:

- `before_agent` / `abefore_agent`(1894 / 1898행) — **T1 사전 점검**
- `wrap_model_call` / `awrap_model_call`(1908 / 1994행) — **T2 디스패치, T3 응답 후 재확인, T4/T5 에러 복구 링**

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
├─ T1  before_agent 사전 점검  (_t1_preflight :1834 / _at1_preflight :1865)
│      ├─ _reset_turn_state (:1797)가 10개의 턴 단위 카운터를 리셋
│      ├─ _decide_overflow_route (:622) → None / "fits" → 통과
│      ├─ 쿨다운 > 0이면 COMPACT 라우트 차단; 트렁케이트 트랙은 여전히
│      │  실행 (그 자체가 가장 저렴한 복구 메커니즘)
│      └─ 디스패치(trigger="T1") + _t1_state_update (:1810)가 결과를
│         그래프에 커밋:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         (add_messages 리듀서는 스스로 메시지를 지우지 않는다 —
│         RemoveMessage 센티널이 압축된 접두부가 상태를 실제로
│         떠나는 유일한 통로)
│
├─ T2  wrap_model_call, 핸들러 이전 (:1908 동기 / :1994 비동기)
│      ├─ force 플래그(:1921)를 스킵 게이트보다 먼저 읽음 — 스킵 게이트
│      │  (_should_skip_compression :1234)가 플래그를 소비함
│      ├─ _tick_cooldown(:811): 모든 호출이 쿨다운을 감소
│      ├─ 안티-스래싱 게이트(:1931–1934):
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      통과 (직전에 compact가 있었다면 시스템 프롬프트 재구축,
│      │      :1938–1952) → 핸들러 → 모니터 → T3
│      ├─ 아니면: 4-경로 결정(:1967) → _dispatch_overflow_route;
│      │  레거시 트리거 절이 발동하면(_check_trigger :566, 예:
│      │  ("messages", 40)) → ROUTE_COMPACT_ONLY(:1972)
│      └─ 세 곳의 핸들러 호출부(:1927, :1953, :1978)는 모두
│         _execute_with_recovery(:1036) 안쪽에서 실행 — 즉 T4/T5 링
│
├─ T3  응답 후 재확인  (_post_response_check :828 / 비동기 :901)
│      ├─ T2가 이번 wrap 호출에서 이미 압축했다면 건너뜀(t2_compressed
│      │  플래그, :1980–1983) — 모델 호출당 압축은 정확히 한 번
│      ├─ extract_reported_input_tokens(response)(:127); None → 반환
│      ├─ 게이트: 턴 시도 상한, 쿨다운, 사용 가능 예산
│      ├─ pressure = max(추정치 + 시스템 프롬프트, 보고값) —
│      │  프로바이더가 보고한 입력 토큰이 우선(compute_pressure)
│      ├─ pressure < usable × 0.80 → 반환; 라우트가 "fits" → 반환
│      └─ 디스패치(trigger="T3")하고 항상 원본 응답을 반환; 함수 전체가
│         fail-open(예외 발생 → 로그, 원본 응답 그대로 유지)
│
└─ T4/T5  프로바이더 에러 복구 링
       (_execute_with_recovery :1036 / _aexecute_with_recovery :1094)
       ├─ 핸들러가 예외를 던짐 → classify_provider_error
       │  (pub_func/message/llm_error_classifier.py):
       │  payload_too_large → T4, context_overflow → T5
       │  (_TRIGGER_BY_ERROR_CLASS :112, _RETRY_KEY_BY_ERROR_CLASS :116)
       ├─ 대상 외 / 미분류 → 원본 예외를 그대로 재던짐(재시도 0회,
       │  상태 기록 0건, 절대 삼키지 않음)
       ├─ 재시도 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  (:964 / 비동기 :1009): 강제 압축 + 예산 트렁케이션, 구조상
       │  모든 안티-스래싱 게이트를 우회(쿨다운, 턴당 상한,
       │  _should_skip_compression 모두 consult하지 않음); 쿨다운을
       │  무장하지 않고 턴 시도도 세지 않지만, 세션 통계의 진실성을 위해
       │  _record_compression은 거침; 클래스별 재시도 카운터는 성공
       │  후에만 증가(:1000)
       ├─ 재시도 소진 → 원본 예외 재던짐(에러 프레임은 messages.py →
       │  turn_runner.py로 전파 — 절대 빈 응답으로 대체되지 않음)
       └─ 강제 압축 단계 자체가 실패 → 원본 예외 재던짐
          (raise exc from compression_exc). _monitor_degradation은
          링이 반환한 뒤 최종 성공 응답에 한 번만 실행.
```

레거시 트리거 절은 여전히 T2의 폴백으로 존재합니다(`_check_trigger`, :566): `("messages", N)`은 히스토리 길이로, `("tokens", N)`은 `max(로컬 추정치, 마지막 AIMessage의 보고된 usage_metadata.total_tokens)` ≥ N으로 발동합니다. 절 목록은 OR 관계입니다.

## 🚦 4-경로 오버플로 라우팅 결정

`pub_func/message/overflow_router.py`는 **순수 결정 레이어**입니다 — 잘라내기도, 압축도, I/O도, 상태도 없습니다. 미들웨어는 여기서 세 함수를 임포트합니다:

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
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :605
system_est     = len(state_register_mem["system_prompt"]) // 4                  # :616–620
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

단일 실행자 `_dispatch_overflow_route`(:739 동기 / :779 비동기)가 T1, T2 **및** T3를 모두 서비스합니다 — 두 번째 복사본은 절대 없습니다:

- `truncate_tool_results_only` → `_run_budget_truncation`(:649)이 제자리 절단 후 **재확인**: 확보한 토큰이 부족하면(`new_tokens ≥ usable × 0.80`) `compact_then_truncate`로 승격; 아니면 압축 없이 통과;
- `compact_only` / `compact_then_truncate` → `_execute_compact`(:681 / 비동기 :710) → `_apply_compression`(예외는 로그, 요청은 그대로) → `_record_compaction_bookkeeping`(:673: 쿨다운 무장, 턴 시도 1회 기록) → `compact_then_truncate`는 압축 결과에 예산 트렁케이션을 백스톱으로 한 번 더 실행 → 구/신 토큰과 압력 비율과 함께 라우트 로깅.

윈도우 산술(테스트 계약): 윈도우 `41 600` → usable `25 600`, 두 경계선 `17 920` / `20 480`, 트렁케이트 예산 `15 360`. `MAIN_LLM_MAX_TOKEN = 65536`일 때 등록된 T2 절은 `52 428`에 놓입니다.

## 🪙 토큰 추정 (토크나이저 없음)

`pub_func/message/estimate_msg_tokens.py`(29행)는 의도적으로 토크나이저 없이 결정론적으로 동작합니다:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

빠르고, 실행 간 안정적이며(같은 입력 → 같은 숫자 → 재현 가능한 테스트), 의도적으로 보수적으로 근사합니다. 트리거/예산 경로의 어떤 것도 모델 토크나이저에 의존하지 않습니다.

## ✂️ 트렁케이트 트랙: 예산 트렁케이션과 TTL 모듈

`pub_func/message/tool_result_ttl.py`는 트렁케이트 트랙이 사용하는 제자리 절단을 제공합니다. 설계 불변식(하중 지지):

- **제자리만** — 이 모듈은 메시지를 절대 삭제, 재정렬, pop하지 않습니다; `msg.content`(또는 content 리스트 블록)만 수정하고 인덱스를 반환합니다. 이것이 프로바이더 API와 `ToolCallNormalize`가 의존하는 tool-call/`ToolMessage` 페어링을 보존합니다.
- **비어 있지 않은 플레이스홀더** — 잘려나간 결과는 항상 비어 있지 않은 내용을 유지합니다: `ToolCallNormalize.before_model`은 **빈 `ToolMessage`를 드롭**해서 트랜스크립트를 정화하므로, 빈 플레이스홀더는 조용히 페어링을 깨뜨립니다.
- **머리 30% / 꼬리 30% 보존**(`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`)에 생략 마커를 붙입니다.

미들웨어가 실제로 소비하는 것: **`truncate_to_budget` 하나뿐** — 라우터의 후보 목록으로 구동되며, `_run_budget_truncation`(:649)이 예산(`usable × TRUNCATE_BUDGET_RATIO`)에 맞을 때까지 후보를 자릅니다.

TTL 레지스트리 자체(`record_first_seen` / `select_expired` / `truncate_expired`, `PRUNE_TTL_SECONDS = 300`, `TTL_REGISTRY_MAX_ENTRIES = 512`, `tool_call_id` 키, 재시작 시 휘발)는 오늘날 **테스트 스위트만 사용**합니다 — 미들웨어에는 나이 기반 만료 로직이 연결되어 있지 않습니다("정직함과 한계" 참조).

## 🔁 컴팩트 트랙: `_apply_compression` 내부

`_apply_compression`(:1636; 비동기 쌍둥이 :1708)은 다음 순서로 실행됩니다:

1. **복구 컨텍스트 캡처**(`_capture_recovery_context`, :1525): 마지막 사용자 요청(≤ 800자)과 파일 작업 래칫 — `read`/`write` 계열 도구 호출에서 경로를 추출하고(:405), 이전 라운드의 집합과 병합(읽은 것은 기억되고, 수정된 파일이 읽기 전용으로 강등되는 일은 없음).
2. **비 LLM 전략**(`_run_non_llm_strategies`, :1472): `중복 제거 → 프루닝 → 타깃 트렁케이트`(상세는 아래). 이것들은 공짜입니다 — 모델 호출 없음.
3. **LLM 사용 여부 결정**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   비 LLM 축소가 첫 기회를 얻습니다; 히스토리가 여전히 보존 예산의 두 배를 넘을 때(또는 거버너가 LLM 요약을 비활성했거나, 비 LLM 전략이 아무것도 줄이지 못했을 때)에만 보조 LLM을 씁니다.
4. **공격적 백스톱**(`_aggressive_truncate`, :1508): 결과가 *그래도* 너무 크면, `AGGRESSIVE_TRUNCATE_CHARS (1 000)`자를 넘는 모든 `ToolMessage`가 마커와 함께 하드 컷됩니다.
5. **요약 자체 절단**(`_truncate_summary_messages`, :1578): `SUMMARY_TOTAL_MAX_CHARS (16 000)`자를 넘는 기존 요약 메시지(`lc_source == "summarization"`)는 머리 30% / 꼬리 30%로 재절단됩니다(`_truncate_content`, :1570).
6. **복구 주입**(`_inject_recovery_context`, :1543): 캡처한 파일 작업 래칫이 요약의 `## Relevant Files` 섹션으로 재작성되어, 체크포인트가 항상 최신 읽기/수정 파일 맵을 품도록 합니다.
7. **장부 기록**(`_record_compression`, :1256), 마지막으로 `request.override(messages=..., system_message=...)`.

**절단점 선택**(`_determine_cutoff`, :1289): 히스토리를 턴으로 쪼개고, **최신에서 거꾸로** 걸으며 보존 예산 `clamp(window × 0.25, 2 000, 15 000)`(`_calculate_preserve_budget`, :555)에 맞춰 누적합니다; 통째로 안 들어가는 턴은 턴 중간에서 쪼개질 수 있습니다. `_adjust_for_orphan_pairs`(:1319)가 절단점을 거꾸로 걸어 `ToolMessage`가 `AIMessage` 도구 호출과 떨어지는 경우가 없도록 합니다. 마지막 턴 비율 게이트가 발동하지 않는 한(마지막 사용자 턴 ≥ 전체 토큰의 `LAST_TURN_RATIO_THRESHOLD (0.5)` — `_check_last_turn_ratio`, wrap 진입 :1916/:2002에서 호출), 절단점은 마지막 `HumanMessage`를 넘지 않습니다.

모든 실패 모드는 fail-open입니다: `_apply_compression`이 예외를 던지면 로그만 남기고 원본 요청이 그대로 진행됩니다 — 깨진 압축이 턴을 망치는 일은 없습니다.

## 📝 LLM 요약: 프롬프트, 체이닝, 폴백

`_create_summary` / `_acreate_summary`(:1389 / :1414):

1. **직렬화**(`_serialize_for_summary`, :255): 각 메시지가 태그 붙은 한 줄로 변합니다 — `[User]:`(≤ 2 000자), `[Assistant]:`(≤ 2 000자), `[Assistant tool call]: name(args ≤ 500 chars)`, `[Tool result|Tool error] (id):`(> 2 000자 → 1 800자 보존 + 생략 마커).
2. **이전 체크포인트 체이닝**(`_extract_previous_summary`, :1355): `additional_kwargs["lc_source"] == "summarization"`인 가장 최신 `AIMessage`를 찾아 `<summary>…</summary>` 본문을 추출합니다. 존재하면 프롬프트가 `_SUMMARY_PROMPT_FIRST`(:234) 대신 `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE`(:242)가 됩니다 — 목표/제약/결정을 앞으로 운반하고, 충돌 시 최신이 이기며, FIFO 상한을 지킵니다.
3. **호출**은 보조 모델에 `config={"metadata": {"lc_source": "summarization"}}`로 수행되어, 다운스트림 도구 체인이 요약 호출을 식별할 수 있게 합니다.
4. **가드 레일:** 비어 있거나 지나치게 짧은 응답은 결정론적 요약으로 폴백하고, 예외도 마찬가지입니다. 실패 시 LLM이 최후의 발언권을 가지는 일은 없습니다.

프롬프트 템플릿(`_SUMMARY_TEMPLATE`, :187)은 Markdown 골격을 고정합니다 — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress(Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — "비어 있어도 모든 섹션을 유지"와 기밀 규칙("NEVER include API keys, tokens, passwords, secrets")을 요구합니다. `_enforce_fifo_limits`(:371)가 반환 텍스트에 항목 상한을 결정론적으로 재적용하고, `"(N earlier items omitted for brevity)"`를 덧붙입니다.

## 🧱 정적 폴백 (LLM 없는 요약)

`_build_static_fallback_summary`(:286)는 모델 호출 0회로 같은 섹션 골격을 만듭니다:

- 마지막 사용자 요청 → *Latest Unresolved User Request*; 첫 요청 → *Goal*;
- 결정 키워드(`decided`, `choosing`, `because`, `therefore`)를 포함한 AI 텍스트 → *Key Decisions*, 아니면 *Completed*;
- 모든 도구 호출 → *Completed*; 경로 같은 토큰(`/` 또는 `\` 포함, 또는 `.py`/`.md`/`.js`/`.ts`/`.json`으로 끝) → *Relevant Files*(≤ 10, `http` 링크 제외);
- 에러 `ToolMessage` → *Blocked*와 *Critical Context*.

`skip_llm`이 활성화되면 그대로 사용되고, 짧거나 실패한 LLM 요약의 안전망이기도 합니다.

## 📦 출력: 요약 메시지 쌍

`_build_new_messages`(:1443)는 요약 텍스트를 감싸 정확히 두 메시지를 내보냅니다:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — 역할 교대를 유지하는 중립적 질문.
- **AIMessage**, `additional_kwargs={"lc_source": "summarization"}` 포함 — 이후 턴들이 (a) 이전 체크포인트를 찾아 체이닝하고, (b) 프루닝이 체크포인트에서 멈추게 하고, (c) 테스트가 대체된 후 요약이 모델 뷰에서 삼켜질 수 있음을 단증하는 마커.
- 전체 콘텐츠는 `SUMMARY_TOTAL_MAX_CHARS (16 000)`으로 봉인되고, 머리/꼬리 30/30 보존.

## 🛡️ 안티-스래싱 가드 매트릭스와 성능 저하 복구

상태는 세션 범위 `state_register_mem`의 **열네 개** `summarization_*` 키(:89–104)에 살습니다. `_reset_turn_state`(:1797)는 매 턴 시작 시 그중 **열 개**를 리셋합니다; `summarization_last_user_question`, `summarization_cooldown_rounds`, 두 T4/T5 재시도 카운터는 의도적으로 턴마다 리셋되지 **않습니다**.

| 가드 | 키 | 임계값 | 효과 |
| :---- | :-- | :-------- | :----- |
| 턴 쿨다운 | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 실제 compact마다 무장(:673); **모든** 모델 호출이 감소(:811); T1 compact 라우트, T2 선제, T3를 차단 — T4/T5 강제 링은 절대 차단하지 않음 |
| 턴당 압축 수 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | :673이 증가; T2 선제 + T3 억제(강제 링은 면제) |
| 클래스별 오버플로 재시도 | `summarization_overflow_retries_t4` / `_t5` | `MAX_OVERFLOW_RETRIES = 3` | 성공한 강제 단계마다 증가; 소진 → 원본 프로바이더 에러 전파 |
| 세션 총 압축 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`(:1234)이 True 반환 — 선제 압축 완전 정지 |
| 연속 무효 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` 설정 — 비 LLM 전략만 |
| 유효성 판정 | (`_record_compression`, :1256) | 메시지 수 감소 **또는** 토큰 축소 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 성공한 비 LLM 전략(`dedup`/`prune`/`truncate`/`fallback`/`aggressive`)이 `skip_llm`을 다시 해제 |
| 성능 저하 복구 예산 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 성능 저하 모니터가 시작하는 강제 복구의 상한 |

**성능 저하 모니터**(`_monitor_degradation`, :1609): 이번 호출에서 실제로 압축이 일어났을 때만 consult됩니다(`_compaction_just_happened` 플래그). 모델 응답에 텍스트가 없으면 카운터가 증가하고; `DEGRADATION_NO_TEXT_THRESHOLD (3)`회 연속 빈 응답 — 그리고 `summarization_recovery_attempts < 2`인 동안 — `force_recovery`를 설정하고 무효 연속 기록과 세션 압축 카운트를 지웁니다. 비어 있지 않은 응답은 카운터를 리셋합니다. 이것은 "압축 → 모델 혼란 → 빈 출력 → 재압축"의 병적 루프를 잡습니다. 상호작용에 주의: force 플래그는 wrap 진입(:1921)에서 `_should_skip_compression` **보다 먼저** 읽히고, 스킵 게이트는 카운터를 리셋하고 진행하며 그 플래그를 소비합니다(:1235–1240) — 복구 압축은 정확히 한 번 실행됩니다.

## 🔄 시스템 프롬프트 갱신

메인 에이전트 전용(`need_update_system_prompt=True`): 압축 후 미들웨어가 시스템 프롬프트를 재구축해 `system_prompt` 상태 키에 기록하므로, 다음 모델 호출은 페르소나 파일 / 장기 기억을 지금 현재 상태 그대로 봅니다. 두 전달 경로: 압축 직후의 `request.override(system_message=SystemMessage(...))`, 그리고 — T1 compact가 이미 일어났지만 안티-스래싱 게이트가 두 번째 압축을 막을 때 — 재구축된 프롬프트는 게이트 경로에서 여전히 주입됩니다(:1938–1952). `ContextEngineHook` 없는 체인은 이 미들웨어가 전달해 주기 때문입니다.

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

모든 임계값은 `config/num.py`에 있습니다. ◆ 표시 상수는 살아있는 코드 경로가 소비합니다; ○ 표시 상수는 정의 또는 임포트는 되지만 살아있는 경로는 **소비하지 않습니다**("정직함과 한계" 참조).

| 상수 | 값 | 소비 위치 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route`의 하드 오버플로 밴드; T3 압력 게이트; 두 트리거 절을 구성 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route`의 소프트 오버플로 밴드 (구 `_preemptive_check` 2-밴드 게이트는 은퇴) |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`(:605): 윈도우 − 예비량 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 트렁케이트 트랙 예산 = usable × 0.60 (:660) |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results`의 후보 하한 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 최신 메시지는 절대 트렁케이트 불가 (페어링 마진) |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | 에러 클래스당 T4/T5 강제 복구 상한 |
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
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 공격적 백스톱 절단 길이 |
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
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 파일 작업 래칫 목록 상한 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 복구 컨텍스트 요청 상한 |
| `CHARS_PER_TOKEN`(추정기) | `4` | 결정론적 토큰 추정 제수 |
| `PRUNE_TTL_SECONDS` | `300` | TTL 만료 지평 — TTL 트리오만 소비 (오늘날 테스트 전용) |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 최초 관찰 레지스트리 한계 (오늘날 테스트 전용) |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 미들웨어가 임포트, 절대 읽지 않음 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 미들웨어가 임포트, 절대 읽지 않음 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 정의됨, 임포트되지 않음 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 정의됨, 임포트되지 않음 (실제 사용되는 것은 900자 목록 상한) |

## 🧪 테스트

| 스위트 | 케이스 | 커버 |
| :---- | :---- | :----- |
| `tests/unit/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` 밴드, 후보 규칙, 안정적 라우트 문자열 |
| `tests/unit/test_tool_result_ttl.py` | 28 | 제자리 트렁케이션, 페어링 불변식, 비어 있지 않은 플레이스홀더, 레지스트리 한계, 예산 트렁케이션 |
| `tests/unit/test_llm_error_classifier.py` | 20 | 413 상태, 텍스트 힌트, 7개 오버플로 패턴, cause 체인 깊이, 읽기 전용 보장 |
| `tests/unit/test_config_num.py` | 43 | 상수 계약 (워치독 `CONTRACT_NAMES`가 문서화된 모든 노브 커버) |
| `tests/module/test_compression_comprehensive.py` | 48 | 12개 클래스: T2 소프트 오버플로, T2 쿨다운, T2 음성/무작동, 동기/비동기 패리티, T1 사전 점검, 라우트 결정, T3 트리거/3형태/음성 이중, T4/T5 복구, 전체 안티-스래싱 매트릭스, 전체 분기 패리티 |
| `tests/module/test_compression_e2e_static.py` | 12 | 6개 엔드투엔드 시나리오 × 2 등록 순서, 정적 폴백 압축, 제로 네트워크 |
| `tests/module/test_summarization_trigger.py` | 3 | 프로덕션 등록 계약: `MAIN_LLM_MAX_TOKEN = 65 536` → 트리거 임계값 `52 428`; 저토큰 통과 |
| `tests/module/test_summarization_comprehensive.py` | 140 | 레거시 딥 스위트: 절단점/예산, FIFO 상한, 폴백, 프루닝/중복 제거/타깃 트렁케이트, 성능 저하 |
| `tests/module/test_e2e_summarization.py` | 7 | 전체 그래프 밀폐 e2e: 실제 `create_agent` 체인 (주 모델 캡처 스텁, 보조 모델 실패 스텁)이 정적 폴백 경로를 유도; 제로 네트워크, 윈도우 32 000 (축소), MAIN_LLM 설정 누락 시 스킵 |
| `tests/integration/test_interrupt_marker_approach.py` | 11 | 마커 의미론: 요약 쌍은 이후 압축에서도 생존; FACT C 픽스처 (윈도우 26 000 → usable 10 000, 트렁케이트 라인 7 000) |

전체 프로세스 겸리 스위트(`uv run python tests/run_tests_split.py`) 통과: **2219 passed / 0 failed** (GROUP A 1469P/2S + GROUP B 750P/5D).

## ⚠️ 정직함과 한계

- **`keep=("messages", 10)`은 받아들여지지만 사용되지 않습니다.** 생성자는 API 호환성을 위해 저장할 뿐; 꼬리 보존은 예산 기반(`PRESERVE_RATIO` × 윈도우, [2 000, 15 000] 클램프)에 라우터의 `TRUNCATABLE_RECENT_SKIP` 마진을 더한 것입니다. `keep`을 바꿔도 효과가 없습니다.
- **문서 장식용 임포트.** `summarization.py` 상단의 `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, `AUTO_CONTINUE_PROMPT`는 임포트되지만 절대 읽히지 않습니다. `DEGRADATION_MONITOR_COUNT`와 `FILE_OPS_SECTION_MAX_CHARS`는 `config/num.py`에 정의되지만 소비자가 없습니다.
- **TTL 레지스트리는 프로덕션에 연결되어 있지 않습니다.** `record_first_seen` / `select_expired` / `truncate_expired`(및 `PRUNE_TTL_SECONDS`, `TTL_REGISTRY_MAX_ENTRIES`)는 테스트만 소비합니다; 미들웨어는 오직 `truncate_to_budget`만 사용합니다. `agent/` 전역 grep에서 TTL 트리오의 프로덕션 호출 지점은 발견되지 않습니다. 레지스트리는 또한 휘발적입니다(인메모리, `tool_call_id` 키, 재시작 시 소실).
- **남아 있지만 비활성인 코드.** `_preemptive_check`(:579)와 `_preemptive_truncate`(:1138)는 더 이상 호출 지점이 없습니다 — 이들이 구현한 2-밴드 선점은 4-경로 결정으로 대체되었습니다. 참조용으로만 유지됩니다.
- **추정기는 토크나이저가 아니라 `chars // 4`입니다.** 의도적으로 결정론적(재현 가능한 테스트, 안정적 예산)이며 영어/코드 혼합 콘텐츠로 보정되었습니다; CJK 중심 콘텐츠는 과소 계수됩니다(중국어는 4가 아닌 1–2자/토큰에 가까움).
- **보고된 사용량이 이기는 곳.** T3만이 보고된 사용량 기반 트리거입니다(`compute_pressure`는 max를 취함). T1/T2 라우트 결정은 추정 기반입니다(추정치 + 시스템 프롬프트 오버헤드만); 레거시 `_check_trigger` 절 폴백은 `max(로컬 추정치, 보고값)`을 사용합니다.
- **T3는 반환되는 응답을 절대 바꾸지 않습니다.** T3 디스패치의 지속 효과는 도구 결과의 제자리 트렁케이션(메시지 객체는 그래프 상태와 공유됨)과 안티-스래싱 장부 기록뿐입니다; T3 compact 라우트의 `request.override`는 로컬이며 원본 응답이 항상 반환됩니다. T3 본문 전체가 fail-open입니다.
- **T4/T5는 설계상 안티-스래싱 매트릭스를 우회합니다** — 그것이 "강제"의 요점입니다. 클래스당 `MAX_OVERFLOW_RETRIES (3)` 초과, 또는 강제 압축 단계 자체의 실패 시, 원본 프로바이더 예외가 전파됩니다(절대 삼켜지지 않고, 절대 압축 에러로 대체되지 않음).
- **압축은 fail-open입니다.** `_apply_compression` 내부의 어떤 예외도 로그되고 삼켜집니다; 턴은 압축되지 않은 히스토리로 진행됩니다.
- **정적 폴백은 휴리스틱입니다.** 키워드 기반 결정/완료 분류와 원시 도구 인자에서의 경로 추출은 최선의 노력입니다; 섹션 골격은 보장되지만 콘텐츠 품질은 아닙니다.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 태그/`lc_source="summarization"`은 하중을 지지는 정확한 문자열입니다.** 이후 턴의 체이닝(`_extract_previous_summary`), 프루닝 정지 조건, 테스트 스위트 전체가 이들을 문자 그대로 매칭합니다 — 함부로 다시 표현하지 마십시오.
