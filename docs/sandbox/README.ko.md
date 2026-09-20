# 🛡️ 도구 샌드박스: terminal과 python_repl

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 모델이 시작한 명령을 어떻게 구속하는지: 모든 자식 프로세스 생성 시점에 환경 변수를 무조건 세척하고, 가능하면 OS 네이티브 샌드박스로 감싸며, 의도적인 우회에는 사람의 승인 게이트를 둡니다.

두 도구가 모델이 여러분의 머신에서 코드를 실행하게 합니다: `terminal`(셸 명령)과 `python_repl`(자식 프로세스 안의 Python). 환각되거나 주입된 명령 하나가 환경 변수에서 API 키를 읽거나, 프로젝트 밖에 파일을 쓰거나, 다른 프로세스를 건드릴 수 있습니다. 샌드박스 계층은 이 세 가지를 모두 제한합니다.

사실의 기준(source of truth): `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/pub_base/path_utils.py`, `agent/tools/file_tools/`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/middlewares/humanInTheLoop/`, `agent/middlewares/path_guard/`.

## 목차

- [개요와 위협 모델](#-개요와-위협-모델)
- [격리 기능과 우선순위](isolation/README.ko.md)
  - [🧱 격리 기능](isolation/README.ko.md#-격리-기능)
  - [📊 우선순위 매트릭스](isolation/README.ko.md#-우선순위-매트릭스)
- [구현과 아키텍처](#%EF%B8%8F-구현과-아키텍처)
- [설정과 사용법](#%EF%B8%8F-설정과-사용법)
- [테스트](#-테스트)
- [정직한 한계 고지](#%EF%B8%8F-정직한-한계-고지)

## 🎯 개요와 위협 모델

| 노출 영역 | 샌드박스가 없을 때 | 방어 |
| :-------- | :----------------- | :--- |
| **환경 변수 속 시크릿** | 자식 프로세스가 `*_API_KEY`를 포함한 모든 변수를 상속 | L1 환경 변수 세척 |
| **파일시스템 쓰기** | 자식이 에이전트 사용자가 쓸 수 있는 어디든 기록 | L2 OS 샌드박스 (Linux / macOS) |
| **파일시스템 읽기** | 자식이 `~/.ssh`, `.env`, 각종 자격 증명 저장소를 읽음 | L2 리드 실드(민감 경로 마스킹, Linux / macOS) + terminal 민감 파일 정규식 |
| **파일 도구 경로 인자** | 도구 호출이 `read_file`에 트래버설 또는 하드 거부 경로를 요청 | §5 외부 경로 게이트 + §6 세 개의 구조적 게이트 + §7 `PathGuard`(외부 경로는 여전히 HITL 경유) |
| **프로세스 / 세션 범위** | 자식이 네임스페이스를 공유하고 부모보다 오래 살 수 있음 | L2 `--unshare-all`, `--die-with-parent` |
| **의도적 우회** | 모델이 `sandbox=False`를 요청 | 사람 승인 게이트 (HITL) |

두 계층과 하나의 게이트 — 여기에 파일 도구만의 경로 방어 스택이 더해집니다:

- **L1. 환경 변수 세척**(`scrub_env`): 무조건, 모든 생성 시점에서 실행. 사람이 `sandbox=False`를 승인한 경우에도 예외 없음.
- **L2. OS 네이티브 샌드박스**: Linux는 bubblewrap, macOS는 Seatbelt — 쓰기 봉쇄에 더해 민감 경로 리드 실드([§2](isolation/README.ko.md#2-os-네이티브-샌드박스-백엔드-l2) 참조). Windows에는 OS 백엔드가 없음([정직한 한계 고지](#️-정직한-한계-고지) 참조).
- **사람 승인 게이트**: `sandbox=False` 우회는 메인 세션에서만 가능하며 HITL 인터럽트를 거칩니다.
- **파일 도구 경로 게이트**([격리 §5–§7](isolation/README.ko.md#5-외부-파일-경로-게이트파일-도구)): `resolve_project_path()`의 세 개의 구조적 게이트와 `O_NOFOLLOW` I/O, 가상 경로 렌더링, 검색 컨테인먼트, 6단계 외부 경로 승인 흐름, 그리고 `PathGuard` 미들웨어 스크리닝.

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
- **폴백 경로(Windows / 백엔드 없음)**: terminal은 명령을 `" && "`로 연결해 `shell=True`로 띄우고, python_repl은 `[sys.executable, "-c", script]`를 list로 띄웁니다. Windows에는 OS 샌드박스 백엔드가 **없습니다**.
- **모든 경로에서 무조건**: `env=scrub_env()`와 `cwd=str(ROOT_DIR)`(cwd 고정). 두 도구 모두 30초 타임아웃(`TERMINAL_TIMEOUT`, `PYTHON_REPL_TIMEOUT`)을 강제하고 만료 시 자식을 죽입니다.
- **오류 표면화**: `REQUIRED`인데 백엔드가 없으면 terminal은 `RuntimeError`를 `ToolException`으로 감싸고(`handle_tool_error=True`가 그대로 표면화), python_repl은 원시 `RuntimeError`를 그대로 던집니다.
- **강등 경고**: 이번 호출이 샌드박스를 원했는데 백엔드가 없고 정책이 `off`가 아니면, 도구 계층이 정확히 한 줄의 loguru 경고를 남긴 뒤 샌드박스 없이 실행합니다:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 🛠️ 설정과 사용법

### `SANDBOX_POLICY`

```bash
# .env 또는 셸 환경 변수
SANDBOX_POLICY=auto      # required | auto | off (대소문자 무시, 기본값: auto)
```

잘못된 값은 조용히 기본값을 쓰는 대신 처음 사용 시점에 `ValueError`를 던집니다. 이 변수는 도구 호출마다 다시 읽히므로 런타임에 바꿀 수 있습니다.

### `SHERRY_DENY_READ_PATHS`

```bash
# .env 또는 셸 환경 변수 (os.pathsep 구분; ~ 확장)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

두 OS 백엔드의 리드 실드 목록에 경로를 추가합니다([격리 §2](isolation/README.ko.md#2-os-네이티브-샌드박스-백엔드-l2)의 기본 목록은 항상 포함). 이 변수는 `wrap()` 호출마다 읽힙니다.

### 모델이 보는 것

두 도구 모두 호출별 `sandbox` 불리언을 받으며 기본값은 `True`입니다. 도구 설명은 모델에게 이렇게 알려줍니다: `false`는 메인 세션에서 사람 승인을 거쳐 세척된 환경으로 실행한다는 것, 서브에이전트와 백그라운드 에이전트의 요청은 거부된다는 것.

### 사용자가 승인하거나 거부하는 방법

메인 세션(비-YOLO)에서 모델이 `sandbox=False`를 요청하면 그래프는 `HumanInTheLoop.after_model` 인터럽트에서 멈춥니다. 프런트엔드는 이 행동(도구 이름, 전체 인자, 명령 또는 query)을 렌더링하고 두 가지 결정을 제공합니다:

- **approve**: `{"decisions": [{"type": "approve"}]}`로 재개. 호출이 즉시 실행됩니다(env는 세척됨, OS 샌드박스 없음).
- **reject**: `{"decisions": [{"type": "reject", "message": "..."}]}`로 재개. 도구 결과가 오류 `ToolMessage`(`User denied: <msg>. <BLOCKED_MESSAGE>`)가 되고 아무것도 실행되지 않습니다.

## 🧪 테스트

| 테스트 스위트 | 커버 범위 |
| :------------ | :-------- |
| `tests/agent/tools/test_sandbox_matrix.py` | 14개 테스트, 매트릭스 칸별 동작 하나씩(1-5번 칸은 도구별 한 번, 6번 칸은 네 번). 실제 그래프의 HITL 인터럽트와 "경고 정확히 한 번" 강등 단언 포함 |
| `tests/agent/tools/pub_base/test_env_scrub.py` | 세척 규칙, 우선순위, 보존/거부 경계 (29개 테스트) |
| `tests/agent/tools/pub_base/test_sandbox_policy.py` | 정책 파싱, 엄격한 `ValueError`, 즉시 읽기 의미론, 플랫폼 디스패치 |
| `tests/agent/tools/pub_base/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 구성(리드 실드 마운트 포함), 프로브 캐싱 (서브프로세스 전부 mock), 선택 실행되는 실제 bwrap 리드 실드 스모크 테스트 |
| `tests/agent/tools/pub_base/test_terminal_tool.py` / `test_python_repl_tool.py` | 도구 계층 가드(위험 명령 / 민감 파일 정규식), 스키마, 생성 형태, 제한 빌트인 방벽 |
| `tests/agent/tools/pub_base/test_path_utils.py` | 외부 경로 흐름, 세 개의 구조적 게이트, 심볼릭 링크 루프 처리, 가상 경로 렌더링 |
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` / `test_search_bounds.py` | `O_NOFOLLOW`를 통한 심볼릭 링크 / TOCTOU 거부, 가상 경로, 검색 결과 컨테인먼트, 스캔 경계 |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` 스크리닝: 트래버설 컴포넌트, 하드 거부 바닥, 외부 경로 통과, 구조화된 오류 `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19개 테스트, 샌드박스 강화 이전의 HITL / terminal 레거시 동작 고정 |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17개 테스트, 우회 승인 흐름, YOLO 통과, 범위 스탬핑 |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` 스탬핑 |

매트릭스 테스트는 `subprocess.Popen`을 전역으로 패치하고, 도구 모듈 경계에서 `get_backend`를 스텁하며, 환경 변수로 `SANDBOX_POLICY`를 설정해 실제 `read_policy`가 매 칸에서 실행되게 합니다.

## ⚠️ 정직한 한계 고지

- **bwrap과 Seatbelt의 구성 로직은 단위 테스트만 거쳤고 실제 Linux/macOS 머신에서 검증되지 않았습니다.** 백엔드 소스 docstring이 명시합니다("구성 로직만 검증, 실기 검증 없음"). 모든 백엔드 테스트는 subprocess를 mock하며, 리드 실드에는 프로브 실패 시 건너뛰는 선택적 실제 bwrap 스모크 테스트가 하나 있습니다. 래프 출력은 믿을 수 있지만, 아직 실제 격리 보장은 아닙니다.
- **Windows에는 OS 샌드박스 백엔드가 없습니다.** 그곳의 방어는 환경 변수 세척 + cwd 고정 + 위험 명령 정규식 + 민감 파일 정규식 + HITL 게이트입니다. 프로젝트 루트 밖의 파일 쓰기를 막는 장치는 없고, **읽기 보호도 사용할 수 없습니다**: OS 백엔드가 없으면 리드 실드도 없고, 애플리케이션 계층 정규식이 유일한 읽기 게이트입니다.
- **민감 파일 정규식은 완화이지 방벽이 아닙니다.** 리터럴 명령 형태만 매칭하며, `dd`, `sed`, `python -c "open(…)"`, `$(< file)`, 변수, 글롭, 히어도큐먼트는 계층 설계상 우회할 수 있습니다. 백엔드가 있는 곳에서 실제 읽기 방벽은 OS 리드 실드입니다.
- **`python_repl`에는 민감 파일 정규식이 없습니다.** terminal 전용 게이트([격리 §3](isolation/README.ko.md#3-위험-명령-게이트-terminal-전용))가 그것을 덮지 않습니다. 대신 래퍼 스크립트가 빌트인을 제한합니다(안전한 부분집합은 `open` / `__import__`를 생략) — 다르고 더 좁은 통제입니다.
- **강등 경로는 설계대로 샌드박스 없이 실행됩니다.** `auto` + 백엔드 없음 = 경고 한 줄 기록 후 평소처럼 샌드박스 없이 실행. 이것은 의도된 "가용성 우선" 선택이며, 반대가 필요하면 `SANDBOX_POLICY=required`를 고르세요.
- **환경 변수 세척은 이름 기반입니다.** 차단 부분 문자열이 하나도 없는 이름(그리고 거부 목록에 없는 이름)으로 저장된 시크릿은 그대로 통과합니다. 값 스캔도 동적 시크릿 탐지도 없으며, 이는 의도된 것입니다.
- **네트워크 샌드박싱, seccomp, AppArmor 프로파일은 주장하지도 구성하지도 않았습니다.** 격리는 [격리 §2](isolation/README.ko.md#2-os-네이티브-샌드박스-백엔드-l2)에 보여준 bwrap / Seatbelt 구성 정확히 그것뿐입니다.
