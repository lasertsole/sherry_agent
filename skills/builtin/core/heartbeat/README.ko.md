# Heartbeat — 주기적 작업 확인 서비스

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat**는 EMA AI Agent의 주기적 웨이크업 서비스입니다. 매 tick마다 [`workspace/HEARTBEAT.md`](../../../../workspace/HEARTBEAT.md)를 읽고 보조 LLM이 활성 작업의 존재 여부를 판단합니다. 작업이 있으면 전용 에이전트 실행으로 처리하고, 그 결과를 알림 게이트를 통해 전달합니다.

---

## 동기

대화가 종료된 후에도 에이전트는 유휴 상태이지만 외부에는 남은 작업이 있을 수 있습니다:
- 실행을 기다리는 작업 (에이전트나 사용자가 HEARTBEAT.md에 기록)
- 주기적 확인이 필요한 모니터링 작업
- 지속적 진행이 필요한 장기 실행 작업

Heartbeat는 유휴 시간 동안 에이전트가 자발적으로 작업할 수 있게 하는 **경량 폴링 메커니즘**을 제공합니다.

---

## 아키텍처

```
┌──────────────────────────────────────────┐
│            HeartbeatService              │
├──────────────────────────────────────────┤
│  asyncio loop (sleep backoff → tick)     │
│  ├─ Phase 1: Read HEARTBEAT.md           │
│  ├─ Phase 2: LLM decision (skip/run)     │
│  └─ Phase 3: Execute + notification gate │
└──────────────────────────────────────────┘
```

### 모듈 책임

| 파일 | 책임 |
|------|---------------|
| [`scripts/base.py`](scripts/base.py) | `HeartbeatService` 클래스: asyncio 루프, LLM 결정(`_decide`), tick 파이프라인. 모듈 수준 `heartbeat_service` 싱글턴 |
| [`scripts/core.py`](scripts/core.py) | HEARTBEAT.md 관리: `ensure_heartbeat_file_exists`, `add_task_to_heartbeat`, `list_active_tasks`, `list_completed_tasks`, `move_task_to_completed`, `remove_tasks_from_completed` / `clear_completed_tasks` |
| [`scripts/evaluate.py`](scripts/evaluate.py) | `evaluate_response()`: 결과를 전달할 가치가 있는지 판단하는 알림 게이트 |
| [`server/service/heartbeat.py`](../../../../server/service/heartbeat.py) | 통합 계층: `process_heartbeat_task`(실행 에이전트), `process_heartbeat_notify`(채널 전달), 파일 읽기/쓰기 헬퍼 |
| [`server/trigger/channels/core.py`](../../../../server/trigger/channels/core.py) | `on_execute` / `on_notify`를 연결하고 채널 매니저의 이벤트 루프에서 서비스 시작 |

---

## HEARTBEAT.md 파일

- 위치는 `workspace/HEARTBEAT.md` — [`config/path.py`](../../../../config/path.py)의 `HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"`.
- 파일이 존재하지 않으면 `ensure_heartbeat_file_exists()`가 언어 비의존적 템플릿 `workspace/template/HEARTBEAT.md`를 복사해 옵니다.
- 골격 포맷 (`workspace/HEARTBEAT.md`와 동일):

```markdown
# Heartbeat Tasks

## Active Tasks

## Completed
```

파싱 규칙 (`scripts/core.py`에 구현):
- 각 섹션은 `## Active Tasks` / `## Completed`의 **행 전체 정확히 일치**로 찾습니다(섹션을 찾지 못하면 `ValueError`).
- 섹션의 **콘텐츠 행**은 `<!--`(HTML 주석)으로 시작하지 않는 비어 있지 않은 행이며, 다음 `##` 제목 또는 파일 끝까지를 대상으로 합니다.
- 작업은 Markdown 리스트 항목입니다. `add_task_to_heartbeat()`는 `-`로 시작하지 않는 텍스트에 `- [ ] ` 접두사를 붙입니다.
- 서버 쓰기 API에는 작업 텍스트 상한 `HEARTBEAT_MAX_CONTENT_LENGTH = 2000`자가 있습니다(제목·빈 행·`- ` 마커는 제외. `heartbeat_content_length()`와 동일한 방식).

---

## 워크플로우

```
start() → asyncio task
   └─ loop: sleep(backoff.current_interval) → tick()   # first tick happens after one full interval
        ↓
   Read HEARTBEAT.md (empty/missing → skip tick)
        ↓
   _decide() — auxiliary LLM, virtual tool call:
     ├─ "skip" → log OK, wait for next tick
     └─ "run"  → on_execute(tasks)         # server: one-shot main-LLM agent
                    ↓
              response non-empty → evaluate_response():
                ├─ True  → on_notify(response)   # server: channel delivery
                └─ False → silenced (logged)

tick 예외 → backoff.record_failure(): 다음 sleep은 2배 (interval_s × 2ⁿ,
상한 7200초). 연속 5회 실패 시 루프 종료 (CRITICAL 로그).
tick 성공 → backoff.record_success(): interval_s로 완전 리셋.
```

### Phase 1: 읽기

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

- 파일이 비어 있음 → 해당 tick은 건너뜀(디버그 로그).
- 파일이 없음 → `read_text()`가 `FileNotFoundError`를 발생시키며, 루프는 오류를 기록하고 다음 주기로 계속 진행합니다. 이는 백오프 실패로 **기록되지 않습니다**(파일 읽기는 tick의 백오프 집계 `try/except` 밖에 있습니다).

### Phase 2: 결정 (`_decide`)

보조 LLM(`models`의 `build_auxiliary_llm()`)은 현재 시각(`current_time_str(self.timezone)`)과 HEARTBEAT.md 전체 내용을 받아 **가상 tool-call**로 판단을 보고합니다. 신뢰할 수 없는 자유 텍스트 파싱을 회피합니다:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "description": "Report heartbeat decision after reviewing tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["skip", "run"],
                    "description": "skip = nothing to do, run = has active tasks",
                },
                "tasks": {
                    "type": "string",
                    "description": "Natural-language summary of active tasks (required for run)",
                },
            },
            "required": ["action"],
        },
    },
}]
```

- 우선 `bind_tools` 경로를 시도합니다. `tool_calls`가 비어 있으면 `skip`으로 처리합니다.
- `NotImplementedError`(도구를 지원하지 않는 로컬 GGUF 계열 등)나 그 밖의 예외 발생 시 `with_structured_output(_HeartbeatDecision)`(`action` 필드가 `^(skip|run)$` 패턴으로 제한된 Pydantic 모델)으로 폴백합니다. 이마저 실패하면 기본값 `("skip", "")`을 반환합니다.

### Phase 3: 실행 및 알림 게이트

`_tick()`의 실제 로직 (`scripts/base.py`):

```python
action, tasks = self._decide(content)
if action != "run":
    return  # "Heartbeat: OK (nothing to report)"

if self.on_execute:
    response: str = await self.on_execute(tasks)
    if response:
        should_notify: bool = evaluate_response(response, tasks)
        if should_notify and self.on_notify:
            await self.on_notify(response)
        # else: "Heartbeat: silenced by post-run evaluation"
```

- `run` → `on_execute(tasks)`가 작업을 실행합니다. **비어 있지 않은** 응답만 `evaluate_response()`로 평가되며, 긍정 판정일 때만 `on_notify()`에 도달합니다.
- tick 내부의 예외는 기록되고(`logger.exception`) **백오프 실패**로 집계됩니다: 다음 sleep은 2배(interval_s × 2ⁿ, 상한 7200초)가 되며 실패 사유가 유지됩니다. tick이 성공하면 백오프는 완전히 리셋됩니다.
- **연속 5회** 실패 시 루프가 스스로 멈추고 CRITICAL 로그를 남깁니다("Heartbeat paused ... manual recovery required"). 일정 재개는 프로세스 재시작뿐입니다. `trigger_now()`는 여전히 1회성 tick을 실행할 수 있습니다. [`runtime/periodic_backoff.py`](../../../../runtime/periodic_backoff.py)와 [무한 루프 방지 문서](../../../../docs/harness/loop-prevention/README.md)를 참조하세요.

---

## HEARTBEAT.md 작업 관리 API (`scripts/core.py`)

에이전트 대상 함수들로, [SKILL.md](SKILL.md)를 통해 모델에 노출됩니다:

| 함수 | 동작 |
|---|---|
| `ensure_heartbeat_file_exists()` | 파일이 없으면 `workspace/template/HEARTBEAT.md`를 `workspace/HEARTBEAT.md`로 복사 |
| `add_task_to_heartbeat(task_text, index=None)` | `## Active Tasks` 아래에 작업 추가. 리스트 항목이 아닌 텍스트에는 `- [ ] ` 접두사를 붙임. `index`는 섹션 콘텐츠 행 기준 0-시작 삽입 위치(범위를 벗어나면 `IndexError`). `None`이면 끝에 추가 |
| `list_active_tasks()` / `list_completed_tasks()` | `## Active Tasks` / `## Completed`의 콘텐츠 행을 반환 |
| `move_task_to_completed(task_text)` | Active Tasks의 행과 부분 문자열 매칭(공백 제거 후 비교). 첫 번째 매칭 행을 제거하고 `## Completed` 끝에 추가(해당 섹션이 비어 있으면 제목 바로 뒤). 매칭 없음 → `ValueError` |
| `remove_tasks_from_completed(task_text=None)` | `None` → 콘텐츠 행 **전체** 제거. `str` / `list[str]` → 부분 문자열 매칭으로 제거(하나도 매칭되지 않으면 `ValueError`). 처리 후 섹션 내 연속된 빈 행을 압축 |
| `clear_completed_tasks(task_text=None)` | `remove_tasks_from_completed`의 별칭 |

이 함수들은 모두 `skills.builtin.core.heartbeat.scripts`에서 내보내지며, 패키지 `skills.builtin.core.heartbeat` 자체는 `heartbeat_service` 싱글턴만 재노출합니다.

---

## 서버 통합

서비스는 채널 계층의 `server/trigger/channels/core.py`가 연결과 시작을 담당합니다:

```python
heartbeat_service.on_execute = _process_heartbeat_task   # → server.service.process_heartbeat_task
heartbeat_service.on_notify = _process_heartbeat_notify  # → server.service.process_heartbeat_notify
asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)  # channel manager loop
```

**실행 (`process_heartbeat_task`)**:
1. `ensure_workspace_system_files()`가 핵심 페르소나 파일의 존재를 보장합니다.
2. 일회성 `create_agent(model=build_main_llm(), tools=[python_repl, read_file, write_file])`를 구성합니다. 시스템 프롬프트는 핵심 페르소나(`build_system_prompt(selected_file_names=CORE_SYSTEM_FILE_NAMES)`), 작업 요약은 `HumanMessage`로 전달됩니다.
3. 마지막 메시지의 내용을 실행 결과로 삼습니다.
4. 실행된 작업을 Active → Completed로 이동합니다. 먼저 `move_task_to_completed(task)`를 시도하고, `ValueError`(작업 텍스트 불일치)가 발생하면 남은 **모든** 활성 작업을 이동하는 폴백을 실행합니다.
5. 세션 `default`로 두 개의 best-effort WebSocket 이벤트를 전송합니다: `heartbeat:updated`(갱신된 파일 내용)와 `notification`(`heartbeat: ` 접두사가 붙은 결과). 실패는 기록만 될 뿐 다시 발생하지 않습니다. 내부 예외 시 함수는 `"Error occurred: {e}"`를 반환합니다.

계층 구조에 유의: 위 WebSocket 이벤트는 `process_heartbeat_task`가 성공할 때마다 전송하는 것이고, **채널 전달**(아래)이야말로 `evaluate_response()` 게이트가 실제로 제어하는 대상입니다.

**전달 (`process_heartbeat_notify`)**: `plugins/channels/config.json`을 읽고, 설정에 `"heartbeat": true`를 가지며 `receiver`를 해석할 수 있는 채널(`plugins/channels/<name>/config.json`에서 가져오며 루트 블록으로 폴백)이 `channel_manager.get_channel(name).send(OutboundMessage(...))`로 결과를 받습니다.

**HTTP API** (`server/trigger/http/heartbeat.py`): `GET /heartbeat`는 `{"HEARTBEAT.md": "<content>"}`를 반환하고(파일이 없으면 빈 dict), `PUT /heartbeat`는 `{"file_to_content": {"HEARTBEAT.md": "..."}}`를 받아 2000자 작업 텍스트 상한을 강제합니다.

---

## 사용 예시

### 기본 사용법 (싱글턴)

```python
from skills.builtin.core.heartbeat import heartbeat_service

heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier        # async (response: str) -> None

await heartbeat_service.start()  # 기본 간격: 1800초 (30분)
```

실제 운영에서는 이 연결이 `server/trigger/channels/core.py`에 있으며, 채널 매니저의 이벤트 루프에서 실행됩니다.

### 수동 트리거

```python
result = await heartbeat_service.trigger_now()
```

`trigger_now()`는 파일을 읽고 `_decide`를 실행하며, `run`이면 `on_execute(tasks)`를 기다립니다. 알림 게이트는 **거치지 않고**, `on_notify`도 **호출하지 않습니다**. 파일이 비어 있거나, 판단이 `skip`이거나, `on_execute`가 설정되지 않았으면 `None`을 반환합니다.

### 사용자 지정 구성

```python
from skills.builtin.core.heartbeat.scripts.base import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15분
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

(`HeartbeatService` 클래스는 `scripts/base.py`에 정의되어 있습니다. 패키지의 `__init__.py`에서는 재노출되지 않습니다.)

### 중지

```python
heartbeat_service.stop()  # _running = False로 설정하고 asyncio 태스크를 취소
```

---

## 구성

| 매개변수 | 기본값 | 설명 |
|-----------|---------|-------------|
| `interval_s` | `30 * 60` (1800초) | tick 사이의 초 단위 간격. 루프는 각 tick **전에 sleep하므로** 첫 확인은 `start()` 후 한 주기 뒤에 발생합니다. 실패 백오프의 기준 간격이기도 합니다 |
| 실패 백오프 | `factor=2.0`, 상한 `7200초`, `5`회 후 중지 | `HeartbeatService.__init__`에 하드코딩된 `PeriodicBackoff` 매개변수(`runtime/periodic_backoff.py`). 연속 tick 실패 시 sleep이 최대 2시간까지 늘어나고, 이후에는 재시작까지 서비스가 중지됩니다 |
| `enabled` | `True` | `False`이면 `start()`가 "Heartbeat disabled"를 기록하고 아무것도 하지 않습니다 |
| `timezone` | `None` | 결정 프롬프트의 "Current Time" 행을 위해 `current_time_str()`에 전달됩니다 |
| `on_execute` / `on_notify` | `None` | 비동기 콜백. 설정되지 않으면 실행 / 전달이 건너뛰어집니다 |
| `HEARTBEAT_PATH` | `workspace/HEARTBEAT.md` | `config/path.py`에 정의 |
| `HEARTBEAT_TEMPLATE_PATH` | `workspace/template/HEARTBEAT.md` | `ensure_heartbeat_file_exists()`의 템플릿 소스 |
| `HEARTBEAT_MAX_CONTENT_LENGTH` | `2000` | 서버 쓰기 API(`write_heartbeat_file`)가 강제하는 작업 텍스트 상한 |

---

## 알림 게이트 전략

`scripts/evaluate.py`의 `evaluate_response(response, task_context)`는 가상 `evaluate_notification` 도구(`should_notify` 불리언·필수, `reason` 문자열)로 보조 LLM에게 판단을 요청합니다. 시스템 프롬프트의 내용:

| 알림 (`should_notify: true`) | 억제 (`should_notify: false`) |
|--------------------------------|-----------------------------------|
| 실행 가능한 정보 | 새로운 소식이 없는 일상적 상태 확인 |
| 오류 | 모든 것이 정상이라는 확인 |
| 완료된 산출물 | 사실상 비어 있는 응답 |
| 사용자가 명시적으로 알림을 요청한 사항 | |

실패 시 동작: tool-call이 반환되지 않거나 예외가 발생하면 **`True`(알림)**가 되어, 중요한 메시지가 조용히 유실되지 않도록 보장합니다. `_decide`와 달리 `with_structured_output` 폴백은 없습니다.

---

## 혼동 주의: `HeartbeatStaleness` 미들웨어

[`agent/middlewares/heartbeat_staleness.py`](../../../../agent/middlewares/heartbeat_staleness.py)는 "heartbeat"라는 이름을 공유하지만 **전혀 다른 하위 시스템**입니다. 에이전트 턴이 멈추는 것을 감지하는 턴 단위 워치독으로, `before_agent`에서 `timer_call_register`를 통해 1분 타이머를 시작하고 `(heartbeat_iter, heartbeat_tool)` 진행 상황을 추적합니다. 유휴 상태에서 7주기, 도구 실행 중 20주기 동안 진행이 없으면 해당 턴을 killed로 표시하여 다음 모델/도구 호출이 `HeartbeatTimeoutError`를 발생시킵니다. HEARTBEAT.md를 읽지 않으며 본 서비스의 일부가 아닙니다.

---

## 기술 스택

| 컴포넌트 | 기술 |
|-----------|-----------|
| 런타임 | Python asyncio (`asyncio.create_task`로 구동되는 단일 `asyncio.Task` 루프) |
| 결정과 게이트 | 보조 LLM (`models`의 `build_auxiliary_llm()`), LangChain 가상 tool-call (`bind_tools`). `_decide`에는 `with_structured_output` 폴백 존재 |
| 파일 I/O | `pathlib` |
| 로깅 | `loguru` |
| 검증 | Pydantic (`_HeartbeatDecision` 폴백 모델) |
| 경로 | `config.path` (`HEARTBEAT_PATH`, `HEARTBEAT_TEMPLATE_PATH`) |
