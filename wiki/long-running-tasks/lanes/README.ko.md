# 🚦 동시성 레인 — MAIN · SUBAGENT · NUDGE · NESTED

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Long-Running Tasks](../README.ko.md)의 일부: 프로세스 수준 동시성 레인(MAIN, SUBAGENT, NUDGE, NESTED)으로 자식 에이전트, 백그라운드 nudge, 중첩 답장 턴을 제어합니다.

---

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
| **재시작 / 복원** | `restore_runs_from_disk()`(`registry/state.py`)는 시작 시 SQLite에서 복원한 모든 PENDING run을 `TERMINAL` / `pending_orphaned`로 확정합니다 —— 조용히 수행되며 announce 흐름은 실행하지 않습니다(부모 세션은 이전 프로세스 생존 기간의 것입니다); 동일 프로세스 생존 기간 내에 task를 잃은 경우만 sweeper의 고아 복구에 들어갑니다 |
| **Yield** | `sessions_yield`는 PENDING 자식을 활성으로 세고, `wake_yield_if_all_children_settled`는 전부 끝나야 부모를 깨웁니다; yield 타임아웃은 레인 대기를 포함하고 만료 시 정상 반환합니다 |
| **계수 / 목록** | `control/list.py`와 `runtime_tools.py`는 PENDING 자식을 RUNNING/INTERRUPTED와 함께 표시합니다 |

PENDING run의 레인 task가 아직 존재하는 동안에는 sweeper 스캔이 건너뜁니다(프로세스가 그저 대기 중일 뿐); task 상실만이 고아로 만듭니다 —— 이전 프로세스가 남긴 PENDING은 시작 복원에서 확정되므로 sweeper에 도달하지 않습니다.

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
| `agent/tools/subagent/registry/state.py` | 재시작 잔여 PENDING의 복원 시 `pending_orphaned` 확정 |
| `tests/runtime/lane/` · `tests/server/service/test_main_lane.py` · `tests/agent/tools/subagent/test_{spawn_lane_integration,kill_pending,steer_lane,sweeper_pending,sessions_yield_pending,registry_restore}.py` · `tests/server/trigger/http/test_lane_api.py` | 레인 테스트 스위트 |

### 구현 트레이드오프

구현 시 기록된 의도적인 트레이드오프 세 가지:

- **MAIN은 불변식까지 상향 클램프.** CPU 스케일링만으로는(`min(16, max(8, CPU))`) CPU ≤ 8 머신에서 8이 되어 시작 시 `validate_lane_config()`가 실패하므로, 기본값을 `SUBAGENT + NUDGE`(12)까지 올려 4/8/12/16/64코어 머신 모두에서 유효합니다.
- **`GET /lane-status`에는 `/api` 접두사가 없습니다.** 최초 설계의 `/api/lane-status` 라우트를 버리고 저장소의 기존 라우팅 관례를 따랐습니다: 핸들러는 `server/trigger/http/lane.py`에 있으며 `/channels`, `/cron`과 나란히 있습니다.
- **종료 drain은 `atexit` 경로.** 저장소에 비동기 셧다운 seam이 없으므로(Robyn의 `shutdown_handler`는 SIGINT/SIGTERM에서 호출되지 않음), 유계 `drain_all(timeout=0)`은 최종 레인별 카운터만 보고하고 종료를 지연시키지 않습니다. 실행 중 턴은 OS에 맡겨집니다.

