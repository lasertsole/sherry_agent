# 🗜️ 컨텍스트 압축: Summarization 미들웨어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 긴 대화를 모델의 컨텍스트 윈도우 안에 유지하는 방법: 결정론적 토큰 추정기가 경보를 울리고, 비 LLM 전략이 공짜로 도구 출력 노이즈를 줄이며, 그래도 부족할 때에만 보조 LLM이 오래된 턴들을 구조화된 체크포인트로 재작성합니다. 스래싱 방지 가드 덕분에 압축이 통제 없이 불어나는 일은 없습니다.

사실상의 기준(source of truth): `agent/middlewares/summarization.py`, `pub_func/message/estimate_msg_tokens.py`, `pub_func/message/tool_output_dedup.py`, `pub_func/message/tool_output_prune.py`, `pub_func/message/target_truncation.py`, `pub_func/message/turn_utils.py`, `config/num.py`, 그리고 두 등록 지점 `agent/core.py`와 `agent/tools/subagent/spawn/core.py`. 이 문서의 모든 줄 번호와 상수는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [실행 위치: 모델 호출별 흐름](#-실행-위치-모델-호출별-흐름)
- [트리거링: 세 개의 게이트](#-트리거링-세-개의-게이트)
- [토큰 추정 (토크나이저 없음)](#-토큰-추정-토크나이저-없음)
- [보존 예산과 컷오프](#-보존-예산과-컷오프)
- [`_apply_compression` 내부의 압축 파이프라인](#-_apply_compression-내부의-압축-파이프라인)
- [LLM 요약: 프롬프트, 체이닝, 폴백](#-llm-요약-프롬프트-체이닝-폴백)
- [정적 폴백 (LLM 없는 요약)](#-정적-폴백-llm-없는-요약)
- [비 LLM 전략](#-비-llm-전략)
- [출력: 요약 메시지 쌍](#-출력-요약-메시지-쌍)
- [스래싱 방지 및 열화 복구](#-스래싱-방지-및-열화-복구)
- [시스템 프롬프트 갱신](#-시스템-프롬프트-갱신)
- [등록 위치](#-등록-위치)
- [설정 참조](#-설정-참조)
- [테스트](#-테스트)
- [⚠️ 정직한 한계 고지](#%EF%B8%8F-정직한-한계-고지)

## 🎯 개요

`Summarization`(`agent/middlewares/summarization.py`, 클래스는 402행)은 **처음부터 새로 작성된(from-scratch)** `AgentMiddleware`이며, LangChain 내장 `SummarizationMiddleware`를 상속하지 **않습니다**. 모든 압축 로직이 자체 포함되어 있습니다: 트리거 검사, 컷오프 결정, 요약 생성, 다중 전략 축소 파이프라인, 열화 모니터링.

역할: 대화가 임계값을 넘어 자라면 메시지 히스토리의 오래된 접두부를 컴팩트한 체크포인트로 교체하되, 가장 최근 컨텍스트는 그대로(verbatim) 보존합니다. 압축 후 히스토리는 항상 다음 형태를 갖습니다:

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

교체물이 Human/AI 쌍이기 때문에 모델은 같은 역할의 연속 메시지를 볼 일이 없고, 쌍 복구(pairing repair)가 필요하지 않습니다.

등록 지점은 두 곳입니다:

| 등록 위치 | 트리거 | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| 메인 에이전트 (`agent/core.py:152`) | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| 워커/서브에이전트 (`agent/tools/subagent/spawn/core.py:755`) | `("messages", 40)` **또는** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False` (기본값) |

두 곳 모두 `main_llm_context_window=main_llm_max_tokens`(`MAIN_LLM_MAX_TOKEN`에서 유래)와 `keep=("messages", 10)`을 전달합니다.

## 🧭 실행 위치: 모델 호출별 흐름

미들웨어는 `before_agent`/`abefore_agent`(카운터 리셋)와 `wrap_model_call`/`awrap_model_call`(1188–1262행)에 훅을 겁니다. 미들웨어 체인에서 **가장 안쪽, 즉 LLM에 가장 가까운** 위치에 있으므로, 이 미들웨어의 메시지 재작성은 모델 호출 직전의 마지막 단계입니다.

```
wrap_model_call(request, handler)
│
├─ 1. _check_last_turn_ratio      last turn ≥ 50% of tokens? → flag it
├─ 2. _should_skip_compression    max attempts reached / LLM marked ineffective?
│        └─ yes → call handler directly, monitor response, return
├─ 3. _preemptive_check           pressure = est_tokens / context_window
│        ├─ ≥ 0.80            → "compact"
│        ├─ ≥ 0.70            → "truncate_only"
│        └─ else              → None
├─ 4. if truncate_only|compact:
│        _preemptive_truncate    shrink oversized ToolMessages (> 2000 chars),
│                                no LLM call, override request messages
├─ 5. need_compress = (action == "compact") OR configured trigger fires
├─ 6. if need_compress: _apply_compression(...)   exceptions logged, never fatal
├─ 7. response = handler(request)
└─ 8. _monitor_degradation(response)   count empty responses after compaction
```

전체 파이프라인은 비동기 경로용 `_aapply_compression`(1087–1153행)에 미러링되어 있으며, 두 구현은 의미적으로 동일합니다.

## 🚦 트리거링: 세 개의 게이트

**게이트 1 — 구성된 트리거** (`_check_trigger`, 478행). 각 절(clause)은 `("messages", N)`(히스토리 길이 ≥ N) 또는 `("tokens", N)`(유효 토큰 수 ≥ N)입니다. 절 목록은 OR 관계입니다.

**게이트 2 — 선제적 압력 검사** (`_preemptive_check`, 491행). `main_llm_context_window`가 설정되어 있어야 하며, `pressure = effective_tokens / context_window`를 계산해 반환합니다:

- `pressure ≥ COMPRESSION_TRIGGER_RATIO (0.80)`이면 `"compact"` — 이번 호출에서 전체 압축;
- `pressure ≥ PREEMPTIVE_TRUNCATE_RATIO (0.70)`이면 `"truncate_only"` — LLM 없는 도구 출력 축소만.

**게이트 3 — 마지막 턴 비율** (`_check_last_turn_ratio`, 581행). 마지막 사용자 턴이 혼자서 전체 토큰의 ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)`를 차지하면 `_compress_last_turn`이 설정됩니다: 컷오프 로직이 마지막 턴을 보호하는 대신 요약이 최종 턴**까지** 확장될 수 있습니다. 마지막 사용자 질문은 복구 컨텍스트용으로 세션 상태에 보관됩니다.

"유효 토큰" = `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)`. API가 보고한 숫자가 존재하면 그것이 지상 진실(ground truth)이므로 이깁니다 (455–461, 478–511행).

## 🪙 토큰 추정 (토크나이저 없음)

`pub_func/message/estimate_msg_tokens.py`는 의도적으로 토크나이저 없이, 결정론적으로 동작합니다:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

빠르고, 실행 간 안정적이며(같은 입력 → 같은 숫자 → 재현 가능한 테스트), 의도적으로 보수적인 근사입니다. 트리거/예산 경로의 어떤 부분도 모델 토크나이저에 의존하지 않습니다.

## 💰 보존 예산과 컷오프

**예산** (`_calculate_preserve_budget`, 467행):

```
budget = clamp(context_window × PRESERVE_RATIO(0.25), MIN_PRESERVE_TOKENS(2000), MAX_PRESERVE_TOKENS(15000))
without a context window → MIN_PRESERVE_TOKENS (2000)
```

**컷오프** (`_determine_cutoff`, 668행)는 히스토리에서 어느 꼬리 부분이 그대로 살아남을지 고릅니다:

1. 히스토리를 턴으로 나누고(`split_into_turns`), **가장 최근에서 거꾸로** 거슬러 올라가며 예산이 찰 때까지 턴 크기를 누적합니다.
2. 턴 전체가 들어가지 않으면 턴 중간에서 잘라(`split_turn`) 남은 예산을 정확히 소진할 수 있습니다.
3. `_adjust_for_orphan_pairs`(698행)가 이어서 컷오프를 **뒤로** 되감아 어떤 `ToolMessage`도 자신의 `AIMessage` 도구 호출과 분리되지 않게 합니다: 호출이 요약으로 사라진 도구 결과는 API 오류가 됩니다.
4. `_compress_last_turn`이 설정되지 않는 한 컷오프는 마지막 `HumanMessage`를 넘지 않도록 고정됩니다: 현재 질문은 항상 그대로 보존됩니다.

⚠️ **noop 함정:** 히스토리 전체가 예산 안에 들어가면 거꾸로 걷기가 컷오프를 전혀 움직이지 못하고 `0`에 머물러, 이번 라운드에서는 아무 요약도 일어나지 않습니다 (`cutoff == 0 → "noop"`, 1045–1047/1117–1119행). 기본 하한 `MIN_PRESERVE_TOKENS = 2000` 때문에 약 2000 추정 토큰보다 작은 히스토리는 절대 LLM 요약되지 않습니다. 통합 테스트는 요약 경로를 결정론적으로 exercise하기 위해 작은 `main_llm_context_window`(예: 8 000)를 주입합니다.

## 🔁 `_apply_compression` 내부의 압축 파이프라인

`_apply_compression`(1015행; 비동기 쌍둥이는 1087행)은 다음 순서로 실행됩니다:

1. **복구 컨텍스트 포착** (`_capture_recovery_context`, 904행): 마지막 사용자 요청(≤ 800자)과 파일 연산 래칫, 즉 `read`/`write` 계열 도구 호출에서 추출한 경로를 이전 라운드의 집합과 병합합니다(읽은 것은 기억되고, 수정된 파일은 읽기 전용으로 강등되지 않습니다).
2. **비 LLM 전략** (`_run_non_llm_strategies`, 851행): `dedup → prune → target truncate` (자세한 내용은 아래). 모델 호출이 없는 공짜 단계입니다.
3. **LLM 호출 여부 판단** (1030행):

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   비 LLM 축소에 첫 기회가 주어집니다; 히스토리가 여전히 보존 예산의 두 배를 넘거나(또는 스래싱 방지 거버너가 LLM 요약을 비활성화했거나, 비 LLM 전략이 아무것도 줄이지 못했을 때)에만 보조 LLM이 사용됩니다.
4. **공격적 백스톱** (1052행): 결과가 그래도 `budget × 2`를 넘으면 `AGGRESSIVE_TRUNCATE_CHARS (1000)`자를 넘는 모든 `ToolMessage`를 하드 컷합니다.
5. **요약 자체 절단** (`_truncate_summary_messages`, 957행): 기존 요약 메시지(`lc_source == "summarization"`)가 `SUMMARY_TOTAL_MAX_CHARS (16 000)`자를 넘으면 앞 30% / 뒤 30%로 재절단합니다.
6. **복구 정보 주입** (`_inject_recovery_context`, 922행): 포착된 파일 연산 래칫을 요약의 `## Relevant Files` 섹션에 다시 써넣어, 체크포인트가 항상 최신 읽기/수정 파일 지도를 갖게 합니다.
7. **기록** (`_record_compression`, 635행), 마지막으로 `request.override(messages=..., system_message=...)`.

모든 실패 모드는 fail-open입니다: `_apply_compression`이 예외를 던지면 예외는 로깅되고(1217행) 원래 요청이 그대로 진행됩니다. 압축이 망가져도 턴이 망가지지는 않습니다.

## 📝 LLM 요약: 프롬프트, 체이닝, 폴백

`_create_summary` / `_acreate_summary` (768–816행):

1. **직렬화** (`_serialize_for_summary`, 167행): 각 메시지가 태그가 붙은 한 줄이 됩니다 — `[User]:` (≤ 2000자), `[Assistant]:` (≤ 2000자), `[Assistant tool call]: name(args ≤ 500 chars)`, `[Tool result|Tool error] (id):` (≤ 1800자 + 생략 마커).
2. **이전 체크포인트 체이닝** (`_extract_previous_summary`, 734행): `additional_kwargs["lc_source"] == "summarization"`을 가진 가장 최근의 `AIMessage`를 찾아 `<summary>…</summary>` 본문을 추출합니다. 존재하면 프롬프트는 `_SUMMARY_PROMPT_FIRST` 대신 `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE`가 됩니다(목표/제약/결정을 앞으로 전달, 충돌 시 최신 우선, FIFO 상한).
3. 보조 모델을 `config={"metadata": {"lc_source": "summarization"}}`와 함께 **호출**하여 다운스트림 도구가 요약 호출을 식별할 수 있게 합니다.
4. **가드 레일:** 비어 있거나 50자보다 짧은 응답은 결정론적 요약으로 폴백하고(785행), 어떤 예외도 마찬가지입니다(789행). 실패 시 LLM에게 최종 결정권을 주지 않습니다.

프롬프트 템플릿(`_SUMMARY_TEMPLATE`, 99행)은 Markdown 골격을 고정합니다 — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — "비어 있어도 모든 섹션을 유지"와 비밀 유지 규칙("NEVER include API keys, tokens, passwords, secrets")이 함께합니다. `_enforce_fifo_limits`(283행)는 반환된 텍스트에 항목 상한을 결정론적으로 다시 적용하고 `"(N earlier items omitted for brevity)"`를 덧붙입니다.

## 🧱 정적 폴백 (LLM 없는 요약)

`_build_static_fallback_summary`(198행)는 모델 호출 없이 같은 섹션 골격을 만듭니다:

- 마지막 사용자 요청 → *Latest Unresolved User Request*; 첫 요청 → *Goal*;
- 결정 키워드(`decided`, `choosing`, `because`, `therefore`)가 들어간 AI 텍스트 → *Key Decisions*, 아니면 *Completed*;
- 모든 도구 호출 → *Completed*; 경로 같은 토큰(`/` 또는 `\` 포함, 또는 `.py`/`.md`/…로 끝남) → *Relevant Files* (≤ 10);
- 오류 `ToolMessage` → *Blocked*와 *Critical Context*.

`skip_llm`이 활성화되면 그대로 사용되며, 짧거나 실패한 LLM 요약의 안전망으로도 쓰입니다.

## 🧹 비 LLM 전략

세 전략 모두 한 번의 패스에서 실행되고(851행) `PROTECTED_TOOLS = {"memory", "skill_view", "skill_list"}`를 존중합니다:

| 전략 | 모듈 | 메커니즘 |
| :------- | :----- | :-------- |
| **Dedup** | `tool_output_dedup.py` | 반복되는 동일한 도구 출력을 접어서 하나로 만듦 |
| **Prune** | `tool_output_prune.py` | `ToolMessage`를 **최신 → 오래된** 순으로 순회, 요약 메시지나 `status="compacted"` 결과에서 정지; 보호 도구는 건너뜀; 크기 누적 (chars // 4) — 가장 최근의 `PRUNE_PROTECT_TOKENS (40 000)` 토큰을 넘는 출력은 내용이 `[Old tool result content cleared]`로 교체됨. 총 절감량 ≥ `PRUNE_MIN_REDUCTION_TOKENS (5 000)`일 때만 적용 |
| **Target truncate** | `target_truncation.py` | 과도하게 큰 출력을 `current_tokens × TARGET_TRUNCATE_RATIO (0.5)` 방향으로 축소: `MIN_OUTPUT_CHARS_TO_TRUNCATE (500)`자 이상의 출력을 `MAX_TOOL_OUTPUT_CHARS (2 000)`자로 절단 |

선제적 절단(파이프라인 이전, 517행)은 추가로 보호되지 않은 모든 `ToolMessage`를 2000자로 제한하며, 앞 30%/뒤 30%를 유지하고 `...[omitted N chars]...` 마커를 붙입니다.

## 📦 출력: 요약 메시지 쌍

`_build_new_messages`(822행)는 요약 텍스트를 감싸 정확히 두 개의 메시지를 만듭니다:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — 역할 교대을 유지하는 중립적인 질문.
- **AIMessage**는 `additional_kwargs={"lc_source": "summarization"}`를 가짐 — 이후 턴들이 (a) 이전 체크포인트를 찾아 체이닝하고, (b) prune이 체크포인트에서 멈추게 하며, (c) 종합 테스트가 요약이 대체된 뒤 모델 뷰에서 삼켜질(swallable) 수 있음을 단언하게 하는 마커.
- 전체 내용은 `SUMMARY_TOTAL_MAX_CHARS (16 000)`자로 제한되며, 앞/뒤 30/30을 유지합니다.

## 🛡️ 스래싱 방지 및 열화 복구

상태는 세션 스코프의 `state_register_mem` 아홉 개 `summarization_*` 키 아래에 살며, 매 턴 `before_agent`가 리셋합니다(1159행).

**압축 거버너** (`_should_skip_compression` / `_record_compression`, 613–662행):

| 가드 | 임계값 | 효과 |
| :---- | :-------- | :----- |
| 총 시도 횟수 | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | 세션 전체에서 압축을 완전히 중단 |
| 연속 무효 횟수 | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` 설정 — 비 LLM 전략만 허용 |
| 유효성 | 메시지 수 감소 **또는** 토큰 절감 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 성공한 비 LLM 전략은 `skip_llm`을 다시 해제 |

**열화 모니터** (`_monitor_degradation`, 988행): 압축 후 모델의 응답에 텍스트가 없으면 카운터가 증가하고, `DEGRADATION_NO_TEXT_THRESHOLD (3)`회 연속 빈 응답에 도달하면 미들웨어가 강제 복구를 실행합니다 — 카운터 리셋, `skip_llm` 해제, 압축 재활성화 — 최대 `MAX_RECOVERY_ATTEMPTS (2)`회까지. 비어 있지 않은 응답은 카운터를 리셋합니다. 이 가드는 "압축 → 모델 혼란 → 빈 출력 → 다시 압축"의 병적 루프를 잡아냅니다.

## 🔄 시스템 프롬프트 갱신

메인 에이전트 전용 (`need_update_system_prompt=True`, 1068–1074행): 압축 후에는 페르소나 파일/장기 기억의 관련성이 달라졌을 수 있으므로, 미들웨어가 `memory_store`를 디스크에서 다시 로드하고, `workspace.prompt_builder.build_system_prompt(session_id)`로 시스템 프롬프트를 재구축한 뒤, 그 값을 `state_register_mem`과 `state_register_db` **양쪽**의 `system_prompt` 키에 기록하고 `request.override(system_message=SystemMessage(...))`로 주입합니다. 바깥의 `ContextEngineHook`이 이후 호출에서 레지스터로부터 이 값을 가져갑니다.

## 📌 등록 위치

```python
# agent/core.py:152 — main agent (Summarization is the LAST middleware:
# innermost wrap layer, closest to the LLM)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — worker agent (first middleware)
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

모든 임계값은 `config/num.py`에 있습니다. ◆ 표시 값은 미들웨어가 가져다 쓰는 값이고, ○ 표시 값은 정의만 있고 미들웨어가 **소비하지 않는** 값입니다(정직한 한계 고지 참조).

| 상수 | 값 | 소비 위치 |
| :------- | :---- | :------------- |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | 선제 게이트 — truncate-only 임계값 |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | 선제 게이트 — compact 임계값; 두 트리거 절 구성에도 사용 |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 예산 하한; 컨텍스트 윈도우 없을 때의 예산 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 예산 상한 |
| `PRESERVE_RATIO` ◆ | `0.25` | 예산 = 컨텍스트 윈도우의 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | prune: 가장 최근에 보존되는 도구 출력 토큰 |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | prune: 적용을 위한 최소 절감량 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | target-truncate: 현재 토큰의 50% 방향으로 축소 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | target-truncate: 적용 자격 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | target-truncate: 출력당 상한 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 공격적 백스톱 절단 길이 |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 요약 메시지 문자 상한 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | 모든 앞/뒤 유지 비율 |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 강제 복구 전 빈 응답 횟수 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 강제 복구 예산 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | 거버너: 세션 시도 상한 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | 거버너: 연속 무효 → LLM 생략 |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | 거버너: 토큰 절감 유효성 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | 모든 축소 전략에서 면제 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 마지막 턴 압축 게이트 |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO 섹션 상한 |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 파일 연산 래칫 목록 상한 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 복구 컨텍스트 요청 상한 |
| `CHARS_PER_TOKEN` (추정기) | `4` | 결정론적 토큰 추정의 나눗수 |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 미들웨어가 가져오지만 읽지 않음 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 미들웨어가 가져오지만 읽지 않음 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 정의만 있고 가져오지 않음 |
| `COMPRESSION_RESERVE_TOKENS` ○ | `16_000` | 정의만 있고 가져오지 않음 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 정의만 있고 가져오지 않음 (900자 목록 상한만 사용) |

## 🧪 테스트

| 스위트 | 커버 범위 |
| :---- | :----- |
| `tests/module/test_summarization_comprehensive.py` | 140케이스 모듈 스위트: 트리거 게이트, 예산/컷오프, FIFO 상한, 폴백, prune/dedup/target-truncate, 열화 |
| `tests/integration/test_interrupt_marker_approach.py` | 마커 의미론: 요약 쌍이 이후 압축에서도 생존; `AIMessage`의 `lc_source`; 마지막 턴 압축 |
| `tests/unit/test_pub_func_message_tools.py` | 추정기, prune (마커 교체, 보호 윈도우, 최소 절감 게이트) |
| `tests/module/test_summarization_trigger.py` | 프로덕션 등록 계약(미캡 윈도우, 0.80 임계값) + 저토큰 패스스루 회귀 |
| `tests/integration/` 밀폐 e2e | 네트워크 접근 없는 전체 그래프 정적 폴백 압축 |

전체 프로세스 격리 스위트(`uv run python tests/run_tests_split.py`)는 **2071 passed / 0 failed**로 통과합니다 (GROUP A 1384P/2S + GROUP B 687P/5D).

## ⚠️ 정직한 한계 고지

- **`keep=("messages", 10)`은 받아들여지지만 사용되지 않습니다.** 생성자가 API 호환성을 위해 저장할 뿐, 실제 꼬리 보존은 순수하게 예산 기반입니다(`PRESERVE_RATIO` × 컨텍스트 윈도우, [2000, 15000]으로 클램프). `keep`을 바꿔도 효과가 없습니다.
- **문서 그대로의 임포트.** `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, `AUTO_CONTINUE_PROMPT`는 `summarization.py` 상단에서 임포트되지만 읽히지 않습니다 — 이 파일이 작성된 명세와 함께 옮겨진 것이며 lint 게이트 면제로 커버됩니다. `DEGRADATION_MONITOR_COUNT`, `COMPRESSION_RESERVE_TOKENS`, `FILE_OPS_SECTION_MAX_CHARS`는 `config/num.py`에 정의되지만 아무도 소비하지 않습니다.
- **추정기는 토크나이저가 아니라 `chars // 4`입니다.** 의도적으로 결정론적(재현 가능한 테스트, 안정적인 예산)이며 영어/코드 혼합 콘텐츠에 맞춰 보정되었습니다; CJK가 많은 콘텐츠는 과소 계수됩니다(중국어는 4보다 토큰당 1–2자에 가깝습니다).
- **보고된 사용량이 추정치를 이깁니다.** 마지막 `AIMessage`가 `usage_metadata.total_tokens`를 갖고 있으면 그 숫자(전체 API 측 카운트를 포함)가 트리거링을 주도합니다 — 로컬 추정은 어디까지나 폴백입니다.
- **압축은 fail-open입니다.** `_apply_compression` 내부의 어떤 예외도 로깅되고 삼켜지며, 그 턴은 압축되지 않은 히스토리로 진행됩니다. 따라서 체계적으로 망가진 보조 LLM은 깨진 턴이 아니라 더 잦은 비 LLM 축소로 귀결됩니다.
- **정적 폴백은 휴리스틱입니다.** 키워드 기반의 결정/완료 분류와 원시 도구 인자로부터의 경로 추출은 최선 노력(best-effort)입니다: 섹션 골격은 보장되지만 내용 품질은 아닙니다.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 태그/`lc_source="summarization"`은 하중을 지는 정확한 문자열입니다.** 이후 턴의 체이닝(`_extract_previous_summary`), prune 정지 조건, 테스트 스위트가 모두 문자 그대로 일치시킵니다 — 함부로 다시 표현하지 마십시오.
