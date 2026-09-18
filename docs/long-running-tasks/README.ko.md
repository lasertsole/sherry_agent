# ⏳ 장시간 실행 작업: TaskFlow, 예산, 데드라인, 메모리, 연속성

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 단일 턴을 넘어 살아남는 작업을 어떻게 수행하는가: 영속 SQLite DAG 엔진(`taskflow_*`, 13개 도구)이 의존 관계가 있는 단계를 대화 턴에 걸쳐 추적하고, 각 단계를 분리된 자식 서브에이전트로 디스패치하며, 옵트인 정책에 따라 실패/사망 단계를 자동 재디스패치하고, 단계 수용 기준을 오케스트레이터가 검증할 수 있도록 에코하며, 예산 대비 토큰/비용 지출을 집계하고, 백그라운드 sweeper가 기한 초과 또는 유휴 flow를 만료시키며, 전역 세션 간 flow 보드를 제공하고, 2계층 메모리 시스템, 압축 전 메모리 플러시, 요약↔TaskFlow 브리지, 도구 출력 한 줄 요약, 세션 간 연속성, 서브에이전트 완료 시 메모리 역류, 그리고 활성 flow를 시스템 프롬프트에 자동 재주입하는 것을 통해 컨텍스트를 앞으로 전달합니다.

사실상의 기준(source of truth): `agent/tools/taskflow/**`, `agent/tools/memory.py`, `agent/middlewares/summarization/memory_flush.py`, `agent/middlewares/summarization/core.py`(TaskFlow 컨텍스트 블록), `agent/middlewares/subagent_completion_drain/core.py`(메모리 역류), `agent/middlewares/task_intent/core.py`, `agent/middlewares/todo_continuation/core.py`, `context_engine/session_continuity.py`, `workspace/prompt_builder.py`, `pub/func/message/tool_output_prune.py`, `agent/tools/subagent/registry/sweeper.py`, `agent/wrapper/**`, `config/features/**`. 아래의 모든 상수, 시그니처, 줄 번호는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [TaskFlow DAG 엔진](#-taskflow-dag-엔진)
- [단계 재시도 정책](#-단계-재시도-정책)
- [토큰 / 비용 예산](#-토큰--비용-예산)
- [작업 데드라인](#-작업-데드라인)
- [결과 검증](#-결과-검증)
- [진행 보고서](#-진행-보고서)
- [유휴 감지](#-유휴-감지)
- [세션 간 보드](#-세션-간-보드)
- [압축 전 메모리 플러시](#-압축-전-메모리-플러시)
- [요약 ↔ TaskFlow 조정](#-요약--taskflow-조정)
- [서브에이전트 메모리 역류](#-서브에이전트-메모리-역류)
- [도구 출력 요약](#-도구-출력-요약)
- [세션 연속성](#-세션-연속성)
- [TaskFlow 자동 재개](#-taskflow-자동-재개)
- [동시성 레인](#-동시성-레인)
- [설정 레지스트리](#-설정-레지스트리)
- [아키텍처 다이어그램](#-아키텍처-다이어그램)
- [API 레퍼런스](#-api-레퍼런스)
- [테스트](#-테스트)
- [알려진 한계](#-알려진-한계)

## 🎯 개요

장시간 실행 작업 스택은 메인 에이전트가 멀티턴 작업을 **영속 flow**로 분해하고, 단계들이 서로 의존할 수 있게 하며, 준비된 각 단계를 자식 서브에이전트로 디스패치하고, 프로세스 재시작을 견디게 합니다. 여섯 개의 하위 시스템이 협력합니다:

| # | 하위 시스템 | 진입점 | 영속 위치 |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG 엔진** | `agent/tools/taskflow/` | `data/taskflow_registry.db`(SQLite, WAL) |
| 2 | **토큰 / 비용 예산** | `taskflow_budget`, `taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **데드라인** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **유휴 감지** | sweeper `_scan_stale_waiting_taskflows` | `wait_json`의 오래된 마커 |
| 5 | **압축 전 플러시** | `agent/middlewares/summarization/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 6 | **연속성 / 자동 재개** | `context_engine/session_continuity.py`, `workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + 프롬프트 블록 |

전체를 관통하는 설계 계약은 **오류를 텍스트로 반환**하는 것입니다: 도구는 비즈니스 오류를 모델에 던지지 않고 `Error:`로 시작하는 사람이 읽을 수 있는 문자열을 반환합니다. 모든 백그라운드 훅은 **페일오픈(fail-open)**입니다 — 레지스트리를 쓸 수 없거나 sweeper가 죽어도 "장시간 작업 컨텍스트 없음"으로 퇴화할 뿐, 턴을 깨뜨리지 않습니다.

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
    deadline_ts REAL
);
```

DAG 자체(`steps[]`, `results[]`, `depends_on`, `creator_session_key`)는 전부 `state_json` 안에 있습니다 — DAG 필드를 추가하는 데 스키마 마이그레이션이 필요 없습니다. 토큰/비용/데드라인 컬럼은 추가적 DDL(`_TOKEN_COLUMN_DDL`, `_DEADLINE_COLUMN_DDL`, `store_sqlite.py:83-129`)로 정의됩니다. WAL 프라그마는 프로세스당 한 번만 전환되며, 모든 문장 앞에 `PRAGMA busy_timeout = 5000`이 실행됩니다(`store_sqlite.py:234-271`).

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

`done`은 **"결과가 주입됨"**을 뜻하며 "자식이 성공함"이 아닙니다([알려진 한계](#-알려진-한계) 참조). `status` 필드가 없는 레거시 단계는 파생됩니다: `child_session_key`를 가진 단계는 `dispatched`, 그렇지 않으면 `ready`로 간주됩니다(`_shared.py:85`, `step_status`).

### `depends_on` 의미론

`deps_satisfied(step, steps)`(`_shared.py:99`)는 모든 `depends_on` id가 현재 단계 목록에 존재**하고** `done`일 때만 참입니다. `depends_on`이 없거나 비면 자명하게 충족됩니다. 알 수 없는 의존 id는 결코 충족되지 않으며, **자기 의존도 결코 충족되지 않습니다** — 따라서 자기 참조 단계는 언락 루프에 빠지지 않고 안전하게 블록된 상태로 남습니다. `unlock_dependents(steps)`(`_shared.py:137`)는 목록을 **단 한 번만 순회**하므로 의존 사이클이 루프를 도는 것을 구조적으로 막습니다. `taskflow_run_task`는 디스패치나 상태 변경 **전에** 알 수 없는 의존 id를 거부합니다.

### 도구 패밀리(13개 도구)

모든 도구는 `async`이며 `@tool("taskflow_…")`로 데코레이트되고, `build_taskflow_tools()`(`tools/__init__.py:46-58`)가 `metadata={"scope": "main_only"}`와 `handle_tool_error=True`를 부여합니다. 공유 flow 상태를 관리할 수 있는 것은 메인 에이전트뿐이며, 서브에이전트 도구 정책은 패밀리 전체를 무조건 제거합니다.

| 도구 | 목적 |
| :--- | :--- |
| `taskflow_create` | 리비전 1로 flow 생성; 선택적으로 `deadline_hours` 설정 |
| `taskflow_run_task` | 단계를 등록(선택적 `validation_criteria` / `retry_policy`)하고 디스패치(또는 `blocked`로 기록) |
| `taskflow_dispatch` | 여러 준비된 단계를 전부 아니면 전무로 일괄 디스패치 |
| `taskflow_wait_all` | flow 범위 유계 폴링으로 디스패치된 단계의 정착을 대기(정책 단계 자동 재시도) |
| `taskflow_resume` | 자식 결과를 멱등하게 주입하고 후속 단계를 언락하며 토큰을 집계(실패 인식 재시도 + 기준 에코) |
| `taskflow_set_waiting` | 이유와 함께 flow를 `waiting`으로 파킹 |
| `taskflow_summary` | 읽기 전용 재조회(충돌 후 재조회에도 사용) |
| `taskflow_progress` | 사람이 읽을 수 있는 진행/완료 보고서 |
| `taskflow_budget` | 토큰/비용 예산 조회 또는 설정 |
| `taskflow_list` | 모든 flow의 세션 간 보드(`active` / `all` / 상태 이름) |
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

### 대기 — `taskflow_wait_all`의 flow 범위

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")`(`taskflow_wait_all.py:110`)는 *이* flow의 `dispatched` 단계에 기록된 자식 세션**만** 폴링하므로, 무관한 백그라운드 자식이 반환을 막을 수 없습니다. 알 수 없거나 이미 정리된 run은 정착된 것으로 간주됩니다(절대 멈추지 않음). 폴링 간격은 `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]`(0.05초)로 클램프됩니다. 타임아웃 시 부분 보고서와 함께, 정착된 자식에 대해 `taskflow_resume`을 호출하고 `wait_all`을 다시 호출하라는 안내를 반환합니다. 요청자 세션의 모든 자식이 정착될 때만 발화하는 `sessions_yield`는 의도적으로 재사용하지 않습니다.

### 낙관적 잠금

모든 변경은 `UPDATE … WHERE flow_id = ? AND expected_revision = ?`를 거치며 리비전을 정확히 1만큼 올립니다(`store_sqlite.py:460-481`). 일치하는 행이 0이면 충돌입니다:

```python
# FlowConflictError 메시지(store_sqlite.py:173)
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

## 📋 세션 간 보드

`taskflow_summary`는 flow 하나를 읽고 자동 재개 판독기는 세션 범위입니다. `taskflow_list`는 의도적으로 그 반대 — 레지스트리 전체를 아우르는 **전역 보드**이므로, 한 채널/채팅에서 시작한 flow가 다른 어디에서나 보입니다:

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active") -> str
```

| `status_filter` | 행 |
| :--- | :--- |
| `"active"`(기본) | `running` + `waiting`만 |
| `"all"` | 종단 상태를 포함한 모든 flow |
| 그 밖의 값 | 상태 정확 일치(`running`, `waiting`, `done`, `failed`, `cancelled`) |

읽기 전용(`expected_revision` 불필요)이며 `store_sqlite.get_all_flows_sync(status_filter)`(`store_sqlite.py:566`)가 뒷받침합니다. 동기 판독기는 이벤트 루프가 필요 없는 stdlib `sqlite3` 경로를 사용하고, 행을 `expected_revision DESC`(가장 최근 활성 순)로 정렬하며 페일오픈입니다 — 초기화/읽기 실패는 `[]`를 반환합니다. `"active"`는 `get_active_flows_sync()`에 위임합니다.

렌더링되는 보드는 고정 열의 패딩된 텍스트 표이며, 설명은 40자, creator key는 16자로 제한됩니다:

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

스키마에는 **`updated_at` 컬럼이 없습니다**(마이그레이션 없음). 따라서 `_last_activity_ts()`(`taskflow_list.py:28`)는 "마지막 업데이트"를 flow 어딘가에 영속된 활동 스탬프의 최댓값으로 도출합니다 — `wait.set_at`, 모든 `step.dispatched_at`, 모든 `result.injected_at` — 이를 UTC 타임스탬프로 렌더링합니다(스탬프가 전혀 없는 flow는 `-`). 빈 레지스트리는 `No task flows found`를 반환합니다.

## 🧠 계층형 메모리

두 계층은 *어떻게* 모델에 도달하는지로 구분됩니다:

| 계층 | 저장소 | 위치 | 프롬프트 포함? |
| :--- | :--- | :--- | :--- |
| **L1 —— 정제 메모리** | `MEMORY.md`(에이전트 노트) + `USER.md`(사용자 프로필) | `workspace/memory/`(`MEMORY_DIR`) | 예——동결 스냅샷으로 항상 주입 |
| **L2 —— 원시 이력** | `mes_memory.db`(SQLite, WAL, FTS5) | `src/store/mes_memory/mes_memory.db` | 아니오——`context_engine` / `message_search`가 검색 |

파일은 **한 줄짜리 구분자 `§`로 나뉜 일반 텍스트 항목**입니다 — `ENTRY_DELIMITER = "\n§\n"`(`agent/tools/memory.py:53`). YAML frontmatter도, 불릿 접두사도 없습니다. 항목은 여러 줄일 수 있습니다.

계층 1은 `MemoryStore`가 관리합니다(`memory.py:104`): 파일당 문자 한도 `2200`(memory)과 `1375`(user), 주입 스캔(`_MEMORY_THREAT_PATTERNS`, `memory.py:68`)이 프롬프트 주입과 자격 증명 유출을 거부하고, 크로스 플랫폼 파일 잠금, 원자적 쓰기, 정확 일치 중복 제거를 갖춥니다. 라이브 항목은 즉시 변경되는 반면, 프롬프트는 `load_from_disk()`에서 캡처한 **동결 스냅샷**을 사용해 세션 동안 프리픽스 캐시를 안정적으로 유지합니다.

`memory` 도구는 `scope="main_only"`로 태그되어 서브에이전트는 절대 볼 수 없습니다.

## 🔥 압축 전 메모리 플러시

요약 미들웨어가 오래된 메시지를 버리기 전에, `agent/middlewares/summarization/memory_flush.py`는 값싼 모델에게 지속적 사실을 `MEMORY.md`에 저장할 마지막 기회를 줍니다. 트리거는 `should_flush(discarded_messages, estimated_tokens)`(`memory_flush.py:43`)입니다:

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

발화하면 `run_memory_flush`(비동기) / `run_memory_flush_sync`가 주입된 팩토리로 모델을 구성하고 단일 일반 텍스트 추출 프롬프트(`_FLUSH_PROMPT`, `memory_flush.py:19`)를 사용합니다. 출력은 `§`로 구분된 `Environment / Project / Decision / User / Tool` 사실 목록입니다. 빈 결과나 리터럴 `(none)`은 건너뜁니다. 추출 텍스트는 `MemoryStore.append_entries(new_entries)`(`memory.py:281`)로 넘어가며, 이는 `§`로 나누고, 각 후보를 주입 스캔하고, 기존 집합과 중복 제거하고, 덧붙이고, 2200자를 넘는 동안 가장 오래된 항목을 축출하고, 마지막으로 한 번의 원자적 쓰기를 수행합니다. `append_entries`는 항상 `MEMORY.md`를 대상으로 합니다. 모든 실패 경로는 `False`를 반환하고 삼켜집니다 — 플러시가 압축을 막을 수 없습니다.

⚠️ **배선 상태.** `Summarization.__init__`은 `memory_store` / `llm_factory`를 받으며(둘 다 기본 `None`, `summarization/core.py:623-624`), 둘 다 설정된 경우에만 `_apply_compression`(`summarization/core.py:1703`)과 `_aapply_compression`(`summarization/core.py:1791`) 안에서 플러시를 호출합니다. 현재 프로덕션 인스턴스 — 메인 에이전트 `agent/core.py:170`과 서브에이전트 `agent/tools/subagent/spawn/core.py:784` — 는 이들을 전달하지 **않습니다**. 따라서 플러시는 구현·테스트되었지만 호출 지점이 저장소와 `factory(model=…, max_tokens=…, timeout=…)` 형태의 팩토리를 제공할 때까지 잠재 상태에 머뭅니다.

## 🔗 요약 ↔ TaskFlow 조정

압축이 LLM 프롬프트를 구성할 때, `_get_taskflow_context_sync(session_id)`(`agent/middlewares/summarization/core.py:262`)가 이 세션의 활성 flow를 렌더링하여 요약 프롬프트의 **마지막** 부분으로 덧붙입니다(`_build_summary_prompt`, `summarization/core.py:1431-1434`):

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

이 블록은 `## Current TaskFlow State (authoritative)`를 제목으로 하며(`summarization/core.py:286`), 세션이 소유한 최대 3개 flow(`requester_session_key(session_id)`로 매칭)에 대해 flow id/상태, 설명, `done/total` 진행과 상태 내역, 마지막 두 완료 단계, 처음 두 대기 단계, 대기 이유를 나열합니다. DAG 헬퍼 `step_status`와 `steps_summary`를 재사용하며 완전히 페일오픈입니다(`except Exception → ""`). 결정론적 폴백 요약(`_build_static_fallback_summary`)은 이 블록을 **포함하지 않습니다** — LLM 프롬프트 전용 추가입니다.

## 🧠 서브에이전트 메모리 역류

`SubagentCompletionDrainMiddleware`(`agent/middlewares/subagent_completion_drain/core.py`)는 큐에 쌓인 서브에이전트 완료 메시지의 부모 턴 수용 지점입니다: `before_model`에서 세션의 `SteeringQueue`를 재수화하고 배출한 뒤, 재구성된 완료 캐리어 메시지를 주입합니다. **배출이 비어 있지 않으면** 공유 메모리를 부모의 인메모리 뷰와 조정합니다:

```python
# subagent_completion_drain/core.py:68-93
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_disk()
    for target in ("memory", "user"):
        memory_store.save_to_disk(target)
```

부모와 자식은 **하나의 프로세스 전역 `MemoryStore`** 를 공유하므로 자식의 쓰기는 이미 파일 수준에서 보입니다. 표류할 수 있는 것은 부모의 인메모리 뷰 — 라이브 항목과 시스템 프롬프트 구성에 쓴 **동결 스냅샷** — 이며, 이는 프로세스 밖 작성자가 `MEMORY.md` / `USER.md`를 갱신했을 때 일어납니다. **먼저 재로드**하는 순서가 핵심입니다: 오래된 인메모리 목록을 재로드 전에 영속화하면 동시 작성자를 덮어쓰므로, 조정은 대상마다 load → persist여야 합니다.

배출과 마찬가지로 역류도 **페일오픈**입니다 — 메모리 I/O 실패는 로그로 남기고 삼키며, 완료 캐리어는 부모 턴에 도달합니다. 또한 배출은 내부 완료 캐리어에 Sisyphus 검증 리마인더를 덧붙여, 완료가 검증된 결과가 아니라 `DoneClaim`임을 부모에게 상기시킵니다(todo를 완료로 표시하기 전에 `todoread`로 검증하고, 수용 기준에 비추고, 오래된 상태를 조사하십시오).

## ✂️ 도구 출력 요약

비-LLM 프루닝에서 크기가 큰 오래된 `ToolMessage` 내용은 보통 마커로 정리됩니다. `pub/func/message/tool_output_prune.py`는 맨 마커 `_PRUNE_MARKER = "[Old tool result content cleared]"`(`tool_output_prune.py:22`)를 **도구별 한 줄 요약**으로 대체하여, 결과에 무엇이 있었는지에 대한 단서를 모델이 유지하게 합니다:

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`(`tool_output_prune.py:103`)는 최신→오래된 순으로 메시지를 순회하고, 첫 요약 메시지에서 멈추며, 최신 `prune_protect_tokens`(40 000)를 보호하고, 보호 대상 도구(`{"memory", "skill_view", "skill_list"}`)를 건너뛰며, 해제된 토큰이 `prune_min_reduction_tokens`(5 000)에 도달할 때만 반영합니다. 교체된 메시지는 `additional_kwargs["status"] = "compacted"`와 `["original_length"]`를 지닌 `model_copy` 복제본입니다. 요약은 200자로 제한되며, 템플릿 예외는 마커로 폴백합니다. 호출자는 `Summarization._run_non_llm_strategies`입니다(`summarization/core.py:1538`).

## 🔄 세션 연속성

세션이 정리될 때, `context_engine/session_continuity.py`가 종료 상태를 영속화하여 다음 세션이 연속성을 제시할 수 있게 합니다. `server/DAO/messages.py::clear_session`은 삭제 전에 `auto_save_on_session_end(session_id)`를 **0단계**로 호출합니다(`server/DAO/messages.py:27-33`). 이 함수는:

1. `runtime.session.relation_register`를 통해 `channel_id`/`chat_id`를 해석합니다(`_get_channel_chat_for_session`, `session_continuity.py:167`).
2. 최근 3턴을 읽고 마지막 AI 응답을 `_MAX_SUMMARY_CHARS = 500`으로 자릅니다(`session_continuity.py:28`).
3. 해당 세션의 활성 flow id를 수집합니다.
4. `save_session_end_state(...)`를 `src/data/session_continuity/{safe-key}.json`에 씁니다(`session_continuity.py:25`). 필드는 `last_session_id`, `ended_at`, `ended_ts`, `summary`, `taskflow_ids`입니다.

다음 세션은 `build_continuity_prompt(session_id)`(`session_continuity.py:80`)로 이를 읽습니다. 이는 `workspace/prompt_builder.py:169`의 `_build_continuity_block`에서 호출되며 전체 프롬프트를 구성할 때 주입됩니다(`prompt_builder.py:289-295`):

```
## Last Session (continuity)
Last conversation ended with: <요약 ≤ 500자>
Related tasks: <최대 3개 flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

세션은 자기 자신의 상태를 받지 않습니다(`last_session_id == session_id → ""`). 조회에 **channel id와 chat id가 모두** 필요하므로, 채널 바인딩이 없는 순수 WebSocket 세션은 연속성 블록을 받지 못합니다. 저장소는 데이터베이스가 아니라 파일 시스템의 JSON(`channel:chat` 키, 퇴화 시 `session_id`)입니다.

## ♻️ TaskFlow 자동 재개

활성 flow는 시스템 프롬프트로 다시 떠올라 새 세션이 미완료 작업을 이어받을 수 있게 합니다. 세 개의 독립적 판독기가 같은 레시피를 씁니다 — `requester_session_key(session_id)` + `get_active_flows_sync()` + `state["creator_session_key"]` 필터:

| 판독기 | 위치 | 목적 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 시스템 프롬프트의 `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:262` | 압축 요약 프롬프트의 TaskFlow 블록 |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:186` | 영속 연속성 상태의 `taskflow_ids` |

`creator_session_key`는 flow 생성 시 `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"`로 찍힙니다(`taskflow_create.py:38`, `_shared.py:21`). `get_active_flows_sync()`(`store_sqlite.py:538`)는 `running`과 `waiting` flow만 리비전 순으로 반환하며, 이벤트 루프가 필요 없는 stdlib `sqlite3` 경로를 사용합니다. 실패 시 `[]`를 반환합니다.

시스템 프롬프트 블록(`prompt_builder.py:140`)은 다음과 같습니다:

```
## Pending TaskFlows
- [running] flow-1: "<설명>" | 2/5 steps done | next: step-3 "<작업>"
Use taskflow_summary to inspect a flow and continue execution.
```

최대 3개 flow로 제한되며, 파일 필터로 프롬프트를 구성할 때(`selected_file_names is not None`) 억제됩니다. 모든 읽기는 페일오픈입니다.

## 🚦 동시성 레인

장시간 실행되는 작업은 무한정 팬아웃하지 않습니다. 분리된 모든 자식 에이전트, 모든 백그라운드 nudge, 모든 중첩 `sessions.send` 응답 턴은 먼저 **프로세스 수준 레인**을 통과합니다. 레인은 `asyncio.Semaphore`와 `active`/`queued` 카운터(`runtime/lane/core.py`)이며, 한도 초과 작업은 거부되지 않고 **FIFO로 대기**합니다.

| 레인 | 제약 대상 | 기본 동시성 | 설정 키 |
| :--- | :--- | :--- | :--- |
| `MAIN` | 메인 에이전트 턴(`server/service/input_queue_service.py::_run_executor`) | `min(16, max(8, CPU))`, `SUBAGENT + NUDGE` 이상으로 상향 클램프 → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | 자식 에이전트 실행(`spawn/core.py`, `control/steer.py`) | `8` | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | 메모리 nudge / 계획 추출 / 압축 후 todo 갱신(`agent/middlewares/summarization/nudges.py` 3곳) | `4` | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` 응답 턴(직렬) | `1` | `LANE_SYSTEM["nested_max_concurrent"]` |

### 설정과 검증

레인 한도는 `config/features/infra_side/lane_system.py`에 있습니다:

```python
LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),   # 12–16
    "subagent_max_concurrent": 8,
    "nudge_max_concurrent": 4,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}
```

`_resolve_main_concurrency()`는 OpenClaw의 CPU 스케일링——`min(16, max(8, CPU))`——을 따르고, 하드 불변식 `SUBAGENT + NUDGE`(8 + 4 = 12) 이상으로 **상향** 클램프하므로 출하 기본값은 어떤 CPU 수에서도 유효합니다. `validate_lane_config()`는 서버 시작 시 `install_lane_lifecycle()`(`server/service/lane_lifecycle.py`)이 한 번만 호출하며(import 시에는 절대 호출하지 않음), `main_max_concurrent < subagent + nudge`이거나 임의 레인 한도가 `< 1`이면 예외를 던집니다. `lane_wait_warn_ms`는 "슬롯을 너무 오래 기다림" 경고를 제어하고, `lane_drain_timeout_seconds`는 `LaneManager.drain_all()`의 기본 타임아웃입니다. `LaneManager.set_concurrency(lane, n)`은 한도를 핫 업데이트합니다——실행 중 슬롯은 permit을 유지하고 새 acquire만 새 한도를 봅니다.

### 대기 의미론: `PENDING`

전역 동시성은 거부 카운터가 아닙니다. `spawn_subagent_direct`는 부모별 어드미션(`validate_spawn_depth`, `validate_concurrent_children`)을 그대로 적용하지만, 전역 한도를 초과한 spawn은 **수락**되어 `ExecutionStatus.PENDING`으로 등록됩니다: `started_at`은 `None`으로 남고 해당 run은 레인 슬롯을 보유하지 않습니다. SUBAGENT 레인 래퍼(`_execute_subagent_with_lane`, `agent/tools/subagent/spawn/core.py`)가 슬롯을 기다린 뒤 `mark_run_running()`으로 `PENDING → RUNNING` 승격하며, 그 시점에만 `started_at`을 찍습니다——대기 시간은 실행 시간으로 계산되지 않습니다. `validate_global_concurrent()`와 `SubagentConfig.max_concurrent`는 하위 호환용으로만 남고 spawn 파이프라인은 호출하지 않습니다.

대기 중인 자식도 어드미션 슬롯을 차지하므로, registry 계수 함수는 `RUNNING + PENDING`을 활성으로 세고(`count_active_runs_for_session`, `count_active_descendant_runs`, `count_all_active_runs`), `is_live_unended_run()`은 `PENDING`을 포함합니다.

### PENDING 수명 주기

| 경로 | 동작 |
| :--- | :--- |
| **Kill** | PENDING run은 목록화/kill 가능(`list_killable_children`); `cancel_task()`가 레인 대기자를 취소하고 `CancelledError`는 permit을 소비하거나 누수하지 않고 `Lane.acquire()`를 빠져나갑니다 |
| **Steer** | 거부——`steer_subagent_run()`은 RUNNING/INTERRUPTED만 받습니다; steer로 재시작된 run은 PENDING으로 레인에 다시 들어가 자기 슬롯 안에서 승격됩니다 |
| **Sweeper / 고아 복구** | `is_live_unended_run()`이 PENDING을 포함하므로 task를 잃은 PENDING run은 고아입니다; `evaluate_recovery_gate()`는 `"wedged"`로 분류하고 `_recovery_loop()`는 `ended_reason="pending_orphaned"`(outcome `TIMEOUT`, error `"pending orphaned"`)로 곧바로 `TERMINAL` 처리한 뒤 announce 흐름을 실행합니다 |
| **Yield** | `sessions_yield`는 PENDING 자식을 활성으로 세고, `wake_yield_if_all_children_settled`는 전부 끝나야 부모를 깨웁니다; yield 타임아웃은 레인 대기를 포함하고 만료 시 정상 반환합니다 |
| **계수 / 목록** | `control/list.py`와 `runtime_tools.py`는 PENDING 자식을 RUNNING/INTERRUPTED와 함께 표시합니다 |

PENDING run의 레인 task가 아직 존재하는 동안에는 sweeper 스캔이 건너뜁니다(프로세스가 그저 대기 중일 뿐); task 상실(프로세스 재시작)만이 고아로 만듭니다.

### Drain 모드와 종료

`Lane.acquire()`는 대기 전에 drain 검사 콜백을 조회합니다: subagent gateway가 draining을 보고하는 동안 획득자는 `RuntimeError`로 거부되며 **permit을 소비하지 않습니다**. 콜백은 시작 시 `set_drain_check(is_gateway_draining)`(`install_lane_lifecycle`)으로 주입되어 `runtime/lane`을 상향 import에서 자유롭게 합니다.

종료 시 같은 seam이 drain 모드를 전환하고(`set_draining(True)`) `atexit`을 통한 **유계** drain을 수행합니다——`asyncio.run(drain_all_lanes(timeout=0))`은 최종 레인별 카운터를 보고하고 프로세스 종료를 절대 지연시키지 않습니다. 저장소에는 비동기 셧다운 경로가 없으므로(Robyn의 `shutdown_handler`는 SIGINT/SIGTERM에서 호출되지 않음) 실행 중 턴은 기다리지 않고 OS에 맡겨집니다.

### 관측성

`GET /lane-status`(`server/trigger/http/lane.py`)는 네 레인 모두의 실시간 스냅샷을 반환합니다:

```json
{"main": {"name": "main", "max_concurrent": 12, "active": 1, "queued": 0},
 "subagent": {"name": "subagent", "max_concurrent": 8, "active": 3, "queued": 2},
 "nudge": {"name": "nudge", "max_concurrent": 4, "active": 0, "queued": 0},
 "nested": {"name": "nested", "max_concurrent": 1, "active": 0, "queued": 0}}
```

### 교착 방지

| 시나리오 | 메커니즘 |
| :--- | :--- |
| 메인 턴이 MAIN 슬롯을 쥔 채 자식 결과를 기다림 | 자식은 SUBAGENT 슬롯만 필요합니다——레인은 독립 세마포어이므로 레인 간 대기 사이클이 없습니다(`test_filling_main_does_not_block_subagent`; spawn 경로: `test_subagent_runs_while_main_lane_slot_is_held`); 선택적 `run_timeout_seconds > 0`이 멈춘 자식을 추가로 끊습니다 |
| Nudge가 SUBAGENT 슬롯을 기다림 | nudge는 서브에이전트를 spawn하지 않고(도구 집합 제한), NUDGE 레인 자체도 독립입니다 |
| 실행 중 레인 핫 업데이트 | `set_concurrency`는 새 acquire에만 영향을 줍니다 |
| `sessions_yield`가 PENDING 자식을 기다림 | yield 타임아웃은 레인 대기를 포함하고 만료 시 정상 반환합니다 |
| Drain 모드 + 레인 대기자 | `acquire()`는 drain 검사로 거부하고 permit을 소비하지 않습니다 |
| PENDING run kill | `CancelledError`는 permit을 해제하지 않고 `Lane.acquire()`를 빠져나갑니다 |
| PENDING run steer | 상태 검사로 거부; RUNNING/INTERRUPTED만 steer 가능합니다 |

### 레인 파일 맵

| 파일 | 역할 |
| :--- | :--- |
| `config/features/infra_side/lane_system.py` | `LaneSystemConfig` / `LANE_SYSTEM` + `validate_lane_config()` |
| `runtime/lane/core.py` | `LaneType`, `Lane`, `LaneManager`, `get_lane_manager()`, `lane_slot()`, `set_drain_check()` |
| `server/service/lane_lifecycle.py` | 시작 검증, drain 게이트 등록, 유계 종료 drain |
| `server/trigger/http/lane.py` | `GET /lane-status` |
| `agent/tools/subagent/spawn/core.py` · `control/steer.py` | SUBAGENT 레인 래퍼 + PENDING → RUNNING 승격 |
| `agent/middlewares/summarization/nudges.py` | NUDGE 레인 3개 호출 지점 |
| `agent/tools/subagent/tools/sessions_send.py` | 응답 턴을 감싸는 NESTED 레인 |
| `server/service/input_queue_service.py` | `_run_executor`를 감싸는 MAIN 레인 |
| `agent/tools/subagent/orphan/recovery.py` | PENDING 고아의 `pending_orphaned` 확정 |
| `tests/runtime/lane/` · `tests/server/service/test_main_lane.py` · `tests/agent/tools/subagent/test_{spawn_lane_integration,kill_pending,steer_lane,sweeper_pending,sessions_yield_pending}.py` · `tests/server/trigger/http/test_lane_api.py` | 레인 테스트 스위트 |

### 구현 트레이드오프

구현 시 기록된 의도적인 트레이드오프 세 가지:

- **MAIN은 불변식까지 상향 클램프.** CPU 스케일링만으로는(`min(16, max(8, CPU))`) CPU ≤ 8 머신에서 8이 되어 시작 시 `validate_lane_config()`가 실패하므로, 기본값을 `SUBAGENT + NUDGE`(12)까지 올려 4/8/12/16/64코어 머신 모두에서 유효합니다.
- **`GET /lane-status`에는 `/api` 접두사가 없습니다.** 최초 설계의 `/api/lane-status` 라우트를 버리고 저장소의 기존 라우팅 관례를 따랐습니다: 핸들러는 `server/trigger/http/lane.py`에 있으며 `/channels`, `/cron`과 나란히 있습니다.
- **종료 drain은 `atexit` 경로.** 저장소에 비동기 셧다운 seam이 없으므로(Robyn의 `shutdown_handler`는 SIGINT/SIGTERM에서 호출되지 않음), 유계 `drain_all(timeout=0)`은 최종 레인별 카운터만 보고하고 종료를 지연시키지 않습니다. 실행 중 턴은 OS에 맡겨집니다.

## ⚙️ 설정 레지스트리

모든 조정 값은 `config/features/` 아래에 있으며, 이는 **객체별 `TypedDict` 패키지**입니다 — 단일 거대 모듈이 아닙니다. 세 부분으로 나뉩니다:

| 부분 | 내용 |
| :--- | :--- |
| `config/features/agent_side/` | **19**개 에이전트 측 설정 모듈(미들웨어, 도구, LLM 클라이언트, 메모리, TaskFlow) |
| `config/features/infra_side/` | **19**개 인프라 측 설정 모듈(서버, 큐, 스킬, 컨텍스트 엔진, 런타임, 모델 가격) |
| `config/features/_env.py` | 유일한 공유 환경 헬퍼 |

각 모듈은 `class XxxConfig(TypedDict)`와 모듈 수준 상수 `XXX: XxxConfig = {…}`를 정의합니다. 환경 인식 모듈은 빌더 `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig`를 정의하고 `env or os.environ`을 읽어 임포트 시 상수를 구체화합니다. 환경 헬퍼는 `_env_int(name, default, env)`(`config/features/_env.py:9`)이며, `1/true/yes/on`과 `0/false/no/off/""`를 받아들이고 예외를 던지지 않습니다.

레지스트리는 현재 **39개 feature 객체**를 보유합니다 — 에이전트 측 20 + 인프라 측 19 — 각 패키지 `__init__.py`를 통해 재수출되고 `config/features/__init__.py`가 집계하므로, 소비자는 절반 또는 전체 레지스트리를 한 곳에서 임포트할 수 있습니다. 소비 코드는 상수를 임포트해 직접 인덱싱합니다(예: `ITERATION_BUDGET["default_max_iterations"]`). `get_feature`/`load_feature` 접근자는 없습니다. `config/__init__.py:38-39`는 `GATEWAY`에서 `API_HOST`/`API_PORT`를 파생합니다.

이 문서와 가장 관련 있는 상수:

| 설정 객체 | 필드 | 값 |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA`(`agent_side/taskflow_infra.py`) | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING`(`infra_side/model_pricing.py`) | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `MEMORY_FLUSH`(`agent_side/memory_flush.py`) | `enabled` | `MEMORY_FLUSH_ENABLED`(기본 1) |
| | `model` | `MEMORY_FLUSH_MODEL`(기본 "") |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION`(`agent_side/summarization.py`) | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY`(`infra_side/mes_memory.py`) | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ 아키텍처 다이어그램

```
                    ┌────────────────────────────────────────────────────────┐
                    │              agent/wrapper/ registry                   │
                    │  apply_graph_wrappers() → innermost-first chain:       │
                    │  RepetitionGuardWrapper → ContextLimitGuardWrapper     │
                    └───────────────────────────┬────────────────────────────┘
                                                │ wraps the compiled graph
                          ┌─────────────────────▼────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (13, main_only)   │          │  add/replace/remove  │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │  agent/tools/        │                 │ ─ Pending TaskFlows    │
│ state_json DAG    │          │  memory.py           │                 │ ─ Last Session         │
│ + retry/validation│          │                      │                 │                        │
└────────┬──────────┘          └──────────────────────┘                 └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
         │                                                │                        │
         │ drain                                          │                        │
         ▼                                                │                        │
┌──────────────────────────────┐                          │                        │
│ SubagentCompletionDrain      │                          │                        │
│ _backflow_shared_memory      │                          │                        │
└──────────────────────────────┘                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+TaskFlow)       │   │
└──────────────────────────────┘                   │                           │   │
                                                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

컴파일된 그래프는 **`agent/wrapper/`** 패키지가 래핑하며, 이 패키지가 가드를 소유합니다. `agent.wrapper.registry`는 프로세스 전역의, 순서가 있고 플러그 가능한 체인(`register_graph_wrapper`, `unregister_graph_wrapper`, `apply_graph_wrappers`, `reset_graph_wrappers`)을 노출하며, `GraphWrapperFactory` 항목은 **최내곽 우선**으로 적용됩니다. 기본 항목은 `RepetitionGuardWrapper(phantom_stream_guard=True)`, 다음 `ContextLimitGuardWrapper(context_window=main_llm_max_tokens)`입니다. 스트림 반복 가드는 `agent/wrapper/repetition_guard.py`, 컨텍스트 윈도 가드는 `agent/wrapper/context_limit.py`에 있습니다. **메모리 역류**는 `agent/middlewares/subagent_completion_drain/core.py`의 `SubagentCompletionDrainMiddleware`가 수행합니다.

## 📚 API 레퍼런스

### TaskFlow 도구

| 도구 | 시그니처 | 반환 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 생성된 id/상태/리비전(+ 데드라인) |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, validation_criteria=None, retry_policy=None, session_id)` | 디스패치된 단계, 또는 미충족 의존이 있는 `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | 디스패치된 step id + 리비전 |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | 단계별 정착 보고서(완전 또는 부분; 정책 단계 자동 재시도) |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None, validation_criteria=None, session_id)` | 재개 상태, 언락된 단계, 단계 상태 카운트, 기준 에코, 재시도 노트 |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None)` | waiting 상태 + 리비전 |
| `taskflow_summary` | `(flow_id)` | 대기/데드라인 상태를 포함한 전체 flow 상태 |
| `taskflow_progress` | `(flow_id)` | 완료율, 내역, 다음 단계, 예상 남은 시간 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None)` | 예산 보고서, 또는 설정 확인 |
| `taskflow_list` | `(status_filter="active")` | 전역 세션 간 보드(`active` / `all` / 상태 이름) |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None)` | 종단 `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None)` | 종단 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None)` | 종단 `cancelled` |

### memory 도구 액션

| 액션 | 시그니처 | 반환 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 성공/오류 |

### 주요 함수와 상수

| 심볼 | 위치 | 역할 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | 라이프사이클 / DAG 열거형 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py:402` | 낙관적 잠금 변경 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:538` | 세션 간 활성 flow 읽기 |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py:489,505` | sweeper 쿼리 |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:99,137` | DAG 전환 |
| `update_flow_with_conflict_retry` | `_shared.py:191` | 생성된 자식을 잃지 않는 영속화 |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | 데드라인 집행 |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | 유휴 감지 마커 |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:262` | 요약 조정 |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 자동 재개 프롬프트 블록 |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:103` | 도구 출력 한 줄 요약 |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | 연속성 저장 훅 |
| `should_flush` / `run_memory_flush` | `agent/middlewares/summarization/memory_flush.py:43,65` | 압축 전 플러시 |
| `append_entries` | `agent/tools/memory.py:281` | MEMORY.md 일괄 추가 |
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:566` | 세션 간 보드 읽기 |
| `classify_failure` / `should_retry_failure` | `agent/tools/taskflow/tools/_retry.py:56,103` | 실패 분류 |
| `plan_settled_retries` / `persist_retry_actions` | `agent/tools/taskflow/tools/_retry.py:199,254` | wait_all 재시도 계획/영속화 |
| `_backflow_shared_memory` | `agent/middlewares/subagent_completion_drain/core.py:68` | 메모리 역류 조정 |
| `apply_graph_wrappers` | `agent/wrapper/registry.py:69` | 플러그 가능한 그래프 래퍼 체인 |

## 🧪 테스트

TaskFlow 스위트는 `tests/agent/tools/taskflow/`에 있습니다(17개 `unit` 테스트 파일 + 공유 `conftest.py`):

| 테스트 파일 | 커버 내용 |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD, 리비전 증가, 낙관적 동시성 충돌, WAL, 동기 접근자, active/waiting/terminal 필터 |
| `test_step_graph.py` | `deps_satisfied`, `mark_step_done`, `unlock_dependents`, 레거시 상태 파생, 자기 의존 가드 |
| `test_summary_dag.py` | `taskflow_summary`의 DAG 렌더링 |
| `test_resume_dag.py` | 재개로 done 표시, 후속 언락, 부분 완료, 멱등 무동작 |
| `test_taskflow_tools.py` | 재시작을 넘는 create→run→resume→finish, 충돌, 종단 전환 |
| `test_taskflow_dispatch.py` | 일괄 디스패치, 전부 아니면 전무 검증, 배치 중간 실패 영속화 |
| `test_dag_e2e.py` | 프로세스 재시작을 넘는 완전 병렬 DAG flow |
| `test_conflict_persistence.py` | 충돌 후 생성된 자식 영속화, 재시도 소진 |
| `test_taskflow_wait_all.py` | flow 범위 대기, 타임아웃 부분 보고서, 무관한 살아있는 자식 |
| `test_run_task_dag.py` | 블록 등록, 충족 후 디스패치, 알 수 없는 의존 오류 |
| `test_taskflow_progress.py` | 완료율/내역/다음 단계/예상 남은 시간/waiting |
| `test_token_budget.py` | 토큰 집계, 비용 계산, 예산 조회/설정/경고/초과 |
| `test_deadline.py` | `deadline_hours`, 요약 렌더링, sweeper 만료 |
| `test_idle_detection.py` | active/stale 대기 상태, sweeper 마커, 살아있는 자식 건너뛰기 |
| `test_retry_policy.py` | 정책 검증, 실패 분류, 재디스패치, 소진 |
| `test_validation.py` | 기준 저장, 재개 에코, 덮어쓰기 |
| `test_taskflow_list.py` | 보드 렌더링, 상태 필터, 마지막 활동 타임스탬프 |

교차 스위트: `tests/agent/middlewares/test_memory_flush.py`(플러시 임계값과 `append_entries`), `tests/agent/middlewares/test_lt5_memory_backflow.py`(완료 배출 시 메모리 조정), `tests/agent/middlewares/test_subagent_completion_drain_reminder.py`(완료 캐리어 검증 리마인더), `tests/context_engine/test_session_continuity.py`(연속성 저장/프롬프트), `tests/agent/middlewares/test_todo_continuation.py`(턴 종료 연속), `tests/pub/func/message/test_tool_output_prune.py`(한 줄 요약), `tests/workspace/test_prompt_builder_taskflow.py`(보류 flow 프롬프트 주입).

표준 uv/pytest 도구로 이 영역만 실행:

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py -q
```

전체 프로세스 격리 스위트에는 `uv run python tests/run_tests_split.py`를 사용합니다(Group A는 `unit` 파일, Group B는 `module`/`integration` 파일 실행).

## ⚠️ 알려진 한계

- **`done`은 성공이 아닙니다.** 단계의 `done`은 "결과가 주입됨"만을 뜻하며, `failed`/`skipped` 단계 상태는 없습니다. 자식이 오류를 보고해도 `taskflow_resume`는 단계를 `done`으로 표시하고 후속을 언락합니다. 실패 인식 단계 전환은 의도적으로 미뤄져 있습니다.
- **`taskflow_wait_all`은 설계상 flow 범위입니다.** 주어진 flow의 디스패치된 단계에 기록된 자식만 기다리며, 알 수 없거나 이미 정리된 run은 정착으로 간주합니다. "모든 활성 flow 대기" 전역 프리미티브는 없습니다.
- **유휴 감지는 권고용입니다.** sweeper는 `stale_detected_at` / `stale_child_session_key`를 `wait_json`에 찍지만 오래된 `waiting` flow를 자동 실패시키지 않습니다. 사람이나 모델이 마커에 따라 조치해야 합니다.
- **압축 전 메모리 플러시는 잠재 상태입니다.** `Summarization`의 프로덕션 인스턴스(메인/서브)는 `memory_store` / `llm_factory`를 전달하지 않아, 호출 지점이 배선할 때까지 플러시가 실행되지 않습니다. 코드는 구현·테스트되었지만 현재 비활성입니다.
- **연속성은 채널 의존입니다.** `build_continuity_prompt`는 channel id와 chat id를 모두 요구하므로, 채널 바인딩이 없는 세션은 연속성 블록을 받지 못합니다. 저장소는 디스크의 키별 JSON이며 데이터베이스가 아닙니다.
- **활성 flow 스캔이 3중 중복입니다.** `prompt_builder._build_taskflow_block`, `summarization._get_taskflow_context_sync`, `session_continuity._get_active_taskflow_ids_sync`가 같은 쿼리를 독립적으로 구현합니다. 동기화를 유지해야 합니다.
- **레지스트리 규모는 39입니다.** 설정 레지스트리는 39개 feature 객체(에이전트 측 20 + 인프라 측 19)를 보유합니다. 인프라 측 계약 테스트는 그중 18개(GATEWAY + 17개 데이터 기반 케이스)를 커버하고 `MODEL_PRICING`을 빠뜨립니다.
- **패키지 재수출 누락.** `agent/tools/taskflow/__init__.py`는 11개 이름만 재수출합니다. `taskflow_dispatch`와 `taskflow_wait_all`은 `build_taskflow_tools()`로 도달할 수 있지만 패키지 `__all__`에서 빠져 있습니다.
- **TaskFlow 블록은 LLM 프롬프트 전용입니다.** LLM 실패 시 사용하는 결정론적 폴백 요약에는 `## Current TaskFlow State`가 없습니다.
- **토큰 회계는 호출자 제공입니다.** 비용은 `taskflow_resume`가 `token_usage` 딕셔너리를 받을 때만 계산됩니다. 없이 주입된 단계는 토큰 0, 비용 0에 기여합니다.
- **결과 검증은 권고용입니다.** `validation_criteria`는 저장되고 결과와 함께 에코되지만 도구가 강제하지 않습니다. 합격/불합격은 오케스트레이터가 스스로 판단해야 합니다. 기준 미충족으로 단계를 실패시킬 수 있는 자동 게이트는 없습니다.
- **재시도 분류는 텍스트 기반입니다.** `classify_failure`는 결과 텍스트에 대한 부분 문자열 휴리스틱입니다: 패턴 표 밖의 표현으로 된 실패(또는 부정 표현에 가려진 실제 실패)는 재시도를 유발하지 않으며, 빈 `retry_on`은 분류된 모든 실패를 재시도합니다. `taskflow_wait_all`은 결과 텍스트가 없는 죽은 자식을 분류할 수 없으므로, 예산이 남아 있는 한 항상 재시도 예산을 소비합니다.
- **`taskflow_list`는 의도적으로 전역입니다.** 세션 간 보드는 `creator_session_key` 범위를 무시하므로, 어떤 메인 에이전트 세션이든 레지스트리의 모든 flow를 열거할 수 있습니다(읽기 전용, `expected_revision` 없음). 세션별 뷰가 아닙니다.
