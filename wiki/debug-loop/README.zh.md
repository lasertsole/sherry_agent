# 🐞 调试闭环：浏览器捕获 + LSP 编辑期诊断

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 编码工作的两个闭环。浏览器记录页面真实发生了什么（console、未捕获异常、失败请求），reload 后可以据此定位而不是猜。语言服务器层保证它看到的文件内容与编辑工具写入的一致，把新错误在下一轮呈现，并写入一条 finish 门控可回放的诊断证据。

**事实来源：** `server/service/browser_manager.py`（`_BrowserDebugCaptureMixin`）、`agent/tools/browser/tools/browser_console.py`、`agent/tools/browser/tools/browser_network.py`、`agent/tools/browser/tools/browser_merged.py`、`agent/tools/code_intel/lsp/client.py`、`agent/tools/code_intel/lsp/manager.py`、`agent/tools/code_intel/lsp/diagnostics.py`、`agent/tools/pub_base/lsp_sync.py`、`agent/tools/pub_base/regression_gate.py`、`agent/middlewares/reminder_catalog/sources.py`、`config/features/infra_side/browser_agent.py`、`config/features/agent_side/lsp.py`。

## 🌐 浏览器 console 与 network 捕获

浏览器功能开启后，每个普通页面还会启用 `Runtime.enable`、`Network.enable` 与 `Log.enable`，管理器缓冲：

| 事件 | 缓冲内容 | 说明 |
| :--- | :--- | :--- |
| `Runtime.consoleAPICalled` | level + 可读参数文本 | `RemoteObject` 的 value / description / preview |
| `Runtime.exceptionThrown` | 错误 + 最多 10 层调用栈 | 缓冲区里的行列是 1 基 |
| `Log.entryAdded` | 浏览器日志 | `warning` 归一为 `warn` |
| `Network.responseReceived` | 仅 status ≥ 400 | url / status / 类型 / mime |
| `Network.loadingFailed` | 失败 + 错误原因 | url 由 `requestWillBeSent` 映射补齐 |

header、cookie 与 body 一律不读。每个页面每个缓冲区最多 `debug_buffer_max`（200）条；主 frame 的 `Page.frameNavigated` 以及每次显式 navigate / back / forward 都会清空。

模型通过合并工具读取：`browser(action="console", level="error")` 返回 console 输出与未捕获异常，`browser(action="network", min_status=400, include_failures=true)` 返回失败请求。`SHERRY_BROWSER_DEBUG_CAPTURE=0` 完全不启用这些域，两个 action 会明确返回“未启用”，而不是假装页面没有输出。

## 🧠 LSP 编辑期一致性

旧的 `lsp_diagnostics` 只在首次打开文件时发 `didOpen`，之后读 server 缓存：agent 编辑后，server 里仍是旧内容。现在闭环分为四步：

1. **编辑同步。** `LSPClient.sync_document` 首次发 `textDocument/didOpen`，之后发全文 `didChange` + `didSave`。`patch_file`、`write_file`、`lsp_format(write=true)` 与直接文件参数的 `ast_grep_rewrite` 调用 `agent/tools/pub_base/lsp_sync.py`，只向**已 acquire** 的 server 推送（`LspServerManager.peek`）——编辑本身绝不启动语言服务器。内容相同则完全不发。
2. **新鲜诊断。** `lsp_diagnostics` 从磁盘刷新 server 视图，并等待一个比观察值更新的 `publishDiagnostics` 代次（`diagnostics_generation` / `wait_diagnostics_since`）。若文件未变且已有缓存，立即返回，不空等一次不会到来的重发。
3. **被动注入。** `agent/tools/code_intel/lsp/diagnostics.py` 按文件聚合 `publishDiagnostics`，并按会话记录投递。`lsp_diagnostics` reminder source（priority 84、transient、只注入 error，上限 10 文件 / 20 条）在每轮首次 model call 前注入未投递错误；每个诊断代次只投递一次，新 publish 重新打开投递。
4. **finish 证据。** 同一条编辑路径会防抖（`AUTO_DIAGNOSTICS_DEBOUNCE_S`）启动一次后台诊断，并追加证据行（`kind="lsp_diagnostics"`、`auto=true`、pass/fail）。`taskflow_flow(action="finish")` 的 regression gate 通过 LSP client（而非 shell 白名单）回放这些行：pass → `unmark`，fail → `broken`，无 server/超时 → 跳过。编辑引入的类型错误因此在 finish 时以 `[broken]` 呈现。

第九个 LSP 工具是 `lsp_hover`（`textDocument/hover`，MarkupContent 拍平成文本）。鲁棒性：崩溃的 server 最多自动重启 `lsp_max_restarts`（3）次，之后工具降级到 tree-sitter / grep；`-32801` ContentModified 以 500/1000/2000 ms 退避重试。

## ⚙️ 配置

| 环境变量 | 默认 | 作用 |
| :--- | :--- | :--- |
| `SHERRY_BROWSER_DEBUG_CAPTURE` | 1 | 启用 console/network/log 域 |
| `AUTO_DIAGNOSTICS_ENABLED` | 1 | 写入自动诊断证据行 |
| `AUTO_DIAGNOSTICS_MAX_FILES` | 20 | 每会话最近自动检查文件数 |
| `AUTO_DIAGNOSTICS_TIMEOUT_S` | 5 | 单次后台诊断预算 |
| `AUTO_DIAGNOSTICS_DEBOUNCE_S` | 0.4 | 合并连续编辑 |
| `LSP_DIAGNOSTICS_INJECT_MAX_FILES` | 10 | 每轮注入文件数 |
| `LSP_DIAGNOSTICS_INJECT_MAX_ERRORS` | 20 | 每轮注入错误数 |
| `LSP_MAX_RESTARTS` | 3 | 降级前的崩溃重启上限 |
| `LSP_CONTENT_MODIFIED_MAX_RETRIES` | 3 | `-32801` 重试次数 |
| `LSP_CONTENT_MODIFIED_BACKOFF_S` | 0.5 | 首次退避（逐次翻倍） |

## 🧪 测试

- `tests/server/service/test_browser_debug_capture.py` —— 事件形状、仅失败网络、环形上限、导航清空、关闭开关。
- `tests/agent/tools/test_browser_tools.py` —— `console` / `network` action、格式化与未启用提示。
- `tests/agent/tools/code_intel/lsp/test_edit_sync.py` —— didOpen/didChange/didSave、幂等同步、publish 代次、ContentModified 重试。
- `tests/agent/tools/code_intel/lsp/test_diagnostics_registry.py` —— 按会话投递、变更检测、根目录过滤、容量上限。
- `tests/agent/tools/pub_base/test_lsp_sync.py` —— 编辑同步、防抖证据行、fail-open。
- `tests/agent/tools/pub_base/test_regression_gate.py` —— LSP 证据回放（pass → unmark、fail → broken、无 server 跳过）。
- `tests/agent/middlewares/test_lsp_diagnostics_reminder.py` —— reminder 渲染与一次性投递。

## ⚠️ 限制

- console/network 捕获按页面隔离，页面销毁即消失；整页 reload 按设计清空缓冲（诊断的是“本次加载以来发生了什么”）。
- 只注入 error 级诊断；warning 仍可通过 `lsp_diagnostics` 主动读取。
- 自动诊断只在已有 server 时运行；从不使用 LSP 的项目没有额外开销，也没有后台检查。
- `ast_grep_rewrite` 只同步直接文件参数：目录级 apply 不报告它实际改了哪些文件。
