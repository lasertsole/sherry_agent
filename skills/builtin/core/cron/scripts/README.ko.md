# EMA Cron — 예약 작업 서비스

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent 시스템을 위한 경량 파일 기반 cron 서비스입니다. 일회성 작업, 고정 간격 작업, cron 표현식 작업을 예약하고 실행합니다. 작업은 프로젝트 루트의 `cron_jobs.json`에 영속화되며, 전용 백그라운드 서비스가 실행하고, 결과는 메시지 버스를 통해 활성화된 채널로 전달됩니다.

## 기능

- 세 가지 스케줄 유형: `at` (일회성), `every` (고정 간격), `cron` (cron 표현식, `croniter` 사용)
- `cron_jobs.json` (프로젝트 루트) 기반 파일 영속화, 외부 수정 시 자동 재로드
- 자체 asyncio 이벤트 루프를 가진 전용 백그라운드 서비스 스레드; 자동 재무장 타이머가 가장 이른 예정 시각의 작업에 맞춰 정확히 기상
- 작업 실행 시 전용 에이전트를 구동 (메인 LLM + 시스템 프롬프트 + Python REPL / 파일 읽기 / 파일 쓰기 도구)
- 결과는 `MessageBus` 인바운드 큐를 통해 채널로 전달되고, 브라우저 UI에는 best-effort WebSocket `notification` 이벤트가 전송됨
- 작업별 실행 로그를 JSON Lines 형식으로 `logs/output/cron/<job_id>.log`에 추가 기록
- 보호된 시스템 작업 (`payload.kind == "system_event"`)은 제거 불가
- cron 표현식용 시간대 지원 (`zoneinfo`를 통한 IANA 시간대 이름)
- REST API (Robyn): `GET/POST/PUT/DELETE /cron`, `POST /cron/trigger`, `POST /cron/enable` — 데스크톱 클라이언트용

## 모듈 구조

```
skills/builtin/core/cron/
├── __init__.py
├── SKILL.md             # 에이전트 스킬 정의 (add / list / remove / set_context 레시피)
└── scripts/
    ├── __init__.py      # 공개 내보내기: CronService, cron_service, Cron, cron, types
    ├── base.py          # CronService 싱글턴, cron_jobs.json 입출력, 타이머 루프, 작업 실행
    ├── core.py          # Cron 퍼사드 (에이전트용): add_job / list_jobs / remove_job / set_context
    ├── types.py         # 데이터 모델: CronSchedule, CronPayload, CronRunRecord, CronJobState, CronJob, CronStore
    └── README.md        # 이 파일
```

이 스킬 디렉터리 밖의 관련 코드:

- [`server/trigger/http/cron.py`](../../../../../server/trigger/http/cron.py) — `cron_service`를 감싸는 REST 엔드포인트
- [`../SKILL.md`](../SKILL.md) — 에이전트의 스킬 스크립트 호출 방법
- `cron_jobs.json` — 프로젝트 루트의 작업 저장소 (`config.ROOT_DIR / "cron_jobs.json"`)
- `logs/output/cron/` — 작업별 실행 로그

## 동작 방식

1. **서비스 시작**: 서비스 엔트리포인트가 `skills.builtin.core.cron.scripts.base`의 `init()`을 호출하여 실행 콜백을 연결하고 `cron-service`라는 이름의 데몬 스레드(`_start_cron_service_thread`)를 시작합니다. 이 스레드는 전용 asyncio 이벤트 루프를 만들어 `cron_service.start()`를 실행한 뒤 계속 루프합니다. cron 스크립트 임포트에는 부수 효과가 없습니다. `CronService.add_job()` / `register_system_job()`도 서비스가 아직 실행 중이 아니면 호출자의 이벤트 루프에서 지연 시작합니다.
2. **타이머 루프**: `_arm_timer()`는 활성 작업 중 가장 이른 `nextRunAtMs`까지의 `asyncio` 슬립을 한 번 예약합니다. 이후 `_on_timer()`가 저장소를 다시 로드하고(외부 수정 사항 반영), `nextRunAtMs <= now`인 활성 작업을 모두 실행한 뒤 저장소를 저장하고 타이머를 재무장합니다.
3. **실행** (`_execute_job`): `set_on_job`으로 등록된 콜백(즉 `_on_cron_job`)이 작업을 실행합니다. 작업의 `lastStatus` / `lastError`를 기록하고, WS 알림을 보내며, 실행 로그 한 줄을 추가합니다. 일회성(`at`) 작업은 이후 삭제되거나(`deleteAfterRun`인 경우) 비활성화되고, 반복 작업은 다음 실행 시각을 다시 계산합니다.

**결과 전달** (`base.py`의 `_on_cron_job`):

1. `create_agent(system_prompt=build_system_prompt(), model=build_main_llm(), tools=[build_python_repl_tool(), build_read_file_tool(), build_write_file_tool()])`로 새 에이전트를 구성하고, 작업의 `payload.message`를 `HumanMessage`로 하여 실행합니다.
2. 에이전트의 최종 메시지를 `InboundMessage(channel=payload.channel, sender_id="cron tool", chat_id=payload.to, content=result)` 형태로 메시지 버스에 게시합니다.
3. 채널 인바운드 컨슈머(`server/trigger/channels/core.py`)가 활성화된 채널마다 해당 메시지를 처리하고, 생성된 답변을 `channel.send(OutboundMessage(...))`로 설정된 `chat_id`에 전달합니다.
4. 별도로 `_push_cron_notification`이 세션 `default`(`CRON_WS_SESSION_ID`)의 WebSocket으로 `{"event": "notification", "content": "cron: <job name> [<status>]"}`를 전송하여 브라우저 UI의 알림 벨을 실시간으로 갱신합니다. Best-effort: 실패는 로그로 남을 뿐 흐름을 중단하지 않습니다.

> 참고: `deliver` 필드는 작업에 저장되고 API로도 노출되지만, 현재 실행 경로(`_on_cron_job`)는 이 값과 무관하게 결과를 버스에 게시합니다. 메시지가 실제로 사용자에게 도달하는지는 활성화된 채널에 따라 달라집니다 (`plugins/channels/config.json` 참조).

## 작업 저장소 (`cron_jobs.json`)

작업은 프로젝트 루트의 `cron_jobs.json`에 영속화됩니다. 파일은 서비스 시작 시 로드되며, 수정 시간(mtime)이 바뀔 때마다 자동으로 다시 로드됩니다. 파일을 직접 편집해 작업을 일괄 추가하거나 수정할 수 있으며, 변경 사항은 다음 타이머 틱에 반영됩니다.

디스크의 필드는 camelCase를 사용합니다 (`base.py`의 `_save_store` / `_load_store`). 최상위는 `version` (int)과 `jobs` (배열)입니다. 작업 예시:

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily_digest",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "atMs": null,
        "everyMs": null,
        "expr": "0 9 * * *",
        "tz": "Asia/Shanghai"
      },
      "payload": {
        "kind": "agent_turn",
        "message": "Summarize today's schedule and important events",
        "deliver": false,
        "channel": null,
        "to": null
      },
      "state": {
        "nextRunAtMs": 1756000000000,
        "lastRunAtMs": null,
        "lastStatus": null,
        "lastError": null
      },
      "createdAtMs": 1755000000000,
      "updatedAtMs": 1755000000000,
      "deleteAfterRun": false
    }
  ]
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | `str` | 고유 작업 ID (`uuid4`의 앞 8자) |
| `name` | `str` | 사람이 읽을 수 있는 이름 |
| `enabled` | `bool` | 활성 여부 (기본 `true`) |
| `schedule` | `object` | 실행 시각: 아래 참조 |
| `payload` | `object` | 실행 내용: 아래 참조 |
| `state` | `object` | 런타임 상태: 아래 참조 |
| `createdAtMs` | `int` | 생성 타임스탬프 (밀리초) |
| `updatedAtMs` | `int` | 마지막 수정 타임스탬프 (밀리초) |
| `deleteAfterRun` | `bool` | 일회성 실행 후 작업 삭제 여부 (기본 `false`) |

**`schedule`**

| 필드 | 타입 | 설명 |
|------|------|------|
| `kind` | `"at" \| "every" \| "cron"` | 스케줄 유형 |
| `atMs` | `int \| null` | Unix 타임스탬프 (밀리초) — `kind: "at"`용 |
| `everyMs` | `int \| null` | 간격 (밀리초) — `kind: "every"`용 |
| `expr` | `str \| null` | cron 표현식, 예: `"0 9 * * *"` — `kind: "cron"`용 |
| `tz` | `str \| null` | IANA 시간대, 예: `"Asia/Shanghai"` — `kind: "cron"`과만 함께 사용 가능 |

**`payload`**

| 필드 | 타입 | 설명 |
|------|------|------|
| `kind` | `"agent_turn" \| "system_event"` | 페이로드 유형 (기본 `"agent_turn"`; 서비스나 API를 통해 추가되는 작업은 항상 `agent_turn`) |
| `message` | `str` | 에이전트에 전달할 프롬프트 메시지 |
| `deliver` | `bool` | 전달 플래그 (기본 `false`; 위 참고 사항 확인 — 현재 실행 경로에서는 읽히지 않음) |
| `channel` | `str \| null` | 채널 이름, 예: `"qq"` |
| `to` | `str \| null` | 수신자 식별자 (`chat_id`로 사용) |

**`state`**

| 필드 | 타입 | 설명 |
|------|------|------|
| `nextRunAtMs` | `int \| null` | 다음 예정 실행 시각 (밀리초); 비활성·만료된 작업은 `null` |
| `lastRunAtMs` | `int \| null` | 마지막 실행 시작 시각 (밀리초) |
| `lastStatus` | `"ok" \| "error" \| "skipped" \| null` | 마지막 실행 결과 |
| `lastError` | `str \| null` | 마지막 오류 메시지 |

Python 쪽 대응 모델(`types.py`)은 snake_case를 사용합니다 (`at_ms`, `every_ms`, `next_run_at_ms`, `last_run_at_ms`, `last_status`, `last_error`, `created_at_ms`, `updated_at_ms`, `delete_after_run`). `CronRunRecord`는 내보내기에는 포함되지만 현재 사용되지 않습니다.

## 공개 API

### 에이전트 스킬 명령 (`Cron` 퍼사드, `core.py`의 `cron` 싱글턴)

다음은 [`../SKILL.md`](../SKILL.md)를 통해 에이전트에 노출되는 명령입니다. `from skills.builtin.core.cron.scripts import cron`으로 사용합니다:

| 명령 | 설명 |
|------|------|
| `cron.set_context(channel, chat_id)` | 세션 컨텍스트 설정 (둘 다 필수이며 비어 있으면 안 됨). 이후 추가되는 작업의 전달 대상이 됨 |
| `cron.add_job(name=None, message, every_seconds=None, cron_expr=None, tz=None, at=None, deliver=True)` | 작업 추가. `every_seconds` / `cron_expr` / `at` (ISO 날짜시간) 중 하나는 반드시 필요. 사전에 `set_context` 필요. `tz`는 `cron_expr`와만 함께 사용 가능 (기본 `"UTC"`); 시간대 정보가 없는 `at`은 UTC로 간주되며, `at` 작업에는 `delete_after_run=True`가 설정됨. `name`의 기본값은 `message`의 앞 30자 |
| `cron.list_jobs()` | 사람이 읽을 수 있는 작업 목록: 스케줄 시각, 시스템 작업의 용도와 보호 플래그, 마지막/다음 실행 시각 |
| `cron.remove_job(job_id)` | 작업 제거. 보호된 시스템 작업에는 친절한 오류 메시지를 반환 |

### `CronService` (Python API, `base.py`의 `cron_service` 싱글턴)

| 메서드 | 설명 |
|--------|------|
| `await start()` | 저장소를 로드하고, 다음 실행 시각을 재계산해 저장한 뒤 타이머를 무장 |
| `stop()` | 서비스를 중지하고 타이머 태스크를 취소 |
| `set_on_job(callback)` | 비동기 실행 콜백 등록 (`init()`이 `_on_cron_job`에 연결) |
| `list_jobs(include_disabled=False)` | 다음 실행 시각 순으로 작업 나열; `include_disabled=True`일 때만 비활성 작업 포함 |
| `add_job(name, schedule, message, deliver=False, channel=None, to=None, delete_after_run=False)` | 작업 추가 (`payload.kind`는 항상 `"agent_turn"`); 서비스 자동 시작; `CronJob` 반환 |
| `register_system_job(job)` | `id`를 기준으로 시스템 작업을 멱등하게 (재)등록 (현재 저장소 내 호출부 없음) |
| `remove_job(job_id)` | `"removed"`, `"protected"` (`payload.kind == "system_event"`), `"not_found"` 중 하나 반환 |
| `enable_job(job_id, enabled=True)` | 활성/비활성화; `nextRunAtMs`를 재계산하거나 비움 |
| `await run_job(job_id, force=False)` | 즉시 실행; 비활성 작업은 `force=True`가 없으면 건너뜀 |
| `get_job(job_id)` | ID로 작업 조회, 없으면 `None` |
| `status()` | `{"enabled": bool, "jobs": int, "next_wake_at_ms": int \| None}` 반환 |

### HTTP REST API (`server/trigger/http/cron.py`, 백엔드 `http://127.0.0.1:8080`)

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /cron?include_disabled=false` | 작업 목록 (camelCase JSON) |
| `POST /cron` | 생성: `{"name", "message", "schedule": {"kind", "atMs"/"everyMs"/"expr"/"tz"}, "deliver", "channel", "to", "delete_after_run"}` |
| `PUT /cron` | 수정: 제거 + 재추가로 적용되며 `id`와 `createdAtMs`는 유지됨 |
| `POST /cron/trigger` | 즉시 실행: `{"id", "force"}` (비활성 상태이고 `force`가 없으면 400) |
| `POST /cron/enable` | 활성/비활성화: `{"id", "enabled"}` |
| `DELETE /cron` | 제거: `{"id"}`; 보호된 시스템 작업은 `403` |

## 사용 예시

에이전트 스킬 스크립트 ([`../SKILL.md`](../SKILL.md) 참조):

```python
from loguru import logger
from skills.builtin.core.cron.scripts import cron

# 작업을 추가하기 전에 세션 컨텍스트를 한 번 설정해야 합니다
cron.set_context(channel="qq", chat_id="group_123456")

# cron 표현식 작업: 매일 상하이 시간 9시
res = cron.add_job(
    name="daily_digest",
    message="Summarize today's schedule and important events",
    cron_expr="0 9 * * *",
    tz="Asia/Shanghai",
)
logger.info(res)

# 고정 간격 작업: 30분마다
res = cron.add_job(
    message="Check today's weather and remind user to bring an umbrella if needed",
    every_seconds=30 * 60,
)

# 일회성 작업: 명시적 ISO 날짜시간
res = cron.add_job(message="Say good morning to the user", at="2026-02-12T10:30:00")

logger.info(cron.list_jobs())
# cron.remove_job("a1b2c3d4")
```

Python API:

```python
from skills.builtin.core.cron.scripts import cron_service, CronSchedule

# 서비스는 처음 사용 시 자동 시작됩니다. 명시적 start는 선택 사항입니다
await cron_service.start()

job = cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

jobs = cron_service.list_jobs()
print([j.name for j in jobs])

await cron_service.run_job(job.id, force=True)   # 수동 트리거
cron_service.remove_job(job.id)                   # "removed" | "protected" | "not_found"
```

HTTP:

```bash
curl http://127.0.0.1:8080/cron
curl -X POST http://127.0.0.1:8080/cron -H "Content-Type: application/json" \
  -d '{"name": "daily_digest", "message": "Summarize today", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}}'
curl -X POST http://127.0.0.1:8080/cron/trigger -H "Content-Type: application/json" -d '{"id": "a1b2c3d4", "force": true}'
```

## 스케줄링 의미론

| 유형 | 동작 |
|------|------|
| `at` | `atMs`에 지정된 시각에 한 번 실행. 계산 시점에 타임스탬프가 과거이면 `nextRunAtMs`가 `null`이 되어 작업은 실행되지 않음. 실행 후 삭제(`deleteAfterRun=true`)되거나 비활성화(`enabled=false`, `nextRunAtMs=null`)됨 |
| `every` | 다음 실행 = 현재 시각 + `everyMs`. 실행할 때마다 재계산됨 |
| `cron` | `croniter`가 표현식에서 다음 실행 시각을 계산. 기준 시각은 `tz` 지정 시 해당 시간대로, 없으면 시스템 로컬 시간대로 평가됨 |

검증 규칙: `tz`는 `kind: "cron"`일 때만 허용됨. 알 수 없는 IANA 시간대 이름은 거부됨(`ValueError`). 서비스 계층과 퍼사드 계층 모두 동일합니다.

## 보호된 시스템 작업

`payload.kind == "system_event"`인 작업은 보호됩니다: `CronService.remove_job()`은 제거를 거부하고(`"protected"`, HTTP `DELETE /cron`은 `403`), 스킬 계층은 추가로 `dream`이라는 이름의 작업을 인식하여 장기 기억을 위한 Dream 기억 통합 작업으로 설명합니다. `add_job` (Python, 스킬, HTTP 모두)으로 추가되는 작업은 항상 `agent_turn`입니다. `system_event` 작업은 `register_system_job()` 또는 `cron_jobs.json` 직접 편집을 통해서만 생성될 수 있습니다.

## 의존성

- `croniter>=6.2.2` — cron 표현식 파싱
- Python `zoneinfo` — 시간대 지원
- `config/`에 cron 전용 설정 항목은 없으며, cron 관련 환경 변수도 존재하지 않음

## 참고 사항

- 실행 기록: 실행할 때마다 `logs/output/cron/<job_id>.log`에 JSON 한 줄이 추가됩니다 (`timestamp`, `job_id`, `job_name`, `start_time`, `end_time`, `duration_ms`, `status`, `error`, `message`). 메모리상의 실행 기록은 없습니다 (`CronRunRecord`는 미사용 레거시).
- `cron_jobs.json`의 외부 수정은 파일 수정 시간으로 감지되어 다음 타이머 틱에 반영되며, 저장소는 실행마다 다시 저장됩니다.
- 서비스는 `cron-service` 데몬 스레드의 독립적인 이벤트 루프에서 동작하며 메인 서버 루프와 별개입니다. `run_job()`과 `start()`는 실행 중인 이벤트 루프에서 await해야 합니다.
- WebSocket 알림은 세션 `"default"`(브라우저 클라이언트 세션)를 대상으로 하므로, 클라이언트가 연결된 동안에만 데스크톱 알림이 도착합니다.
