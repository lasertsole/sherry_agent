# 计划：汲取各家 agent 工具优点——terminal / read_file / search_files / write_file 改进

- 日期：2026-11-17
- 状态：**待评审**
- 定位：全项目工具对比（Sherry / DSH / Codex / OpenClaw / ZCode / Hermes / Madorin / OpenCode / Claude Code）发现的其他工具改进点，按 ROI 排序。
- 风险等级：**低-中** — 多数是增量改进，不改现有行为
- 依赖：无

---

## 0. Sherry 当前排名

| 维度 | Sherry 排名 | 缺口 |
|------|-----------|------|
| Terminal 沙箱 | 第 2（Codex 第 1） | Windows 无沙箱（H3 风险）；缺 landlock |
| Terminal 持久 shell | 无 | Madorin/DSH 有持久交互式 shell |
| Terminal 多后端 | 无 | Hermes 有 docker/modal/ssh/singularity/daytona |
| Terminal PTY | 无 | Codex 有 PTY（tty:true） |
| Read token/堆安全 | **第 1** | 无缺口 |
| Read 选择语法 | 第 2 | Claude Code 的 GJC `:50-200` 更灵活 |
| Write 原子性/CAS | **第 1** | 无缺口 |
| Write 多文件 patch | **缺** | patch_file patch 模式补齐（独立计划） |
| Write auto-format | 第 2 | 只有 .py；OpenCode 全语言 |
| Edit 模糊匹配 | **第 1** | 无缺口 |
| Search 后端韧性 | **第 1** | 无缺口 |
| Search 输出模式 | 第 2 | ZCode 有 count/multiline/type |

---

## 1. terminal 工具改进

### 1.0 超时行为改进（来自 ZCode）——优先级：高

**现状**：
- 默认超时 30 秒（`tools_timeouts.py:99`）——`npm test`/`pytest`/`cargo build` 经常 >30 秒
- 超时 → **杀进程树 + 丢弃全部输出**（`core.py:583-586`）——跑了 28 秒的测试输出全丢
- 不自动转后台——agent 必须预判命令会超时并提前设 `run_in_background=True`
- 不支持 env 覆盖默认超时

**ZCode 怎么做**：
- 默认 120 秒
- 超时 → **自动转后台**（`auto_on_timeout`）——进程继续跑，返回后台任务 ID + 已收集的部分输出
- `sleep` 等无意义命令不转后台（直接杀）
- 支持 `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` env 覆盖

**改进 4 项**：

#### a. 默认超时 30→120 秒

**文件**：`config/features/agent_side/tools_timeouts.py:99`

```python
# 改前
"terminal_timeout_seconds": 30,
# 改后
"terminal_timeout_seconds": 120,
```

**成本**：1 行。让大部分测试/构建命令在前台完成。

#### b. 超时保留部分输出

**文件**：`agent/tools/terminal/core.py`

当前超时后 `discard_stdout_tmp`（丢弃临时文件）。改为读取已写入的部分输出：

```python
# core.py:573-586 (sync) / 946-958 (async)
# 改前：超时 → kill → discard_stdout_tmp → 返回固定字符串
# 改后：超时 → kill → 读取已写入的部分输出 → 返回部分输出 + 超时提示

# sync 路径
except subprocess.TimeoutExpired:
    kill_process_tree(proc)
    # 读取已写入的部分输出（不丢弃）
    partial = _read_partial_stdout(tmp_path)
    return ToolResult(
        content=f"[Terminal command timed out after {timeout}s. "
                f"Process killed. Partial output ({len(partial)} chars):\n\n{partial}",
        status="error"
    )
```

```python
def _read_partial_stdout(tmp_path: Path) -> str:
    """读取超时前已写入的 stdout（head+tail 截断）。"""
    try:
        data = tmp_path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        if len(text) > 5000:
            return text[:2500] + "\n... [truncated] ...\n" + text[-2500:]
        return text
    except Exception:
        return ""
```

**成本**：~50 行。

#### c. 超时自动转后台（auto_on_timeout）

**文件**：`agent/tools/terminal/core.py`

超时时不杀进程，而是转后台（复用现有 `_run_background` 的 `bg_registry` 机制）：

```python
# core.py async 路径（_arun）
try:
    await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
except asyncio.TimeoutError:
    # 检查是否应该自动转后台
    if self._should_auto_background(command):
        # 不杀进程，转后台
        task = await bg_registry.adopt_running_process(
            session_id=session_id,
            proc=proc,
            command=command,
            stdout_tmp=tmp_path,
        )
        partial = _read_partial_stdout(tmp_path)
        return ToolResult(
            content=f"[Command timed out after {effective_timeout}s. "
                    f"Automatically moved to background (task: {task.id}). "
                    f"Use terminal_bg(action='output', task_id='{task.id}') to check progress. "
                    f"Partial output so far:\n\n{partial}"
        )
    else:
        # 不符合转后台条件 → 杀 + 返回部分输出
        await akill_process_tree(proc)
        partial = _read_partial_stdout(tmp_path)
        return ToolResult(
            content=f"[Terminal command timed out after {effective_timeout}s. "
                    f"Process killed. Partial output:\n\n{partial}",
            status="error"
        )
```

**`_should_auto_background` 判定**（参考 ZCode 的 `isBashAutoBackgroundEligible`）：

```python
def _should_auto_background(command: str) -> bool:
    """超时命令是否应该自动转后台。

    排除：
    - 空命令
    - sleep（无意义等待）
    - 已显式设 run_in_background=True 的（已在后台）
    """
    cmd = command.strip().lower()
    if not cmd:
        return False
    if cmd.startswith("sleep "):
        return False
    return True
```

**`bg_registry.adopt_running_process`**（新增方法）——把一个已经在运行的进程"领养"为后台任务：

```python
# agent/tools/terminal/bg_registry.py
async def adopt_running_process(self, session_id, proc, command, stdout_tmp) -> BackgroundTask:
    """把超时的前台进程转为后台任务（不杀不重启）。"""
    task = BackgroundTask(
        id=f"bg-{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        command=command,
        proc=proc,
        stdout_path=stdout_tmp,  # 复用已写入的临时文件
        started_at=time.time(),
    )
    self._tasks[session_id].append(task)
    # 启动 watcher（同 _run_background 的 watcher）
    asyncio.create_task(self._watch(task))
    return task
```

**成本**：~120 行（_should_auto_background + adopt_running_process + 超时路径改写）。

#### d. env 覆盖默认超时

**文件**：`config/features/agent_side/tools_timeouts.py`

```python
# 改前
"terminal_timeout_seconds": 30,
"terminal_max_timeout_seconds": 600,

# 改后（加 env 读取）
"terminal_timeout_seconds": _int_env(source, "TERMINAL_DEFAULT_TIMEOUT_SECONDS", 120),
"terminal_max_timeout_seconds": _int_env(source, "TERMINAL_MAX_TIMEOUT_SECONDS", 600),
```

**成本**：2 行。

### 1.0 总成本

| 改动 | 行数 |
|------|------|
| 默认超时 30→120 | 1 |
| env 覆盖 | 2 |
| 超时保留部分输出 | ~50 |
| 超时自动转后台 | ~120 |
| 测试 | ~80 |
| **总计** | **~253** |

### 1.0 验收

| # | 场景 | 预期 |
|---|------|------|
| 1 | 命令 <120 秒完成 | 正常返回（默认超时提高到 120） |
| 2 | `npm test` 跑 90 秒 | 在前台完成（之前 30 秒就被杀） |
| 3 | `npm test` 跑 130 秒（超时） | 自动转后台 + 返回部分输出 + 任务 ID |
| 4 | `sleep 999`（超时） | 杀进程（不转后台）+ 返回部分输出 |
| 5 | 超时后的部分输出 | 保留已写入的 stdout（head+tail 截断） |
| 6 | `TERMINAL_DEFAULT_TIMEOUT_SECONDS=60` env | 默认超时变 60 秒 |
| 7 | 显式 `run_in_background=True` | 不受影响（现有后台行为不变） |
| 8 | 后台任务完成 | watcher 通知 + auto-turn（现有机制不变） |

---

**现状**：Sherry Linux 沙箱只有 bwrap。某些环境（无 bwrap 的精简容器）无沙箱。

**Codex 怎么做**：bwrap + landlock 双重——landlock 是内核级 LSM，无需外部二进制。

**改进**：
```python
# agent/tools/pub_base/sandbox_landlock.py（新建）
class LandlockSandboxBackend(SandboxBackend):
    """Linux landlock 沙箱——内核级，无需 bwrap。"""
    def wrap_command(self, command, allowed_paths):
        # 用 landlock Python 绑定限制文件系统访问
        ...
```

**成本**：~200 行。作为 bwrap 的 fallback（bwrap 不可用时用 landlock）。

### 1.3 持久交互式 shell（来自 Madorin/DSH）——优先级：中

**现状**：Sherry 的 terminal 每次 `create_subprocess_shell`——无持久 cwd/env。agent 每次 `cd` 后下一个命令又回到默认 cwd（DockerBridge 有 cwd 恢复机制，但本地 terminal 没有）。

**Madorin/DSH 怎么做**：
- Madorin：`sys_start_local_session` → `sys_send_command` → `sys_close_session`（显式 session 生命周期）
- DSH：`tool-bash-persistent`（持久 shell 变体）

**改进**：给 terminal 加 `session` 参数：

```python
# terminal 调用时
terminal(commands="cd /app/src", session="work1")  # 开启持久 session
terminal(commands="ls", session="work1")            # 同一 session，cwd 保留
terminal(commands="pytest", session="work1")        # 同一 session
terminal(action="close_session", session="work1")   # 关闭
```

**实现**：用 PTY 或 `subprocess.Popen` 保持 shell 进程常驻，通过 stdin 发命令 + stdout 读结果。

**成本**：~400 行 + session 管理。**复杂度高**——需处理超时/死锁/输出缓冲。

**替代方案**（更轻量）：现有 DockerBridge 的 cwd/env 恢复机制（`/tmp/.sherry-eval/env` + `/cwd` 文件）已解决容器场景。本地 terminal 可复制同样的机制——每次命令前恢复 cwd/env，不需要持久 shell。

### 1.4 PTY 支持（来自 Codex）——优先级：低

**现状**：Sherry 用 `asyncio.create_subprocess_shell`（pipe 模式）。某些交互式程序（vim / less / top）需要 PTY。

**Codex 怎么做**：`tty:true` 参数 → 用 PTY 而非 pipe。

**改进**：给 terminal 加 `tty: bool` 参数，`tty=True` 时用 `pty.openpty()`。

**成本**：~150 行。但 agent 很少需要交互式程序——优先级低。

### 1.5 多后端（来自 Hermes）——优先级：低

**现状**：Sherry 只支持本地 terminal（eval 用 DockerBridge 桥接到容器）。

**Hermes 怎么做**：local / docker / modal / ssh / singularity / daytona 多后端。

**改进**：terminal 的 `backend` 参数支持 `local` / `docker` / `ssh`。

**成本**：高（每后端 ~500 行）。Sherry 的 Fleet cell + DockerBridge 已覆盖容器场景，SSH 后端可通过 MCP server 实现。优先级低。

---

## 2. read_file 工具改进

### 2.1 GJC 行选择语法（来自 Claude Code）——优先级：中

**现状**：Sherry 的 read_file 用 `offset` + `limit`（数字）。

**Claude Code 怎么做**：GJC 选择器——`file_path:50-200`（读 50-200 行）、`:50-200,960-973`（多段）、`:conflicts`（读 git conflict 标记）、`:raw`（无行号）。

**改进**：给 read_file 的 `file_path` 参数支持 GJC 后缀语法：

```python
read_file(file_path="src/app.py:50-200")        # 读 50-200 行
read_file(file_path="src/app.py:50-200,300-350") # 多段
read_file(file_path="src/app.py:conflicts")       # 读 git conflict 行
```

**Windows 盘符冲突处理（关键）**：`C:\file.py:50-200` 中有两个冒号——盘符冒号 `C:` 和行号冒号 `:50-200`。解析规则：

```python
def _parse_gjc_selector(file_path: str) -> tuple[str, str | None]:
    """解析 file_path 中的 GJC 行选择后缀。
    只把最后一个冒号后的部分当选择器，且必须匹配行号/conflicts/raw 格式。
    Windows 盘符冒号（C:）不受影响——因为 D:\file.py 的 D: 后面跟 \ 不是数字。
    """
    # 从右往左找冒号
    idx = file_path.rfind(":")
    if idx == -1:
        return file_path, None  # 无冒号 → 无选择器

    selector = file_path[idx+1:]
    path_part = file_path[:idx]

    # 选择器必须匹配：数字范围 / conflicts / raw
    if selector in ("conflicts", "raw"):
        return path_part, selector
    if re.match(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$", selector):
        return path_part, selector

    # 不匹配 → 整个字符串是路径（Windows 盘符 C:\file.py 的 : 后面是 \ 不是数字）
    return file_path, None
```

**验证**：
- `src/app.py:50-200` → path=`src/app.py`, selector=`50-200` ✅
- `C:\file.py:50-200` → rfind(":") 找到 `:50-200` 的冒号 → selector=`50-200` ✅（盘符 `C:` 的冒号不是最后一个）
- `C:\file.py` → rfind(":") 找到 `C:` 的冒号 → selector=`\file.py` → 不匹配数字格式 → 返回整个为路径 ✅
- `src/app.py` → 无冒号 → 无选择器 ✅

**成本**：~120 行（含 Windows 盘符特殊处理）。

### 2.2 全语言 auto-format（来自 OpenCode）——优先级：低

**现状**：write_file 只对 `.py` 跑 autopep8（`write_file.py:78-86` 的 `_format_py_code`，在 `write_file.py:332,363` 调用）。

**OpenCode 怎么做**：`Format.Service`——写后自动跑 LSP format（全语言：TS/JS/Rust/Go/C++ 等）。

**Sherry 已有 lsp_format 工具**：`agent/tools/code_intel/lsp/tools.py:661` 的 `LspFormatTool`（name=`"lsp_format"`）——调 `textDocument/formatting` 或 `rangeFormatting`。但 write_file 不调它。

**改进**：write_file 写后对非 .py 文件调 `lsp_format`：

```python
# write_file 写后
if path.suffix == ".py":
    content = _format_py_code(content)  # 现有 autopep8
elif path.suffix in LSP_FORMATABLE_EXTENSIONS:  # .ts/.js/.rs/.go/.cpp 等
    await _lsp_format_file(session_id, path)  # 复用现有 LspFormatTool 逻辑
```

**成本**：~50 行（复用现有 `lsp_format` 工具的 `_run` 逻辑，不需新建）。需要 host 装 LSP server。

---

## 3. search_files 工具改进

### 3.1 count 和 multiline 输出模式（来自 ZCode）——优先级：中

**现状**：Sherry 的 search_files 有 `content`（行号+内容）和 `files`（文件路径）两种模式。

**ZCode 怎么做**：`content` / `files_with_matches` / `count`（匹配数）/ `multiline`（跨行匹配）+ `type`（按语言过滤 js/py/rust）。

**改进**：给 search_files 加 `output_mode` 参数：

```python
search_files(query="TODO", output_mode="count")      # 返回每文件匹配数
search_files(query="try.*catch", output_mode="content", multiline=True)  # 跨行
search_files(query="function", file_glob="*.ts", type="typescript")     # 语言过滤
```

**成本**：~100 行（ripgrep 原生支持 `--count` / `--multiline` / `--type`）。

### 3.2 rg --json 结构化输出（来自 DSH）——已实现，删除

**代码验证**：`agent/tools/pub_base/rg_backend.py:104` **已经使用 `--json`**。`_base_args` 默认 `json_output=True`，content 搜索用 `rg --json` + `_iter_json_lines` 解析帧，文件名搜索用 `rg --files`。此改进项**不需要做**——从优先级表和文件清单中删除。

---

## 4. patch_file 工具改进

### 4.1 多文件 patch 模式——独立计划

已在 `计划_patch_file扩展多文件patch模式_20261117.md` 中详细规划。不在此重复。

---

## 5. 优先级排序

| # | 改进 | 来源 | 优先级 | 成本 | ROI |
|---|------|------|--------|------|-----|
| 1 | **terminal 超时行为改进**（默认 120s + 保留输出 + 自动转后台 + env 覆盖） | ZCode | **高** | ~253 行 | 测试命令不再被杀+丢输出 |
| 2 | **GJC 行选择语法**（含 Windows 盘符处理） | Claude Code | **中** | ~120 行 | LLM 更容易精确读文件段 |
| 3 | **count/multiline 搜索模式** | ZCode | **中** | ~100 行 | 搜索更灵活 |
| 4 | **landlock 沙箱** | Codex | 低 | ~200 行 | bwrap 不可用时 fallback（多租户用容器已覆盖） |
| 5 | **全语言 auto-format**（复用已有 lsp_format） | OpenCode | 低 | ~50 行 | 复用现有 LspFormatTool |
| 6 | **持久交互式 shell**（或 cwd 恢复） | Madorin/DSH | 低 | ~200-400 行 | cwd/env 保留 |
| 7 | 多文件 patch 模式 | Codex | 低 | ~900 行 | 独立计划（ROI 低，待验证） |
| 8 | PTY 支持 | Codex | 低 | ~150 行 | 交互式程序少见 |
| ~~9~~ | ~~rg --json~~ | ~~DSH~~ | ~~已实现~~ | ~~0~~ | ~~删除——rg_backend.py:104 已用 --json~~ |
| 10 | 多后端 terminal | Hermes | 低 | ~500 行/后端 | Fleet+DockerBridge 已覆盖 |
| ~~11~~ | ~~Windows 沙箱~~ | ~~Codex~~ | ~~降级~~ | ~~300 行~~ | ~~多租户用容器隔离已覆盖；桌面单用户风险低~~ |

---

## 6. 不改的（Sherry 已是第 1）

| 维度 | 为什么不改 |
|------|-----------|
| Read token/堆安全 | Sherry 双重预算 + 流式分块 + 逐行上限已是所有项目最强 |
| Write 原子性/CAS | atomic_write_bytes_no_follow + flock + fstat CAS 已是最严格 |
| Edit 模糊匹配 | 9 级匹配链 + "Did you mean?" 已是最强 |
| Terminal 输出安全 | fd 直写 + eviction + head/tail 已是最防御性 |
| Search 后端韧性 | ripgrep + pure-Python fallback 双引擎已是唯一 |

---

## 7. 文件变更清单（按优先级）

### 高优先级

| 文件 | 操作 | 行数 |
|------|------|------|
| `config/features/agent_side/tools_timeouts.py` | 改：默认超时 30→120 + env 覆盖 | ~3 |
| `agent/tools/terminal/core.py` | 改：超时保留部分输出 + 自动转后台 | ~170 |
| `agent/tools/terminal/bg_registry.py` | 改：加 `adopt_running_process` | ~50 |
| `tests/agent/tools/terminal/test_timeout_behavior.py` | 新建：超时行为测试 | ~80 |

### 中优先级

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/tools/file_tools/read_file.py` | 改：GJC 行选择语法（含 Windows 盘符处理） | ~120 |
| `agent/tools/file_tools/search_files.py` | 改：count/multiline/type 模式 | ~100 |
| `agent/tools/pub_base/sandbox_landlock.py` | 新建：landlock 沙箱 | ~200 |
| `agent/tools/file_tools/write_file.py` | 改：写后 LSP format（非 .py 文件） | ~50 |

### 低优先级

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/tools/terminal/core.py` | 改：持久 session / cwd 恢复 | ~200 |
| `agent/tools/terminal/core.py` | 改：PTY 支持 | ~150 |

| **总计** | | ~1123 |

---

## 8. Kill criteria

- Windows 沙箱：如果 pywin32/ctypes 调 CreateRestrictedToken 在目标 Windows 版本上不稳定，退化为现有的正则黑名单（H3 接受）
- landlock：如果 Python landlock 绑定不可用，保持 bwrap-only（当前行为）
- GJC 语法：如果 LLM 不使用 `:50-200` 语法而继续用 offset+limit，删除 GJC 解析（省 prompt token）
- 持久 shell：如果 PTY/subprocess 持久 shell 在长时间运行后死锁/泄漏，退化为 cwd 恢复机制（DockerBridge 已有）
- auto-format：如果 LSP format 在写后调用导致延迟过高（LSP server 冷启动），改为异步格式化（不阻塞 write_file 返回）
