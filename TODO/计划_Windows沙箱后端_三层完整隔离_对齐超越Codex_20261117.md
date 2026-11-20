# 计划：Windows 沙箱后端——三层完整隔离（对齐+超越 Codex）

- 日期：2026-11-17（r3，三层全做，对齐+超越 Codex）
- 状态：**待评审**
- 定位：CreateRestrictedToken + AppContainer + Job Object 三层合一。Codex 有的 Sherry 全有，Job Object 是 Sherry 额外多的。
- 风险等级：**中-高** — launcher + 三层 ctypes Win32 API
- 依赖：现有 `sandbox.py` ABC

---

## 0. 对齐+超越 Codex

| 层 | Codex | Sherry（本计划） | 对齐/超越 |
|---|-------|-----------------|----------|
| CreateRestrictedToken（权限移除） | ✅ | ✅ | 对齐 |
| AppContainer（文件系统/网络/注册表隔离） | ✅ | ✅ | 对齐 |
| Job Object（进程限制/内存/kill-on-close） | ❌ | ✅ | **超越** |
| Fail-open 降级链 | 可能有 | ✅ 四层 | 超越 |
| Stale handle 清理 | 不清楚 | ✅ 定期+atexit | 超越 |
| Env 可配限制 | 不清楚 | ✅ | 超越 |

**三层全做**：Codex 的两层（RestrictedToken + AppContainer）Sherry 全有 + Sherry 额外加 Job Object（进程限制 + kill-on-close）。

---

## 1. 三层隔离

```
层 1: CreateRestrictedToken（Win2000+，不需要管理员）
  → 移除 SeDebug / SeTcb / SeLoadDriver / SeTakeOwnership / SeBackup / SeRestore 等
  → 效果：agent 不能提权、不能调试其他进程、不能加载驱动

层 2: AppContainer（Win8+，需要创建 profile）
  → 文件系统：只能写 writable_roots（workspace + session dir + TEMP）
  → 网络：可选限制（默认允许，SHERRY_WIN_SANDBOX_BLOCK_NETWORK 可配）
  → 注册表：限制写 HKLM
  → 效果：agent 不能破坏系统文件、不能偷其他文件

层 3: Job Object（Win2000+，不需要管理员）
  → 进程数上限（防 fork bomb，默认 50）
  → 内存上限（防 OOM，默认 4GB）
  → kill-on-close（launcher 死 → command 死）
  → 效果：agent 不能耗尽资源

AppContainer 不可用时（Win7）：层 1 + 层 3（受限 token + Job Object）
```

---

## 2. 设计——launcher 方案

### 2.1 为什么用 launcher

`wrap()` 只能改 `(argv, env)`。三层隔离都需要在**进程创建时**干预：
- CreateRestrictedToken → `CreateProcessAsUser(restricted_token, ...)`
- AppContainer → `CreateProcess` with `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`
- Job Object → `AssignProcessToJobObject` 在 spawn 后

launcher 统一处理三层，不需要改 ABC / 不需要改 spawn 点。

### 2.2 wrap() 返回 launcher 命令

```python
def wrap(self, cmd, env, writable_roots, *, allow_network=False):
    """返回 launcher 命令。launcher 内部做三层隔离。"""
    normalize_writable_roots(writable_roots)
    roots = [str(p) for p in writable_roots]
    launcher_code = self._build_launcher(cmd, roots, allow_network)
    encoded = base64.b64encode(launcher_code.encode("utf-8")).decode("ascii")
    return [sys.executable, "-c",
            f"import base64;exec(base64.b64decode('{encoded}').decode())"], env
```

### 2.3 launcher 执行流程

```
Sherry spawn → python -c <launcher>
  ↓
launcher 启动（普通 Python 进程，继承了 Sherry 的 stdout handle → 临时文件）
  ↓
层 1: OpenProcessToken + CreateRestrictedToken → restricted_token
  ↓ 失败 → fall back 到普通创建（fail-open 层 2）
层 2: 检测 AppContainer 可用？
  → 是（Win8+）: CreateAppContainerProfile → 配置 ACL → appcontainer_token
  → 否（Win7）: 跳过，只用 restricted_token
  ↓ 失败 → fall back 到 restricted_token only（fail-open 层 3）
层 3: CreateJobObject → 设置限制（进程/内存/kill-on-close）
  ↓ 失败 → 继续，不绑 Job Object（fail-open 层 4）
创建子进程: CreateProcessAsUserW(token, cmd, STARTUPINFO, bInheritHandles=TRUE)
  ↓ 失败 → fall back 到 CreateProcessW（普通创建，fail-open）
绑定 Job Object: AssignProcessToJobObject(job, child)
等待: WaitForSingleObject(child, INFINITE)
退出: GetExitCodeProcess(child) → sys.exit(exit_code)

如果 Sherry kill launcher:
  → launcher 进程死 → Job Object handle 关闭 → kill-on-close 杀 child
```

### 2.3.1 stdout/stderr 传递（关键修复）

Sherry 把 stdout 重定向到临时文件（`os.fdopen(fd)`），不是 pipe。launcher 继承了这个 handle，但 **必须显式传递给子进程**——否则子进程输出丢失。

launcher 创建子进程前必须：

```python
# 1. 获取 launcher 继承的 stdout handle（指向 Sherry 的临时文件）
stdout_handle = KERNEL32.GetStdHandle(STD_OUTPUT_HANDLE)  # -11
stderr_handle = KERNEL32.GetStdHandle(STD_ERROR_HANDLE)   # -12

# 2. 确保 handle 可被子进程继承（DuplicateHandle 标记 inheritable）
inheritable_stdout = ctypes.c_void_p()
KERNEL32.DuplicateHandle(
    KERNEL32.GetCurrentProcess(), stdout_handle,
    KERNEL32.GetCurrentProcess(), ctypes.byref(inheritable_stdout),
    0, True,  # bInheritHandle=True
    DUPLICATE_SAME_ACCESS)

# 3. 构建 STARTUPINFO（带 hStdOutput + STARTF_USESTDHANDLES）
STARTF_USESTDHANDLES = 0x00000100
si = STARTUPINFOW()
si.cb = ctypes.sizeof(STARTUPINFOW)
si.dwFlags = STARTF_USESTDHANDLES
si.hStdOutput = inheritable_stdout
si.hStdError = inheritable_stdout  # Sherry 合并 stderr 到 stdout（STDOUT）
si.hStdInput = KERNEL32.GetStdHandle(STD_INPUT_HANDLE)  # -10

# 4. 创建子进程时传 bInheritHandles=TRUE
result = ADVAPI32.CreateProcessAsUserW(
    token, None, cmd_line, None, None,
    True,  # bInheritHandles=TRUE ← 关键！
    CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, None, None,
    ctypes.byref(si), ctypes.byref(pi))
```

**AppContainer + handle 继承**：AppContainer token 限制 handle 继承。如果 AppContainer 创建的子进程无法访问继承的 stdout handle：
1. 先尝试 `bInheritHandles=TRUE` + 继承 handle（通常对文件 handle 有效）
2. 如果失败：fall back 到非 AppContainer 创建（用受限 token + 继承 handle）
3. 终极 fall back：launcher 自己读子进程 stdout 管道 → 写到自己的 stdout（多一层中转，但保证输出不丢）

### 2.3.2 性能评估

| 维度 | 值 | 影响 |
|------|-----|------|
| launcher 启动（warm） | ~50ms | 每次 tool call 一次（命令先 join 成一条再 sandbox） |
| launcher 启动（cold） | ~150-300ms | 首次调用 / DLL cache 被清 |
| launcher 内存 | ~15-17MB | 一个额外 Python 进程 |
| 频率 | 每次 terminal/python_repl 调用 1 次 | 不按命令数，按 tool call 数 |

**50ms 可接受**——terminal 命令本身通常 >1 秒，50ms 占比 <5%。如果不可接受，改为 C helper binary（~5ms 启动）。

### 2.4 进程树

```
Sherry agent (Python)
  └── launcher (python -c <launcher_code>)  ← Sherry 的 proc.pid
       └── command (cmd.exe /c <user_command>)  ← launcher 的子进程
            └── command 的子进程（如有）

Sherry kill_process_tree(launcher.pid):
  → launcher 死
  → Job Object KILL_ON_JOB_CLOSE → command + 其子进程全死
```

Sherry 看到的 `proc.pid` 是 launcher 的 PID。launcher 内部管理 command。kill-on-close 保证 launcher 死时 command 也死。

---

## 3. WinSandboxBackend 实现

### 3.1 probe

```python
def probe(self) -> bool:
    """烟雾测试：CreateRestrictedToken + CreateJobObject 都可用。"""
    if self._probe_cache is not None:
        return self._probe_cache
    try:
        if sys.platform != "win32" or _KERNEL32 is None:
            self._probe_cache = False
            return False
        # 测试 1: CreateRestrictedToken
        current_token = ctypes.c_void_p()
        if not _ADVAPI32.OpenProcessToken(
                _KERNEL32.GetCurrentProcess(),
                TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(current_token)):
            self._probe_cache = False
            return False
        restricted = ctypes.c_void_p()
        result = _ADVAPI32.CreateRestrictedToken(
            current_token, DISABLE_MAX_PRIVILEGE,
            0, None, 0, None, 0, None, ctypes.byref(restricted))
        _KERNEL32.CloseHandle(current_token)
        if not (result and restricted):
            self._probe_cache = False
            return False
        _KERNEL32.CloseHandle(restricted)
        # 测试 2: CreateJobObject
        job = _KERNEL32.CreateJobObjectW(None, None)
        if not job:
            self._probe_cache = False
            return False
        _KERNEL32.CloseHandle(job)
        self._probe_cache = True
    except Exception:
        self._probe_cache = False
    return self._probe_cache
```

### 3.2 launcher 脚本（三层合一）

launcher 是一段 Python 代码，做三件事：

```python
# launcher 核心逻辑（简化版，实际嵌入 base64）

import ctypes, json, sys

KERNEL32 = ctypes.windll.kernel32
ADVAPI32 = ctypes.windll.advapi32

# === 层 1: CreateRestrictedToken ===
current_token = ctypes.c_void_p()
ADVAPI32.OpenProcessToken(KERNEL32.GetCurrentProcess(),
    TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(current_token))

restricted_token = ctypes.c_void_p()
ADVAPI32.CreateRestrictedToken(current_token, DISABLE_MAX_PRIVILEGE,
    0, None, 0, None, 0, None, ctypes.byref(restricted_token))
KERNEL32.CloseHandle(current_token)

# === 层 2: AppContainer（Win8+ 时） ===
appcontainer_sid = None
if appcontainer_available:
    try:
        appcontainer_sid = _create_appcontainer_profile("sherry-agent")
        # 配置文件系统 ACL
        for root in writable_roots:
            _grant_write_access(appcontainer_sid, root)
        if block_network:
            _deny_network_access(appcontainer_sid)
    except Exception:
        appcontainer_sid = None  # fail-open，只用受限 token

# === 层 3: Job Object ===
job_handle = KERNEL32.CreateJobObjectW(None, None)
if job_handle:
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS |
        JOB_OBJECT_LIMIT_JOB_MEMORY |
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.BasicLimitInformation.ActiveProcessLimit = max_processes
    limits.JobMemoryLimit = max_memory_bytes
    KERNEL32.SetInformationJobObject(job_handle, 9,
        ctypes.byref(limits), ctypes.sizeof(limits))

# === 创建子进程 ===
if appcontainer_sid:
    # AppContainer 进程创建（带 SECURITY_CAPABILITIES）
    token = _create_appcontainer_token(restricted_token, appcontainer_sid)
    result = ADVAPI32.CreateProcessAsUserW(token, None, cmd_line, ...)
else:
    # 受限 token 进程创建
    result = ADVAPI32.CreateProcessAsUserW(restricted_token, None, cmd_line, ...)

if not result:
    # fail-open: 受限 token 失败 → 普通创建
    result = KERNEL32.CreateProcessW(None, cmd_line, ...)

# === 绑定 Job Object ===
if job_handle and result:
    KERNEL32.AssignProcessToJobObject(job_handle, pi.hProcess)

# === 等待 + 退出 ===
KERNEL32.WaitForSingleObject(pi.hProcess, INFINITE)
exit_code = ctypes.c_uint32()
KERNEL32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
# 清理 handles
KERNEL32.CloseHandle(pi.hProcess)
KERNEL32.CloseHandle(pi.hThread)
if restricted_token: KERNEL32.CloseHandle(restricted_token)
if job_handle: KERNEL32.CloseHandle(job_handle)  # → kill-on-close 杀残留子进程
sys.exit(exit_code.value)
```

### 3.3 AppContainer 创建（Win8+）

```python
def _create_appcontainer_profile(package_name: str):
    """创建 AppContainer profile，返回 package SID。"""
    # 1. 生成 package family name
    family_name = f"sherry.agent.{package_name}"
    
    # 2. 调用 CreateAppContainerProfile（Win8+ API）
    #    返回 package SID
    sid = ctypes.c_void_p()
    result = _USERENV.CreateAppContainerProfile(
        ctypes.c_wchar_p(package_name),
        ctypes.c_wchar_p(f"Sherry Agent Sandbox ({package_name})",
        None, 0, None, ctypes.byref(sid))
    if not result:
        return None
    
    return sid

def _grant_write_access(package_sid, path: str):
    """给 AppContainer SID 在指定路径的写权限。"""
    # 修改路径的 DACL，给 package_sid 添加 FILE_GENERIC_WRITE
    # 使用 SetNamedSecurityInfo + BuildExplicitAccessWithName
    ...

def _deny_network_access(package_sid):
    """拒绝 AppContainer 的网络访问。"""
    # 通过 Windows Firewall 规则或 AppContainer network capabilities
    ...
```

### 3.4 配置

```bash
# Job Object 限制（env 可配）
SHERRY_WIN_SANDBOX_MAX_PROCESSES=50      # 最大子进程数
SHERRY_WIN_SANDBOX_MAX_MEMORY_MB=4096     # 最大内存（MB）

# AppContainer
SHERRY_WIN_SANDBOX_BLOCK_NETWORK=false    # 是否阻止网络（默认允许）

# SANDBOX_POLICY（同现有）
SANDBOX_POLICY=auto       # auto → Windows 后端 probe → 三层隔离
SANDBOX_POLICY=required   # required → 必须可用
SANDBOX_POLICY=off        # off → 不用沙箱
```

---

## 4. Fail-open 四层降级链

```
层 1: probe() 失败
  → ctypes 不可用 / API 被禁 / 非 Windows
  → get_backend() 返回 None → 裸 subprocess + 日志（现有行为）

层 2: CreateRestrictedToken 失败（launcher 内）
  → fall back 到普通 CreateProcessW（无受限 token）
  → Job Object + AppContainer 也跳过（都需要进程 token）
  → 命令继续执行 + stderr 警告

层 3: AppContainer 创建失败（Win7 / 权限不足 / SID 失败）
  → 只用受限 token + Job Object（不用 AppContainer）
  → 文件系统/网络/注册表不隔离，但权限已移除 + 进程已限制
  → 命令继续执行

层 4: Job Object 创建或绑定失败
  → 只用受限 token（+ AppContainer 如果可用）
  → 进程限制/kill-on-close 不生效
  → 命令继续执行
```

**每一层独立 fail-open**——即使三层中两层都失败，剩下的一层仍然提供部分保护。

---

## 5. 不需要改的东西

| 组件 | 改不改 | 理由 |
|------|--------|------|
| SandboxBackend ABC | ❌ 不改 | 不加 post_spawn，launcher 自己处理 |
| terminal/core.py spawn 点（4 处） | ❌ 不改 | launcher 在 wrap() 返回的命令里 |
| python_repl.py | ❌ 不改 | 调同一个 get_backend().wrap() |
| bwrap 后端 | ❌ 不改 | Linux 不受影响 |
| seatbelt 后端 | ❌ 不改 | macOS 不受影响 |
| HITL / parallel_safety / PTC | ❌ 不改 | 沙箱独立于这些机制 |

**只改 2 个文件 + 新建 2 个文件**。

---

## 6. 文件变更清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/tools/pub_base/sandbox_win.py` | 新建：WinSandboxBackend + launcher 生成 + 三层 ctypes | ~500 |
| `agent/tools/pub_base/sandbox.py` | 改：注册 Windows 后端（1 行） | 1 |
| `agent/tools/terminal/core.py` | 改：shell 构造平台感知（2 行） | 2 |
| `tests/agent/tools/pub_base/test_sandbox_win.py` | 新建：probe + 三层 + fail-open + **stdout 传递** + 后台输出测试 | ~200 |
| **总计** | | **~703** |

---

## 7. 验收

### 三层隔离

| # | 场景 | 层 | 预期 |
|---|------|---|------|
| 1 | agent 尝试 taskkill 其他进程 | 层 1 | SeDebugPrivilege 移除 → 阻止 |
| 2 | agent 尝试加载驱动 | 层 1 | SeLoadDriverPrivilege 移除 → 阻止 |
| 3 | agent 写 C:\Windows | 层 2 | AppContainer 文件系统隔离 → 阻止（Win8+） |
| 4 | agent 写用户其他目录 | 层 2 | AppContainer → 阻止（Win8+） |
| 5 | agent 访问网络（BLOCK_NETWORK=true） | 层 2 | AppContainer → 阻止 |
| 6 | agent spawn >50 子进程 | 层 3 | Job Object → 第 51 个被拒绝 |
| 7 | agent 用 >4GB 内存 | 层 3 | Job Object → 进程被杀 |
| 8 | launcher 被 kill | 层 3 | kill-on-close → command 全死 |

### stdout/stderr 传递（关键修复验证）

| # | 场景 | 预期 |
|---|------|------|
| 9 | sandboxed `echo hello` | 输出 "hello"（不丢失） |
| 10 | sandboxed `dir C:\workspace` | 输出目录列表 |
| 11 | sandboxed 命令 stderr 输出 | stderr 合并到 stdout（Sherry 现有行为） |
| 12 | 后台 sandboxed 命令 | output 文件非空，`read_output_tail` 有内容 |
| 13 | sandboxed 命令超时 | 部分输出保留（launcher 被 kill → kill-on-close 杀子进程） |
| 14 | AppContainer 模式 stdout 传递 | 输出不丢（或 fall back 到非 AppContainer） |

### Fail-open

| # | 场景 | 预期 |
|---|------|------|
| 9 | probe 失败 | 降级为裸 subprocess |
| 10 | CreateRestrictedToken 失败 | 普通创建 + 警告 |
| 11 | AppContainer 不可用（Win7） | 受限 token + Job Object（不用 AppContainer） |
| 12 | Job Object 绑定失败 | 受限 token + AppContainer（不用 Job Object） |
| 13 | CreateProcessAsUserW 失败 | CreateProcessW 普通创建 |

### 不破坏现有

| # | 场景 | 预期 |
|---|------|------|
| 14 | Linux bwrap | 不变 |
| 15 | macOS seatbelt | 不变 |
| 16 | terminal 超时 | launcher 被 kill → kill-on-close 杀 command |
| 17 | terminal 后台 | launcher 在后台跑，watcher 监控 launcher |
| 18 | 子 agent | 继承 SANDBOX_POLICY → 三层隔离 |
| 19 | eval harness | SANDBOX_POLICY=auto → Windows backend |
| 20 | python_repl | 同 terminal 路径 |

### 配置

| # | 场景 | 预期 |
|---|------|------|
| 21 | SHERRY_WIN_SANDBOX_MAX_PROCESSES=10 | Job Object 进程上限 10 |
| 22 | SHERRY_WIN_SANDBOX_MAX_MEMORY_MB=1024 | 内存上限 1GB |
| 23 | SHERRY_WIN_SANDBOX_BLOCK_NETWORK=true | AppContainer 阻止网络 |

---

## 8. 和 Codex 的对比

| 维度 | Codex | Sherry（本计划） |
|------|-------|-----------------|
| CreateRestrictedToken | ✅ | ✅ 对齐 |
| AppContainer | ✅ | ✅ 对齐 |
| Job Object | ❌ | ✅ **超越** |
| 进程数限制 | ❌（靠 timeout） | ✅ Job Object |
| 内存限制 | ❌ | ✅ Job Object |
| kill-on-close | ❌（靠 process-tree kill） | ✅ Job Object |
| Fail-open 降级 | 不清楚 | ✅ 四层独立降级 |
| Stale handle 清理 | 不清楚 | ✅ 定期 + atexit |
| Env 可配限制 | 不清楚 | ✅ |
| 版本要求 | Win8+（AppContainer） | **Win2000+**（受限 token + Job Object 在 Win7 也可用，AppContainer 在 Win8+ 自动启用） |
| 管理员权限 | 需要（AppContainer） | **不需要**（受限 token + Job Object 不需要管理员，AppContainer 失败时 fail-open） |

**Sherry 超越 Codex 的点**：
1. Job Object（进程限制 + 内存限制 + kill-on-close）——Codex 没有
2. Win7 支持（受限 token + Job Object 在 Win7 可用，AppContainer 自动跳过）
3. 不需要管理员（AppContainer 失败时 fail-open 到受限 token + Job Object）
4. 四层独立 fail-open（每一层独立降级）

---

## 9. Kill criteria

- **stdout 传递失败**（最关键）：如果 `CreateProcessAsUserW` + `bInheritHandles=TRUE` + `STARTUPINFO.hStdOutput` 仍然丢失输出 → launcher 改为自己创建 pipe 给子进程 + 读 pipe 写到自己的 stdout（多一层中转但保证不丢）
- **AppContainer + handle 继承冲突**：如果 AppContainer token 创建的子进程无法继承 stdout handle → AppContainer 路径 fall back 到非 AppContainer 创建（受限 token + Job Object 仍有效，文件系统隔离降级）
- 如果 launcher 导致 ~50ms 延迟不可接受 → 评估用 C helper 替代 Python launcher（~5ms 启动）
- 如果 AppContainer profile 创建在某些 Win8/10 版本上失败 → fail-open 到受限 token + Job Object
- 如果 `CreateProcessAsUserW` 在某些环境下不稳定 → fall back 到 `CreateProcessW`
- 如果 base64 launcher 代码太长（超过 Windows argv 上限 32767 字符）→ 改为写临时 .py 文件 + 执行文件
- 如果 launcher 的 grandchild 进程逃逸 Job Object（CREATE_BREAKAWAY_FROM_JOB）→ 加 `JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0` 禁止 breakaway
