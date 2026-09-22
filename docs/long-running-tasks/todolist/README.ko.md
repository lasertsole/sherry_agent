# TaskFlow 기반 계획 및 규율 레이어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 세션 범위의 경량 계획 및 규율 레이어로, 기존 TaskFlow 시스템 위에 구축됩니다. 이것은 **태스크 엔진이 아니며**, **DAG가 아니고**, **스케줄러를 구현하지 않습니다**. 세션 수준의 계획은 `todowrite` / `todoread`로 관리됩니다 (시스템 프롬프트 주입을 통해 압축에 면역). 크로스 세션, 영구적, 서브에이전트를 디스패치하는 실행은 **TaskFlow**가 담당합니다 (`depends_on`, `blocked/ready/dispatched/done`, `taskflow_dispatch`, `taskflow_wait_all`). 두 레이어는 `flow_id` / `step_id`로 연결되며, 단일 상태에는 하나의 권위 소스만 있고 레이어 간 복제가 없습니다.

---

## 목차

- [TaskFlow와의 관계](#taskflow와의-관계)
- [설계 철학 및 핵심 원칙](#설계-철학-및-핵심-원칙)
- [아키텍처 개요](#아키텍처-개요)
- [데이터 레이어](data/README.ko.md)
  - [계획 파일 (workspace/sessions/<session_id>/plans/*.md)](data/README.ko.md#계획-파일-workspacesessionssession_idplansmd)
  - [Boulder 상태 (src/data/boulder.json)](data/README.ko.md#boulder-상태-srcdataboulderjson)
  - [증거 원장 (src/data/evidence-ledger.jsonl)](data/README.ko.md#증거-원장-srcdataevidence-ledgerjsonl)
  - [todos.db — 세션 수준 TODO 저장](data/README.ko.md#todosdb--세션-수준-todo-저장)
- [서비스 레이어](#서비스-레이어)
- [도구 레이어](#도구-레이어)
- [오케스트레이션 실행 레이어](#오케스트레이션-실행-레이어)
- [압축 보호 레이어](#압축-보호-레이어)
- [강제 레이어 E1–E7](enforcement/README.ko.md)
  - [E1: 시스템 프롬프트 강제 — orchestrator doctrine](enforcement/README.ko.md#e1-시스템-프롬프트-강제--orchestrator-doctrine)
  - [E2: 도구 설명 강제 — 형식 + 위임 규칙](enforcement/README.ko.md#e2-도구-설명-강제--형식--위임-규칙)
  - [E3: 계속 강제기 — 사후 클로저 코어 ★★](enforcement/README.ko.md#e3-계속-강제기--사후-클로저-코어-)
  - [E4: 전환 배리어 — 이중 보험](enforcement/README.ko.md#e4-전환-배리어--이중-보험)
  - [E5: Sisyphus 완료 계약 ★★](enforcement/README.ko.md#e5-sisyphus-완료-계약-)
  - [E6: 위임 라우팅 ★](enforcement/README.ko.md#e6-위임-라우팅-)
  - [E7: 의도 인식 및 가이던스 — 사전 트리거 ★★](enforcement/README.ko.md#e7-의도-인식-및-가이던스--사전-트리거-)
- [프론트엔드](#프론트엔드)

---

## TaskFlow와의 관계

### 2층 분업

| 차원      | 세션 계획/체크리스트                                         | 실행 흐름 (DAG)                                                                          |
| --------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| 소유자    | 계획 및 규율 레이어 (본 문서)                                | TaskFlow (기존 시스템)                                                                   |
| 범위      | 단일 세션, 경량                                              | 크로스 세션, 영구적                                                                      |
| 상태      | todo의 content/status/priority/category/delegation           | step의 depends_on/status/child_session_key/results                                       |
| 영속화    | `todos.db` (세션 수준)                                       | `taskflow_registry.db` + flow `state_json`                                               |
| 주입 방식 | 시스템 프롬프트 블록 (압축 후 매번 재구축, 자연적 압축 면역) | 도구 반환 텍스트 (`taskflow_summary` 재읽기)                                             |
| 주요 책임 | 계획, 가시성, 규율 강제 (E1–E7)                              | 의존성 충족, 블록/언록, 병렬 디스패치, 유계 대기, 결과 주입                              |
| 스케줄링  | 아니오                                                       | 예 (`taskflow_run_task` / `taskflow_dispatch` / `taskflow_wait_all` / `taskflow_resume`) |

한 줄 요약: **계획 및 규율 레이어는 "명확히 생각하기, 추적하기, 완료 강제하기"를 담당하고, TaskFlow는 "실제로 실행, 크로스 세션, 의존성 관리"를 담당합니다.**

### 경계: 무엇이 어디에 있는가

| 관심사                            | 권위 소스             | 설명                                                     |
| --------------------------------- | --------------------- | -------------------------------------------------------- |
| todo 완료 여부                    | `todos.db`            | `todowrite`만 todo 상태 변경 가능                        |
| step 완료 여부 / 의존성 충족 여부 | TaskFlow `state_json` | `taskflow_resume`만 step을 `done`으로 표시하고 후속 언록 |
| 자식 세션 실행 중 여부            | subagent registry     | `get_run_by_child_session_key` + `is_live_unended_run`   |
| 계획 파일 진행 (체크박스)         | `workspace/sessions/<session_id>/plans/*.md`     | orchestrator가 `- [ ]` → `- [x]` 편집                    |
| 실행 증거                         | `src/data/evidence-ledger.jsonl`   | `EvidenceLedger` 추가                                    |
| 활성 작업 상태                    | `src/data/boulder.json`   | 계획 활성화/복원                                         |

### 연결 (단일 진실 소스)

todo는 TaskFlow를 가리키는 두 개의 선택적 필드를 가질 수 있습니다:

- `flow_id`: 연결된 TaskFlow flow id.
- `step_id`: 연결된 TaskFlow step id (예: `step-2`), 해당 step의 DAG 상태를 재읽기 위해.

**비미러링 원칙**: todo 레이어는 `depends_on`을 복사하지 않고, 웨이브를 저장하지 않으며, step 상태를 캐시하지 않습니다. DAG 상태가 필요할 때 읽기 전용 `taskflow_summary(flow_id)`를 호출하여 `blocked/ready/dispatched/done`을 UI/프롬프트에 매핑합니다.

---

## 설계 철학 및 핵심 원칙

HTN(계층적 태스크 네트워크) 패러다임 채택: 계획 파일 → 체크박스 → 원자 서브태스크 → subagent worker 위임 → 대항적 검증.

```
Plan (workspace/sessions/<session_id>/plans/*.md)
  └─ Wave 0: [Checkbox A] [Checkbox B]          ← 병렬, 의존성 없음
  └─ Wave 1: [Checkbox C] (depends on A, B)      ← Wave 0 완료 대기
  └─ Wave 2: [Final Verification Wave]            ← 전역 마무리

각 Checkbox → 원자 서브태스크로 분해 → subagent worker에 위임
  └─ Worker가 DoneClaim 반환
       └─ AdversarialVerify (독립 검증)
            └─ FullyDone → 체크박스 완료 표시
```

여기서 "웨이브"는 **계획 파일 내의 서술 단위**이며, todolist의 스케줄링 데이터 구조가 아닙니다. "다음 단계를 언제 디스패치할 수 있는가"는 TaskFlow의 `depends_on` + `blocked/ready` 상태로 결정됩니다.

**핵심 원칙**: YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER. 코드를 작성하지 않고, 프로덕트 파일을 편집하지 않으며, 모든 구현 단위를 서브에이전트에 위임합니다.

모델의 자각에 의존하지 않고, 7층 강제로 폐루프를 형성합니다:

```
Before (message arrives)    During                     After
┌──────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ E7 Intent │  │ E6 Delegation ★    │  │ E3 Continuation ★★  │
│ Recognizer│  │ #a fan-out         │  │ idle + incomplete    │
│ ★★       │  │ #b category route  │  │ todo + backoff +     │
│ before_   │  │ #c delegation cmd  │  │ stagnation + abort   │
│ model     │  │ #d barrier         │  │ + recovery mode      │
│ inject    │  └────────────────────┘  └──────────────────────┘
└──────────┘  ┌────────────────────┐  ┌──────────────────────┐
┌──────────┐  │ E1 System prompt   │  │ E5 Sisyphus verify ★★│
│ E2 Tool  │  │ orchestrator       │  │ DoneClaim →          │
│ desc      │  │ doctrine           │  │ AdversarialVerify →  │
│ MANDATORY │  │ + hook notice      │  │ FullyDone             │
│ format    │  └────────────────────┘  └──────────────────────┘
└──────────┘                            ┌──────────────────────┐
                                          │ E4 Transition barrier│
                                          │ TaskFlow step status+│
                                          │ subagent alive → block│
                                          └──────────────────────┘
```

---

## 아키텍처 개요

```
Layer 9  │ UI 컴포넌트     │ TodoDock.vue + TodoItem.vue (PrimeVue), TaskFlow flow별 그룹화
Layer 8  │ 프론트엔드 상태  │ useTodoList.ts (모듈 레벨 싱글톤) + flow별 그룹화
Layer 7  │ 실시간 통신      │ WS: todo_updated 푸시 + todo_refresh 재연결 시 재전송
Layer 6  │ 압축 보호 ★     │ build_system_prompt가 현재 todos + boulder 상태 주입
Layer 5  │ DAG 실행 ★      │ TaskFlow 제공: depends_on + blocked/ready/dispatched/done (본 레이어 미구현)
Layer 4  │ 오케스트레이션 ★ │ ulw-execute: plan→checkbox→sub-task→worker→verify, TaskFlow 호출
Layer 3  │ 도구            │ todowrite + todoread (DAG는 taskflow_* 도구 패밀리가 실행)
Layer 2  │ 서비스           │ TodoService + EvidenceLedger (DAG 상태 쿼리는 TaskFlow에 위임)
Layer 1  │ 데이터 저장      │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
─────────│────────────────│
E1       │ 시스템 프롬프트  │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ 도구 설명        │ todowrite docstring 형식 규칙
E3       │ 계속 강제 ★★    │ after_agent 미들웨어: idle+미완료→자동 계속 (백오프/정체/abort/복구)
E4       │ 전환 배리어 ★   │ TaskFlow step 미 done / subagent 실행 중 → completed 블록
E5       │ Sisyphus 검증 ★★│ DoneClaim → AdversarialVerify → FullyDone (5 gates)
E6       │ 위임 라우팅 ★    │ #a fan-out + #b category 라우팅 + #c 위임 명령 + #d 배리어
E7       │ 의도 인식 ★★    │ before_model: arming(계획 없음+태스크 의도→주입) + plan-active(계획 있음→reminder)
```

**Layer 5의 DAG 기능은 본 레이어에서 구현되지 않습니다** — TaskFlow(`agent/tools/taskflow/tools/*`)가 제공합니다. 본 레이어는 호출만 하고, 재구축하지 않습니다.

---

## 서비스 레이어

### TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4: 전환 배리어 — TaskFlow step 미 done / subagent 실행 중이면 completed 블록
        for todo in validated:
            if todo["status"] == "completed":
                _assert_transition_allowed(todo)
        await store.replace_all(session_id, validated)
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def get_flow_progress(session_id: str, flow_id: str) -> dict:
        """TaskFlow의 DAG 상태를 todo 뷰에 매핑 (frontier 재계산 없음).
        DAG 스케줄링은 TaskFlow가 소유; 이 메서드는 읽기 전용 taskflow_summary만 호출."""
        from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary
        linked = await store.get_todos_by_flow(session_id, flow_id)
        summary_text = await taskflow_summary.ainvoke({"flow_id": flow_id})
        return {"todos": linked, "taskflow_summary": summary_text}
```

### EvidenceLedger

`agent/tools/todolist/evidence_ledger.py`는 append-only JSONL 원장(`src/data/evidence-ledger.jsonl`, 한 줄에 JSON 객체 하나)입니다. `append` + `read_all`이 저장 계약의 전부이며, 세션 범위 뷰는 같은 공유 파일을 읽습니다:

```python
class EvidenceLedger:
    LEDGER_PATH = "src/data/evidence-ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None: ...          # 한 줄 JSON + UTC 타임스탬프
    @classmethod
    def read_all(cls) -> list[dict]: ...
    @classmethod
    def for_session(cls, session_key: str) -> SessionEvidenceLedger: ...
    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]: ...
    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int: ...
```

staleness는 저장되지 않고 파생됩니다: `mark_stale_for_path`는 `{"event": "stale", "file_path": …}` 행을 덧붙이고, 읽기 측은 이후 stale 이벤트가 해당 evidence 행의 `command`에 포함된 경로를 지명할 때만 그 행을 stale로 봅니다. `agent/tools/todolist/evidence_recorder.py`가 이를 도구에 배선합니다(페일오픈, 예외 삼킴): `terminal` / `python_repl`은 인식된 검증 명령의 행을 항상 덧붙이고(분류표는 `EVIDENCE_LEDGER["verify_commands"]`), `write_file` / `patch_file`은 편집된 경로의 stale 이벤트를 항상 덧붙입니다. `agent/tools/taskflow/evidence_collector.py`는 판정기와 `taskflow_finish` evidence 게이트에 표시되는 요약을 렌더링합니다.

### WS 푸시

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(json.dumps({
            "event": "todo_updated", "session_id": session_id,
            "content": {"todos": todos}
        }))
```

`relation_register`의 직접 전송 패턴을 재사용하여 `todo_updated` 이벤트를 푸시합니다.

---

## 도구 레이어

### todowrite — 전량 교체, 쓰기 즉 읽기

```python
@tool("todowrite")
async def todowrite(todos: list[dict], session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Update the todo list for the current session (full replacement).
    Pass the COMPLETE list every time.
    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...])."""
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER  # E6a: 최초 호출 시 fan-out 리마인더 추가
    return output
```

### todoread — 명시적 읽기

```python
@tool("todoread")
async def todoread(session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### 도구 등록 (E2 주입 지점)

```python
def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES  # E2 형식 규칙
    return list(_TODOLIST_TOOLS)
```

`build_todolist_tools()`에서 todowrite 설명을 오버라이드하고 MANDATORY 형식 규칙을 주입:

- 각 todo 제목은 WHERE/WHY/HOW/RESULT를 인코딩
- 원자 입자도 (1-3 도구 호출로 완료)
- 동시에 하나의 in_progress만
- subagent 반환 전 completed 마크 금지

### SKILL.md

todolist를 언제 사용할지 (3+ 단계 복잡 작업), 사용 가능 도구, 상태/우선순위/위임 필드, DAG 필드 (TaskFlow에 위임), 규칙을 정의합니다.

### knowledge — 계획 아이덴티티 격리

`knowledge` 도구(`agent/tools/todolist/knowledge/`)는 계획 이름이 아니라 **계획 아이덴티티**를 키로 사용합니다. 연결은 세 가지 소스(`ownership.association_plan_refs()`)에서 옵니다: 세션의 `plan_ref` 상태 키, 세션 todo의 `plan_ref`(SQL에서 `session_id`로 필터), `src/data/boulder.json`에서 `plan_name`이 일치하고 `session_ids`에 해당 세션을 포함하는 work. `identity.resolve_plan_identity()`가 그 이름을 정규화된 계획 경로로 해석하고 저장 디렉터리 `workspace/knowledge/plans/<plan_key>/`를 도출합니다(`plan_key = sha1(리포지토리 루트 상대 계획 경로)[:12]`). 각 디렉터리의 `meta.json`에 가독 `plan_name` / `plan_ref`를 기록합니다. 결과:

- 계획 파일이 다른 같은 이름 계획은 **물리적으로 격리**됩니다 — 각자 자신의 key 디렉터리에 기록하며 서로 덮어쓰지 않습니다;
- boulder `session_ids`로 **하나의 계획 파일**을 공유하는 모든 세션은 같은 경로로 해석되므로 **다중 세션 협업이 유지**됩니다(하나의 디렉터리 공유);
- 계획 파일이 해석되지 않는 세션은 폴백 아이덴티티 `session-<sha1(session_id)[:8]>`에 기록합니다 — 전체 id 해시로 앞 8자가 같은 세션 id도 서로 격리됩니다;
- 이름이 여러 경로에 걸리면 세션 자신의 `plan_ref`를 우선하며, 그래도 모호하면(자기 세션의 폴백 이름이 아닌 경우) 진단 가능한 오류로 거부하고 다른 곳에 조용히 기록하지 않습니다.

`list`는 연결된 계획만(가독 이름 + key) 반환하고, `read` / `write`의 다른 세션·모호한 계획 접근은 거부됩니다(조용한 빈 결과가 아님). 레거시 이름 키 디렉터리 `workspace/knowledge/plans/<plan-name>/`는 key 디렉터리가 생기기 전까지 읽기 가능하며, 쓰기는 항상 key 디렉터리에 기록됩니다. `clear_session`은 세션 전용 아이덴티티 디렉터리를 삭제하고 boulder `session_ids`로 다른 세션과 공유된 계획은 유지합니다; 레거시 디렉터리는 절대 삭제하지 않습니다. boulder 파일 누락/손상, 빈 `session_id`, 알 수 없는 계획은 모두 안전하게 거부됩니다 — 예외도, 교차 세션 읽기도 없습니다.

---

## 오케스트레이션 실행 레이어

### 5단계 흐름

```
Phase 1: 계획 선택 → src/data/boulder.json 읽기, workspace/sessions/<session_id>/plans/*.md 리스트, 매치 또는 복원
Phase 2: Boulder 상태 생성/업데이트 → boulder.json 작성, 단계/태스크를 todos로 등록
Phase 3: 다음 체크박스 실행 (스케줄링은 모두 TaskFlow에 위임)
  → 첫 번째 미체크 체크박스 찾기
  → 원자 서브태스크로 분해
  → taskflow_run_task(flow_id, task, depends_on=[...])로 등록
  → blocked는 디스패치 안 함; ready는 taskflow_dispatch로 일괄 디스패치
  → taskflow_wait_all → taskflow_resume (결과 주입, 후속 언록)
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: 검증 및 증거 기록 → 5 gates → src/data/evidence-ledger.jsonl
Phase 5: 진행 마크 → 체크박스 - [ ] → - [x], 계속할지 묻지 않음
```

### 계획 → TaskFlow 매핑

1. `taskflow_create(flow_id, description, initial_state)` — flow 생성.
2. `taskflow_run_task(flow_id, task, depends_on=[...])` — step 등록: 의존성 없음 → `dispatched`; 의존성 미충족 → `blocked` (디스패치 안 함); 알 수 없는 의존성 id → 에러, 상태 변경 없음.
3. `taskflow_resume(flow_id, child_session_key, result)` — 결과 주입: step이 `done`이 되고, `blocked` 의존성이 `ready`로 언록. **resume은 자동 디스패치하지 않음.**
4. `taskflow_dispatch(flow_id, step_ids)` — ready step 일괄 병렬 디스패치.
5. `taskflow_wait_all(flow_id, timeout, poll_interval)` — 디스패치한 자식 세션 완료 대기.
6. `taskflow_finish(flow_id, summary)` — 마무리; `taskflow_summary`로 언제든 재읽기 가능.

### step 상태 기계 (TaskFlow 권위 정의)

```
blocked (의존성이 모두 done이 아님; run_task는 등록만, spawn 없음)
  → ready (의존성 충족, 디스패치 대기; taskflow_resume이 언록)
  → dispatched (detached 자식 세션 spawn, child_session_key 영속화)
  → done (taskflow_resume으로 결과 주입 완료)
```

### HTN → TaskFlow 기능 매핑

| 계획 레이어 개념 (HTN)    | TaskFlow 기능                                                             |
| ------------------------- | ------------------------------------------------------------------------- |
| 체크박스 간 의존성        | step의 `depends_on` (step-id 리스트)                                      |
| 웨이브                    | 명시적 웨이브 없음; `ready` 상태 = "현재 웨이브"                          |
| 노드 상태                 | `status ∈ {blocked, ready, dispatched, done}` (`state_json` 내)           |
| Frontier (실행 가능 항목) | `ready` 상태 + `taskflow_dispatch`가 `deps_satisfied` 검증                |
| 의존성 미충족 시 블록     | `taskflow_run_task(..., depends_on=[...])`가 `blocked`로 등록, spawn 없음 |
| 완료 시 후속 언록         | `taskflow_resume`이 step을 `done`으로 + `unlock_dependents`               |
| 병렬 디스패치             | `taskflow_dispatch(flow_id, step_ids)`로 ready step 일괄 디스패치         |
| 병렬 자식 세션 대기       | `taskflow_wait_all(...)` flow 범위 유계 폴링                              |
| DAG 상태 재읽기           | `taskflow_summary(flow_id)`로 각 step의 status/depends_on + 카운트 표시   |

### 병렬 딜리버리 채널 결정

| 토폴로지                     | 조건                           | 전략                                        |
| ---------------------------- | ------------------------------ | ------------------------------------------- |
| 독립 채널 → 병렬 workers     | 분리 파일, 공유 계약 없음      | 일괄 병렬 spawn burst (`taskflow_dispatch`) |
| 순서 의존 채널 → 웨이브 직렬 | C가 A와 B 완료 필요            | 선행 step `done` 후 `depends_on` + `resume` 언록으로 실행 |
| 중복 채널 → team             | 동일 모듈/계약, 병렬이 더 빠름 | 직렬화 또는 수동 조정                       |

---

## 압축 보호 레이어

`build_system_prompt()`는 컨텍스트 압축 후 `Summarization` 미들웨어에 의해 재호출됩니다. 여기서 현재 todos + boulder 상태를 주입하면 압축에 자연적으로 면역됩니다.

### 주입 내용

| Block                      | 데이터 소스       | 컨텍스트 비용 | 설명                                              |
| -------------------------- | ----------------- | ------------- | ------------------------------------------------- |
| `_build_todo_block()`      | todos.db          | ~10행         | 현재 todo 리스트 + 상태 + TaskFlow flow/step 연결 |
| `_build_boulder_block()`   | src/data/boulder.json | ~5행          | 활성 작업 상태                                    |
| `_build_knowledge_block()` | workspace/knowledge/plans/&lt;plan_key&gt;/ | ~20행         | key_failures + key_successes + reusable_patterns (아이덴티티 해석) |

### 구현

```python
def _build_todo_block(session_id: str) -> str:
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        tag_parts = [t.get("category", "quick")]
        if t.get("delegation", "self") != "self":
            tag_parts.append(t["delegation"])
        if t.get("flow_id"):
            tag_parts.append(f"flow:{t['flow_id']}")
        if t.get("step_id"):
            tag_parts.append(t["step_id"])
        lines.append(f"- [{icon.get(t['status'], '○')}] ({', '.join(tag_parts)}) {t['content']} ({t['priority']})")
    lines.append("Update todos via todowrite. Pass the COMPLETE list each time.")
    lines.append("Dependency scheduling is owned by TaskFlow; declare depends_on via taskflow_run_task.")
    lines.append("Your todo list is tracked by the continuation system. Incomplete todos will trigger automatic continuation.")
    lines.append("Completion is verified by the Sisyphus contract — unverified claims will be rejected.")
    return "\n".join(lines)
```

### omo의 5층 방어보다 경량인 이유

| omo의 5층                     | 본 접근 방식                                       |
| ----------------------------- | -------------------------------------------------- |
| Prune 보호 리스트             | 불필요 (todos가 시스템 프롬프트 내)                |
| 압축 전 스냅샷 + 압축 후 복원 | 불필요 (시스템 프롬프트가 매번 DB에서 재구축)      |
| 8세그먼트 압축 컨텍스트 주입  | 불필요 (todos가 대화 기록에 없음)                  |
| 60s 압축 보호 창              | 불필요 (보호할 계속 주입기 없음)                   |
| 계속 강제기                   | 필요, after_agent 미들웨어로 간소화 (E3)           |
| 지식 요약 주입                | 신규 `_build_knowledge_block()` (약 25행)          |
| **합계 ~600+ 행**             | **~65행 (프롬프트 주입) + ~130행 (계속 미들웨어)** |

---

## 프론트엔드

### 프론트엔드 상태 레이어 (useTodoList.ts)

모듈 레벨 싱글톤, WebSocket 구동, TaskFlow flow별 그룹화:

```typescript
interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  flow_id?: string | null;
  step_id?: string | null;
  taskflow_status?: "blocked" | "ready" | "dispatched" | "done" | null;
}
```

WS 이벤트: `todo_updated` (변경 시 푸시) + `todo_refresh` (재연결 시 재전송).

### UI 컴포넌트

- **TodoDock.vue**: `flow_id`별 그룹화 표시, 진행 카운트, 접기 가능.
- **TodoItem.vue**: Checkbox (PrimeVue), category 배지, 위임 아이콘, in_progress 펄스 닷.

### WS 메시지 형식

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      {
        "content": "Implement login",
        "status": "completed",
        "priority": "high",
        "flow_id": "login-flow",
        "step_id": "step-1",
        "taskflow_status": "done"
      }
    ]
  }
}
```

`taskflow_status`는 `taskflow_summary(flow_id)`로 재읽기한 step 상태이며, 프론트엔드는 웨이브/의존성을 재계산하지 않습니다.
