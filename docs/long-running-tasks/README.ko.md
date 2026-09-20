# ⏳ 장시간 실행 작업: TaskFlow, 예산, 데드라인, 메모리, 연속성

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 단일 턴을 넘어 살아남는 작업을 어떻게 수행하는가: 영속 SQLite DAG 엔진(`taskflow_*`, 14개 도구)이 의존 관계가 있는 단계를 대화 턴에 걸쳐 추적하고, 각 단계를 분리된 자식 서브에이전트로 디스패치하며, 옵트인 정책에 따라 실패/사망 단계를 자동 재디스패치하고, 단계 수용 기준을 오케스트레이터가 검증할 수 있도록 에코하며, 예산 대비 토큰/비용 지출을 집계하고, 백그라운드 sweeper가 기한 초과 또는 유휴 flow를 만료시키며, 세션별로 격리된 flow 보드를 제공하고(모든 읽기가 SQL 계층에서 소유 세션을 필터링하며, 자식 에이전트는 taskflow/todolist/knowledge 도구를 받지 않습니다), 2계층 메모리 시스템, 압축 전 메모리 플러시, 요약↔TaskFlow 브리지, 도구 출력 한 줄 요약, 세션 간 연속성, 서브에이전트 완료 시 메모리 역류, 그리고 활성 flow를 시스템 프롬프트에 자동 재주입하는 것을 통해 컨텍스트를 앞으로 전달합니다.

사실상의 기준(source of truth): `agent/tools/taskflow/**`, `agent/tools/memory.py`, `agent/middlewares/summarization/memory_flush.py`, `agent/middlewares/summarization/core.py`(TaskFlow 컨텍스트 블록), `agent/middlewares/subagent_completion_drain/core.py`(메모리 역류), `agent/middlewares/task_intent/core.py`, `agent/middlewares/todo_continuation/core.py`, `context_engine/session_continuity.py`, `workspace/prompt_builder.py`, `pub/func/message/tool_output_prune.py`, `agent/tools/subagent/registry/sweeper.py`, `agent/wrapper/**`, `config/features/**`. 아래의 모든 상수, 시그니처, 줄 번호는 해당 코드와 대조하여 검증했습니다.

## 목차

- [개요](#-개요)
- [TaskFlow 엔진](engine/README.ko.md)
  - [TaskFlow DAG 엔진](engine/README.ko.md#-taskflow-dag-엔진)
  - [단계 재시도 정책](engine/README.ko.md#-단계-재시도-정책)
  - [토큰 / 비용 예산](engine/README.ko.md#-토큰--비용-예산)
  - [작업 데드라인](engine/README.ko.md#-작업-데드라인)
  - [결과 검증](engine/README.ko.md#-결과-검증)
  - [진행 보고서](engine/README.ko.md#-진행-보고서)
  - [유휴 감지](engine/README.ko.md#-유휴-감지)
  - [세션 보드와 격리](engine/README.ko.md#-세션-보드와-격리)
- [메모리와 연속성](memory/README.ko.md)
  - [계층형 메모리](memory/README.ko.md#-계층형-메모리)
  - [압축 전 메모리 플러시](memory/README.ko.md#-압축-전-메모리-플러시)
  - [요약 ↔ TaskFlow 조정](memory/README.ko.md#-요약--taskflow-조정)
  - [서브에이전트 메모리 역류](memory/README.ko.md#-서브에이전트-메모리-역류)
  - [도구 출력 요약](memory/README.ko.md#-도구-출력-요약)
  - [세션 연속성](memory/README.ko.md#-세션-연속성)
  - [TaskFlow 자동 재개](memory/README.ko.md#-taskflow-자동-재개)
- [동시성 레인](lanes/README.ko.md)
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
│ (14, main_only)   │          │  add/replace/remove  │                 │ build_system_prompt    │
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
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None, session_id)` | waiting 상태 + 리비전 |
| `taskflow_summary` | `(flow_id, session_id)` | 대기/데드라인 상태를 포함한 전체 flow 상태 |
| `taskflow_progress` | `(flow_id, session_id)` | 완료율, 내역, 다음 단계, 예상 남은 시간 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None, session_id)` | 예산 보고서, 또는 설정 확인 |
| `taskflow_list` | `(status_filter="active", session_id)` | 이 세션의 보드(`active` / `all` / 상태 이름) |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None, session_id)` | 종단 `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None, session_id)` | 종단 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None, session_id)` | 종단 `cancelled` |

### memory 도구 액션

| 액션 | 시그니처 | 반환 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 성공/오류 |

### 주요 함수와 상수

| 심볼 | 위치 | 역할 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | 라이프사이클 / DAG 열거형 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py` | 낙관적 잠금 세션 범위 변경 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | 세션 범위 활성 flow 읽기(SQL `session_id` 필터) |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py` | sweeper 쿼리(의도적으로 세션 간) |
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
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | 세션 보드 읽기(SQL `session_id` 필터) |
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
| `test_taskflow_list.py` | 세션 보드 렌더링, 상태 필터, 마지막 활동 타임스탬프 |

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
- **`taskflow_list`는 세션 범위입니다.** 전역 세션 간 보드는 없습니다: 모든 읽기가 소유 `session_id`로 SQL 필터링되므로 한 세션이 다른 세션의 flow를 열거할 수 없습니다. 격리 전 행(`session_id = ''`)은 세션 읽기에서 보이지 않지만 sweeper의 세션 간 데드라인/유휴 스캔은 해석합니다.
- **지식 저장은 계획 이름이 아니라 계획 아이덴티티를 키로 사용합니다.** 접근은 소유권 검사(`ownership.is_plan_associated()`: 세션의 `plan_ref`, todo의 `plan_ref`, 또는 `session_ids`에 해당 세션을 포함하는 boulder work)로 제어되고, 저장 디렉터리는 정규화된 계획 경로에서 파생됩니다 — `workspace/knowledge/plans/<plan_key>/`, `plan_key = sha1(리포지토리 루트 상대 경로)[:12]`, 각 디렉터리의 `meta.json`에 가독 `plan_name` / `plan_ref` 기록. **계획 파일이 다른** 같은 이름 계획은 두 세션에서 **물리적으로 격리**되며(각자 자신의 key 디렉터리에 기록), boulder `session_ids`로 **하나의 계획 파일**을 협업하는 세션은 같은 경로로 해석되어 하나의 디렉터리를 공유합니다. 계획 파일이 해석되지 않는 세션은 폴백 아이덴티티 `session-<sha1(session_id)[:8]>`에 기록합니다(전체 id 해시로 앞 8자가 같은 세션 id도 충돌하지 않음). 레거시 이름 키 디렉터리는 읽기 가능하게 유지되고, 쓰기는 항상 key 디렉터리에 기록됩니다. `clear_session`은 세션 전용 아이덴티티 디렉터리를 삭제하고 다른 세션과 공유된 계획은 유지합니다. 서브에이전트 경계는 그대로 절대적입니다 — `knowledge`는 `main_only`입니다.
