# HITL(Human-In-The-Loop) 미들웨어

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

hermes-agent 파이프라인을 위한 종합적인 인간-참여(human-in-the-loop) 미들웨어입니다. 명령 실행(하드라인/위험), 파일 쓰기, MCP 도구 호출, 파괴적 슬래시 명령, 피어 페어링에 대한 계층형 승인 게이트를 제공하며, 모두 단일 미들웨어 훅을 통해 관리됩니다.

---

## 목차

- [아키텍처 개요](#아키텍처-개요)
- [레이어 레퍼런스](#레이어-레퍼런스)
  - [1. 하드라인 및 위험 감지](#1-하드라인-및-위험-감지)
  - [2. 쓰기 승인 게이트](#2-쓰기-승인-게이트)
  - [3. 인터럽트 매니저](#3-인터럽트-매니저)
  - [4. MCP 유발 동의](#4-mcp-유발-동의)
  - [5. 칸반 트리아지](#5-칸반-트리아지)
  - [6. 스마트 승인](#6-스마트-승인)
  - [7. 페어링 저장소](#7-페어링-저장소)
  - [8. 슬래시 확인](#8-슬래시-확인)
- [미들웨어 훅](#미들웨어-훅)
- [설정](#설정)
- [승인 훅 시스템](#승인-훅-시스템)
- [파일 구조](#파일-구조)

---

## 아키텍처 개요

HITL 미들웨어는 `HumanInTheLoop` 미들웨어 클래스에 의해 조율되는 7개의 독립적인 하위 게이트로 구성됩니다:

```
HumanInTheLoop
├── ApprovalPipeline      (approval.py — 계층형 명령 승인)
│   ├── detect_hardline_command()
│   ├── detect_dangerous_command()
│   └── smart_approve()
├── WriteApprovalGate     (gates.py — 파일/메모리 쓰기 게이팅)
├── InterruptManager      (gates.py — 세션별 인터럽트 플래그)
├── MCPElicitationConsent (gates.py — MCP 서버 동의)
├── KanbanTriage          (gates.py — 작업 실패 트리아지)
├── PairingStore          (gates.py — 플랫폼 사용자 승인)
└── SlashConfirm          (gates.py — 파괴적 슬래시 확인)
```

각 게이트는 독립적으로 인스턴스화하고 테스트할 수 있습니다. `HumanInTheLoop` 미들웨어가 이를 연결하고 표준 `AgentMiddleware` 수명주기 훅(`after_model`, `wrap_tool_call`, `awrap_tool_call`, `abefore_agent`)을 통해 노출합니다.

---

## 레이어 레퍼런스

### 1. 하드라인 및 위험 감지

**파일:** `detection.py`

부작용 없이 명령을 분류하는 두 개의 정적 패턴 매칭기:

| 함수 | 용도 |
|---|---|
| `detect_hardline_command(cmd)` | `HARDLINE_PATTERNS`에 대해 검사 — 항상 검토해야 하는 명령(`rm -rf`, `format`, `dd` 등) |
| `detect_dangerous_command(cmd)` | `DANGEROUS_PATTERNS`에 대해 검사 — 파괴 가능성이 높은 명령(`DROP TABLE`, `shutdown`, `rm`, 강제 푸시) |

둘 다 첫 번째 일치하는 패턴(문자열) 또는 `None`을 반환합니다.

### 2. 쓰기 승인 게이트

**파일:** `gates.py` — 클래스 `WriteApprovalGate`

파일 또는 메모리 대상에 대한 보류 중인 쓰기 작업을 관리합니다. 각 쓰기는 고유 ID로 추적되며 승인/거부를 위해 저장됩니다:

| 메서드 | 설명 |
|---|---|
| `request_write(target, content, session_id)` | 승인을 위해 쓰기를 제출합니다. 추적된 `write_id`가 포함된 `ApprovalResult`를 반환합니다. |
| `approve_write(session_id, write_id)` | 보류 중인 쓰기를 승인합니다. |
| `reject_write(session_id, write_id)` | 보류 중인 쓰기를 거부합니다. |
| `get_pending_writes(session_id, target)` | 보류 중인 쓰기를 나열하며, 대상 유형별로 필터링할 수 있습니다. |

### 3. 인터럽트 매니저

**파일:** `gates.py` — 클래스 `InterruptManager`

실행 중에 도구 실행을 게이팅하는 세션별 부울 플래그:

| 메서드 | 설명 |
|---|---|
| `set_interrupt(session_id, active=True)` | 인터럽트 플래그를 설정하거나 해제합니다. |
| `is_interrupted(session_id)` | 세션이 인터럽트되었는지 확인합니다. |
| `clear_interrupt(session_id)` | 인터럽트 플래그를 해제합니다(편의 별칭). |

인터럽트가 설정되면 `wrap_tool_call` / `awrap_tool_call` 훅이 상태 `"error"`인 `ToolMessage`를 반환하고 실행을 차단합니다.

### 4. MCP 유발 동의

**파일:** `gates.py` — 클래스 `MCPElicitationConsent`

부작용을 유발할 수 있는 MCP(Model Context Protocol) 서버의 경우:

| 메서드 | 설명 |
|---|---|
| `request_consent(server_name, session_id)` | MCP 서버 상호작용에 대한 명시적 동의를 요청하는 인터럽트를 사용자에게 표시합니다. |

### 5. 칸반 트리아지

**파일:** `gates.py` — 클래스 `KanbanTriage`

칸반 스타일 트리아지 에스컬레이션을 위한 작업 실패를 추적합니다:

| 메서드 | 설명 |
|---|---|
| `report_task_failure(task_id, session_id)` | 작업 실패를 등록합니다. `TriageStatus`(`NEW`, `ACKNOWLEDGED` 또는 `RESOLVED`)를 반환합니다. 실패 횟수가 설정된 `recurrence_limit`을 초과하면 `RecurrenceLimitError`를 발생시킵니다. |
| `resolve_triage(task_id, session_id)` | 트리아지된 작업을 해결됨으로 표시합니다. |

### 6. 스마트 승인

**파일:** `approval.py` — 클래스 `ApprovalPipeline`

여러 레이어를 가진 구성 가능한 승인 파이프라인:

| 레벨 | 메커니즘 |
|---|---|
| **레이어 1 — 하드라인 감지** | 항상 차단되는 명령(`rm -rf`, `format` 등) |
| **레이어 2 — 위험 감지** | 플래그가 지정된 명령(`DROP TABLE`, `shutdown` 등) |
| **레이어 3 — 터미널 모드** | 터미널 명령의 승인 정책에 위임 |
| **레이어 4 — 도구 승인** | 플러그인 에스컬레이션 도구 승인(`request_tool_approval`) |
| **레이어 5 — 세션 캐시** | 반복 프롬프트를 피하기 위해 세션별로 승인된 도구 캐시 |
| **레이어 6 — 스마트 승인** | `smart_approve()` — 명령 내용과 컨텍스트를 기반으로 하는 휴리스틱 자동 승인/자동 거부 |
| **레이어 7 — 인간 인터럽트** | 사용자 결정을 위한 `interrupt()` 폴백 |

파이프라인은 외부 호출자를 위해 직접 노출됩니다:

| 메서드 | 설명 |
|---|---|
| `check_command(command, session_id)` | 하드라인 + 위험 감지를 실행합니다. `ApprovalResult`를 반환합니다. |
| `check_command_with_approval(command, session_id, prompt_fn)` | 스마트 승인 + 인간 인터럽트를 포함한 전체 파이프라인. |
| `smart_approve(command)` | 휴리스틱 전용 승인(감지 또는 인터럽트 없음). |
| `request_tool_approval(name, args, session_id)` | 플러그인 에스컬레이션 도구 승인 확인. |
| `approve_tool_for_session(name, args, session_id)` | 세션의 나머지 동안 승인된 도구를 캐시합니다. |

### 7. 페어링 저장소

**파일:** `gates.py` — 클래스 `PairingStore`

플랫폼 수준 사용자 허용 목록:

| 메서드 | 설명 |
|---|---|
| `is_user_allowed(platform, user_id)` | 사용자가 특정 플랫폼에서 승인되었는지 확인합니다. |
| `approve_user(platform, user_id)` | 사용자를 허용 목록에 추가합니다. |
| `revoke_user(platform, user_id)` | 사용자를 허용 목록에서 제거합니다. |

### 8. 슬래시 확인

**파일:** `gates.py` — 클래스 `SlashConfirm`

파괴적 슬래시 명령의 확인 게이트(예: `/reset`, `/kill`):

| 메서드 | 설명 |
|---|---|
| `confirm_destructive(action, session_id)` | 파괴적 작업을 확인하도록 요청하는 인터럽트를 표시합니다. `ApprovalResult`를 반환합니다. |

---

## 미들웨어 훅

`HumanInTheLoop` 클래스는 네 개의 훅을 통해 에이전트 수명주기에 통합됩니다:

| 훅 | 용도 |
|---|---|
| `after_model` / `aafter_model` | LLM 출력을 가로챕니다. 각 도구 호출에 대해: 명령 승인, 쓰기 게이트 검사, `interrupt_on` 설정 검사, 플러그인 에스컬레이션 승인을 실행합니다. 차단되었을 때 도구 호출을 인공 `ToolMessage` 결과로 대체합니다. |
| `wrap_tool_call` | 도구를 실행하기 전에 인터럽트 플래그를 확인합니다. 세션이 인터럽트되면 오류 `ToolMessage`를 반환합니다. |
| `awrap_tool_call` | `wrap_tool_call`의 비동기 변형. |
| `abefore_agent` / `before_agent` | 턴별 상태를 재설정합니다(`turn_interrupted` 플래그 해제). |

### 인터럽트 흐름

```
LLM 출력 → after_model
  ├── 하드라인/위험 검사 (레이어 1-2)
  ├── 쓰기 승인 게이트 (메모리 쓰기만)
  ├── interrupt_on 설정 검사
  ├── 플러그인 도구 승인 (레이어 4)
  └── 수정된 tool_calls + 인공 ToolMessages

각 도구 호출 → wrap/awrap_tool_call
  └── 인터럽트 플래그 확인 → 차단 또는 통과
```

---

## 설정

모든 설정은 `HITLConfig` 데이터클래스(`types.py`에 정의됨)를 통해 전달됩니다:

| 필드 | 유형 | 기본값 | 설명 |
|---|---|---|---|
| `mode` | `ApprovalMode` | `STRICT` | `STRICT`, `SMART` 또는 `DISABLED` |
| `interrupted_tools` | `dict[str, bool \| dict]` | `{}` | `interrupt_on` 설정에 의해 게이팅되는 도구 이름. 각 항목은 부울(기본 허용 결정 `["approve", "edit", "reject"]`) 또는 `allowed_decisions`와 선택적 `description` 콜러블이 있는 dict일 수 있습니다. |
| `interrupt_on` | deprecated | — | `interrupted_tools`로 대체됨. |
| `write_approval_memory` | `bool` | `False` | `WriteApprovalGate`를 통해 메모리 쓰기를 게이팅합니다. |
| `description_prefix` | `str` | `"Agent wants to"` | 사람이 읽을 수 있는 작업 설명의 접두사. |
| `kanban_recurrence_limit` | `int` | `5` | KanbanTriage에서 `RecurrenceLimitError` 이전의 최대 실패 횟수. |

### 예시

```python
from agent.middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig, ApprovalMode

middleware = HumanInTheLoop(HITLConfig(
    mode=ApprovalMode.SMART,
    interrupted_tools={
        "terminal": {"allowed_decisions": ["approve", "reject"]},
        "memory": True,
    },
    write_approval_memory=True,
    kanban_recurrence_limit=3,
))
```

---

## 승인 훅 시스템

모든 승인 결정 후에 실행되는 외부 콜백을 등록합니다:

```python
def log_approval(session_id: str, result: ApprovalResult):
    print(f"[{session_id}] {result.decision}: {result.reason}")

middleware.register_approval_hook(log_approval)
```

훅은 세션 ID와 전체 `ApprovalResult`를 받습니다. 모든 훅은 try/except로 감싸져 있어 실패하는 훅이 승인 흐름을 차단하지 않습니다.

---

## 파일 구조

```
agent/middlewares/HumanInTheLoop/
├── __init__.py        # 공개 내보내기
├── types.py           # 열거형, 데이터클래스, 설정, 스텁
├── detection.py       # 하드라인 + 위험 패턴 감지
├── approval.py        # 계층형 승인 파이프라인
├── gates.py           # 하위 게이트 (쓰기, 인터럽트, MCP, 칸반, 페어링, 슬래시)
├── core.py            # HumanInTheLoop 미들웨어 클래스
├── README.md          # 이 파일 (영어)
├── README.zh.md       # 중국어 버전
├── README.ko.md       # 한국어 버전
└── README.ja.md       # 일본어 버전
```
