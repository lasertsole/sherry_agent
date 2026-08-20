# Heartbeat — 주기적 작업 확인 서비스

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat**는 EMA AI Agent의 주기적 웨이크업 서비스로, 정기적으로 `HEARTBEAT.md`를 확인하여 보류된 작업이 있는지 검사하고 자동으로 실행 및 알림을 수행합니다.

---

## 동기

대화가 종료된 후에도 외부에서 해야 할 작업이 남아 있는 동안 에이전트가 유휴 상태일 수 있습니다:
- 결과를 기다리는 백그라운드 작업 (비동기 도구 호출)
- 주기적 확인이 필요한 모니터링 작업
- 지속적 진행이 필요한 장기 실행 작업

Heartbeat는 유휴 시간 동안 에이전트가 자발적으로 작업할 수 있게 하는 **경량 폴링 메커니즘**을 제공합니다.

---

## 아키텍처

```
┌─────────────────────────────────────┐
│          HeartbeatService            │
├─────────────────────────────────────┤
│  Loop (tick every N seconds)         │
│  ├─ Phase 1: Read HEARTBEAT.md       │
│  ├─ Phase 2: LLM decide (skip/run)   │
│  └─ Phase 3: Execute + notification gate │
└─────────────────────────────────────┘
```

### 모듈 책임

| 파일 | 책임 |
|------|---------------|
| `core.py` | 메인 서비스: 루프, LLM 결정, 작업 실행 트리거 |
| `evaluate.py` | 알림 게이트: 결과가 전달할 가치가 있는지 결정 |

---

## 워크플로우

```
Timer fires (default 30 min)
     ↓
Read HEARTBEAT.md
     ↓
LLM (tool-call) decision:
  ├─ "skip" → no tasks, wait for next tick
  └─ "run" → execute via on_execute callback
                   ↓
              evaluate_response():
                ├─ True  → on_notify pushes result to user
                └─ False → silent (routine check, nothing new)
```

### Phase 1: 읽기

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

`HEARTBEAT_PATH`는 `config.py`에서 구성되며 프로젝트의 `HEARTBEAT.md`를 가리킵니다. 파일이 없거나 비어 있으면 해당 tick은 건너뜁니다.

### Phase 2: 결정

**가상 tool-call**을 사용하여 LLM이 활성 작업이 있는지 판단하게 하여, 신뢰할 수 없는 자유 텍스트 파싱을 방지합니다:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "parameters": {
            "action": {"enum": ["skip", "run"]},
            "tasks": {"type": "string"},  # task summary when run
        },
        "required": ["action"],
    },
}]
```

`skip` → 작업 없음; `run` → Phase 3으로 진행.

### Phase 3: 실행 및 알림 게이트

```python
if action == "run" and self.on_execute:
    response = await self.on_execute(tasks)           # execute task
    should_notify = evaluate_response(response, tasks) # evaluate notification
    if should_notify and self.on_notify:
        await self.on_notify(response)                 # push to user
```

`evaluate_response()`는 독립적인 LLM tool-call을 사용하여 응답에 **실행 가능한 정보**(오류, 산출물, 사용자가 요청한 결과)가 포함되었는지 판단하여 일상적인 상태 업데이트를 억제합니다.

---

## 사용 예시

### 기본 사용법

```python
from skills.builtin.core.heartbeat import heartbeat_service

# 콜백 구성
heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier  # async (response: str) -> None

# 시작 (기본 30분 간격)
await heartbeat_service.start()
```

### 수동 트리거

```python
result = await heartbeat_service.trigger_now()
if result:
    print(f"Task result: {result}")
```

### 사용자 지정 구성

```python
from skills.builtin.core.heartbeat import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 minutes
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

### 중지

```python
heartbeat_service.stop()
```

---

## 구성

| 매개변수 | 기본값 | 설명 |
|-----------|---------|-------------|
| `interval_s` | 1800 (30 min) | tick 사이의 간격 |
| `enabled` | True | Heartbeat 서비스 활성화 |
| `timezone` | None | LLM 결정용 시간대 (예: "Asia/Shanghai") |
| `HEARTBEAT_PATH` | config.py 참조 | HEARTBEAT.md 경로 |

---

## 알림 게이트 전략

`evaluate_response()` 결정 로직:

| 알림 | 억제 |
|--------|----------|
| 오류 또는 예외 | 일상적 확인, 특이 사항 없음 |
| 작업 산출물 완료 | 모든 것이 정상이라는 확인 |
| 사용자가 명시적으로 요청한 정보 | 응답이 비어 있거나 관련 없음 |

실패 시 기본값 `True`(알림)로 설정되어 중요한 메시지가 자동으로 유실되지 않도록 보장합니다.

---

## 기술 스택

| 컴포넌트 | 기술 |
|-----------|-----------|
| 런타임 | Python asyncio |
| LLM 결정 | `auxiliary_llm` (bind_tools) |
| 파일 I/O | pathlib |
| 구성 | `config.HEARTBEAT_PATH` |
