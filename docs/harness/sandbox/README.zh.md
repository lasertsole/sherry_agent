# 🛡️ 工具沙箱：terminal 与 python_repl

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何约束模型发起的命令：每次子进程创建前无条件做环境变量清洗，可用时套上操作系统原生沙箱，主动绕过沙箱则需要人工审批。

两个工具让模型能在你的机器上执行代码：`terminal`（shell 命令）和 `python_repl`（子进程中的 Python）。一条幻觉或被注入的命令可能从环境变量里读走 API 密钥、把文件写到项目之外、或者触碰其他进程。沙箱层对这三类风险都有约束。

事实来源：`agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/terminal.py`、`agent/tools/python_repl.py`、`agent/middlewares/humanInTheLoop/`。

## 🎯 概览与威胁模型

| 风险面 | 没有沙箱时 | 防线 |
| :----- | :--------- | :--- |
| **环境变量中的密钥** | 子进程继承全部变量，包括 `*_API_KEY` | L1 环境变量清洗 |
| **文件系统写入** | 子进程可以写到 agent 用户能写的任何地方 | L2 操作系统沙箱（Linux / macOS） |
| **进程 / 会话作用域** | 子进程共享命名空间，且可能在父进程结束后存活 | L2 `--unshare-all`、`--die-with-parent` |
| **主动绕过** | 模型请求 `sandbox=False` | 人工审批门（HITL） |

两层防线加一道门：

- **L1. 环境变量清洗**（`scrub_env`）：无条件、在每个子进程创建点执行，即使人工批准了 `sandbox=False` 也不例外。
- **L2. 操作系统原生沙箱**：Linux 用 bubblewrap，macOS 用 Seatbelt。Windows 没有操作系统级后端（见[诚实声明与局限](#️-诚实声明与局限)）。
- **人工审批门**：`sandbox=False` 的绕过只可能发生在主会话，且必须经过 HITL 中断审批。

## 🧱 隔离能力

### 1. 环境变量清洗（`scrub_env`），L1，无条件

`scrub_env(base_env=None)` 为每个子进程构造安全的环境字典。它是纯函数（只依赖 `os` / `re`，无 IO、无日志），从不修改入参；只检查变量**名**，从不检查值。两个工具的同步与异步创建点都会执行，即使人工批准了 `sandbox=False` 的调用也会执行。

| 规则类别 | 匹配规则 | 结果 | 示例 |
| :------- | :------- | :--- | :--- |
| **按精确名保留** | 变量名精确匹配（大小写不敏感） | 保留，优先级高于一切拒绝规则 | `PATH`、`HOME`、`USER`、`USERNAME`、`LANG`、`TERM`、`TMPDIR`、`TMP`、`TEMP`、`SHELL`、`LOGNAME`、`PYTHONPATH`、`PYTHONUTF8`、`VIRTUAL_ENV`、`COMPUTERNAME`、`SYSTEMROOT`、`SYSTEMDRIVE`、`WINDIR`、`COMSPEC`、`PATHEXT`、`OS`、`PROCESSOR_ARCHITECTURE`、`NUMBER_OF_PROCESSORS`、`APPDATA`、`LOCALAPPDATA`、`USERPROFILE`、`HOMEDRIVE`、`HOMEPATH` |
| **按前缀保留** | 变量名以 `LC_`、`XDG_` 或 `CONDA` 开头 | 保留，优先级高于一切拒绝规则 | `LC_ALL`、`XDG_CONFIG_HOME`、`CONDA_TOKEN` |
| **强制拒绝（项目密钥）** | 变量名精确匹配（大小写不敏感） | 一律丢弃 | `MAIN_LLM_API_KEY`、`REASONER_LLM_API_KEY`、`AUXILIARY_LLM_API_KEY`、`TAVILY_API_KEY`、`LANGSMITH_API_KEY`、`ITTT_API_KEY`、`VTTT_API_KEY`、`TTI_API_KEY`、`RERANKER_API_KEY`、`EMBEDDING_API_KEY`、`STT_API_KEY` |
| **子串拦截** | 变量名包含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL`、`PASSWD`、`AUTH`、`DSN`、`WEBHOOK`、`BEARER`、`APIKEY` 之一（大小写不敏感） | 丢弃 | `MY_CUSTOM_TOKEN`、`AWS_SECRET_ACCESS_KEY` |
| **原样放行** | 不匹配任何规则 | 原样保留 | `EDITOR`、`GIT_AUTHOR_NAME` |

- **优先级**：保留（精确 / 前缀）> 强制拒绝 > 子串拦截。`CONDA_TOKEN` 虽然包含 `TOKEN`，但靠前缀保留规则幸存；仅仅*包含* `PATH` 的名字（如 `KEY_PATH_DELIM`）不算保留名，会被 `KEY` 子串规则拦截。
- 这**不是白名单**：不匹配任何规则的变量原样放行（纯白名单模式会因为丢掉 `PATH` 而弄坏子进程）。
- 被过滤的变量名从不写日志，密钥名不会泄漏到日志里。

### 2. 操作系统原生沙箱后端（L2）

**Linux：bubblewrap（`bwrap`）**。命令被包进一个 list-exec 形式的 argv，参数顺序是承重结构：

```text
bwrap
  --ro-bind / /                              # 整个根文件系统：只读
  --bind <项目根目录> <项目根目录>             # 唯一可写的位置：
  --bind <临时目录> <临时目录>                 # 项目根目录 + 临时目录（相同则去重）
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 隔离全部命名空间
  --die-with-parent  --new-session
  --clearenv                                 # 清空环境变量，必须在所有 --setenv 之前
  --setenv <K> <V> ...                       # 只重新注入清洗后的变量
  -- /bin/sh -c "<命令>"                      # 被包装的命令
```

`--clearenv` 出现在所有 `--setenv` 之前，两者结合才把清洗后的字典变成真正的环境变量白名单。根文件系统只读；写入只能落在项目根目录和临时目录。

**macOS：Seatbelt（`sandbox-exec`）**。命令以 `sandbox-exec -p <profile> -- <cmd...>` 运行，profile 如下：

```text
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "<项目根目录>"))
(allow file-write* (subpath "<临时目录>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

顺序即规范：`(allow default)` 之下的 `(deny file-write*)` 表示"除文件写入外全部放行"，随后用显式 allow 重新打开两个可写路径以及 `/dev/null`、`/dev/tty` 两个字面量。路径通过 `json.dumps` 嵌入，路径里的引号或反斜杠无法逃逸成注入的 sbpl 片段。

**探测（可用性检查）**。两个后端都实现 `probe() -> bool`，带类级缓存（每个进程只探测一次，失败结果同样缓存）：

- `BwrapBackend.probe()`：以 3 秒超时冒烟运行 `bwrap --ro-bind / / --proc /proc --dev /dev true`。二进制存在并不代表可用；Ubuntu 24.04+ 的 AppArmor 非特权 user namespace 限制可以在 uid-map 阶段杀死所有 bwrap，所以真实的冒烟运行才是诚实的检查。
- `SeatbeltBackend.probe()`：仅 `shutil.which("sandbox-exec")`；sbpl 没有基于退出码的冒烟探测可用。

### 3. 危险命令拦截（仅 terminal）

`DANGEROUS_COMMAND_REGEX` 是含 6 个分支的黑名单正则，以 `re.IGNORECASE` 匹配用 `" && "` 拼接后的完整命令串，在任何子进程创建之前执行：

| # | 模式意图 | 命中示例 |
| :- | :------- | :------- |
| 1 | 递归/强制的 `rm` 指向 `/` 或 `~` | `rm -rf /`、`rm -fr ~` |
| 2 | 任何递归 `rm` | `rm -r build/` |
| 3 | `mkfs` | 文件系统格式化 |
| 4 | `shutdown` | 关机 |
| 5 | `reboot` | 重启 |
| 6 | `|`、`&&` 或 `;` 后跟 `rm` / `shutdown` / `reboot` / `mkfs` | 链式变体，如 `echo ok && rm -rf /` |

匹配**拼接后**的完整串很关键：旧的按元素精确匹配的黑名单放过过 `["echo ok", "rm -rf /"]`，因为每个元素单独看都无害。命中即抛出 `ToolException("Blocked: unsafe command.")`，经 `handle_tool_error=True` 变成错误工具结果。该拦截与 `sandbox` 取值无关，始终执行。`python_repl` 没有对应的正则；它的包装脚本改用受限内建。

### 4. 人工审批的绕过通道

`sandbox=False` 的调用是主动绕过请求。在运行 `HumanInTheLoop` 中间件、且未开启 YOLO 的**主会话**图里，`after_model` 会把该调用停在 LangGraph 的 `interrupt()` 上：

- 中断载荷展示完整工具调用（工具名、参数、命令或 query），`allowed_decisions: ["approve", "reject"]`。
- **批准**（`{"decisions": [{"type": "approve"}]}`）：调用按原参数执行。环境变量仍然清洗、cwd 仍然钳制在项目根目录、危险命令正则仍然生效。已批准的绕过会跳过智能审批和危险命令的二次询问，因为人工批准的就是这一次完整调用；硬线黑名单在此之前已经跑过。
- **拒绝**（或没有决定）：结果被替换为内容为 `User denied: <msg>. <BLOCKED_MESSAGE>` 的错误 `ToolMessage`。命令绝不执行，也不会触发第二次中断。`GraphInterrupt` 会被重新抛出，绝不被吞掉。
- **YOLO 模式**（`is_yolo_mode`：`config.yolo_mode`，或 `ApprovalMode.OFF`，或环境变量 `SHERRY_YOLO_MODE` 为 `1` / `true` / `yes`）：跳过中断，调用直接执行（环境变量清洗仍然生效）。
- **后台 / 子代理作用域**：heartbeat 与 cron 工具被标记 `caller_scope="background"`；子代理管线标记 `caller_scope="subagent"`。那些图里没有 HITL 中间件，所以由工具层直接以 `ToolException` 硬拒 `sandbox=False`。那里不存在中断，也不需要。

## ⚙️ 实现与架构

### 策略：`SandboxPolicy`

从 `SANDBOX_POLICY` 环境变量解析出的三个状态：

| 取值 | 含义 |
| :--- | :--- |
| `required` | 后端不可用 ⇒ 拒绝命令，绝不无沙箱运行 |
| `auto`（默认） | 后端不可用 ⇒ 降级为无沙箱执行并记录一条警告 |
| `off` | 完全关闭沙箱 |

`parse_policy` 先去空白、再大小写不敏感匹配，未知取值抛 `ValueError`：安全配置写错了必须大声失败，绝不静默回退。`read_policy()` **每次**调用都执行 `os.getenv`（不做导入期缓存），运行期修改变量立即生效。

### 后端契约与分发

`SandboxBackend` 是所有后端实现的 ABC：

- `probe() -> bool`：绝不抛异常；后端自己捕获探测异常并返回 `False`。
- `wrap(cmd, env) -> (argv, env)`：返回包装后的 argv 与 env，以 list 形式直接 exec（不经 shell）。

`get_backend(policy)` 的分发逻辑：

1. `OFF` 立即返回 `None`：不探测、不导入、不碰子进程。
2. Linux 导入 `BwrapBackend`，macOS 导入 `SeatbeltBackend`（惰性导入；`ImportError` 视为"不可用"，绝不是崩溃）。其余平台（包括 Windows）没有后端。
3. 后端存在但 `probe()` 失败：`REQUIRED` 抛出 `RuntimeError("Required sandbox unavailable on {system}")`；`AUTO` / `OFF` 返回 `None`。

### 工具集成

`SafeShellTool`（名称 `terminal`）与 `TimedPythonREPLTool`（名称 `python_repl`）都在 LLM 可见的工具调用 schema 中暴露 `sandbox: bool = True` 参数，由模型逐次调用时选择。

- **沙箱路径**：terminal 走 `backend.wrap(["/bin/sh", "-c", cmd_str], env)`（语义上等价于 POSIX `shell=True`），python_repl 走 `backend.wrap([sys.executable, "-c", script], env)`。包装后的 argv 以 list 形式 exec，完全不带 shell 参数。
- **回退路径（Windows / 无后端）**：保持与原来逐字节一致的构造，只新增 `env=`。terminal 用 `" && "` 拼接命令并以 `shell=True` 启动；python_repl 以 list 形式启动 `[sys.executable, "-c", script]`。Windows **没有**操作系统沙箱后端。
- **所有路径都无条件执行**：`env=scrub_env()` 与 `cwd=str(ROOT_DIR)`（cwd 钳制）。两个工具都强制 30 秒超时（`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`），超时即杀死子进程。
- **错误呈现**：`REQUIRED` 且无后端时，terminal 把 `RuntimeError` 包成 `ToolException`（经 `handle_tool_error=True` 原样呈现）；python_repl 直接抛出原始 `RuntimeError`。
- **降级警告**：当这次调用想要沙箱、但后端不存在且策略不是 `off` 时，工具层记录恰好一条 loguru 警告，然后无沙箱执行：

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 📊 优先级矩阵

权威表格来自 `agent/tools/pub_base/sandbox.py`，由 `tests/integration/test_sandbox_matrix.py` 逐格测试：

| # | 策略 | `sandbox` 标志 | 后端可用？ | 调用方作用域 | 结果 |
| :- | :--- | :------------- | :--------- | :----------- | :--- |
| 1 | `required` | `True` | 是 | 任意 | 在后端包装内执行（list-exec，环境已清洗） |
| 2 | `required` | `True` | 否 | 任意 | `RuntimeError` / 工具错误；不产生任何子进程 |
| 3 | `required` | `False` | （不查询） | 任意 | 工具层 `ToolException`，绝不是 `GraphInterrupt`；不产生子进程 |
| 4 | `auto` | `False` | （不查询） | 主会话，非 YOLO | HITL 中断：批准 → 执行（仍清洗），拒绝 → 错误 `ToolMessage` |
| 5 | `auto` | `True` | 否 | 任意 | 降级：直接无沙箱执行，恰好一条警告，环境仍然清洗 |
| 6 | `off` | `True` / `False` | 从不探测 | 主会话 | 无沙箱、无审批、无警告；直接执行 |

补充说明：

- `auto` + `True` + 后端可用的行为与第 1 格相同：经后端包装执行。
- 调用方作用域守卫是策略处理之前的工具层检查：任何非主会话作用域（`subagent`、`background`）请求 `sandbox=False` 都会在所有策略下被 `ToolException` 硬拒，因为那些图里不存在审批中断。因此第 4 格的中断只对主会话调用触发。

## 🛠️ 配置与使用

### `SANDBOX_POLICY`

```bash
# .env 或 shell 环境变量
SANDBOX_POLICY=auto      # required | auto | off（大小写不敏感，默认：auto）
```

非法取值在首次使用时抛 `ValueError`，而不是静默使用默认值。该变量在每次工具调用时重新读取，可以运行期切换。

### 模型看到什么

两个工具都接受逐调用的 `sandbox` 布尔参数，默认 `True`。模型的工具描述会说明：`false` 表示在主会话经人工审批后、以清洗过的环境执行；子代理与后台代理的该请求会被拒绝。

### 用户如何批准或拒绝

主会话（非 YOLO）中模型请求 `sandbox=False` 时，图会停在 `HumanInTheLoop.after_model` 中断上。前端渲染这次动作（工具名、完整参数、命令或 query），并提供两个决定：

- **approve**：以 `{"decisions": [{"type": "approve"}]}` 恢复；调用立即执行（环境已清洗，无操作系统沙箱）。
- **reject**：以 `{"decisions": [{"type": "reject", "message": "..."}]}` 恢复；工具结果变成错误 `ToolMessage`（`User denied: <msg>. <BLOCKED_MESSAGE>`），什么都不会执行。

## 🧪 测试

| 测试套件 | 覆盖内容 |
| :------- | :------- |
| `tests/integration/test_sandbox_matrix.py` | 14 个测试，逐格覆盖矩阵行为（第 1-5 格每个工具一次，第 6 格四次），包括真实图上的 HITL 中断与"恰好一条警告"的降级断言 |
| `tests/module/test_env_scrub.py` | 清洗规则、优先级、保留/拒绝边界（29 个测试） |
| `tests/module/test_sandbox_policy.py` | 策略解析、严格 `ValueError`、即时读取语义、平台分发 |
| `tests/module/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 构造、探测缓存（子进程全部 mock） |
| `tests/module/test_terminal_tool.py` / `test_python_repl_tool.py` | 工具层守卫、schema、启动形态 |
| `tests/module/test_hitl_characterization.py` | 19 个测试，锁定沙箱改造前的 HITL / terminal 遗留行为 |
| `tests/module/test_hitl_sandbox_bypass.py` | 17 个测试，覆盖绕过审批流、YOLO 直通、作用域标记 |
| `tests/unit/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` 标记 |

矩阵测试全局 patch `subprocess.Popen`，在工具模块接缝处 stub `get_backend`，并通过环境变量设置 `SANDBOX_POLICY`，让真实的 `read_policy` 在每一格中运行。

## ⚠️ 诚实声明与局限

- **bwrap 与 Seatbelt 的构造逻辑只做了单元测试，未在真实 Linux/macOS 机器上验证。** 后端源码的 docstring 明确写了这一点（"仅验证构造逻辑，未在 Linux/macOS 实机验证"）；所有后端测试都 mock 了 subprocess。可以信任包装出的 argv，但还不构成真实的隔离保证。
- **Windows 没有操作系统沙箱后端。** 那里的防护是环境变量清洗 + cwd 钳制 + 危险命令正则 + HITL 审批门。没有任何机制阻止写到项目根目录之外。
- **降级路径按设计就是无沙箱执行。** `auto` + 无后端 = 记录一条警告，然后照常无沙箱运行。这是"可用性优先于严格性"的有意取舍；需要相反语义请选 `SANDBOX_POLICY=required`。
- **环境变量清洗只看名字。** 存放在不含任何被拦截子串名字下（也不在拒绝名单里）的密钥会原样通过。没有值扫描，也没有动态密钥检测，这是有意为之。
- **不宣称、也未配置任何网络隔离、seccomp 或 AppArmor profile。** 隔离能力就是上文展示的 bwrap / Seatbelt 构造，仅此而已。
