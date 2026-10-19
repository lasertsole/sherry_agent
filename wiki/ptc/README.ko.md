# 🧰 프로그래매틱 도구 호출(PTC)

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 이 문서는 [서브에이전트 설계 페이지](../subagent/README.ko.md)(`execute_code`를 executor 전용으로 만드는 두 축 역할 모델), [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md)(역할 표에 이 도구를 싣는 런타임·API 레퍼런스), 그리고 [도구 샌드박스 페이지](../sandbox/README.ko.md)(`terminal`, `python_repl`, PTC의 `execute_code`가 해석하는 격리 스택)의 설계 레이어 자매편입니다. 이 페이지는 PTC가 무엇인지, 스크립트가 왜 별도 프로세스에서 실행되는지, 어떻게 실제 도구에 도달하는지, 이 봉쇄가 실제로 무엇을 보장하는지, 그리고 어디서 OS 샌드박스보다 솔직하게 약한지를 기록합니다.

사실 출처: `agent/tools/ptc/**`, `config/features/agent_side/ptc.py`, `config/features/agent_side/tools_timeouts.py`, `agent/tools/subagent/types/functional_role.py`, `agent/tools/subagent/spawn/core.py`, `agent/tools/subagent/spawn/system_prompt.py`, `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `tests/agent/tools/ptc/**`. 아래 모든 서술은 해당 코드에 대조해 검증했습니다.

## 목차

- [개요](#-개요)
- [설계 근거](#-설계-근거)
- [프로토콜과 산출물](#-프로토콜과-산출물)
- [보안 모델](#-보안-모델)
- [구성](#-구성)
- [한계](#-한계)
- [실패 모드](#-실패-모드)
- [테스트 맵](#-테스트-맵)
- [관련 문서](#-관련-문서)

## 🎯 개요

프로그래매틱 도구 호출(PTC)은 `execute_code` 도구 뒤의 능력입니다. 모델이 Python 스크립트 하나를 작성하면, 그 스크립트가 생성된 래퍼를 통해 Sherry의 실제 도구를 동기적으로 호출합니다. 대상은 턴 단위 도구 루프가 잘 다루지 못하는 형태입니다——사이에 처리 로직이 끼는 3회 이상의 도구 호출, 루프·분기 형태의 도구 사용, 그리고 모델 컨텍스트에 들어가기 전에 걸러야 하는 큰 도구 출력입니다.

| 질문 | 답 |
|------|-----|
| 누가 호출할 수 있는가? | `executor` 기능 역할만 가능합니다. 게이트는 `PTC_ROLES`이므로 메인 에이전트와 다른 모든 기능 역할은 이 도구를 받지 않습니다 |
| 스크립트는 어디서 실행되는가? | 별도의 Python 자식 프로세스에서, 호출마다 생성되며 자체 프로세스 그룹을 가집니다. 따라서 타임아웃은 그룹 전체를 종료합니다 |
| 스크립트는 어떻게 도구에 도달하는가? | `from sherry_tools import ...`——생성된 스텁이며, 모든 래퍼는 부모 RPC 서버로 loopback TCP를 통해 보내는 줄바꿈 구분 JSON 요청 하나입니다 |
| 도구는 누구의 세션에서 실행되는가? | 자식 자신의 `child_session_key`입니다. 도구 생성 시 바인딩되어 모든 RPC 디스패치에 주입되며, 스크립트 인자가 되는 일은 없습니다 |
| 무엇이 반환되는가? | JSON 엔벨로프 하나입니다. `status`(`ok` / `timeout` / `error` / `budget_exceeded` / `sandbox_unavailable`), `output`, `error`, `exit_code`, `tool_calls_made`를 담습니다 |

스크립트는 평범한 Python에 생성 래퍼를 더한 것입니다——async도, 컨텍스트 객체도 없이 도구마다 동기 호출 하나입니다:

```python
from sherry_tools import json_parse, read_file

raw = read_file("README.md")
print(json_parse(raw)["total_lines"])
```

```text
executor child agent
  └─ execute_code(code)
       └─ run_ptc: RPC listener + temp dir + sherry_tools.py stub + wrapper script
            └─ python child process  (restricted builtins, scrubbed env, own process group)
                 └─ from sherry_tools import read_file
                      └─ one-shot TCP to 127.0.0.1:<ephemeral port>
                           └─ parent event loop → real BaseTool.ainvoke(session_id=<child key>)
```

## 🧠 설계 근거

**프로세스 내 `exec`가 아니라 별도 프로세스.** "제한된 builtins"와 "스크럽된 환경"은 인터프리터가 일회용일 때에만 의미가 있습니다. 부모는 자식에 축소된 `__builtins__` 네임스페이스를 넣고, SIGKILL할 수 있는 프로세스에 `env=scrub_env()`를 전달합니다. 벽시계 예산에는 죽일 수 있는 프로세스 그룹이 필요하고, 우발적 무한 루프·크래시·fork 폭탄은 자식 안에 머뭅니다. 프로세스 내 `exec`는 부모의 전역을 공유해 버려 그런 식으로 종료할 수 없습니다.

**도구 객체가 아니라 브리지.** 실제 도구는 비동기 `BaseTool`이라 부모 이벤트 루프, 프로바이더 설정, 상태를 해석할 세션을 필요로 합니다——일회용 인터프리터에 들어가면 안 되는 것들의 정반대입니다. 생성 스텁은 인자를 소켓으로 보낼 뿐이고, 부모는 각 요청을 `asyncio.run_coroutine_threadsafe`로 자신의 루프에 디스패치하므로 블로킹하는 자식이 부모의 비동기 작업을 막지 않습니다.

**`python_repl`과의 분업.** 두 실행 표면 모두 미러링된 제한 builtins 집합과 같은 `scrub_env`로 모델 작성 Python을 실행하고, 둘 다 자식의 시작 디렉터리를 `ROOT_DIR`로 둡니다. 형태는 다릅니다. `python_repl`은 30초 예산으로 도구 접근 없는 스니펫 하나를 실행하고, `execute_code`는 더 긴 스크립트(120초 예산)를 실행하며 최대 50회의 실제 도구 호출을 동기적으로 할 수 있습니다. 이 추가 도달 범위가 PTC를 executor 전용으로 만들고, 프로세스 그룹·RPC 브리지·호출 예산·임포트 허용 목록을 더하는 이유입니다. executor 역할은 `python_repl`을 그대로 유지하며, PTC는 대체가 아니라 추가입니다. 둘은 같은 OS 네이티브 샌드박스 백엔드도 해석합니다: PTC 자식 argv는 `sandbox=True`인 `python_repl` 호출과 똑같이 감싸지며([도구 샌드박스 페이지](../sandbox/README.ko.md) 참조), `auto` 강등과 `required` 거부의 의미론은 [한계](#-한계)에 적었습니다.

**도구 면의 게이트는 하나의 명명된 집합.** `PTC_ROLES`——`agent/tools/subagent/types/functional_role.py`에서 `executor`를 담는 유일한 집합——를 주입 지점(`spawn/core.py`, 역할 allow/deny 정책 뒤)과 프롬프트 빌더(`spawn/system_prompt.py`, PTC 안내 절 하나)가 함께 읽습니다. 따라서 도구 면과 그 프롬프트 절은 서로 어긋날 수 없고, executor 역할 정의 파일은 PTC 전용 배선을 갖지 않습니다.

**페일 클로즈 도구 집합.** `build_ptc_tool`은 자식이 사용할 수 있는 도구를 허용 목록으로 걸러 `execute_code` 자신을 제거합니다. executor가 들고 있지 않은 도구를 나열하는 것은 무해하며, 실제로 강제되는 것은 교집합입니다. 또한 `sessions_spawn` / `sessions_yield` / `sessions_kill` / `sessions_steer` / `memory` / `skill_manage` / `question`은 결코 후보가 되지 않습니다.

## 🔌 프로토콜과 산출물

다섯 모듈이 각각 하나의 책임을 맡습니다:

| 산출물 | 책임 |
|--------|------|
| `tool.py` | `ExecuteCodeTool`(`execute_code`)을 정의하고, 자식 세션을 바인딩하며, `available_tools`와 `ptc_allowed_tools`의 교집합을 구하고, 살아 있는 도구 schema로 모델 대상 설명을 렌더링합니다 |
| `runner.py` | `run_ptc`가 한 번의 호출을 조율합니다: RPC 서버 시작, 임시 디렉터리 생성, `sherry_tools.py` 생성, 래퍼 스크립트 작성, 일회용 RPC 토큰 생성, OS 네이티브 샌드박스 백엔드로 자식 argv 래핑, 자식 생성, 출력 캡처·절단, 그리고 정리 |
| `rpc_server.py` | loopback TCP 리스너: 줄마다 JSON 요청 하나를 파싱하고, 모든 프레임을 실행별 토큰으로 인증하며, 스크립트별 호출 예산을 집행하고, 각 호출을 부모 이벤트 루프로 디스패치합니다 |
| `stub_generator.py` | 각 도구의 `tool_call_schema`에서 `ToolStub` 명세를 도출하고 `sherry_tools.py` 모듈을 렌더링합니다——도구마다 동기 래퍼 하나와 로컬 헬퍼들입니다. RPC 호출은 토큰을 박아 넣지 않고 `SHERRY_PTC_RPC_TOKEN`에서 읽습니다 |
| `builtins.py` | 자식의 축소된 builtin 네임스페이스와 임포트 허용 목록을 소유하고, 둘 다 래퍼 스크립트로 렌더링합니다 |

와이어 프로토콜은 양방향 모두 줄마다 JSON 객체 하나입니다:

```json
{"tool": "read_file", "args": {"file_path": "README.md"}, "token": "<일회용 토큰>"}
{"ok": true, "result": "..."}
{"ok": false, "error": "PTC call budget exhausted", "code": "PTCCallBudgetExceeded"}
```

**시그니처 충실성.** 스텁 파라미터는 `args_schema`가 아니라 `tool_call_schema`에서 오므로, 프레임워크가 주입하는 인자(예: `session_id`)는 호출 가능한 파라미터로 나타나지 않습니다. 필수 필드는 필수 파라미터로 렌더링되고, 선택 필드는 기본값을 Python 리터럴로 렌더링하며, 리터럴로 표현할 수 없는 기본값은 `None`으로 폴백합니다——어찌 됐든 실제 도구가 매 호출을 재검증합니다. 생성 모듈은 RPC 브리지를 건드리지 않는 로컬 헬퍼 세 개도 갖습니다: `json_parse`(관대한 `json.loads`), `shell_quote`(`shlex.quote`), `retry`(지수 백오프)입니다.

**산출물과 수명주기.** 호출마다 새 `sherry_ptc_*` 임시 디렉터리가 생겨 `sherry_tools.py`와 래퍼 스크립트를 담습니다. 자식에 대한 유일한 경로 추가는 `PYTHONPATH=<tmpdir>`입니다. 자식 환경은 실행별 RPC 토큰(`SHERRY_PTC_RPC_TOKEN`)도 싣고, 생성된 스텁이 이를 읽어 매 요청에 보냅니다. 토큰은 `secrets.token_urlsafe`로 생성되어 메모리에만 존재하며, `sherry_tools.py`나 다른 어떤 파일에도 결코 쓰이지 않습니다. 백엔드를 쓸 수 있으면 자식 argv가 `backend.wrap([sys.executable, script_path], env)`로 한 번 더 감싸집니다([도구 샌드박스](../sandbox/README.ko.md)의 샌드박스 경로). 프로세스는 여전히 `start_new_session=True`로 생성되므로 타임아웃은 그룹 전체를 SIGKILL합니다. 래퍼는 사용자 스크립트의 stdout과 stderr를 버퍼로 캡처해 실제 stdout에 JSON 엔벨로프 하나를 출력하고, runner가 이를 `status` 필드로 분류합니다. `finally` 블록은 RPC 서버를 멈추고 스레드를 join하며 임시 디렉터리를 지우고, 자식이 살아남았다면 다시 종료합니다.

**자식 세션.** `spawn/core.py`는 해당 run의 `child_session_key`를 도구의 세션으로 넘기고, `rpc_server.py`는 runnable config를 통해 이를 모든 `ainvoke` 호출에 주입합니다. 스크립트는 세션을 제공하거나 위조할 수 없습니다: `session_id`는 애초에 도구 파라미터가 아닙니다.

## 🔒 보안 모델

아래 통제는 능력 축소이며 OS 경계가 아닙니다——솔직한 위치는 [한계](#-한계)에 적었습니다.

1. **환경 스크러빙.** 자식 환경은 `scrub_env()`에서 시작합니다: 이름에 `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `BEARER`, `APIKEY`(대소문자 무시)가 들어간 변수는 버려지고, Sherry 자신의 `*_API_KEY` 변수는 명시적으로 거부되며, `PATH` 같은 핵심 이름은 정확한 이름 우선순위로 유지됩니다. 그다음 runner는 `PYTHONPATH`, `PYTHONUNBUFFERED`, 그리고 실행별 RPC 토큰(`SHERRY_PTC_RPC_TOKEN`)만 더합니다.
2. **제한된 builtins.** 래퍼는 축소된 `__builtins__`로 사용자 코드를 실행합니다: `open`, `exec`, `eval`, `compile`, `globals`, `locals`, `input`, `breakpoint`가 없고 `exit`, `quit`, `help`도 없습니다. `print`, 컬렉션 타입, `getattr` 계열 내성 헬퍼는 남습니다.
3. **가드된 임포트.** `__import__`는 통째로 제거되지 않습니다——임포트 바이트코드가 이를 필요로 하기 때문입니다——대신 허용 목록만 해석하는 가드로 교체됩니다(`sherry_tools`, `json`, `re`, `math`, `time`, `csv`, `datetime`, `collections`, `itertools`, `functools`, `statistics`, `string`, `textwrap`, `decimal`, `random`, `fractions`, `heapq`, `bisect`). `import os`는 `ImportError`를 던지고, `json`과 `sherry_tools`는 통과합니다.
4. **스크립트별 도구 호출 예산.** RPC 서버는 51번째 호출을 거부합니다: 예산 검사가 디스패치 전에 실행되고, 초과 시 `PTCCallBudgetExceeded`를 던지며 엔벨로프의 status는 `budget_exceeded`가 됩니다. 알 수 없는 도구 이름은 예산을 소모하지 않고 거부됩니다.
5. **벽시계 타임아웃과 프로세스 그룹 종료.** 자식은 `ptc_timeout_seconds`(120초) 아래에서 실행됩니다. 만료되면 runner는 프로세스 그룹 전체에 SIGKILL을 보내며(`start_new_session=True`) 손자 프로세스도 함께 죽습니다. 이후 파이프에 남은 출력을 캡처하고 `status: "timeout"`을 보고합니다.
6. **출력 상한.** stdout은 50 KB, stderr는 10 KB에서 절단되며, 각각 명시적인 `...[truncated N bytes]` 표시가 붙은 뒤에야 엔벨로프가 모델에 도달합니다.
7. **loopback 전용 RPC + 일회용 토큰.** 리스너는 `127.0.0.1`의 임시 포트에 바인딩합니다. 생성자는 `0.0.0.0`, `::`, 빈 호스트를 즉시 거부하고, 바인딩된 주소는 자식에 넘기기 전에 다시 확인합니다. 모든 요청 프레임은 자식이 `SHERRY_PTC_RPC_TOKEN`에서 읽는 실행별 토큰도 실어야 합니다. 서버는 상수 시간으로 비교하고, 토큰이 없거나 틀리면 연결을 즉시 끊고 아무 응답도 보내지 않으므로, 무관한 로컬 프로세스는 도구를 호출할 수도, 추측이 근접했는지 알 수도 없습니다.
8. **자식 세션 격리.** 디스패치되는 각 도구 호출은 runnable config에 자식의 `session_id`를 실어, 도구가 자식 세션 기준으로 상태를 해석하게 합니다. 부모 세션은 PTC를 통해 노출되지 않습니다.
9. **재귀 방지와 특권 제외.** `execute_code`는 자신의 허용 목록에 없고, spawn·memory·스킬 관리·질문 도구는 구성으로 제외됩니다——스크립트는 샌드박스 안에서 에이전트를 spawn하거나 스킬을 관리하거나 사용자에게 질문할 수 없습니다.
10. **메인 에이전트 비가시성.** `_MAIN_TOOLS_BUILDERS`에 PTC 빌더는 등록되지 않습니다. 이 도구는 자식 에이전트 조립 시에만, 역할이 `PTC_ROLES`에 속할 때만 생성됩니다.

## ⚙️ 구성

| 키 | 기본값 | 관장 범위 |
|----|--------|-----------|
| `ptc_timeout_seconds` | 120 | 스크립트 하나의 벽시계 예산, 그리고 도구 호출 1회의 디스패치 창 |
| `ptc_max_tool_calls` | 50 | 예산 오류 전까지 스크립트 하나가 디스패치할 수 있는 도구 호출 수 |
| `ptc_max_stdout_bytes` | 50000 | 절단 전에 자식에서 보존하는 stdout 바이트 수 |
| `ptc_max_stderr_bytes` | 10000 | 절단 전에 자식에서 보존하는 stderr 바이트 수 |
| `ptc_allowed_tools` | `read_file`, `write_file`, `patch_file`, `terminal`, `search_files`, `web_search` | 자식의 사용 가능 도구와 교집합을 취하는 허용 목록 |

이 객체는 `config/features/agent_side/ptc.py`에 있습니다. `config/features/agent_side/tools_timeouts.py`의 공유 도구별 레지스트리도 같은 120초 기본값으로 `ptc_timeout_seconds`를 선언하지만, runner의 타임아웃은 `PTC` 객체에서 옵니다. PTC는 pip 의존성을 전혀 추가하지 않습니다: 구현은 표준 라이브러리와 이미 있는 LangChain·loguru뿐입니다.

## ⚠️ 한계

- **그 자체로는 OS 수준 샌드박스가 아닙니다.** 제한된 네임스페이스와 임포트 가드가 묶는 것은 문자 그대로의 스크립트가 지목할 수 있는 이름뿐입니다. 그러나 `getattr`을 비롯한 내성 builtins가 남아 있으므로, 작정한 스크립트는 도달 가능한 객체를 따라가 문자 그대로의 표면을 넘어선 능력에 닿을 수 있습니다. 이는 제한된 네임스페이스 계층——기존 `python_repl` 래퍼 builtins와 같은 계층——입니다. 그 위에서, 쓸 수 있는 백엔드가 있으면 자식 argv가 `terminal`과 `python_repl`이 해석하는 같은 OS 네이티브 샌드박스 백엔드(Linux의 bubblewrap, macOS의 Seatbelt)로 감싸져 그들의 쓰기 봉쇄와 민감 경로 읽기 실드를 얻습니다. 다만 정직한 주의가 둘 남습니다: `SANDBOX_POLICY=auto`(기본)이고 쓸 수 있는 백엔드가 없으면 실행은 정확히 한 줄의 loguru 경고 뒤에 샌드박스 없이 강등되고, bwrap 구성은 `--unshare-all`을 넘겨 네트워크 네임스페이스도 비공유로 만듭니다 — 실제 bwrap 아래에서 loopback RPC 브리지가 도달 가능한지는 실제 Linux 머신에서 검증되지 않았습니다. 백엔드 자체는 [도구 샌드박스](../sandbox/README.ko.md)를 보십시오.
- **타임아웃 취소는 최선 노력입니다.** 도구 호출이 디스패치 창을 넘으면 서버는 자신이 쥔 future를 취소하지만, 부모 루프에서 실행을 시작한 코루틴은 끝까지 돌 수 있습니다. 스크립트는 타임아웃 오류를 받고, 도구의 부작용은 계속될 수 있습니다.
- **호출마다 준비 비용을 냅니다.** 워밍 풀이 없습니다: `execute_code`마다 임시 디렉터리, 스텁 생성, RPC 스레드, 새 인터프리터를 띄우고 모두 해체합니다. 사소한 호출 하나에도 프로세스 기동이 붙습니다.
- **자식의 작업 디렉터리는 저장소 루트입니다.** runner는 자식의 `cwd`를 `ROOT_DIR`로 기본 설정하고, PTC 도구는 서브에이전트의 spawn된 작업 디렉터리를 전달하지 않습니다. 따라서 스크립트 내 상대 경로는 executor가 spawn된 위치가 아니라 저장소 루트에서 해석됩니다.
- **출력 상한은 설계상 손실입니다.** stdout 50 KB 또는 stderr 10 KB를 넘으면 꼬리가 바이트 수 표시로 대체되며, 큰 표를 출력한 스크립트는 잘린 버전만 받습니다.
- **보고는 엔벨로프에 의존합니다.** 래퍼가 엔벨로프를 출력하기 전에 인터프리터가 실패하면 파싱할 구조화 출력이 없습니다. runner는 생 stderr를 오류로 돌려주고 stdout은 비어 있을 수 있습니다.
- **RPC 토큰은 교차 사용자 경계가 아닙니다.** 리스너는 이제 일회용 토큰을 요구하므로, 그것을 읽을 수 없는 로컬 프로세스는 도구를 호출할 수도, 요청을 완료할 수도 없습니다. 그러나 토큰은 자식 환경을 거치므로, 스크립트의 짧은 수명 동안 `/proc/<pid>/environ`을 읽을 수 있는 *동일 사용자* 프로세스는 여전히 복구할 수 있습니다. 이 토큰이 막는 것은 무관한 로컬 프로세스이며, 동일 사용자 공격자가 아닙니다. 리스너도 여전히 loopback 전용이고 스크립트 하나의 실행 시간 동안만 삽니다.

## 🧯 실패 모드

| 실패 | 관찰되는 것 | 완화 |
|------|-------------|------|
| 예산 소진 | `status: "budget_exceeded"`, 오류에 `PTCCallBudgetExceeded` | 이후 호출은 계속 실패합니다. 스크립트가 잡을 수 있고, 부모 턴은 부분 출력을 받습니다 |
| 벽시계 타임아웃 | `status: "timeout"`, SIGKILL로 인한 음수 `exit_code` | 프로세스 그룹이 죽고, 부분 stdout·stderr가 상한까지 캡처됩니다. 부모 턴은 평소처럼 계속됩니다 |
| `required`인데 샌드박스 사용 불가 | `status: "sandbox_unavailable"`, 오류에 `SANDBOX_POLICY=required` | 자식을 하나도 spawn하기 전에 거부됩니다. 부모 턴은 bubblewrap 설치 / `SANDBOX_POLICY=auto` 설정이라는 실행 가능한 힌트를 받습니다 |
| 출력 절단 | `output` 또는 `error`가 `...[truncated N bytes]`로 끝남 | 스크립트는 완료됐고, 모델로 돌아가는 사본만 잘립니다 |
| 도구 오류 | RPC 응답 `{"ok": false, "error": "TypeError: ..."}` | 스텁이 `RuntimeError`를 던집니다. 스크립트가 잡을 수 있고, 못 잡으면 엔벨로프의 `error`로 들어갑니다 |
| 임포트 거부 | `ImportError: import of 'os' is not allowed in PTC` | 스크립트가 잡을 수 있고, 못 잡으면 엔벨로프의 `error`로 들어갑니다 |
| RPC 단절 | 스텁이 `RuntimeError: PTC RPC connection closed before a response arrived`를 던짐 | 서버는 `accept`로 돌아가 재접속하는 자식을 처리합니다. 재시도하지 않는 스크립트는 오류 엔벨로프로 실패합니다 |
| 알 수 없는 도구 이름 | RPC 응답 `{"ok": false, "error": "unknown tool: ..."}` | 예산을 소모하지 않고 거부되므로 스크립트가 다른 도구로 폴백할 수 있습니다 |
| 인터프리터 수준 실패 | 엔벨로프 없음. 생 stderr가 `error`가 되고 `status: "error"` | stdout은 비어 있을 수 있습니다. 모델은 구조화 보고서 대신 인터프리터 자신의 메시지를 봅니다 |

## 🗺️ 테스트 맵

| 영역 | 테스트 |
|------|--------|
| 도구 면: 정체성, 허용 목록 교집합, 바인딩 세션, 재귀 방지 | `tests/agent/tools/ptc/test_tool.py` |
| 스텁 생성: 실제 schema와의 시그니처 일치, 주입 인자 제외, 로컬 헬퍼 | `tests/agent/tools/ptc/test_stub_generator.py` |
| RPC 프로토콜: loopback 거부, JSON 왕복, 예산, 도구 오류, 재접속, 정지 | `tests/agent/tools/ptc/test_rpc_server.py` |
| RPC 토큰 핸드셰이크: 정답 / 누락 / 오답 세 상태, 거부 시 즉시 연결 종료 | `tests/agent/tools/ptc/test_rpc_auth.py` |
| 샌드박스 정책 매핑: 백엔드 래핑, `required` 거부, `auto` 강등과 경고 한 번, `off` | `tests/agent/tools/ptc/test_sandbox_integration.py` |
| runner 강화: 실행별 토큰 고유성, 생성된 스텁에 토큰 없음, 자식 argv에 백엔드 래핑 적용, 유일한 강등 경고 | `tests/agent/tools/ptc/test_runner_hardening.py` |
| 자식 프로세스: 엔드투엔드 도구 호출, 제한 builtins, 임포트 게이트, 타임아웃 종료, 절단, 환경 스크러빙, 임시 디렉터리 정리 | `tests/agent/tools/ptc/test_runner.py` |
| 역할 그리드: executor 주입, 나머지 네 역할, 프롬프트 안내, 메인 레지스트리 격리 | `tests/agent/tools/ptc/test_integration.py` |
| 실제 LLM 엔드투엔드: executor 자식이 `execute_code`를 선택해 RPC로 파일을 읽음 | `tests/agent/tools/subagent/test_ptc_executor_e2e.py` |

PTC의 다섯 스위트는 밀폐 그룹(`unit`, `integration`, `module`)에서 돕니다. 엔드투엔드 파일만 유일한 `llm_e2e` 테스트로, 기본 선택 해제되고 전용 실제 LLM 작업에서 실행됩니다. 검증하는 것은 실제 모델 경로입니다——자식은 `execute_code`를 호출해야 하고, 캡처된 엔벨로프는 디스패치된 RPC 호출을 보고해야 하며, 출력된 줄 수는 테스트 시점에 파일에서 계산한 수와 일치해야 합니다. 샌드박스에 `open`이 없으므로 `read_file`을 거치지 않고는 이 숫자를 만들 수 없습니다.

## 🔗 관련 문서

| 페이지 | 다루는 내용 |
|--------|-------------|
| [서브에이전트 설계](../subagent/README.ko.md) | 두 축 역할 모델(깊이 역할 × 기능 역할)과 `execute_code`가 executor 전용인 이유 |
| [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md) | 런타임·API 레퍼런스: spawn 파이프라인, 도구 목록이 있는 역할 표, 레지스트리 동작 |
| [도구 샌드박스](../sandbox/README.ko.md) | 환경 스크러빙과 `terminal`·`python_repl`·PTC의 `execute_code`가 해석하는 OS 네이티브 격리——PTC가 상속하는 `auto` 강등과 `required` 거부 포함 |
