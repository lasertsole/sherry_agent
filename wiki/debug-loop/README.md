# 🐞 Debug Loop: Browser Capture & LSP Edit-Time Diagnostics

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Two closed loops for coding work. The browser records what the page actually did (console, uncaught exceptions, failed requests) so a reload can be diagnosed instead of guessed. The language-server layer keeps its view of a file equal to what the edit tools wrote, surfaces new errors on the next turn, and writes a diagnostics row the finish gate can replay.

**Source of truth:** `server/service/browser_manager.py` (`_BrowserDebugCaptureMixin`), `agent/tools/browser/tools/browser_console.py`, `agent/tools/browser/tools/browser_network.py`, `agent/tools/browser/tools/browser_merged.py`, `agent/tools/code_intel/lsp/client.py`, `agent/tools/code_intel/lsp/manager.py`, `agent/tools/code_intel/lsp/diagnostics.py`, `agent/tools/pub_base/lsp_sync.py`, `agent/tools/pub_base/regression_gate.py`, `agent/middlewares/reminder_catalog/sources.py`, `config/features/infra_side/browser_agent.py`, `config/features/agent_side/lsp.py`.

## 🌐 Browser console & network capture

With the opt-in browser enabled, each ordinary page also enables `Runtime.enable`, `Network.enable` and `Log.enable`. The manager buffers:

| Event | Buffered as | Notes |
| :--- | :--- | :--- |
| `Runtime.consoleAPICalled` | level + readable argument text | `RemoteObject` value / description / preview |
| `Runtime.exceptionThrown` | error + up to 10 stack frames | line/column are 1-based in the buffer |
| `Log.entryAdded` | browser log entry | `warning` is normalized to `warn` |
| `Network.responseReceived` | status >= 400 only | url / status / type / mime type |
| `Network.loadingFailed` | failure + error text | url resolved through `requestWillBeSent` |

Headers, cookies and bodies are never read. Each page keeps at most `debug_buffer_max` (200) entries per buffer; a main-frame `Page.frameNavigated` and every explicit navigate / back / forward clear them.

The model reads them through the merged tool: `browser(action="console", level="error")` returns console output plus uncaught exceptions, `browser(action="network", min_status=400, include_failures=true)` returns failed requests. `SHERRY_BROWSER_DEBUG_CAPTURE=0` disables the domains entirely; both actions then answer a disabled notice instead of pretending the page was quiet.

## 🧠 LSP edit-time consistency

The old `lsp_diagnostics` opened a file once and then read the server's cache: after an agent edit the server still held the previous text. The loop is now closed in four steps:

1. **Edit sync.** `LSPClient.sync_document` sends `textDocument/didOpen` (first sight) or `didChange` with the FULL text plus `didSave`. `patch_file`, `write_file`, `lsp_format(write=true)` and direct-file `ast_grep_rewrite` call `agent/tools/pub_base/lsp_sync.py`, which only pushes to an ALREADY-ACQUIRED server (`LspServerManager.peek`) — an edit never starts a language server on its own. Identical content is a no-op.
2. **Fresh diagnostics.** `lsp_diagnostics` refreshes the server from disk and waits for a publish generation NEWER than the one it observed (`diagnostics_generation` / `wait_diagnostics_since`). If the file did not change and a cache exists, it answers immediately instead of waiting on a republish that will not come.
3. **Passive injection.** `agent/tools/code_intel/lsp/diagnostics.py` aggregates `publishDiagnostics` per file and tracks delivery per session. The `lsp_diagnostics` reminder source (priority 84, transient, error severity only, 10 files / 20 errors) injects undelivered errors before the turn's first model call, once per diagnostic generation; a new publish reopens delivery.
4. **Finish evidence.** The same edit path debounces (`AUTO_DIAGNOSTICS_DEBOUNCE_S`) a background pass per file and appends an evidence row (`kind="lsp_diagnostics"`, `auto=true`, pass/fail). `taskflow_flow(action="finish")`'s regression gate replays those rows through the LSP client, not the shell allowlist: pass → `unmark`, fail → `broken`, no server/timeout → skipped. A type error introduced by an edit therefore surfaces as `[broken]` at finish.

The ninth LSP tool is `lsp_hover` (`textDocument/hover`, MarkupContent flattened to text). Robustness: a crashed server is restarted at most `lsp_max_restarts` (3) times before the tools degrade to tree-sitter / grep, and a `-32801` ContentModified answer is retried with 500/1000/2000 ms backoff.

## ⚙️ Configuration

| Env var | Default | Effect |
| :--- | :--- | :--- |
| `SHERRY_BROWSER_DEBUG_CAPTURE` | 1 | enable console/network/log domains |
| `AUTO_DIAGNOSTICS_ENABLED` | 1 | write auto-diagnostics evidence rows |
| `AUTO_DIAGNOSTICS_MAX_FILES` | 20 | recent files auto-checked per session |
| `AUTO_DIAGNOSTICS_TIMEOUT_S` | 5 | one background diagnostics pass |
| `AUTO_DIAGNOSTICS_DEBOUNCE_S` | 0.4 | coalesce rapid edits |
| `LSP_DIAGNOSTICS_INJECT_MAX_FILES` | 10 | reminder files per turn |
| `LSP_DIAGNOSTICS_INJECT_MAX_ERRORS` | 20 | reminder entries per turn |
| `LSP_MAX_RESTARTS` | 3 | crash restarts before degrading |
| `LSP_CONTENT_MODIFIED_MAX_RETRIES` | 3 | `-32801` retries |
| `LSP_CONTENT_MODIFIED_BACKOFF_S` | 0.5 | first backoff (doubles) |

## 🧪 Tests

- `tests/server/service/test_browser_debug_capture.py` — event shapes, failure-only network filtering, ring caps, navigation clears, disabled switch.
- `tests/agent/tools/test_browser_tools.py` — `console` / `network` actions, formatting and the disabled notice.
- `tests/agent/tools/code_intel/lsp/test_edit_sync.py` — didOpen/didChange/didSave, idempotent sync, publish generations, ContentModified retry.
- `tests/agent/tools/code_intel/lsp/test_diagnostics_registry.py` — per-session delivery, change detection, root scoping, caps.
- `tests/agent/tools/pub_base/test_lsp_sync.py` — edit sync, debounced evidence rows, fail-open.
- `tests/agent/tools/pub_base/test_regression_gate.py` — LSP row replay (pass → unmark, fail → broken, skip on no server).
- `tests/agent/middlewares/test_lsp_diagnostics_reminder.py` — reminder rendering and one-shot delivery.

## ⚠️ Limits

- Console/network capture is per page and dies with it; a full reload clears the buffers by design (the diagnosis is "what happened since this load").
- Only error-severity diagnostics are injected. Warnings stay available through `lsp_diagnostics`.
- The auto-diagnostics pass runs only when a server is already acquired; a project that never used LSP pays nothing but also gets no background check.
- `ast_grep_rewrite` syncs only direct file arguments: a directory apply does not report which files it touched.
