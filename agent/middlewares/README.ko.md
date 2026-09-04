# EMA Agent 미들웨어 시스템

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent의 미들웨어 계층: 모델 호출과 도구 호출의 모든 단계에 개입하는 8개의 `AgentMiddleware` 컴포넌트 — 컨텍스트 엔지니어링, 멀티모달 입력 처리, 반복 예산, 도구 가드레일, 트랜스크립트 복구, 하트비트 스테일니스 감지, 휴먼인더루프 승인, 컨텍스트 요약 — 그리고 워커 에이전트가 사용하는 출력 반복 가드.

> 이 문서의 모든 서술은 소스 코드를 기준으로 검증되었습니다(설치된 `langchain 1.3.9`, `agent/core.py`, `agent/tools/subagent/spawn/core.py`, 그리고 `agent/middlewares/` 하위 모듈). 아래에 등장하는 클래스명·파일명·기본값·상태 키는 모두 실제 코드에 존재합니다.

---

## 목차

- [아키텍처 개요](#아키텍처-개요)
- [미들웨어 체인](#미들웨어-체인)
- [미들웨어 레퍼런스](#미들웨어-레퍼런스)
  - [ContextEngineHook](#contextenginehook)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [Summarization](#summarization)
  - [OutputRepetitionGuard와 RepetitionGuardWrapper](#outputrepetitionguard와-repetitionguardwrapper)
- [공유 상태 시스템](#공유-상태-시스템)
- [설정](#설정)
- [수명주기 및 데이터 흐름](#수명주기-및-데이터-흐름)
- [커스텀 미들웨어 작성](#커스텀-미들웨어-작성)
- [부록](#부록)

---

## 아키텍처 개요

### 미들웨어란?

미들웨어는 `langchain.agents.middleware.AgentMiddleware`를 상속하여 에이전트 루프의 명확히 정의된 지점에서 후킹합니다. 시스템은 네 개의 후크 패밀리를 사용합니다(모두 동기/비동기 형태 제공):

| 후크 패밀리 | 동기 | 비동기 | 실행 시점 |
|---|---|---|---|
| 에이전트 전/후 | `before_agent` / `after_agent` | `abefore_agent` / `aafter_agent` | 대화 턴당 1회, 모델–도구 루프 전체를 감쌈 |
| 모델 전/후 | `before_model` / `after_model` | `abefore_model` / `aafter_model` | 개별 모델 요청을 감쌈 |
| 모델 호출 래핑 | `wrap_model_call` | `awrap_model_call` | 모델 요청 자체를 인터셉트(메시지 / 시스템 프롬프트 수정, LLM 쇼트서킷) |
| 도구 호출 래핑 | `wrap_tool_call` | `awrap_tool_call` | 각 도구 실행을 인터셉트 |

### 후크 순서 시맨틱스

설치된 `langchain 1.3.9` 소스(`agents/middleware/factory.py` 및 `agents/middleware/types.py`)를 기준으로 검증됨:

- `before_agent` 후크는 **리스트 순서**대로 실행됩니다 — 먼저 등록된 미들웨어가 먼저 실행됩니다.
- `after_agent` 후크는 **리스트 역순**으로 실행됩니다 — 마지막에 등록된 미들웨어의 `after_agent`가 먼저 실행됩니다(컴파일된 그래프의 출구 노드 체인입니다).
- `wrap_model_call` / `wrap_tool_call`은 **리스트의 첫 번째 미들웨어가 최외곽 계층**, 마지막이 최내곽(LLM / 도구에 가장 가까움)으로 합성됩니다.

> ⚠️ 구형 미들웨어 프레임워크에는 `awrap_before_agent` 스타일 후크가 있었지만, LangChain 1.3에는 없습니다. 비동기 형태는 단순히 `a` 접두사를 붙입니다: `abefore_agent`, `abefore_model`, `aafter_model`, `aafter_agent`, `awrap_model_call`, `awrap_tool_call`.

### 상태 영속화

미들웨어 상태는 LangGraph 그래프 상태에 두지 **않습니다**(프레임워크가 관리하는 일부 키 제외). 호출 간 상태는 세션 단위 런타임 레지스터에 보관됩니다:

- `state_register_mem` (`StateRegisterMeM`) — 인메모리 딕셔너리. 휘발성(프로세스 재시작 시 초기화).
- `state_register_db` (`StateRegisterDB`) — SQLite 기반(`src/data/state_register.db`), 재시작 후에도 유지.
- `timer_call_register` (`TimerCallRegister`) — 백그라운드 카운트다운 타이머(1–60분). `HeartbeatStaleness`가 사용.

자세한 내용은 [공유 상태 시스템](#공유-상태-시스템)을 참고하세요.

---

## 미들웨어 체인

### 메인 에이전트 (`agent/core.py`)

```python
middleware = [
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# 컴파일된 그래프를 다시 래핑:
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
```

`main_llm_max_tokens`는 환경 변수 `MAIN_LLM_MAX_TOKEN`에서 읽습니다(`models/LLMs/main_llm.py`). 따라서 메인 에이전트의 요약 트리거는 메인 모델 컨텍스트 윈도우의 80% 지점에 놓입니다(`COMPRESSION_TRIGGER_RATIO = 0.80`).

> **참고:** `OutputRepetitionGuard`는 메인 에이전트의 미들웨어로 **등록되지 않습니다**. 메인 에이전트에는 컴파일된 그래프를 래핑하는 `RepetitionGuardWrapper`가 동일한 역할을 제공합니다 — [OutputRepetitionGuard와 RepetitionGuardWrapper](#outputrepetitionguard와-repetitionguardwrapper) 참조.

### 워커 / 서브에이전트 파이프라인 (`agent/tools/subagent/spawn/core.py`)

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[
            ("messages", 40),
            ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
        ],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# 자식 그래프도 같은 방식으로 래핑:
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

메인 에이전트와의 차이:

- 요약 트리거가 토큰 전용이 아니라 메시지 수(40) **또는** 토큰 수(컨텍스트 윈도우의 80%).
- 더 타이트한 반복 예산(90 대신 60).
- `ContextEngineHook`, `MultimodalProcessor`, `HumanInTheLoop` 없음.
- `OutputRepetitionGuard`는 여기서 실제 미들웨어로 동작.
- 자식 세션이 끝나면 spawn 코드가 `finally` 블록에서 `state_register_mem`으로부터 `OutputRepetitionGuard`의 6개 상태 키(`SESSION_STATE_KEYS`)를 삭제합니다.

### 턴별 실제 실행 순서 (메인 에이전트)

| 페이즈 | 순서 |
|---|---|
| `before_agent` (리스트 순서) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `wrap_model_call` (최외곽 → 최내곽) | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization (Summarization이 LLM에 가장 가까움) |
| `after_agent` (역순) | Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

해당 후크를 구현한 미들웨어만 그 페이즈에 참여합니다. 표는 "구현했다면 실행될 위치"를 보여줍니다.

---

## 미들웨어 레퍼런스

### ContextEngineHook

**모듈:** `agent/middlewares/context_engine/core.py` · **클래스:** `ContextEngineHook(AgentMiddleware)`
**후크:** `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`, `after_agent` / `aafter_agent`

리스트의 첫 번째, 즉 최외곽 래핑 계층입니다.

**`wrap_model_call` — 시스템 프롬프트 주입**

1. `state_register_mem`의 `system_prompt`를 조회합니다.
2. 없으면 `state_register_db`로 폴백하고, 그래도 없으면 `workspace.prompt_builder.build_system_prompt(session_id)`로 재구축합니다.
3. `request.override(system_message=...)`로 주입하고, 프롬프트를 `state_register_mem`에 캐시백합니다.

**`wrap_tool_call` — 스킬 리뷰 집계**

도구 메타데이터에 `nudge: true`가 설정되지 않은 한(nudge/limit 도구 자체의 면제), 모든 도구 호출마다 `state_register_db`의 `nudge_review_skill_count`를 1 증가시킵니다.

**`after_agent` / `aafter_agent` — 턴 마무리**

1. `state_register_db`의 `nudge_review_memory_count`를 1 증가시킵니다.
2. 카운터가 임계값에 도달하면 — `_NUDGE_MEMORY_THRESHOLD = 10` 턴, `_NUDGE_SKILL_THRESHOLD = 10` 도구 호출 — `state_register_mem`의 세션별 락 `nudge_review_memory_lock` / `nudge_review_skill_lock` 하에서 해당 **nudge 서브에이전트**(아래)를 기동합니다. 락이 유지되는 동안 `after_agent`는 nudge 판정을 건너뜁니다(카운터는 계속 증가).
3. 마지막 턴을 MesMemory에 영속화: `slice_last_turn` → `sanitize_tool_use_result_pairing` → `add_messages(session_id, messages)` (SQLite).
4. 동기 `after_agent`는 `run_async`로 서브에이전트를 실행하고, `aafter_agent`는 `asyncio.gather`로 영속화와 nudge를 동시에 실행합니다.

**Nudge 서브에이전트** (`context_engine/nudge.py`): 메인 LLM 기반의 독립적인 `create_agent` 인스턴스로, 미들웨어는 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget()]`. `_NudgeLimitTool`은 메타데이터에 `nudge: true`가 없는 모든 도구를 거부하므로, nudge 에이전트는 메모리/스킬 도구만 사용할 수 있습니다. 프롬프트: `_MEMORY_REVIEW_PROMPT`(메모리 리뷰), `_SKILL_REVIEW_PROMPT`(스킬 라이브러리 리뷰), `_COMBINED_REVIEW_PROMPT`(동시 수행).

> 이 문서의 이전 버전은 지식 그래프 유지관리(`after_turn`)와 `MemoryCache`를 언급했습니다. **현재 코드에는 둘 다 존재하지 않습니다.** 시스템 프롬프트는 상태 레지스터와 `build_system_prompt()`에서 공급되며, 미들웨어 계층 어디에도 지식 그래프 호출은 없습니다.

### MultimodalProcessor

**모듈:** `agent/middlewares/multimodal_processor.py` · **클래스:** `MultimodalProcessor(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `after_agent` / `aafter_agent`

`before_agent`는 내용이 멀티모달 리스트인 **마지막** `HumanMessage`를 처리합니다:

- **텍스트** 항목은 그대로 통과(최대 1개).
- **`image_url`**: 원격 `http(s)` URL은 그대로 유지. `data:` / base64 페이로드는 디코딩되어 PIL로 `src/<session_id>/mutil_temp/<타임스탬프><확장자>`에 저장됩니다(확장자는 `_IMAGE_MAGIC` 매직바이트로 추정), 영구 복사본이 `media/`에도 생성됩니다.
- **`audio_url`**: 임시 파일로 다운로드(30초 타임아웃). **`audio_bytes` / `video_url` / `video_bytes`**: 동일하게 디코딩·저장(`_AUDIO_MAGIC` / `_VIDEO_MAGIC`).
- 메시지 텍스트 끝에 `"[Uploaded media]"` 지시 블록을 덧붙여, `skill_view` 도구인 `image_to_text` / `speech_to_text` / `video_text_to_text`로 파일을 확인하도록 모델에 지시합니다(모델은 네이티브 비전 능력이 없음).
- 영속화된 경로는 `additional_kwargs["images"]` / `["audios"]` / `["videos"]`에 저장되고, 이후 MesMemory에 기록되어 히스토리 렌더링에 사용됩니다.
- **더 오래된** `HumanMessage`에서는 `image_url` 블록이 제거되어, 낡은 base64 덩어리가 컨텍스트에 남지 않습니다.

`after_agent`는 `mutil_temp`를 청소합니다: 파일명 본체가 순수 숫자 타임스탬프가 아니거나 7일보다 오래된 파일을 삭제합니다.

### IterationBudget

**모듈:** `agent/middlewares/iteration_budget.py` · **클래스:** `IterationBudget(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`

한 턴 안의 **모델 호출 + 도구 호출 합계**에 대한 하드 상한. 생성자: `__init__(max_iterations: int = 50)`. 메인 에이전트는 `IterationBudget(90)`, 워커 에이전트는 `IterationBudget(60)`을 등록합니다.

- `before_agent`는 `state_register_mem`의 카운터를 리셋: `iteration_budget = max_iterations`, `iteration_budget_used = 0`.
- `wrap_model_call`은 모델 호출당 1 소모. 예산이 소진되면 **모델을 호출하지 않고** 종단 `AIMessage`를 반환합니다.
- `wrap_tool_call`은 도구 호출당 1 소모. 소진되면 실행 대신 오류 `ToolMessage`("Tool [x] skipped — iteration budget exhausted")를 반환합니다.

### ToolGuardrails

**모듈:** `agent/middlewares/tool_guardrails.py` · **클래스:** `ToolGuardrails(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `wrap_tool_call` / `awrap_tool_call`

세 가지 실패 병리를 감지하고 4단계 에스컬레이션 `ALLOW → WARN → BLOCK → HALT`(`GuardrailAction` 열거형)으로 대응합니다:

| 병리 | 트리거 | 기본 대응 |
|---|---|---|
| 완전한 실패 반복 | 동일 도구 + 동일 인자(인자 JSON을 `sort_keys`한 MD5)의 실패 | ≥ 2회 경고, ≥ 5회 차단 (`exact_failure_warn_after=2`, `exact_failure_block_after=5`) |
| 동일 도구 실패 누적 | 동일 도구가 **다른** 인자로 반복 실패 | ≥ 3회 경고, ≥ 8회 정지 (`same_tool_failure_warn_after=3`, `same_tool_failure_halt_after=8`) |
| 멱등 무진행 | 메타데이터 `idempotent: true` 도구가 동일한 결과 해시를 반환 | ≥ 2회 경고, ≥ 5회 차단 (`no_progress_warn_after=2`, `no_progress_block_after=5`) |

- `before_agent`는 턴 단위 가드 상태를 리셋합니다(`state_register_mem`의 키 `tool_guardrail_state`).
- `wrap_tool_call`은 차단된 도구와 정지 상태를 사전 점검(실행하지 않고 오류 `ToolMessage` 반환)한 뒤 도구를 실행하고 결과를 평가합니다:
  - `warn`은 `ToolMessage`에 경고를 덧붙임;
  - `block`은 도구를 `blocked_tools`에 기록;
  - `halt`는 턴의 나머지 기간에 대한 스티키 정지를 설정(`halt_decision`).
- `ToolCallGuardrailConfig` 기본값: `warnings_enabled=True`, `hard_stop_enabled=False` — `hard_stop_enabled=True`이면 *차단* 수준도 정지로 에스컬레이션됩니다.

### ToolCallNormalize

**모듈:** `agent/middlewares/tool_call_normalize.py` · **클래스:** `ToolCallNormalize(AgentMiddleware)`
**후크:** `before_model` / `abefore_model` 전용

컨텍스트 트리밍 후의 tool-call / tool-result 페어링을 복구하여 프로바이더의 "Message ordering conflict" 오류를 방지합니다. 처리는 `pub_func.sanitize_tool_use_result_pairing(state["messages"])`(`pub_func/transcript_repair.py`에 정의)로 위임되며, 다음을 수행합니다:

- `tool_call_id` 기준으로 `ToolMessage` 중복 제거;
- 빈 `ToolMessage` 제거;
- 누락된 결과에 대한 플레이스홀더 `ToolMessage`("tool result missing after context trim.") 삽입;
- 오류 상태 `AIMessage`의 `invalid_tool_calls`를 클리어하여 OpenAI tool_calls로 직렬화되지 않도록 함.

후크는 메시지 전체 교체를 반환합니다: `[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`.

### SubagentCompletionDrainMiddleware

**모듈:** `agent/middlewares/subagent_completion_drain.py` · **클래스:** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**후크:** `before_model` / `abefore_model` 전용

메인 에이전트에 `ToolCallNormalize` 바로 뒤에 등록되므로, 주입되는 메시지는 주입 턴에서 sanitize 재작성을 우회합니다. `before_model`에서 세션의 `SteeringQueue`—부모가 바쁜 동안 announce 파이프라인이 적립한 완료 캐리어 메시지—를 재하이드레이트하고 배출(drain)하여 `{"messages": [carrier, ...]}`를 반환함으로써, 다음 모델 호출 직전에 재구축된 완료 캐리어 `HumanMessage`를 주입합니다.

- 배출된 각 큐 항목은 큐의 SQLite 저장소에서 `CONSUMED`로 마킹되므로 캐리어는 정확히 한 번만 주입됩니다(체크포인트 영속화가 HITL 재개 리플레이의 안전성을 보장).
- Fail-open: `session_id` 누락/빈 값, 빈 큐, 그리고 모든 오류는 삼켜집니다(로그 + no-op) — drain이 부모 턴을 깨뜨리지 않으며, 큐는 재시도를 위해 보존됩니다.
- 주입된 캐리어는 `origin='subagent_completion'`으로 MesMemory에 영속화됩니다.

### HeartbeatStaleness

**모듈:** `agent/middlewares/heartbeat_staleness.py` · **클래스:** `HeartbeatStaleness(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `after_agent` / `aafter_agent`, `wrap_model_call` / `awrap_model_call`, `wrap_tool_call` / `awrap_tool_call`

멈춘 턴을 위한 워치독. **메인 에이전트와 워커 에이전트 양쪽에** 등록되어 있습니다(이 문서의 이전 버전은 워커 전용이라고 주장했습니다 — 잘못된 정보였습니다).

- `before_agent`는 상태 키를 리셋하고 `timer_call_register.register(..., execute_now=True)`로 백그라운드 타이머를 시작합니다(1분 주기).
- `wrap_model_call`은 `heartbeat_iter`를 증가시킵니다 — 단, 이전 점검에서 이미 턴이 kill되었다면 먼저 `HeartbeatTimeoutError`를 발생시킵니다. `wrap_tool_call`은 도구 실행 중 `heartbeat_tool`을 설정하고 반환 후 클리어합니다.
- 타이머 콜백은 `(heartbeat_iter, heartbeat_tool)`을 `_last_heartbeat_iter` / `_last_heartbeat_tool`과 비교합니다. 진행이 있으면 스테일 카운터를 리셋, 없으면 증가. 아이들 상태에서 `stale_cycles_idle = 7`회, 또는 하나의 도구 안에 갇혀 `stale_cycles_in_tool = 20`회에 도달하면 `heartbeat_killed = True`가 되어, 다음 모델 / 도구 호출은 계속 진행하는 대신 `HeartbeatTimeoutError`를 발생시킵니다.
- `after_agent`는 타이머를 중지합니다.
- 상태 키: `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, 그리고 `_last_heartbeat_iter` / `_last_heartbeat_tool`.

### HumanInTheLoop

**모듈:** `agent/middlewares/humanInTheLoop/core.py` · **클래스:** `HumanInTheLoop(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `after_model` / `aafter_model`, `wrap_tool_call` / `awrap_tool_call`

메인 에이전트에는 `HumanInTheLoop(HITLConfig())`로 등록 — 모두 기본값, 즉 모드 `ApprovalMode.SMART`. 각 모델 응답 후에 도구 호출을 인터셉트하고, 정책이 요구하면 LangGraph 네이티브 `interrupt()`로 그래프를 일시 중단시켜 프런트엔드가 승인 다이얼로그를 렌더링하게 합니다. 거부된 호출은 오류 `ToolMessage`(`BLOCKED_MESSAGE`)로 대체되며, `GraphInterrupt`는 삼켜지지 않고 재발생됩니다.

`after_model`에서 호출별 파이프라인:

1. 하드라인 / 위험 명령 감지(`detection.py`: `detect_hardline_command`, `detect_dangerous_command`, 기반은 `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS`)를 `ApprovalPipeline.check_command`(`approval.py`)로 수행.
2. 스마트 승인(`ApprovalMode.SMART`, 선택적 `smart_approval_llm`) — 명백히 안전한 호출을 자동 승인.
3. `interrupt()` — 기본 결정 타임아웃 60초.
4. `write_approval_memory=True`일 때 메모리 도구 쓰기는 `WriteApprovalGate`를 통과. `interrupted_tools`에 나열된 도구는 항상 인터럽트되며 결정은 `approve` / `edit` / `reject`(`edit`은 도구 호출의 인자/이름을 재작성).
5. `wrap_tool_call`은 승인이 거부되었거나 타임아웃된 호출의 실행을 거부합니다(턴 단위 플래그는 `before_agent`에서 리셋).

서브게이트(`gates.py` / `approval.py`): `ApprovalPipeline`, `WriteApprovalGate`, `InterruptManager`, `MCPElicitationConsent`, `KanbanTriage`, `PairingStore`, `SlashConfirm`. 상태는 `state_register_mem`에 `hitl:` 접두사 키로 저장됩니다.

`HITLConfig` 기본값:

| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `mode` | `ApprovalMode.SMART` | `SMART` / `MANUAL` / `OFF` |
| `timeout` | `60` | 인터럽트 결정 타임아웃 |
| `deny_rules` | `[]` | 명시적 거부 패턴 |
| `yolo_mode` | `False` | 모든 승인 건너뛰기 |
| `write_approval_memory` | `False` | 메모리 도구 쓰기 게이트 |
| `write_approval_skills` | `False` | 스킬 쓰기 게이트 |
| `clarify_timeout` | `3600` | 명확화 질문 타임아웃 |
| `kanban_recurrence_limit` | `3` (`BLOCK_RECURRENCE_LIMIT`) | 칸반 트리아지 전 반복 차단 한도 |
| `mcp_reload_confirm` | `True` | MCP 서버 리로드 확인 |
| `destructive_slash_confirm` | `True` | 파괴적 슬래시 명령 확인 |
| `smart_approval_llm` | `None` | 스마트 자동 승인에 사용할 LLM |
| `interrupted_tools` | `{}` | 항상 `interrupt()`를 일으키는 도구 |
| `description_prefix` | `"Action requires human approval"` | 승인 다이얼로그 제목 접두사 |

▶️ 전체 문서: [humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### Summarization

**모듈:** `agent/middlewares/summarization.py` · **클래스:** `Summarization(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`(카운터 리셋), `wrap_model_call` / `awrap_model_call`

최내곽 미들웨어 — LLM에 가장 가까운 위치. 처음부터 직접 구현한 `AgentMiddleware`입니다(LangChain의 `SummarizationMiddleware` **아님**): 트리거가 발동하면 예산 기반 컷오프로 히스토리를 압축합니다 — 비(非)LLM 전략 우선, 텍스트 저하가 안전할 때만 보조 LLM 요약 사용. `keep` 파라미터는 받아들이지만 사용하지 않으며, 꼬리 보존은 예산 기반입니다: `clamp(context_window × 0.25, 2 000, 15 000)` 토큰(`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`).

- **트리거 시맨틱스**: 절은 `("messages", N)` 또는 `("tokens", N)`이며, 절 리스트 사이는 **OR** — 절이 하나라도 발동하면 압축이 시작됩니다. 메인 에이전트: `[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`. 워커: `[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`. `COMPRESSION_TRIGGER_RATIO = 0.80`.
- **컷오프 안전성:** `_determine_cutoff`가 컷오프 지점을 고르고, 이어서 `_adjust_for_orphan_pairs`가 `ToolMessage`가 자신의 `AIMessage` 도구 호출과 분리되지 않을 때까지 위치를 뒤로 이동시킵니다. 마지막 사용자 턴이 추정 토큰의 ≥ 50%를 차지하면(`LAST_TURN_RATIO_THRESHOLD = 0.5`), 그 턴을 요약으로 없애는 대신 턴 자체를 압축합니다(`_compress_last_turn`).
- **안티스래싱:** 세션당 최대 `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5`회 압축(턴당이 아님). 연속 `INEFFECTIVE_THRESHOLD = 2`회 무효 압축이면(유효 = 메시지 수 감소 또는 토큰 절감 ≥ `MIN_EFFECTIVENESS_PCT = 0.05`) LLM 단계를 비활성화(`summarization_skip_llm`)하고 비(非)LLM 전략만 실행합니다. 카운터는 세션 단위 `summarization_*` 키로 `state_register_mem`에 저장됩니다(압축 횟수, 무효 연속, 마지막 토큰, 마지막 전략, 스킵 플래그, 복구 상태 등).
- **절단:** 기존 요약 메시지(`additional_kwargs["lc_source"] == "summarization"`로 식별)가 `SUMMARY_TOTAL_MAX_CHARS = 16 000`자를 넘으면 재절단되어, 머리 30% / 꼬리 30%(`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`)를 유지하고 생략 마커가 삽입됩니다.
- **출력:** 교체 메시지는 `HumanMessage` / `AIMessage` **쌍**입니다 — 중립적인 `"What did we do so far?"` 뒤에 `additional_kwargs={"lc_source": "summarization"}`을 담은 `AIMessage`가 이어집니다 — 모델이 연속된 같은 역할 메시지를 보는 일이 없어 사후 페어링 복구도 필요 없습니다.
- `need_update_system_prompt=True`(메인 에이전트만): 압축 후 시스템 프롬프트를 재구축 — 메모리 스토어를 다시 로드한 뒤 `build_system_prompt()` 호출 — 하여 `system_prompt` 키로 두 상태 레지스터에 기록합니다.

▶️ 전체 문서: [docs/harness/summarization/README.md](../../docs/harness/summarization/README.md) · [中文](../../docs/harness/summarization/README.zh.md) · [한국어](../../docs/harness/summarization/README.ko.md) · [日本語](../../docs/harness/summarization/README.ja.md)

> 예산 미들웨어의 클래스 기본값 `max_iterations`는 50입니다. *실제 등록된* 값은 90(메인)과 60(워커). 이 문서의 이전 버전은 예산 10이라고 주장했습니다 — 잘못된 정보였습니다.

### OutputRepetitionGuard와 RepetitionGuardWrapper

**모듈:** `agent/middlewares/output_repetition_guard.py` · **클래스:** `OutputRepetitionGuard(AgentMiddleware)`
**후크:** `before_agent` / `abefore_agent`, `wrap_model_call` / `awrap_model_call`

사후(事後)형 출력 반복 감지기로, `WARN → HALT` 에스컬레이션을 가집니다. `agent.middlewares.output_repetition_guard`에서 익스포트되며(`agent/middlewares/__init__.py`에서는 **재익스포트되지 않음**), **워커 파이프라인에만 등록**됩니다.

메인 에이전트에서는 동일한 감지가 **`RepetitionGuardWrapper`**(`agent/stream_repetition_guard_wrapper.py`)를 통해 수행됩니다. 이것은 컴파일된 그래프를 래핑하고, 스트림 수준에서 인터셉트하며(`ainvoke`의 사후 백스톱 포함), 같은 상태 키와 기본값을 재사용합니다. 두 등록 모두 `phantom_stream_guard=True`를 전달합니다.

**감지 계층**

- **호출 간 반복** — 가시 출력의 마지막 `_TAIL_CHARS = 500`자의 MD5를 롤링 히스토리(`_MAX_HISTORY = 30`)와 비교. `warn_after = 2`개의 동일 출력에서 WARN(`AIMessage`로 주의 환기), `max_identical_outputs = 3`에서 HALT — 종단 `AIMessage`와 스티키 정지 플래그를 반환.
- **단일 출력 내 반복**:
  - 문장/줄 중복률 > `internal_repeat_ratio = 0.6`(세그먼트 수 ≥ `internal_min_lines = 6`);
  - `char_run_min = 8`개 이상의 동일한 공백 아닌 문자 연속;
  - 2–10자의 짧은 구문이 ≥ 5회 반복.

  내부 경고는 라벨별로 세션당 1회만 발화합니다.
- `_MIN_CONTENT_LENGTH = 20`자 미만의 내용은 건너뜀. 도구 호출을 포함한 모델 응답은 통째로 건너뜁니다(도구 루프 후에 재점검).
- **추론 내용은 별도 추적**됩니다(`additional_kwargs`의 `reasoning_content` / `reasoning` / `reasoning_text`, 그리고 가시 내용에서 추출·제거되는 인라인 `<think>` / `<thinking>` / `<reasoning>` 블록).

**스트림 계층 헬퍼** `check_stream_repetition(session_id, accumulated_text)` — 공유 `_STREAM_GUARD` 싱글턴으로, `server/service/messages.py::async_generate`가 반복 감지 시 스트리밍 응답을 조기에 차단하는 데 사용합니다. 같은 상태 키와 내부 경고 중복 제거 게이트를 공유합니다.

**워커 클린업:** 자식 세션 종료 시 `SESSION_STATE_KEYS`(6개 키)가 `state_register_mem`에서 삭제됩니다.

---

## 공유 상태 시스템

호출 간 모든 미들웨어 상태는 세션 단위로, 두 개의 레지스터와 타이머 레지스트리에 보관됩니다:

| 레지스터 | 백엔드 | 비고 |
|---|---|---|
| `state_register_mem` (`StateRegisterMeM`) | 인메모리 딕셔너리 | 휘발성. `_initialized` 가드로 프로세스 시작 시 1회만 리셋 |
| `state_register_db` (`StateRegisterDB`) | SQLite (`src/data/state_register.db`) | 재시작 후에도 유지. `clear_session` 미지원(`False` 반환), `get_all_session_ids` 제공 |
| `timer_call_register` (`TimerCallRegister`) | asyncio 타이머 | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

공통 인터페이스(`runtime/state_register.py`): `set_state`, `get_state`, `get_all_states`, `delete_state`, `clear_session`, `has_session`, `has_key`, `update_states`.

### 네임스페이스 컨벤션

| 키 | 소유자 | 레지스터 |
|---|---|---|
| `system_prompt` | ContextEngineHook / Summarization | mem + db |
| `nudge_review_memory_count`, `nudge_review_skill_count` | ContextEngineHook | db |
| `nudge_review_memory_lock`, `nudge_review_skill_lock` | ContextEngineHook | mem |
| `iteration_budget`, `iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_*` 키(압축 카운터, 무효 연속, 마지막 토큰/전략, 스킵 LLM 플래그, 복구 상태, 마지막 사용자 질문) | Summarization | mem |
| `heartbeat_iter`, `heartbeat_tool`, `heartbeat_stale`, `heartbeat_killed`, `_last_heartbeat_iter`, `_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard 키(`SESSION_STATE_KEYS`, 6개) | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `hitl:` 접두사 키(`_STATE_PREFIX = "hitl"`) | HumanInTheLoop | mem |

---

## 설정

### 환경 변수와 설정 노브

| 노브 | 위치 | 효과 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 메인 에이전트의 요약 트리거 = 이 값의 80%. `main_llm_context_window`로도 전달 |

> **관련되지만 독립적:** 도구별 타임아웃은 하드코딩된 모듈 상수입니다 — `WEB_SEARCH_TIMEOUT = 15`(`agent/tools/web_search.py`), `TERMINAL_TIMEOUT = 30`(`agent/tools/terminal.py`), `PYTHON_REPL_TIMEOUT = 30`(`agent/tools/python_repl.py`, 만료 시 자식 프로세스 kill). `.env.example`의 `TOOL_CALL_TIMEOUT_MINUTES = 5`는 **이를 소비하는 코드가 존재하지 않습니다** — 유효한 노브가 아닙니다. `config/num.py`의 상수(`ARCHIVE_THRESHOLD`, `MEMORY_THRESHOLD`, `COMPRESS_RATIO`)도 미들웨어 계층에서 소비되지 않습니다.

### 빌드 예제

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook, MultimodalProcessor, IterationBudget, ToolGuardrails,
    ToolCallNormalize, HeartbeatStaleness, HumanInTheLoop, HITLConfig, Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),          # 시스템 프롬프트 + nudge + 영속화
        MultimodalProcessor(),        # 멀티모달 입력 정규화
        IterationBudget(90),          # 턴 단위 호출 예산
        ToolGuardrails(),             # 실패 병리 감지
        ToolCallNormalize(),          # tool_use/tool_result 복구
        HeartbeatStaleness(),         # 멈춘 턴 워치독
        HumanInTheLoop(HITLConfig()), # 승인 게이트
        Summarization(                # 컨텍스트 압축 (최내곽)
            need_update_system_prompt=True,
            model=auxiliary_llm,
            main_llm_context_window=main_llm_max_tokens,
            trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
            keep=("messages", 10),
        ),
    ],
)
```

### 미들웨어별 파라미터

| 미들웨어 | 파라미터 | 기본값 | 등록값 |
|---|---|---|---|
| `IterationBudget` | `max_iterations` | `50` | `90`(메인) / `60`(워커) |
| `Summarization` | `need_update_system_prompt` | `False` | `True`(메인) |
| `Summarization` | `model` | 필수 | `auxiliary_llm` |
| `Summarization` | `main_llm_context_window` | 필수 | `main_llm_max_tokens` |
| `Summarization` | `trigger` | 필수 | [미들웨어 체인](#미들웨어-체인) 참조 |
| `Summarization` | `keep` | 필수 | `("messages", 10)`(받아들이지만 미사용) |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | 위 기본값 | 기본값 |
| `HumanInTheLoop` | `config: HITLConfig` | 위 기본값 | 기본값 |
| `HeartbeatStaleness` | (기본) | 주기 1분, 아이들 7 / 도구 내 20 | 기본값 |
| `OutputRepetitionGuard` | (기본) | 3 / 2 / 0.6 / 6 / 8 | 기본값 |

---

## 수명주기 및 데이터 흐름

### 단일 턴 (상세)

```
사용자 턴 도착
│
├─ before_agent (리스트 순서)
│   ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails
│   → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · ContextEngineHook   여기서는 아무것도 안 함(영속화는 after_agent에서)
│   · MultimodalProcessor  마지막 HumanMessage 정규화, 오래된 image_url 블록 제거
│   · IterationBudget  예산 카운터 리셋
│   · ToolGuardrails  턴 단위 가드 상태 리셋
│   · HeartbeatStaleness  상태 키 리셋 + 1분 주기 하트비트 타이머 시작
│   · HumanInTheLoop  턴 단위 인터럽트 플래그 리셋
│   · Summarization  압축 카운터 리셋
│
├─ 루프: 모델 호출
│   ├─ before_model
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage 재작성
│   ├─ wrap_model_call (최외곽 → 최내곽)
│   │   · ContextEngineHook  시스템 프롬프트 주입(request.override)
│   │   · IterationBudget  1 소모. 소진 시 종단 AIMessage
│   │   · HeartbeatStaleness  kill됐으면 HeartbeatTimeoutError, 아니면 heartbeat_iter += 1
│   │   · Summarization  필요 시 히스토리 압축(비(非)LLM 전략 + 보조 LLM), 안티스래싱 카운터
│   ├─ LLM 응답
│   └─ after_model
│       · HumanInTheLoop  정책 점검. 필요 시 interrupt(). 차단 → 오류 ToolMessage
│
├─ 루프: 도구 호출 (호출별)
│   └─ wrap_tool_call
│       · IterationBudget  1 소모. 소진 시 오류 ToolMessage
│       · ToolGuardrails  block/halt 사전 점검 → 실행 → 평가 → warn/block/halt
│       · ContextEngineHook  스킬 리뷰 카운터(도구 메타데이터 nudge: true 제외)
│       · HeartbeatStaleness  kill됐으면 발생. heartbeat_tool 설정 후 반환 시 클리어
│       · HumanInTheLoop  승인 거부/타임아웃된 호출 거부
│
└─ after_agent (역순)
    Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize
    → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook
    · HeartbeatStaleness  하트비트 타이머 중지
    · MultimodalProcessor  mutil_temp 청소(7일 초과 / 숫자 아닌 파일명)
    · ContextEngineHook  메모리 리뷰 카운터 → 필요 시 nudge 서브에이전트(락)
                        → 마지막 턴을 MesMemory에 영속화(slice → sanitize → add_messages)
```

---

## 커스텀 미들웨어 작성

`AgentMiddleware`를 상속하고 필요한 후크만 오버라이드합니다(시그니처는 설치된 `langchain 1.3.9` 기준 — 상태 후크는 `(state, runtime)`, 래핑 후크는 `(request, handler)`를 받습니다):

```python
from langchain.agents.middleware import AgentMiddleware


class MyMiddleware(AgentMiddleware):
    """턴마다, 루프 전후로 1회씩 실행."""

    def before_agent(self, state, runtime):
        # 상태 업데이트 딕셔너리를 반환하거나 None
        return None

    def after_agent(self, state, runtime):
        return None

    def wrap_model_call(self, request, handler):
        # `request`를 검사/수정하고 `handler(request)`로 위임
        return handler(request)

    def wrap_tool_call(self, request, handler):
        return handler(request)
```

비동기 변형은 `a` 접두사 규약을 따릅니다: `abefore_agent`, `aafter_agent`, `awrap_model_call`, `awrap_tool_call` 등. 래핑 후크는 가볍고 부작용이 적게 유지하세요 — **모든** 모델/도구 호출에서 실행되며, 이 코드베이스에서는 첫 번째로 등록된 미들웨어가 최외곽 래핑 계층이 됩니다.

---

## 부록

### 파일 레이아웃

```
agent/middlewares/
├── __init__.py                  # 공개 익스포트
├── context_engine/              # ContextEngineHook + nudge 서브에이전트
│   ├── __init__.py              # ContextEngineHook만 익스포트
│   ├── core.py                  # ContextEngineHook
│   └── nudge.py                 # nudge 프롬프트 + 서브에이전트 빌더
├── heartbeat_staleness.py       # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig (자체 README 보유)
│   ├── __init__.py              # HumanInTheLoop, HITLConfig 익스포트
│   ├── types.py                 # 열거형 + 설정 데이터클래스 (_STATE_PREFIX = "hitl")
│   ├── detection.py             # 하드라인 / 위험 명령 패턴
│   ├── approval.py              # ApprovalPipeline
│   ├── gates.py                 # WriteApprovalGate, InterruptManager, MCPElicitationConsent,
│   │                            # KanbanTriage, PairingStore, SlashConfirm
│   └── core.py                  # HumanInTheLoop
├── iteration_budget.py          # IterationBudget
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard (아래에서 재익스포트되지 않음)
├── summarization.py             # Summarization
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # 이 문서 (+ .zh / .ja / .ko 버전)

agent/stream_repetition_guard_wrapper.py  # RepetitionGuardWrapper (이 패키지 바깥에 존재)
```

### 익스포트 (`__init__.py`)

```python
from agent.middlewares import (
    Summarization,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# OutputRepetitionGuard는 여기서 재익스포트되지 않습니다 —
# agent.middlewares.output_repetition_guard에서 임포트하세요.
```
