# 🛡️ 도구 샌드박스: terminal과 python_repl

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 에이전트가 모델이 시작한 명령을 어떻게 구속하는지: 모든 자식 프로세스 생성 시점에 환경 변수를 무조건 세척하고, 가능하면 OS 네이티브 샌드박스로 감싸며, 의도적인 우회에는 사람의 승인 게이트를 둡니다.

두 도구가 모델이 여러분의 머신에서 코드를 실행하게 합니다: `terminal`(셸 명령)과 `python_repl`(자식 프로세스 안의 Python). 환각되거나 주입된 명령 하나가 환경 변수에서 API 키를 읽거나, 프로젝트 밖에 파일을 쓰거나, 다른 프로세스를 건드릴 수 있습니다. 샌드박스 계층은 이 세 가지를 모두 제한합니다.

사실의 기준(source of truth): `agent/tools/pub_base/env_scrub.py`, `agent/tools/pub_base/sandbox.py`, `agent/tools/pub_base/sandbox_bwrap.py`, `agent/tools/pub_base/sandbox_seatbelt.py`, `agent/tools/pub_base/path_utils.py`, `agent/tools/file_tools/`, `agent/tools/terminal.py`, `agent/tools/python_repl.py`, `agent/middlewares/humanInTheLoop/`, `agent/middlewares/path_guard/`.

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
- **L2. OS 네이티브 샌드박스**: Linux는 bubblewrap, macOS는 Seatbelt — 쓰기 봉쇄에 더해 민감 경로 리드 실드([§2](#2-os-네이티브-샌드박스-백엔드-l2) 참조). Windows에는 OS 백엔드가 없음([정직한 한계 고지](#️-정직한-한계-고지) 참조).
- **사람 승인 게이트**: `sandbox=False` 우회는 메인 세션에서만 가능하며 HITL 인터럽트를 거칩니다.
- **파일 도구 경로 게이트**(§5–§7): `resolve_project_path()`의 세 개의 구조적 게이트와 `O_NOFOLLOW` I/O, 가상 경로 렌더링, 검색 컨테인먼트, 6단계 외부 경로 승인 흐름, 그리고 `PathGuard` 미들웨어 스크리닝.

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
  --ro-bind /var/empty <민감 디렉터리>         # 리드 실드: 민감 디렉터리를 빈 디렉터리로 마스킹
  --ro-bind /dev/null <민감 파일>              # 리드 실드: 민감 파일 마스킹
                                              # (/var/empty가 없으면 --tmpfs <경로>)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 모든 네임스페이스 비공유
  --die-with-parent  --new-session
  --clearenv                                 # 환경 비우기, 모든 --setenv보다 먼저
  --setenv <K> <V> ...                       # 세척된 변수만 다시 주입
  -- /bin/sh -c "<명령>"                      # 감싸진 명령
```

`--clearenv`가 모든 `--setenv`보다 앞에 오는 것과 결합해야 세척된 딕셔너리가 진짜 환경 변수 화이트리스트가 됩니다. 루트 파일시스템은 읽기 전용이고, 쓰기는 프로젝트 루트와 임시 디렉터리에만 가능합니다.

**리드 실드.** `--ro-bind / /`는 읽기를 "어디서나 가능"하게 만들 뿐 무해하게 만들지는 않습니다. 마스킹이 없으면 모델은 `cat ~/.ssh/id_rsa`를 실행할 수 있습니다. 그래서 두 백엔드 모두 기본 민감 경로 목록 `DEFAULT_DENY_READ_PATHS` — `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.docker` — 을 마스킹하고, 호출마다 `_sensitive_read_paths()`가 해석하며, 환경 변수 `SHERRY_DENY_READ_PATHS`(`os.pathsep` 구분, `~` 확장, 빈 항목 건너뜀, 순서 유지, 중복 제거)로 확장할 수 있습니다. bwrap 실드는 존재하는 각 디렉터리 위에 빈 디렉터리를 마운트하고(민감 파일에는 `--ro-bind /dev/null`), 존재하지 않는 경로는 건너뜁니다(읽을 것이 없고, bwrap은 읽기 전용 루트 바인드 아래에 마운트 지점을 만들 수 없습니다). `/var/empty`가 없는 호스트에서는 디렉터리가 `--tmpfs <경로>`로 폴백합니다. 실드는 쓰기 가능 바인드 **이후**에 놓여, 쓰기 가능한 프로젝트 루트가 마스킹된 경로를 다시 노출할 수 없습니다.

**macOS: Seatbelt(`sandbox-exec`)**. 명령은 `sandbox-exec -p <profile> -- <cmd...>`로 실행되며 profile은 다음과 같습니다:

```text
(version 1)
(allow default)
(deny file-write*)
(deny file-read* (subpath "<민감 경로>"))           # 민감 경로마다 한 줄, ~ 확장
(deny file-read* (regex #"(^|/)\.env$"))           # 임의 깊이의 .env / .env.*
(deny file-read* (regex #"(^|/)\.env\."))
(allow file-write* (subpath "<프로젝트 루트>"))
(allow file-write* (subpath "<임시 디렉터리>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

순서가 곧 규격입니다: `(allow default)` 아래의 `(deny file-write*)`는 "파일 쓰기만 금지하고 나머지는 허용"을 뜻하고, 이후 명시적 allow가 두 쓰기 가능 경로와 `/dev/null`, `/dev/tty` 리터럴을 다시 엽니다. 리드 실드의 `deny file-read*` 규칙은 `(deny file-write*)` 바로 뒤에 놓입니다: 민감 경로마다 하나의 `subpath` 규칙(기본 목록과 `SHERRY_DENY_READ_PATHS` 확장은 bwrap과 공통)과 임의 위치의 `.env` / `.env.*`를 덮는 두 개의 정규식 규칙입니다. 존재하지 않는 경로도 여전히 거부됩니다 — 존재하지 않는 경로에 대한 거부는 무해합니다. 경로는 `json.dumps`로 삽입되어, 경로 안의 따옴표나 역슬래시가 sbpl 주입 코드로 탈출할 수 없습니다.

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

**민감 파일 게이트 (`_SENSITIVE_FILE_PATTERNS`).** `_run`과 `_arun` 두 경로 모두에서 `_check_sensitive_file_access(cmd_str)`가 `_check_dangerous` **이후**, **어떤 생성보다도 이전**에 실행됩니다: 컴파일된 6개 패턴 중 하나라도 연결된 명령 문자열에 매칭되면 `ToolException("Blocked: sensitive file access. …")`(`_SENSITIVE_FILE_MESSAGE`)을 던지고 — 자식 프로세스는 결코 생성되지 않습니다 — 모델을 `read_file`(외부 경로는 사람 승인을 거침)로 안내합니다:

| 패턴 | 대상 |
| :--- | :--- |
| `(cat\|head\|tail\|less\|more) … /etc/(passwd\|shadow\|sudoers)` | 시스템 자격 증명 파일 |
| `(cat\|head\|tail) … .env` | `.env` / `.env.*` 읽기 |
| `cp … .ssh/` | SSH 자료 복사 유출 |
| `curl … -d @… .env` | dotenv 업로드 유출 |
| `(cat\|head\|tail) … ~/.ssh/`, `(cat\|head\|tail) … ~/.aws/` | 홈 자격 증명 저장소 |

**이것은 완화이지 방벽이 아닙니다.** `dd`, `sed`, `python -c "open(…)"`, `$(< file)`, 셸 변수, 글롭은 모두 리터럴 정규식을 우회할 수 있습니다 — 진짜 읽기 방벽은 위의 L2 리드 실드이며, 승인된 `sandbox=False` 호출은 설계대로 샌드박스 밖입니다. 이 정규식은 뻔하고 흔한 시도를 막고 모델을 승인 흐름으로 유도하기 위해 존재합니다.

### 4. 사람이 승인하는 우회 통로

`sandbox=False` 호출은 의도적인 우회 요청입니다. `HumanInTheLoop` 미들웨어가 실행 중이고 YOLO가 아닌 **메인 세션** 그래프에서는 `after_model`이 해당 호출을 LangGraph `interrupt()` 위에 세워 둡니다:

- 인터럽트 페이로드는 전체 도구 호출(도구 이름, 인자, 명령 또는 query)을 보여주고 `allowed_decisions: ["approve", "reject"]`를 담습니다.
- **승인**(`{"decisions": [{"type": "approve"}]}`): 원래 인자 그대로 실행됩니다. 환경은 여전히 세척되고, cwd는 프로젝트 루트로 고정되며, 위험 명령 정규식도 여전히 적용됩니다. 승인된 우회는 스마트 승인과 위험 명령 재확인을 건너뜁니다. 사람이 이번 호출 전체를 승인했기 때문이고, 하드라인 블랙리스트는 그 전에 이미 실행됐습니다.
- **거부**(또는 결정 없음): 결과가 내용이 `User denied: <msg>. <BLOCKED_MESSAGE>`인 오류 `ToolMessage`로 대체됩니다. 명령은 절대 실행되지 않고 두 번째 인터럽트도 발생하지 않습니다. `GraphInterrupt`는 삼켜지지 않고 다시 던져집니다.
- **YOLO 모드**(`is_yolo_mode`: `config.yolo_mode`, 또는 `ApprovalMode.OFF`, 또는 환경 변수 `SHERRY_YOLO_MODE`가 `1` / `true` / `yes`): 인터럽트를 건너뛰고 바로 실행합니다(환경 세척은 여전히 적용).
- **백그라운드 / 서브에이전트 범위**: heartbeat와 cron 도구는 `caller_scope="background"`로 스탬프되고, 서브에이전트 파이프라인은 `caller_scope="subagent"`로 스탬프합니다. 그 그래프에는 HITL 미들웨어가 없으므로 도구 계층이 직접 `sandbox=False`를 `ToolException`으로 강제 거부합니다. 거기에는 인터럽트가 없고, 필요하지도 않습니다.

### 5. 외부 파일 경로 게이트(파일 도구)

위의 L1/L2 샌드박스와 독립적으로, 파일 도구(`read_file`, `write_file`, `patch_file`, `search_files` 등)는 모든 경로를 `agent/tools/pub_base/path_utils.py::resolve_external_path()`로 해석하며 다음 6단계 검사를 순서대로 적용합니다:

1. **`ROOT_DIR` 내부** — 안전한 경로로 그대로 반환합니다.
2. **YOLO 거부 목록** — 보안 바닥: `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.config/gcloud/`, `~/.env`, `~/.gitconfig`, `~/.npmrc`, `~/.pypirc`이며 `sherry.jsonc`의 `yolo_deny_paths`로 확장할 수 있습니다. 여기서 걸리면 그 자리에서 거부되며 이후 계층은 이를 넘을 수 없습니다: YOLO 모드, allowlist 일치, 서브에이전트 권한 상속 모두 이 게이트에서 멈춥니다.
3. **YOLO 모드** — 전역 전체 허용, 경로를 반환합니다.
4. **세션 allowlist** — 정확한 경로 항목과 디렉터리 항목(끝 `/`, 해당 디렉터리와 모든 하위 경로 일치)은 세션 범위이며 서브에이전트에 상속됩니다.
5. **사전 승인 없는 서브에이전트** — 거부: 서브에이전트는 새 경로를 스스로 승인할 수 없습니다.
6. **메인 세션** — HITL 인터럽트, `allowed_decisions: ["approve", "approve_dir", "yolo", "reject"]`:
   - `approve` — 이 파일만 허용(세션 범위, 서브에이전트에 상속);
   - `approve_dir` — 파일이 속한 디렉터리 전체 허용(세션 범위 접두사 일치, 서브에이전트에도 상속);
   - `yolo` — 모든 외부 경로를 영구 허용;
   - `reject` — 접근 거부.

`resolve_project_path()`(ROOT_DIR 쪽 흐름)에서는 경로가 파일 I/O 이전에 세 개의 구조적 게이트를 통과하고, 이후 모든 열기는 마지막 컴포넌트가 심볼릭 링크인 것을 거부합니다(`O_NOFOLLOW`). 이 모듈은 모델이 보는 경로를 `ROOT_DIR`을 포함하지 않는 가상 경로로 렌더링하기도 합니다. 이러한 메커니즘과 검색 컨테인먼트 필터는 §6에, 도구 실행 전에 경로 인자를 스크리닝하는 `PathGuard` 미들웨어는 §7에 자세히 설명합니다.

### 6. 파일 도구 경로 게이트: 세 개의 구조적 게이트, no-follow I/O, 가상 경로

파일 도구는 샌드박스 프로세스에 의존하지 않습니다: 모든 프로젝트 경로는 `agent/tools/pub_base/path_utils.py`가 프로세스 안에서 해석합니다. `resolve_project_path()`는 도구가 파일 시스템을 건드리기 전에 세 개의 게이트를 순서대로 적용하며, 거부된 경로는 도구의 `except PathOutOfBoundsError` 분기가 §5의 외부 경로 HITL 흐름으로 넘깁니다.

1. **문자열 수준 거부(`_reject_traversal_input`).** `~` 접두사 또는 `..` 컴포넌트는 파일 시스템 접근 전에 `PathOutOfBoundsError`로 거부됩니다. 검사는 컴포넌트 단위(`Path(file_path).parts`)이며, 의도적으로 부분 문자열 판정을 쓰지 않습니다. 부분 문자열 판정은 `foo..bar`나 `配置..md` 같은 정상적인 이름을 잘못 걸러내기 때문입니다.
2. **컨테인먼트.** 상대 경로는 먼저 `ROOT_DIR`에 결합되고(`~` 사전 확장), 그다음 `resolve()`가 실행됩니다. `resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR)`이면 `PathOutOfBoundsError`를 던집니다. `ROOT_DIR` 자체는 허용됩니다.
3. **심볼릭 링크 루프 감지(`_raise_if_symlink_loop`).** `Path.resolve()`는 심볼릭 링크 루프에서 조용히 멈추고 루프 중인 링크를 그대로 돌려줍니다. 해석된 경로가 심볼릭 링크이면 `stat()`이 그 상태를 `OSError(ELOOP)`(Linux/macOS, 또는 Windows `winerror` 1921)로 매핑해 다시 던집니다 — 이후에 혼란스럽게 실패하는 대신 명시적 오류로 만듭니다.

**No-follow I/O(`_open_no_follow`).** 모든 읽기와 쓰기는 `os.open(path, flags | O_NOFOLLOW, mode)`로 열리므로 경로의 마지막 컴포넌트가 심볼릭 링크일 수 없습니다. 해석과 열기 사이에 교체된 링크는 I/O를 `ROOT_DIR` 밖으로 돌릴 수 없습니다 — TOCTOU 창이 닫힙니다. 거부는 `OSError(ELOOP)`를 던지며 Linux/macOS가 네이티브로 내는 errno와 같습니다. Windows에는 `O_NOFOLLOW`가 없어 헬퍼가 명시적 `path.is_symlink()` 검사로 폴백하고 같은 오류를 던집니다. `read_file`(읽기), `write_file`(쓰기, 그리고 `.py` 추가/포맷 흐름의 읽기), `patch_file`(읽기 + 쓰기) 모두 이 경로를 지납니다.

**가상 경로 렌더링.** `to_virtual_path()`는 `ROOT_DIR` 아래의 실제 경로를 가상 경로(`/src/main.py`)로 매핑합니다. `display_path()`는 정상적으로 그 가상 경로를 반환하고, 대상이 루트 밖이거나 해석할 수 없으면(`ValueError` / `OSError` / `RuntimeError` 포착) `real_path.name or "/"`로 폴백합니다 — 그래서 `ROOT_DIR`은 결코 새지 않습니다. `safe_error_detail()`은 `OSError.strerror`(예: `Permission denied`) 또는 `UnicodeDecodeError.reason`(`invalid start byte`)만 반환합니다. 다른 예외의 메시지는 의도적으로 버려집니다. 일반적인 예외 텍스트는 실제 루트 경로를 포함할 수 있기 때문입니다(예: `Path.rglob` 도중 던져진 오류). 남는 것은 예외 타입 이름뿐입니다. 순효과: 모델이 보는 결과와 오류 정보에 실제 프로젝트 루트가 결코 포함되지 않습니다.

**검색 컨테인먼트(`_stays_within_root`).** `os.walk`는 디렉터리 심볼릭 링크를 내려가지 않지만, 파일 심볼릭 링크는 목록에 나타납니다. 두 검색 모드 모두 모든 히트를 `_stays_within_root(candidate, root)`(`candidate.resolve().relative_to(root.resolve())`, `ValueError` / `OSError` / `RuntimeError` 시 건너뜀)로 필터링하므로 검색 트리 밖으로 해석되는 심볼릭 링크는 결코 반환되지 않습니다 — `/etc/passwd`를 가리키는 파일 심볼릭 링크는 건너뜁니다. 검색 루트는 항상 이미 해석된 상태이며(프로젝트 내 검색은 추가로 `ROOT_DIR`에 묶임), allowlist에 등록된 외부 디렉터리 검색은 계속 동작합니다.

**스캔 경계.** 두 모드 모두 `TOOLS_TIMEOUTS`(`config/features/agent_side/tools_timeouts.py`)로 스캔 자체를 제한합니다: `file_tools_search_time_budget_s`(기본 5.0초)가 만료되면 순회를 멈추고, `file_tools_search_max_matches`(기본 10,000)가 수집 히트 수를 제한하며, `file_tools_search_prune_dirs`(기본 `proc`, `sys`, `dev`)는 `dirnames[:]`에서 걸러져 의사 파일 시스템으로는 결코 내려가지 않습니다. 잘린 스캔은 결코 조용하지 않습니다 — JSON 결과에 `scan_truncated: true`, `scan_stop_reason`(`time_budget` / `max_matches`)과 힌트가 추가되고, 정리가 발생하면 `pruned_dir_count`도 붙습니다. 파일명 패턴은 `fnmatch`를 거치며(중괄호 확장 없음), 확장 수 상한이 필요하지 않습니다.

**deepagents 참조 구현과의 설계 차이.** 참조 구현은 모든 경로를 가상 네임스페이스(`virtual_mode`)에 고정해 트래버설을 설계상 불가능하게 만듭니다. Sherry는 대신 실제 파일 시스템 경로를 유지하고(`prompt_builder`, 스킬 도구, terminal cwd가 모두 여기에 의존), 해석 **이후**에 컨테인먼트(위의 세 게이트)를 적용하며 `O_NOFOLLOW`로 TOCTOU를 닫습니다. `BackendProtocol`, `CompositeBackend`, `StateBackend`, 전체 가상 경로 네임스페이스는 의도적으로 채택하지 않았습니다. 그것은 아키텍처 재작성이며, Sherry에는 멀티 백엔드 사용 사례가 없습니다.

### 7. `PathGuard` 미들웨어

**모듈:** `agent/middlewares/path_guard/core.py` · **클래스:** `PathGuard(AgentMiddleware)` · **후크:** `wrap_tool_call` / `awrap_tool_call` 전용

파일 도구의 게이트는 거기까지 도달한 호출만 보호합니다. `PathGuard`는 메인 에이전트 체인에서 `ToolCallNormalize` 바로 뒤에 등록되는 호출 지점 스크린입니다(`agent/core.py`). 리스트 순서가 wrap 후크의 바깥 순서이므로 `ToolGuardrails` **안쪽**에서 실행됩니다(`IterationBudget` → `ToolGuardrails` → `PathGuard` → 도구). 거부는 일반 오류 `ToolMessage`로 ToolGuardrails에 평가되어 다른 도구 실패와 동일하게 취급됩니다. worker / 서브에이전트 파이프라인에는 등록하지 않습니다 — 자식 도구는 자체 게이트를 유지하고, 서브에이전트의 외부 접근은 어차피 강제 거부입니다.

스크리닝은 의도적으로 보수적입니다:

- 인자 이름 `file_path` / `path` / `directory` / `dir`의 문자열 값만 검사하며, `scheme://` 형태의 URL은 건너뛰므로 비경로 의미론을 파일 시스템 경로로 오독하지 않습니다;
- `..` 트래버설 컴포넌트는 공용 `has_traversal_component` 술어로 거부합니다 — URL 디코드와 백슬래시 정규화를 먼저 하므로 `%2e%2e`와 `..\`가 빠져나갈 수 없습니다; 점만 있는 컴포넌트(`...`)도 트래버설로 취급합니다;
- `resolve_project_path()`가 받아들이는 값은 그대로 통과합니다;
- `ROOT_DIR` 밖으로 해석되는 값은 하드 거부 바닥에 걸리지 않는 한 통과합니다: `_SYSTEM_DENY_PATHS`(`/etc/passwd`, `/etc/shadow`, `/etc/sudoers`) 또는 YOLO 거부 목록(`_is_yolo_denied`);
- 그 밖의 외부 경로는 도구 자체의 `resolve_external_path()` HITL 흐름에 맡깁니다 — 미들웨어는 인자를 재작성하지도, 인터럽트를 일으키지도 않으므로 한 번의 호출은 승인 결정을 정확히 하나만 만듭니다(도구가 실행 시 같은 게이트를 다시 돌기 때문에 여기서 개입하면 결정이 두 번 내려집니다);
- 존재하지 않거나 해석할 수 없는 대상, 알 수 없는 예외 클래스는 도구로 통과시키며, 오류 표면은 도구가 책임집니다.

거부 시 `PathGuard`는 경고를 기록하고, 도구를 실행하지 않은 채 구조화된 오류 `ToolMessage`(`status="error"`, 원래 `tool_call_id`와 도구 이름 유지)를 반환합니다.

**2차 방어선.** 네 개의 파일 도구는 자체 `resolve_project_path()` / `resolve_external_path()` 호출을 유지하며, 코드에 `# redundant: path_guard middleware handles this — kept as the second line of defense`로 표시되어 있습니다(`read_file`, `write_file`, `patch_file`, `search_files`). 미들웨어는 자체 검사를 잊은 도구를 걸러내는 바깥 스크린이고, 도구별 게이트가 계속 권위이며, 외부 경로는 여전히 사람 승인 흐름을 거칩니다. 미들웨어 측 세부 사항: [Middlewares README §PathGuard](../../agent/middlewares/README.ko.md#pathguard).

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

`agent/tools/pub_base/sandbox.py`의 권위 있는 표이며, `tests/agent/tools/test_sandbox_matrix.py`가 칸마다 테스트합니다:

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

### `SHERRY_DENY_READ_PATHS`

```bash
# .env 또는 셸 환경 변수 (os.pathsep 구분; ~ 확장)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

두 OS 백엔드의 리드 실드 목록에 경로를 추가합니다(위의 기본 목록은 항상 포함). 이 변수는 `wrap()` 호출마다 읽힙니다.

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
- **`python_repl`에는 민감 파일 정규식이 없습니다.** 위의 terminal 전용 게이트가 그것을 덮지 않습니다. 대신 래퍼 스크립트가 빌트인을 제한합니다(안전한 부분집합은 `open` / `__import__`를 생략) — 다르고 더 좁은 통제입니다.
- **강등 경로는 설계대로 샌드박스 없이 실행됩니다.** `auto` + 백엔드 없음 = 경고 한 줄 기록 후 평소처럼 샌드박스 없이 실행. 이것은 의도된 "가용성 우선" 선택이며, 반대가 필요하면 `SANDBOX_POLICY=required`를 고르세요.
- **환경 변수 세척은 이름 기반입니다.** 차단 부분 문자열이 하나도 없는 이름(그리고 거부 목록에 없는 이름)으로 저장된 시크릿은 그대로 통과합니다. 값 스캔도 동적 시크릿 탐지도 없으며, 이는 의도된 것입니다.
- **네트워크 샌드박싱, seccomp, AppArmor 프로파일은 주장하지도 구성하지도 않았습니다.** 격리는 위에 보여준 bwrap / Seatbelt 구성 정확히 그것뿐입니다.
