# 计划：terminal 后台运行 + Git Bash 检测 + 进程跟踪（学 ZCode）

- 建档日期：2026-10-13
- 状态：**计划（未开工）**
- 触发问题：Sherry 的 terminal 工具在 Windows 上用 `cmd.exe`（`shell=True`），不支持 `&`
  后台、`&&` 链接等 POSIX 语法；也没有后台运行能力——命令必须等 `proc.wait()` 返回
  才能继续。ZCode 有完整的 `run_in_background` 机制：Git Bash 检测、进程注册表、
  输出文件保留、跨 turn 查询、退出通知 + auto-turn。本计划把其中三项核心能力移植到 Sherry。
- 结论先行：
  1. **Git Bash 检测**是独立的基础设施改动——检测到 `bash.exe` 后用 `bash -c` 替代
     `cmd.exe /c`，让 POSIX 语法在 Windows 可用。不依赖后台运行功能。
  2. **进程跟踪 + 输出保留**是后台运行的核心——spawn 后不等 `wait()`，注册到进程级
     in-memory 注册表，输出写保留文件，asyncio watcher 在进程退出时推 WS + 触发 auto-turn。
  3. **Sherry 已有全部基础设施可复用**——`stdout_fd.py`（fd-direct-write）、`process_reap.py`
     （有界 kill）、`runtime/hooks.py`（auto-turn 触发）、`relation_register`（WS push）、
     `subagent/announce/core.py`（strong-ref asyncio task 模式）、`BaseSQLiteRepository`（可选持久化）。
  4. 和 ZCode 的关键差异：不做 OS 级 `unref()` 脱离（进程生命周期绑定到 server 进程，
     和 ZCode 一致）；不做前台实时进度推送（初始版本只做退出通知）。

---

## 1. ZCode 的实现（参照基准）

### 1.1 Windows Git Bash 检测

ZCode 在 `bash-shell-provider.ts` 中实现了完整的 Windows shell 解析链：

1. **固定路径检测**——`C:\Program Files\Git\bin\bash.exe`、`C:\Program Files (x86)\Git\bin\bash.exe`
2. **从 git.exe 推断**——在 PATH 中找 `git.exe`，然后 `dirname(git.exe)/../bin/bash.exe` 和
   `dirname(git.exe)/../../bin/bash.exe`
3. **验证可执行**——`accessSync(path, X_OK)`
4. **spawn 方式**——`shell: false`，`file = bash.exe`，`args = ["-c", "-l", command]`
   （`-l` 加载登录 shell profile，确保 PATH 完整；首次后缓存 shell-init 快照，后续去掉 `-l`）
5. **fallback**——找不到 Git Bash 时退到系统 shell（legacy shell selection）

ZCode 的 shell 解析在进程启动时做一次，结果缓存。

### 1.2 进程注册表（PID + 输出文件 + 跨 turn 查询）

ZCode 有两层注册表：

| 层 | 位置 | 存储 | 用途 |
|---|---|---|---|
| adapter 层 | `node-execution-adapter-base.ts` | `Map<string, BackgroundTaskRecord>` | 持有 PID、completion Promise、AbortController、输出路径 |
| core 层 | `runtime-task/registry.ts` | `Map<string, RuntimeTaskSnapshot>` | 跨 turn 查询，模型可见 |

- **注册**——`register(task)` 写入 Map，key = `taskId`（`exec_<uuid>` 格式）
- **查询**——`get(taskId)` 返回 snapshot；`list` 按 session 过滤
- **退出检测**——两种并行机制：
  - **1s 轮询**——`setInterval(poll, 1000)`，调 `getBackgroundTask(taskId)`，状态非 running 时发 terminal 通知
  - **直接等待**——`await record.completion`（adapter 侧的 Promise，在 `finalizeBackgroundTaskRecord` 时 resolve）
- **通知**——terminal snapshot 时：更新 core 注册表 → `emitTerminalSnapshot` →
  `SessionEventType.BackgroundTaskCompleted` 事件 → `maybeEnqueueBackgroundTaskNotification`
  （单次投递，`claimRuntimeBackgroundTaskNotification` 取 token）

### 1.3 输出文件保留

ZCode 的 `BashFileOutput`（`bash-file-output.ts`）：

- **文件位置**——`<ZCODE_STORAGE_DIR or ~/.zcode>/cli/exec/<sessionId>/<toolCallId>-stdout.log`
- **打开方式**——POSIX: `O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW`, mode `0o600`；
  Windows: `"w"`（MSYS 把 append-only 句柄判为只读）
- **stdio 指向**——`spawnOptions.stdio = [stdin, file.fd, file.fd]`，child 直接写文件 fd，
  Node 不经手字节
- **大文件 watchdog**——5s 轮询 `stat(path).size`，超 `BASH_RUNTIME_OUTPUT_LIMIT_BYTES`（5GiB）
  时 kill 进程
- **读取**——`readBashOutput(path, maxBytes=8192, tail=true)`：stat 取 size →
  seek 到 `size - min(size, maxBytes)` → 读最后 maxBytes → decode
- **文件清理**——`discard()` 只在 `this.created`（自己创建的）时 `rm`；前台结束时也保留
  供后台 handoff

### 1.4 工具返回值

`run_in_background=true` 时立即返回：
```ts
{
  stdout: "",
  stderr: "",
  status: "backgrounded",
  backgroundTaskId: "exec_<uuid>",
  outputPath: "<file path>",
  // 模型可用 read_file 读 outputPath，或用 background-bash-output runtime method 做有界 tail read
}
```

### 1.5 关键常量

| 常量 | 值 | 来源 |
|---|---|---|
| `BACKGROUND_BASH_OUTPUT_MAX_BYTES` | 8192 (8KiB) | `shared/background-bash-output.ts:3` |
| `BASH_RUNTIME_OUTPUT_LIMIT_BYTES` | 5GiB | `adapters/execution-utils.ts:11` |
| `OUTPUT_WATCH_INTERVAL_MS` | 5000 (5s) | `bash-file-output.ts:9` |
| 轮询间隔 | 1000ms (1s) | `core/background-tasks.ts:358` |
| `OUTPUT_FILE_MODE` | 0o600 | `bash-file-output.ts:10` |

---

## 2. Sherry 现状与可复用基础设施

### 2.1 Windows shell 现状

- terminal 工具的 `_arun` 在无 sandbox backend（Windows 总是如此）时走
  `asyncio.create_subprocess_shell(cmd_str, ...)` → Windows 上是 `cmd.exe /c cmd_str`
- `_run`（sync）在同样条件下走 `subprocess.Popen(cmd_str, shell=True, ...)` → 也是 `cmd.exe`
- `cmd.exe` 不支持 `&`（后台）、`&&`（条件链接在 cmd 里是无条件）、`$()`（命令替换）、
  `2>&1`（重定向语法不同）
- **没有任何 Git Bash 检测**——全仓库 grep `bash.exe`/`git bash`/`MSYS` 零结果

### 2.2 已有的可复用基础设施

| 基础设施 | 文件 | 复用方式 |
|---|---|---|
| fd-direct-write | `agent/tools/pub_base/stdout_fd.py` | `open_stdout_tmp` 创建 temp 文件；新增 `open_retained_output` 创建保留文件 |
| 有界 kill | `agent/tools/pub_base/process_reap.py` | `reap_process`/`areap_process` 杀进程时有界升级 |
| auto-turn 触发 | `runtime/hooks.py` | `MAYBE_TRIGGER_AUTO_TURN` hook，注入 `HumanMessage` |
| WS push | `agent/tools/taskflow/progress_push.py` | `relation_register.get_websocket_by_session_id` + `await send_text` |
| strong-ref asyncio task | `agent/tools/subagent/announce/core.py` | `_background_tasks: set[asyncio.Task]` + `add_done_callback` 防 GC |
| session 安全路径 | `config/path.py` | `is_safe_session_segment` + `SESSIONS_DIR` |
| session 清理 | `server/service/session_cleanup_service.py` | `purge_session()` 已 `shutil.rmtree` 整个 session 目录 |
| TypedState | `runtime/session/state_keys.py` | `StateKey` + `TypedState.set/get` 存跨 turn 元数据 |
| 工具注册 | `agent/tools/__init__.py` | `_MAIN_TOOLS_BUILDERS` list + `build_*_tool()` |
| 工具目录 | `agent/tools/catalog.py` | `TOOL_GROUPS` 分组 |
| Config 体系 | `config/features/agent_side/` | TypedDict + 实例 |

---

## 3. 设计方案

### 3.1 Git Bash 检测

**新建文件：** `agent/tools/pub_base/shell_resolver.py`

```python
"""Windows Git Bash 检测——让 POSIX 语法在 Windows 上可用。"""

import os
import platform
import shutil
from pathlib import Path

# 固定路径（覆盖 64 位 / 32 位安装）
_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

_cached_bash: str | None = None
_cached = False  # 是否已检测过

def resolve_windows_bash() -> str | None:
    """返回 Windows 上 bash.exe 的路径，找不到返回 None。

    非 Windows 直接返回 None（POSIX 已有 /bin/sh）。
    结果在进程生命周期内缓存。
    """
    global _cached_bash, _cached
    if _cached:
        return _cached_bash
    _cached = True
    if platform.system() != "Windows":
        return None
    # 1. 固定路径
    for candidate in _GIT_BASH_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _cached_bash = candidate
            return candidate
    # 2. 从 git.exe 推断
    git_exe = shutil.which("git")
    if git_exe:
        git_dir = str(Path(git_exe).resolve().parent)
        for relative in ("../bin/bash.exe", "../../bin/bash.exe"):
            inferred = str((Path(git_dir) / relative).resolve())
            if os.path.isfile(inferred) and os.access(inferred, os.X_OK):
                _cached_bash = inferred
                return inferred
    return None
```

**terminal.py 改动点：**

`_arun` 的 `create_subprocess_shell` fallback 路径（:491）：
```python
# 当前：
proc = await asyncio.create_subprocess_shell(
    cmd_str, stdout=tmp_handle, stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env,
)
# 改为：
bash_exe = resolve_windows_bash()
if bash_exe:
    proc = await asyncio.create_subprocess_exec(
        bash_exe, "-c", cmd_str,
        stdout=tmp_handle, stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env,
    )
else:
    proc = await asyncio.create_subprocess_shell(
        cmd_str, stdout=tmp_handle, stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env,
    )
```

`_run`（sync）的 `shell=True` fallback 路径同理。

**效果：** Windows 上 `sleep 9999 &`、`cmd1 && cmd2`、`$(...)`、`2>&1` 全部可用。

### 3.2 输出保留 + 有界 tail read

**扩展文件：** `agent/tools/pub_base/stdout_fd.py`

新增两个函数（不改现有函数）：

```python
_BG_OUTPUT_DIR_NAME = "background"

def open_retained_output(session_id: str, task_id: str) -> Path | None:
    """在 SESSIONS_DIR/<session_id>/background/ 下创建保留的输出文件。

    文件名为 <task_id>.log，不会被 sweep_stale_temp_files 清理
    （不同前缀 .sherry-bg- vs .sherry-tmp-stdout-）。
    返回 None 表示 session_id 不安全。
    """
    from config import SESSIONS_DIR
    from config.path import is_safe_session_segment
    if not session_id or not is_safe_session_segment(session_id):
        return None
    directory = Path(SESSIONS_DIR) / session_id / _BG_OUTPUT_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.log"
    path.touch()
    return path

def read_retained_output(path: Path, *, max_bytes: int = 8192) -> str:
    """有界 tail read：读文件最后 max_bytes 字节。

    对标 ZCode 的 readBashOutput(path, maxBytes, tail=true)。
    返回格式："...[truncated, {total} bytes total]\n{tail_text}" 或纯文本。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    length = min(size, max_bytes)
    offset = size - length  # tail: 从末尾往前读
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        return f"...[truncated, {size} bytes total]\n{text}"
    return text
```

**设计要点：**
- 保留文件用 `.log` 后缀但文件名是 `<task_id>.log`（不以 `.sherry-tmp-` 开头），不会被 `sweep_stale_temp_files` 清理
- `open_retained_output` 只创建空文件（`touch`），child 的 stdout 指向它
- `read_retained_output` 做有界 tail read，对标 ZCode 的 8KiB 上限
- session 清理时 `shutil.rmtree(SESSIONS_DIR/<session_id>/)` 自动删除 `background/` 子目录

### 3.3 后台进程注册表

**新建文件：** `agent/tools/terminal/background_registry.py`

```python
"""进程级后台任务注册表——in-memory 单例，跨 turn 可查。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from agent.tools.pub_base.stdout_fd import read_retained_output


@dataclass
class BackgroundTask:
    task_id: str           # "bg-" + uuid4().hex[:12]
    session_id: str
    pid: int | None
    command: str
    output_path: Path | None
    started_at: float      # time.monotonic()
    status: str = "running"  # "running" | "completed" | "failed"
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "pid": self.pid,
            "command": self.command,
            "output_path": str(self.output_path) if self.output_path else None,
            "started_at": self.started_at,
            "status": self.status,
            "exit_code": self.exit_code,
        }


class _BackgroundTaskRegistry:
    """进程级单例。后台进程的 PID + 输出文件 + 状态，跨 turn 可查。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._watchers: set[asyncio.Task[None]] = set()  # strong-ref 防 GC

    def register(self, task: BackgroundTask) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_session(self, session_id: str) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.session_id == session_id]

    def update(self, task_id: str, *, status: str, exit_code: int | None = None) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.status = status
            task.exit_code = exit_code

    def start_watcher(
        self, task_id: str, proc: asyncio.subprocess.Process, session_id: str
    ) -> None:
        """启动 asyncio watcher：await proc.wait() → 更新注册表 → WS push → auto-turn。"""
        task = asyncio.create_task(self._watch(task_id, proc, session_id))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch(
        self, task_id: str, proc: asyncio.subprocess.Process, session_id: str
    ) -> None:
        """等待进程退出，发通知。fail-open：任何异常只 log 不 raise。"""
        try:
            exit_code = await proc.wait()
        except Exception as e:  # noqa: BLE001
            logger.warning("background task {} watcher error: {}", task_id, e)
            exit_code = -1
        bg_task = self._tasks.get(task_id)
        if bg_task is None:
            return
        status = "completed" if exit_code == 0 else "failed"
        self.update(task_id, status=status, exit_code=exit_code)
        # WS push（best-effort）
        await self._push_exit_notification(bg_task)
        # auto-turn（best-effort）
        await self._trigger_auto_turn(bg_task)

    async def _push_exit_notification(self, task: BackgroundTask) -> None:
        """同 push_taskflow_progress 模式：relation_register + send_text。"""
        try:
            from runtime.session.relation_register import relation_register
            websocket = relation_register.get_websocket_by_session_id(task.session_id)
            if websocket is None:
                return
            payload = {
                "event": "terminal_bg_exit",
                "session_id": task.session_id,
                "content": {
                    "task_id": task.task_id,
                    "exit_code": task.exit_code,
                    "command": task.command,
                },
            }
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            logger.warning("background task WS push failed for {}: {}", task.task_id, e)

    async def _trigger_auto_turn(self, task: BackgroundTask) -> None:
        """同 PlanContinuationEnforcer 模式：resolve hook + 注入 HumanMessage。"""
        try:
            from runtime import hooks
            from langchain_core.messages import HumanMessage
            trigger = hooks.resolve(hooks.MAYBE_TRIGGER_AUTO_TURN)
            if trigger is None:
                return  # evals/tests 无 server
            session_key = f"agent:main:session:{task.session_id}"
            await trigger(
                session_key,
                HumanMessage(
                    content=(
                        f"Background task {task.task_id} (command: {task.command}) "
                        f"exited with code {task.exit_code}."
                    ),
                    metadata={"origin": "background_task", "internal": True},
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("background task auto-turn trigger failed for {}: {}", task.task_id, e)


# 进程级单例
background_registry = _BackgroundTaskRegistry()
```

**设计要点：**
- **进程级 in-memory**——不持久化到 SQLite。后台进程是子进程，server 重启后进程已死，
  持久化注册表没有意义（和 ZCode 的 adapter 层 Map 一致）
- **strong-ref pattern**——`_watchers: set[asyncio.Task]` + `add_done_callback(_watchers.discard)`
  防止 watcher 被 GC（复用 `subagent/announce/core.py` 的模式）
- **fail-open**——watcher 内每步都 try/except，不会因为通知失败导致 watcher 异常
- **WS push**——复用 `relation_register.get_websocket_by_session_id` + `await send_text`
  （同 `push_taskflow_progress` 模式）
- **auto-turn**——复用 `hooks.MAYBE_TRIGGER_AUTO_TURN`，注入 `HumanMessage`
  （同 `PlanContinuationEnforcer` 的 `_inject()` 模式，`agent/middlewares/todo_continuation/core.py:398-407`）

### 3.4 terminal 工具改动

**修改文件：** `agent/tools/terminal.py`

**Schema 新增字段：**
```python
class SafeShellInput(ShellInput):
    """ShellInput + the ``sandbox`` flag + ``run_in_background`` flag."""
    session_id: SessionId = ""
    sandbox: bool = Field(default=True, ...)
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run the command in the background. Returns a task_id immediately; "
            "the process keeps running with output captured to a file. "
            "Use terminal_bg_list to check status and terminal_bg_output to read output. "
            "The agent is notified when the process exits."
        ),
    )
```

**`_run` / `_arun` 签名新增 `run_in_background` 参数：**
```python
def _run(self, commands, run_manager=None, sandbox=True, session_id="",
         run_in_background=False, **kwargs) -> str:
    # sync 路径忽略 run_in_background（production 用 async 路径）
    ...

async def _arun(self, commands, run_manager=None, sandbox=True, session_id="",
                run_in_background=False, **kwargs) -> str:
    ...
    if run_in_background:
        return await self._run_background(
            cmd_str, bound_session, cwd, env, sandbox, writable_roots
        )
    # ... 现有前台路径
```

**`_run_background` 方法（新增）：**
```python
async def _run_background(
    self, cmd_str: str, session_id: str, cwd: str | None,
    env: dict[str, str], sandbox: bool, writable_roots: list[Path],
) -> str:
    """后台启动：spawn 后不等 wait()，注册到注册表，立即返回 task_id。"""
    import uuid
    from agent.tools.pub_base.shell_resolver import resolve_windows_bash
    from agent.tools.pub_base.stdout_fd import open_retained_output
    from agent.tools.terminal.background_registry import background_registry, BackgroundTask

    task_id = f"bg-{uuid.uuid4().hex[:12]}"
    output_path = open_retained_output(session_id, task_id)
    if output_path is None:
        return "Error: could not create output file for background task (invalid session)."

    # stdout → 保留文件（不是 temp 文件），不删除
    handle = open(output_path, "wb")
    try:
        # shell 解析：sandbox backend > Git Bash > shell=True fallback
        argv, spawn_env = self._resolve_sandbox_argv(cmd_str, env, writable_roots)
        if argv is not None:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=handle, stderr=asyncio.subprocess.STDOUT,
                env=spawn_env, cwd=cwd or str(ROOT_DIR),
            )
        else:
            bash_exe = resolve_windows_bash()
            if bash_exe:
                proc = await asyncio.create_subprocess_exec(
                    bash_exe, "-c", cmd_str,
                    stdout=handle, stderr=asyncio.subprocess.STDOUT,
                    env=env, cwd=cwd or str(ROOT_DIR),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str, stdout=handle, stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd or str(ROOT_DIR), env=env,
                )
    finally:
        handle.close()  # child 持有自己的 fd 副本

    # 注册 + 启动 watcher
    task = BackgroundTask(
        task_id=task_id, session_id=session_id, pid=proc.pid,
        command=cmd_str, output_path=output_path, started_at=time.monotonic(),
    )
    background_registry.register(task)
    background_registry.start_watcher(task_id, proc, session_id)

    return (
        f"Background task started.\n"
        f"Task ID: {task_id}\n"
        f"PID: {proc.pid}\n"
        f"Output: {output_path}\n"
        f"The agent will be notified when the process exits. "
        f"Use terminal_bg_list to check status, terminal_bg_output to read output."
    )
```

**设计要点：**
- **输出文件是保留文件**（`open_retained_output`），不是 temp 文件（`open_stdout_tmp`）
  ——不会被 `read_or_evict_output` 删除，不会被 `sweep_stale_temp_files` 清理
- **child 的 stdout 指向保留文件**——fd-direct-write 模式，child 直接写文件，
  parent 不经手字节（和 terminal 前台路径、ZCode 的 `BashFileOutput` 一致）
- **`handle.close()`** 在 spawn 后立即调用——child 持有自己的 fd 副本，
  parent 关闭自己的不影响 child 写入
- **不调 `proc.wait()`**——立即返回，watcher 异步等待退出
- **shell 解析优先级**：sandbox backend（Linux/macOS）> Git Bash（Windows）> shell=True fallback
  ——sandbox 路径不变，Git Bash 是 Windows unsandboxed 路径的改进

### 3.5 两个查询工具

**新建文件：** `agent/tools/terminal/bg_tools.py`

```python
"""后台终端任务查询工具：列状态 + 读输出。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from agent.tools.pub_base import SessionId
from agent.tools.terminal.background_registry import background_registry
from agent.tools.pub_base.stdout_fd import read_retained_output


@tool("terminal_bg_list")
def terminal_bg_list_tool(
    session_id: SessionId = "",  # type: ignore[valid-type]
) -> str:
    """List all background terminal tasks for the current session.

    Returns a JSON array of {task_id, command, status, pid, exit_code, started_at}.
    """
    tasks = background_registry.list_session(session_id)
    if not tasks:
        return "No background terminal tasks for this session."
    return json.dumps(
        [
            {
                "task_id": t.task_id,
                "command": t.command,
                "status": t.status,
                "pid": t.pid,
                "exit_code": t.exit_code,
            }
            for t in tasks
        ],
        ensure_ascii=False,
    )


@tool("terminal_bg_output")
def terminal_bg_output_tool(
    task_id: str,
    session_id: SessionId = "",  # type: ignore[valid-type]
) -> str:
    """Read the output (tail, bounded to 8KiB) of a background terminal task.

    Args:
        task_id: The background task ID returned by terminal(run_in_background=True).
    """
    task = background_registry.get(task_id)
    if task is None:
        return f"No background task found with task_id: {task_id}"
    if task.session_id != session_id:
        return f"No background task found with task_id: {task_id}"
    if task.output_path is None:
        return "Background task has no output file."
    return read_retained_output(task.output_path)


def build_bg_list_tool():
    return terminal_bg_list_tool

def build_bg_output_tool():
    return terminal_bg_output_tool
```

**注册到 `_MAIN_TOOLS_BUILDERS`（`agent/tools/__init__.py`）：**
```python
_MAIN_TOOLS_BUILDERS = [
    ...
    build_terminal_tool,
    build_bg_list_tool,       # 新增
    build_bg_output_tool,    # 新增
    ...
]
```

**加入 catalogue（`agent/tools/catalog.py`）：**
- `terminal_bg_list` 和 `terminal_bg_output` 加入 terminal 组
- 两者都是 `main_only` scope（子代理不需要后台任务）

### 3.6 Config

**新建文件：** `config/features/agent_side/terminal_bg.py`

```python
"""Terminal background task configuration."""

from __future__ import annotations
from typing import TypedDict


class TerminalBgConfig(TypedDict):
    max_background_tasks_per_session: int
    output_max_read_bytes: int       # tail read 上限
    output_limit_bytes: int         # 输出文件硬上限，超限 kill 进程
    auto_turn_on_exit: bool         # 进程退出时是否触发 auto-turn


TERMINAL_BG: TerminalBgConfig = {
    "max_background_tasks_per_session": 10,
    "output_max_read_bytes": 8192,        # 8KiB，对标 ZCode
    "output_limit_bytes": 50 * 1024 * 1024,  # 50MiB
    "auto_turn_on_exit": True,
}
```

在 `config/features/agent_side/__init__.py` 和 `config/features/__init__.py` 中 re-export。

### 3.7 Session 清理

**修改文件：** `server/service/session_cleanup_service.py`

在 `purge_session()` 中新增一步（在 `shutil.rmtree` 之前）：
```python
# Kill running background tasks for this session
from agent.tools.terminal.background_registry import background_registry
for task in background_registry.list_session(session_id):
    if task.status == "running" and task.pid is not None:
        try:
            import os, signal
            os.kill(task.pid, signal.SIGTERM)  # POSIX
        except (OSError, ProcessLookupError):
            pass  # already dead
        background_registry.update(task.task_id, status="killed")
```

`shutil.rmtree(SESSIONS_DIR/<session_id>/)` 会自动删除 `background/` 子目录和其中的输出文件。

### 3.8 Windows shell 解析整合（非后台场景也受益）

步骤 1 的 Git Bash 检测不仅用于后台运行——**前台命令也受益**：

```python
# _arun 的 unsandboxed fallback 路径（现有代码 :491）：
bash_exe = resolve_windows_bash()
if bash_exe:
    proc = await asyncio.create_subprocess_exec(
        bash_exe, "-c", cmd_str,
        stdout=tmp_handle, stderr=asyncio.subprocess.STDOUT,
        cwd=cwd or str(ROOT_DIR), env=env,
    )
else:
    proc = await asyncio.create_subprocess_shell(
        cmd_str, stdout=tmp_handle, stderr=asyncio.subprocess.STDOUT,
        cwd=cwd or str(ROOT_DIR), env=env,
    )
```

`_run`（sync）的 `shell=True` fallback 路径同理：
```python
bash_exe = resolve_windows_bash()
if bash_exe:
    proc = subprocess.Popen(
        [bash_exe, "-c", argv],  # argv 是 cmd_str
        stdout=tmp_handle, stderr=subprocess.STDOUT,
        cwd=cwd, env=env,
    )
else:
    proc = subprocess.Popen(
        argv, shell=True,  # nosec B602
        stdout=tmp_handle, stderr=subprocess.STDOUT,
        cwd=cwd, env=env,
    )
```

**效果：** 即使不用 `run_in_background`，Windows 上的前台命令也能用 POSIX 语法
（`&&`、`&`、`$()`、`2>&1`、heredoc 等）。

---

## 4. 文件清单

### 新建文件（5 个）

| 文件 | 用途 |
|---|---|
| `agent/tools/pub_base/shell_resolver.py` | Git Bash 检测 |
| `agent/tools/terminal/background_registry.py` | 进程级后台任务注册表 + watcher + WS push + auto-turn |
| `agent/tools/terminal/bg_tools.py` | `terminal_bg_list` + `terminal_bg_output` 工具 |
| `config/features/agent_side/terminal_bg.py` | Config TypedDict |
| `tests/agent/tools/terminal/test_background_registry.py` | 注册表 + watcher 测试 |

### 修改文件（4 个）

| 文件 | 改动 |
|---|---|
| `agent/tools/terminal.py` | `SafeShellInput` 加 `run_in_background` 字段；`_run`/`_arun` 加参数；新增 `_run_background` 方法；shell fallback 路径整合 Git Bash 检测 |
| `agent/tools/pub_base/stdout_fd.py` | 新增 `open_retained_output` + `read_retained_output` 函数 |
| `agent/tools/__init__.py` | `_MAIN_TOOLS_BUILDERS` 加 `build_bg_list_tool` + `build_bg_output_tool` |
| `agent/tools/catalog.py` | terminal 组加两个新工具 |
| `server/service/session_cleanup_service.py` | `purge_session` 新增 kill running background tasks |
| `config/features/agent_side/__init__.py` | re-export `TERMINAL_BG` |

### 测试文件（2 个）

| 文件 | 覆盖 |
|---|---|
| `tests/agent/tools/pub_base/test_shell_resolver.py` | Git Bash 检测（mock `os.access`/`shutil.which`） |
| `tests/agent/tools/terminal/test_background_registry.py` | 注册表 CRUD + watcher 生命周期 + WS push mock + auto-turn mock |

---

## 5. 和 ZCode 的差异

| | ZCode | Sherry（本方案） |
|---|---|---|
| **OS 级脱离** | 不 `unref()`，进程绑定到 CLI 会话 | 不做，进程绑定到 server 进程（一致） |
| **`detached`** | POSIX `true`，Windows `false` | 用 `start_new_session=True`（POSIX 新进程组）；Windows 不适用 |
| **输出写入** | child 直接写文件 fd（`BashFileOutput`） | child 直接写文件 fd（`open_retained_output`），同 fd-direct-write |
| **大文件 watchdog** | 5s 轮询 `stat().size`，超 5GiB kill | 初始版本不做轮询，用 `output_limit_bytes`（50MiB）做硬上限（超限时 watcher 退出时检查） |
| **前台进度推送** | `watchProgress` 实时 tail（2s 延迟，1s 间隔，4KiB tail） | 不做，初始版本只做退出通知 |
| **shell 检测缓存** | 进程启动时做一次，缓存 shell-init 快照 | 进程级缓存（`_cached` 标志），首次调 `resolve_windows_bash()` 后缓存 |
| **登录 shell** | `bash -c -l command`（加载 `.bash_profile`） | `bash -c command`（不加 `-l`，PATH 已由 `scrub_env` 保留） |
| **轮询 vs 直接等待** | 两种并行（1s 轮询 + `await completion` Promise） | 只用 `await proc.wait()`（asyncio 直接等待，无需轮询） |
| **跨重启持久化** | adapter 层 in-memory（重启丢失）；core 层也是 in-memory | in-memory（重启丢失，和 ZCode 一致） |
| **退出通知** | `SessionEventType.BackgroundTaskCompleted` 事件 + 模型通知队列 | WS push `terminal_bg_exit` + auto-turn `HumanMessage` 注入 |
| **查询工具** | `background-bash-output` runtime method（8KiB tail read） | `terminal_bg_output` 工具（8KiB tail read，对标） |
| **Windows shell** | 自动检测 Git Bash，fallback 到系统 shell | 自动检测 Git Bash，fallback 到 `cmd.exe`（`shell=True`） |

---

## 6. 不做的事

- **不做 OS 级进程脱离**——后台进程生命周期绑定到 server 进程（和 ZCode 一致）
- **不做前台实时进度推送**——初始版本只做退出通知，前台进度推送是后续 enhancement
- **不做大文件 watchdog 轮询**——初始版本用 `output_limit_bytes` 硬上限，不做 5s 轮询 stat
- **不改 sync `_run` 支持 background**——production 用 async 路径，sync 路径忽略 `run_in_background`
- **不做持久化注册表**——后台进程是子进程，server 重启后已死，持久化无意义
- **不做 `terminal_bg_kill` 工具**——初始版本不做取消后台任务的能力，session 清理时统一 kill
- **不做 `detached: true`**——用 `start_new_session=True`（POSIX），Windows 不适用
- **不做登录 shell `-l`**——`scrub_env()` 已保留 PATH，不需要 `.bash_profile`
