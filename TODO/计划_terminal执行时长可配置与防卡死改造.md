# 计划：terminal 执行时长可配置 + 兄弟工具防卡死改造

- 建档日期：2026-10-13
- 状态：**方案 B 已完成；方案 A 未开工**
- 触发问题：Sherry 的 `terminal` 工具超时写死 30 秒，模型无法按场景调整（编译
  /安装等长命令必然超时），也不像 ZCode 那样支持 per-call timeout；同时
  `python_repl`、`user_terminal_service`、`ptc` 三个兄弟工具仍用旧
  `PIPE + communicate(无timeout)` 模式，在子进程 fork 孙进程的场景下会永久挂起。
- 结论先行：
  1. **terminal 本身不会永久挂起**——已采用 fd-direct-write-to-file + 有界 reap
     （`process_reap.py`），最坏 ~40s 后放弃句柄返回超时消息。问题在于 30 秒太短
     且不可配置，不是卡死。
  2. **三个兄弟工具会卡死**——`python_repl`、`user_terminal_service`、`ptc/runner.py`
     超时后 `proc.kill()` 再调无界 `communicate()`，孙进程继承 pipe 时不返回。
     **✅ 已修复（方案 B，2026-10-13）**。
  3. **两件事互相独立，可分批交付**：per-call timeout 是增量功能（方案 A，未开工），
     防卡死改造是 bugfix（方案 B，已完成）。

---

## 1. 现状对比：Sherry vs ZCode

### 1.1 Sherry `terminal`

| 维度 | 现状 |
|---|---|
| 默认超时 | 30 秒（`config/features/agent_side/tools_timeouts.py:59`） |
| 超时上限 | 就是 30 秒，没有单独的上限概念 |
| per-call timeout | ❌ 不支持——`SafeShellInput` schema 只有 `commands`/`session_id`/`sandbox`，无 `timeout` 字段 |
| per-session 配置 | ❌ 不支持——没有 API 或 state register 能在运行时改这个值 |
| 全局配置方式 | 只能编辑源码 `tools_timeouts.py:59` 然后重启进程 |
| 绑定时机 | import 时绑定一次（`terminal.py:85`），之后不再读取 `TOOLS_TIMEOUTS` |
| 卡死风险 | ✅ 不会——fd-direct-write + 有界 reap |

**关键代码路径：**

```python
# terminal.py:85 — import 时绑定
TERMINAL_TIMEOUT = TOOLS_TIMEOUTS["terminal_timeout_seconds"]  # = 30

# terminal.py:448 — 同步等待，30s 超时
proc.wait(timeout=TERMINAL_TIMEOUT)

# terminal.py:610 — 异步等待，30s 超时
await asyncio.wait_for(proc.wait(), timeout=TERMINAL_TIMEOUT)

# 超时后：reap_process — terminate → 5s → kill → 5s → 放弃句柄（不永久阻塞）
```

### 1.2 ZCode `Bash`

| 维度 | 现状 |
|---|---|
| 默认超时 | 120 秒（`DEFAULT_BASH_TIMEOUT_MS = 120_000`） |
| 超时上限 | 600 秒（`DEFAULT_BASH_MAX_TIMEOUT_MS = 600_000`） |
| per-call timeout | ✅ 支持——输入 schema 有可选 `timeout`（毫秒），`allowCallOverride: true` |
| 全局配置方式 | 环境变量 `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` |
| 绑定时机 | runtime-config 构建时解析（启动 / session 创建），不是 import 时 |
| 卡死风险 | ✅ 不会——杀进程树 + 5s 后强制 SIGKILL + IO drain 超时保护 |

**关键代码路径：**

```ts
// bash.ts — 输入 schema 有 timeout 字段
timeout: semanticNumber(z.number()).optional()
  .describe(`Optional timeout in milliseconds (max ${MAX_BASH_TIMEOUT_MS})`)

// timeout.ts:181-204 — per-call 值封顶 + 加清理宽限
const requestedMs = policy?.allowCallOverride && typeof input.timeout === "number"
  ? input.timeout : defaultMs;
const cappedMs = Math.min(requestedMs, policy.maxMs);  // 封顶 600s
return cappedMs + cleanupGraceMs;  // +6s 清理宽限

// bash-timeout-policy.ts:14-28 — 环境变量可配默认值和上限
const defaultTimeoutMs = parsePositiveTimeout(env.BASH_DEFAULT_TIMEOUT_MS) ?? 120_000;
const maxTimeoutMs = parsePositiveTimeout(env.BASH_MAX_TIMEOUT_MS) ?? 600_000;
```

### 1.3 差异一览

| | Sherry | ZCode |
|---|---|---|
| 模型能决定超时吗 | ❌ | ✅ 每次调用传 `timeout` |
| 默认值合理吗 | 30s 偏短（编译/安装必超时） | 120s 合理 |
| 用户能调默认值吗 | 改源码 + 重启 | 设环境变量 |
| 有上限保护吗 | 无（因为不可调） | 有（600s 封顶） |

---

## 2. 兄弟工具的卡死风险（terminal 已修，这三个没有）

### 2.1 `python_repl`（`agent/tools/python_repl.py:171-183`）

```python
# 超时后的"善后"——无界 communicate() 是卡死根源
proc.kill()                    # 只杀直接子进程，孙进程存活
proc.communicate()             # ← 无 timeout！孙进程继承 pipe → 永久阻塞
```

- 风险场景：Python 脚本里 `os.system("sleep 9999")` 或 subprocess fork 子进程
- 卡死条件：孙进程继承了 stdout/stderr 的 pipe fd
- 影响：agent 整个 turn 永久挂起，只能进程级 kill

### 2.2 `user_terminal_service`（`server/service/user_terminal_service.py:78-109`）

```python
# 同样的模式
proc.kill()                    # 只杀直接子进程
await proc.communicate()       # ← 无 timeout！同样的 pipe-EOF 挂起
```

- 这是操作员控制台面板（`POST /terminal/run`），不是 agent 的 terminal 工具
- 超时 120 秒（`user_terminal_timeout_seconds`），但超时后的善后同样无界

### 2.3 `ptc/runner.py`（`agent/tools/ptc/runner.py:275-291`）

```python
# 比 python_repl 好一点：用了 killpg 杀整组
os.killpg(os.getpgid(pid), SIGKILL)   # 杀进程组，缓解了孙进程问题
await asyncio.to_thread(proc.communicate)  # ← 仍无 timeout！
```

- `start_new_session=True` + `killpg(SIGKILL)` 能杀掉孙进程，**大多数情况下不卡**
- 残留风险：不可中断 IO（进程卡在死 NFS 挂载上，SIGKILL 不被投递），无界
  `communicate()` 仍会阻塞——这是 `process_reap.py` 文档里明确提到的边界

### 2.4 三个工具的共同修复方向

把 `PIPE + communicate()` 改成和 `terminal.py` 一样的 **fd-direct-write-to-file +
`reap_process`/`areap_process`** 方案。`process_reap.py` 这个防卡死原语已经写好，
直接复用即可，不需要重新造轮子。

---

## 3. 实施方案

### 方案 A：terminal per-call timeout（增量功能，推荐先做）

**目标：** 让模型在调用 terminal 时能指定超时时长，未指定时用默认值，封顶在上限以内。

**改动范围：**

| 文件 | 改动 |
|---|---|
| `config/features/agent_side/tools_timeouts.py` | 加 `terminal_max_timeout_seconds: int = 600`（上限） |
| `agent/tools/terminal.py` | `SafeShellInput` 加 `timeout: int | None = None` 字段；`_run`/`_arun` 签名加 `timeout` 参数；有效值 = `min(timeout or TERMINAL_TIMEOUT, TERMINAL_MAX_TIMEOUT)` |

**设计要点：**

1. **输入 schema 加字段**——`SafeShellInput`（`terminal.py:246-258`）加：

   ```python
   timeout: int | None = Field(
       default=None,
       description=(
           "Optional max execution time in seconds. "
           f"Defaults to {TERMINAL_TIMEOUT}s, max {TERMINAL_MAX_TIMEOUT}s. "
           "Use a longer timeout for compilation, installation, or "
           "long-running builds."
       ),
   )
   ```

2. **_run / _arun 签名加参数**——两个入口都加 `timeout=None`，解析有效值：

   ```python
   def _run(self, commands, run_manager=None, sandbox=True,
            session_id="", timeout=None, **kwargs) -> str:
       effective_timeout = _resolve_timeout(timeout)
       return self._execute_sync(commands, sandbox, session_id, effective_timeout)

   def _arun(self, commands, run_manager=None, sandbox=True,
             session_id="", timeout=None, **kwargs) -> str:
       effective_timeout = _resolve_timeout(timeout)
       # ... 异步路径用 effective_timeout 替换 TERMINAL_TIMEOUT
   ```

   ```python
   def _resolve_timeout(requested: int | None) -> int:
       if requested is None:
           return TERMINAL_TIMEOUT
       if requested <= 0:
           return TERMINAL_TIMEOUT
       return min(requested, TERMINAL_MAX_TIMEOUT)
   ```

3. **_execute_sync 透传**——把 `effective_timeout` 作为参数传入，替换写死的
   `TERMINAL_TIMEOUT`（`terminal.py:448` 的 `proc.wait(timeout=...)`）。

4. **不做 per-session 配置**——ZCode 也不做，per-call 已经足够灵活。
   如果后续要全局调默认值，改 `tools_timeouts.py` 里的
   `terminal_timeout_seconds` 即可（已有键，重启生效）。

5. **不做环境变量**——Sherry 的配置体系是 TypedDict + Python 常量，不走环境变量
   覆盖（和 ZCode 的 Node 体系不同）。保持一致性。

**预期行为：**

```
模型调用: terminal(commands=["npm run build"], timeout=300)
→ effective_timeout = min(300, 600) = 300 秒
→ proc.wait(timeout=300)
→ 正常完成或 300s 超时后 reap_process

模型调用: terminal(commands=["ls"])
→ 未传 timeout → effective_timeout = 30（默认）
→ proc.wait(timeout=30)
```

**测试：**

- `tests/agent/tools/test_terminal.py`（如不存在则新建）
- 测用例：未传 timeout → 默认值；传 timeout=60 → 60s；传 timeout=999 → 封顶 600s；
  传 timeout=0 或负数 → fallback 默认值
- 复用现有的 `_execute_sync` mock 模式

### 方案 B：兄弟工具防卡死改造（bugfix，可后做）

**目标：** 把 `python_repl`、`user_terminal_service`、`ptc/runner.py` 从
`PIPE + communicate()` 改成 fd-direct-write + `reap_process`。

**改动范围：**

| 文件 | 改动 |
|---|---|
| `agent/tools/python_repl.py:171-183` | `stdout=PIPE` → fd-direct-write 文件；`communicate()` → `proc.wait(timeout=)` + `reap_process` |
| `server/service/user_terminal_service.py:78-109` | 同上（异步路径用 `areap_process`） |
| `agent/tools/ptc/runner.py:275-291` | 同上（已有 `killpg`，但仍需去掉无界 `communicate`） |

**设计要点：**

1. **复用 terminal.py 的基础设施**——`_open_stdout_tmp`、`_read_or_evict_output`、
   `_discard_stdout_tmp`、`sweep_stale_temp_files` 这些函数都在
   `terminal.py` 里，考虑提取到 `agent/tools/pub_base/` 共享，或者直接 import。

2. **复用 `process_reap.py`**——`reap_process` / `areap_process` 已经是通用原语，
   三个工具直接调用即可，不需要各自实现。

3. **ptc 的残留风险**——即使改了，不可中断 IO 场景下仍需 `reap_process` 的
   "放弃句柄"兜底（`process_reap.py` 的设计目标就是这个）。

4. **python_repl 的 stdout 需求**——python_repl 需要分别拿到 stdout 和 stderr
   （不像 terminal 是合并的），需要开两个临时文件或在文件里用分隔标记。
   最简方案：两个 fd（`stdout` 一个文件，`stderr` 一个文件）。

5. **user_terminal_service 是异步路径**——用 `asyncio.create_subprocess_exec` +
   `areap_process`，和 terminal.py 的 `_arun` 对齐。

**测试：**

- `tests/agent/tools/test_python_repl.py`——fork 孙进程的命令（`python -c "import os; os.system('sleep 9999')"`），
  验证超时后不阻塞
- `tests/server/service/test_user_terminal_service.py`——同上
- `tests/agent/tools/ptc/test_runner.py`——同上

---

## 4. 优先级与依赖

```
方案 A（per-call timeout）          方案 B（兄弟工具防卡死）
     │                                    │
     │ 无依赖，可独立交付                    │ 无依赖，可独立交付
     │                                    │
     ▼                                    ▼
  terminal.py + tools_timeouts.py    python_repl + user_terminal_service + ptc
  （2 个文件）                        （3 个文件）
```

**推荐顺序：A → B**

- A 是用户可感知的功能改进（长命令不再必然超时），改动小（2 文件），风险低
- B 是内部健壮性改进（防卡死），改动大（3 文件 + 提取共享基础设施），可独立排期
- 两者无代码依赖，B 不等 A 也能做

---

## 5. 不做什么

- **不做 per-session timeout 配置**——per-call 已经覆盖所有场景，per-session 增加
  state register + park/promote 机制的复杂度，收益不明
- **不做环境变量覆盖**——Sherry 的配置体系是 TypedDict 常量，不走 env overlay
- **不把 terminal_timeout_seconds 从 30 调到 120**——默认值可以保持 30（短命令
  快速失败），有了 per-call timeout 后模型自己会在长命令时传更大的值
- **不做交互式 PTY 模式**——terminal 是 one-shot exec，不是交互终端
  （Octop / deepseek-harness 是交互 PTY，设计目标不同）

---

## 6. 方案 B 实施记录（2026-10-13 完成）

### 6.1 改动文件清单

| 文件 | 改动 |
|---|---|
| **`agent/tools/pub_base/stdout_fd.py`**（新建） | 从 terminal.py 提取的共享 fd-direct-write 基础设施：`open_stdout_tmp(session_id, *, prefix)`、`read_or_evict_output(tmp_path, session_id, encoding, *, evict_prefix)`、`discard_stdout_tmp(handle, path)`。参数化了 temp 文件前缀和 evict 文件名前缀。`unlink` 在 `finally` 块中用 `try/except OSError` 包裹，防止 Windows 上文件被放弃的进程锁定时丢失已读取的数据。 |
| **`agent/tools/terminal.py`** | 删除本地定义的三个 helper 和常量（`FD_WRITE_THRESHOLD`、`_STDOUT_TMP_PREFIX` 等），改为从 `pub_base.stdout_fd` 导入。调用点去掉下划线前缀，传 `evict_prefix="terminal-"` 保持 evict 文件名不变。删除不再使用的 `hashlib`/`shutil`/`tempfile`/`os` 导入。行为完全不变。 |
| **`agent/tools/python_repl.py`** | `Popen(stdout=PIPE, stderr=PIPE, text=True)` + `communicate(timeout=)` + `proc.kill(); communicate()` → 两个临时文件（stdout 信封 + stderr fallback）+ `proc.wait(timeout=)` + `reap_process`。`_run_with_timeout` 签名加 `session_id` 关键字参数，`_run`/`_arun` 传 `session_id=session_id`。`finally` 块清理两个 temp 文件。 |
| **`server/service/user_terminal_service.py`** | `create_subprocess_shell(stdout=PIPE)` + `wait_for(communicate(), timeout)` + `proc.kill(); communicate()` → 一个临时文件（merged）+ `wait_for(proc.wait(), timeout)` + `areap_process`。超时路径读文件拿 partial output。`CancelledError` 路径用 `areap_process` 替代 `proc.kill()`。 |
| **`agent/tools/ptc/runner.py`** | `Popen(stdout=PIPE, stderr=PIPE, text=True)` + `to_thread(communicate, timeout=)` + `_kill_process_tree; to_thread(communicate)` → 两个临时文件 + `to_thread(proc.wait, timeout=)` + `to_thread(reap_process, proc)`。保留 `start_new_session=True`。`finally` 块新增 `discard_stdout_tmp` 两个文件（在 `server.stop()` 之前），保留防御性 `_kill_process_tree`。 |

### 6.2 测试改动

| 文件 | 改动 |
|---|---|
| `tests/agent/tools/test_terminal_fd_write.py` | 导入从 `agent.tools.terminal` 改为 `agent.tools.pub_base.stdout_fd`；`monkeypatch` 从 `terminal` 改为 `stdout_fd` 模块 |
| `tests/agent/tools/pub_base/test_python_repl_tool.py` | `_FakeProc` 改为 fd-direct-write 模式（写 file handle + `wait()` + `communicate()` 断言守卫）；新增 `test_timeout_uses_reap_not_communicate` 回归测试（`_HangingProc` 验证超时路径调用 `reap_process` 而非 `communicate`） |
| `tests/agent/tools/ptc/test_runner_hardening.py` | `_FakeProc` 同上模式更新 |
| `tests/server/test_user_terminal.py` | 新增 `test_a_backgrounded_grandchild_does_not_hang`（POSIX-only，`sleep 9999 &` 验证孙进程存活不阻塞） |

### 6.3 测试结果

- **131 passed, 1 skipped, 2 failed**（Windows 环境）
- 2 个失败是预存 Windows 问题（`pwd` 返回 POSIX 路径、`exit 3` 不经 Git Bash 传播退出码），与本次改动无关
- Lint（ruff check）和 format（ruff format --check）全部通过

### 6.4 核心修复原理

旧代码超时后 `proc.kill()` 再调无界 `communicate()`——孙进程继承 pipe 写端时 pipe 永不 EOF，永久阻塞。

新代码把 stdout/stderr 指向**临时文件**而非 pipe。`proc.wait(timeout=)` 只依赖直接子进程退出（OS `waitpid`），与孙进程是否存活无关。超时后 `reap_process` 做有界升级（SIGTERM→5s→SIGKILL→5s→放弃句柄），每步有上界，最坏 ~40s 后返回。

同类型项目的做法对比：

| 项目 | 超时后杀进程 | 无界等待？ | 方式 |
|---|---|---|---|
| codex | 杀进程组 SIGKILL | 不会 | pipe + `tokio::time::timeout(2s)` + `handle.abort()` 有界 drain |
| hermes-agent | killpg SIGTERM→1s→SIGKILL→2s | 不会 | pipe + daemon drain thread + `select()` + `join(timeout=2)` |
| ZCode | 杀进程树 SIGTERM→1.5s→SIGKILL | 不会 | bash 模式用 FILE（无 pipe）+ `IO_DRAIN_TIMEOUT_MS(1s)` + `forceExitTimer(5s)` |
| opencode | force-kill after 3s | **会** | SIGKILL 后 `Deferred.await(signal)` 无界等 `close` 事件 |
| **Sherry（本次修复后）** | `reap_process` SIGTERM→5s→SIGKILL→5s→放弃 | **不会** | FILE（无 pipe）+ `proc.wait(timeout=)` |

Sherry 的方案和 ZCode 的 bash 模式本质相同：输出写文件而非 pipe，`wait` 只等直接子进程退出。Sherry 的 `reap_process` 比 ZCode 多了一层"放弃句柄"兜底（SIGKILL 不生效时不永久阻塞）。

---

## 7. 后台运行对比：ZCode vs Sherry

### 7.1 ZCode 的 `run_in_background` 机制

ZCode 的 bash 工具有一个 `run_in_background` 参数，但它**不是用 shell `&` 实现的**，而是一个工具级别的后台生命周期管理机制：

1. **纯 Node `spawn()`**——不用 shell `&`。进程通过 Node 的 `spawn()` 直接启动，POSIX 上 `detached: true`（新进程组），Windows 上 `detached: false`。
2. **不 `unref()`**——后台进程不脱离 Node 事件循环，进程的生命周期绑定到 ZCode CLI 会话（不是 OS 级脱离）。进程在会话内跨 turn 运行，退出时触发通知重新唤醒 agent。
3. **stdout/stderr 写入文件**——和 Sherry 的 fd-direct-write 一样，child 的 stdio 直接指向文件 fd（不是 pipe），输出保留在会话目录里，可按需读取（`readBackgroundBashOutput` 做有界 tail read）。
4. **返回值**——工具立即返回 `{status: "backgrounded", backgroundTaskId, outputPath}`，stdout/stderr 为空字符串。模型后续可用 `backgroundTaskId` 查状态、读输出。
5. **Windows 用 Git Bash**——ZCode 自动检测 `bash.exe`（`C:\Program Files\Git\bin\bash.exe` 等），直接 `spawn` 它（`shell: false`），**不用 `cmd.exe`**。只有找不到 Git Bash 时才 fallback 到系统 shell。

### 7.2 Sherry 的 terminal 能否后台运行？

**结论：可以用 shell `&` 在 POSIX 上后台运行（本次修复后不会卡死），但和 ZCode 的 `run_in_background` 是不同的东西。**

| | ZCode `run_in_background` | Sherry terminal `&` |
|---|---|---|
| 实现层级 | 工具级（Node spawn + 进程跟踪 + 输出文件 + 通知） | Shell 级（`/bin/sh -c "cmd &"` 的 `&`） |
| 进程生命周期 | 绑定到 CLI 会话，跨 turn 运行，退出通知 agent | 孤儿进程，脱离 agent，无跟踪 |
| 输出处理 | 写入保留文件，可按需读取 | 写入 temp 文件，工具返回后 unlink，输出丢失 |
| Windows | ✅ 用 Git Bash（自动检测 `bash.exe`） | ❌ 用 `cmd.exe`（`shell=True`），`&` 是命令分隔符不是后台符 |
| POSIX | ✅ `detached: true` | ✅ `&` 后台 + `proc.wait()` 返回 |

**在 Sherry 上后台启动服务的可行方式：**

- **POSIX**：`uv run python -m server > /tmp/server.log 2>&1 &`——shell 后台启动，工具立即返回（`wait()` 在 shell 退出后返回），服务作为孤儿进程继续运行。输出重定向到日志文件（否则写入 temp 文件被 unlink，输出丢失）。
- **Windows**：`cmd.exe` 不支持 `&` 后台。需用 `start /b` 或显式调用 Git Bash：`/bin/sh -c "uv run python -m server > server.log 2>&1 &"`（前提是 `/bin/sh` 在 PATH 中）。

### 7.3 如果要实现 ZCode 级别的后台运行

Sherry 要实现和 ZCode 一样的后台运行（进程跟踪 + 输出保留 + 跨 turn 可读），需要：

1. terminal 工具加 `run_in_background: bool` 参数
2. 后台进程注册表（session 级，跟踪 PID + 输出文件路径）
3. `proc.wait()` 不阻塞——spawn 后不等待，立即返回 `{backgroundTaskId, outputPath}`
4. 输出文件保留在 session 目录（不 unlink），可按需读取
5. Windows 上自动检测 Git Bash（`bash.exe`）替代 `cmd.exe`
6. 进程退出通知机制（可选——Sherry 有 WS push 通道可复用）

这是一个独立的 feature，不在本次防卡死改造的范围内，但本次的 fd-direct-write 基础设施（`stdout_fd.py`）可以直接复用——后台进程的输出文件就是 `open_stdout_tmp` 创建的，只是不立即 `read_or_evict_output` + `discard_stdout_tmp`，而是保留文件供后续读取。
