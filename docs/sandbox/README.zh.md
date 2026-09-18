# 🛡️ 工具沙箱：terminal 与 python_repl

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何约束模型发起的命令：每次子进程创建前无条件做环境变量清洗，可用时套上操作系统原生沙箱，主动绕过沙箱则需要人工审批。

两个工具让模型能在你的机器上执行代码：`terminal`（shell 命令）和 `python_repl`（子进程中的 Python）。一条幻觉或被注入的命令可能从环境变量里读走 API 密钥、把文件写到项目之外、或者触碰其他进程。沙箱层对这三类风险都有约束。

事实来源：`agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/pub_base/path_utils.py`、`agent/tools/file_tools/`、`agent/tools/terminal.py`、`agent/tools/python_repl.py`、`agent/middlewares/humanInTheLoop/`、`agent/middlewares/path_guard/`。

## 🎯 概览与威胁模型

| 风险面 | 没有沙箱时 | 防线 |
| :----- | :--------- | :--- |
| **环境变量中的密钥** | 子进程继承全部变量，包括 `*_API_KEY` | L1 环境变量清洗 |
| **文件系统写入** | 子进程可以写到 agent 用户能写的任何地方 | L2 操作系统沙箱（Linux / macOS） |
| **文件系统读取** | 子进程可以读 `~/.ssh`、`.env`、各类凭据库 | L2 读遮蔽（敏感路径掩蔽，Linux / macOS）+ terminal 敏感文件正则 |
| **文件工具路径参数** | 工具调用让 `read_file` 读取穿越路径或硬拒绝路径 | §5 外部路径门禁 + §6 三道结构门 + §7 `PathGuard`（外部路径仍走 HITL） |
| **进程 / 会话作用域** | 子进程共享命名空间，且可能在父进程结束后存活 | L2 `--unshare-all`、`--die-with-parent` |
| **主动绕过** | 模型请求 `sandbox=False` | 人工审批门（HITL） |

两层防线、一道门，外加文件工具独立的一套路径防御栈：

- **L1. 环境变量清洗**（`scrub_env`）：无条件、在每个子进程创建点执行，即使人工批准了 `sandbox=False` 也不例外。
- **L2. 操作系统原生沙箱**：Linux 用 bubblewrap，macOS 用 Seatbelt——写入围堵之外还有敏感路径读遮蔽（见[§2](#2-操作系统原生沙箱后端l2)）。Windows 没有操作系统级后端（见[诚实声明与局限](#️-诚实声明与局限)）。
- **人工审批门**：`sandbox=False` 的绕过只可能发生在主会话，且必须经过 HITL 中断审批。
- **文件工具路径门禁**（§5–§7）：`resolve_project_path()` 的三道结构门与 `O_NOFOLLOW` I/O、虚拟路径回显、搜索 containment、六层外部路径审批流，以及 `PathGuard` 中间件筛选。

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
  --ro-bind /var/empty <敏感目录>             # 读遮蔽：用空目录掩蔽敏感目录
  --ro-bind /dev/null <敏感文件>              # 读遮蔽：掩蔽敏感文件
                                             #（无 /var/empty 时回退为 --tmpfs <路径>）
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 隔离全部命名空间
  --die-with-parent  --new-session
  --clearenv                                 # 清空环境变量，必须在所有 --setenv 之前
  --setenv <K> <V> ...                       # 只重新注入清洗后的变量
  -- /bin/sh -c "<命令>"                      # 被包装的命令
```

`--clearenv` 出现在所有 `--setenv` 之前，两者结合才把清洗后的字典变成真正的环境变量白名单。根文件系统只读；写入只能落在项目根目录和临时目录。

**读遮蔽。** `--ro-bind / /` 只是让"读"处处可行，并不无害：没有掩蔽时模型可以 `cat ~/.ssh/id_rsa`。因此两个后端都会掩蔽一份默认敏感路径清单 `DEFAULT_DENY_READ_PATHS`——`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config/gh`、`~/.docker`——每次调用由 `_sensitive_read_paths()` 解析，并可用环境变量 `SHERRY_DENY_READ_PATHS` 扩展（以 `os.pathsep` 分隔，展开 `~`；空项跳过、顺序保留、去重）。bwrap 的读遮蔽会在每个存在的目录上挂载空目录（对敏感文件则 `--ro-bind /dev/null`）；不存在的路径直接跳过（本来就没有东西可读，而且 bwrap 无法在只读根绑定之下创建挂载点）；主机没有 `/var/empty` 时目录回退为 `--tmpfs <路径>`。遮蔽挂载位于可写绑定**之后**，因此可写的项目根目录永远无法重新暴露被掩蔽的路径。

**macOS：Seatbelt（`sandbox-exec`）**。命令以 `sandbox-exec -p <profile> -- <cmd...>` 运行，profile 如下：

```text
(version 1)
(allow default)
(deny file-write*)
(deny file-read* (subpath "<敏感路径>"))            # 每个敏感路径一条，展开 ~
(deny file-read* (regex #"(^|/)\.env$"))           # 任意深度的 .env / .env.*
(deny file-read* (regex #"(^|/)\.env\."))
(allow file-write* (subpath "<项目根目录>"))
(allow file-write* (subpath "<临时目录>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

顺序即规范：`(allow default)` 之下的 `(deny file-write*)` 表示"除文件写入外全部放行"，随后用显式 allow 重新打开两个可写路径以及 `/dev/null`、`/dev/tty` 两个字面量。读遮蔽的 `deny file-read*` 规则紧跟在 `(deny file-write*)` 之后：每个敏感路径一条 `subpath` 规则（默认清单与 bwrap 相同，同样支持 `SHERRY_DENY_READ_PATHS` 扩展），另加两条正则覆盖任意位置的 `.env` / `.env.*`。不存在的路径照样拒绝——对不存在路径的拒绝无害。路径通过 `json.dumps` 嵌入，路径里的引号或反斜杠无法逃逸成注入的 sbpl 片段。

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

匹配**拼接后**的完整串很关键：按元素精确匹配的黑名单会放过 `["echo ok", "rm -rf /"]`，因为每个元素单独看都无害。命中即抛出 `ToolException("Blocked: unsafe command.")`，经 `handle_tool_error=True` 变成错误工具结果。该拦截与 `sandbox` 取值无关，始终执行。`python_repl` 没有对应的正则；它的包装脚本改用受限内建。

**敏感文件门禁（`_SENSITIVE_FILE_PATTERNS`）。** 在 `_run` 与 `_arun` 两条路径中，`_check_sensitive_file_access(cmd_str)` 都在 `_check_dangerous` **之后**、**任何子进程创建之前**执行：六条编译后的模式任意一条命中拼接后的命令串，就抛出 `ToolException("Blocked: sensitive file access. …")`（`_SENSITIVE_FILE_MESSAGE`）——绝不创建子进程——并提示模型改用 `read_file`（其外部路径会走人工审批）：

| 模式 | 拦截对象 |
| :--- | :------- |
| `(cat\|head\|tail\|less\|more) … /etc/(passwd\|shadow\|sudoers)` | 系统凭据文件 |
| `(cat\|head\|tail) … .env` | `.env` / `.env.*` 读取 |
| `cp … .ssh/` | 把 SSH 材料复制出去 |
| `curl … -d @… .env` | 通过上传外泄 dotenv |
| `(cat\|head\|tail) … ~/.ssh/`、`(cat\|head\|tail) … ~/.aws/` | 家目录凭据库 |

**这是缓解，不是屏障。** `dd`、`sed`、`python -c "open(…)"`、`$(< file)`、shell 变量、通配符与 heredoc（内联文档）都能绕过字面正则——真正的读屏障是上面的 L2 读遮蔽（[§2](#2-操作系统原生沙箱后端l2)）；而已批准的 `sandbox=False` 调用按设计就是无沙箱的。该正则用于拦住明显、常见的尝试，并把模型引导到审批流程。

### 4. 人工审批的绕过通道

`sandbox=False` 的调用是主动绕过请求。在运行 `HumanInTheLoop` 中间件、且未开启 YOLO 的**主会话**图里，`after_model` 会把该调用停在 LangGraph 的 `interrupt()` 上：

- 中断载荷展示完整工具调用（工具名、参数、命令或 query），`allowed_decisions: ["approve", "reject"]`。
- **批准**（`{"decisions": [{"type": "approve"}]}`）：调用按原参数执行。环境变量仍然清洗、cwd 仍然钳制在项目根目录、危险命令正则仍然生效。已批准的绕过会跳过智能审批和危险命令的二次询问，因为人工批准的就是这一次完整调用；硬线黑名单在此之前已经跑过。
- **拒绝**（或没有决定）：结果被替换为内容为 `User denied: <msg>. <BLOCKED_MESSAGE>` 的错误 `ToolMessage`。命令绝不执行，也不会触发第二次中断。`GraphInterrupt` 会被重新抛出，绝不被吞掉。
- **YOLO 模式**（`is_yolo_mode`：`config.yolo_mode`，或 `ApprovalMode.OFF`，或环境变量 `SHERRY_YOLO_MODE` 为 `1` / `true` / `yes`）：跳过中断，调用直接执行（环境变量清洗仍然生效）。
- **后台 / 子代理作用域**：heartbeat 与 cron 工具被标记 `caller_scope="background"`；子代理管线标记 `caller_scope="subagent"`。那些图里没有 HITL 中间件，所以由工具层直接以 `ToolException` 硬拒 `sandbox=False`。那里不存在中断，也不需要。

### 5. 外部文件路径门禁（文件工具）

与上面的 L1/L2 沙箱相互独立，文件工具（`read_file`、`write_file`、`patch_file`、`search_files` 等）的每个路径都经过 `agent/tools/pub_base/path_utils.py::resolve_external_path()`，它按顺序执行六层检查：

1. **位于 `ROOT_DIR` 之内** —— 作为安全路径直接返回。
2. **YOLO 排除列表** —— 安全地板：`~/.ssh/`、`~/.aws/`、`~/.gnupg/`、`~/.config/gcloud/`、`~/.env`、`~/.gitconfig`、`~/.npmrc`、`~/.pypirc`，可通过 `sherry.jsonc` 的 `yolo_deny_paths` 扩展。命中即在此拒绝，后面的任何一层都无法越过：YOLO 模式、allowlist 命中、子代理授权继承都在此止步。
3. **YOLO 模式** —— 全局放行，直接返回路径。
4. **会话 allowlist** —— 精确路径条目与目录条目（以 `/` 结尾，匹配该目录及其全部后代）仅在本会话有效，并由子代理继承。
5. **未获授权的子代理** —— 拒绝：子代理永远不能自行批准新路径。
6. **主会话** —— HITL 中断，`allowed_decisions: ["approve", "approve_dir", "yolo", "reject"]`：
   - `approve` —— 仅允许该文件（本会话有效，子代理继承）；
   - `approve_dir` —— 允许该文件所在的整个目录（本会话内前缀匹配，子代理同样继承）；
   - `yolo` —— 永久允许所有外部路径；
   - `reject` —— 拒绝本次访问。

在 `resolve_project_path()`（ROOT_DIR 一侧的流程）内部，路径在任何文件 I/O 之前要过三道结构门，随后每次打开都拒绝符号链接作为路径最后一段（`O_NOFOLLOW`）；该模块还把模型可见路径统一渲染为不含 `ROOT_DIR` 的虚拟路径。这些机制与搜索 containment 过滤详见 §6；在任何工具执行前筛选路径参数的 `PathGuard` 中间件见 §7。

### 6. 文件工具路径门禁：三道结构门、no-follow I/O 与虚拟路径

文件工具不依赖沙箱进程：每个项目路径都由 `agent/tools/pub_base/path_utils.py` 在进程内解析。`resolve_project_path()` 在工具触碰文件系统之前按顺序执行三道门；一旦它拒绝某个路径，工具的 `except PathOutOfBoundsError` 分支就会把它改道到 §5 的外部路径 HITL 流程。

1. **字符串级拒绝（`_reject_traversal_input`）。** `~` 前缀或任何 `..` 分量都会在任何文件系统访问之前被 `PathOutOfBoundsError` 拒绝。检查按分量进行（`Path(file_path).parts`），刻意不做子串判定：子串判定会误伤 `foo..bar`、`配置..md` 这类合法名称。
2. **Containment（越界拦截）。** 相对路径先拼到 `ROOT_DIR`（`~` 提前展开），再执行 `resolve()`；`resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR)` 即抛 `PathOutOfBoundsError`。`ROOT_DIR` 本身允许。
3. **符号链接环检测（`_raise_if_symlink_loop`）。** `Path.resolve()` 遇到符号链接环会静默停下并把环链接本身返回；若解析结果是符号链接，`stat()` 会把这种情况映射为 `OSError(ELOOP)`（Linux/macOS，或 Windows `winerror` 1921）并重新抛出，而不是留到后续以令人困惑的方式失败。

**No-follow I/O（`_open_no_follow`）。** 每次读写都经 `os.open(path, flags | O_NOFOLLOW, mode)` 打开，因此路径最后一段不允许是符号链接：在校验与打开之间被换入的链接无法把 I/O 重定向到 `ROOT_DIR` 之外——TOCTOU 窗口就此闭合。拒绝时抛 `OSError(ELOOP)`，与 Linux/macOS 原生错误码一致；Windows 没有 `O_NOFOLLOW`，辅助函数回退为显式的 `path.is_symlink()` 检查并抛出同一错误。`read_file`（读）、`write_file`（写，以及 `.py` 追加/格式化流程中的读）与 `patch_file`（读 + 写）都走这条路径。

**虚拟路径回显。** `to_virtual_path()` 把 `ROOT_DIR` 之下的真实路径映射为虚拟路径（`/src/main.py`）。`display_path()` 正常时返回该虚拟路径；目标在根之外或无法解析（捕获 `ValueError` / `OSError` / `RuntimeError`）时回退为 `real_path.name or "/"`——因此 `ROOT_DIR` 绝不泄漏。`safe_error_detail()` 只返回 `OSError.strerror`（如 `Permission denied`）或 `UnicodeDecodeError.reason`（`invalid start byte`）；其他异常的文本被刻意丢弃，因为通用异常文本可能内嵌真实根路径（例如 `Path.rglob` 中途抛出的错误），只保留异常类型名。净效果：模型可见的结果与错误信息都不含真实项目根路径。

**搜索 containment（`_stays_within_root`）。** `os.walk` 不会进入目录符号链接，但文件符号链接仍会出现在列表里。两种搜索模式都用 `_stays_within_root(candidate, root)`（`candidate.resolve().relative_to(root.resolve())`，遇 `ValueError` / `OSError` / `RuntimeError` 即跳过）过滤每个命中，因此经符号链接解析到搜索树之外的文件绝不会返回——指向 `/etc/passwd` 的文件符号链接会被跳过。搜索根本身始终是已解析路径（项目内搜索还额外受 `ROOT_DIR` 约束），所以已获批准的外部目录搜索仍可正常工作。

**扫描边界。** 两种模式还通过 `TOOLS_TIMEOUTS`（`config/features/agent_side/tools_timeouts.py`）限制扫描本身：`file_tools_search_time_budget_s`（默认 5.0 秒）到期即停止遍历，`file_tools_search_max_matches`（默认 10,000）限制收集到的命中数，`file_tools_search_prune_dirs`（默认 `proc`、`sys`、`dev`）在 `dirnames[:]` 中被过滤，伪文件系统绝不会被进入。被截断的扫描绝不静默：JSON 结果会带上 `scan_truncated: true`、`scan_stop_reason`（`time_budget` / `max_matches`）与提示；剪枝发生时另有 `pruned_dir_count`。文件名匹配走 `fnmatch`（不支持花括号展开），因此无需展开数上限。

**与 deepagents 参考实现的设计差异。** 参考实现把每个路径锚定到虚拟命名空间（`virtual_mode`），使穿越在设计上不可能；Sherry 则保留真实文件系统路径——`prompt_builder`、技能工具与 terminal 的 cwd 都依赖它们——改在解析**之后**做 containment（上文三道门），并用 `O_NOFOLLOW` 关闭 TOCTOU。其 `BackendProtocol`、`CompositeBackend`、`StateBackend` 与完整的虚拟路径命名空间被刻意弃用：那是架构重写，而 Sherry 没有多后端场景。

### 7. `PathGuard` 中间件

**模块：** `agent/middlewares/path_guard/core.py` · **类：** `PathGuard(AgentMiddleware)` · **钩子：** 仅 `wrap_tool_call` / `awrap_tool_call`

文件工具的门禁只能保护真正走到它们的调用；`PathGuard` 是注册在主 Agent 链中 `ToolCallNormalize` 正后方的调用点筛选器（`agent/core.py`）。由于列表顺序即 wrap 钩子的外层顺序，它运行在 `ToolGuardrails` **之内**（`IterationBudget` → `ToolGuardrails` → `PathGuard` → 工具）：拒绝会作为普通错误 `ToolMessage` 交给 ToolGuardrails 评估，与其他工具失败一视同仁。worker / 子代理链不注册它——子工具保有自己的门禁，且子代理的外部访问本就硬拒绝。

筛选刻意保守：

- 只检查参数名 `file_path` / `path` / `directory` / `dir` 的字符串值；形如 `scheme://` 的 URL 跳过，非路径语义不会被误读为文件系统路径；
- `..` 穿越分量用共享的 `has_traversal_component` 判定拒绝——先做 URL 解码与反斜杠规范化，`%2e%2e` 与 `..\` 无法溜过；纯点分量（`...`）同样算穿越；
- 能被 `resolve_project_path()` 接受的路径原样放行；
- 解析到 `ROOT_DIR` 之外的值**除非**命中硬拒绝下限，否则放行：`_SYSTEM_DENY_PATHS`（`/etc/passwd`、`/etc/shadow`、`/etc/sudoers`）或 YOLO 排除列表（`_is_yolo_denied`）；
- 其余外部路径交给工具自身的 `resolve_external_path()` HITL 流程——中间件从不改写参数、从不触发中断，因此一次调用只产生一次审批决定（工具在执行时还会再跑同一道门；在这里拦截等于做两次决定）；
- 缺失 / 无法解析的目标与未知异常类都放行给工具，由工具负责自己的错误呈现。

拒绝时 `PathGuard` 记录一条警告，并返回结构化错误 `ToolMessage`（`status="error"`，保留原 `tool_call_id` 与工具名），不执行工具。

**第二道防线。** 四个文件工具都保留自己的 `resolve_project_path()` / `resolve_external_path()` 调用，代码中标记为 `# redundant: path_guard middleware handles this — kept as the second line of defense`（`read_file`、`write_file`、`patch_file`、`search_files`）。中间件是外层筛选器，用于兜住可能忘记自检的工具；每工具门禁仍是权威，外部路径依旧走人工审批流程。中间件侧的细节见 [Middlewares README §PathGuard](../../agent/middlewares/README.zh.md#pathguard)。

### 8. 工具结果与人类消息的体积治理

沙箱约束子进程**能做什么**；一个配套层约束工具结果**能携带多少**。`ContextEvictionMiddleware`（主 Agent 管线）把超过 20 000 字符的通用工具结果卸载到 `SESSIONS_DIR/<session_id>/evicted/`，在 graph state 中只留 head/tail 预览与 `read_file` 指针；把 `read_file` 输出切片为前 4 000 字符；并把超长（> 200 000 字符）的尾部人类消息卸载到同一目录 —— state 保留全文，只截断模型视图。P1-2 溢出尾部裁剪随后在不调 LLM 的情况下把尾部工具结果替换为 stub，而每份载荷都能从 MesMemory 或驱逐文件恢复。完整细节：[上下文治理](../context-governance/README.zh.md)。

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
- **回退路径（Windows / 无后端）**：terminal 用 `" && "` 拼接命令并以 `shell=True` 启动；python_repl 以 list 形式启动 `[sys.executable, "-c", script]`。Windows **没有**操作系统沙箱后端。
- **所有路径都无条件执行**：`env=scrub_env()` 与 `cwd=str(ROOT_DIR)`（cwd 钳制）。两个工具都强制 30 秒超时（`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`），超时即杀死子进程。
- **错误呈现**：`REQUIRED` 且无后端时，terminal 把 `RuntimeError` 包成 `ToolException`（经 `handle_tool_error=True` 原样呈现）；python_repl 直接抛出原始 `RuntimeError`。
- **降级警告**：当这次调用想要沙箱、但后端不存在且策略不是 `off` 时，工具层记录恰好一条 loguru 警告，然后无沙箱执行：

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 📊 优先级矩阵

权威表格来自 `agent/tools/pub_base/sandbox.py`，由 `tests/agent/tools/test_sandbox_matrix.py` 逐格测试：

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

### `SHERRY_DENY_READ_PATHS`

```bash
# .env 或 shell 环境变量（以 os.pathsep 分隔；~ 会被展开）
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

向两个操作系统后端的读遮蔽清单追加路径（上面的默认清单始终包含）。该变量在每次 `wrap()` 调用时读取。

### 模型看到什么

两个工具都接受逐调用的 `sandbox` 布尔参数，默认 `True`。模型的工具描述会说明：`false` 表示在主会话经人工审批后、以清洗过的环境执行；子代理与后台代理的该请求会被拒绝。

### 用户如何批准或拒绝

主会话（非 YOLO）中模型请求 `sandbox=False` 时，图会停在 `HumanInTheLoop.after_model` 中断上。前端渲染这次动作（工具名、完整参数、命令或 query），并提供两个决定：

- **approve**：以 `{"decisions": [{"type": "approve"}]}` 恢复；调用立即执行（环境已清洗，无操作系统沙箱）。
- **reject**：以 `{"decisions": [{"type": "reject", "message": "..."}]}` 恢复；工具结果变成错误 `ToolMessage`（`User denied: <msg>. <BLOCKED_MESSAGE>`），什么都不会执行。

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

矩阵测试全局 patch `subprocess.Popen`，在工具模块接缝处 stub `get_backend`，并通过环境变量设置 `SANDBOX_POLICY`，让真实的 `read_policy` 在每一格中运行。

## ⚠️ 诚实声明与局限

- **bwrap 与 Seatbelt 的构造逻辑只做了单元测试，未在真实 Linux/macOS 机器上验证。** 后端源码的 docstring 明确写了这一点（"仅验证构造逻辑，未在 Linux/macOS 实机验证"）；所有后端测试都 mock 了 subprocess，读遮蔽另有 1 条可选的真实 bwrap 冒烟测试（探测失败即跳过）。可以信任包装出的 argv，但还不构成真实的隔离保证。
- **Windows 没有操作系统沙箱后端。** 那里的防护是环境变量清洗 + cwd 钳制 + 危险命令正则 + 敏感文件正则 + HITL 审批门。没有任何机制阻止写到项目根目录之外，而且**读保护不可用**：没有操作系统后端就没有读遮蔽，应用层正则只是唯一的读取门禁。
- **敏感文件正则是缓解，不是屏障。** 它只匹配字面命令形态；`dd`、`sed`、`python -c "open(…)"`、`$(< file)`、变量与通配符都能绕过。后端存在时，真正的读屏障是操作系统读遮蔽。
- **`python_repl` 没有对应的敏感文件正则。** 上面的 terminal 专属门禁不覆盖它；它的包装脚本则限制内建（安全子集省略了 `open` / `__import__`）——这是一道不同且更窄的控制。
- **降级路径按设计就是无沙箱执行。** `auto` + 无后端 = 记录一条警告，然后照常无沙箱运行。这是"可用性优先于严格性"的有意取舍；需要相反语义请选 `SANDBOX_POLICY=required`。
- **环境变量清洗只看名字。** 存放在不含任何被拦截子串名字下（也不在拒绝名单里）的密钥会原样通过。没有值扫描，也没有动态密钥检测，这是有意为之。
- **不宣称、也未配置任何网络隔离、seccomp 或 AppArmor profile。** 隔离能力就是上文展示的 bwrap / Seatbelt 构造，仅此而已。
