# PTC（编程式工具调用）— Subagent 专属实现计划

> 基于对 `hermes-agent-main`（Python 子进程 + UDS/TCP RPC）和 `deepagents-main`（QuickJS 嵌入式 JS 沙箱）的 PTC 实现调研，为 Sherry 设计子进程 + TCP RPC 方案的 PTC。
>
> **核心约束：仅限 subagent 可用，main agent 无法调用。**

---

## 目录

1. [架构概览](#架构概览)
2. [设计决策](#设计决策)
3. [新增文件（7 个）](#新增文件7-个)
4. [修改文件（5 个）](#修改文件5-个)
5. [测试计划（5 个文件）](#测试计划5-个文件)
6. [实施步骤顺序](#实施步骤顺序)
7. [安全清单](#安全清单)
8. [关键参考文件](#关键参考文件)

---

## 架构概览

```
┌─ Parent process (async event loop) ──────────────────────┐
│                                                           │
│  execute_code(code, session_id)                          │
│  ├─ 1. 生成 sherry_tools.py stub（RPC client）            │
│  ├─ 2. 启动 TCP RPC listener（后台线程）                  │
│  ├─ 3. spawn 子进程: python -c "用户代码"                 │
│  ├─ 4. 等待子进程完成 / 超时                               │
│  └─ 5. 捕获 stdout → 返回给模型                           │
│                                                           │
│  RPC Listener Thread:                                     │
│  ├─ accept child connection                               │
│  ├─ read JSON request {tool_name, args}                   │
│  ├─ asyncio.run_coroutine_threadsafe(tool._arun, loop)   │
│  ├─ enforce call budget (max 50)                          │
│  └─ write JSON response {result / error}                  │
└──────────────┬────────────────────────────────────────────┘
               │ TCP localhost (cross-platform)
               ▼
┌─ Child process (python) ─────────────────────────────────┐
│                                                           │
│  from sherry_tools import read_file, web_search, ...     │
│                                                           │
│  data = read_file("/path/to/file")                        │
│  results = web_search("query")                            │
│  print(process(data, results))  # → stdout (captured)     │
│                                                           │
│  sherry_tools internals:                                  │
│  ├─ connect to TCP socket                                 │
│  ├─ send JSON {tool, args} → block → recv JSON {result}  │
│  └─ sync wrapper around async parent-side tool dispatch   │
└───────────────────────────────────────────────────────────┘
```

**关键设计：** 子进程的工具调用是同步的（发请求→阻塞等响应），父进程的 RPC listener 线程通过 `asyncio.run_coroutine_threadsafe` 派发到事件循环——子进程的阻塞不影响父进程的 async。

---

## 设计决策

### 为什么选子进程 + TCP RPC 而非进程内 exec

| 维度     | 子进程 + TCP RPC（选定）           | 进程内 async exec         |
| -------- | ---------------------------------- | ------------------------- |
| 隔离性   | 真进程隔离，受限 builtins          | 弱隔离，exec 在主进程     |
| 稳定性   | 子进程崩溃不影响主进程             | exec 崩溃可能影响事件循环 |
| 复杂度   | 高（RPC + stub 生成 + 子进程管理） | 低                        |
| 跨平台   | TCP 无 US 限制                     | N/A                       |
| 异步工具 | `run_coroutine_threadsafe` 桥接    | 天然 async                |

### 为什么仅限 subagent

- Main agent 应该逐步推理，不应批量调工具
- Subagent 是任务执行者，PTC 的批量能力更适合
- 防止 main agent 利用 PTC 绕过 middleware 链（guardrails、HITL 等）

### Main agent 不可用的实现方式

PTC tool **不加入** `_MAIN_TOOLS_BUILDERS`，只在 `_build_child_agent()` 中注入：

```python
# spawn/core.py::_build_child_agent — 仅 subagent 路径
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# ↓↓↓ 新增：仅 subagent 注入 PTC
from agent.tools.ptc import build_ptc_tool
ptc_tool = build_ptc_tool(available_tools=filtered_tools, session_id=run.child_session_key)
final_tools = [*filtered_tools, ptc_tool]
```

Main agent 的 `built_agent()` 用 `get_agent_tools()` → `_tools = build_main_tools()` — 无 PTC tool。

---

## 新增文件（7 个）

### 1. `config/features/agent_side/ptc.py`

PTC 配置 TypedDict + 默认实例。

```python
class PtcConfig(TypedDict):
    ptc_timeout_seconds: int           # 子进程超时 (默认 120)
    ptc_max_tool_calls: int            # 每脚本最大工具调用数 (默认 50)
    ptc_max_stdout_bytes: int          # stdout 截断 (默认 50_000)
    ptc_max_stderr_bytes: int          # stderr 截断 (默认 10_000)
    ptc_allowed_tools: list[str]       # PTC 可调用的工具白名单

PTC: PtcConfig = {
    "ptc_timeout_seconds": 120,
    "ptc_max_tool_calls": 50,
    "ptc_max_stdout_bytes": 50_000,
    "ptc_max_stderr_bytes": 10_000,
    "ptc_allowed_tools": [
        "read_file", "write_file", "patch",
        "search_files", "web_search", "terminal",
    ],
}
```

**遵循约定：** 一个 TypedDict + 一个实例，re-export via `__init__.py`，NEVER bare dict。

### 2. `agent/tools/ptc/__init__.py`

```python
from .tool import build_ptc_tool, ExecuteCodeTool
```

导出 `build_ptc_tool(available_tools, session_id)` 工厂。

### 3. `agent/tools/ptc/tool.py` — LangChain Tool 定义

```python
class ExecuteCodeInput(BaseModel):
    code: str
    session_id: SessionId = ""

class ExecuteCodeTool(BaseTool):
    name = "execute_code"
    # description 动态生成：列出可调用工具的签名
    # metadata = {"idempotent": False, "scope": "subagent_only"}

    def __init__(self, available_tools, session_id):
        # 过滤 available_tools ∩ PTC_ALLOWED_TOOLS
        # 生成 description（含工具签名列表）
        # 存储 session_id 和 filtered tools map

    async def _arun(self, code, session_id, run_manager):
        return await _run_ptc(code, self._tools_map, session_id, self._config)
```

**description 生成**（参考 hermes `build_execute_code_schema`）：

```
Run a Python script that can call Sherry tools programmatically.
Use this when you need 3+ tool calls with processing logic between them,
need to filter/reduce large tool outputs before they enter your context,
need conditional branching (if X then Y else Z), or need to loop
(fetch N pages, process N files, retry on failure).

Use normal tool calls instead when: single tool call with no processing,
you need to see the full result and apply complex reasoning,
or the task requires interactive user input.

Available via `from sherry_tools import ...`:

  read_file(file_path: str, offset: int = 1, limit: int = 500) -> dict
  write_file(file_path: str, content: str) -> dict
  patch(file_path: str, old_string: str, new_string: str) -> dict
  search_files(pattern: str, ...) -> dict
  web_search(query: str, limit: int = 5) -> dict
  terminal(command: str, timeout: int = None) -> dict

Limits: 120s timeout, 50KB stdout cap, max 50 tool calls per script.
Print your final result to stdout.

Also available (no import needed — built into sherry_tools):
  json_parse(text: str) — json.loads with strict=False
  shell_quote(s: str) — shlex.quote()
  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff
```

### 4. `agent/tools/ptc/rpc_server.py` — TCP RPC 服务端

```python
class PtcRpcServer:
    """TCP RPC listener for PTC tool-call dispatch."""

    def __init__(self, tools_map, loop, max_calls, session_id):
        # tools_map: {tool_name: BaseTool}
        # loop: parent event loop (for run_coroutine_threadsafe)
        # max_calls: call budget
        # session_id: child session key for tool dispatch

    async def start(self) -> tuple[str, int]:
        # bind TCP 127.0.0.1:0 (ephemeral port)
        # return (host, port)

    async def serve(self):
        # accept 1 connection
        # loop: read JSON line → dispatch → write JSON line
        # dispatch: asyncio.run_coroutine_threadsafe(tool._arun(...), self.loop)
        #   then await the future result (blocks listener thread, not event loop)

    def _dispatch(self, tool_name, args) -> dict:
        # budget check (max_calls)
        # resolve tool from tools_map
        # call tool._arun or tool.ainvoke with session_id injected
        # return {ok: True, result} or {ok: False, error}
```

**协议：** 每行一个 JSON 消息（newline-delimited JSON）：

- Request: `{"tool": "read_file", "args": {"file_path": "/etc/hosts"}}`
- Response: `{"ok": true, "result": "...content..."}` 或 `{"ok": false, "error": "...message..."}`

### 5. `agent/tools/ptc/stub_generator.py` — 生成 `sherry_tools.py`

```python
def generate_stub(host: str, port: int, tool_docs: list[tuple[str, str]]) -> str:
    """Generate sherry_tools.py source for the child process."""
```

生成的 stub 包含：

```python
# === AUTO-GENERATED sherry_tools.py ===
import json, socket, sys

_RPC_HOST = "127.0.0.1"
_RPC_PORT = {port}

def _rpc_call(tool_name, args):
    """Send RPC request, block for response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((_RPC_HOST, _RPC_PORT))
    sock.sendall((json.dumps({"tool": tool_name, "args": args}) + "\n").encode())
    resp = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        resp += chunk
        if b"\n" in resp:
            break
    sock.close()
    msg = json.loads(resp.decode().strip())
    if not msg["ok"]:
        raise RuntimeError(msg["error"])
    return msg["result"]

# === Tool wrappers ===
def read_file(file_path, offset=1, limit=500):
    return _rpc_call("read_file", {"file_path": file_path, "offset": offset, "limit": limit})

def web_search(query, limit=5):
    return _rpc_call("web_search", {"query": query, "limit": limit})

# ... one wrapper per allowed tool ...

# === Built-in helpers ===
def json_parse(text):
    import json as _json
    return _json.loads(text, strict=False)

def shell_quote(s):
    import shlex
    return shlex.quote(s)

def retry(fn, max_attempts=3, delay=2):
    import time
    for i in range(max_attempts):
        try:
            return fn()
        except Exception:
            if i == max_attempts - 1:
                raise
            time.sleep(delay * (2 ** i))
```

### 6. `agent/tools/ptc/runner.py` — 执行编排

```python
async def run_ptc(code: str, tools_map: dict, session_id: str, config: PtcConfig) -> str:
    """Orchestrate: start RPC → spawn child → capture stdout → return."""
    import tempfile, subprocess, sys, os, threading, asyncio

    # 1. Start RPC server
    rpc = PtcRpcServer(tools_map, asyncio.get_running_loop(),
                       config["ptc_max_tool_calls"], session_id)
    host, port = await rpc.start()

    # 2. Generate stub + write to tempdir
    tmpdir = tempfile.mkdtemp(prefix="sherry_ptc_")
    stub_code = generate_stub(host, port, tool_docs)
    stub_path = os.path.join(tmpdir, "sherry_tools.py")
    with open(stub_path, "w") as f:
        f.write(stub_code)

    # 3. Write user script
    script_path = os.path.join(tmpdir, "script.py")
    with open(script_path, "w") as f:
        f.write(code)

    # 4. Start RPC serve in background thread
    serve_thread = threading.Thread(target=lambda: asyncio.run(rpc.serve()))
    serve_thread.daemon = True
    serve_thread.start()

    # 5. Spawn child process
    env = scrub_env()  # reuse existing env scrub
    env["PYTHONPATH"] = tmpdir  # so `import sherry_tools` works
    cwd = ...  # resolve from session state (subagent's spawned_cwd)

    proc = subprocess.Popen(
        [sys.executable, script_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=cwd,
    )

    # 6. Wait for child (timeout)
    try:
        stdout, stderr = proc.communicate(timeout=config["ptc_timeout_seconds"])
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return json.dumps({"status": "timeout", "error": "..."})

    # 7. Truncate
    stdout = stdout[:config["ptc_max_stdout_bytes"]]
    stderr = stderr[:config["ptc_max_stderr_bytes"]]

    # 8. Return result
    return json.dumps({
        "status": "ok" if proc.returncode == 0 else "error",
        "output": stdout,
        "stderr": stderr if proc.returncode != 0 else "",
        "exit_code": proc.returncode,
        "tool_calls_made": rpc.calls_made,
    }, ensure_ascii=False)
```

**关键点：**

- 环境变量清洗：复用 `agent/tools/pub_base/env_scrub.py` 的 `scrub_env()`
- CWD：从 `runtime/session/state_register.py` 读 subagent 的 `spawned_cwd`
- 子进程隔离：独立 Python 进程，受限 builtins（通过 stub 脚本的 wrapper 注入）

### 7. `agent/tools/ptc/builtins.py` — 子进程限制 builtins

```python
RESTRICTED_BUILTINS = {
    "True": True, "False": False, "None": None,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "len": len, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "reversed": reversed, "sorted": sorted,
    "any": any, "all": all, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "pow": pow,
    "print": print, "type": type,
    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
    "dir": dir, "vars": vars, "id": id, "repr": repr,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "AttributeError": AttributeError,
    "RuntimeError": RuntimeError, "ZeroDivisionError": ZeroDivisionError,
}
```

注入到子进程脚本的全局命名空间，阻止 `open`/`__import__`/`exec`/`eval`/`compile` 等。

**注意：** 子进程通过 `import sherry_tools` 获得工具代理，通过 `import json/re/math/csv/datetime/collections` 获得标准库。`__import__` 被阻止但 Python 的 `import` 语句仍可用（因为 import 语句不走 `__import__` builtins，而是走 import system）。需要在 wrapper 脚本中显式 import 允许的标准库模块并注入到 globals。

---

## 修改文件（5 个）

### 1. `agent/tools/subagent/spawn/core.py` — `_build_child_agent()` (约 line 818)

**现有代码：**

```python
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# ... LLM selection ...

child_agent = create_agent(
    ...
    tools=filtered_tools,
    ...
)
```

**修改后：**

```python
base_tools = tools if tools is not None else build_main_tools()
filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

# Inject PTC tool — subagent only, never available to main agent
from agent.tools.ptc import build_ptc_tool
ptc_tool = build_ptc_tool(
    available_tools=filtered_tools,
    session_id=run.child_session_key,
)
final_tools = [*filtered_tools, ptc_tool]

# ... LLM selection (unchanged) ...

child_agent = create_agent(
    ...
    tools=final_tools,  # ← was filtered_tools
    ...
)
```

**影响范围：**

- Main agent：`built_agent()` → `get_agent_tools()` → `_tools = build_main_tools()` — 不含 PTC tool，无需修改
- Leaf subagent：自动获得 PTC tool
- Orchestrator subagent：自动获得 PTC tool

### 2. `agent/tools/subagent/spawn/system_prompt.py` — 增加 PTC 指导

在 `build_subagent_system_prompt()` 的 sections 列表末尾追加条件 section：

```python
# Section 7: PTC (always present for subagents since they always get execute_code)
sections.append(
    "## Programmatic Tool Calling (execute_code)\n"
    "You have an `execute_code` tool that runs Python with tool access.\n"
    "Use it when:\n"
    "- 3+ tool calls with processing logic between them\n"
    "- Need to filter/reduce large outputs before they enter your context\n"
    "- Need conditional branching or loops over tool calls\n"
    "Don't use it for: single tool calls, tasks needing complex reasoning, "
    "or user interaction.\n"
    "Print your final result to stdout."
)
```

### 3. `config/features/agent_side/tools_timeouts.py`

在 `ToolsTimeoutsConfig` TypedDict 中增加字段，在 `TOOLS_TIMEOUTS` 实例中增加值：

```python
class ToolsTimeoutsConfig(TypedDict):
    # ... existing fields ...
    ptc_timeout_seconds: int          # ← 新增

TOOLS_TIMEOUTS: ToolsTimeoutsConfig = {
    # ... existing values ...
    "ptc_timeout_seconds": 120,      # ← 新增
}
```

### 4. `config/features/agent_side/__init__.py`

re-export PTC 配置：

```python
from .ptc import (
    PTC as PTC,
    PtcConfig as PtcConfig,
)
```

### 5. `config/features/__init__.py`

re-export PTC 配置（顶层聚合）：

```python
from .agent_side import (
    # ... existing exports ...
    PTC as PTC,
    PtcConfig as PtcConfig,
)
```

---

## 测试计划（5 个文件）

| 文件                                           | 标记          | 覆盖点                                                                                                                                                         |
| ---------------------------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/agent/tools/ptc/test_rpc_server.py`     | `unit`        | TCP RPC 握手、JSON 序列化/反序列化、call budget 耗尽→`PTCCallBudgetExceeded`、工具调用超时、并发请求处理、连接断开恢复                                         |
| `tests/agent/tools/ptc/test_stub_generator.py` | `unit`        | stub 生成正确性、函数签名匹配实际工具 schema、import 路径、辅助函数（json_parse/shell_quote/retry）                                                            |
| `tests/agent/tools/ptc/test_runner.py`         | `integration` | 端到端：spawn child → RPC → stdout capture、超时 kill、env scrub 验证、stderr 捕获、exit code 传播                                                             |
| `tests/agent/tools/ptc/test_tool.py`           | `unit`        | execute_code tool schema 生成、description 动态工具列表（只列 allowed ∩ available）、metadata scope 标签、session_id 注入                                      |
| `tests/agent/tools/ptc/test_integration.py`    | `module`      | 在 `_build_child_agent` 中注入 PTC tool、main agent 无 PTC tool（`build_main_tools()` 不含 execute_code）、工具白名单交集计算、PTC tool 不在白名单中（防递归） |

### 关键测试用例

```python
# test_rpc_server.py
async def test_call_budget_exceeded():
    """超过 max_calls 次调用应返回 PTCCallBudgetExceeded 错误。"""

async def test_tool_dispatch_via_event_loop():
    """RPC listener 线程通过 run_coroutine_threadsafe 正确派发到事件循环。"""

async def test_json_protocol_roundtrip():
    """JSON request/response 序列化/反序列化正确。"""

# test_runner.py
async def test_timeout_kills_child():
    """超时后子进程被 kill，返回 status=timeout。"""

async def test_env_scrub_removes_secrets():
    """子进程环境中不含 *_KEY/*_TOKEN/*_SECRET 变量。"""

async def test_stdout_truncation():
    """超过 max_stdout_bytes 的 stdout 被截断。"""

# test_integration.py
async def test_main_agent_has_no_ptc():
    """build_main_tools() 返回值中不含 execute_code。"""

async def test_subagent_has_ptc():
    """_build_child_agent 构建的工具列表中包含 execute_code。"""

async def test_ptc_not_in_own_whitelist():
    """execute_code 不在 ptc_allowed_tools 中（防递归）。"""
```

---

## 实施步骤顺序

| 步骤 | 内容                                                                            | 依赖      | 预估工时 |
| ---- | ------------------------------------------------------------------------------- | --------- | -------- |
| 1    | 创建 `config/features/agent_side/ptc.py` + 修改 re-exports（`__init__.py` × 2） | 无        | 0.5h     |
| 2    | 修改 `tools_timeouts.py` 增加 `ptc_timeout_seconds`                             | 步骤 1    | 0.25h    |
| 3    | 创建 `agent/tools/ptc/builtins.py`                                              | 无        | 0.5h     |
| 4    | 创建 `agent/tools/ptc/rpc_server.py`                                            | 步骤 1, 3 | 2h       |
| 5    | 创建 `agent/tools/ptc/stub_generator.py`                                        | 步骤 4    | 1h       |
| 6    | 创建 `agent/tools/ptc/runner.py`                                                | 步骤 4, 5 | 2h       |
| 7    | 创建 `agent/tools/ptc/tool.py` + `__init__.py`                                  | 步骤 6    | 1h       |
| 8    | 修改 `spawn/core.py` — `_build_child_agent` 注入 PTC                            | 步骤 7    | 0.5h     |
| 9    | 修改 `spawn/system_prompt.py` — 增加 PTC 指导                                   | 步骤 1    | 0.25h    |
| 10   | 编写测试（5 个文件）                                                            | 步骤 8, 9 | 3h       |
| 11   | `uv run --with ruff ruff check . && ruff format --check .`                      | 步骤 10   | 0.5h     |
| 12   | `uv run --no-sync basedpyright agent/tools/ptc/`                                | 步骤 11   | 0.5h     |
| 13   | `uv run pytest tests/agent/tools/ptc -q`                                        | 步骤 12   | 1h       |

**总预估：约 13 小时**

---

## 安全清单

| 维度                         | 措施                                                                                             | 状态                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------- |
| **工具白名单**               | 只有 6 个安全工具可 PTC 调用（read_file, write_file, patch, search_files, web_search, terminal） | `PTC["ptc_allowed_tools"]`            |
| **环境变量清洗**             | 子进程复用 `scrub_env()`，剥离 `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD`/`*_CREDENTIAL`          | `runner.py`                           |
| **受限 builtins**            | 无 `open`/`__import__`/`exec`/`eval`/`compile`/`globals`/`locals`/`__builtins__`                 | `builtins.py`                         |
| **调用预算**                 | 每脚本最多 50 次工具调用，超限报错终止                                                           | `rpc_server.py`                       |
| **超时**                     | 子进程 wall-clock 超时（默认 120s），`proc.kill()` 清理                                          | `runner.py`                           |
| **stdout 截断**              | 50KB stdout / 10KB stderr，防上下文窗口污染                                                      | `runner.py`                           |
| **session 隔离**             | PTC 工具用 subagent 的 `child_session_key` 调用工具，不泄漏 parent session                       | `tool.py`                             |
| **PTC 工具自排**             | `execute_code` 不在 `ptc_allowed_tools` 中（防递归）                                             | `ptc.py`                              |
| **main agent 不可用**        | PTC tool 不在 `_MAIN_TOOLS_BUILDERS` 中，仅 `_build_child_agent` 注入                            | `spawn/core.py`                       |
| **子 agent 递归**            | leaf subagent 无 `sessions_spawn`（已有 deny），PTC 白名单也不含 spawn/yield/kill/steer          | `inherited_tool_policy.py` + `ptc.py` |
| **subagent 子进程内 spawn**  | PTC 白名单不含 `sessions_spawn`/`sessions_yield`/`sessions_kill`/`sessions_steer`                | `ptc.py`                              |
| **subagent 子进程内 memory** | PTC 白名单不含 `memory`/`skill_manage`/`question`                                                | `ptc.py`                              |

---

## 关键参考文件

### Sherry 内部

| 文件                                                  | 用途                                                     |
| ----------------------------------------------------- | -------------------------------------------------------- |
| `agent/tools/__init__.py`                             | `_MAIN_TOOLS_BUILDERS` 列表 — PTC 不加入此处             |
| `agent/tools/subagent/spawn/core.py:818`              | `_build_child_agent` — PTC 注入点                        |
| `agent/tools/subagent/spawn/inherited_tool_policy.py` | `apply_tool_policy` — 工具过滤逻辑                       |
| `agent/tools/subagent/spawn/system_prompt.py`         | subagent 系统提示 — PTC 指导注入点                       |
| `agent/tools/python_repl.py`                          | 现有 python_repl — 参考 restricted builtins + subprocess |
| `agent/tools/pub_base/env_scrub.py`                   | `scrub_env()` — 环境变量清洗复用                         |
| `config/features/agent_side/tools_timeouts.py`        | 工具超时配置 — 增加 PTC 超时                             |
| `agent/core.py:175`                                   | `built_agent` — main agent 工具来源（无 PTC）            |

### 外部参考

| 项目                | 文件                                              | 参考内容                                         |
| ------------------- | ------------------------------------------------- | ------------------------------------------------ |
| `hermes-agent-main` | `tools/code_execution_tool.py`                    | 完整的子进程 + RPC 实现（1910 行），参考架构模式 |
| `hermes-agent-main` | `agent/conversation_loop.py:4818`                 | PTC 迭代退还（subagent 无此需求，但可参考）      |
| `hermes-agent-main` | `hermes_cli/config.py:2800`                       | `code_execution` 配置项结构                      |
| `deepagents-main`   | `libs/partners/quickjs/langchain_quickjs/_ptc.py` | PTC 工具过滤、camelCase 转换、prompt 渲染        |
| `deepagents-main`   | `libs/partners/quickjs/README.md`                 | PTC 完整文档、配置参考、错误类型                 |
