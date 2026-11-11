# 🐞 디버그 루프: 브라우저 캡처와 LSP 편집 시점 진단

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 코딩 작업을 위한 두 개의 닫힌 루프입니다. 브라우저는 페이지에서 실제로 일어난 일(console, 잡히지 않은 예외, 실패한 요청)을 기록해 reload 후 원인을 추측하지 않게 합니다. 언어 서버 계층은 편집 도구가 쓴 내용과 서버가 보는 내용을 일치시키고, 새 오류를 다음 턴에 보여 주며, finish 게이트가 재생할 수 있는 진단 증거를 기록합니다.

**사실 출처:** `server/service/browser_manager.py`(`_BrowserDebugCaptureMixin`), `agent/tools/browser/tools/browser_console.py`, `agent/tools/browser/tools/browser_network.py`, `agent/tools/browser/tools/browser_merged.py`, `agent/tools/code_intel/lsp/client.py`, `agent/tools/code_intel/lsp/manager.py`, `agent/tools/code_intel/lsp/diagnostics.py`, `agent/tools/pub_base/lsp_sync.py`, `agent/tools/pub_base/regression_gate.py`, `agent/middlewares/reminder_catalog/sources.py`, `config/features/infra_side/browser_agent.py`, `config/features/agent_side/lsp.py`.

## 🌐 브라우저 console 및 network 캡처

옵트인 브라우저가 켜져 있으면 일반 페이지마다 `Runtime.enable`, `Network.enable`, `Log.enable`도 활성화됩니다. 관리자는 다음을 버퍼링합니다:

| 이벤트 | 버퍼 내용 | 비고 |
| :--- | :--- | :--- |
| `Runtime.consoleAPICalled` | 레벨 + 읽을 수 있는 인자 텍스트 | `RemoteObject`의 value / description / preview |
| `Runtime.exceptionThrown` | 오류 + 최대 10개 스택 프레임 | 버퍼의 행/열은 1 기반 |
| `Log.entryAdded` | 브라우저 로그 | `warning`은 `warn`으로 정규화 |
| `Network.responseReceived` | status 400 이상만 | url / status / 유형 / mime |
| `Network.loadingFailed` | 실패 + 이유 | url은 `requestWillBeSent` 매핑으로 보완 |

header, cookie, body는 읽지 않습니다. 페이지마다 버퍼당 `debug_buffer_max`(200)개까지 유지하며, 메인 프레임 `Page.frameNavigated`와 명시적 navigate / back / forward에서 지웁니다.

모델은 병합 도구로 읽습니다: `browser(action="console", level="error")`는 console 출력과 잡히지 않은 예외를, `browser(action="network", min_status=400, include_failures=true)`는 실패 요청을 반환합니다. `SHERRY_BROWSER_DEBUG_CAPTURE=0`이면 도메인을 전혀 켜지 않고, 두 action은 페이지가 조용했다고 가장하지 않고 비활성 안내를 반환합니다.

## 🧠 LSP 편집 시점 일관성

예전 `lsp_diagnostics`는 처음 `didOpen` 후 서버 캐시를 읽었습니다. 에이전트가 편집해도 서버는 이전 본문을 가지고 있었습니다. 이제 네 단계로 닫았습니다:

1. **편집 동기화.** `LSPClient.sync_document`는 처음에 `textDocument/didOpen`, 이후 전체 텍스트 `didChange`와 `didSave`를 보냅니다. `patch_file`, `write_file`, `lsp_format(write=true)`, 직접 파일 인자의 `ast_grep_rewrite`가 `agent/tools/pub_base/lsp_sync.py`를 호출하며, **이미 acquire된** 서버에만 보냅니다(`LspServerManager.peek`). 편집이 언어 서버를 시작하지는 않습니다. 내용이 같으면 아무것도 보내지 않습니다.
2. **최신 진단.** `lsp_diagnostics`는 디스크에서 서버 뷰를 갱신하고, 관측한 세대보다 새로운 `publishDiagnostics`를 기다립니다(`diagnostics_generation` / `wait_diagnostics_since`). 파일이 그대로이고 캐시가 있으면 오지 않을 재전송을 기다리지 않고 즉시 답합니다.
3. **수동 주입.** `agent/tools/code_intel/lsp/diagnostics.py`가 파일별로 `publishDiagnostics`를 모으고 세션별 전달을 기록합니다. `lsp_diagnostics` reminder source(priority 84, transient, error만, 파일 10개 / 오류 20개 한도)가 각 턴의 첫 model call 전에 미전달 오류를 주입합니다. 진단 세대마다 한 번이며, 새 publish가 전달을 다시 엽니다.
4. **finish 증거.** 같은 편집 경로가 디바운스(`AUTO_DIAGNOSTICS_DEBOUNCE_S`) 후 파일별 백그라운드 진단을 한 번 실행하고 증거 행(`kind="lsp_diagnostics"`, `auto=true`, pass/fail)을 추가합니다. `taskflow_flow(action="finish")`의 regression gate는 shell 허용 목록이 아니라 LSP client로 이 행들을 재생합니다: pass → `unmark`, fail → `broken`, 서버 없음/타임아웃 → 건너뜀. 편집이 만든 타입 오류는 따라서 finish에서 `[broken]`으로 나타납니다.

아홉 번째 LSP 도구는 `lsp_hover`(`textDocument/hover`, MarkupContent를 텍스트로 평탄화)입니다. 견고성: 크래시한 서버는 최대 `lsp_max_restarts`(3)회까지 재시작하고, 그 후 도구는 tree-sitter / grep으로 퇴화합니다. `-32801` ContentModified는 500/1000/2000 ms 백오프로 재시도합니다.

## ⚙️ 설정

| 환경 변수 | 기본값 | 효과 |
| :--- | :--- | :--- |
| `SHERRY_BROWSER_DEBUG_CAPTURE` | 1 | console/network/log 도메인 활성화 |
| `AUTO_DIAGNOSTICS_ENABLED` | 1 | 자동 진단 증거 행 기록 |
| `AUTO_DIAGNOSTICS_MAX_FILES` | 20 | 세션당 자동 검사 파일 수 |
| `AUTO_DIAGNOSTICS_TIMEOUT_S` | 5 | 백그라운드 진단 1회 예산 |
| `AUTO_DIAGNOSTICS_DEBOUNCE_S` | 0.4 | 연속 편집 병합 |
| `LSP_DIAGNOSTICS_INJECT_MAX_FILES` | 10 | 턴당 주입 파일 수 |
| `LSP_DIAGNOSTICS_INJECT_MAX_ERRORS` | 20 | 턴당 주입 오류 수 |
| `LSP_MAX_RESTARTS` | 3 | 퇴화 전 크래시 재시작 한도 |
| `LSP_CONTENT_MODIFIED_MAX_RETRIES` | 3 | `-32801` 재시도 횟수 |
| `LSP_CONTENT_MODIFIED_BACKOFF_S` | 0.5 | 첫 백오프(두 배씩) |

## 🧪 테스트

- `tests/server/service/test_browser_debug_capture.py` — 이벤트 형태, 실패만 network, 링 한도, 내비게이션 초기화, 비활성 스위치.
- `tests/agent/tools/test_browser_tools.py` — `console` / `network` action, 포맷, 비활성 안내.
- `tests/agent/tools/code_intel/lsp/test_edit_sync.py` — didOpen/didChange/didSave, 멱등 동기화, publish 세대, ContentModified 재시도.
- `tests/agent/tools/code_intel/lsp/test_diagnostics_registry.py` — 세션별 전달, 변경 감지, 루트 범위, 한도.
- `tests/agent/tools/pub_base/test_lsp_sync.py` — 편집 동기화, 디바운스 증거 행, fail-open.
- `tests/agent/tools/pub_base/test_regression_gate.py` — LSP 행 재생(pass → unmark, fail → broken, 서버 없음은 건너뜀).
- `tests/agent/middlewares/test_lsp_diagnostics_reminder.py` — reminder 렌더링과 일회 전달.

## ⚠️ 제한

- console/network 캡처는 페이지 단위이며 페이지가 닫히면 사라집니다. 전체 reload가 버퍼를 지우는 것은 설계입니다(이번 로드 이후 무슨 일이 있었는지를 진단).
- error 심각도 진단만 주입합니다. warning은 `lsp_diagnostics`로 직접 읽습니다.
- 자동 진단은 이미 acquire된 서버가 있을 때만 동작합니다. LSP를 전혀 쓰지 않는 프로젝트는 비용이 없지만 백그라운드 검사도 받지 않습니다.
- `ast_grep_rewrite`는 직접 파일 인자만 동기화합니다. 디렉터리 적용은 어떤 파일을 바꿨는지 보고하지 않습니다.
