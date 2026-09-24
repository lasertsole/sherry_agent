# 🧰 程序化工具调用（PTC）

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本文是[子代理设计页](../subagent/README.zh.md)（让 `execute_code` 仅归 executor 的两轴角色模型）、[子代理系统 README](../../agent/tools/subagent/README.zh.md)（角色表中列出该工具的运行时与 API 参考）与[工具沙箱页](../sandbox/README.zh.md)（`terminal` 与 `python_repl` 所解析的隔离栈）的设计层姊妹篇。本页记录 PTC 是什么、脚本为何在独立进程中执行、它如何抵达真实工具、这层约束真正保证了什么，以及在哪些地方它诚实地弱于 OS 沙箱。

事实来源：`agent/tools/ptc/**`、`config/features/agent_side/ptc.py`、`config/features/agent_side/tools_timeouts.py`、`agent/tools/subagent/types/functional_role.py`、`agent/tools/subagent/spawn/core.py`、`agent/tools/subagent/spawn/system_prompt.py`、`agent/tools/pub_base/env_scrub.py` 与 `tests/agent/tools/ptc/**`。下文每一条陈述均已对照该源码核实。

## 目录

- [总览](#-总览)
- [设计理由](#-设计理由)
- [协议与产物](#-协议与产物)
- [安全模型](#-安全模型)
- [配置](#-配置)
- [限制](#-限制)
- [失败模式](#-失败模式)
- [测试地图](#-测试地图)
- [相关文档](#-相关文档)

## 🎯 总览

程序化工具调用（PTC）是 `execute_code` 工具背后的能力：模型编写一段 Python 脚本，脚本通过生成的包装器同步调用 Sherry 的真实工具。它针对的是逐轮工具循环处理不佳的形态——三次以上且之间夹有处理逻辑的工具调用、循环与分支形态的工具使用，以及在进入模型上下文之前就应被过滤的大体积工具输出。

| 问题 | 答案 |
|------|------|
| 谁可以调用？ | 仅 `executor` 功能角色；闸门是 `PTC_ROLES`，因此主代理与其他任何功能角色都看不到该工具 |
| 脚本在哪里执行？ | 在独立的 Python 子进程中，每次调用现场派生并拥有自己的进程组，超时即整组终止 |
| 脚本如何抵达工具？ | `from sherry_tools import ...`——生成的 stub，每个包装器都是一条经 loopback TCP 发往父进程 RPC 服务的换行分隔 JSON 请求 |
| 工具运行在谁的会话中？ | 子进程自己的 `child_session_key`，在工具构造时绑定并注入每一次 RPC 派发——永远不是脚本参数 |
| 返回什么？ | 一个 JSON 信封，含 `status`（`ok` / `timeout` / `error` / `budget_exceeded`）、`output`、`error`、`exit_code` 与 `tool_calls_made` |

脚本就是普通 Python 加上生成的包装器——没有 async、没有上下文对象，每个工具一次同步调用：

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

## 🧠 设计理由

**独立进程，而非进程内 `exec`。** “受限 builtins”与“清洗过的环境”只有在解释器是一次性的时候才有意义。父进程在子进程中装入精简的 `__builtins__` 命名空间，并把 `env=scrub_env()` 交给一个它可以 SIGKILL 的进程。墙钟预算需要可被杀死的进程组；意外的死循环、崩溃或 fork 炸弹都留在子进程内。进程内 `exec` 会与父进程共享全局变量，无法以上述方式终止。

**做桥接，而非传递工具对象。** 真实工具是异步 `BaseTool`：需要父进程事件循环、模型提供方配置，以及用于解析状态的会话——这些恰恰不该进入一次性解释器。生成的 stub 只通过 socket 传参数，父进程用 `asyncio.run_coroutine_threadsafe` 把每个请求派发到自己的事件循环上，因此阻塞的子进程永远不会阻塞父进程的异步工作。

**与 `python_repl` 的分工。** 两个执行面都以镜像的受限 builtins 集合与同一个 `scrub_env` 执行模型编写的 Python，也都把子进程的启动目录设为 `ROOT_DIR`。它们的形态不同：`python_repl` 在 30 秒预算内运行单个代码片段且没有任何工具访问；`execute_code` 则运行更长的脚本（120 秒预算），可以同步调用最多 50 次真实工具。正是这份额外的触达使 PTC 仅限 executor，也正是它额外引入进程组、RPC 桥、调用预算与导入白名单的原因。executor 角色保留 `python_repl`；PTC 是增量能力，不是替代品。

**工具面只有一处命名闸门。** `PTC_ROLES`——`agent/tools/subagent/types/functional_role.py` 中包含 `executor` 的唯一集合——同时被注入点（`spawn/core.py`，位于角色 allow/deny 策略之后）与提示词构建器（`spawn/system_prompt.py`，一个 PTC 指引小节）读取。因此工具面与它的提示词小节不可能各自漂移，executor 的角色定义文件也不必携带任何 PTC 专属接线。

**故障关闭的工具集合。** `build_ptc_tool` 用白名单过滤子进程可用的工具，并剔除 `execute_code` 自身。列出 executor 并未携带的工具是无害的；实际执行的是交集，而 `sessions_spawn` / `sessions_yield` / `sessions_kill` / `sessions_steer` / `memory` / `skill_manage` / `question` 永远不在候选之中。

## 🔌 协议与产物

五个模块，各担一责：

| 产物 | 职责 |
|------|------|
| `tool.py` | 定义 `ExecuteCodeTool`（`execute_code`），绑定子会话，将 `available_tools` 与 `ptc_allowed_tools` 求交，并依据实时工具 schema 渲染面向模型的描述 |
| `runner.py` | `run_ptc` 编排单次调用：启动 RPC 服务、创建临时目录、生成 `sherry_tools.py`、写入包装脚本、派生子进程、捕获并截断输出，最后清理 |
| `rpc_server.py` | loopback TCP 监听器：按行解析 JSON 请求、执行每脚本调用预算，并把每次调用派发到父事件循环 |
| `stub_generator.py` | 从每个工具的 `tool_call_schema` 推导 `ToolStub` 规格，并渲染 `sherry_tools.py` 模块——每个工具一个同步包装器，外加本地辅助函数 |
| `builtins.py` | 持有子进程的精简 builtin 命名空间与导入白名单，并把两者渲染进包装脚本 |

线协议是双向的、每行一个 JSON 对象：

```json
{"tool": "read_file", "args": {"file_path": "README.md"}}
{"ok": true, "result": "..."}
{"ok": false, "error": "PTC call budget exhausted", "code": "PTCCallBudgetExceeded"}
```

**签名保真。** stub 参数取自 `tool_call_schema` 而非 `args_schema`，因此框架注入的参数（例如 `session_id`）永远不会成为可调用参数。必填字段渲染为必填参数；可选字段以 Python 字面量形式渲染其默认值，而无法用字面量表达的默认值回退为 `None`——无论如何，真实工具会对每次调用重新校验。生成的模块还携带三个绝不触碰 RPC 桥的本地辅助函数：`json_parse`（宽松的 `json.loads`）、`shell_quote`（`shlex.quote`）与 `retry`（指数退避）。

**产物与生命周期。** 每次调用都会获得全新的 `sherry_ptc_*` 临时目录，内含 `sherry_tools.py` 与包装脚本；子进程唯一的路径增补是 `PYTHONPATH=<tmpdir>`。包装器把用户脚本的 stdout 与 stderr 捕获进缓冲区，向真实 stdout 打印单个 JSON 信封，runner 据此归类出 `status` 字段。`finally` 块停止 RPC 服务、join 其线程、删除临时目录，并在子进程意外存活时再次击杀。

**子会话。** `spawn/core.py` 把本次运行的 `child_session_key` 作为工具的会话传入，`rpc_server.py` 则通过 runnable config 把它注入每次 `ainvoke` 调用。脚本无法提供或伪造会话：`session_id` 根本不是工具参数。

## 🔒 安全模型

以下控制属于能力削减，而非 OS 边界——诚实定位见[限制](#-限制)。

1. **环境清洗。** 子进程环境从 `scrub_env()` 出发：名称含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL`、`PASSWD`、`AUTH`、`DSN`、`WEBHOOK`、`BEARER` 或 `APIKEY`（大小写不敏感）的变量被丢弃，Sherry 自身的 `*_API_KEY` 变量被显式拒绝，而 `PATH` 等关键名称依照精确名称优先级保留。随后 runner 只增补 `PYTHONPATH` 与 `PYTHONUNBUFFERED`。
2. **受限 builtins。** 包装器以精简的 `__builtins__` 执行用户代码：`open`、`exec`、`eval`、`compile`、`globals`、`locals`、`input`、`breakpoint` 均不存在，`exit`、`quit`、`help` 同样缺席。`print`、集合类型与 `getattr` 系列内省辅助函数则保留。
3. **受控导入。** `__import__` 没有被整体移除——导入字节码需要它——但被替换为只解析白名单的守卫（`sherry_tools`、`json`、`re`、`math`、`time`、`csv`、`datetime`、`collections`、`itertools`、`functools`、`statistics`、`string`、`textwrap`、`decimal`、`random`、`fractions`、`heapq`、`bisect`）。`import os` 抛 `ImportError`；`json` 与 `sherry_tools` 放行。
4. **每脚本工具调用预算。** RPC 服务拒绝第 51 次调用：预算检查在派发之前运行，超限抛 `PTCCallBudgetExceeded`，信封状态变为 `budget_exceeded`。未知工具名被拒绝且不消耗预算。
5. **墙钟超时与进程组击杀。** 子进程在 `ptc_timeout_seconds`（120 秒）下运行。超时后 runner 对整组进程发 SIGKILL（`start_new_session=True`），孙进程随之死亡；随后捕获管道中尚存的所有输出并报告 `status: "timeout"`。
6. **输出截断上限。** stdout 截断于 50 KB、stderr 截断于 10 KB，各自带有显式的 `...[truncated N bytes]` 标记，之后信封才抵达模型。
7. **RPC 仅绑 loopback。** 监听器在 `127.0.0.1` 上绑定临时端口；构造函数直接拒绝 `0.0.0.0`、`::` 与空主机，且在把地址交给子进程之前会再次核验。
8. **子会话隔离。** 每一次派发的工具调用都在其 runnable config 中携带子进程的 `session_id`，工具据此针对子会话解析状态。父会话永远不会经 PTC 暴露。
9. **防递归与特权剔除。** `execute_code` 不在自身白名单中，spawn、memory、技能管理与提问类工具被配置排除——脚本无法在沙箱内派生代理、管理技能或询问用户。
10. **主代理不可见。** `_MAIN_TOOLS_BUILDERS` 中没有注册任何 PTC 构建器；该工具只在子代理解析组装时构造，且仅当角色属于 `PTC_ROLES`。

## ⚙️ 配置

| 键 | 默认值 | 管辖范围 |
|----|--------|----------|
| `ptc_timeout_seconds` | 120 | 单个脚本的墙钟预算，以及单次工具调用的派发窗口 |
| `ptc_max_tool_calls` | 50 | 单个脚本在触发预算错误前可派发的工具调用数 |
| `ptc_max_stdout_bytes` | 50000 | 截断之前保留的子进程 stdout 字节数 |
| `ptc_max_stderr_bytes` | 10000 | 截断之前保留的子进程 stderr 字节数 |
| `ptc_allowed_tools` | `read_file`、`write_file`、`patch_file`、`terminal`、`search_files`、`web_search` | 与子进程可用工具求交的白名单 |

该对象位于 `config/features/agent_side/ptc.py`；`config/features/agent_side/tools_timeouts.py` 中的共享逐工具注册表也声明了 `ptc_timeout_seconds` 并取相同的 120 秒默认值，而 runner 的超时来自 `PTC` 对象。PTC 没有新增任何 pip 依赖：其实现是标准库加上项目既有的 LangChain 与 loguru。

## ⚠️ 限制

- **它不是 OS 级沙箱。** 受限命名空间与导入守卫约束的是字面脚本能指称的名字，但 `getattr` 及其他内省 builtins 仍然可用，因此有决心的脚本可以沿可达对象继续走，触及其字面表面之外的更多能力。这层约束属于受限命名空间层级——与既有 `python_repl` 包装器 builtins 同级。从隔离角度看更弱的一点是：PTC 子进程被直接派生，**没有**经过 `terminal` 与 `python_repl` 所解析的 OS 原生沙箱后端，因此不会继承它们的写包含与敏感路径读屏蔽；这些后端究竟提供什么，见[工具沙箱页](../sandbox/README.zh.md)。
- **超时取消是尽力而为。** 当工具调用超出派发窗口，服务会取消自己持有的 future，但已开始在父循环上执行的协程仍可能跑完；脚本收到超时错误，而工具的副作用可能仍在继续。
- **每次调用都付启动成本。** 没有预热池：每次 `execute_code` 都创建临时目录、生成 stub、启动 RPC 线程与全新解释器，再全部拆除。一次再简单的调用也要付出一次进程启动。
- **子进程的工作目录是仓库根目录。** runner 将子进程的 `cwd` 默认为 `ROOT_DIR`，且 PTC 工具不会转发子代理的派生工作目录，因此脚本内的相对路径从仓库根解析，而不是从 executor 的派生位置。
- **输出截断按设计有损。** 超过 stdout 50 KB 或 stderr 10 KB 之后，尾部被替换为字节数标记；打印大表格的脚本只会得到被截断的版本。
- **报告依赖信封。** 如果解释器在包装器打印信封之前失败，就没有可解析的结构化输出；runner 把原始 stderr 作为错误回传，stdout 可能为空。
- **RPC 监听器无鉴权。** 它绑定临时 loopback 端口，且只存活于单个脚本的执行期间，但在这段窗口内任何本机进程都可能连上，并以子会话身份调用白名单内的工具；该设计信任本机。

## 🧯 失败模式

| 失败 | 可观察表现 | 降级行为 |
|------|------------|----------|
| 预算耗尽 | `status: "budget_exceeded"`，错误中点名 `PTCCallBudgetExceeded` | 后续调用持续失败；脚本可以捕获该错误，父回合仍会收到部分输出 |
| 墙钟超时 | `status: "timeout"`，`exit_code` 为 SIGKILL 导致的负值 | 进程组已死；在截断上限内捕获部分 stdout 与 stderr；父回合照常继续 |
| 输出截断 | `output` 或 `error` 以 `...[truncated N bytes]` 结尾 | 脚本已完成；只有回传给模型的那一份被裁掉 |
| 工具错误 | RPC 响应 `{"ok": false, "error": "TypeError: ..."}` | stub 抛 `RuntimeError`；脚本可以捕获，未捕获者落入信封的 `error` |
| 导入被拒 | `ImportError: import of 'os' is not allowed in PTC` | 脚本可以捕获；未捕获者落入信封的 `error` |
| RPC 断开 | stub 抛 `RuntimeError: PTC RPC connection closed before a response arrived` | 服务返回 `accept` 并继续服务重连的子进程；不重试的脚本以错误信封失败 |
| 未知工具名 | RPC 响应 `{"ok": false, "error": "unknown tool: ..."}` | 调用被拒且不消耗预算，脚本可以回退到其它工具 |
| 解释器级失败 | 没有信封；原始 stderr 成为 `error`，`status: "error"` | stdout 可能为空；模型看到的是解释器自己的消息，而不是结构化报告 |

## 🗺️ 测试地图

| 领域 | 测试 |
|------|------|
| 工具面：身份、白名单求交、绑定会话、防递归 | `tests/agent/tools/ptc/test_tool.py` |
| stub 生成：对照真实 schema 的签名匹配、注入参数排除、本地辅助函数 | `tests/agent/tools/ptc/test_stub_generator.py` |
| RPC 协议：loopback 拒绝、JSON 往返、预算、工具错误、重连、停止 | `tests/agent/tools/ptc/test_rpc_server.py` |
| 子进程：端到端工具调用、受限 builtins、导入门、超时击杀、截断、环境清洗、临时目录清理 | `tests/agent/tools/ptc/test_runner.py` |
| 角色网格：executor 注入、其余四个角色、提示词指引、主注册表隔离 | `tests/agent/tools/ptc/test_integration.py` |
| 真实 LLM 端到端：executor 子代理选择 `execute_code` 并经 RPC 读取文件 | `tests/agent/tools/subagent/test_ptc_executor_e2e.py` |

PTC 的五个测试套件运行在密闭分组（`unit`、`integration`、`module`）中。端到端文件是唯一的 `llm_e2e` 测试：默认反选，在专用的真实 LLM 作业中运行，验证真实的模型路径——子代理必须调用 `execute_code`，捕获的信封必须报告已派发 RPC 调用，且打印的行数必须与测试时从文件现算的行数一致；由于沙箱内没有 `open`，不经过 `read_file` 就无法产出这个数字。

## 🔗 相关文档

| 页面 | 覆盖内容 |
|------|----------|
| [子代理设计](../subagent/README.zh.md) | 两轴角色模型（深度角色 × 功能角色），以及 `execute_code` 为何仅归 executor |
| [子代理系统 README](../../agent/tools/subagent/README.zh.md) | 运行时与 API 参考：spawn 流水线、含工具清单的角色表与注册表行为 |
| [工具沙箱](../sandbox/README.zh.md) | 环境清洗，以及 `terminal` 与 `python_repl` 所解析的 OS 原生隔离——PTC 并未使用的那层约束 |
