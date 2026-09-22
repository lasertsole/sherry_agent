# TaskFlow — DAG 의존성을 갖춘 영속적 다중 단계 태스크 플로우

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> TaskFlow는 SQLite(낙관적 락, WAL 모드) 기반의 턴 간 영속 태스크 플로우 관리 시스템입니다. 핵심 기능: 분리된 서브에이전트 단계 디스패치, DAG 기반 의존성 관리, 배치 병렬 디스패치, 유계 폴링 대기, 멱등 결과 주입. 14개의 도구가 완전한 라이프사이클 API를 구성하며 openclaw managedFlows 인터페이스와 대응됩니다: `taskflow_create` → `taskflow_run_task` → `taskflow_dispatch` / `taskflow_wait_all` → `taskflow_resume` → `taskflow_finish` / `taskflow_fail` / `taskflow_cancel`, 그리고 `taskflow_summary`(읽기 전용 재조회), `taskflow_set_waiting`(대기 상태 전환), `taskflow_progress`(진행 보고서), `taskflow_budget`(토큰/비용 예산), `taskflow_update_steps`(단계 목록 전체 교체), `taskflow_list`(세션 보드).

신뢰할 수 있는 소스: `agent/tools/taskflow/tools/*.py`, `agent/tools/taskflow/registry/store_sqlite.py`, `agent/tools/taskflow/config.py`. 스킬 참조: `skills/builtin/core/taskflow/SKILL.md`.

---

## 목차

- [개요](#개요)
- [아키텍처](#아키텍처)
- [상태 머신](#상태-머신)
- [도구 패밀리 (14개 도구)](#도구-패밀리-14개-도구)
- [낙관적 락과 충돌 재시도](#낙관적-락과-충돌-재시도)
- [DAG 의존성 시스템](#dag-의존성-시스템)
- [병렬 단계 실행](#병렬-단계-실행)
- [멱등 리줌](#멱등-리줌)
- [이미 생성된 자식 에이전트를 동반한 충돌 재시도](#이미-생성된-자식-에이전트를-동반한-충돌-재시도)
- [영속화와 연결 라이프사이클](#영속화와-연결-라이프사이클)
- [알려진 제한 사항](#알려진-제한-사항)

---

## 개요

TaskFlow(`agent/tools/taskflow/`)는 SQLite(WAL 모드) 기반의 영속적 태스크 플로우 시스템으로, 여러 대화 턴에 걸친 다단계 작업을 관리합니다. 각 플로우는 라이프사이클(running → waiting → done/failed/cancelled)을 가지며, 단계는 서로 의존성을 선언할 수 있고, 결과는 분리된 자식 에이전트 세션에서 주입됩니다.

핵심 설계 결정:

- **낙관적 락**: 모든 변경이 `expected_revision`을 1씩 증분. 리비전 충돌로 동시 쓰기를 감지(최종 쓰기 우선이 아님).
- **state_json 내의 DAG**: 단계 의존성(`depends_on`), 상태, 결과는 모두 플로우의 `state_json` 컬럼 내에 저장——DAG 기능에 DB 스키마 마이그레이션 불필요.
- **분리된 디스패치**: 단계는 기존 spawn 파이프라인을 통해 분리된 자식 에이전트 세션을 spawn하여 실행. 결과는 announce/settle-wake 파이프라인을 통해 환류.
- **멱등 리줌**: `(child_session_key, result)` 쌍을 지문화. 중복 배달은 두 번 주입되지 않음.
- **에러 즉 텍스트 계약**: 도구는 LLM에 비즈니스 에러를 발생시키지 않음. `Error:` 접두사의 사람 가독 문자열을 반환.

---

## 아키텍처

```
agent/tools/taskflow/
├── __init__.py              # 패키지 내보내기 (11개 도구 재내보내기)
├── config.py                # TaskFlowStatus, StepStatus 열거형, TERMINAL_STATUSES, TABLE_NAME
├── step_judge.py            # StepJudge — 보조 LLM pass/retry/block 판정기
├── evidence_collector.py    # 판정 프롬프트용 세션/플로우 evidence 요약 렌더링
├── registry/
│   ├── __init__.py
│   └── store_sqlite.py      # SQLite 영속화: create/get/update, WAL, busy_timeout,
│                            #   FlowConflictError/FlowNotFoundError/FlowExistsError,
│                            #   동기 경로 (get_flow_sync)
└── tools/
    ├── __init__.py           # build_taskflow_tools() → 14개 도구, scope=main_only
    ├── _dispatch.py          # monkeypatch 가능한 디스패치 시임 (spawn_subagent_direct)
    ├── _shared.py            # DAG 헬퍼 + 충돌 재시도 영속화
    ├── taskflow_create.py    # 플로우 생성, 초기 리비전 1
    ├── taskflow_run_task.py  # 단계 등록 + 디스패치 (또는 의존성 미충족 시 차단)
    ├── taskflow_dispatch.py  # 여러 준비 완료 단계를 배치 디스패치
    ├── taskflow_wait_all.py  # 디스패치된 단계의 완료를 유계 폴링 대기
    ├── taskflow_resume.py    # 결과 주입, 완료 마크, 후속 단계 언락 (멱등)
    ├── taskflow_set_waiting.py # 플로우를 waiting 상태로 전환
    ├── taskflow_summary.py   # 읽기 전용 재조회 (충돌 후 재조회에도 사용)
    ├── taskflow_progress.py  # 읽기 전용 진행 보고서
    ├── taskflow_budget.py    # 토큰/비용 예산 조회와 설정
    ├── taskflow_update_steps.py # 단계 목록 전체 교체
    ├── taskflow_list.py      # 세션 단위 플로우 보드
    ├── taskflow_finish.py    # 완료 마크 (종단 상태)
    ├── taskflow_fail.py      # 실패 마크 (종단 상태)
    └── taskflow_cancel.py    # 플로우 취소 (종단 상태)
```

### 등록

도구는 `agent/tools/taskflow/tools/__init__.py`의 `build_taskflow_tools()`를 통해 등록되며, 전체 14개 도구를 `metadata = {"scope": "main_only"}` 및 `handle_tool_error = True` 태그와 함께 반환합니다. 서브에이전트 도구 정책은 이를 무조건 폐기——메인 에이전트만 공유 플로우 상태를 관리합니다.

---

## 상태 머신

### 플로우 상태 (`TaskFlowStatus`)

```
running ──→ waiting ──→ running (taskflow_resume 경유)
  │                       └──→ done    (taskflow_finish, 종단)
  │                       └──→ failed  (taskflow_fail, 종단)
  │                       └──→ cancelled(taskflow_cancel, 종단)
  └──→ done / failed / cancelled (running에서 직접)
```

종단 상태(`done`, `failed`, `cancelled`)는 불변: 모든 변경 도구는 상태 변경 전 `is_terminal(status)`을 점검하고 `Error: TaskFlow '<id>' is terminal (status=...); no further mutations allowed`를 반환합니다.

### 단계 상태 (`StepStatus`)

```
blocked → ready → dispatched → done
```

| 상태         | 의미                                                                       | 전환 트리거                             |
| ------------ | -------------------------------------------------------------------------- | --------------------------------------- |
| `blocked`    | 의존성이 아직 모두 `done`이 아님; `taskflow_run_task`는 등록만, spawn 없음 | `taskflow_resume`이 의존성 충족 시 언락 |
| `ready`      | 의존성 충족, 디스패치 대기                                                 | `taskflow_dispatch`가 디스패치          |
| `dispatched` | 분리된 자식 세션이 spawn됨, `child_session_key` 영속화됨                   | `taskflow_resume`이 결과 주입           |
| `done`       | 결과가 `taskflow_resume`로 주입됨                                          | (이 단계의 종단 상태)                   |

> `done`은 "결과가 주입됨"을 의미하며, **"자식 에이전트가 성공했음"을 의미하지 않습니다**. `failed`/`skipped` 단계 상태는 존재하지 않으며, 실패 인식 처리는 단계의 선택적 `retry_policy`와 단계 판정기(`block` 판정 또는 재시도 예산 소진 시 `blocked`)에 있습니다.

### 기존 단계 호환성

`status` 필드가 없는 단계는 `step_status()`로 처리: `child_session_key`를 가진 단계는 `dispatched`, 그렇지 않으면 `ready`로 취급. 해당 필드가 없는 플로우와의 하위 호환성을 보장합니다.

---

## 도구 패밀리 (14개 도구)

### taskflow_create

```python
async def taskflow_create(flow_id: str, description: str = "", initial_state: dict | None = None) -> str
```

영속적 플로우를 `INITIAL_REVISION = 1`, 상태 `running`으로 생성. 플로우 id, 상태, 리비전을 반환. id가 이미 사용 중이면 `FlowExistsError` (현재 리비전을 포함한 에러 문자열 반환, 재조회 가능).

### taskflow_run_task

```python
async def taskflow_run_task(
    flow_id: str, task: str, label: str | None = None,
    depends_on: list[str] | None = None,
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

플로우에 단계를 등록하고 분리된 자식 에이전트에 디스패치. 단계 id는 `step-1`, `step-2` 등으로 자동 할당.

- **의존성 없음** (또는 모두 충족) → 단계 즉시 `dispatched` (자식 spawn).
- **의존성 미충족** → 단계는 `blocked`로 등록, 자식 spawn 안 함. 알 수 없는 의존 id는 spawn이나 상태 변경 전에 거부.
- `step_id`, `child_session_key`, `revision` 반환 (차단 시 `pending=[...]`에 미충족 의존성 리스트).

### taskflow_dispatch

```python
async def taskflow_dispatch(
    flow_id: str, step_ids: list[str],
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

하나 이상의 현재 준비 완료 단계를 배치 디스패치. 단계가 `ready` 상태이거나, `blocked`이지만 의존성이 충족된 경우 디스패치 가능.

- **전부 아니면 전무 검증**: 모든 id를 spawn 전에 검증. 알 수 없는 id, 이미 디스패치/완료된 단계, 의존성 미충족 차단 단계는 전체 호출을 거부, spawn 0건.
- **배치 중간 실패**: spawn이 배치 중간에 실패하면, 성공한 spawn을 1회 `update_flow`로 영속화하여 자식 에이전트를 누락하지 않음. 에러는 실패 및 디스패치된 단계 id를 명시.
- 플로우급 `child_session_key`는 의도적으로 수정하지 않음——단계별 child key가 권위 소스.

### taskflow_wait_all

```python
async def taskflow_wait_all(
    flow_id: str, timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.5,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

이 플로우의 디스패치된 자식 세션이 완료(또는 타임아웃)될 때까지 대기. 이 플로우의 디스패치된 단계에 기록된 자식 세션만 폴링——무관한 백그라운드 자식 에이전트가 반환을 차단하지 않음.

- 알 수 없는/부재 중인 run은 완료된 것으로 간주(정지하지 않음).
- 타임아웃 시 부분 보고서를 반환하며, 완료된 자식 에이전트를 `taskflow_resume`으로 처리하고 `wait_all`을 다시 호출하도록 안내.
- 레지스트리 시임(`get_run_by_child_session_key` / `is_live_unended_run`)은 지연 임포트되며 주입 가능하여 테스트 용이.

### taskflow_resume

```python
async def taskflow_resume(
    flow_id: str, child_session_key: str = "", result: str = "",
    expected_revision: int | None = None, token_usage: dict | None = None,
    validation_criteria: str | None = None,
) -> str
```

완료된 자식 세션의 결과를 플로우 상태에 주입(멱등). `{child_session_key, result, result_hash, injected_at}`를 results에 추가. 플로우가 `waiting`이었다면 `running`으로 복귀.

**DAG 기장**: 매칭되는 단계를 `done`으로 마크하고 `unlock_dependents()`를 호출하여 새로 충족된 `blocked` 단계를 `ready`로 이동. 언락된 단계 id 반환. **resume은 결코 spawn하지 않음**——호출자는 `taskflow_dispatch`로 새로 준비된 단계를 명시적으로 디스패치해야 함.

**단계 판정기**: 단계가 `validation_criteria`를 가질 때(`taskflow_run_task`가 설정하거나 여기서 전달), 보조 LLM 판정기(`agent/tools/taskflow/step_judge.py`, 온도 0)가 결과를 기준에 비추어 심사하고 `pass` / `retry` / `block`을 반환. `retry`는 공유 `_retry` 시임을 통해 단계를 재디스패치하며, 단계 자체의 `retry_count` 예산(`STEP_JUDGE["max_retries"]`, 기본 2)을 재사용하고 판정기의 지침을 `with_judge_feedback`으로 대체 작업에 덧붙임. 예산이 소진되거나 `block` 판정이면 판정기 사유와 함께 단계를 `blocked`로 표시. 판정기에는 이 플로우의 evidence 요약이 제공되며 페일오픈 — 판정기 비활성, 모델 오류, 파싱 불가 응답은 `pass`로 강등됨.

### taskflow_set_waiting

```python
async def taskflow_set_waiting(
    flow_id: str, wait_reason: str = "",
    expected_revision: int | None = None,
) -> str
```

플로우를 `waiting` 상태로 전환하고 대기 사유를 기록. 대기 페이로드는 `taskflow_resume`으로 클리어.

### taskflow_summary

```python
async def taskflow_summary(flow_id: str) -> str
```

읽기 전용으로 플로우 상태를 전부 재조회: 상태, 리비전, child_session_key, 설명, 전체 단계(상태, depends_on, child_session_key 포함), 단계 상태 카운트, 결과, 대기 페이로드, 요약, 실패 사유, 취소 사유. 리비전 충돌 후의 지정 재조회 단계이기도 함.

### taskflow_progress

```python
async def taskflow_progress(flow_id: str) -> str
```

읽기 전용 완료 보고서: 완료율, 상태 분포, 다음 단계, 예상 남은 시간(`dispatched_at` 타임스탬프가 있는 `done` 단계가 2개 이상일 때). 플로우를 변경하지 않습니다.

### taskflow_budget

```python
async def taskflow_budget(
    flow_id: str, action: str = "query", token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

플로우의 토큰/비용 예산을 조회(`query`)하거나 설정(`set`)합니다. `query`는 `total_tokens`, `total_cost`, 예산, 남은 토큰, 상태(`ok` / 80%에서 `WARNING` / `EXCEEDED`)를 보고하고, `set`은 양의 `token_budget`을 요구하며 낙관적 락을 통해 기록합니다.

### taskflow_update_steps

```python
async def taskflow_update_steps(
    flow_id: str, steps: list[dict],
    expected_revision: int | None = None,
) -> str
```

플로우의 단계 목록을 전체 교체합니다(TaskFlow의 `todowrite`에 해당): 단계를 추가·삭제·재정렬하거나 `task`/`depends_on`을 다시 쓸 수 있습니다. 안전 규칙은 `dispatched` 단계를 해당 `child_session_key`에 묶어 두고 `done` 단계 재작성을 거부합니다. 자식이 아직 실행 중인 `dispatched` 단계를 삭제하면 성공하지만 자식 키를 명시한 비차단 `Warning:`을 반환합니다.

### taskflow_list

```python
async def taskflow_list(status_filter: str = "active") -> str
```

이 세션의 플로우에 대한 읽기 전용 보드: `"active"`(running + waiting), `"all"`(종단 상태 포함), 또는 정확한 상태 이름. 모든 읽기는 소유 `session_id`로 SQL 필터링되며, 렌더링된 표는 설명을 40자로 제한하고 플로우의 활동 타임스탬프에서 `updated_at`을 파생합니다.

### taskflow_finish / taskflow_fail / taskflow_cancel

```python
async def taskflow_finish(flow_id: str, summary: str = "", expected_revision: int | None = None,
                          todo: dict | None = None, plan_path: str | None = None,
                          checkbox_label: str | None = None) -> str
async def taskflow_fail(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
async def taskflow_cancel(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
```

종단 전환. `finish`는 `summary`, `fail`은 `failure_reason`, `cancel`은 `cancel_reason`을 플로우 상태에 기록. 디스패치된 자식 세션은 계속 실행되며, 플로우가 취소되기 전까지 결과는 `taskflow_resume`으로 배달 가능.

`finish`는 DONE 전환 전에 네 개의 게이트를 순서대로 통과: **A** 모든 단계가 `done` 또는 `blocked`; **B** `blocked` 단계가 없음; **C** 이 플로우의 evidence(`agent/tools/taskflow/evidence_collector.py`)에 `FAIL` 행도 `[stale]` 행도 없음; **D** `SisyphusVerifier` 통과 — 호출자가 `todo`와 `plan_path`를 모두 명시적으로 전달한 경우에만 해당하며 플로우/단계 schema 마이그레이션이 필요 없음. 모든 게이트는 페일오픈: evidence 수집기를 사용할 수 없거나 검증기가 오류를 내면 완료를 막지 않고 통과시킴.

---

## 낙관적 락과 충돌 재시도

모든 변경은 `UPDATE ... WHERE flow_id = ? AND expected_revision = ?`로 실행. 업데이트가 0행 매치되면, 쓰기가 충돌한(또는 플로우가 사라진) 것:

- **`FlowConflictError`**: 최신 리비전을 휴대하여 호출자가 재조회 후 재시도 가능. 에러 텍스트에 직접 사용 가능한 값이 내장: `expected_revision=2 but latest revision=3; re-read with taskflow_summary and retry with expected_revision=3`.
- **`FlowNotFoundError`**: 플로우가 삭제됨.

**재시도 흐름** (LLM 관점):

1. `taskflow_summary(flow_id)`를 호출하여 재조회하고 최신 `revision` 획득.
2. `expected_revision=<최신 리비전>`으로 변경을 재적용.
3. 충돌 에러 텍스트에 직접 사용 가능한 재시도 값이 포함.

종단 플로우는 불변: 모든 변경 도구가 상태 변경 전 `is_terminal(status)`을 점검.

---

## DAG 의존성 시스템

DAG 필드는 완전히 `state_json` 내에 존재(DB 마이그레이션 불필요). `StepStatus` 열거형이 4개 상태를 정의하고, `_shared.py`의 3개 순수 함수가 전환을 관리:

### deps_satisfied(step, steps) → bool

모든 `depends_on` id가 `steps`에 존재하고 `done`일 때 True. `depends_on`이 부재/빈은 자명하게 충족. 알 수 없는 dep id는 결코 충족되지 않음. **자기 의존성은 결코 충족되지 않아**, 자기 참조 단계를 안전하게 blocked 상태로 유지하며 무한 언락 루프를 발생시키지 않음.

### mark_step_done(steps, child_session_key) → str | None

`child_session_key`에 매칭되는 단계를 `done`으로 마크. 멱등: 동일 key에 대한 반복 호출은 동일 단계 id 반환. 해당 child key를 가진 단계가 없으면 `None` 반환.

### unlock_dependents(steps) → list[str]

`steps`를 **싱글 패스**로 순회: 의존성이 충족된 `blocked` 단계를 `ready`로 이동. 새로 준비된 단계 id 반환. 싱글 패스는 의존성 사이클이 무한 루프를 발생시키지 않음을 보장.

---

### 합성 — `aggregate_deps`

`taskflow_run_task`에 `aggregate_deps=true`를 넘기면 합성 단계가 의존성 단계의 기록된 결과를 디스패치되는 작업 텍스트 뒤에 받습니다. 집계는 `agent/tools/taskflow/tools/_shared.py::build_task_with_dep_results(step, steps, results)`가 구성합니다. `depends_on` 순서로 각 의존성 단계의 `child_session_key`를 flow의 `{child_session_key, result, result_hash}` 원장과 대조해 `## Upstream Results` 제목 아래에 덧붙입니다. 기록된 결과가 없는 의존성은 `no result recorded` 자리표시자가 됩니다.

플래그는 단계에 저장되므로 `taskflow_dispatch`, StepJudge 재시도, 재시도 정책 경로가 저장된 `task`를 재작성하지 않고 집계를 다시 도출합니다. 기본값(`aggregate_deps` 없음 또는 false)은 디스패치 텍스트를 변경하지 않습니다.

## 병렬 단계 실행

두 도구가 독립 단계의 병렬 실행을 지원:

### 배치 디스패치 (`taskflow_dispatch`)

모든 spawn 이전에 배치 전체를 검증(전부 아니면 전무). 공유 `_dispatch.dispatch_child` 시임을 통해 순차 spawn. 배치 중간 spawn 실패 시 배치를 중단하고 성공한 spawn을 1회 `update_flow`로 영속화. `apply_dispatched_steps()` 헬퍼가 신규 조회된 단계 리스트에 기록된 디스패치 단계 페이로드를 재적용하며, `step_id`로 매칭——spawn된 자식 에이전트가 동시 쓰기로 인한 단계 리스트 재작성에 의해 유실되지 않음.

### 유계 폴링 대기 (`taskflow_wait_all`)

플로우 스코프: 이 플로우의 디스패치된 단계에 기록된 자식 세션만 폴링. 무관한 백그라운드 자식 에이전트로 인해 차단되지 않음. 폴링 간격에 하한 설정(최소 `0.05s`). 알 수 없는/부재 중인 run은 완료된 것으로 간주(정지하지 않음). 타임아웃 시 부분 보고서 반환.

---

## 멱등 리줌

`taskflow_resume`은 `(child_session_key, result)` 쌍을 SHA-256으로 16 헥스 문자로 잘라 지문화. 주입 전에 플로우의 `results` 리스트에 동일한 `result_hash`가 이미 존재하는지 확인. 존재하는 경우, 호출은 no-op: 재주입도 리비전 증분도 수행하지 않음. 이를 통해 announce 파이프라인의 중복 배달이 안전해짐——중복 배달이 상태를 손상시키지 않음.

---

## 이미 생성된 자식 에이전트를 동반한 충돌 재시도

자식 세션이 이미 spawn되었지만 낙관적 락 쓰기가 경합에 패배한 경우, 자식 에이전트는 결코 조용히 폐기되지 않음. `_shared.py`의 `update_flow_with_conflict_retry()`가 이를 처리:

1. spawn 성공 후, 호출자가 `build_state` 콜백으로 이 함수를 호출.
2. `FlowConflictError` 시: 플로우를 재조회하고, 최신 데이터에 대해 `build_state(fresh_flow, attempt)`를 재호출하여 최대 `PERSIST_MAX_ATTEMPTS = 3`회까지 재시도.
3. `FlowNotFoundError` 또는 종단 상태의 신규 플로우 시: spawn된 모든 `child_session_key`를 명시한 에러 반환——호출자는 key로 회복해야 하며, 재디스패치하지 않아야 함.
4. 재시도 횟수 소진 시: 동일하게 모든 child key를 명시한 에러 반환.

`build_state` 콜백은 신규 플로우와 시도 횟수를 수신하여, 최신 데이터에 대해 상태를 재구성 가능(예: 현재 단계 수에 기반하여 `step_id` 재할당).

---

## 영속화와 연결 라이프사이클

`store_sqlite.py`는 서브에이전트 레지스트리의 청사진을 추종:

- **데이터베이스**: `agent/tools/taskflow/data/taskflow_registry.db`
- **테이블**: `task_flows(flow_id TEXT PK, state_json TEXT NOT NULL, wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, child_session_key TEXT)`
- **WAL 모드**: 프로세스 단위로 `_switch_to_wal_if_needed()`를 통해 1회 전환. 파일이 이미 WAL인 경우 pragma를 스킵.
- **비지 타임아웃**: 모든 연결에서 `5000ms` (첫 번째 구문). journal-mode 전환은 이를 확실히 존중하지 않으므로, try/except로 개별 허용.
- **비동기 초기화**: 프로세스 단위 1회, 호출자 이벤트 루프가 소유하는 `asyncio.Lock` 하에서 실행. 타 루프는 `_initialized`를 폴링(타 루프의 락에 절대 접근하지 않음——스레드 안전하지 않은 `call_soon` 웨이크업 회피).
- **동기 경로**: `get_flow_sync()`는 표준 라이브러리 `sqlite3`를 사용하며, `threading.Lock`으로 보호된 1회성 테이블 생성. 실패 시 로그 출력 후 `None` 반환(이벤트 루프가 존재하지 않는 시스템 프롬프트 주입용).

---

## 알려진 제한 사항

- **`done` ≠ 성공**: 단계 `done`은 "결과가 주입됨"만을 의미하며, "자식 에이전트가 성공했음"을 의미하지 않음. `failed`/`skipped` 단계 상태는 없으며, `validation_criteria`도 일치하는 `retry_policy`도 없는 단계는 자식이 실패해도 `done`으로 남고 후속 단계를 언락함. 실패 인식 복구를 위해서는 단계에 `retry_policy`를 부착: `taskflow_wait_all`은 재시도 예산이 남아 있는 동안 종료된 자식을 재디스패치하고, 예산 소진 후 실패 노트와 함께 `done`으로 마크함. `validation_criteria`가 있으면 판정기의 `block` 판정이나 재시도 예산 소진이 대신 단계를 `blocked`로 표시함.
- **`taskflow_wait_all` 타임아웃은 유계 폴링**: 완료되지 않는 자식 세션이 자동으로 플로우를 실패시키지 않음. 타임아웃은 부분 보고서 반환.
- **단계 id는 순차 할당**: 등록 시 `step-{len(steps)+1}` 할당. 단계가 동시에 추가되는 경우, id는 충돌 재시도 시 `build_state` 내에서 재계산.
