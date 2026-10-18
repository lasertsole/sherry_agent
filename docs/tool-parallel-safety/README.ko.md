# 🧵 도구 병렬 안전

[**English**](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> LangGraph의 `ToolNode`는 한 번의 모델 응답에 포함된 모든 도구 호출을 동시에 시작한다. Sherry는 배치 인식 노드와 두 계층의 리소스 잠금으로 이를 대체해, 같은 파일을 공유하는 호출은 순서를 지키고 독립적인 호출은 병렬로 유지한다.

## 문제

모델은 한 응답에서 여러 tool call을 반환할 수 있다. ToolNode는 이를 `asyncio.gather`로 함께 실행한다. 독립적인 읽기에는 맞지만, 두 개의 쓰기, 읽기와 쓰기의 경쟁, 두 agent의 shell 경쟁에는 위험하다. 전부 아니면 전무 규칙(쓰기가 하나라도 있으면 배치 전체 직렬화)은 안전하지만 무관한 파일의 병렬성을 버린다. 필요한 것은 불리언이 아니라 리소스 키다.

## 정적 분류

`agent/tools/parallel_safety.py`가 결정한다:

- `READ_ONLY_TOOLS`는 부작용이 없는 도구 목록이다.
- 도구 metadata는 `concurrent_safe`(명시적으로 안전) 또는 `side_effect_scope`(`none`, `workspace`, `system`, `session`, `user_interaction` 등)를 선언할 수 있다.
- 알 수 없는 도구와 metadata 누락은 안전하지 않다.
- `destructive`는 모든 허용 선언을 덮어쓴다.
- `idempotent`는 의도적으로 **사용하지 않는다**: `memory`는 멱등이지만 세션 상태를 쓰고, `sessions_spawn`은 비멱등이지만 병렬 실행할 수 있다.

## 배치 계획과 순서

각 ToolNode 배치는 `plan_batch`가 그룹으로 나눈다:

- `read_file`, `write_file`, `patch_file`은 canonical `file_path`로 묶인 파일 작업이다.
- 쓰기가 있는 그룹은 모델의 `tool_calls` 순서대로 직렬, 읽기만 있는 그룹은 병렬.
- 서로 다른 path는 독립적이다.
- 파일이 아닌 안전하지 않은 호출(예: `terminal`)은 배치 전체를 barrier로 만든다. 대상 리소스를 인자에서 추론할 수 없기 때문이다.
- 해석할 수 없는 file path도 barrier로 돌아간다.

순서 규칙은 모델의 호출 순서다: `read_file(A)`가 `write_file(A)`보다 앞서면 이전 내용을 읽고 쓰기에 license를 제공한다. 반대면 먼저 쓰고 이후 읽기는 새 내용을 본다. 같은 path의 두 쓰기는 순서대로 실행되며 마지막이 이긴다.

## 파일 잠금

배치 계획은 **한 배치 안**의 순서만 담당한다. 파일 계층은 동시에 실행되는 agent와 프로세스를 보호한다:

- `agent/tools/pub_base/path_lock.py`는 `path_read_lock`(공유)과 `path_write_lock`(배타, 쓰기 우선)을 제공한다.
- `agent/tools/pub_base/file_lock.py`는 공유 `flock`(`LOCK_SH`)을 추가하고 배타 `LOCK_EX`를 유지한다.
- `read_file`은 `file_read_lock`, `write_file`과 `patch_file`은 `file_write_lock`을 쓴다.
- 고정 순서는 프로세스 내 게이트, 그 다음 프로세스 간 `flock`이다.

같은 path의 두 읽기는 겹치고, 읽기와 쓰기는 절대 겹치지 않으며, 다른 path는 경쟁하지 않는다. 배치/agent 간 순서는 잠금 획득 순서이지 모델 호출 순서가 아니다.

## 리소스 잠금

`agent/middlewares/tool_resource_lock.py`는 path로 표현할 수 없는 두 리소스를 다룬다:

- `side_effect_scope: "system"`(`terminal`, `python_repl`, `execute_code`, `ptc`)은 프로세스 전체 잠금을 잡는다. `TOOL_PARALLEL["system_lock"]`은 모든 shell 작업을 직렬화하므로 기본 OFF.
- `side_effect_scope: "user_interaction"`(`question`, `exit_plan_mode`)은 세션별 잠금을 잡는다. `interaction_lock`은 기본 ON이라 하나의 HITL 채널이 한 번에 하나의 프롬프트만 처리한다.
- `workspace`, `session`, `network`는 다시 잠그지 않는다. 파일 잠금, memory store, SQLite store, browser manager가 이미 소유한다.

## 설치와 범위

`create_agent`에는 `tool_node` 파라미터가 없다. `agent/core.py`는 가져올 때 `TOOL_PARALLEL["enabled"]`에 따라 `langchain.agents.factory.ToolNode`를 한 번 patch하므로 main, subagent, 보조 agent 그래프가 모두 `SafeToolNode`를 쓴다. 자식 에이전트 미들웨어 체인도 `ToolResourceLockMiddleware`를 갖는다. `TOOL_PARALLEL["enforce"] = False`는 네이티브 병렬을 유지하고 계획만 로그하며, `True`면 적용한다. 호환성 테스트가 의존하는 `ToolNode` 비공개 메서드를 pin하므로 LangGraph 업그레이드는 조용히 깨지지 않고 명시적으로 실패한다.

## 제한

- `terminal`, `python_repl`, `execute_code`, `ptc`는 여전히 배치 전체 barrier다. 명령 문자열에서 파일 대상을 안정적으로 파싱할 수 없다.
- 여러 agent 사이의 순서는 보장하지 않는다. 상호 배제만 보장한다. `system_lock`을 켠 경우가 명시적 예외다.
- 배치 계획은 canonical path를 쓴다. 해석할 수 없으면 추측하지 않고 barrier로 돌아간다.
- 파일 잠금은 로컬 파일 시스템의 `flock`이다. NFS 시맨틱은 범위 밖이다.

## 코드 맵

- `agent/safe_tool_node.py`: `SafeToolNode`, `_afunc` / `_func`, factory 설치.
- `agent/tools/parallel_safety.py`: 분류, `file_resource_key`, `plan_batch`.
- `agent/tools/pub_base/path_lock.py`: 프로세스 내 읽기/쓰기 게이트.
- `agent/tools/pub_base/file_lock.py`: `file_read_lock` / `file_write_lock`, `flock_path(shared=...)`.
- `agent/middlewares/tool_resource_lock.py`: 프로세스 전체 및 세션별 리소스 잠금.
- `agent/tools/file_tools/read_file.py`: 읽기 잠금 호출 지점.
- `tests/agent/test_safe_tool_node.py`, `tests/agent/tools/test_parallel_safety.py`, `tests/agent/tools/file_tools/test_file_rw_lock.py`, `tests/agent/middlewares/test_tool_resource_lock.py`: 위 동작과 불변 조건.
