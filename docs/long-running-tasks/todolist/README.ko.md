# TaskFlow 기반 계획 및 규율 레이어

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 세션 범위의 경량 계획 및 규율 레이어로, 기존 TaskFlow 시스템 위에 구축됩니다. 이것은 **태스크 엔진이 아니며**, **DAG가 아니고**, **스케줄러를 구현하지 않습니다**. 세션 수준의 계획은 `todowrite` / `todoread`로 관리됩니다 (시스템 프롬프트 주입을 통해 압축에 면역). 크로스 세션, 영구적, 서브에이전트를 디스패치하는 실행은 **TaskFlow**가 담당합니다 (`depends_on`, `blocked/ready/dispatched/done`, `taskflow_dispatch`, `taskflow_wait_all`). 두 레이어는 `flow_id` / `step_id`로 연결되며, 단일 상태에는 하나의 권위 소스만 있고 레이어 간 복제가 없습니다.

---

## 목차

- [TaskFlow와의 관계](#taskflow와의-관계)
- [설계 철학 및 핵심 원칙](#설계-철학-및-핵심-원칙)
- [아키텍처 개요](#아키텍처-개요)
- [데이터 레이어](#데이터-레이어)
- [서비스 레이어](#서비스-레이어)
- [도구 레이어](#도구-레이어)
- [오케스트레이션 실행 레이어](#오케스트레이션-실행-레이어)
- [압축 보호 레이어](#압축-보호-레이어)
- [강제 레이어 E1–E7](#강제-레이어-e1e7)
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
| 계획 파일 진행 (체크박스)         | `.omo/plans/*.md`     | orchestrator가 `- [ ]` → `- [x]` 편집                    |
| 실행 증거                         | `.omo/ledger.jsonl`   | `EvidenceLedger` 추가                                    |
| 활성 작업 상태                    | `.omo/boulder.json`   | 계획 활성화/복원                                         |

### 연결 (단일 진실 소스)

todo는 TaskFlow를 가리키는 두 개의 선택적 필드를 가질 수 있습니다:

- `flow_id`: 연결된 TaskFlow flow id.
- `step_id`: 연결된 TaskFlow step id (예: `step-2`), 해당 step의 DAG 상태를 재읽기 위해.

**비미러링 원칙**: todo 레이어는 `depends_on`을 복사하지 않고, 웨이브를 저장하지 않으며, step 상태를 캐시하지 않습니다. DAG 상태가 필요할 때 읽기 전용 `taskflow_summary(flow_id)`를 호출하여 `blocked/ready/dispatched/done`을 UI/프롬프트에 매핑합니다.

---

## 설계 철학 및 핵심 원칙

HTN(계층적 태스크 네트워크) 패러다임 채택: 계획 파일 → 체크박스 → 원자 서브태스크 → subagent worker 위임 → 대항적 검증.

```
Plan (.omo/plans/*.md)
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

- 모델이 계획하지 않음 → E7이 가이던스 주입
- 모델이 게을러서 멈춤 → E3이 끌어옴
- 모델이 가짜 완료 → E5가 블록
- 모델이 직접 코드 작성 → E1 doctrine이 억제
- 모델이 일찍 완료 표시 → E4가 하드 블록

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

**Layer 5의 DAG 기능은 본 레이어에서 구현되지 않습니다** — TaskFlow가 제공합니다. 본 레이어는 호출만 하고, 재구축하지 않습니다.

---

## 데이터 레이어

### 계획 파일 (.omo/plans/*.md)

체크박스 형식의 Markdown으로, 완전한 HTN 분해를 정의합니다:

```markdown
# <Plan Name>

## Goal

<상세 목표: 계획명, 경로, 종단 상태, 딜리버리 모드, 검증 방법>

## Context

<프로젝트 배경, 제약, 알려진 정보>

## TODOs

### Wave 0: <Wave description>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave description> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder 상태 (.omo/boulder.json)

영구 작업 상태. `session_id`에 `sherry:` 접두사 사용:

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": ".omo/plans/<plan-name>.md",
      "session_ids": ["sherry:<session_id>"],
      "status": "active"
    }
  }
}
```

### 증거 원장 (.omo/ledger.jsonl)

한 줄에 하나의 JSON 객체로, 각 체크박스의 실행 증거를 기록합니다.

### todos.db — 세션 수준 TODO 저장

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

> **설계 경계**: `todos.db`는 `depends_on` / 웨이브 / step 상태를 보유하지 않습니다. 의존성 그래프, `blocked/ready/dispatched/done`과 언록 로직은 모두 TaskFlow가 제공합니다. todo는 `flow_id`/`step_id`로 해당 flow step을 가리킬 뿐이며, DAG 상태는 `taskflow_summary(flow_id)`로 재읽기합니다.

CRUD 인터페이스: `replace_all` (전량 교체), `get_todos` (position순), `get_todos_sync` (동기 경로, 프롬프트 주입용), `get_todos_by_flow` (flow 관련 todo 조회).

---

## 서비스 레이어

### TodoService

- `update_todos`: 전량 교체 + E4 전환 배리어 검증 + WS 푸시.
- `get_todos`: DB에서 읽기.
- `get_flow_progress`: TaskFlow의 DAG 상태를 todo 뷰에 매핑 (frontier 재계산 없음). 읽기 전용 `taskflow_summary`를 호출하여 step의 status/depends_on을 가져옴.

### EvidenceLedger

```python
class EvidenceLedger:
    LEDGER_PATH = ".omo/ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None:
        entry["timestamp"] = datetime.utcnow().isoformat()
        with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        ...
```

### WS 푸시

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

`build_todolist_tools()`에서 todowrite 설명을 오버라이드하고 MANDATORY 형식 규칙을 주입: 각 todo 제목은 WHERE/WHY/HOW/RESULT를 인코딩, 원자 입자도 (1-3 도구 호출로 완료), 동시에 하나의 in_progress만, subagent 반환 전 completed 마크 금지.

### SKILL.md

todolist를 언제 사용할지 (3+ 단계 복잡 작업), 사용 가능 도구, 상태/우선순위/위임 필드, DAG 필드 (TaskFlow에 위임), 규칙을 정의합니다.

---

## 오케스트레이션 실행 레이어

### 5단계 흐름

```
Phase 1: 계획 선택 → .omo/boulder.json 읽기, .omo/plans/*.md 리스트, 매치 또는 복원
Phase 2: Boulder 상태 생성/업데이트 → boulder.json 작성, 단계/태스크를 todos로 등록
Phase 3: 다음 체크박스 실행 (스케줄링은 모두 TaskFlow에 위임)
  → 첫 번째 미체크 체크박스 찾기
  → 원자 서브태스크로 분해
  → taskflow_run_task(flow_id, task, depends_on=[...])로 등록
  → blocked는 디스패치 안 함; ready는 taskflow_dispatch로 일괄 디스패치
  → taskflow_wait_all → taskflow_resume (결과 주입, 후속 언록)
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: 검증 및 증거 기록 → 5 gates → .omo/ledger.jsonl
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
| 순서 의존 채널 → 웨이브 직렬 | C가 A와 B 완료 필요            | 선행 step `done` + `resume` 언록 후 실행    |
| 중복 채널 → team             | 동일 모듈/계약, 병렬이 더 빠름 | 직렬화 또는 수동 조정                       |

---

## 압축 보호 레이어

`build_system_prompt()`는 컨텍스트 압축 후 `Summarization` 미들웨어에 의해 재호출됩니다. 여기서 현재 todos + boulder 상태를 주입하면 압축에 자연적으로 면역됩니다.

### 주입 내용

| Block                      | 데이터 소스       | 컨텍스트 비용 | 설명                                              |
| -------------------------- | ----------------- | ------------- | ------------------------------------------------- |
| `_build_todo_block()`      | todos.db          | ~10행         | 현재 todo 리스트 + 상태 + TaskFlow flow/step 연결 |
| `_build_boulder_block()`   | .omo/boulder.json | ~5행          | 활성 작업 상태                                    |
| `_build_knowledge_block()` | .omo/knowledge/   | ~20행         | key_failures + key_successes + reusable_patterns  |

### omo의 5층 방어보다 경량인 이유

| omo의 5층                     | 본 접근 방식                                       |
| ----------------------------- | -------------------------------------------------- |
| Prune 보호 리스트             | 불필요 (todos가 시스템 프롬프트 내)                |
| 압축 전 스냅샷 + 압축 후 복원 | 불필요 (시스템 프롬프트가 매번 DB에서 재구축)      |
| 8세그먼트 압축 컨텍스트 주입  | 불필요 (todos가 대화 기록에 없음)                  |
| 60s 압축 보호 창              | 불필요 (보호할 계속 주입기 없음)                   |
| 계속 강제기                   | 필요, after_agent 미들웨어로 간소화 (E3)           |
| **합계 ~600+ 행**             | **~65행 (프롬프트 주입) + ~130행 (계속 미들웨어)** |

---

## 강제 레이어 E1–E7

### E1: 시스템 프롬프트 강제 — orchestrator doctrine

`AGENTS.md`에 추가. 제로 코드, 순 텍스트. 핵심:

- YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER
- 다단계 태스크 (2+ 단계) → 반드시 먼저 todos 생성
- 각 단계에서 in_progress 마크 → subagent 반환 후 검증 → completed 마크
- 전환 배리어: subagent 실행 중 / TaskFlow step 미완료 시 completed 마크 금지
- Sisyphus 계약: DoneClaim → AdversarialVerify → FullyDone

### E2: 도구 설명 강제 — 형식 + 위임 규칙

`build_todolist_tools()`에서 todowrite 설명을 오버라이드하고 MANDATORY 형식 규칙을 주입: 제목에 WHERE/WHY/HOW/RESULT 인코딩, 원자 입자도, 동시에 하나의 in_progress만, subagent 반환 전 completed 마크 금지.

### E3: 계속 강제기 — 사후 클로저 코어 ★★

turn 종료 후 미완료 todo가 있으면 시스템이 자동으로 계속 메시지를 주입하여 LLM을 끌어옵니다. 모델의 자각에 의존하지 않습니다.

**주요 상수**: `_MAX_STAGNATION = 3` (연속 3회 변화 없음 → 정지), `_BASE_COOLDOWN_S = 2.0` (기본 백오프), `_MAX_COOLDOWN_S = 60.0` (최대 백오프), `_FAILURE_RESET_WINDOW_S = 300` (5분간 실패 없음 → 리셋), `_MAX_RECOVERY_ATTEMPTS = 2` (복구 모드 상한).

**워크플로우**: turn 종료 → todos 읽기 → 미완료 필터 → 정체 검출 → 백오프 냉각 → 계속 프롬프트 구축 → `maybe_trigger_auto_turn()`으로 주입. 사용자가 메시지 전송 → 취소 → reset().

### E4: 전환 배리어 — 이중 보험

**프롬프트 레벨** (E1 AGENTS.md): subagent 실행 중 / TaskFlow step 미완료 시 completed 마크 금지.

**코드 레벨 하드 블록** (`service.py`), 두 소스 모두 todolist 내에 스케줄러를 구축하지 않음:

1. **TaskFlow step 상태**: todo가 `flow_id`/`step_id`에 연결 → `done`만 `completed` 허용. `blocked`/`ready`/`dispatched`는 모두 블록.
2. **subagent registry liveness**: todo가 `subagent_id`에 연결 → `get_run_by_child_session_key` + `is_live_unended_run`으로 자식 세션 실행 중 판단.

### E5: Sisyphus 완료 계약 ★★

3단계 완료 검증으로 "가짜 완료"에 대한 최종 방어선:

```
Worker 반환 → DoneClaim → AdversarialVerify (5 gates) → FullyDone / NOT done
  Gate 1: Plan reread (계획 재읽기, 수락 기준 확인)
  Gate 2: Automated verification (검증 명령 실행)
  Gate 3: Manual QA (인공 또는 agent가 확인)
  Gate 4: Adversarial QA (stale state / dirty worktree / leftover resources 탐지)
  Gate 5: Cleanup (임시 리소스 정리)
```

### E6: 위임 라우팅 ★

**라이프사이클**: LLM이 todo 생성 (category + delegation) → #a fan-out reminder → #b category로 subagent 유형 지정 → #c AGENTS.md가 위임 방법 지도 → 의존성 step은 taskflow_run_task(depends_on) → ready step은 taskflow_dispatch → LLM이 subagent_id + flow_id/step_id 업데이트 → #d 전환 배리어 → taskflow_wait_all → taskflow_resume → E5 검증 → completed 마크.

**Category 라우팅 테이블**:

| Category     | 라우팅 대상                      | 설명                              |
| ------------ | -------------------------------- | --------------------------------- |
| `quick`      | subagent spawn (default model)   | 기계적, 단일 파일, 보일러플레이트 |
| `deep`       | subagent spawn (reasoning model) | 복잡한 디버깅, 연구 집약형        |
| `ultrabrain` | subagent spawn (최강 model)      | 진정으로 어려운 논리 문제         |
| `visual`     | subagent spawn                   | 프론트엔드, UI/UX                 |
| `git`        | subagent spawn                   | git 작업                          |
| `writing`    | subagent spawn                   | 문서                              |

### E7: 의도 인식 및 가이던스 — 사전 트리거 ★★

듀얼 모드 설계로 "모델이 자발적으로 계획 도구를 호출하지 않음" 문제를 해결:

| 시나리오                    | 모드       | 동작                                      |
| --------------------------- | ---------- | ----------------------------------------- |
| 최초 태스크 요청, 계획 없음 | E7a        | 완전 가이던스 프롬프트 주입               |
| arm 완료 + 신규 태스크 요청 | E7a 경량   | 짧은 리마인더 주입                        |
| 활성 계획 + 사용자 메시지   | E7b        | plan-active reminder 추가                 |
| 활성 계획 + turn 종료       | (E3)       | E7 불간섭                                 |
| 비태스크 (질문/잡담)        | 스킵       | 주입 안 함                                |
| 압축 후 + 신규 태스크       | E7a 재 arm | armed 플래그 클리어, 완전 가이던스 재주입 |

**의도 검출**: 경량 휴리스틱, LLM 호출 없음 (제로 지연, 제로 비용) — 질문 패턴 → 비태스크, 잡담 패턴 → 비태스크, 태스크 키워드 → 태스크, 장문 (>100자) 비질문 → 태스크 가능성.

**루프 방지 설계**: E7b가 E7a보다 우선, E7a는 세션당 1회, E7b는 before_model에서만, `_is_system_directive()`로 시스템 주입 메시지 필터, E3는 백오프 냉각 있음, 압축 후 re-arm.

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

`taskflow_status`는 `taskflow_summary(flow_id)`로 재읽기한 step 상태이며, 프론트엔드는 웨이브/의존성을 재계산하지 않습니다.
