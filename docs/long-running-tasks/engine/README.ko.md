# 🧩 TaskFlow 엔진 — DAG, 재시도, 예산, 데드라인과 보드

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Long-Running Tasks](../README.ko.md)의 일부: 영속 DAG 엔진, 단계 재시도 정책, 토큰/비용 예산, 데드라인, 결과 검증, 진행 보고서, 유휴 감지, 세션 보드.

---

## 🧩 TaskFlow DAG 엔진

### 상태 열거형(`agent/tools/taskflow/config.py`)

```python
TABLE_NAME = "task_flows"          # config.py:5
INITIAL_REVISION = 1               # config.py:8

class TaskFlowStatus(StrEnum):     # config.py:11
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class StepStatus(StrEnum):         # config.py:21
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})   # config.py:35
```

### 테이블 스키마(`agent/tools/taskflow/registry/store_sqlite.py`)

```sql
CREATE TABLE IF NOT EXISTS task_flows (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT ''
);
```

DAG 자체(`steps[]`, `results[]`, `depends_on`, `creator_session_key`)는 전부 `state_json` 안에 있습니다 — DAG 필드를 추가하는 데 스키마 마이그레이션이 필요 없습니다. 토큰/비용/데드라인 컬럼은 추가적 DDL(`_TOKEN_COLUMN_DDL`, `_DEADLINE_COLUMN_DDL`, `_SESSION_ID_COLUMN_DDL`, `store_sqlite.py:83-107`)로 정의됩니다. `session_id`는 격리 컬럼입니다(`idx_taskflow_session_status` 인덱스): 생성 시 기록되며 이후 모든 읽기/변경이 이를 필터링합니다(`WHERE flow_id = ? AND session_id = ?`, `WHERE session_id = ? AND status IN (…)`). WAL 프라그마는 프로세스당 한 번만 전환되며, 모든 문장 앞에 `PRAGMA busy_timeout = 5000`이 실행됩니다(`agent/tools/pub_base/sqlite_store.py:79-139`).

### 단계 상태 기계

```
blocked ──(의존 충족)──▶ ready ──(디스패치)──▶ dispatched ──(재개)──▶ done
```

| 상태 | 의미 | 설정 주체 |
| :--- | :--- | :--- |
| `blocked` | 최소 하나의 `depends_on`이 `done`이 아님; 자식이 생성되지 않음 | `taskflow_run_task` |
| `ready` | 모든 의존이 충족되어 디스패치 대기 | 등록 시, 또는 재개 후 `unlock_dependents` |
| `dispatched` | 분리된 자식 세션이 생성됨; `child_session_key`와 `dispatched_at` 기록 | `taskflow_run_task` / `taskflow_dispatch` |
| `done` | `taskflow_resume`가 결과를 주입함 | `taskflow_resume` |

`done`은 **"결과가 주입됨"**을 뜻하며 "자식이 성공함"이 아닙니다([알려진 한계](../README.ko.md#-알려진-한계) 참조). `status` 필드가 없는 레거시 단계는 파생됩니다: `child_session_key`를 가진 단계는 `dispatched`, 그렇지 않으면 `ready`로 간주됩니다(`_shared.py:85`, `step_status`).

### `depends_on` 의미론

`deps_satisfied(step, steps)`(`_shared.py:99`)는 모든 `depends_on` id가 현재 단계 목록에 존재**하고** `done`일 때만 참입니다. `depends_on`이 없거나 비면 자명하게 충족됩니다. 알 수 없는 의존 id는 결코 충족되지 않으며, **자기 의존도 결코 충족되지 않습니다** — 따라서 자기 참조 단계는 언락 루프에 빠지지 않고 안전하게 블록된 상태로 남습니다. `unlock_dependents(steps)`(`_shared.py:137`)는 목록을 **단 한 번만 순회**하므로 의존 사이클이 루프를 도는 것을 구조적으로 막습니다. `taskflow_run_task`는 디스패치나 상태 변경 **전에** 알 수 없는 의존 id를 거부합니다.

### 도구 패밀리(14개 도구)

모든 도구는 `async`이며 `@tool("taskflow_…")`로 데코레이트되고, `build_taskflow_tools()`(`tools/__init__.py:58-76`)가 `metadata={"scope": "main_only"}`와 `handle_tool_error=True`를 부여합니다. 공유 flow 상태를 관리할 수 있는 것은 메인 에이전트뿐이며, 서브에이전트 도구 정책은 패밀리 전체를 무조건 제거합니다.

| 도구 | 목적 |
| :--- | :--- |
| `taskflow_create` | 리비전 1로 flow 생성; 선택적으로 `deadline_hours` 설정 |
| `taskflow_run_task` | 단계를 등록(선택적 `validation_criteria` / `retry_policy`)하고 디스패치(또는 `blocked`로 기록) |
| `taskflow_dispatch` | 여러 준비된 단계를 전부 아니면 전무로 일괄 디스패치 |
| `taskflow_update_steps` | 단계 목록 전체 교체(추가/삭제/재정렬/task·depends_on 재작성; dispatched/done 안전 규칙; 고아 child 경고) |
| `taskflow_wait_all` | flow 범위 유계 폴링으로 디스패치된 단계의 정착을 대기(정책 단계 자동 재시도) |
| `taskflow_resume` | 자식 결과를 멱등하게 주입하고 후속 단계를 언락하며 토큰을 집계(실패 인식 재시도 + 기준 에코) |
| `taskflow_set_waiting` | 이유와 함께 flow를 `waiting`으로 파킹 |
| `taskflow_summary` | 읽기 전용 재조회(충돌 후 재조회에도 사용) |
| `taskflow_progress` | 사람이 읽을 수 있는 진행/완료 보고서 |
| `taskflow_budget` | 토큰/비용 예산 조회 또는 설정 |
| `taskflow_list` | 이 세션의 보드(`active` / `all` / 상태 이름) |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 종단 전환 |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:40
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    session_id: SessionId = "",
) -> str
```

### 디스패치 — `taskflow_dispatch` 일괄 의미론

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")`(`taskflow_dispatch.py:36`)는 무엇이든 생성되기 **전에** **모든** id를 검증합니다. 단계가 `ready`이거나, `blocked`이지만 의존이 이미 충족되었으면 디스패치 가능합니다. 알 수 없는 id, 중복 id, 이미 `dispatched`/`done`인 단계는 호출 전체를 거부하며 생성이 전혀 일어나지 않습니다. 성공하면 단계는 공유 `_dispatch.dispatch_child` 시임(`_dispatch.py:10`)을 통해 순차 생성되고 **한 번의** `update_flow` 호출로 영속화됩니다. 배치 중간에 생성이 실패하면 루프가 멈추고, 이미 생성된 자식이 영속화되어 자식이 조용히 유실되지 않습니다. 오류는 실패한 step id와 디스패치된 step id를 모두 나열합니다. flow 수준 `child_session_key`는 의도적으로 건드리지 않습니다 — 각 단계 자신의 child key가 권위입니다.

### 업데이트 — `taskflow_update_steps` 전체 교체

`taskflow_update_steps(flow_id, steps, expected_revision=None, session_id="")`(`taskflow_update_steps.py:191`)는 flow의 단계 목록을 전체 교체합니다(TaskFlow의 `todowrite`에 해당): 저장되는 DAG는 전달한 목록 그대로이며, 단계 추가·삭제·재정렬·`task`/`depends_on` 재작성이 가능합니다. 안전 규칙: `step_id`는 고유해야 하고 모든 `depends_on`은 새 목록에 존재하는 id를 참조해야 합니다(자기 의존 금지). `dispatched` 단계는 `child_session_key`를 유지하며 `ready`/`blocked`로 강등할 수 없습니다. `done` 단계는 task/depends_on/status를 변경할 수 없습니다. 새 단계는 `ready`/`blocked`여야 합니다(디스패치는 `taskflow_dispatch` 경유). 종단 flow는 호출을 거부합니다. 실행 중인 `dispatched` 단계를 삭제하면 성공하지만 비차단 `Warning:`(child key 포함)을 반환합니다 — 먼저 child를 kill하거나 `taskflow_wait_all`/`taskflow_resume`으로 정착시키세요. 동시성은 동일한 낙관적 잠금을 사용하며, `expected_revision` 불일치는 최신 리비전과 함께 거부되어 재조회 후 재시도에 사용됩니다.

### 대기 — `taskflow_wait_all`의 flow 범위

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")`(`taskflow_wait_all.py:110`)는 *이* flow의 `dispatched` 단계에 기록된 자식 세션**만** 폴링하므로, 무관한 백그라운드 자식이 반환을 막을 수 없습니다. 알 수 없거나 이미 정리된 run은 정착된 것으로 간주됩니다(절대 멈추지 않음). 폴링 간격은 `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]`(0.05초)로 클램프됩니다. 타임아웃 시 부분 보고서와 함께, 정착된 자식에 대해 `taskflow_resume`을 호출하고 `wait_all`을 다시 호출하라는 안내를 반환합니다. 요청자 세션의 모든 자식이 정착될 때만 발화하는 `sessions_yield`는 의도적으로 재사용하지 않습니다.

### 낙관적 잠금

모든 변경은 `UPDATE … WHERE flow_id = ? AND expected_revision = ?`를 거치며 리비전을 정확히 1만큼 올립니다(`store_sqlite.py:393-406`). 일치하는 행이 0이면 충돌입니다:

```python
# FlowConflictError 메시지(store_sqlite.py:177)
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

자식이 **이미 생성된** 상태에서 쓰기가 경합에 지면, `update_flow_with_conflict_retry()`(`_shared.py:191`)가 최신 flow를 다시 읽고 `build_state` 콜백으로 상태를 재구성하며 `PERSIST_MAX_ATTEMPTS = 3`회까지 재시도합니다. flow가 사라졌거나 종단이 되었거나 재시도가 소진되면, 생성된 모든 `child_session_key`를 나열한 오류를 반환하고 호출자에게 **key로 회수하고 절대 재디스패치하지 말라**고 지시합니다.

## 🔁 단계 재시도 정책

단계는 선언적 `retry_policy`를 가질 수 있어, 실패한 자식이 단계의 최종 결과로 수용되는 대신 자동으로 재디스패치됩니다. 정책, 카운터, 대체 자식 세션 key는 모두 `state_json` 안에 있으며(스키마 마이그레이션 없음), 재시도 헬퍼는 `agent/tools/taskflow/tools/_retry.py`에 있습니다.

```python
# retry_policy on a step (stored by taskflow_run_task)
{"max_retries": 2, "retry_delay_seconds": 30.0, "retry_on": ["timeout", "rate_limit"]}
```

| 필드 | 타입 | 기본값 | 의미 |
| :--- | :--- | :--- | :--- |
| `max_retries` | 음이 아닌 정수 | `0` | 최대 **재**디스패치 횟수 |
| `retry_delay_seconds` | 음이 아닌 수 | `60.0`(`DEFAULT_RETRY_DELAY_SECONDS`) | 각 재디스패치 전에 대기하는 백오프 |
| `retry_on` | `list[str]` | `[]` | 재시도를 유발하는 실패 유형; 비어 있으면 분류된 모든 실패 |

`validate_policy()`(`_retry.py:120`)는 형식이 잘못된 정책을 `taskflow_run_task` 시점에 거부합니다 — dict가 아닌 정책, 음수/비정수 `max_retries`, 음수/비수치 `retry_delay_seconds`, 문자열 리스트가 아닌 `retry_on` — 어느 것이든 생성이나 쓰기 전에 `Error:` 문자열을 반환합니다. 재개 시점에 누락/잘못된 저장 정책은 `None`(`normalize_policy`)으로 퇴화하여, 호출을 실패시키는 대신 무재시도 동작으로 폴백합니다.

`retry_count`(단계에 저장, 기본 `0`)는 **재디스패치** 횟수를 세며 최초 디스패치는 세지 않습니다: 첫 자식이 도는 동안 `0`, 첫 대체 자식이 생성되면 `1`. `retry_count < max_retries`인 동안 재시도가 허용됩니다(`retries_remaining`, `_retry.py:95`). `apply_redispatch()`(`_retry.py:170`)는 단계를 제자리에서 변경합니다 — 새 `child_session_key`, `dispatched_at`, `status = dispatched`, 증가된 `retry_count`.

### 실패 분류

`classify_failure(result)`(`_retry.py:56`)는 **결과 텍스트 휴리스틱**입니다. 텍스트를 소문자로 만들고 실패 단어를 담고 있지만 성공을 뜻하는 표현(`_NEGATED_FAILURE_PHRASES`, 예: `"no error"`, `"error-free"`)을 제거한 뒤, 처음 일치하는 순서 있는 버킷을 반환합니다:

| 분류 유형 | 트리거 부분 문자열 |
| :--- | :--- |
| `timeout` | `timeout`, `timed out`, `time limit` |
| `rate_limit` | `rate limit`, `rate_limit`, `429`, `too many requests` |
| `error` | `error`, `failed`, `failure`, `exception`, `traceback`, `aborted`, `crashed` |

깨끗한 결과는 절대 재시도하지 않습니다. `retry_on`이 비어 있지 않으면 일치하는 분류 유형만 재시도하고, 비어 있으면 분류된 모든 실패가 재시도합니다(`should_retry_failure`, `_retry.py:103`).

### 두 가지 자동 재디스패치 경로

| 진입점 | 트리거 | 동작 |
| :--- | :--- | :--- |
| `taskflow_wait_all` | 폴링된 자식이 결과 없이 **사망** 정착 | `plan_settled_retries()`가 예산이 남는 한 정착 단계마다 한 번 재디스패치하고, 소진되면 실패 노트 결과와 함께 `done`으로 표시 |
| `taskflow_resume` | 주입된 결과 텍스트가 `retry_on`이 허용하는 **실패**로 분류됨 | 대체 자식을 생성하고 실패 결과를 기록하며, 단계를 새 자식 위에서 `dispatched`로 유지 |

- **`taskflow_wait_all`** 은 모든 대상이 정착한 뒤 `_retry_settled_steps()`(`taskflow_wait_all.py:117`)를 호출합니다. 정책이 없는 단계는 아무 동작도 만들지 않으며(레거시 flow는 바이트 단위로 동일한 출력을 유지), 결과가 이미 주입된 자식은 그대로 둡니다. 호출당 정착 단계마다 **최대 하나**의 재시도 결정만 실행됩니다 — 대체 자식이 생성·기록되고 오케스트레이터가 다시 `wait_all`을 호출해 기다립니다. 도구 안에 백그라운드 재시도 루프는 없습니다. 대체 자식은 `persist_retry_actions()`(`_retry.py:254`)로 영속화되며, `update_flow_with_conflict_retry()`를 통해 계획을 새로 읽은 단계 목록에 다시 적용하므로 동시 작성자가 생성된 대체 자식을 떨어뜨릴 수 없습니다.
- **`taskflow_resume`** 은 단계를 done으로 표시하기 전에 정책을 확인합니다(`taskflow_resume.py:122-144`). 재시도 가능한 실패에서는 `retry_delay_seconds`만큼 대기한 뒤 대체 자식을 생성하고, 단계는 새 자식 위에서 `dispatched`로 남습니다. 대체 자식 생성 자체가 예외를 던지면 단계는 실패 결과와 함께 `done`으로 남고 응답에는 그 예외를 지목하는 `retry:` 노트가 붙습니다.

### 소진과 실패 노트

`retry_count`가 `max_retries`에 도달하면 `plan_settled_retries()`는 재디스패치 대신 `exhausted` 액션을 냅니다. 단계는 `done`으로 표시되고 `retry_exhausted: True`를 지닌 실패 노트 결과가 덧붙여지며, 이는 `exhausted_note()` + `failure_result_record()`가 생성합니다(`_retry.py:178-196`):

```
retry budget exhausted: step step-4 child agent:main:session:... settled without a
result after 2 retry/retries (max_retries=2); marked done by taskflow_wait_all
```

소진된 단계는 명시적 결정이 필요합니다 — 실패 노트를 재개하거나 flow를 실패시키십시오. `wait_all`과 `resume` 두 경로 모두 resume의 멱등 계약을 지킵니다: 실패 노트는 `result_hash`를 지니므로 재전달된 정착이 두 번 기록되지 않습니다.

## 🪙 토큰 / 비용 예산

`taskflow_budget`(`taskflow_budget.py:10`)은 조회와 설정을 모두 담당합니다:

```python
@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",           # "query" | "set"
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

- **`query`**는 `total_tokens`, `total_cost`, 예산, 남은 토큰, 상태를 보고합니다: `total_tokens >= budget`이면 `EXCEEDED`; 사용량이 `MODEL_PRICING["budget_warn_threshold"]`(0.80) 이상이면 `WARNING`; 그 외에는 `ok`. 예산이 없으면 백분율은 미설정으로 보고됩니다.
- **`set`**은 양의 정수 `token_budget`을 요구하고 종단 flow를 거부하며 낙관적 잠금을 통해 씁니다(`expected_revision`을 전달하면 빠르게 실패).

호출자가 `token_usage`를 전달하면 지출이 `taskflow_resume`에서 집계됩니다(`taskflow_resume.py:108-122`):

```python
token_usage={"input_tokens": 1200, "output_tokens": 800, "model_name": "deepseek-chat"}
```

이 도구는 지연 임포트 `from config.features import MODEL_PRICING`을 수행하고, `MODEL_PRICING["model_pricing_per_m_tokens"]`에서 모델을 조회하며(`_default`로 폴백), 다음을 계산합니다:

```
total_tokens = existing_total_tokens + input_tokens + output_tokens
cost_delta   = input_tokens  * pricing["input"]  / 1_000_000
             + output_tokens * pricing["output"] / 1_000_000
total_cost   = round(existing_total_cost + cost_delta, 6)
```

`MODEL_PRICING`은 `config/features/infra_side/model_pricing.py`에 있습니다:

| 모델 | 입력(USD / 백만 토큰) | 출력(USD / 백만 토큰) |
| :--- | :--- | :--- |
| `glm-5` | 0.5 | 1.5 |
| `deepseek-chat` | 0.14 | 0.28 |
| `kimi-latest` | 0.55 | 2.19 |
| `_default` | 1.0 | 3.0 |

`budget_warn_threshold` = `0.80`.

## ⏰ 작업 데드라인

`taskflow_create`는 선택적 `deadline_hours`를 받습니다(`taskflow_create.py:17`):

```python
@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
    deadline_hours: float | None = None,
) -> str
```

`deadline_hours`가 양수이면 도구는 `deadline_ts = time.time() + deadline_hours * 3600`을 `deadline_ts` 컬럼에 저장합니다. `taskflow_summary`는 데드라인을 렌더링하고 지나면 `EXCEEDED`로 표시합니다. 실제 집행자는 백그라운드 **sweeper**(`agent/tools/subagent/registry/sweeper.py`)이며, 매 스윕 주기마다 `_expire_overdue_taskflows()`를 실행합니다(`sweeper.py:123`):

```python
overdue = await taskflow_store.get_overdue_flows(time.time())
for flow in overdue:
    state["failure_reason"] = "Deadline exceeded: " + time.strftime("%Y-%m-%d %H:%M", ...)
    update_flow(flow_id, flow["expected_revision"], state=state, status=TaskFlowStatus.FAILED.value)
```

기한 초과 flow는 `failed`로 표시됩니다. 이미 종단인 flow는 쿼리에서 제외되고, flow별 예외는 로그로 남긴 뒤 삼켜지므로 잘못된 행 하나가 전체 스윕을 중단시키지 않습니다.

## ✅ 결과 검증

단계는 자연어 **수용 기준**을 가질 수 있어 오케스트레이터가 자식 결과를 판단할 구체적 근거를 얻습니다. 두 도구 모두 `validation_criteria`를 받습니다:

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:46
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,     # stored on the step
    retry_policy: dict | None = None,           # (see above)
    session_id: SessionId = "",
) -> str
```

`taskflow_run_task`는 공백을 제거한 뒤 비어 있지 않은 `validation_criteria`를 단계에 저장합니다(`blocked`와 `dispatched` 두 쓰기 경로 모두). 재개 시점에 기준은 강제되는 대신 오케스트레이터에게 **에코**됩니다:

```python
# taskflow_resume.py:146-157
if step is not None and redispatched_key is None:
    step["status"] = str(StepStatus.DONE)
    criteria = (validation_criteria or "").strip()
    if criteria:
        step["validation_criteria"] = criteria
    stored_criteria = str(step.get("validation_criteria") or "").strip()
    if stored_criteria:
        validation_text = (
            f"\n  validation_criteria: {stored_criteria}"
            f"\n  ⚠ Result needs validation against criteria"
        )
```

도구는 결코 기준을 평가하지 않습니다 — 주입된 결과와 나란히 제시할 뿐이며, 응답 끝에 `validation_criteria: …`와 `⚠ Result needs validation against criteria` 블록이 붙습니다. 중요한 가드레일 두 가지:

- `taskflow_resume`에 `validation_criteria`를 전달하면 저장 값이 **덮어써집니다**(예: 자식이 실제로 한 일에 따라 기준을 강화하거나 교정하기 위해).
- 에코는 단계가 실제로 `done`으로 표시될 때(`redispatched_key is None`)에만 나옵니다. 재시도 정책으로 재디스패치된 단계는 기준을 저장한 채 두고, 최종적으로 성공한 재개에서 에코를 받습니다.

판정자는 오케스트레이터(메인 에이전트 모델)입니다: 자식 결과를 기준과 비교한 뒤 단계를 수용할지, 재디스패치할지, flow를 실패시킬지 결정합니다. 자동 합격/불합격 게이트는 없습니다.

## 📊 진행 보고서

`taskflow_progress`(`taskflow_progress.py:22`)는 완료율, 내역, 다음 단계, 예상 남은 시간을 담은 읽기 전용 보고서입니다:

```python
@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str
```

출력 형태:

```
Progress Report: <flow_id>
  Status: running
  Description: <앞 80자>
  Completion: 3/5 steps (60%)
  Breakdown: done=3 · dispatched=1 · ready=0 · blocked=1
  Next steps:
    → [step-4] <작업, 앞 60자>
    ⊘ [step-5] <작업, 앞 60자>
  Est. remaining: ~12.5 minutes (based on 3 completed steps)
  Waiting on: <대기 이유>              # flow가 waiting일 때만
  Results injected: 3                  # 결과가 있을 때만
```

상태 아이콘은 `done=✓`, `dispatched=→`, `ready=○`, `blocked=⊘`(`taskflow_progress.py:14`). 추정치는 최소 **두 개**의 `done` 단계가 `dispatched_at` 타임스탬프를 가질 때만 생성됩니다. 디스패치 시각 사이의 평균 간격을 구해 남은 단계 수를 곱합니다. 단계가 없는 flow는 `Progress: flow_id=…, status=…`와 `No steps registered yet.`을 반환합니다.

## 💤 유휴 감지

`taskflow_set_waiting`으로 파킹된 flow는 자식이 크래시하여 재개되지 못할 수 있습니다. sweeper는 `_scan_stale_waiting_taskflows()`로 이를 감지합니다(`sweeper.py:154`):

1. 모든 `waiting` flow를 읽습니다.
2. `timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600`(기본 24시간).
3. `set_at`이 있는 대기 페이로드에 대해 `now - set_at <= timeout_secs`인 동안 건너뜁니다.
4. `get_run_by_child_session_key(flow["child_session_key"])` + `is_live_unended_run(run)`으로 자식 생존을 확인합니다. 자식이 아직 살아 있으면 건너뛰고, 생존 확인 임포트 자체가 실패하면 자식을 죽은 것으로 간주합니다.
5. 그렇지 않으면 `wait_json`에 **비파괴 마커**를 찍습니다: `stale_detected_at`, `stale_child_session_key`.

유휴 감지는 flow를 **절대 자동 실패시키지 않습니다** — 마커는 권고용이며 주기마다 갱신됩니다. 이와 별개로 `taskflow_summary`는 대기 상태를 렌더링하고, 대기가 `TASKFLOW_INFRA["waiting_timeout_hours"]`(24시간)를 넘으면 `wait_status: STALE (waiting X.Xh, timeout=24h) — child session may have crashed; consider taskflow_resume with a failure result or re-dispatch`를 출력합니다(`taskflow_summary.py:67-90`).

## 📋 세션 보드와 격리

각 세션은 자신의 데이터만 봅니다. `taskflow_summary`, 자동 재개 판독기, 그리고 `taskflow_list`까지 모두 `session_id`로 범위가 정해지며 저장소가 SQL 계층에서 필터링합니다 — 다른 세션의 flow는 존재하지 않는 flow와 구분할 수 없습니다(변경 시 `FlowNotFoundError`, 읽기 시 `None`):

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active", session_id: SessionId = "") -> str
```

| `status_filter` | 행 |
| :--- | :--- |
| `"active"`(기본) | 이 세션의 `running` + `waiting` |
| `"all"` | 이 세션의 flow(종단 포함) |
| 그 밖의 값 | 상태 정확 일치(`running`, `waiting`, `done`, `failed`, `cancelled`) |

읽기 전용(`expected_revision` 불필요)이며 `store_sqlite.get_all_flows_sync(session_id, status_filter)`가 뒷받침합니다. 동기 판독기는 이벤트 루프가 필요 없는 stdlib `sqlite3` 경로를 사용하고, 행을 `expected_revision DESC`(가장 최근 활성 순)로 정렬하며 페일오픈입니다 — 초기화/읽기 실패는 `[]`를 반환합니다. `"active"`는 `get_active_flows_sync(session_id)`에 위임합니다.

렌더링되는 보드는 고정 열의 패딩된 텍스트 표이며, 설명은 40자, creator key는 16자로 제한됩니다:

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

스키마에는 **`updated_at` 컬럼이 없습니다**(마이그레이션 없음). 따라서 `_last_activity_ts()`(`taskflow_list.py:28`)는 "마지막 업데이트"를 flow 어딘가에 영속된 활동 스탬프의 최댓값으로 도출합니다 — `wait.set_at`, 모든 `step.dispatched_at`, 모든 `result.injected_at` — 이를 UTC 타임스탬프로 렌더링합니다(스탬프가 전혀 없는 flow는 `-`). 빈 레지스트리는 `No task flows found`를 반환합니다.

### 세션 소유의 세 가지 계획 도구 패밀리

| 패밀리 | 세션 연결 | 저장소 |
| :--- | :--- | :--- |
| **TaskFlow** | `task_flows.session_id` 컬럼(이번 변경) | `agent/tools/taskflow/registry/store_sqlite.py` |
| **TodoList** | `session_id`가 테이블의 기본키 접두사 — 처음부터 세션 범위 | `agent/tools/todolist/registry/store_sqlite.py` |
| **Knowledge** | 계획 아이덴티티 단위로 격리(도구 계층에서 강제): 연결은 `ownership.association_plan_refs()`(세션 `plan_ref` 상태 키, 세션 todo `plan_ref`(SQL에서 `session_id`로 필터), `src/data/boulder.json`에서 `plan_name`이 일치하고 `session_ids`에 해당 세션을 포함하는 work)에서 오고, `identity.resolve_plan_identity()`가 이름을 정규화된 계획 경로로 해석해 저장 키 `sha1(리포지토리 루트 상대 경로)[:12]`를 도출합니다. 서로 다른 파일의 같은 이름 계획은 물리적으로 격리되고, boulder `session_ids`로 공유된 하나의 계획 파일은 나열된 모든 세션에서 같은 `<plan_key>`로 해석됩니다. 비연결/모호한 계획에 대한 `read` / `write`는 진단 가능한 오류로 거부되고 `list`는 연결된 계획만(가독 이름 + key) 반환합니다; `build_knowledge_block(session_id)`는 프롬프트 블록을 위해 세션의 기본 아이덴티티를 해석합니다; `clear_session`은 세션 전용 아이덴티티 디렉터리를 제거하고 다른 세션과 공유된 계획은 유지합니다. | `agent/tools/todolist/knowledge/identity.py` |

**서브에이전트 경계.** 세 패밀리 모두 빌더(`build_taskflow_tools`, `build_todolist_tools`, `build_knowledge_tools`)가 `metadata["scope"] = "main_only"`를 부여합니다. `apply_tool_policy`(`agent/tools/subagent/spawn/inherited_tool_policy.py`)는 `main_only` 도구를 **가장 먼저 무조건** 제거합니다 — allow/deny 목록보다 앞서며 ORCHESTRATOR 해제로도 덮어쓸 수 없습니다 — 따라서 스폰된 자식 에이전트는 `taskflow_*`, `todowrite`/`todoread`, `knowledge` 도구를 절대 받지 않습니다. 같은 태그 패턴은 이미 `memory`, `skill_manage`, `sessions_kill`, `sessions_steer`를 포괄했습니다. 실제 도구 세트 단언은 `tests/agent/tools/taskflow/test_taskflow_tools.py`, `_build_child_agent` 경계는 `tests/agent/tools/subagent/test_max_tokens_boost_wiring.py`가 고정합니다.

**세션 간 거부.** `taskflow_create`에서 다른 세션이 사용 중인 `flow_id`와 충돌하면 존재만 알리고 리비전은 누출하지 않습니다; 다른 세션의 flow에 대한 변경은 알 수 없는 id와 같은 "not found" 텍스트를 반환합니다. 읽기/목록/업데이트/퍼지 경로는 `tests/agent/tools/taskflow/test_store_sqlite.py`, `test_taskflow_tools.py`, `test_dag_e2e.py`, `tests/server/DAO/test_clear_session.py`가 포괄합니다.


