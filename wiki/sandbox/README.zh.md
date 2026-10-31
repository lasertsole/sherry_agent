# 🛡️ 工具沙箱：terminal、python_repl 与 PTC

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何约束模型发起的代码执行：每次子进程创建前无条件做环境变量清洗，可用时套上操作系统原生沙箱，主动绕过沙箱则需要人工审批。

三个工具让模型能在你的机器上执行代码：`terminal`（shell 命令）、`python_repl`（子进程中的 Python），以及 PTC 的 `execute_code`（子进程中一段更长的 Python 脚本，可经 RPC 桥调用真实工具）。一条幻觉或被注入的命令可能从环境变量里读走 API 密钥、把文件写到项目之外、或者触碰其他进程。沙箱层对这三类风险都有约束。

事实来源：`agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/pub_base/path_utils.py`、`agent/tools/file_tools/`、`agent/tools/terminal/core.py`、`agent/tools/python_repl.py`、`agent/tools/ptc/runner.py`、`agent/middlewares/humanInTheLoop/`、`agent/middlewares/path_guard/`。

## 目录

- [概览与威胁模型](#-概览与威胁模型)
- [隔离能力与优先级](isolation/README.zh.md)
  - [🧱 隔离能力](isolation/README.zh.md#-隔离能力)
  - [📊 优先级矩阵](isolation/README.zh.md#-优先级矩阵)
- [实现与架构](#%EF%B8%8F-实现与架构)
- [配置与使用](#%EF%B8%8F-配置与使用)
- [测试](#-测试)
- [诚实声明与局限](#%EF%B8%8F-诚实声明与局限)

## 🎯 概览与威胁模型

| 风险面 | 没有沙箱时 | 防线 |
| :----- | :--------- | :--- |
| **环境变量中的密钥** | 子进程继承全部变量，包括 `*_API_KEY` | L1 环境变量清洗 |
| **文件系统写入** | 子进程可以写到 agent 用户能写的任何地方 | L2 操作系统沙箱（Linux / macOS） |
| **文件系统读取** | 子进程可以读 `~/.ssh`、`.env`、各类凭据库 | L2 读遮蔽（敏感路径掩蔽，Linux / macOS）+ terminal 敏感文件正则 |
| **文件工具路径参数** | 工具调用让 `read_file` 读取穿越路径或硬拒绝路径 | §5 外部路径门禁 + §6 三道结构门 + §7 `PathGuard`（外部路径仍走 HITL） |
| **Shell 命令参数** | `terminal(commands=…)` / `python_repl(query=…)` 里点名凭据文件 —— 这些不是路径参数，路径门禁看不到 | §7 `PathGuard` 的命令筛查（只挡硬拒绝名单；其余外部路径交给 OS 沙箱） |
| **进程 / 会话作用域** | 子进程共享命名空间，且可能在父进程结束后存活 | L2 `--unshare-all`、`--die-with-parent` |
| **主动绕过** | 模型请求 `sandbox=False` | 人工审批门（HITL）——处于 `full_access` 的会话跳过它 |
| **程序化工具调用（PTC）** | 生成的 `execute_code` 子脚本调用真实工具，可能直接触达文件系统 | L1 环境变量清洗 + L2 操作系统沙箱包装（与 `terminal` / `python_repl` 同一后端）+ 受限内建 + 导入白名单 |

两层防线、一道门，外加文件工具独立的一套路径防御栈：

- **L1. 环境变量清洗**（`scrub_env`）：无条件、在每个子进程创建点执行，即使人工批准了 `sandbox=False` 也不例外。它除了按名称剔除密钥变量，还会以高于自身白名单的优先级**阻止启动钩子变量**（`PYTHONPATH`、`BASH_ENV`、`ENV` 等），使子进程永远不会加载由父环境设置者指定的脚本或模块；加载器变量（`LD_PRELOAD`、`LD_LIBRARY_PATH` 等）仅在 `SHERRY_STRICT_ENV_HIJACK=1` 下阻止——容器运行时会真的设置它们，剔除后所有子进程都起不来。
- **L2. 操作系统原生沙箱**：Linux 用 bubblewrap，macOS 用 Seatbelt——写入围堵之外还有敏感路径读遮蔽（见[§2](isolation/README.zh.md#2-操作系统原生沙箱后端l2)）。Windows 没有操作系统级后端（见[诚实声明与局限](#️-诚实声明与局限)）。
- **人工审批门**：`sandbox=False` 的绕过只可能发生在主会话，且必须经过 HITL 中断审批。
- **PTC（`execute_code`）**：仅 executor 可用且不暴露 `sandbox` 开关，因此始终请求沙箱——子进程 argv 由同一套 L2 后端包装。`SANDBOX_POLICY=required` 拒绝该次运行，`auto` 降级时恰好一条警告（见[隔离 §2](isolation/README.zh.md#2-操作系统原生沙箱后端l2)）。
- **文件工具路径门禁**（[隔离 §5–§7](isolation/README.zh.md#5-外部文件路径门禁文件工具)）：`resolve_project_path()` 的三道结构门与 `O_NOFOLLOW` I/O、虚拟路径回显、搜索 containment、六层外部路径审批流，以及 `PathGuard` 中间件筛选。

## ⚙️ 实现与架构

### 策略：`SandboxPolicy`

从 `SANDBOX_POLICY` 环境变量解析出的三个状态：

| 取值 | 含义 |
| :--- | :--- |
| `required`（出厂默认） | 后端不可用 ⇒ 拒绝命令，绝不无沙箱运行 |
| `auto` | 后端不可用 ⇒ 降级为无沙箱执行并记录一条警告（裸跑，会记入证据账本） |
| `off` | 完全关闭沙箱 |

`read_policy()` 在环境变量未设置时仍回退 `auto`（已在运行的安装保持其既有配置）；但 `.env` 出厂值是 `required` —— 在后端根本无法运行的主机上，`auto` 会把每一条 shell 命令悄悄变成无沙箱执行。这不是假设，而是实测：**内核不支持 user namespace** 的主机（Ubuntu 24.04 默认 AppArmor 策略，或 PRoot/容器主机）里 `bwrap` 二进制在，却不可用。启动时会打印实际生效的策略与后端（`sandbox: policy=… backend=… available=…`），`required` 不可用会以 ERROR 打出修复命令，`SANDBOX_STRICT_BOOT=1` 则直接拒绝启动；运行期可用 `GET /sandbox/status` 查询同一份状态。

`parse_policy` 先去空白、再大小写不敏感匹配，未知取值抛 `ValueError`：安全配置写错了必须大声失败，绝不静默回退。`read_policy()` **每次**调用都执行 `os.getenv`（不做导入期缓存），运行期修改变量立即生效。

### 后端契约与分发

`SandboxBackend` 是所有后端实现的 ABC：

- `probe() -> bool`：绝不抛异常；后端自己捕获探测异常并返回 `False`。
- `wrap(cmd, env, writable_roots) -> (argv, env)`：返回包装后的 argv 与 env，以 list 形式直接 exec（不经 shell）。`writable_roots` 是该命令**唯一**的可写面（即调用会话的项目目录；共享归一化函数会追加 `TEMP_DIR`，因为沙箱自身需要可写临时目录）。该参数**必填**，空列表抛 `ValueError` —— 给它默认值会让任何忘记传参的调用点悄悄退回「整个 `ROOT_DIR` 可写」，而那正是本契约要堵死的写绕过。

`get_backend(policy)` 的分发逻辑：

1. `OFF` 立即返回 `None`：不探测、不导入、不碰子进程。
2. Linux 导入 `BwrapBackend`，macOS 导入 `SeatbeltBackend`（惰性导入；`ImportError` 视为"不可用"，绝不是崩溃）。其余平台（包括 Windows）没有后端。
3. 后端存在但 `probe()` 失败：`REQUIRED` 抛出 `RuntimeError("Required sandbox unavailable on {system}")`；`AUTO` / `OFF` 返回 `None`。

### 工具集成

`SafeShellTool`（名称 `terminal`）与 `TimedPythonREPLTool`（名称 `python_repl`）都在 LLM 可见的工具调用 schema 中暴露 `sandbox: bool = True` 参数，由模型逐次调用时选择。PTC 的 `ExecuteCodeTool`（名称 `execute_code`）完全没有 `sandbox` 参数——它仅归 executor，且始终请求沙箱。

- **沙箱路径**：terminal 走 `backend.wrap(["/bin/sh", "-c", cmd_str], env, roots)`（语义上等价于 POSIX `shell=True`），python_repl 与 PTC 子进程同理。包装后的 argv 以 list 形式 exec，完全不带 shell 参数。`roots` 来自**会话的项目目录**（`current_project_dir(session_id)` / PTC 子进程的 cwd）—— 与命令的 cwd 同源，因此可写面与工作目录永不打架；绑定在仓库之外的项目目录可写（此前是 EROFS），而仓库本身不可写（此前可写，那正是 terminal 绕过外部路径门禁的通道）。
- **裸跑会被记录**：证据账本里的命令行携带 `sandboxed: false`，而沙箱绕过审批卡片会标注 `⚠ UNSANDBOXED` 并说明含义 —— 人工审批时看得到，事后审计也查得到。
- **回退路径（Windows / 无后端）**：terminal 用 `" && "` 拼接命令并以 `shell=True` 启动；python_repl 以 list 形式启动 `[sys.executable, "-c", script]`。Windows **没有**操作系统沙箱后端。
- **所有路径都无条件执行**：`env=scrub_env()` 与 `cwd=str(ROOT_DIR)`（cwd 钳制）。两个工具都强制 30 秒超时（`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`），超时即杀死子进程；PTC 强制执行自己的 `ptc_timeout_seconds` 并对子进程组发 SIGKILL。
- **错误呈现**：`REQUIRED` 且无后端时，terminal 把 `RuntimeError` 包成 `ToolException`（经 `handle_tool_error=True` 原样呈现）；python_repl 直接抛出原始 `RuntimeError`；PTC 在派生子进程之前返回 `status: "sandbox_unavailable"` 信封。
- **降级警告**：当这次调用想要沙箱、但后端不存在且策略不是 `off` 时，工具层记录恰好一条 loguru 警告，然后无沙箱执行：

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`
  - `execute_code: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

  裸跑会被记录：该命令的证据账本行携带 `sandboxed: false`，而沙箱绕过审批卡片会把请求标注为 `⚠ UNSANDBOXED` 并写明含义。于是这个事实在两个方向上都留痕 —— 审批的人看得到，事后审计账本也查得到。

## 🛠️ 配置与使用

### `SANDBOX_POLICY`

```bash
# .env 或 shell 环境变量
SANDBOX_POLICY=auto      # required | auto | off（大小写不敏感，默认：auto）
```

非法取值在首次使用时抛 `ValueError`，而不是静默使用默认值。该变量在每次工具调用时重新读取，可以运行期切换。

### `SANDBOX_STRICT_BOOT`

```bash
# .env 或 shell 环境
SANDBOX_STRICT_BOOT=1    # SANDBOX_POLICY=required 未满足时拒绝启动
```

默认（未设置）只告警，让没有可用沙箱的开发机仍能启动。

### `SANDBOX_READ_ALLOWLIST` / `SANDBOX_READ_EXTRA`

```bash
# .env 或 shell 环境
SANDBOX_READ_ALLOWLIST=1              # 默认关闭：先跑 scripts/sandbox_smoke.py
SANDBOX_READ_EXTRA="~/.cache:~/.local/share"
```

开启后，读面不再是「整个文件系统只读挂载」：沙箱只挂载系统目录（`/usr`、`/bin`、`/lib`、`/etc` …）、解析出的 `writable_roots` 与 `SANDBOX_READ_EXTRA`（默认 `~/.cache`，npm/pip/uv 缓存所在），并用空视图盖掉 `/home` `/root` `/mnt` `/media` `/srv` `/run`（bwrap），或在 Seatbelt 里改为默认拒绝读取、只重开上述路径。**默认关闭**，因为两个后端都从未在「沙箱真能运行」的机器上验证过：请先在目标主机上执行 `uv run --no-sync python scripts/sandbox_smoke.py`（它会探测写面、读面，并在 `SANDBOX_READ_ALLOWLIST=1` 时验证未列入白名单的读取会失败）。不开白名单时，沙箱内仍可读取任意用户文件（只有 5 个凭据路径与 `.env` 文件被遮蔽）。

### `SHERRY_DENY_READ_PATHS`

```bash
# .env 或 shell 环境变量（以 os.pathsep 分隔；~ 会被展开）
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

向两个操作系统后端的读遮蔽清单追加路径（[隔离 §2](isolation/README.zh.md#2-操作系统原生沙箱后端l2) 中的默认清单始终包含）。该变量在每次 `wrap()` 调用时读取。

### 模型看到什么

两个工具都接受逐调用的 `sandbox` 布尔参数，默认 `True`。模型的工具描述会说明：`false` 表示在主会话经人工审批后、以清洗过的环境执行；子代理与后台代理的该请求会被拒绝。

### 用户如何批准或拒绝

主会话（非 YOLO）中模型请求 `sandbox=False` 时，图会停在 `HumanInTheLoop.after_model` 中断上。前端渲染这次动作（工具名、完整参数、命令或 query），并提供两个决定：

- **approve**：以 `{"decisions": [{"type": "approve"}]}` 恢复；调用立即执行（环境已清洗，无操作系统沙箱）。
- **reject**：以 `{"decisions": [{"type": "reject", "message": "..."}]}` 恢复；工具结果变成错误 `ToolMessage`（`User denied: <msg>. <BLOCKED_MESSAGE>`），什么都不会执行。

终端审批卡片只有在后端把 `session` 放进 `allowed_decisions` 时才显示 **本会话允许**：即真正到达卡片的可缓存普通命令。选择后会把命令前两个词的模式写入 session allowlist，后续匹配命令不再弹卡；**approve** 仍只放行本次。危险命令、变更前确认模式和 clawhub 远程执行不提供也不写入 session 缓存；即使之前存过匹配模式，危险命令也会跳过 session allowlist。

## 🧪 测试

| 测试套件 | 覆盖内容 |
| :------- | :------- |
| `tests/agent/tools/test_sandbox_matrix.py` | 14 个测试，逐格覆盖矩阵行为（第 1-5 格每个工具一次，第 6 格四次），包括真实图上的 HITL 中断与"恰好一条警告"的降级断言 |
| `tests/agent/tools/pub_base/test_env_scrub.py` | 清洗规则、优先级、保留/拒绝边界（29 个测试） |
| `tests/agent/tools/pub_base/test_sandbox_policy.py` | 策略解析、严格 `ValueError`、即时读取语义、平台分发 |
| `tests/agent/tools/pub_base/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 构造（含读遮蔽挂载）、探测缓存（子进程全部 mock），以及 1 条可选的真实 bwrap 读遮蔽冒烟测试 |
| `tests/agent/tools/pub_base/test_terminal_tool.py` / `test_python_repl_tool.py` | 工具层守卫（危险命令 / 敏感文件正则）、schema、启动形态、受限内建屏障 |
| `tests/agent/tools/pub_base/test_path_utils.py` | 外部路径流程、三道结构门、符号链接环处理、虚拟路径回显 |
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` / `test_search_bounds.py` | 经 `O_NOFOLLOW` 拒绝符号链接 / TOCTOU、虚拟路径、搜索结果 containment、扫描边界 |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` 筛选：穿越分量、硬拒绝下限、外部路径放行、结构化错误 `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19 个测试，锁定沙箱改造前的 HITL / terminal 遗留行为 |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17 个测试，覆盖绕过审批流、YOLO 直通、作用域标记 |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` 标记 |
| `tests/agent/tools/ptc/test_rpc_auth.py` / `test_sandbox_integration.py` / `test_runner_hardening.py` | PTC RPC 一次性 token 握手；PTC 沙箱策略映射；runner token 生命周期与后端包装 |

矩阵测试全局 patch `subprocess.Popen`，在工具模块接缝处 stub `get_backend`，并通过环境变量设置 `SANDBOX_POLICY`，让真实的 `read_policy` 在每一格中运行。

## ⚠️ 诚实声明与局限

- **bwrap 与 Seatbelt 的构造逻辑只做了单元测试，未在真实 Linux/macOS 机器上验证。** 后端源码的 docstring 明确写了这一点（"仅验证构造逻辑，未在 Linux/macOS 实机验证"）；所有后端测试都 mock 了 subprocess，读遮蔽另有 1 条可选的真实 bwrap 冒烟测试（探测失败即跳过）。可以信任包装出的 argv，但还不构成真实的隔离保证。
- **Windows 没有操作系统沙箱后端。** 那里的防护是环境变量清洗 + cwd 钳制 + 危险命令正则 + 敏感文件正则 + HITL 审批门。没有任何机制阻止写到项目根目录之外，而且**读保护不可用**：没有操作系统后端就没有读遮蔽，应用层正则只是唯一的读取门禁。
- **敏感文件正则是缓解，不是屏障。** 它只匹配字面命令形态；`dd`、`sed`、`python -c "open(…)"`、`$(< file)`、变量与通配符都能绕过。后端存在时，真正的读屏障是操作系统读遮蔽。
- **`python_repl` 没有对应的敏感文件正则。** terminal 专属门禁（[隔离 §3](isolation/README.zh.md#3-危险命令拦截仅-terminal)）不覆盖它；它的包装脚本则限制内建（安全子集省略了 `open` / `__import__`）——这是一道不同且更窄的控制。
- **PTC 继承同一套后端的局限。** `execute_code` 现在会在后端可用时用 L2 后端包装子进程，但在 `SANDBOX_POLICY=auto`（默认）且无可用后端的主机上，仍会在一条警告后无沙箱运行；PTC 传入 `allow_network=True`，bwrap 因此在 `--unshare-all` 旁加上 `--share-net` 以保持 loopback RPC 桥可达；`terminal` 与 `python_repl` 仍使用私有网络命名空间。PTC 的一次性 RPC token 及其 `/proc/<pid>/environ` 残余见 [PTC 页](../ptc/README.zh.md)。
- **降级路径按设计就是无沙箱执行。** `auto` + 无后端 = 记录一条警告，然后照常无沙箱运行（证据账本记为 `sandboxed: false`，启动日志也会点名）。这是"可用性优先于严格性"的有意取舍；出厂默认已改为 `required`，并且 `SANDBOX_STRICT_BOOT=1` 会直接拒绝启动。
- **命令路径筛查是缓解层，不是屏障。** 它只拒绝**字面点名**硬拒路径（凭据/系统文件、YOLO 拒绝名单）的命令，其余一律交给沙箱；`base64 -d | sh`、用碎片拼出的变量、以及 `cd` + 相对路径都天然绕过它。真正的边界是 OS 沙箱。
- **环境变量清洗只看名字。** 存放在不含任何被拦截子串名字下（也不在拒绝名单里）的密钥会原样通过。没有值扫描，也没有动态密钥检测，这是有意为之。
- **不宣称、也未配置任何网络隔离、seccomp 或 AppArmor profile。** 隔离能力就是 [隔离 §2](isolation/README.zh.md#2-操作系统原生沙箱后端l2) 展示的 bwrap / Seatbelt 构造，仅此而已。
