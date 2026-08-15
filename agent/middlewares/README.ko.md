# EMA Agent Middleware 시스템

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangGraph 1.2+](https://img.shields.io/badge/LangGraph-1.2%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

LLM 에이전트 실행을 위한 조합 가능한 미들웨어 파이프라인 — 메시지 관리, 도구 호출 검증, 가드레일, 예산 제어, 멀티모달 처리 및 하트비트 모니터링. 모든 미들웨어는 공유되고 영속적인 상태 시스템을 통해 **LangGraph 에이전트 수명주기**에 연결됩니다.

---

## 목차

- [아키텍처 개요](#아키텍처-개요)
- [미들웨어 체인](#미들웨어-체인)
- [미들웨어 참조](#미들웨어-참조)
  - [Summarization](#summarization)
  - [ToolCallNormalize](#toolcallnormalize)
  - [ToolGuardrails](#toolguardrails)
  - [IterationBudget](#iterationbudget)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [MultimodalProcessor](#multimodalprocessor)
  - [ContextEngineHook](#contextenginehook)
- [공유 상태 시스템](#공유-상태-시스템)
- [구성](#구성)
- [수명주기 및 데이터 흐름](#수명주기-및-데이터-흐름)
- [사용자 정의 미들웨어 작성](#사용자-정의-미들웨어-작성)

---

## 아키텍처 개요

모든 미들웨어는 전용 **기본 클래스**(예: `SummarizationMiddleware`, `AgentMiddleware`, 또는 `ContextEngineHook`)를 상속하며, 각각은 다음 **수명주기 훅** 중 하나 이상을 구현합니다:

| 훅 | 호출 시점 | 목적 |
|---|---|---|
| `awrap_before_agent(state)` | 모든 LLM 호출 전 | 상태 준비, 시스템 프롬프트 주입, 기록 정리 |
| `awrap_after_agent(state)` | 모든 LLM 호출 후 | 어시스턴트 응답 후처리, 부수 효과 실행 |
| `awrap_tool_call(state, tool_call)` | 각 개별 도구 실행 전 | 도구 호출 검증, 보호 또는 강화 |
| `awrap_after_tool(state)` | 도구가 반환된 후 | 도구 결과 처리, 예산 확인, 계산된 필드 추가 |

미들웨어 인스턴스는 **주 에이전트 빌더**에 등록되며, 에이전트 노드를 감싸는 체인으로서 **선언 순서대로 실행**됩니다.

### 상태 영속성

미들웨어는 `runtime`에서 제공되는 **싱글턴 인스턴스**인 두 개의 교차-훅 상태 사전을 통해 통신합니다:

- **`state_register_mem`** (`StateRegisterMeM`) — 메모리 내, 세션별 상태 저장소. 카운터, 예산, 가드레일 추적, 하트비트 진행에 사용됩니다.
- **`state_register_db`** (`StateRegisterDB`) — SQLite 지원, 세션별 상태 저장소. 프로세스 재시작 후에도 유지되어야 하는 구조화된 레코드에 사용됩니다.

둘 다 `runtime.state_register`에서 가져온 싱글턴입니다. 동일한 `Register` 인터페이스(`set_state`, `get_state`, `delete_state`, `clear_session` 등)를 공유합니다.

---

## 미들웨어 체인

**주 에이전트**에 대한 전체 파이프라인은 다음 순서로 실행됩니다 (각각은 내부 레이어를 감쌉니다):

```
┌─────────────────────────────────────────────────────────┐
│  Summarization                (가장 바깥쪽 — 먼저 정리)  │
│  ToolCallNormalize            (손상된 도구 호출 복구)   │
│  ToolGuardrails               (반복 감지, 중지)         │
│  IterationBudget              (하드 반복 한도)          │
│  HeartbeatStaleness           (하트비트 시간 초과)      │
│  MultimodalProcessor          (미디어 처리)             │
│  ContextEngineHook            (메모리 및 넛지, 가장 안쪽)│
│    ┌─────────────────────────────────────┐              │
│    │         LLM (Agent Node)            │              │
│    └─────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
```

> **참고:** `HeartbeatStaleness`는 **작업자 에이전트**에서만 사용됩니다 (주 에이전트 아님). 하위 에이전트 진행 상황을 모니터링하고 유휴/무응답 상태가 된 에이전트를 종료합니다.

**데이터 흐름 (단일 턴):**

1. `awrap_before_agent` — 바깥쪽에서 안쪽으로 실행 (summarization 먼저, context engine 마지막)
2. LLM이 응답 생성 (도구 호출 포함 가능)
3. `awrap_after_agent` — 안쪽에서 바깥쪽으로 실행 (context engine 먼저, summarization 마지막)
4. 각 도구 호출에 대해: 각 미들웨어의 `awrap_tool_call`이 순서대로 실행
5. 각 도구 반환 후: 각 미들웨어의 `awwrap_after_tool`이 순서대로 실행
6. LLM이 최종 답변을 생성하거나 예산이 소진될 때까지 1단계부터 반복

---

## 미들웨어 참조

### Summarization

**파일:** `summarization.py`
**클래스:** `Summarization` (`SummarizationMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_agent`

토큰 한도를 초과할 때 대화 기록을 정리하여 최근 턴을 보존하고 이전 메시지의 압축 요약을 생성합니다.

**동작:**

- `awrap_before_agent`에서: 메시지 기록의 총 토큰을 계산합니다. 구성된 `max_tokens`를 초과하면:
  1. 최근 대화의 마지막 N턴을 그대로 유지
  2. 그 이전의 모든 것을 요약 프롬프트로 압축
  3. 메시지 목록 시작 부분에 `SystemMessage`로 요약 주입
- `awrap_after_agent`에서: 요약 결과를 `state_register_mem`에 저장

**구성:**

```json
{
  "summarization": {
    "max_tokens": 64000,
    "recent_turns": 10
  }
}
```

---

### ToolCallNormalize

**파일:** `tool_call_normalize.py`
**클래스:** `ToolCallNormalize` (`AgentMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

손상된 도구 호출을 복구합니다 — 주로 LLM이 잘못되거나 불일치한 `id`/`name` 필드를 가진 도구 호출을 생성하는 **짝이 맞지 않는 ID/이름 패턴**을 수정합니다.

**동작:**

- **쌍 복구:** `tool_call`의 여러 항목에서 `id` 값이 예상 패턴과 일치하지 않으면, 미들웨어는 이름 → 예상 id 매핑을 구성하고 재할당합니다.
- **중복 제거:** 이미 처리된 도구 호출은 건너뜁니다 (상태를 통해 추적).
- **노이즈 감소:** 검증에 실패한 도구 호출 항목을 제거합니다.

**필요한 이유:** LLM (특히 더 작거나 양자화된 모델)은 매달려 있거나, 바뀌거나, 중복된 `id` 필드를 가진 도구 호출을 자주 생성합니다. 이 미들웨어는 런타임에 도달하기 전에 이러한 문제를 조용히 해결합니다.

---

### ToolGuardrails

**파일:** `tool_guardrails.py`
**클래스:** `ToolGuardrails` (`AgentMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

**무한 도구 호출 루프**, **반복된 실패 패턴**, **동일한 재시도**를 감지하고 방지합니다. 3단계 승격 시스템을 사용합니다.

**단계:**

| 단계 | 조건 | 동작 |
|---|---|---|
| `warn` | 최근 호출에서 도구 이름이 N+회 반복됨 (같은 도구, 같은 이름, 모든 인자) | 다음 LLM 호출 전에 대화에 경고 `SystemMessage` 주입 |
| `block` | 같은 도구 + 같은 인자가 N+회 반복됨 | 도구 호출 실행을 방지 — 대신 오류 `ToolMessage` 반환 |
| `halt` | 차단된 호출이 연속으로 N+회 재생성됨 | **하드 중지** 강제: 에이전트 실행을 종료하는 `AgentHalt` 발생 |

**감지 데이터:**

- `state_register_mem`에서 도구 호출 이름과 직렬화된 인자 추적
- 슬라이딩 윈도우 방식 사용 — 가장 최근 호출만 고려 (윈도우 크기 구성 가능)

**구성:**

```json
{
  "tool_guardrails": {
    "call_window": 15,
    "warn_threshold": 4,
    "block_threshold": 3,
    "halt_threshold": 3
  }
}
```

---

### IterationBudget

**파일:** `iteration_budget.py`
**클래스:** `IterationBudget` (`AgentMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_tool`

대화 턴당 **LLM-도구 반복 횟수의 하드 한도**를 적용합니다. 한도에 도달하면 에이전트는 추가 도구 호출 없이 최종 답변을 생성하도록 강제됩니다.

**동작:**

- `awrap_before_agent`에서: 현재 반복 횟수를 `max_iterations` 한도와 비교합니다. 초과하면 사용 가능한 정보로 즉시 답변하도록 LLM에 지시하는 `SystemMessage`를 추가합니다.
- `awrap_after_tool`에서: `state_register_mem`의 반복 카운터를 증가시킵니다.
- **반복 카운터 리셋**은 한도가 초과된 후 다음 `awrap_before_agent` 호출에서 발생합니다 ("resetting" 플래그로 감지).

**구성:**

```json
{
  "iteration_budget": {
    "max_iterations": 10
  }
}
```

---

### HeartbeatStaleness

**파일:** `heartbeat_staleness.py`
**클래스:** `HeartbeatStaleness` (`AgentMiddleware` 상속)
**내보내기 이름:** `HeartbeatStaleness`
**사용처:** 작업자 에이전트 전용 (주 에이전트 아님)
**훅:** `awrap_before_agent`, `awrap_after_agent`, `awrap_model_call`, `awrap_tool_call`

작업자 에이전트가 너무 오랫동안 **유휴 또는 무응답** 상태인지 감지하고 종료합니다. 에이전트가 진행 상황을 만들었는지 (반복 횟수 증가 또는 현재 도구 변경) 확인하는 주기적 하트비트 타이머를 사용합니다.

**이중 임계값 시스템:**

| 상태 | 임계값 | 근거 |
|---|---|---|
| **유휴** (실행 중인 도구 없음) | `stale_cycles_idle` (기본 7주기 ≈ 7분) | 더 엄격 — 에이전트가 중단된 호출에 걸려 있을 가능성이 큼 |
| **도구 사용 중** (도구 실행 중) | `stale_cycles_in_tool` (기본 20주기 ≈ 20분) | 더 느슨 — 도구가 정당하게 오래 실행 중일 수 있음 |

**진행 감지:**

`heartbeat_interval_minutes`(기본 1분)마다 백그라운드 타이머가 에이전트의 현재 `(iteration_count, current_tool)` 쌍을 이전에 관찰된 값과 비교합니다. **둘 중 하나**가 진행되면 stale 카운터는 0으로 리셋되고, 그렇지 않으면 1씩 증가합니다.

**종료:**

stale 카운터가 구성된 임계값에 도달하면 세션이 `killed`로 표시됩니다. 이후 `awrap_model_call` 또는 `awrap_tool_call` 호출은 **`HeartbeatTimeoutError`**를 발생시켜 에이전트를 정상적으로 종료합니다.

**상태 저장:**

모든 세션별 상태는 `state_register_mem`에 유지되어 같은 턴의 미들웨어 훅 간에 유지됩니다:

| 키 | 목적 |
|---|---|
| `heartbeat_iter` | 현재 반복 횟수 |
| `heartbeat_tool` | 현재 실행 중인 도구 이름 (또는 `None`) |
| `heartbeat_stale` | 연속 stale 주기 카운터 |
| `heartbeat_killed` | 세션이 종료되었는지 여부 |

**구성:**

```json
{
  "heartbeat_staleness": {
    "heartbeat_interval_minutes": 1,
    "stale_cycles_idle": 7,
    "stale_cycles_in_tool": 20
  }
}
```

---

### MultimodalProcessor

**파일:** `multimodal_processor.py`
**클래스:** `MultimodalProcessor` (`AgentMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_agent`, `awrap_after_tool`

대화의 **멀티모달 콘텐츠** (이미지, 파일)를 처리합니다 — 다양한 소스(로컬 파일, S3, HTTP)의 미디어 URI를 LLM이 사용할 수 있는 형식으로 정규화합니다.

**동작:**

- 사용자 메시지의 미디어 참조 감지 (파일 경로, S3 URI, HTTP URL)
- LLM 사용을 위해 미디어를 base64 데이터 URI로 해석하고 인코딩
- 기록을 깨끗하게 유지하기 위해 LLM 호출 후 대화에서 해석된 URI 제거
- 지원: 로컬 파일 시스템, S3 호환 스토리지, HTTP/HTTPS URL

**구성:**

```json
{
  "multimodal_processor": {
    "enabled": true,
    "max_image_size_mb": 20,
    "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif"]
  }
}
```

---

### ContextEngineHook

**파일:** `context_engine/core.py`, `context_engine/nudge.py`
**클래스:** `ContextEngineHook` (`AgentMiddleware` 상속)
**훅:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`

**가장 안쪽 미들웨어** — LLM에 가장 가깝습니다. 시스템 프롬프트, 대화 영속성, 주기적 **넛지** 개입 (메모리 검토, 스킬 검토) 및 **지식 그래프 유지**를 관리합니다.

#### 핵심 동작 (`core.py`)

- **`awrap_before_agent`:**
  1. 캐시(`MemoryCache`)에서 **시스템 프롬프트**를 로드 (기본값 대체)
  2. **스레드 안전 변수 설정** (`conversation_id`, `user_id`) for 다운스트림 훅
  3. `MesMemory`에서 저장된 메시지를 로드하여 대화 상태에 병합
  4. (선택적으로 사용자 정의된) 시스템 프롬프트를 첫 번째 `SystemMessage`로 주입
- **`awrap_after_agent`:`
  1. `add_messages()`를 통해 어시스턴트 응답을 `MesMemory`에 영속화
  2. 포스트-에이전트 부수 효과로 **넛지 시스템** 실행 (아래 참조)
  3. 주기적 작업을 위해 **지식 그래프 유지(`after_turn`)** 호출 (예: 오래된 노드 정리, 엣지 가중치 업데이트). try/except로 감싸져 있으며, 실패는 치명적이지 않고 디버그 수준으로 기록됩니다.
- **`awrap_tool_call` (넛지 부수 효과):** 도구가 호출될 때마다 `MesMemory`의 스킬 검토 카운터 증가

#### 넛지 시스템 (`nudge.py`)

메모리/스킬 시스템에 사용자가 참여하도록 장려하는 개입 메시지를 주기적으로 주입합니다.

| 넛지 유형 | 트리거 | 내용 |
|---|---|---|
| **메모리 넛지** | 마지막 메모리 작업 이후 10턴마다 | 더 나은 결과를 위해 사용자에게 메모리/시스템 프롬프트 업데이트 요청 |
| **스킬 검토 넛지** | 마지막 검토 이후 10번의 도구 호출마다 | 사용자에게 마지막 도구 실행 결과 평가 요청 |
| **결합 넛지** | 두 조건이 동시에 충족됨 | 메모리와 스킬 검토를 모두 다루는 병합된 메시지 |

**잠금 메커니즘:** 각 넛지 유형은 `MesMemory`에서 **쿨다운 잠금**(`nudge_lock_memory`, `nudge_lock_skill`)을 가져 10턴 창 내에서 반복된 넛지를 방지합니다. 사용자가 실제로 작업을 수행하면 (예: 메모리 업데이트 또는 스킬 평가) 잠금이 리셋됩니다.

넛지 메시지는 일반 어시스턴트 응답 뒤에 추가된 **별도의 `AIMessage` 청크**로 전송되어 UI에서 자연스러운 후속 제안으로 나타납니다.

#### 지식 그래프 통합

넛지 로직 후에 `aafter_agent`는 주기적 유지를 위해 지식 그래프의 `after_turn(session_id)`을 호출합니다. 여기에는 오래된 노드 정리 및 엣지 가중치 업데이트와 같은 작업이 포함됩니다. 호출은 try/except 블록으로 감싸져 있어 실패 시 오류는 디버그 수준으로 기록되고 에이전트 흐름을 중단하지 않습니다.

**구성 (`system/config.tool.md`에 있음):**

```json
{
  "context_engine": {
    "enabled": true,
    "nudge_memory_interval": 10,
    "nudge_skill_interval": 10
  }
}
```

#### 종속성

- **`MemoryCache`** — 시스템 프롬프트 및 메타데이터용 스레드 안전 메모리 내 캐시
- **`MesMemory`** — 영속적인 대화 메모리 백엔드 (메시지, 넛지 잠금, 스킬 검토 카운터 저장)
- **경험 그래프** — 각 턴 후 주기적 유지를 위한 지식 그래프 모듈
- **`system/config.tool.md`** — 시스템 프롬프트 및 넛지 간격 구성 파일

---

## 공유 상태 시스템

모든 미들웨어는 `runtime.state_register`에서 두 개의 공유 싱글턴 인스턴스에 접근합니다:

| 인스턴스 | 클래스 | 영속성 | 목적 |
|---|---|---|---|
| `state_register_mem` | `StateRegisterMeM` | 메모리 내, 세션별 | 카운터, 플래그, 현재 요약, 윈도우 버퍼, 하트비트 상태 |
| `state_register_db` | `StateRegisterDB` | SQLite 지원, 세션별 | 프로세스 재시작 후에도 유지되는 구조화된 레코드 |

두 클래스 모두 `Register`를 상속하며 동일한 인터페이스를 노출합니다:

| 메서드 | 설명 |
|---|---|
| `set_state(session_id, key, value)` | 세션에 키-값 쌍 설정 |
| `get_state(session_id, key, default)` | 기본 대체가 있는 키에 대한 값 가져오기 |
| `get_all_states(session_id)` | 세션의 모든 키-값 쌍 가져오기 |
| `delete_state(session_id, key)` | 특정 키 삭제 |
| `clear_session(session_id)` | 세션의 모든 상태 제거 |
| `has_session(session_id)` | 세션이 존재하는지 확인 |
| `has_key(session_id, key)` | 키가 세션에 존재하는지 확인 |
| `update_states(session_id, states)` | 여러 키 일괄 업데이트 |

### 초기화 가드

`StateRegisterMeM`과 `StateRegisterDB`는 모두 `__init__`에서 `_initialized` 가드를 사용하여 재초기화를 방지합니다:

```python
class StateRegisterMeM(Register):
    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._states = {}
        self._initialized = True
```

이것은 `Register.clear_all_register_sessions`가 `__init__`을 트리거하고 `_states`를 리셋하여 모든 메모리 내 상태를 지울 수 있는 버그를 수정합니다.

### 네임스페이스 규칙

각 미들웨어는 자체 최상위 키 네임스페이스를 사용합니다:

```
state_register_mem (session "abc123") = {
    "summarization": { "current_summary": "..." },
    "tool_call_normalize": { "last_names": [...] },
    "tool_guardrails": { "tool_calls": [...], "block_count": 3 },
    "iteration_budget": { "count": 5 },
    "heartbeat_iter": 3,
    "heartbeat_tool": "web_search",
    "heartbeat_stale": 0,
    "heartbeat_killed": False,
    "multimodal_processor": { "resolved_uris": [...] },
}
```

---

## 구성

미들웨어는 주 에이전트 빌더에서 구성됩니다. 체인 순서와 매개변수는 별도의 구성 파일이 아닌 에이전트 생성 중에 설정됩니다.

### 빌더 구성 예시

```python
from agent.middlewares import (
    Summarization,
    ToolCallNormalize,
    ToolGuardrails,
    IterationBudget,
    HeartbeatStaleness,
    MultimodalProcessor,
    ContextEngineHook,
)

middlewares = [
    Summarization(session_id="session_001"),
    ToolCallNormalize(session_id="session_001"),
    ToolGuardrails(config=ToolCallGuardrailConfig(warn_threshold=4, block_threshold=3, halt_threshold=3)),
    IterationBudget(session_id="session_001", max_iterations=10),
    # HeartbeatStaleness — worker agents only
    MultimodalProcessor(session_id="session_001"),
    ContextEngineHook(session_id="session_001"),
]
```

### 미들웨어별 매개변수

```yaml
middleware:
  summarization:
    max_tokens: 64000
    recent_turns: 10
  tool_guardrails:
    call_window: 15
    warn_threshold: 4
    block_threshold: 3
    halt_threshold: 3
  iteration_budget:
    max_iterations: 10
  heartbeat_staleness:
    heartbeat_interval_minutes: 1
    stale_cycles_idle: 7
    stale_cycles_in_tool: 20
  multimodal_processor:
    enabled: true
    max_image_size_mb: 20
  context_engine:
    enabled: true
    nudge_memory_interval: 10
    nudge_skill_interval: 10
```

---

## 수명주기 및 데이터 흐름

### 단일 턴 (상세)

```
[사용자가 메시지 전송]
    │
    ▼
Summarization.awrap_before_agent(state)
    │  토큰 예산을 초과하면 기록 정리
    ▼
ToolCallNormalize.awrap_before_agent(state)
    │  (일반적으로 no-op)
    ▼
ToolGuardrails.awrap_before_agent(state)
    │  루프가 감지되면 경고 주입
    ▼
IterationBudget.awrap_before_agent(state)
    │  예산 초과 시 "즉시 답변" 주입
    ▼
HeartbeatStaleness.awrap_before_agent(state)  [작업자 에이전트 전용]
    │  카운터 리셋, 하트비트 타이머 시작
    ▼
MultimodalProcessor.awrap_before_agent(state)
    │  미디어 URI 해석 → base64
    ▼
ContextEngineHook.awrap_before_agent(state)
    │  시스템 프롬프트 로드, 대화 복원, 스레드 변수 설정
    ▼
┌──────────────────────────────────────────────┐
│              LLM CALL (Agent Node)            │
│  반환: 어시스턴트 메시지 (text + tool_calls)    │
└──────────────────────────────────────────────┘
    │
    ▼
ContextEngineHook.awrap_after_agent(state)
    │  MesMemory에 영속화, 넛지 실행, 지식 그래프 유지
    ▼
MultimodalProcessor.awrap_after_agent(state)
    │  기록에서 해석된 URI 정리
    ▼
HeartbeatStaleness.awrap_after_agent(state)  [작업자 에이전트 전용]
    │  하트비트 타이머 중지
    ▼
IterationBudget.awrap_after_agent(state)
    │  (일반적으로 no-op)
    ▼
ToolGuardrails.awrap_after_agent(state)
    │  도구 호출 기록 창 업데이트
    ▼
ToolCallNormalize.awrap_after_agent(state)
    │  last_names 추적 업데이트
    ▼
Summarization.awrap_after_agent(state)
    │  요약 결과 저장
    │
    ▼
[어시스턴트 메시지의 각 tool_call에 대해:]
    │
    ├─ ToolCallNormalize.awrap_tool_call(state, tc)
    │     잘못된 id/name 쌍 복구
    ├─ ToolGuardrails.awrap_tool_call(state, tc)
    │     임계값 초과 시 차단 또는 중지
    ├─ IterationBudget.awrap_tool_call(state, tc)
    │     (일반적으로 no-op)
    ├─ HeartbeatStaleness.awrap_tool_call(state, tc)  [작업자 에이전트 전용]
    │     현재 도구 추적, killed면 HeartbeatTimeoutError 발생
    ├─ MultimodalProcessor.awrap_tool_call(state, tc)
    │     (일반적으로 no-op)
    └─ ContextEngineHook.awrap_tool_call(state, tc)
           스킬 검토 카운터 증가 (넛지 부수 효과)
    │
    ▼
    [도구 실행]
    │
    ▼
    [각 도구 결과에 대해:]
    │
    ├─ IterationBudget.awrap_after_tool(state)
    │     반복 카운터 증가
    ├─ ToolGuardrails.awrap_after_tool(state)
    │     향후 감지를 위해 결과 등록
    ├─ ToolCallNormalize.awrap_after_tool(state)
    │     (일반적으로 no-op)
    ├─ Summarization.awrap_after_tool(state)
    │     (일반적으로 no-op)
    ├─ MultimodalProcessor.awrap_after_tool(state)
    │     (일반적으로 no-op)
    └─ ContextEngineHook.awrap_after_tool(state)
           (per-tool이 아닌 awrap_after_agent를 통해 실행)
    │
    ▼
[최종 답변까지 다음 반복을 위해 before_agent로 루프백]
```

---

## 사용자 정의 미들웨어 작성

```python
from agent.middlewares.base import AgentMiddleware

class MyCustomMiddleware(AgentMiddleware):
    """Custom middleware example."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.my_param = config.get("my_param", "default")

    async def awrap_before_agent(self, state: AgentState) -> AgentState:
        # Runs before each LLM call
        state.state_register_mem["my_middleware"] = {"started": True}
        return state

    async def awrap_after_agent(self, state: AgentState) -> AgentState:
        # Runs after each LLM call
        return state

    async def awrap_tool_call(
        self, state: AgentState, tool_call: ToolCall
    ) -> AgentState:
        # Runs before each individual tool execution
        if tool_call["name"] == "sensitive_tool":
            # Add guard logic here
            pass
        return state

    async def awrap_after_tool(
        self, state: AgentState
    ) -> AgentState:
        # Runs after each tool returns
        return state
```

에이전트 빌더에 등록합니다:

```python
middlewares = [
    # ...existing middlewares...
    MyCustomMiddleware(config={"my_param": "value"}),
]
```

---

## 부록

### 파일 구조

```
agent/middlewares/
├── __init__.py                   # Public exports
├── summarization.py              # Summarization
├── tool_call_normalize.py        # ToolCallNormalize
├── tool_guardrails.py            # ToolGuardrails
├── iteration_budget.py           # IterationBudget
├── heartbeat_staleness.py        # HeartbeatStaleness
├── multimodal_processor.py       # MultimodalProcessor
├── context_engine/
│   ├── __init__.py
│   ├── core.py                   # ContextEngineHook (main)
│   └── nudge.py                  # Nudge logic (memory/skill review)
├── README.md                     # This file
├── README.zh.md                  # Chinese version
├── README.ko.md                  # Korean version
└── README.ja.md                  # Japanese version
```

### 내보내기 (`__init__.py`)

```python
from .summarization import Summarization
from .tool_guardrails import ToolGuardrails
from .iteration_budget import IterationBudget
from .context_engine import ContextEngineHook
from .tool_call_normalize import ToolCallNormalize
from .heartbeat_staleness import HeartbeatStaleness
from .multimodal_processor import MultimodalProcessor
```
