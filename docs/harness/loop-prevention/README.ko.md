# 🔁 런어웨이 루프 방지: 가드, 브레이커, 크래시 게이팅

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 상시 가동 에이전트가 스스로 빠져나올 수 없는 상황에 갇히는 것을 막는 방법: 모델·도구 호출에 대한 턴 단위 병리 가드, 백그라운드 서비스에 대한 지수 백오프 브레이커, 서브에이전트 간 신뢰할 수 있는 완료 통지 전달, 부팅 시점의 프로세스 수준 크래시 게이팅, 그리고 모든 것이 실패했을 때의 REST / 수동 탈출구.

런어웨이 루프(runaway loop)란 시스템이 스스로 빠져나올 수 없는 모든 순환을 뜻합니다. 같은 문장을 영원히 반복하는 모델 호출, 똑같이 실패하는 인자로 계속 재시도하는 도구 호출, 허공을 향해 찍히는 하트비트나 cron 작업, 부모에게 영원히 도달하지 못하거나(혹은 두 번 도달하는) 서브에이전트 완료 통지, 그리고 크래시 후 재시작되어 같은 크래시로 떨어지는 프로세스가 그 예입니다. 이 하니스는 무인으로 운영되므로(cron 스케줄, 하트비트 기상, 서브에이전트 스윕, 긴 채팅 턴), 모든 루프는 모델에게 '예쁘게 행동해 달라'는 프롬프트가 아니라 *시스템 자신이* 강제하는 경계를 가져야 합니다.

아래의 모든 가드는 두 가지 설계 규칙을 공유합니다:

1. **성능을 낮추되, 절대 크래시하지 않는다.** 보호 기능은 프로세스를 죽이지 않습니다: 백그라운드 서비스는 *정지*하고, 턴은 *우아하게 끝나며*, 부팅 게이트는 프로세스를 HTTP 전용 모드로 *축소*합니다.
2. **항상 탈출구를 남긴다.** 모든 브레이커에는 문서화된 수동 리셋(REST 엔드포인트, 상태 파일 삭제, 프로세스 재시작)이 있습니다.

**사실상의 기준(source of truth):** `agent/middlewares/tool_guardrails.py`, `agent/middlewares/iteration_budget.py`, `agent/middlewares/output_repetition_guard.py`, `agent/stream_repetition_guard_wrapper.py`, `agent/middlewares/heartbeat_staleness.py`, `agent/middlewares/subagent_completion_drain.py`, `agent/tools/subagent/announce/delivery.py`, `agent/tools/subagent/announce/idempotency.py`, `runtime/periodic_backoff.py`, `runtime/crash_loop_breaker.py`, `skills/builtin/core/cron/scripts/base.py`, `skills/builtin/core/heartbeat/scripts/base.py`, `agent/tools/subagent/registry/sweeper.py`, `server/__main__.py`, `server/trigger/http/cron.py`, `server/trigger/__init__.py`, `server/trigger/channels/core.py`.

## 🎯 개요와 위협 모델

| 루프 고도 | 모습 | 방어 |
|---|---|---|
| **텍스트 데스 루프** (모델 호출 1회) | 같은 문장 / 같은 문자열이 영원히 스트리밍됨 | `OutputRepetitionGuard` (워커) + `RepetitionGuardWrapper` (메인 에이전트 스트림) |
| **도구 병리 루프** (턴 1회) | 같은 실패 도구 호출, 핑퐁 쌍, 인자 갱신 | `ToolGuardrails`: 5가지 병리 → WARN → BLOCK → HALT, 복구 모드 포함 |
| **무한 턴** | 모델/도구 호출이 끝나지 않음 | `IterationBudget` (메인 90 / 워커 60, 합산 호출) |
| **멈춘 턴** | 수 분간 진행 없음 (도구 행, 끼인 루프) | `HeartbeatStaleness` 와치독 → `HeartbeatTimeoutError` |
| **백그라운드 서비스 루프** | 하트비트 / 스위퍼 / cron 틱이 영원히 실패 | `PeriodicBackoff` (소진 = 서비스 정지) / cron 강등 → 자동 비활성 |
| **완료 통지 유실 또는 중복** | 서브에이전트는 끝났는데 부모가 통지를 못 받거나 두 번 받음 | 완료 drain (1회만 주입) + announce 재시도 사다리 + 멱등 키 |
| **크래시-재부팅 루프** | 프로세스가 부팅 중 크래시하고, 슈퍼바이저가 같은 크래시로 재시작 | `CrashLoopBreaker` → HTTP 전용 모드 |
| **그 외 전부** | 브레이커가 작업을 비활성화했거나 부팅 게이트가 작동함 | Cron REST `failure-state` / `reset-failures`, 상태 파일 리셋 |

방어는 고도별로 계층화되어 있어, 한 층을 빠져나간 실패는 다음 층에 걸립니다:

1. **턴 수준**: 턴 단위 가드가 하나의 대화 턴을 묶습니다 (텍스트, 도구, 반복, 스테일니스).
2. **복구 간주**: `ToolGuardrails` BLOCK은 HALT로 격상되기 전에 *관리되는 재시도 창*을 한 번 얻습니다.
3. **백그라운드 수준**: 주기적 서비스는 지수적으로 백오프하고 연속 5회 실패 후 *정지*합니다.
4. **프로세스 수준**: 크래시-재부팅 루프는 부팅 브레이커를 작동시켜 최소한의 HTTP 전용 모드로 부팅합니다.
5. **운영**: REST 엔드포인트와 문서화된 수동 리셋.

## ⚙️ 구현과 아키텍처

### 턴 수준: `ToolGuardrails`, 도구 호출 병리 감지

메인 에이전트, 워커 에이전트, nudge 서브에이전트에서 활성화됩니다. 모든 도구 호출 기록은 평가*이전에* 턴 단위 상태(`state_register_mem`의 `tool_guardrail_state`)에 추가되므로 임계값은 자기 포함적입니다. '2회 이후'는 현재 호출이 2번째임을 뜻하고, 5번째 동일한 무진전 호출 자체가 np=5에 도달해 차단됩니다. 격상 사다리: ALLOW → WARN (대화 기록에 nudge 추가) → BLOCK (설명 메시지와 함께 호출 거부) → HALT (터미널 메시지와 함께 턴이 우아하게 종료, 반복 예산과 동일한 패턴).

| 병리 | 신호 | WARN 이후 | BLOCK 이후 | hard-stop 모드 |
|---|---|---|---|---|
| 동일 실패 반복 | 같은 도구 + 같은 (해시된) 인자가 계속 실패 | 2 | 5 | 5에서 HALT |
| 동일 도구 실패 폭풍 | 같은 도구가 계속 실패, 인자는 달라도 됨 | 3 | 8 | 8에서 HALT |
| 멱등 무진전 | 멱등 도구에서 동일한 (해시된) 결과 | 2 | 5 | 5에서 HALT |
| 핑퐁 | 끊김 없는 읽기 전용 A → B → A → B 왕복 | 4 | 6 | 6에서 HALT |
| 인자 갱신 | 같은 멱등 도구가 인자 변형을 순환 | 3개 변형 | 5개 변형 | 5에서 HALT |

중요한 세부 사항:

- **복구 모드** (`recovery_mode_enabled=True` 기본값): 첫 BLOCK은 턴을 벽돌로 만들지 않습니다. 턴은 복구 상태로 들어가고, *precheck* 경로가 차단된 도구를 풀어주어 재시도가 새로 평가됩니다. 이후 BLOCK마다 위반 카운터가 증가하고, 카운터가 `recovery_max_violations`(기본 1)를 초과하면 HALT로 격상됩니다. 요컨대 즉석 벽이 아니라 관리되는 재시도 창입니다. 이전의 엄격한 동작을 원하면 `recovery_mode_enabled=False`로, 모든 BLOCK 임계값을 HALT로 바꾸려면 `hard_stop_enabled=True`로 설정하세요(표 참조).
- **핑퐁 쌍**은 인접 호출의 두 도구 이름을 해시하고, *연속된 두* 호출이 모두 성공한 멱등 호출일 때(두 기록 모두 결과 해시 보유)만 누적됩니다. 에러가 하나라도 있거나, 성공한 비멱등(변이) 호출이 하나라도 있으면 누적된 모든 쌍 연속 기록이 0으로 돌아갑니다. 결과 내용은 비교하지 않습니다: 끊김 없는 읽기 전용 왕복은 그 자체로 루프 신호입니다. 비멱등 도구의 성공 역시 인자 갱신 상태를 리셋합니다.
- 가드 상태는 엄격히 **턴 범위**입니다: `before_agent`가 리셋하므로 새 턴은 깨끗하게 시작합니다.

### 턴 수준: `IterationBudget`

턴당 모델 호출과 도구 호출을 **합산**해서 셉니다. 메인 에이전트 90, 워커 에이전트 60 (기본값 50). 소진된 모델 호출은 터미널 AIMessage를 반환하고, 소진된 도구 호출은 에러 ToolMessage를 반환해 모델이 루프 한가운데서 죽지 않고 마무리할 수 있게 합니다. 내부 완료 통지 턴은 면제됩니다(반복을 소모하지 않음). 카운터(`iteration_budget` / `iteration_budget_used`)는 매 턴 리셋됩니다.

### 턴 수준: 텍스트 데스 루프, `OutputRepetitionGuard` + `RepetitionGuardWrapper`

**교차 호출 감지** (`OutputRepetitionGuard`, 워커 파이프라인의 미들웨어):

- 콘텐츠는 정규화되고(NFKC → 공백 제거 → 구두점 제거) 처음/마지막 500자를 이용한 이중 `head|tail` MD5로 해시되어, 긴 출력의 어느 쪽 끝에서든 반복을 잡아냅니다. 세션당 30개 해시의 롤링 히스토리를 유지합니다.
- 연속 2회 동일 출력에서 WARN (nudge 추가); 3회에서 HALT (터미널 메시지; halt 플래그는 턴 동안 고착). 교차 호출 매칭은 콘텐츠 1자만 필요로 하므로, 연속 호출 사이에서 반복되는 짧은 문장 하나만으로도 유효한 데스 루프 신호입니다.
- 출력별 **내부 감지**: 중복 세그먼트 비율 > 0.6 (구두점/개행으로 분할, 최소 6세그먼트), 문자 연속 실행 ≥ 8, 2-10자의 짧은 구절이 연속 ≥ 5회 반복. 20자 미만 콘텐츠는 오탐 방지를 위해 건너뜁니다. 내부 경고는 라벨당 세션당 최대 1회 발생합니다.
- **추론은 독립적으로 추적**: `reasoning_content` / `reasoning` / `reasoning_text` kwargs와 인라인 `<think>` / `<thinking>` / `<reasoning>` 래퍼는 별도의 히스토리와 warned 플래그를 사용합니다.
- 미들웨어는 정확히 여섯 개의 세션 상태 키(`SESSION_STATE_KEYS`)를 소유하며, 서브에이전트에서 파생된 에이전트가 해체될 때 해제됩니다. 미들웨어 간 상태 누수가 없습니다.

**스트림 계층** (`RepetitionGuardWrapper`, 메인 에이전트 래핑):

- 청크가 클라이언트에 도달하기 *전*, 스트림 도중에 내부 반복을 잘라냅니다: 경고 하나를 주입한 뒤 해당 호출 스트림의 나머지를 억제합니다.
- HALT 쇼트서킷: 턴에 halt가 기록되면 이후 모델 호출은 halt 메시지를 직접 반환합니다.
- **팬텀 스트림 가드** (옵트인, 프로덕션에서 활성화): 새 dict 입력 호출이 진행 중인 런을 대체할 때 갱신 전 모델 텍스트를 버립니다.

### 턴 수준: `HeartbeatStaleness`, 멈춘 턴 와치독

턴당 1분 타이머(`timer_call_register`)를 등록해 `heartbeat_iter` / `heartbeat_tool` 카운터를 마지막 관측값과 비교합니다. 진행이 있으면 스테일 카운터가 리셋됩니다; 유휴 에이전트에서 **7** 사이클(약 7분), 도구 실행 중에는 **20** 사이클(약 20분) 동안 진행이 없으면 killed 플래그가 설정되고, 다음 에이전트 루프 진입 시 `HeartbeatTimeoutError`를 던져 턴을 우아하게 끝냅니다. 메인과 워커 에이전트 모두에 등록되며, 턴 단위 상태는 `before_agent`에서 리셋되고 타이머는 `after_agent`에서 해제됩니다.

### 파이프라인 수준: 서브에이전트 완료 drain + announce 재시도

**주입 drain** (`agent/middlewares/subagent_completion_drain.py`): 세션의 `SteeringQueue`를 재수화하고 비우는 `before_model` 미들웨어로, 다음 모델 호출 직전에 대기 중인 완료 캐리어를 주입합니다. SQLite 행은 drain 시 `CONSUMED`로 표시되므로 체크포인트 리플레이(HITL 재개)가 같은 완료를 다시 주입할 수 없습니다. 이 미들웨어는 완전히 fail-open입니다: 모든 실패는 로그로 남기고 삼켜지며, 부모 턴은 주입 없이 계속됩니다. 이것이 '이미 끝난 자식을 부모가 영원히 기다리는' 루프를 닫습니다.

**전달 재시도 + 멱등성** (`agent/tools/subagent/announce/delivery.py`, `idempotency.py`): 바쁜 세션의 완료 통지는 고정 사다리로 일시적 실패를 재시도합니다(5s / 10s / 20s, 최대 `announce_retry_max=3`; 컴팩션 에러는 1s / 2s / 4s / 8s). 영구 실패는 재시도하지 않습니다. 모든 전달은 `subagent_announce:{run_id}:gen:{generation}` 키로 유계 메모리 멱등 집합에 기록되므로, 재시도된 announce가 이중 주입할 수 없습니다. 재시도 소진 → run FAILED; 소프트 재시도 한도 → SUSPENDED; `max_announce_retry_count`(10) 재시도에 도달했거나 24시간 나이 한도를 넘은 run은 폐기됩니다. 스위퍼의 고아 복구와 함께 서브에이전트 라이프사이클의 전달 측면에 경계가 생깁니다.

### 백그라운드 수준: `PeriodicBackoff`, 브레이커 하나, 서비스 셋

`runtime/periodic_backoff.py`는 순수 상태 머신입니다(스레드 없음, I/O 없음):

- `record_failure()`: `consecutive_failures += 1`; `current_interval = min(base × factor^n, max_interval)`; `consecutive_failures >= max_consecutive_failures`일 때 소진.
- `record_success()`: 완전 리셋. 기본값: `factor=2.0`, `max_interval=7200s`, `max_consecutive_failures=5`.

| 서비스 | 기본 간격 | 실패 간격 | 소진 시 |
|---|---|---|---|
| 하트비트 (`skills/builtin/core/heartbeat/scripts/base.py`) | 1800s (`HeartbeatConfig.interval_s`와 일치) | 3600s → 7200s → 7200s → 7200s | CRITICAL 로그 ("paused ... manual recovery required"); 루프가 반환되어 서비스는 정지하고 프로세스는 살아있음 |
| 서브에이전트 스위퍼 (`agent/tools/subagent/registry/sweeper.py`) | 60s (`sweeper_interval_seconds`) | 120s → 240s → 480s → 960s | CRITICAL 로그; `_running=False`가 스윕 태스크를 종료 |
| cron 작업 브레이커 (아래) | 작업별, 5s 기본 | 강등 → 비활성 | 작업 자동 비활성 |

알아둘 만한 의미론:

- 하트비트는 tick *내부에서* 성공을 기록하고(tick이 자기 에러를 삼킴), 진짜 tick 실패만 카운트됩니다. 일시정지 후에도 `trigger_now()`는 동작합니다: 수동 호출은 잠자는 루프를 우회합니다.
- `stop_sweeper()`는 백오프 객체를 버리고(`_backoff=None`) 수동으로 재시작한 스윕이 새로 시작되게 합니다. 백오프 객체는 지연 생성되고(`_get_backoff`) import 시점에 만들어지지 않습니다.
- 프로덕션에서 스위퍼는 `server/trigger/channels/core.py`의 `_schedule_sweeper`가 시작하며, 코루틴을 메인 이벤트 루프로 옮깁니다(`run_coroutine_threadsafe`); 이 배선은 `tests/unit/server/test_sweeper_wiring.py`가 커버합니다.
- 백오프 상태는 Python 객체에 존재합니다: 프로세스를 재시작하면 하트비트와 스위퍼 브레이커가 리셋됩니다.

### 백그라운드 수준: cron 작업 실패 브레이커

`skills/builtin/core/cron/scripts/base.py`의 작업별 상태 머신(`CronJobFailureState`, 메모리 전용; `enabled` 플래그를 제외하고 `cron_jobs.json`에 기록되지 않음):

| 연속 실패 | 효과 |
|---|---|
| 1-4 | 작업이 평소처럼 실패: 상태를 error로 표시, WS 벨 알림 |
| ≥ 5 (강등) | 백오프 창 안에서는 트리거가 건너뛰어짐: 마지막 실패로부터 `min(5000ms × 2^(n-5), 300000ms)` |
| ≥ 10 | `enabled=False`가 영속화됨; 최선의 노력으로 작업의 payload 채널에 알림 |

- **기록 후 재던지기:** 실패를 먼저 기록한 다음 예외를 그대로 다시 던지므로 상태/에러 보고가 온전하게 유지됩니다.
- **일회성 `at` 작업은 면제** (두 번 발화할 수 없으므로 단일 실패는 루프가 아님).
- 성공은 상태를 완전히 리셋합니다. 수동 `enable_job`이 이를 지우고; REST `reset-failures` 엔드포인트는 *브레이커 자신이* 비활성화한 경우에만 재활성화하므로 운영자의 비활성화는 보존됩니다.

### 프로세스 수준: `CrashLoopBreaker` + 부팅 게이팅

`runtime/crash_loop_breaker.py`는 부팅 저널을 `src/data/boot_lifecycle.json`에 영속화합니다(키: `{ts, clean, reason}` 항목을 담은 `boots`, reason은 200자 제한; `last_exit_clean` 일회용 마커):

| 파라미터 | 값 | 의미 |
|---|---|---|
| `TRIP_THRESHOLD` | 3 | 작동에 필요한 불클린 부팅 횟수 |
| `WINDOW_S` | 300 | 5분 윈도우 안에서 |
| `RETENTION_S` | 3600 | 부팅 기록은 1시간 후 정리 |

부팅 시퀀스 (`server/__main__.py`), 순서대로:

1. `was_last_exit_clean()`이 `record_boot(clean=..., reason="startup")`이 마커를 소비하기 **전에** 일회용 마커를 읽습니다.
2. `atexit.register(mark_clean_exit)`: *우아한* 종료는 다음 부팅을 클린으로 표시합니다. 이것이 자가 치유입니다; 한 번만 클린하게 종료하면 오래된 불클린 기록이 5분 윈도우에서 자연 소멸합니다.
3. 작동 시 (5분 내 3회 이상 불클린 부팅): `SHERRY_HTTP_ONLY=1` 설정, CRITICAL 로그, **HTTP 전용 모드**로 부팅:
   - `init_agent_core()`는 여전히 실행되므로 채팅은 계속 동작합니다.
   - 큐레이터와 cron 백그라운드 초기화는 건너뛰어지고; `server/trigger/__init__`는 채널 매니저와 서브에이전트 임포트를 건너뛰므로 하트비트 서비스와 스위퍼도 시작되지 않습니다.
   - HTTP/WS 라우트와 cron REST API는 유지됩니다.

수동 리셋: `src/data/boot_lifecycle.json`을 삭제하거나, 그냥 한 번 클린하게 종료해 윈도우가 자연 소멸하게 두면 됩니다.

### 계층 매트릭스: 어느 계층이 무엇을 잡는가

| 계층 | 메커니즘 | 잡는 것 |
|---|---|---|
| 미들웨어 (그래프 내, 턴별) | `ToolGuardrails`, `IterationBudget`, `OutputRepetitionGuard` / `RepetitionGuardWrapper`, `HeartbeatStaleness`, `SubagentCompletionDrain` | 도구 병리, 무한 턴, 텍스트 데스 루프, 멈춘 턴, 누락된 완료 주입 |
| 프로세스 (백그라운드 서비스) | `PeriodicBackoff` (하트비트, 스위퍼), cron 실패 브레이커, announce 재시도 사다리 + 멱등성 | 서비스 재시도 폭풍, 실패하는 예약 작업, 중복 완료 전달 |
| 부팅 (프로세스 라이프사이클) | `CrashLoopBreaker`, `server/__main__` 게이팅, `trigger.__init__` 조기 종료 | 크래시-재부팅 루프 |
| 인프라 / 운영 | cron REST 탈출구, HTTP 전용 환경변수, 상태 파일 삭제 | 운영자의 개입이 필요한 꼼짝 못 하는 브레이커 상태 |

## 📊 우선순위 매트릭스

| 가드 | 고도 | 상태의 집 | 리셋 시점 |
|---|---|---|---|
| `ToolGuardrails` | 턴 (도구 호출) | `state_register_mem` (`tool_guardrail_state`) | 매 턴 (`before_agent`) |
| `IterationBudget` | 턴 (호출 수) | `state_register_mem` | 매 턴 |
| `OutputRepetitionGuard` | 턴 + 세션 (텍스트) | 6개 세션 키 | halt 플래그는 턴별; 해시 히스토리는 세션별 (서브에이전트 해체 시 해제) |
| `RepetitionGuardWrapper` | 스트림 호출 (텍스트) | in-flight + halt 키 | 모델 호출별 |
| `HeartbeatStaleness` | 턴 (월클록) | `heartbeat_*` 키 + 1분 타이머 | 매 턴 |
| `SubagentCompletionDrain` | 턴 (주입) | SteeringQueue 행 (SQLite) | drain 시 행이 CONSUMED로 표시 |
| Announce 재시도 + 멱등성 | 런 (전달) | 메모리 멱등 집합 + run 기록 | 성공 / 재시도 한도 / 24시간 만료 |
| `PeriodicBackoff` (하트비트 / 스위퍼) | 서비스 (틱) | Python 객체 | 성공 / 프로세스 재시작 / `stop_sweeper` |
| cron 실패 브레이커 | 작업 (트리거) | 메모리 내 `CronJobFailureState` | 성공 / `reset-failures` / 수동 `enable_job` |
| `CrashLoopBreaker` | 프로세스 (부팅) | `src/data/boot_lifecycle.json` | 클린 종료 소멸 / 파일 삭제 |

한 턴 안에서 턴 가드들은 서로 직교하며 병렬로 발동합니다: `OutputRepetitionGuard` / `RepetitionGuardWrapper`는 *텍스트*를, `ToolGuardrails`는 *도구 호출*을, `IterationBudget`은 *횟수*를, `HeartbeatStaleness`는 *월클록*을 지킵니다. 먼저 작동하는 것이 턴을 끝내며, 서로를 막지 않습니다. 모두 놓치면 백그라운드 브레이커가 *다음* 트리거를 묶고, 부팅 브레이커가 *다음* 프로세스를 묶습니다.

## 🛠️ 설정과 사용법

- **모든 임계값은 코드 기본값입니다** (dataclass / 생성자 파라미터); 환경 변수는 의도적으로 두지 않았습니다. 특히 `config/schema.py`의 `max_tool_iterations = 40`은 미들웨어가 소비하지 *않고*(예산은 명시적으로 전달됨: 90 / 60), `HeartbeatConfig.interval_s = 1800`은 하트비트 서비스 기본값과 일치하지만 서비스는 기본값으로 생성됩니다.
- `TOOL_CALL_TIMEOUT_MINUTES` (`.env.example` 기본 5)는 현재 **문서 전용**입니다: 이를 소비하는 코드가 없습니다. 실제로 활성인 도구별 상한은 상수입니다(웹 검색 15s, 터미널 30s, python REPL 30s). 이것을 루프 경계로 삼지 마세요.
- 워커는 미들웨어로 `OutputRepetitionGuard`를 받고; 메인 에이전트는 `RepetitionGuardWrapper`로 래핑됩니다(미들웨어 훅은 원시 스트림 청크를 볼 수 없음).
- `ToolGuardrails` 노브: `warnings_enabled` (기본 True), `hard_stop_enabled` (기본 False, BLOCK은 차단으로 유지), `recovery_mode_enabled` (기본 True), `recovery_max_violations` (기본 1).
- Announce 전달 노브 (서브에이전트 announce 설정): `announce_retry_max=3`에 5s / 10s / 20s 일시적 지연, 그리고 `max_announce_retry_count=10`과 24시간 run 만료.

수동 복구 치트시트:

| 상황 | 조치 |
|---|---|
| cron 작업이 브레이커에 의해 자동 비활성화됨 | `POST /cron/reset-failures {"id": ...}` (브레이커가 비활성화한 작업만 재활성화) |
| cron 작업의 브레이커 상태 조회 | `POST /cron/failure-state {"id": ...}` (모르는 작업 → 404, 한 번도 실패한 적 없는 작업 → 0으로 초기화된 상태) |
| 하트비트 일시정지 (틱 5회 실패) | 프로세스 재시작; `trigger_now`는 여전히 일회성 틱을 발화 |
| 스위퍼 정지 (백오프 소진) | 프로세스 재시작; 새 스윕이 새 백오프로 시작 |
| 부팅 게이트 작동 (HTTP 전용) | 한 번 클린하게 종료, 또는 `src/data/boot_lifecycle.json` 삭제 |

## 🧪 테스팅

| 스위트 | 커버 범위 |
|---|---|
| `tests/unit/middlewares/test_tool_guardrails.py` | 병리 감지, 격상 사다리, 복구 모드 |
| `tests/unit/runtime/test_periodic_backoff.py` | 간격 수학, 소진, 성공 리셋 |
| `tests/unit/runtime/test_crash_loop_breaker.py` | 작동 윈도우 / 보존, 클린 마커, 손상된 상태 |
| `tests/unit/cron/test_cron_failure_breaker.py` | 강등 → 비활성, 리셋 의미론 |
| `tests/unit/heartbeat/test_heartbeat_backoff.py` | 서비스 백오프 배선, 소진 일시정지 |
| `tests/unit/subagent/test_sweeper_backoff.py` | 스위퍼 백오프 배선, 루프 정지 |
| `tests/unit/server/test_sweeper_wiring.py` | 스위퍼 시작 배선 |
| `tests/unit/server/test_crash_gating.py` | 부팅 게이팅, HTTP 전용 모드 |
| `tests/unit/server/test_cron_api.py` | Cron REST, failure-state / reset-failures 포함 |

## ⚠️ 정직함과 한계

- **턴 가드는 설계상 턴 범위입니다**: 새 턴은 새 가드 상태로 시작합니다. 턴 간 반복 감지는 `OutputRepetitionGuard`의 영역(세션 범위 히스토리)이지 도구 가드레일의 영역이 아닙니다.
- **메모리 내 브레이커 상태는 재시작을 살아남지 못합니다**: 가드레일/반복/반복 예산 상태는 애초에 턴 또는 세션 범위이고; cron 실패 카운터는 프로세스 재시작 시 사라지지만(영속화된 `enabled` 플래그는 사라지지 않음); 하트비트/스위퍼 백오프는 Python 객체에 존재합니다. 따라서 재시작은 언제나 리셋이며, 때로는 너무 관대한 리셋입니다.
- **소진된 서비스는 재시작까지 멈춥니다**: 일시정지된 하트비트나 정지된 스위퍼에는 런타임 재무장 API가 없습니다("manual recovery required"는 문자 그대로입니다). 프로세스 자체는 계속 서빙합니다.
- **크래시 게이트는 윈도우 기반입니다**: 5분보다 멀리 떨어진 크래시 루프는 절대 작동하지 않고, 상태 파일을 지우는 슈퍼바이저는 이를 리셋합니다. 이 파일은 브레이커의 기억이자 수동 탈출구입니다.
- **`hard_stop_enabled`는 기본 False입니다**: 엄격 모드에서는 동일 도구 실패와 hard-stop으로 변환된 BLOCK만 HALT에 도달합니다; 다른 병리는 BLOCK에서 멈춥니다(복구 모드의 영향을 받음).
- **콘텐츠 정규화는 양날의 검입니다**: 공백/구두점 제거는 해시를 포맷 노이즈에 강하게 만들지만, 매번 *말을 바꿔* 루프를 도는 모델은 해시 기반 감지를 피해갑니다. 내부 세그먼트/연속 실행 감지기가 부분적으로 이를 커버합니다; 완전히 바꿔 말한 루프는 범위 밖입니다.
- **복구 모드는 모델에게 실패의 여지를 줍니다**: 완고한 병리는 HALT 전에 관리되는 재시도 한 번을 치러야 합니다. 즉시 벽을 원하는 운영자는 `recovery_mode_enabled=False`로 설정해야 합니다.
- **`TOOL_CALL_TIMEOUT_MINUTES`는 선언만 있고 읽히지 않습니다**: `.env.example`에 존재하고(루트 README에도 설명됨) 하지만 오늘날 이를 소비하는 코드는 없습니다; 위에 나열된 도구별 상수가 실제 경계입니다.
- **HTTP 전용 모드는 축소된 풋프린트이지 잠금이 아닙니다**: 채팅, HTTP/WS 라우트, cron REST는 설계상 유지됩니다; 목표는 *크래시 루프*를 끊는 것이지 프로세스를 완전히 격리하는 것이 아닙니다.
