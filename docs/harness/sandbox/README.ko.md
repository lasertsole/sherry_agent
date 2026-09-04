# 🛡️ 도구 샌드박스: terminal과 python_repl

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 모델이 시작한 명령을 어떻게 구속하는지: 모든 자식 프로세스 생성 시점에 환경 변수를 무조건 세척하고, 가능하면 OS 네이티브 샌드박스로 감싸며, 의도적인 우회에는 사람의 승인 게이트를 둡니다.

두 도구가 모델이 여러분의 머신에서 코드를 실행하게 합니다: `terminal`(셸 명령)과 `python_repl`(자식 프로세스 안의 Python). 환각되거나 주입된 명령 하나가 환경 변수에서 API 키를 읽거나, 프로젝트 밖에 파일을 쓰거나, 다른 프로세스를 건드릴 수 있습니다. 샌드박스 계층은 이 세 가지를 모두 제한합니다.

사실의 기준(source of truth): `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/middlewares/humanInTheLoop/`.

## 🎯 개요와 위협 모델

| 노출 영역 | 샌드박스가 없을 때 | 방어 |
| :-------- | :----------------- | :--- |
| **환경 변수 속 시크릿** | 자식 프로세스가 `*_API_KEY`를 포함한 모든 변수를 상속 | L1 환경 변수 세척 |
| **파일시스템 쓰기** | 자식이 에이전트 사용자가 쓸 수 있는 어디든 기록 | L2 OS 샌드박스 (Linux / macOS) |
| **프로세스 / 세션 범위** | 자식이 네임스페이스를 공유하고 부모보다 오래 살 수 있음 | L2 `--unshare-all`, `--die-with-parent` |
| **의도적 우회** | 모델이 `sandbox=False`를 요청 | 사람 승인 게이트 (HITL) |

두 계층과 하나의 게이트:

- **L1. 환경 변수 세척**(`scrub_env`): 무조건, 모든 생성 시점에서 실행. 사람이 `sandbox=False`를 승인한 경우에도 예외 없음.
- **L2. OS 네이티브 샌드박스**: Linux는 bubblewrap, macOS는 Seatbelt. Windows에는 OS 백엔드가 없음([정직한 한계 고지](#️-정직한-한계-고지) 참조).
- **사람 승인 게이트**: `sandbox=False` 우회는 메인 세션에서만 가능하며 HITL 인터럽트를 거칩니다.

## 🧱 격리 기능

### 1. 환경 변수 세척(`scrub_env`), L1, 무조건

`scrub_env(base_env=None)`은 모든 자식 프로세스에 넘길 안전한 환경 딕셔너리를 만듭니다. 순수 함수이고(`os` / `re`만 사용, IO 없음, 로깅 없음) 입력을 절대 변경하지 않으며, 값은 검사하지 않고 변수 **이름**만 봅니다. 두 도구의 동기·비동기 생성 지점 모두에서 실행되며, 승인된 `sandbox=False` 호출에서도 실행됩니다.

| 규칙 범주 | 매칭 규칙 | 결과 | 예시 |
| :-------- | :-------- | :--- | :--- |
| **정확한 이름으로 보존** | 대소문자 무시 정확 일치 | 보존, 모든 거부 규칙보다 우선 | `PATH`, `HOME`, `USER`, `USERNAME`, `LANG`, `TERM`, `TMPDIR`, `TMP`, `TEMP`, `SHELL`, `LOGNAME`, `PYTHONPATH`, `PYTHONUTF8`, `VIRTUAL_ENV`, `COMPUTERNAME`, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `OS`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH` |
| **접두사로 보존** | 이름이 `LC_`, `XDG_`, `CONDA`로 시작 | 보존, 모든 거부 규칙보다 우선 | `LC_ALL`, `XDG_CONFIG_HOME`, `CONDA_TOKEN` |
| **강제 거부(프로젝트 시크릿)** | 대소문자 무시 정확 일치 | 항상 제거 | `MAIN_LLM_API_KEY`, `REASONER_LLM_API_KEY`, `AUXILIARY_LLM_API_KEY`, `TAVILY_API_KEY`, `LANGSMITH_API_KEY`, `ITTT_API_KEY`, `VTTT_API_KEY`, `TTI_API_KEY`, `RERANKER_API_KEY`, `EMBEDDING_API_KEY`, `STT_API_KEY` |
| **부분 문자열 차단** | 이름이 `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `BEARER`, `APIKEY` 중 하나를 포함(대소문자 무시) | 제거 | `MY_CUSTOM_TOKEN`, `AWS_SECRET_ACCESS_KEY` |
| **그대로 통과** | 어떤 규칙에도 해당 없음 | 변경 없이 보존 | `EDITOR`, `GIT_AUTHOR_NAME` |

- **우선순위**: 보존(정확 / 접두사) > 강제 거부 > 부분 문자열 차단. `CONDA_TOKEN`은 `TOKEN`을 포함하지만 접두사 보존으로 살아남고, `PATH`를 *포함만* 하는 이름(예: `KEY_PATH_DELIM`)은 보존명이 아니라서 `KEY` 부분 문자열 규칙에 걸려 제거됩니다.
- 이것은 **화이트리스트가 아닙니다**: 어떤 규칙에도 걸리지 않은 변수는 그대로 통과합니다(화이트리스트 전용 모드는 `PATH`를 잃어버려 자식 프로세스를 망가뜨립니다).
- 제거된 변수 이름은 로그에 남지 않아, 시크릿 이름이 로그로 새어나가지 않습니다.

### 2. OS 네이티브 샌드박스 백엔드 (L2)

**Linux: bubblewrap(`bwrap`)**. 명령은 순서가 하중 구조인 list-exec argv로 감싸집니다:

```text
bwrap
  --ro-bind / /                              # 루트 파일시스템 전체: 읽기 전용
  --bind <프로젝트 루트> <프로젝트 루트>        # 유일한 쓰기 가능 위치:
  --bind <임시 디렉터리> <임시 디렉터리>       # 프로젝트 루트 + 임시 디렉터리 (같으면 중복 제거)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 모든 네임스페이스 비공유
  --die-with-parent  --new-session
  --clearenv                                 # 환경 비우기, 모든 --setenv보다 먼저
  --setenv <K> <V> ...                       # 세척된 변수만 다시 주입
  -- /bin/sh -c "<명령>"                      # 감싸진 명령
```

`--clearenv`가 모든 `--setenv`보다 앞에 오는 것과 결합해야 세척된 딕셔너리가 진짜 환경 변수 화이트리스트가 됩니다. 루트 파일시스템은 읽기 전용이고, 쓰기는 프로젝트 루트와 임시 디렉터리에만 가능합니다.

**macOS: Seatbelt(`sandbox-exec`)**. 명령은 `sandbox-exec -p <profile> -- <cmd...>`로 실행되며 profile은 다음과 같습니다:

```text
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "<프로젝트 루트>"))
(allow file-write* (subpath "<임시 디렉터리>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

순서가 곧 규격입니다: `(allow default)` 아래의 `(deny file-write*)`는 "파일 쓰기만 금지하고 나머지는 허용"을 뜻하고, 이후 명시적 allow가 두 쓰기 가능 경로와 `/dev/null`, `/dev/tty` 리터럴을 다시 엽니다. 경로는 `json.dumps`로 삽입되어, 경로 안의 따옴표나 역슬래시가 sbpl 주입 코드로 탈출할 수 없습니다.

**프로브(가용성 확인)**. 두 백엔드 모두 클래스 수준 캐시와 함께 `probe() -> bool`을 구현합니다(프로세스당 한 번 프로브, 실패 결과도 캐시):

- `BwrapBackend.probe()`: `bwrap --ro-bind / / --proc /proc --dev /dev true`를 3초 타임아웃으로 스모크 실행. 바이너리 존재만으로는 부족합니다. Ubuntu 24.04+의 AppArmor 비특권 user namespace 제한은 uid-map 단계에서 모든 bwrap을 죽일 수 있으므로, 실제 스모크 실행만이 정직한 확인입니다.
- `SeatbeltBackend.probe()`: `shutil.which("sandbox-exec")`만 확인. sbpl에는 종료 코드 기반 스모크 프로브가 없습니다.

### 3. 위험 명령 게이트 (terminal 전용)

`DANGEROUS_COMMAND_REGEX`는 6개 대안 패턴의 블랙리스트 정규식이고, `" && "`로 연결한 전체 명령 문자열에 대해 `re.IGNORECASE`로 매칭하며, 어떤 생성보다 먼저 실행됩니다:

| # | 패턴 의도 | 걸리는 예 |
| :- | :-------- | :-------- |
| 1 | `/` 또는 `~`를 겨냥한 재귀/강제 `rm` | `rm -rf /`, `rm -fr ~` |
| 2 | 재귀 플래그가 붙은 모든 `rm` | `rm -r build/` |
| 3 | `mkfs` | 파일시스템 포맷 |
| 4 | `shutdown` | 시스템 종료 |
| 5 | `reboot` | 시스템 재부팅 |
| 6 | `|`, `&&`, `;` 뒤에 `rm` / `shutdown` / `reboot` / `mkfs` | `echo ok && rm -rf /` 같은 연쇄 변형 |

**연결된** 문자열을 매칭하는 것이 중요합니다: 이전의 요소 단위 정확 매칭 블랙리스트는 각 요소가 따로 보면 무해해 보이는 `["echo ok", "rm -rf /"]`를 놓쳤습니다. 걸리면 `ToolException("Blocked: unsafe command.")`을 던지고, `handle_tool_error=True`를 통해 오류 도구 결과로 표면화됩니다. 이 게이트는 `sandbox` 값과 무관하게 항상 작동합니다. `python_repl`에는 대응하는 정규식이 없고, 대신 래퍼 스크립트가 빌트인을 제한합니다.

### 4. 사람이 승인하는 우회 통로

`sandbox=False` 호출은 의도적인 우회 요청입니다. `HumanInTheLoop` 미들웨어가 실행 중이고 YOLO가 아닌 **메인 세션** 그래프에서는 `after_model`이 해당 호출을 LangGraph `interrupt()` 위에 세워 둡니다:

- 인터럽트 페이로드는 전체 도구 호출(도구 이름, 인자, 명령 또는 query)을 보여주고 `allowed_decisions: ["approve", "reject"]`를 담습니다.
- **승인**(`{"decisions": [{"type": "approve"}]}`): 원래 인자 그대로 실행됩니다. 환경은 여전히 세척되고, cwd는 프로젝트 루트로 고정되며, 위험 명령 정규식도 여전히 적용됩니다. 승인된 우회는 스마트 승인과 위험 명령 재확인을 건너뜁니다. 사람이 이번 호출 전체를 승인했기 때문이고, 하드라인 블랙리스트는 그 전에 이미 실행됐습니다.
- **거부**(또는 결정 없음): 결과가 내용이 `User denied: <msg>. <BLOCKED_MESSAGE>`인 오류 `ToolMessage`로 대체됩니다. 명령은 절대 실행되지 않고 두 번째 인터럽트도 발생하지 않습니다. `GraphInterrupt`는 삼켜지지 않고 다시 던져집니다.
- **YOLO 모드**(`is_yolo_mode`: `config.yolo_mode`, 또는 `ApprovalMode.OFF`, 또는 환경 변수 `SHERRY_YOLO_MODE`가 `1` / `true` / `yes`): 인터럽트를 건너뛰고 바로 실행합니다(환경 세척은 여전히 적용).
- **백그라운드 / 서브에이전트 범위**: heartbeat와 cron 도구는 `caller_scope="background"`로 스탬프되고, 서브에이전트 파이프라인은 `caller_scope="subagent"`로 스탬프합니다. 그 그래프에는 HITL 미들웨어가 없으므로 도구 계층이 직접 `sandbox=False`를 `ToolException`으로 강제 거부합니다. 거기에는 인터럽트가 없고, 필요하지도 않습니다.

## ⚙️ 구현과 아키텍처

### 정책: `SandboxPolicy`

`SANDBOX_POLICY` 환경 변수에서 파싱되는 세 가지 상태:

| 값 | 의미 |
| :- | :--- |
| `required` | 백엔드 사용 불가 ⇒ 명령을 거부, 샌드박스 없이 절대 실행하지 않음 |
| `auto` (기본값) | 백엔드 사용 불가 ⇒ 경고 한 줄과 함께 샌드박스 없이 실행으로 강등 |
| `off` | 샌드박싱 완전 비활성화 |

`parse_policy`는 공백을 제거하고 대소문자를 무시하여 매칭하며, 모르는 값에는 `ValueError`를 던집니다: 잘못 입력된 안전 설정은 반드시 크게 실패해야지 조용히 기본값으로 떨어지면 안 됩니다. `read_policy()`는 **매번** `os.getenv`를 호출합니다(가져오기 시점 캐시 없음), 그래서 런타임 변경이 즉시 반영됩니다.

### 백엔드 계약과 디스패치

`SandboxBackend`는 모든 백엔드가 구현하는 ABC입니다:

- `probe() -> bool`: 절대 예외를 던지지 않아야 합니다. 백엔드가 자체 프로브 예외를 잡고 `False`를 반환합니다.
- `wrap(cmd, env) -> (argv, env)`: 감싸진 argv와 env를 반환하며, list 형태로 직접 exec됩니다(셸 없음).

`get_backend(policy)`의 디스패치:

1. `OFF`는 즉시 `None`을 반환: 프로브 없음, 가져오기 없음, 서브프로세스 없음.
2. Linux는 `BwrapBackend`를, macOS는 `SeatbeltBackend`를 지연 가져옵니다(`ImportError`는 "사용 불가"이지 크래시가 아님). 그 외 플랫폼, Windows 포함,에는 백엔드가 없습니다.
3. 백엔드가 존재하지만 `probe()`가 실패하면: `REQUIRED`는 `RuntimeError("Required sandbox unavailable on {system}")`를 던지고, `AUTO` / `OFF`는 `None`을 반환합니다.

### 도구 통합

`SafeShellTool`(이름 `terminal`)과 `TimedPythonREPLTool`(이름 `python_repl`)은 모두 LLM이 보는 도구 호출 스키마에 `sandbox: bool = True` 파라미터를 노출하므로, 모델이 호출마다 선택합니다.

- **샌드박스 경로**: terminal은 `backend.wrap(["/bin/sh", "-c", cmd_str], env)`(POSIX `shell=True`와 의미적으로 동일), python_repl은 `backend.wrap([sys.executable, "-c", script], env)`를 씁니다. 감싸진 argv는 list로 exec되고 셸 kwargs는 전혀 없습니다.
- **폴백 경로(Windows / 백엔드 없음)**: 원래 생성 방식을 바이트 단위로 그대로 유지하고 `env=`만 추가합니다. terminal은 명령을 `" && "`로 연결해 `shell=True`로 띄우고, python_repl은 `[sys.executable, "-c", script]`를 list로 띄웁니다. Windows에는 OS 샌드박스 백엔드가 **없습니다**.
- **모든 경로에서 무조건**: `env=scrub_env()`와 `cwd=str(ROOT_DIR)`(cwd 고정). 두 도구 모두 30초 타임아웃(`TERMINAL_TIMEOUT`, `PYTHON_REPL_TIMEOUT`)을 강제하고 만료 시 자식을 죽입니다.
- **오류 표면화**: `REQUIRED`인데 백엔드가 없으면 terminal은 `RuntimeError`를 `ToolException`으로 감싸고(`handle_tool_error=True`가 그대로 표면화), python_repl은 원시 `RuntimeError`를 그대로 던집니다.
- **강등 경고**: 이번 호출이 샌드박스를 원했는데 백엔드가 없고 정책이 `off`가 아니면, 도구 계층이 정확히 한 줄의 loguru 경고를 남긴 뒤 샌드박스 없이 실행합니다:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 📊 우선순위 매트릭스

`agent/tools/pub_base/sandbox.py`의 권위 있는 표이며, `tests/integration/test_sandbox_matrix.py`가 칸마다 테스트합니다:

| # | 정책 | `sandbox` 플래그 | 백엔드 가능? | 호출자 범위 | 결과 |
| :- | :--- | :--------------- | :----------- | :---------- | :--- |
| 1 | `required` | `True` | 예 | 모두 | 백엔드 래프 안에서 실행 (list-exec, 세척된 env) |
| 2 | `required` | `True` | 아니오 | 모두 | `RuntimeError` / 도구 오류, 아무것도 생성되지 않음 |
| 3 | `required` | `False` | (조회 안 함) | 모두 | 도구 계층 `ToolException`, 절대 `GraphInterrupt` 아님, 생성 없음 |
| 4 | `auto` | `False` | (조회 안 함) | 메인, 비-YOLO | HITL 인터럽트: 승인 → 실행(여전히 세척), 거부 → 오류 `ToolMessage` |
| 5 | `auto` | `True` | 아니오 | 모두 | 강등: 샌드박스 없이 직접 실행, 정확히 한 번의 경고, env는 여전히 세척 |
| 6 | `off` | `True` / `False` | 프로브 안 함 | 메인 | 샌드박스 없음, 승인 없음, 경고 없음, 그대로 실행 |

참고:

- `auto` + `True` + 백엔드 가능은 1번 칸과 같습니다: 백엔드 래프 안에서 실행.
- 호출자 범위 가드는 정책 처리 전에 도는 도구 계층 검사입니다: 메인이 아닌 범위(`subagent`, `background`)의 `sandbox=False` 요청은 모든 정책에서 `ToolException`으로 강제 거부됩니다. 그 그래프에는 승인 인터럽트가 존재하지 않기 때문입니다. 따라서 4번 칸의 인터럽트는 메인 범위 호출에만 발생합니다.

## 🛠️ 설정과 사용법

### `SANDBOX_POLICY`

```bash
# .env 또는 셸 환경 변수
SANDBOX_POLICY=auto      # required | auto | off (대소문자 무시, 기본값: auto)
```

잘못된 값은 조용히 기본값을 쓰는 대신 처음 사용 시점에 `ValueError`를 던집니다. 이 변수는 도구 호출마다 다시 읽히므로 런타임에 바꿀 수 있습니다.

### 모델이 보는 것

두 도구 모두 호출별 `sandbox` 불리언을 받으며 기본값은 `True`입니다. 도구 설명은 모델에게 이렇게 알려줍니다: `false`는 메인 세션에서 사람 승인을 거쳐 세척된 환경으로 실행한다는 것, 서브에이전트와 백그라운드 에이전트의 요청은 거부된다는 것.

### 사용자가 승인하거나 거부하는 방법

메인 세션(비-YOLO)에서 모델이 `sandbox=False`를 요청하면 그래프는 `HumanInTheLoop.after_model` 인터럽트에서 멈춥니다. 프런트엔드는 이 행동(도구 이름, 전체 인자, 명령 또는 query)을 렌더링하고 두 가지 결정을 제공합니다:

- **approve**: `{"decisions": [{"type": "approve"}]}`로 재개. 호출이 즉시 실행됩니다(env는 세척됨, OS 샌드박스 없음).
- **reject**: `{"decisions": [{"type": "reject", "message": "..."}]}`로 재개. 도구 결과가 오류 `ToolMessage`(`User denied: <msg>. <BLOCKED_MESSAGE>`)가 되고 아무것도 실행되지 않습니다.

## 🧪 테스트

| 테스트 스위트 | 커버 범위 |
| :------------ | :-------- |
| `tests/integration/test_sandbox_matrix.py` | 14개 테스트, 매트릭스 칸별 동작 하나씩(1-5번 칸은 도구별 한 번, 6번 칸은 네 번). 실제 그래프의 HITL 인터럽트와 "경고 정확히 한 번" 강등 단언 포함 |
| `tests/module/test_env_scrub.py` | 세척 규칙, 우선순위, 보존/거부 경계 (29개 테스트) |
| `tests/module/test_sandbox_policy.py` | 정책 파싱, 엄격한 `ValueError`, 즉시 읽기 의미론, 플랫폼 디스패치 |
| `tests/module/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 구성, 프로브 캐싱 (서브프로세스 전부 mock) |
| `tests/module/test_terminal_tool.py` / `test_python_repl_tool.py` | 도구 계층 가드, 스키마, 생성 형태 |
| `tests/module/test_hitl_characterization.py` | 19개 테스트, 샌드박스 강화 이전의 HITL / terminal 레거시 동작 고정 |
| `tests/module/test_hitl_sandbox_bypass.py` | 17개 테스트, 우회 승인 흐름, YOLO 통과, 범위 스탬핑 |
| `tests/unit/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` 스탬핑 |

매트릭스 테스트는 `subprocess.Popen`을 전역으로 패치하고, 도구 모듈 경계에서 `get_backend`를 스텁하며, 환경 변수로 `SANDBOX_POLICY`를 설정해 실제 `read_policy`가 매 칸에서 실행되게 합니다.

## ⚠️ 정직한 한계 고지

- **bwrap과 Seatbelt의 구성 로직은 단위 테스트만 거쳤고 실제 Linux/macOS 머신에서 검증되지 않았습니다.** 백엔드 소스 docstring이 명시합니다("구성 로직만 검증, 실기 검증 없음"). 모든 백엔드 테스트는 subprocess를 mock합니다. 래프 출력은 믿을 수 있지만, 아직 실제 격리 보장은 아닙니다.
- **Windows에는 OS 샌드박스 백엔드가 없습니다.** 그곳의 방어는 환경 변수 세척 + cwd 고정 + 위험 명령 정규식 + HITL 게이트입니다. 프로젝트 루트 밖의 파일 쓰기를 막는 장치는 없습니다.
- **강등 경로는 설계대로 샌드박스 없이 실행됩니다.** `auto` + 백엔드 없음 = 경고 한 줄 기록 후 평소처럼 샌드박스 없이 실행. 이것은 의도된 "가용성 우선" 선택이며, 반대가 필요하면 `SANDBOX_POLICY=required`를 고르세요.
- **환경 변수 세척은 이름 기반입니다.** 차단 부분 문자열이 하나도 없는 이름(그리고 거부 목록에 없는 이름)으로 저장된 시크릿은 그대로 통과합니다. 값 스캔도 동적 시크릿 탐지도 없으며, 이는 의도된 것입니다.
- **네트워크 샌드박싱, seccomp, AppArmor 프로파일은 주장하지도 구성하지도 않았습니다.** 격리는 위에 보여준 bwrap / Seatbelt 구성 정확히 그것뿐입니다.
