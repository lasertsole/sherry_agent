# EMA Cron — 예약 작업 서비스

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent 시스템 내에서 주기적, 일회성, cron 표현식 기반 에이전트 작업을 예약 및 실행하기 위한 경량 파일 기반 cron 서비스 모듈입니다.

## 기능

- 세 가지 스케줄 유형: `at` (일회성), `every` (간격), `cron` (cron 표현식)
- 파일 기반 영속 저장 (`jobs.json`) 및 외부 수정 시 자동 재로드
- 밀리초 정밀도의 타이머 기반 실행
- 작업별 실행 기록 (최근 20개 레코드 유지)
- 보호된 시스템 작업 (API로 제거 불가)
- cron 표현식용 시간대 지원
- 외부 채널로의 메시지 전달 구성 가능 (예: QQ, WhatsApp)

## 모듈 구조

```
cron/
├── __init__.py    # 공개 내보내기: CronService, cron_service, types
├── core.py        # 핵심 구현: CronService, 작업 실행, 타이머 루프
├── types.py       # 데이터 모델: CronSchedule, CronPayload, CronJob 등
├── jobs.json      # 영속 작업 저장소 (자동 관리)
└── README.md      # 이 파일
```

## 타입 참고

### CronSchedule

작업이 실행될 시기를 정의합니다.

| 필드      | 타입   | 설명 |
|-----------|--------|-------------|
| `kind`    | `"at" \| "every" \| "cron"` | 스케줄 유형 |
| `at_ms`   | `int \| None` | "at"용 밀리초 Unix 타임스탬프 |
| `every_ms`| `int \| None` | "every"용 밀리초 간격 |
| `expr`    | `str \| None` | "cron"용 cron 표현식, 예: `"0 9 * * *"` |
| `tz`      | `str \| None` | 시간대, 예: `"Asia/Shanghai"`. "cron"에서만 사용 |

### CronPayload

작업이 실행될 때 수행할 동작을 정의합니다.

| 필드      | 타입            | 설명 |
|-----------|-----------------|-------------|
| `kind`    | `"system_event" \| "agent_turn"` | 페이로드 유형 |
| `message` | `str`           | 에이전트로 보낼 프롬프트 메시지 |
| `deliver` | `bool`          | 결과를 외부 채널로 전달할지 여부 |
| `channel` | `str \| None`   | 채널 이름 (예: `"whatsapp"`, `"qq"`) |
| `to`      | `str \| None`   | 수신자 식별자 |

### CronJob

완전한 작업 정의입니다.

| 필드               | 타입            | 설명 |
|---------------------|-----------------|-------------|
| `id`                | `str`           | 고유 작업 ID (자동 생성) |
| `name`              | `str`           | 사람이 읽을 수 있는 이름 |
| `enabled`           | `bool`          | 작업 활성 여부 |
| `schedule`          | `CronSchedule`  | 스케줄 정의 |
| `payload`           | `CronPayload`   | 동작 정의 |
| `delete_after_run`  | `bool`          | 일회성 실행 후 자동 삭제 |

## 공개 API

### `CronService` (`cron_service`를 통한 싱글턴)

| 메서드 | 설명 |
|--------|-------------|
| `start()` | cron 서비스 시작 |
| `stop()` | cron 서비스 중지 |
| `list_jobs(include_disabled=False)` | 모든 작업 나열 |
| `add_job(name, schedule, message, ...)` | 새 작업 추가 |
| `register_system_job(job)` | 보호된 시스템 작업 등록 |
| `remove_job(job_id)` | 작업 제거 |
| `enable_job(job_id, enabled=True)` | 작업 활성/비활성화 |
| `run_job(job_id, force=False)` | 작업 수동 트리거 |
| `get_job(job_id)` | 작업 세부 정보 가져오기 |
| `status()` | 서비스 상태 가져오기 |

## 사용 예시

```python
from cron import cron_service, CronSchedule

# 서비스 시작
await cron_service.start()

# 일회성 작업: 특정 시간에 실행
cron_service.add_job(
    name="morning_greeting",
    schedule=CronSchedule(kind="at", at_ms=1700000000000),
    message="Say good morning to the user",
    deliver=True,
    channel="qq",
    to="group_123456",
    delete_after_run=True,
)

# 간격 작업: 매 30분마다 실행
cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

# Cron 작업: 상하이 시간 매일 오전 9시 실행
cron_service.add_job(
    name="daily_digest",
    schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
    message="Summarize today's schedule and important events",
)

# 모든 작업 나열
jobs = cron_service.list_jobs()
for j in jobs:
    print(f"{j.name}: next run at {j.state.next_run_at_ms}")

# 작업 수동 트리거
await cron_service.run_job("job_id_here", force=True)

# 작업 제거
cron_service.remove_job("job_id_here")
```

## 작업 영속성

모든 작업은 `jobs.json`에 영속 저장됩니다. 파일은 서비스 시작 시 자동 로드되며, 외부 수정이 감지되면(파일 수정 시간 비교를 통해) 자동 재로드됩니다. `jobs.json`을 직접 편집하여 작업을 일괄 추가하거나 수정할 수 있습니다 — 서비스가 다음 tick에서 변경 사항을 반영합니다.

## 스케줄링 의미

| 종류 | 동작 |
|------|----------|
| `at` | 지정된 타임스탬프에 한 번 실행. 실행 후 비활성화 (또는 `delete_after_run=True`인 경우 삭제) |
| `every` | 각 완료로부터 고정된 `every_ms` 간격으로 재실행 |
| `cron` | `croniter`를 사용해 주어진 시간대의 cron 표현식에서 다음 실행 시간 계산 |

## 의존성

- `croniter` — cron 표현식 파싱
- Python `zoneinfo` — 시간대 지원

## 참고 사항

- 일회성 (`at`) 작업은 실행 후 기본적으로 **비활성화**(삭제 아님)됩니다. 자동 삭제하려면 `delete_after_run=True`로 설정하세요.
- 시스템 작업 (`payload.kind == "system_event"`)은 보호되며 `remove_job()`으로 제거할 수 없습니다.
- cron 서비스는 asyncio 이벤트 루프에 의존합니다 — `await cron_service.start()` 호출 시 응용 프로그램이 이벤트 루프를 실행 중인지 확인하세요.
