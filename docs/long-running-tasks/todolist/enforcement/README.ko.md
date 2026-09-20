# 🛡️ TodoList 강제 레이어 — E1–E7와 오케스트레이터 규율

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [TodoList](../README.ko.md)의 일부: 오케스트레이터가 계획하고 위임하고 검증하며 정직하도록 유지하는 일곱 개의 강제 레이어(E1–E7).

---

## 강제 레이어 E1–E7

### E1: 시스템 프롬프트 강제 — orchestrator doctrine

`AGENTS.md`에 추가. 제로 코드, 순 텍스트:

```markdown
## Task Management & Orchestration (CRITICAL)

### Orchestrator Doctrine (MANDATORY)

YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.

- You DO NOT write code. You DO NOT edit product files.
- EVERY unit of implementation MUST be delegated to a spawned subagent.

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. For each checkbox: decompose into atomic sub-tasks for ONE worker.
3. DELEGATE every sub-task via task tool — route by category.
4. Before starting each step: Mark in_progress (only ONE at a time)
5. After subagent returns: verify via acceptance criteria, then mark completed.

### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Do NOT mark a TaskFlow-linked todo completed while its step is not `done`

### Completion Contract (Sisyphus)

- DoneClaim → AdversarialVerify → FullyDone
- If verification fails, the task is NOT done — re-dispatch or fix.

**FAILURE TO FOLLOW ORCHESTRATOR DOCTRINE = INCOMPLETE WORK.**
```

### E2: 도구 설명 강제 — 형식 + 위임 규칙

`build_todolist_tools()`에서 todowrite 설명을 오버라이드하고 MANDATORY 형식 규칙을 주입: 제목에 WHERE/WHY/HOW/RESULT 인코딩, 원자 입자도, 동시에 하나의 in_progress만, subagent 반환 전 completed 마크 금지.

### E3: 계속 강제기 — 사후 클로저 코어 ★★

turn 종료 후 미완료 todo가 있으면 시스템이 자동으로 계속 메시지를 주입하여 LLM을 끌어옵니다. 모델의 자각에 의존하지 않습니다.

**주요 상수**: `_MAX_STAGNATION = 3` (연속 3회 변화 없음 → 정지), `_BASE_COOLDOWN_S = 2.0` (기본 백오프), `_MAX_COOLDOWN_S = 60.0` (최대 백오프), `_FAILURE_RESET_WINDOW_S = 300` (5분간 실패 없음 → 리셋), `_MAX_RECOVERY_ATTEMPTS = 2` (복구 모드 상한).

**워크플로우**:

```
turn ends (no tool_call, agent loop exits)
  → Summarization.aafter_agent (rebuild system prompt with latest todos)
  → TodoContinuationEnforcer.aafter_agent
      → is_abort_error? → skip
      → get_todos_sync(session_id) → filter incomplete
      → check_stagnation: snapshot compare, N consecutive no-change?
          → should_enter_recovery? → RECOVERY_PROMPT
          → else: stop continuation
      → is_in_cooldown? → skip
      → build continuation prompt (full todo status + flow/step)
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _watch_user_takeover() 0.5s poll
      → user sends message → detect_state() busy → cancel → reset()
```

**파일**: `stagnation_tracker.py`(~90줄) + `todo_continuation/core.py`(~110줄) + `agent/core.py`(~2줄 등록).

### E4: 전환 배리어 — 이중 보험

**프롬프트 레벨** (E1 AGENTS.md): subagent 실행 중 / TaskFlow step 미완료 시 completed 마크 금지.

**코드 레벨 하드 블록** (`service.py`), 두 소스 모두 todolist 내에 스케줄러를 구축하지 않음:

1. **TaskFlow step 상태**: todo가 `flow_id`/`step_id`에 연결 → `done`만 `completed` 허용. `blocked`/`ready`/`dispatched`는 모두 블록.
2. **subagent registry liveness**: todo가 `subagent_id`에 연결 → `get_run_by_child_session_key` + `is_live_unended_run`으로 자식 세션 실행 중 판단.

```python
for todo in validated:
    if todo["status"] != "completed":
        continue
    # 소스 1: TaskFlow step 상태
    flow_id, step_id = todo.get("flow_id"), todo.get("step_id")
    if flow_id and step_id:
        step_status = _read_taskflow_step_status(flow_id, step_id)
        if step_status is not None and step_status != "done":
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} is '{step_status}'. "
                "Call taskflow_wait_all then taskflow_resume to inject the result first."
            )
    # 소스 2: subagent liveness
    if todo.get("subagent_id") and _is_subagent_running(todo["subagent_id"]):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {todo['subagent_id']} is still running."
        )
```

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

**구현**: `verifier.py`(~80줄) + `subagent_completion_drain/core.py` 확장(~15줄, subagent 완료 후 Sisyphus 검증 리마인더 추가).

### E6: 위임 라우팅 ★

**라이프사이클**:

```
LLM creates todo (with category + delegation fields)
  → #a fan-out reminder (first time per session)
  → #b category tells LLM which subagent type to spawn
  → #c AGENTS.md guides how to delegate
  → Dependent steps: taskflow_run_task(depends_on=[...]) → blocked or dispatched
  → Ready steps: taskflow_dispatch(flow_id, step_ids) → get child_session_key
  → LLM updates todo's subagent_id + flow_id/step_id
  → #d-prompt: "Do NOT mark done before step done / subagent returned"
  → #d-code: update_todos() checks step status + is_live_unended_run() → hard block
  → taskflow_wait_all → taskflow_resume → E5 Sisyphus verify → LLM marks completed
```

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

**루프 방지 설계**: E7b가 E7a보다 우선, E7a는 세션당 1회(`_armed_sessions` Set. 이미 armed면 짧은 리마인더만), E7b는 `before_model`에서만 실행(E3의 `after_agent` 계속은 E7b를 트리거하지 않음), `_is_system_directive()`로 시스템 주입 메시지 필터(E7 주입은 `metadata={"origin":"task_intent","internal":true}`를 가지며, 저장소와 필터가 내부 메시지로 올바르게 식별하여 실제 사용자 요청으로 취급하지 않고 세션 제목이나 사용자 요청 추출에도 들어가지 않습니다), E3는 백오프 냉각 있음, 압축 후 re-arm.

**파일**: `agent/middlewares/task_intent/core.py`(~160줄) + `agent/core.py`(~2줄 등록).
