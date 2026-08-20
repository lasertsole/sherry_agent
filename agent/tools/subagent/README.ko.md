[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

# Subagent 시스템

> 경험 지식 그래프 연동을 갖춘 계층적 작업 분해 및 병렬 실행 하위 시스템.

## 개요

**Subagent 시스템**은 AI Agent가 복잡한 작업을 분해하고, 백그라운드에서 하위 작업을 병렬 실행하며, 메시지 버스를 통해 결과를 비동기적으로 반환할 수 있게 해줍니다. **경험 지식 그래프 폐쇄 루프**를 갖추고 있습니다: 초안 → 증류 → 저장 → 회수 → 조립.

핵심 계층:

- **`SubagentManager`** — 백그라운드 하위 에이전트 작업의 수명 주기를 관리하는 싱글톤 오케스트레이터.
- **`Commander`** — 작업 단위로 생성되는 LangGraph 에이전트로, 작업을 계획·분해·Worker에게 배포합니다.
- **Distiller** — 작업 후 증류 엔진으로, 재사용 가능한 경험을 추출해 지식 그래프에 기록합니다.
- **Draft 도구** — 작업 실행 중 핵심 발견 사항을 기록하기 위한 Agent 호출 가능 도구.

## 아키텍처

```
사용자 / 메인 Agent
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│                     SubagentManager                          │
│  (싱글톤, 수명 주기 오케스트레이터)                          │
│                                                              │
│  _run_subagent() 흐름:                                      │
│    1. 지식 그래프 회수 → Commander에 AIMessage 주입       │
│    2. Commander가 작업 실행 (도구: todo_writer, worker,      │
│       draft)                                                 │
│    3. 결과를 버스에 게시 (계획 C)                           │
│    4. 경험을 지식 그래프로 증류                              │
│    5. 런타임 레지스터 정리                                   │
└──────────────────────────────────────────────────────────────┘
       │ 생성
       ▼
┌──────────────────────────────────────────────────────────────┐
│                      Commander 에이전트                      │
│  (LangGraph, 작업별 인스턴스)                               │
│                                                              │
│  도구:                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
│  │TodoWriter│  │  Worker  │  │  Draft   │                  │
│  │(todo.md  │  │(병렬     │  │(발견 사항 │                  │
│  │ 작성)     │  │ 배포)    │  │ 기록)    │                  │
│  └──────────┘  └────┬─────┘  └──────────┘                 │
│                      │                                       │
│  미들웨어:           │                                       │
│  ┌───────────────┐   │                                       │
│  │Summarization  │   │                                       │
│  ├───────────────┤   │                                       │
│  │TODOManager    │   │                                       │
│  │(주입+정리)    │   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolCallNorm   │   │                                       │
│  ├───────────────┤   │                                       │
│  │IterationBudget│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolGuardrails │   │                                       │
│  └───────────────┘   │                                       │
└──────────────────────┼──────────────────────────────────────┘
                        │ 배포
                        ▼
                ┌────────────────┐
                │ Worker 에이전트 │
                │ (codeact_agent)│
                │ Worker 에이전트 │
                │ ... (병렬)      │
                └────────────────┘
                        │
                        ▼ 작업 후
┌──────────────────────────────────────────────────────────────┐
│              경험 지식 그래프                                 │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Draft 도구  │→ │  Distiller  │→ │  지식       │         │
│  │(작업 중     │  │(보조 LLM    │  │  그래프     │         │
│  │ 메모 기록)  │  │ 추출)       │  │(노드/엣지   │         │
│  └─────────────┘  └─────────────┘  └──────┬──────┘        │
│                                              │ 회수          │
│  ┌───────────────────────────────────────────┘               │
│  │  다음 작업: 회수 → 조립 → AIMessage로 주입               │
│  └───────────────────────────────────────────────────────────│
│                                                              │
│  DB 역할:                                                    │
│    default → 경험 그래프 (전략 레벨)                         │
│    worker  → 경험 그래프 (운영 레벨)                         │
└──────────────────────────────────────────────────────────────┘
```

## 모듈 구조

```
subagent/
├── __init__.py              # 내보내기: build_subagent_tool
├── base.py                  # SubagentManager — 싱글톤 오케스트레이터 + 증류
├── core.py                  # @tool subagent_tool — 비동기 생성 인터페이스
├── type.py                  # SubAgentOutput — pydantic 데이터 모델
├── draft.py                 # Draft @tool — 핵심 발견 사항 기록 + 헬퍼 함수
├── distiller.py             # Distiller — 작업 후 경험 증류
├── commander/
│   ├── __init__.py          # 내보내기: build_commander
│   ├── core.py              # build_commander() — LangGraph 에이전트 생성
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — todo.md 파일 작성
│   │   └── worker/
│   │       ├── core.py      # Worker — 병렬 하위 작업 배포
│   │       └── middlewares/
│   │           └── WorkerSummarization.py
│   └── middlewares/
│       └── core.py          # TODOManager — todo 컨텍스트 주입 + 보관
├── templates/
│   └── subagent_announce.md # 결과 알림용 Jinja2 템플릿
├── README.md
└── README.zh.md
```

## 경험 지식 그래프 폐쇄 루프

### 흐름

```
1. 작업 실행:  Commander/Worker가 draft_tool 호출 → state_register_db
2. 작업 완료:  bus.publish → distill_and_ingest → Register.clear_all
3. 증류:       auxiliary_llm이 초안+결과에서 노드/엣지 추출
4. 저장:       전략 → 지식 그래프("default"), 운영 → 지식 그래프("worker")
5. 다음 작업:  recall(task) → assemble_context → AIMessage 주입
```

### Draft 도구

`draft`는 Commander, Worker, 메인 Agent 모두가 호출할 수 있는 `@tool` 함수입니다:

```python
@tool
def draft(
    key_points: str,
    category: Literal["strategy", "obstacle", "tool_pattern", "insight"],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

헬퍼 함수 (distiller에서 사용):
- `get_drafts(session_id)` — 모든 초안 항목 읽기
- `append_drafts(session_id, drafts)` — Worker의 초안을 Commander 세션으로 병합
- `clear_drafts(session_id)` — 증류 후 초안 항목 정리

### Distiller

`distill_and_ingest()`는 각 subagent 작업 후 실행됩니다 (계획 C 순서):

1. **전략 증류** → `get_instance("default").ingest_experiences()` (Commander 레벨 패턴)
2. **운영 증류** → `get_instance("worker").ingest_experiences()` (Worker 레벨 기법)

Worker 초안은 증류 전에 Commander 세션으로 병합됩니다.

### 지식 그래프 주입

`agent.ainvoke()` 이전에, 회수된 경험이 `AIMessage`로 주입됩니다:

```python
messages = [HumanMessage(content=task)]
# 지식 그래프에서 회수
if recall_result["nodes"]:
    assembled = assemble_context(db, nodes, edges)
    messages.append(AIMessage(content=f"徊\n{system_prompt}\n\n{xml}\n徊"))
```

- **Commander**: 경험 그래프에서 회수 (전략 레벨)
- **Worker**: 경험 그래프에서 회수 (운영 레벨)

## 데이터 모델

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]          # 작업 성공/실패
    finish_reason: str                       # 완료 사유 (실패 시 오류 세부 내용 포함)
    result: str                              # 결과 또는 결과 저장 경로
```

## SubagentManager 수명 주기

### 계획 C: 게시 → 증류 → 정리

Commander 실행이 완료된 후 (성공, 타임아웃 또는 오류):

```
1. 결과를 버스에 게시 (사용자가 즉시 알림 수신)
2. distill_and_ingest() (초안은 여전히 state_register_db에 있음)
3. Register.clear_all_register_sessions() (정리, 초안도 함께 제거)
```

사용자가 결과를 신속하게 받는 동시에, 초안은 증류가 끝날 때까지 보존됨을 보장합니다.

### 생성 → 실행 → 알림

```
spawn(task, session_id)
  │
  ├─ task_id 생성 (타임스탬프 기반)
  ├─ asyncio 태스크 생성 (_run_subagent)
  ├─ _running_tasks 및 _session_tasks에 추적
  ├─ _cleanup 콜백 등록
  └─ "시작됨" 메시지 반환

_run_subagent(session_id, task_id, task, label)
  │
  ├─ Commander 지식 그래프 회수 → AIMessage 주입으로 messages 구성
  ├─ Commander 에이전트 구성
  ├─ agent.ainvoke({messages: [HumanMessage(task), AIMessage(knowledge)]})
  ├─ SubAgentOutput으로 알림 템플릿 렌더링
  ├─ 버스에 InboundMessage 게시
  ├─ distill_and_ingest() → 경험을 지식 그래프로 추출
  └─ Register.clear_all_register_sessions()
```

### 서비스 모드

`start_service()`는 `_consume_loop()`를 실행하며, 이는:
1. 버스에서 `InboundMessage`를 대기합니다.
2. 캐릭터 페르소나를 통해 결과를 다시 개인화합니다.
3. 등록된 `_consumer` 콜백으로 전달합니다.

## Commander 에이전트

### 구성

`build_commander()`는 LangGraph 에이전트를 구성합니다:

| 구성 요소 | 세부 사항 |
|-----------|---------|
| **시스템 프롬프트** | 작업 분해, 병렬화, 동적 계획 조정, 초안 기록 |
| **모델** | `main_llm` (프로젝트 공유 모델) |
| **체크포인터** | `InMemorySaver` |
| **도구** | `todo_writer` + `worker` + `draft` |
| **미들웨어** | `SummarizationMiddleware` (15개 트리거, 8개 유지) + `TODOManager` + `ToolCallNormalize` + `IterationBudget` + `ToolGuardrails` |
| **응답 형식** | `SubAgentOutput` 구조화 출력 |

## Commander 미들웨어

### TODOManager (TodoInjector + TodoCleaner 대체)

- **`abefore_model`**: `todo/{task_id}.md`를 읽어 `[SYSTEM CONTEXT - TODO LIST UPDATE]`로 주입합니다.
- **`aafter_agent`**: todo 파일을 `todo_archive/`에 보관하거나 삭제합니다.

### ToolCallNormalize

요약이 메시지를 정리한 후 발생하는 고아 tool_call을 수정합니다.

### IterationBudget

작업당 에이전트 반복 횟수를 제한합니다.

### ToolGuardrails

도구 호출이 안전 규칙을 준수하는지 검증합니다.

## Worker 에이전트

Worker는 `codeact_agent` 인스턴스 (LangGraph 에이전트 아님)이며:

- **도구**: `build_worker_tools()` (subagent 전용을 제외한 모든 도구, `draft` 포함)
- **미들웨어**: `WorkerSummarization` + `HeartbeatStaleness` + `IterationBudget`
- **응답 형식**: `SubAgentOutput`
- **지식 그래프 주입**: 실행 전 경험 그래프에서 회수
- **초안 병합**: Worker 초안은 `finally` 블록에서 Commander 세션으로 병합

## FAQ

### 왜 distiller가 지식 그래프 모듈에서 분리되었나요?

`distiller.py`는 원래 지식 그래프 추출기 안에 있었지만, `draft.py` (subagent 계층)를 import 하면서 역방향 의존성이 발생했습니다: 지식 그래프 인프라 → subagent 비즈니스 로직. distiller를 `subagent/`로 이동하면 의존 방향이 단방향이 됩니다: `subagent/distiller` → 지식 그래프 ✓

### 왜 계획 C (게시 → 증류 → 정리)인가요?

사용자는 즉시 결과를 받아야 합니다. 증류는 `state_register_db`의 초안 데이터를 필요로 하는데, `Register.clear_all`이 먼저 실행되면 초안이 유실됩니다. 계획 C는 둘 다 보장합니다: 신속한 전달 + 완전한 증류.

### 증류가 실패하면 어떻게 하나요?

증류는 `try/except`로 감싸져 있으며, 실패 시 경고 로그만 기록되고 이미 사용자에게 게시된 결과에는 영향을 주지 않습니다.

### Worker의 초안은 어떻게 수집되나요?

`_arun_task`의 `finally` 블록에서 `get_drafts(worker_session_id)`로 Worker 초안을 읽고, `append_drafts(commander_session_id, ...)`로 Commander 세션에 병합합니다. distiller는 Commander 세션에서 일괄적으로 읽습니다.

## 기술 스택

| 계층 | 기술 |
|-------|-----------|
| 에이전트 프레임워크 | LangGraph (`CompiledStateGraph`) + codeact_agent |
| LLM | `main_llm` (공유), `auxiliary_llm` (증류) |
| 체크포인팅 | `InMemorySaver` |
| 미들웨어 | `@before_model` / `@after_agent` 데코레이터 |
| 지식 그래프 | 경험 그래프 (SQLite + FTS5 + 벡터 검색 + PageRank) |
| 비동기 | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| 데이터 검증 | Pydantic v2 |
| 템플릿 | 커스텀 `render_template_file()` (Jinja2 스타일) |
| 메시지 버스 | 프로젝트 내부 `MessageBus` / `InboundMessage` |
| 상태 관리 | `state_register_db` (SQLite), `state_register_mem` (메모리) |
