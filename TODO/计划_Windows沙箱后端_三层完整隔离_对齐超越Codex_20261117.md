# 计划：Windows 沙箱后端——AppContainer 文件/网络隔离（第 2 阶段）

- 日期：2026-11-20（r4：第 1 阶段已实施并合入，本文件只保留剩余工作）
- 状态：**待实施**（层 1 + 层 3 已上线；本阶段只做层 2）
- 定位：在已合入的 launcher 上补 AppContainer——文件写入围堵、读遮蔽、按需断网。
- 依赖：现有 `agent/tools/pub_base/sandbox_win.py`、共享 `sandbox.py` ABC（本阶段**不改** ABC / spawn 点）

---

## 0. 第 1 阶段完成情况（2026-11-20）

已合入 Windows terminal 沙箱（`WinSandboxBackend`，注册于 `platform.system() == "Windows"`）：

| 能力 | 状态 | 位置 |
|---|---|---|
| launcher（`python -c <base64>`，payload 内嵌 cmd/roots/limits） | ✅ | `sandbox_win.py::build_launcher_source` |
| 层 1 受限令牌（`CreateRestrictedToken(DISABLE_MAX_PRIVILEGE)`） | ✅ | launcher |
| 层 3 Job Object（ActiveProcessLimit / JobMemoryLimit / KILL_ON_JOB_CLOSE，挂起后绑定再恢复） | ✅ | launcher |
| stdout/stderr 传递（可继承副本 + `STARTF_USESTDHANDLES` + `bInheritHandles`） | ✅ | launcher |
| 每层独立 fail-open + stderr 报 Win32 错误码 | ✅ | launcher |
| probe（OpenProcessToken + CreateRestrictedToken + CreateJobObject） | ✅ | `WinSandboxBackend.probe` |
| 配置 `SHERRY_WIN_SANDBOX_MAX_PROCESSES` / `_MAX_MEMORY_MB` / `_BLOCK_NETWORK` | ✅（BLOCK_NETWORK 只告警） | `read_win_config` |
| terminal 沙箱 shell 平台感知（Git Bash → COMSPEC） | ✅ | `terminal/core.py::_sandbox_shell_argv` |
| 全条件路径测试（配置/负载/探测/真实 stdout/退出码/cwd/env/进程上限/内存上限/kill-on-close） | ✅ | `tests/agent/tools/pub_base/test_sandbox_win.py` |

**尚未强制执行**：`writable_roots`（写入围堵）、读遮蔽、`SHERRY_WIN_SANDBOX_BLOCK_NETWORK`、注册表限制——它们全部属于层 2。当前 Windows 上的文件边界仍是工具层路径门禁 + HITL。

---

## 1. 剩余目标

| 层 | 能力 | 状态 |
|---|---|---|
| 1 | 受限令牌：移除全部特权（不能调试进程、加载驱动、夺取所有权） | ✅ 已完成 |
| 2 | **AppContainer：只能写 writable_roots；读默认隔离；按需断网；HKLM 只读** | ⬜ 本阶段 |
| 3 | Job Object：进程数 / 内存 / kill-on-close | ✅ 已完成 |

层 2 的验收口径：沙箱命令**能**读写 `writable_roots`（项目目录 + 本会话 `SESSIONS_DIR/<id>` + `TEMP_DIR`），**不能**写这些之外的用户目录或 `C:\Windows`，读不到未授权用户文件（AppContainer 默认），`SHERRY_WIN_SANDBOX_BLOCK_NETWORK=1` 时无外网，同时层 1/层 3 与 stdout 传递全部保持。

---

## 2. AppContainer 设计

### 2.1 Profile（每用户一个，幂等）

- 名称：`sherry-agent-<sha256(用户名)[:12]>`（稳定即可，不随会话变化；session 隔离靠 ACL 的 root 集合，不靠 profile 数量）。
- 创建顺序：
  1. `DeriveAppContainerSidFromAppContainerName(name, &sid)` —— 已存在则直接复用；
  2. 不存在则 `CreateAppContainerProfile(name, display_name, description, NULL, 0, &sid)`；
  3. 两者都失败 → 层 2 整体 fail-open（保留当前层 1 + 层 3）。
- **不删除 profile**：`DeleteAppContainerProfile` 会连带删掉该用户所有 Sherry 沙箱的授权状态，运行期删除等于给下一个会话埋坑；卸载是打包/文档层面的事，不在 launcher 里做。

### 2.2 以 AppContainer 创建进程

launcher 里新增一条创建分支，优先于现有 `CreateProcessAsUserW`：

```text
SECURITY_CAPABILITIES caps = { AppContainerSid = sid, Capabilities = NULL / {internetClient}, CapabilityCount = 0 / 1 }
InitializeProcThreadAttributeList(NULL, 1, 0, &size) → 分配
UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, &caps, sizeof(caps), NULL, NULL)
STARTUPINFOEXW si = { StartupInfo: 现有 STARTF_USESTDHANDLES + 三个 handle, lpAttributeList = list }
CreateProcessAsUserW(restricted_token, ..., EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, ...)
→ AssignProcessToJobObject → ResumeThread
```

要点：

- **必须同时带受限令牌**：AppContainer 单独用也能隔离，但受限令牌是层 1 的既有保证；两者叠加没有冲突。
- `EXTENDED_STARTUPINFO_PRESENT` 与现有 `STARTF_USESTDHANDLES` 共存；不继承 handle 的实现（kill criterion 2）直接回退。
- 环境变量继续继承 launcher 的环境（scrubbed），`TEMP`/`TMP` 指向 `TEMP_DIR`（该目录在 2.3 里授权）。

### 2.3 ACL 授权（写入面）

对 payload 的每个 `roots` 条目 + `TEMP_DIR`：

- 给 package SID 添加一条 `FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE` 的 allow ACE（`SetNamedSecurityInfoW` + `EXPLICIT_ACCESS_W`，`SUB_CONTAINERS_AND_OBJECTS_INHERIT`，保证新建文件/子目录继承）。
- 幂等：先枚举 DACL，已有同 SID 同 mask 的 ACE 就跳过；单个 root 失败只记 stderr，继续处理其余 root（fail-open per-root）。
- 只授权**这些 root**，不给 `C:\Users\<user>` 整体、不给仓库外的任何路径。

读取面：

- AppContainer 进程默认读不到用户目录（只有系统目录上 `ALL APPLICATION PACKAGES` 的 ACE 可读），因此读遮蔽天然成立；`SHERRY_DENY_READ_PATHS` 在 Windows 上没有额外工作。
- **shell 与解释器本身**必须可读可执行：Git Bash（`resolve_windows_bash()` 的路径）与 `sys.executable` 所在目录通常位于 `Program Files` / venv。若这些目录没有 `ALL APPLICATION PACKAGES` 读权限，launcher 必须给 shell 的**安装目录**（不是每个文件）补一条只读 + 执行 ACE，否则命令根本起不来。这是本阶段最可能踩的坑，验收里单列。
- `read_file` 等文件工具不经沙箱，行为不变。

### 2.4 网络

- `SECURITY_CAPABILITIES` 里**不加** `internetClient` → AppContainer 无外网（默认）。
- `SHERRY_WIN_SANDBOX_BLOCK_NETWORK` 语义就此变成真实开关：为 false 时加 `internetClient`（保持旧行为“默认允许网络”）；为 true 时不加。PTC 的 `allow_network=True` 映射为“加 capability”，与 bwrap 的 `--share-net` 对齐。
- 不做防火墙规则（需要管理员，且会把整个用户/进程路径变成配置面）；capability 粒度已经够用。

### 2.5 注册表与系统

- AppContainer 的 HKCU 是独立 hive；HKLM 默认只读，无需额外 ACE。
- 不授予 `lpacCapability` / `runAsClient` 等能力。

---

## 3. 代码改动点

| 文件 | 改动 |
|---|---|
| `agent/tools/pub_base/sandbox_win.py` | launcher 内新增 `_create_appcontainer_profile` / `_grant_root_access` / `_spawn_in_appcontainer`；`wrap()` 的 payload 增加 `appcontainer_name`；`read_win_config` 增加 `SHERRY_WIN_SANDBOX_APPCONTAINER`（默认 on，off 时保持当前层 1+3） |
| `tests/agent/tools/pub_base/test_sandbox_win.py` | 新增层 2 用例（profile 复用 / 授权幂等 / 越界写被拒 / 网络被拒 / shell 可执行 / 回退） |
| `wiki/sandbox/README*.md` + `isolation/README*.md` | 把“AppContainer 未实现”改为已实现，补验收与限制 |
| `AGENTS.md` | 更新 Windows 段落的层 2 状态 |

不改：ABC、`terminal/core.py`、`python_repl.py`、bwrap / seatbelt、HITL、PTC。

---

## 4. Fail-open（在现有四步上再加三步）

```text
probe 失败                                   → 裸 subprocess（现有）
CreateRestrictedToken 失败                   → 普通 CreateProcessW（现有）
AppContainer profile 创建/派生失败            → 受限令牌 + Job Object（层 2 跳过）
某个 root 授权失败                            → 跳过该 root，其余继续（命令仍启动）
EXTENDED_STARTUPINFO / 属性列表失败           → 现有 CreateProcessAsUserW 分支
Job Object 创建/绑定失败                      → 受限令牌（+ AppContainer 若可用）
```

每一层失败都在 stderr 留 Win32 错误码；`SANDBOX_POLICY=required` 的语义不变（probe 仍只看层 1 + 层 3，AppContainer 不参与 probe）。

---

## 5. 验收

### 层 2 隔离

| # | 场景 | 预期 |
|---|------|------|
| 1 | 在 `writable_roots` 内创建/改写/删除文件 | 成功 |
| 2 | 写 `C:\Windows` / 用户桌面 / 其他会话目录 | 拒绝（Access denied） |
| 3 | 读 `~/.ssh` / `~/.aws` | 拒绝 |
| 4 | `SHERRY_WIN_SANDBOX_BLOCK_NETWORK=1` 时访问外网 | 失败（无 internetClient） |
| 5 | 默认（BLOCK_NETWORK 未设）访问外网 | 成功 |
| 6 | `git status` / `bash -c 'echo hi'` | 正常执行（shell 目录已授权） |
| 7 | `python_repl` 同一路径 | 正常执行 |
| 8 | 同一会话连续多次 terminal 调用 | profile 复用，无新 profile 堆积 |
| 9 | 层 2 被 `SHERRY_WIN_SANDBOX_APPCONTAINER=0` 关闭 | 退回当前层 1 + 层 3，命令仍执行 |
| 10 | profile 创建被策略禁止 | 命令仍执行（层 2 跳过，stderr 有错误码） |

### 不回归（第 1 阶段用例全部保持）

stdout/stderr/退出码/cwd/env、进程上限、内存上限、kill-on-close、Git Bash/COMSPEC 选择、Linux/macOS 后端不受影响。

---

## 6. Kill criteria

- **AppContainer + handle 继承冲突**：AppContainer 子进程无法继承 stdout 句柄 → 层 2 对该命令回退到层 1 + 层 3（现有路径），并在文档中记录为已知限制。
- **Git Bash 无法在 AppContainer 内启动**（安装目录 ACE 不可写 / 依赖 DLL 不在系统目录）→ 允许为 shell 目录追加只读 ACE；仍失败则回退到 `cmd.exe`，再失败则层 2 fail-open。
- **`CreateAppContainerProfile` 在受限企业策略下失败** → 层 2 跳过，不升级为 probe 失败（否则整台机器失去沙箱）。
- **ACL 授权污染用户目录**（每次 wrap 都改 DACL）→ 只在首次（ACE 不存在时）写一次；同一 SID 的 ACE 幂等，不重复添加。
- 如果层 2 的实施让 `test_sandbox_win.py` 的现有 Windows 用例出现不稳定 → 先保证层 1/3 用例全绿，层 2 用例标记为条件跳过并保留计划。

---

## 7. 文档与收尾

- `wiki/sandbox/README*.md`（四语言）：L2 表的 Windows 列从“待 AppContainer”改为“AppContainer”；新增 profile/ACL/网络 capability 说明；诚实声明里删掉“未实现”，保留“profile 不自动删除”。
- `wiki/sandbox/isolation/README*.md`（四语言）：§2 Windows 小节补第 2 层构造与 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`。
- `AGENTS.md`：Windows 段落补 AppContainer 层、`SHERRY_WIN_SANDBOX_APPCONTAINER`，并把“Windows file confinement is still the tool-level path gate”改为层 2 生效后的表述。
