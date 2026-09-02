# sherry_agent 沙箱实现方案

> 参考项目：opencode-dev (D:\selfProj\opencode-dev) + oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev) + hermes-agent-main (D:\selfProj\hermes-agent-main)
> 日期：2026-09-02

---

## 设计哲学

```
hermes-agent:     多后端完整架构（Docker/Modal/Daytona/SSH/Singularity）  → 过重 ❌
oh-my-openagent:  OS 原生沙箱（bwrap/sandbox-exec）+ 进程级               → 参考 ✅
sherry_agent:     进程级修复 + OS 原生沙箱（平台条件启用）               → 折中 ✅
```

核心原则：

1. **同工具不同参数**：terminal 和 python_repl 复用现有工具，仅增加 `sandbox: bool=True` 参数。默认有沙箱，`sandbox=False` 时触发 HITL 用户确认
2. **绕过沙箱需 HITL**：agent 想要不沙箱来执行，得通过 HITL 要用户确认
3. **纵深防御**：即使无 OS 沙箱（Windows），仍有 env scrub + 黑名单 + cwd 钳制

---

## 参考来源映射

| sherry_agent 组件     | 参考来源                                                          | 取什么 / 不取什么                                                                        |
| --------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `env_scrub.py`        | hermes-agent `code_execution_tool.py:_scrub_child_env()`          | 取：密钥子串拦截 + 安全前缀允许 + Windows 必需变量；不取：HERMES_ 专属变量、动态密钥检测 |
| `sandbox.py`          | oh-my-openagent `sandbox-contracts.ts` + `sandbox-platform.ts`    | 取：SandboxPolicy 三态 + 后端选择逻辑；不取：lockPaths/facts/memorian 等专用概念         |
| `sandbox_bwrap.py`    | oh-my-openagent `sandbox-platform.ts:buildPathSandboxTransform()` | 取：bwrap 参数模式 + 烟雾测试；不取：TypeScript 泛型                                     |
| `sandbox_seatbelt.py` | oh-my-openagent `sandbox-platform.ts:buildDarwinProfile()`        | 取：Seatbelt profile 格式；不取：lockPaths                                               |
| `terminal.py` 修改    | hermes-agent `local.py:_sanitize_subprocess_env()`                | 取：env 清洗集成；不取：session snapshot 机制                                            |
| `core.py` 修改        | 复用 sherry_agent 现有 HITL `interrupt()` 机制                    | 取：interrupt + GraphInterrupt 传播；不额外引入新机制                                    |

---

## 执行流程

### 默认路径（sandbox=True）

```
LLM 调用 terminal(commands="ls -la")
  → HITL after_model: sandbox 参数 = True (默认)
    → 跳过沙箱绕过审批
    → 进入现有命令审批管道 (危险模式检测)
  → 工具执行: sandbox_backend.wrap(bash -c "ls -la")
    → Linux: bwrap --ro-bind / / --bind ROOT_DIR ROOT_DIR -- bash -c "ls -la"
    → macOS: sandbox-exec -p profile -- bash -c "ls -la"
    → Windows: bash -c "ls -la" (仅进程级隔离 + env scrub)
```

### 沙箱绕过路径（sandbox=False）

```
LLM 调用 terminal(commands="docker build .", sandbox=False)
  → HITL after_model: sandbox 参数 = False
    → YOLO 模式开启？
      → 是 → 跳过审批，直接放行 (sandbox=False)
      → 否 → 触发 interrupt():
         "⚠ Agent 请求在不使用沙箱的情况下执行（完全系统访问权限）
          命令: docker build .
          批准将允许命令在无 OS 沙箱限制下运行。"
         → 用户批准 → 透传 tool_call (sandbox=False)
         → 用户拒绝 → blocked ToolMessage
  → 工具执行: 无 sandbox_backend 包装，但仍执行 env scrub + 黑名单 + cwd 钳制
```

### python_repl 沙箱绕过路径

```
LLM 调用 python_repl(query="import subprocess; ...", sandbox=False)
  → HITL after_model: sandbox 参数 = False
    → YOLO 模式开启？
      → 是 → 跳过审批，直接放行
      → 否 → 触发 interrupt():
         "⚠ Agent 请求在不使用沙箱的环境下执行 Python（完全系统访问权限）
          代码（200 字符中第 1-200 个，共 N 字符）：
          import subprocess; ...
          批准将允许在无 bwrap/seatbelt 隔离的情况下运行此代码。"
         → 用户批准 → 透传 tool_call (sandbox=False)
         → 用户拒绝 → blocked ToolMessage
  → 工具执行: 无 sandbox_backend 包装，但仍执行 env scrub + 受限 builtins + 超时
```

---

## 第一层：进程级隔离（修复现有漏洞）

### 1.1 新建 `agent/tools/pub_base/env_scrub.py`

环境变量清洗模块，在子进程启动前过滤含密钥的环境变量。

**核心接口：**

```python
def scrub_env(base_env: dict | None = None) -> dict[str, str]:
    """过滤含密钥子串的环境变量，返回安全的子进程环境字典。"""
```

**规则（参考 hermes-agent `code_execution_tool.py:88-143`）：**

- 密钥子串拦截：`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `BEARER`, `APIKEY`
- 安全前缀允许：`PATH`, `HOME`, `USER`, `LANG`, `LC_`, `TERM`, `TMPDIR`, `TMP`, `TEMP`, `SHELL`, `LOGNAME`, `XDG_`, `PYTHONPATH`, `VIRTUAL_ENV`, `CONDA`
- Windows OS 必需变量（按名）：`SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `OS`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`
- sherry_agent 专属密钥（按名）：`MAIN_LLM_API_KEY`, `TAVILY_API_KEY`, `LANGSMITH_API_KEY`, `REASONER_LLM_API_KEY`, `AUXILIARY_LLM_API_KEY`, `ITTT_API_KEY`, `VTTT_API_KEY`, `TTI_API_KEY`, `RERANKER_API_KEY`, `EMBEDDING_API_KEY`, `STT_API_KEY`

**不取 hermes-agent 的部分：** `HERMES_HOME`/`HERMES_PROFILE` 等（sherry_agent 无此概念）、`_is_hermes_internal_secret` 动态密钥检测（sherry_agent 无 gateway relay 场景）

### 1.2 修改 `agent/tools/terminal.py`

| 改动点       | 现状                                                        | 目标                                                                |
| ------------ | ----------------------------------------------------------- | ------------------------------------------------------------------- |
| 工具参数     | 无 sandbox 参数                                             | 新增 `sandbox: bool = True` 参数                                    |
| `shell=True` | `subprocess.Popen(cmd_str, shell=True)`                     | 保留（shell 管道/重定向需要），但增加命令安全检查                   |
| 黑名单       | `{"rm -rf /", "mkfs", "shutdown", "reboot"}` — 4 条子串匹配 | 扩展为正则模式集（参考已有 `detection.py` 的 `HARDLINE_PATTERNS`）  |
| 环境变量     | 继承完整 `os.environ`                                       | 集成 `scrub_env()` 过滤密钥                                         |
| `cwd`        | `ROOT_DIR`                                                  | 不变                                                                |
| 沙箱         | 无                                                          | 第二层集成后由 `sandbox.wrap()` 包装                                |
| 异步路径     | `create_subprocess_shell`                                   | 改为 `create_subprocess_exec` + `bash -c`（经沙箱包装后命令是列表） |

**工具 schema 变更：**

```python
class SafeShellTool(ShellTool):
    name: str = "terminal"
    description: str = (
        "Run shell commands in a sandboxed workspace. "
        "Set sandbox=False to bypass OS sandbox (requires human approval)."
    )
```

**`_run()` 和 `_arun()` 核心逻辑：**

```python
def _run(self, commands: str | list[str], sandbox: bool = True, **kwargs) -> str:
    # 1. 危险命令检测（扩展黑名单为正则模式）
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(cmd_str):
            return "Blocked: unsafe command."

    # 2. 环境变量清洗
    env = scrub_env(os.environ)

    # 3. 命令包装
    cmd_list = ["bash", "-c", cmd_str]
    if sandbox:
        backend = get_backend(SANDBOX_POLICY)
        if backend:
            cmd_list, env = backend.wrap(cmd_list, env)
        # backend=None (Windows/auto): 仅进程级隔离

    # 4. 执行（不再用 shell=True，因为 cmd_list 已是列表）
    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
    )
```

**不取 hermes-agent 的部分：** `_rewrite_compound_background`（subshell-wait 防护）、session snapshot 机制

### 1.3 修改 `agent/tools/python_repl.py`

| 改动点         | 现状                                               | 目标                                                    |
| -------------- | -------------------------------------------------- | ------------------------------------------------------- |
| 工具参数       | 无 sandbox 参数                                    | 新增 `sandbox: bool = True` 参数                        |
| `__builtins__` | `_REPL_WRAPPER` 中定义了安全白名单字典             | 确认正确使用（当前代码实际正确，audit report 可能误报） |
| 子进程隔离     | `subprocess.Popen([sys.executable, "-c", script])` | 不变（已有隔离）                                        |
| 超时           | 30s + kill                                         | 不变                                                    |
| 环境变量       | 继承完整 `os.environ`                              | 集成 `scrub_env()`                                      |
| 沙箱           | 无                                                 | 第二层集成后由 `sandbox.wrap()` 包装子进程命令          |

**工具 schema 变更：**

```python
class TimedPythonREPLTool(PythonREPLTool):
    name: str = "python_repl"
    description: str = (
        "Python REPL with timeout and subprocess isolation. "
        "Set sandbox=False to bypass OS sandbox (requires human approval)."
    )
```

**关于 audit report #13 的分析：**

`_REPL_WRAPPER` 模板中 `__builtins__ = {安全字典}` 重定义了 `__builtins__`，后续 `exec(code, {"__builtins__": __builtins__}, {})` 中的 `__builtins__` 引用的是重定义后的字典。代码实际是正确的，但仍有**类型穿越逃逸**风险（`().__class__.__base__.__subclasses__()` 链），这需要 OS 级沙箱（第二层）来根本解决。

---

## 第二层：OS 原生沙箱（平台条件启用）

### 2.1 新建 `agent/tools/pub_base/sandbox.py` — 沙箱抽象层

参考 oh-my-openagent-dev 的 `sandbox-contracts.ts` + `sandbox-platform.ts`。

```python
from enum import Enum

class SandboxPolicy(Enum):
    REQUIRED = "required"  # 不可用则拒绝命令
    AUTO = "auto"          # 不可用则降级为无沙箱（默认）
    OFF = "off"            # 禁用沙箱

class SandboxBackend(ABC):
    @abstractmethod
    def probe(self) -> bool:
        """检测沙箱工具是否可用且能正常工作。"""
        ...

    @abstractmethod
    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        """包装命令列表和环境变量，返回沙箱包装后的版本。"""
        ...

def get_backend(policy: SandboxPolicy) -> SandboxBackend | None:
    """平台检测 + 工具探测，返回可用后端或 None。"""
    import platform
    system = platform.system()
    if system == "Linux":
        backend = BwrapBackend()
    elif system == "Darwin":
        backend = SeatbeltBackend()
    else:
        backend = None  # Windows: 无原生轻量沙箱

    if backend and backend.probe():
        return backend
    if policy == SandboxPolicy.REQUIRED:
        raise RuntimeError(f"Required sandbox unavailable on {system}")
    return None  # AUTO: 降级为无沙箱
```

配置：`.env` 中 `SANDBOX_POLICY=auto`

### 2.2 新建 `agent/tools/pub_base/sandbox_bwrap.py` — Linux bwrap 后端

参考 oh-my-openagent-dev `sandbox-platform.ts:68-132` + `sandbox-bwrap-probe.ts`。

```python
class BwrapBackend(SandboxBackend):
    _probe_cache: bool | None = None  # 类级缓存

    def probe(self) -> bool:
        """烟雾测试：bwrap 可能存在但被 AppArmor 阻止。"""
        if self._probe_cache is not None:
            return self._probe_cache
        # 参考 oh-my-openagent sandbox-bwrap-probe.ts
        # 运行: bwrap --ro-bind / / --proc /proc --dev /dev true
        # 3s 超时，缓存结果
        ...

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        writable = [str(ROOT_DIR), str(TEMP_DIR)]
        bwrap_args = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev-bind", "/dev", "/dev",
            "--tmpfs", "/tmp",
            "--unshare-all",
            *[item for d in writable for item in ("--bind", d, d)],
            "--chdir", str(ROOT_DIR),
            "--",
            *cmd,
        ]
        return bwrap_args, env
```

**不取 oh-my-openagent 的部分：** `lockPaths`（proper-lockfile 机制）、`foreignRoots`、`payloadPaths`、TypeScript 泛型约束

### 2.3 新建 `agent/tools/pub_base/sandbox_seatbelt.py` — macOS sandbox-exec 后端

参考 oh-my-openagent-dev `sandbox-platform.ts:135-155`。

```python
class SeatbeltBackend(SandboxBackend):
    def probe(self) -> bool:
        return shutil.which("sandbox-exec") is not None

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        profile = self._build_profile()
        return ["sandbox-exec", "-p", profile, "--", *cmd], env

    def _build_profile(self) -> str:
        writable = [str(ROOT_DIR), str(TEMP_DIR)]
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            *[f'(allow file-write* (subpath {json.dumps(p)}))' for p in writable],
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-write* (literal "/dev/tty"))',
        ]
        return "\n".join(lines)
```

### 2.4 Windows 降级策略

Windows 无原生轻量沙箱可用。降级策略：

- `SANDBOX_POLICY=auto`（默认）：`get_backend()` 返回 `None`，命令在进程级隔离下执行（env scrub + 黑名单 + cwd 钳制）
- `SANDBOX_POLICY=required`：`get_backend()` 抛出 `RuntimeError`，拒绝执行
- 工具内捕获 `RuntimeError` 后返回错误信息给 LLM

---

## HITL 沙箱绕过审批

### 2.5 修改 `agent/middlewares/humanInTheLoop/core.py`

在 `after_model()` 的 terminal 拦截路径中，**现有危险命令审批管道之前**，增加沙箱绕过检查。

**terminal 路径（现有代码 `core.py:276` 之前插入）：**

```python
# ── Terminal tool: sandbox bypass approval ──
if tool_name == "terminal":
    sandbox_enabled = tool_args.get("sandbox", True)
    command = tool_args.get("commands", "") or tool_args.get("command", "")
    if isinstance(command, list):
        command = " && ".join(command)

    # 沙箱绕过 → 必须用户确认（YOLO 模式除外）
    if not sandbox_enabled and not _is_yolo_active(self.config):
        action_request = ActionRequest(
            name=tool_name,
            args=tool_args,
            description=(
                f"Agent requests UNSANDBOXED execution "
                f"(full system access without OS sandbox).\n"
                f"Command: {command}\n"
                f"Approving will run this command without bwrap/seatbelt isolation."
            ),
        )
        review_config = ReviewConfig(
            action_name=tool_name,
            allowed_decisions=["approve", "reject"],
        )
        try:
            hitl_response = interrupt(HITLRequest(
                action_requests=[action_request],
                review_configs=[review_config],
            ))
            decisions = hitl_response.get("decisions", [])
            if not decisions or decisions[0]["type"] != "approve":
                artificial_tool_messages.append(ToolMessage(
                    content=f"Sandbox bypass rejected. {BLOCKED_MESSAGE}",
                    name=tool_name,
                    tool_call_id=tool_call["id"],
                    status="error",
                ))
                continue
        except GraphInterrupt:
            raise  # 让 LangGraph 持久化 interrupt
        except Exception:
            artificial_tool_messages.append(ToolMessage(
                content=f"Sandbox bypass approval failed. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=tool_call["id"],
                status="error",
            ))
            continue

    # 然后进入现有的危险命令审批管道（check_command）
    result = self.approval.check_command(command, session_id)
    ...  # 现有逻辑不变
```

**python_repl 路径（新增，在 terminal 路径之后）：**

```python
# ── Python REPL tool: sandbox bypass approval ──
if tool_name == "python_repl":
    sandbox_enabled = tool_args.get("sandbox", True)
    code = tool_args.get("query", "") or tool_args.get("command", "")

    if not sandbox_enabled and not _is_yolo_active(self.config):
        code_preview = code[:200]
        code_suffix = "..." if len(code) > 200 else ""
        action_request = ActionRequest(
            name=tool_name,
            args=tool_args,
            description=(
                f"Agent requests UNSANDBOXED Python execution "
                f"(full system access without OS sandbox).\n"
                f"Code (first 200 chars of {len(code)} total):\n"
                f"{code_preview}{code_suffix}\n"
                f"Approving will run this code without bwrap/seatbelt isolation."
            ),
        )
        review_config = ReviewConfig(
            action_name=tool_name,
            allowed_decisions=["approve", "reject"],
        )
        try:
            hitl_response = interrupt(HITLRequest(
                action_requests=[action_request],
                review_configs=[review_config],
            ))
            decisions = hitl_response.get("decisions", [])
            if not decisions or decisions[0]["type"] != "approve":
                artificial_tool_messages.append(ToolMessage(
                    content=f"Sandbox bypass rejected. {BLOCKED_MESSAGE}",
                    name=tool_name,
                    tool_call_id=tool_call["id"],
                    status="error",
                ))
                continue
        except GraphInterrupt:
            raise
        except Exception:
            artificial_tool_messages.append(ToolMessage(
                content=f"Sandbox bypass approval failed. {BLOCKED_MESSAGE}",
                name=tool_name,
                tool_call_id=tool_call["id"],
                status="error",
            ))
            continue

    # python_repl 默认不经过危险命令审批管道
    # （Python 代码无法用 shell 模式匹配检测）
    revised_tool_calls.append(tool_call)
    continue
```

**YOLO 模式交互：**

- `_is_yolo_active(self.config)` 已在 `approval.py:37-49` 实现
- YOLO 模式开启时（`config.yolo_mode == True` 或 `config.mode == ApprovalMode.OFF` 或 `SHERRY_YOLO_MODE` 环境变量设置）：跳过沙箱绕过审批，直接放行
- 与现有 Layer 3 YOLO bypass 语义一致——YOLO 绕过一切

### 2.6 不改动 `agent/middlewares/humanInTheLoop/detection.py`

现有 `HARDLINE_PATTERNS` 和 `DANGEROUS_PATTERNS` 已覆盖沙箱绕过场景中的危险命令检测。沙箱绕过检查在审批管道之前独立运行，不与危险模式检测耦合。

---

## 集成：工具内沙箱包装

### 2.7 修改 `agent/tools/terminal.py` — 集成 sandbox.wrap()

在 `_run()` 和 `_arun()` 中，子进程启动前调用沙箱包装：

```python
def _run(self, commands: str | list[str], sandbox: bool = True, **kwargs) -> str:
    # ... 黑名单检测 + cmd_str 组装 ...

    env = scrub_env(os.environ)

    cmd_list = ["bash", "-c", cmd_str]
    if sandbox:
        try:
            backend = get_backend(SANDBOX_POLICY)
        except RuntimeError as e:
            return f"Error: {e}"
        if backend:
            cmd_list, env = backend.wrap(cmd_list, env)

    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
    )
    # ... 超时 + 编码处理 ...
```

异步路径同理，改为 `asyncio.create_subprocess_exec(*cmd_list, env=env, ...)`。

### 2.8 修改 `agent/tools/python_repl.py` — 集成 sandbox.wrap()

```python
def _run_with_timeout(command: str, timeout: int, sandbox: bool = True) -> str:
    safe_repr = repr(command)
    script = _REPL_WRAPPER.format(command_repr=safe_repr)

    cmd_list = [sys.executable, "-c", script]
    env = scrub_env(os.environ)

    if sandbox:
        try:
            backend = get_backend(SANDBOX_POLICY)
        except RuntimeError as e:
            return f"Error: {e}"
        if backend:
            cmd_list, env = backend.wrap(cmd_list, env)

    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    # ... 超时 + 结果解析 ...
```

`TimedPythonREPLTool._run()` 和 `_arun()` 需要透传 `sandbox` 参数到 `_run_with_timeout()`。

### 2.9 修改 `.env.example`

```bash
# Sandbox policy: required / auto / off
# required: sandbox must be available (bwrap on Linux, sandbox-exec on macOS),
#           else reject commands
# auto: use sandbox if available, otherwise run unsandboxed with warning (default)
# off: disable sandboxing entirely
SANDBOX_POLICY = auto
```

---

## 文件清单

| #   | 操作 | 文件路径                                   | 参考来源                                                         | 行数估算   |
| --- | ---- | ------------------------------------------ | ---------------------------------------------------------------- | ---------- |
| 1   | 新建 | `agent/tools/pub_base/env_scrub.py`        | hermes-agent `code_execution_tool.py:_scrub_child_env`           | ~80        |
| 2   | 新建 | `agent/tools/pub_base/sandbox.py`          | oh-my-openagent `sandbox-contracts.ts` + `sandbox-platform.ts`   | ~60        |
| 3   | 新建 | `agent/tools/pub_base/sandbox_bwrap.py`    | oh-my-openagent `sandbox-platform.ts` + `sandbox-bwrap-probe.ts` | ~80        |
| 4   | 新建 | `agent/tools/pub_base/sandbox_seatbelt.py` | oh-my-openagent `sandbox-platform.ts:buildDarwinProfile`         | ~50        |
| 5   | 修改 | `agent/tools/terminal.py`                  | hermes-agent `local.py:_sanitize_subprocess_env` + `docker.py`   | ~60 行改动 |
| 6   | 修改 | `agent/tools/python_repl.py`               | —                                                                | ~30 行改动 |
| 7   | 修改 | `agent/middlewares/humanInTheLoop/core.py` | 复用现有 interrupt 机制                                          | ~80 行新增 |
| 8   | 修改 | `.env.example`                             | —                                                                | +5 行      |
| 9   | 新建 | `tests/test_env_scrub.py`                  | hermes-agent `tests/tools/test_env_passthrough.py`               | ~60        |
| 10  | 新建 | `tests/test_sandbox.py`                    | oh-my-openagent `sandbox-bwrap-probe.test.ts`                    | ~80        |

---

## 实现顺序

```
Phase 1: 进程级隔离基础
  1. env_scrub.py — 环境变量清洗模块
  2. test_env_scrub.py — 单元测试
  3. 修改 terminal.py — 集成 env scrubbing + 扩展黑名单 + 加 sandbox 参数
  4. 修改 python_repl.py — 集成 env scrubbing + 加 sandbox 参数

Phase 2: HITL 沙箱绕过审批
  5. 修改 core.py — after_model() 增加 sandbox=False 拦截（terminal + python_repl）
  6. 测试 HITL 沙箱绕过审批流程

Phase 3: OS 原生沙箱
  7. sandbox.py — 抽象层 + SandboxPolicy + get_backend()
  8. sandbox_bwrap.py — Linux bwrap 后端 + 烟雾测试
  9. sandbox_seatbelt.py — macOS sandbox-exec 后端
  10. test_sandbox.py — 单元测试

Phase 4: 集成
  11. 修改 terminal.py — 调用 sandbox.wrap() 包装命令
  12. 修改 python_repl.py — 调用 sandbox.wrap() 包装子进程
  13. .env.example — 添加 SANDBOX_POLICY 配置
```

---

## 关键设计决策

| 决策                 | 选择                                 | 理由                                                        |
| -------------------- | ------------------------------------ | ----------------------------------------------------------- |
| 工具复用方式         | 同工具 + `sandbox` 参数              | 不创建新工具；LLM 通过参数表达意图；默认安全                |
| 沙箱绕过审批         | HITL interrupt                       | 复用现有 6 层审批管道基础设施；用户明确知情后才放行         |
| 沙箱绕过后仍有防护   | env scrub + 黑名单 + cwd 钳制        | 纵深防御；即使无 OS 沙箱也不裸奔                            |
| HITL 拦截位置        | `after_model()` 现有 terminal 路径前 | 在命令审批管道之前先检查沙箱状态；逻辑清晰                  |
| `sandbox` 参数默认值 | `True`                               | 安全默认；LLM 需要显式声明 `sandbox=False` 才触发审批       |
| YOLO 模式交互        | YOLO 绕过一切                        | 与现有 Layer 3 YOLO bypass 语义一致                         |
| python_repl 审批描述 | 显示代码前 200 字符 + 总长度         | 平衡信息量和可读性                                          |
| shell 执行           | 保留 `bash -c`                       | 需要管道/重定向；OS 沙箱提供边界，而非 shell=False          |
| 沙箱策略             | 三态 `required/auto/off`             | 参考 oh-my-openagent；auto 为默认，降级安全                 |
| bwrap 烟雾测试       | 必须                                 | 参考 oh-my-openagent；AppArmor 可能阻止可用但存在的 bwrap   |
| Windows              | 无原生沙箱，降级为进程级             | 无原生轻量沙箱可用；env scrub + 黑名单 + cwd 仍提供基本防护 |

---

## 与参考项目的对比

| 维度              | hermes-agent                                    | oh-my-openagent         | sherry_agent（本方案）                  |
| ----------------- | ----------------------------------------------- | ----------------------- | --------------------------------------- |
| 沙箱后端数        | 6（Local/Docker/SSH/Modal/Daytona/Singularity） | 2（bwrap/sandbox-exec） | 2（bwrap/sandbox-exec）+ Windows 进程级 |
| 环境变量清洗      | 完整（子串+前缀+动态检测+会话隔离）             | 不涉及                  | 简化版（子串+前缀+Windows 必需）        |
| 文件写入沙箱      | HERMES_WRITE_SAFE_ROOT + denylist               | 不涉及                  | 不涉及（已有 resolve_path 越界防护）    |
| 代码执行沙箱      | 子进程 + RPC + 审批闸门                         | 不涉及                  | 子进程 + env scrubbing                  |
| 终端沙箱          | 多后端                                          | bwrap/sandbox-exec      | bwrap/sandbox-exec                      |
| HITL 沙箱绕过审批 | 无（直接用不同后端）                            | 不涉及                  | sandbox=False → interrupt()             |
| 代码量            | ~5000+ 行                                       | ~500 行                 | ~400 行                                 |

---

## 三项目沙箱方式摘要

### opencode-dev (TypeScript/Bun)

- **沙箱概念**："sandboxes" 是 worktree 目录注册到项目，不是 OS 级隔离
- **Bash 工具**：明确不沙箱——"the spawned shell runs with the host user's filesystem, process, and network authority"
- **Code mode**：`@opencode-ai/codemode` 受限解释器，限制可用工具但不提供 OS 级隔离
- **权限系统**：rule-based permission model (ask/allow/deny)，不是沙箱

### oh-my-openagent-dev (TypeScript/Bun)

- **沙箱方式**：OS 原生沙箱
  - Linux: `bwrap`（`--ro-bind / /` + `--bind` 可写目录 + `--tmpfs /tmp`）
  - macOS: `sandbox-exec`（Seatbelt profile: `(deny file-write*)` + `(allow file-write* (subpath ...))`)
  - Windows: 不支持，降级为无沙箱
- **策略三态**：`required` / `auto` / `off`
- **bwrap 烟雾测试**：检测 bwrap 是否实际可用（AppArmor 可能阻止）
- **路径规范化**：处理不存在的路径，仅规范化已存在的前缀
- **作用范围**：内存/反射子进程，非主 agent 工具

### hermes-agent-main (Python)

- **多后端架构**：
  - LocalEnvironment: 无隔离
  - DockerEnvironment: cap-drop ALL, no-new-privileges, PID 限制, 资源限制, 网络隔离
  - SSHEnvironment: 远程机器隔离
  - ModalEnvironment: 云沙箱 via Modal SDK
  - DaytonaEnvironment: 云沙箱 via Daytona SDK
  - SingularityEnvironment: HPC 容器隔离
- **BaseEnvironment**: ABC with unified execute() — session snapshot, CWD tracking, interrupt handling, timeout
- **execute_code**: 子进程 + 环境变量清洗 + RPC 工具调用 + 审批闸门
- **文件写入安全**: `HERMES_WRITE_SAFE_ROOT` sandbox + protected paths denylist
- **Sandbox-mirror write guard**: 防止写入 sandbox-mirrored Hermes 状态

### sherry_agent (Python) — 现状

- `terminal.py`：`shell=True` + 4 条黑名单子串匹配
- `python_repl.py`：子进程隔离 + 受限 builtins（有类型穿越逃逸风险）
- `resolve_path`：已修复（有 `PathOutOfBoundsError` 越界防护）
- subagent：仅 CWD 前缀校验，无进程隔离
- 无 Docker/容器后端
