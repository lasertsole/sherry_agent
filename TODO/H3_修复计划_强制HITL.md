# H3 修复计划：强制 HITL 方向

> 风险项 H3：Windows 无 bwrap/seatbelt；`SANDBOX_POLICY=auto`（默认）下 `get_backend()` 返回 `None`，工具层静默降级为裸 `subprocess`，正则黑名单可绕过。
> 方向：把"请求了沙箱但后端不可用"的降级路径路由到已有的 `sandbox=False` HITL 审批管线，不新建审批机制。

## 问题定位

### 当前代码流（有缺口）

```
LLM 调用 terminal(sandbox=True)
  │
  ├─ strategies.py: TerminalApprovalHandler.handle()
  │    行 134: if not is_yolo_mode(...) and not tool_args.get("sandbox", True):
  │             ↑ 只看 sandbox 原始值，True → 跳过 HITL
  │             ← sandbox=True 时直接放行，中间件不知道后端是否存在
  │
  ├─ terminal/core.py: _resolve_sandboxed_argv()
  │    行 449: policy = read_policy()          # auto
  │    行 453: backend = get_backend(policy)   # Windows → None
  │    行 456: if backend is None:
  │    行 457:     logger.warning("...degrading to unsandboxed shell execution")
  │    行 461:     return None, env            ← 静默降级，H3 漏洞
  │
  └─ _execute_sync() → subprocess.Popen(shell=True)  ← 裸执行，仅正则黑名单兜底
```

### 缺口本质

审批中间件在工具执行**之前**运行，但它只看 `sandbox` 参数的原始值（True/False），不知道后端是否真的可用。工具层在执行时才发现后端不可用，此时审批窗口已过。

## 解决方案

引入 `sandbox_effective(requested)` 判定函数，让中间件在审批时就知道沙箱是否真的能用。`sandbox=True` 但后端不可用时返回 `False`，路由到已有的 `_sandbox_bypass_interrupt` HITL 审批。

### 行为矩阵（修复后）

```
========== =============================== ==============================
policy     sandbox=True                    sandbox=False
           + backend available
========== =============================== ==============================
auto       沙箱执行，无审批（不变）          HITL 审批（不变）

auto       sandbox=True + backend 不可用（Windows / 无 bwrap）：
           → sandbox_effective() = False
           → 路由到 _sandbox_bypass_interrupt（与 sandbox=False 同路径）
           ├─ auto_edit:    弹审批卡（跳过 SMART）
           ├─ confirm_all:  Layer 4 已覆盖，审批卡标注沙箱不可用
           └─ full_access:  YOLO 绕过 + 一次性启动警告

required   后端可用：沙箱执行                 DENIED（不变）
           后端不可用：ToolException

off        无沙箱，无审批（不变）              无沙箱，无审批（不变）
========== =============================== ==============================
```

## 三种访问模式处理

### 变更前确认（confirm_all）

- Layer 4 已对每条命令弹审批卡，H3 的 `sandbox_effective` 检查效果被 Layer 4 覆盖
- **无额外变化**，仅审批卡描述增加沙箱状态提示
- 审批卡文案：`Confirm-all mode: approval needed (sandbox unavailable — runs unsandboxed)`

### 自动编辑（auto_edit，默认）

- **修复主战场**
- 当前：`sandbox=True` + Windows → 静默降级为裸 subprocess，无审批
- 修复后：`sandbox=True` + Windows → `sandbox_effective=False` → 弹审批卡
- SMART 审批跳过（与 `sandbox=False` 对齐）：无沙箱执行的风险高于有沙箱，让人判断而非 LLM 判断
- 首次弹卡，用户选 `approve` 后可设 session 级白名单，后续同类命令自动放行
- 危险命令（`detect_dangerous_command` 命中）仍单独弹卡

### 完全访问（full_access / YOLO）

- YOLO Layer 3 绕过所有审批，H3 修复被自然绕过——设计如此
- 增加**一次性启动警告**：在无沙箱平台上激活 YOLO 时发出提示
- 警告内容：`完全访问模式在无 OS 沙箱的平台上激活 — 所有命令将无审批无沙箱运行`
- 触发点：`access_mode_service.set_access_mode` 切换到 `full_access` 时检查 `sandbox_effective(True)`

## 实现步骤

### Step 1: sandbox.py 新增 `sandbox_effective()`

**文件**：`agent/tools/pub_base/sandbox.py`

在 `get_backend()` 之后新增：

```python
def sandbox_effective(requested: bool) -> bool:
    """Whether OS sandboxing will actually be used for this call.

    Returns False when:
    - The caller did not request sandboxing (``sandbox=False``)
    - The policy is OFF
    - The policy is AUTO and no backend is available on this platform
    - The policy is REQUIRED and no backend is available (get_backend
      raises RuntimeError; caught here so the middleware can gate it)

    The middleware calls this BEFORE the tool runs, so the HITL approval
    fires when degradation will happen — not after the fact.
    """
    if not requested:
        return False
    policy = read_policy()
    if policy is SandboxPolicy.OFF:
        return False
    try:
        return get_backend(policy) is not None
    except RuntimeError:
        return False
```

**要点**：
- `read_policy()` 每次读环境变量（不缓存），与现有设计一致
- `get_backend(AUTO)` 返回 `None` 不抛异常（现有设计）
- `get_backend(REQUIRED)` 抛 `RuntimeError` 时捕获返回 `False`
- `__all__` 中添加 `"sandbox_effective"`

### Step 2: strategies.py TerminalApprovalHandler 用 sandbox_effective 替代原始值

**文件**：`agent/middlewares/humanInTheLoop/strategies.py`

行 134 改动：

```python
# 现有：
if not is_yolo_mode(mw.config, ctx.session_id) and not tool_args.get("sandbox", True):

# 改为：
if not is_yolo_mode(mw.config, ctx.session_id) and not sandbox_effective(
    tool_args.get("sandbox", True)
):
```

头部添加导入：
```python
from agent.tools.pub_base.sandbox import sandbox_effective
```

### Step 3: strategies.py SandboxBypassApprovalHandler 同步改动

**文件**：同上，`SandboxBypassApprovalHandler.matches`（行 258-264）

```python
# 现有：
and not tool_call.get("args", {}).get("sandbox", True)

# 改为：
and not sandbox_effective(tool_call.get("args", {}).get("sandbox", True))
```

### Step 4: core.py 审批卡文案区分降级与绕过

**文件**：`agent/middlewares/humanInTheLoop/core.py`

`_sandbox_bypass_interrupt`（约行 310-325）中构造 `ActionRequest` 的描述文案：

```python
requested_sandbox = tool_call.get("args", {}).get("sandbox", True)
if requested_sandbox and not sandbox_effective(requested_sandbox):
    reason = (
        "OS sandbox is unavailable on this platform, so the command "
        "will run unsandboxed. Approve to proceed without isolation."
    )
else:
    reason = (
        "sandbox=False, so it executes directly after env scrubbing with no OS "
        "sandbox isolation. The OS sandbox is the only thing that keeps a shell "
        "command inside the session's project directory, so approving this call "
        "lets it read and write anywhere this process can."
    )
# reason 拼入 ActionRequest description
```

### Step 5: terminal/core.py 降级日志调整

**文件**：`agent/tools/terminal/core.py`

行 456-461：

```python
# 现有：
if backend is None:
    logger.warning(
        "terminal: sandbox requested but no backend available "
        f"(policy={policy.value}) — degrading to unsandboxed shell execution"
    )
    return None, env

# 改为：
if backend is None:
    logger.info(
        "terminal: sandbox unavailable "
        f"(policy={policy.value}) — running unsandboxed (HITL-approved)"
    )
    return None, env
```

severity 从 `warning` 降为 `info`：审批已通过，不再是静默降级。

### Step 6: python_repl.py 同步降级日志

**文件**：`agent/tools/python_repl.py`

找到同类降级警告（如果存在），同步调整为 `info` + `HITL-approved` 标注。

### Step 7: access_mode_service.py YOLO 激活时沙箱警告

**文件**：`server/service/access_mode_service.py`

`set_access_mode` 中切换到 `full_access` 时：

```python
if mode == FULL_ACCESS:
    from agent.tools.pub_base.sandbox import sandbox_effective
    if not sandbox_effective(True):
        logger.warning(
            "Full access mode activated on a platform without OS sandboxing "
            "— all commands will run unsandboxed without approval"
        )
        # 可选：通过 runtime.hooks 推送 WS 帧到前端 toast
```

## 改动清单

| # | 文件 | 改动 | 行数 |
|---|---|---|---|
| 1 | `agent/tools/pub_base/sandbox.py` | 新增 `sandbox_effective()` + `__all__` | +15 |
| 2 | `agent/middlewares/humanInTheLoop/strategies.py` | `TerminalApprovalHandler` 行 134 | 改 1 |
| 3 | `agent/middlewares/humanInTheLoop/strategies.py` | `SandboxBypassApprovalHandler.matches` 行 263 | 改 1 |
| 4 | `agent/middlewares/humanInTheLoop/core.py` | `_sandbox_bypass_interrupt` 文案分支 | 改 ~10 |
| 5 | `agent/tools/terminal/core.py` | 降级日志 warning→info | 改 2 |
| 6 | `agent/tools/python_repl.py` | 同步降级日志 | 改 2 |
| 7 | `server/service/access_mode_service.py` | YOLO 激活时沙箱警告 | +10 |

## 测试计划

### 单元测试

| 测试 | 场景 | 预期 |
|---|---|---|
| `test_sandbox_effective_off` | `SANDBOX_POLICY=off` + `requested=True` | 返回 `False` |
| `test_sandbox_effective_auto_with_backend` | mock backend probe 成功 | 返回 `True` |
| `test_sandbox_effective_auto_no_backend` | mock `get_backend` 返回 `None` | 返回 `False` |
| `test_sandbox_effective_required_no_backend` | `SANDBOX_POLICY=required` + 无后端 | 返回 `False`（捕获 RuntimeError） |
| `test_sandbox_effective_not_requested` | `requested=False` | 返回 `False` |
| `test_terminal_degrade_routes_to_hitl` | auto_edit + sandbox=True + 无后端 + 有 operator | 审批卡触发 |
| `test_terminal_degrade_yolo_bypass` | full_access + sandbox=True + 无后端 | 不弹卡 |
| `test_terminal_degrade_headless_deny` | headless turn + 降级 | 自动拒绝 |
| `test_terminal_degrade_confirm_all` | confirm_all + 降级 | Layer 4 审批卡含沙箱不可用提示 |
| `test_python_repl_degrade_routes_to_hitl` | python_repl + sandbox=True + 无后端 | 审批卡触发 |
| `test_approval_card_text_degrade` | 降级路径审批卡 | 文案含 "sandbox unavailable" |
| `test_approval_card_text_bypass` | sandbox=False 审批卡 | 文案含 "sandbox=False" |

### 集成测试

| 测试 | 场景 | 预期 |
|---|---|---|
| Linux + bwrap 可用 + auto_edit | sandbox=True | 沙箱执行，无审批卡 |
| Linux + bwrap 可用 + full_access | sandbox=True | 沙箱执行，无审批卡，无警告 |
| Windows + auto_edit | sandbox=True | 审批卡，approve 后 unsandboxed 执行 |
| Windows + confirm_all | sandbox=True | 审批卡标注沙箱不可用 |
| Windows + full_access 激活 | 切换到 YOLO | 一次性警告 |
| SANDBOX_POLICY=required + Windows | sandbox=True | ToolException（行为不变） |

## 不改动的部分

- `get_backend()` 的现有语义和返回值——不修改
- `SandboxPolicy` 枚举和 `read_policy()` / `parse_policy()`——不修改
- `_sandbox_bypass_interrupt` 的审批决议流程（approve/reject/yolo）——不修改
- YOLO bypass 逻辑（Layer 3）——不修改
- SMART 审批对 `sandbox=False` 的处理——不修改（降级路径复用同一 handler，自然跳过 SMART）
- `self_path_policy` 和 PathGuard 中间件——不修改
- `SandboxBackend` ABC 和 bwrap/seatbelt 后端——不修改

## 风险与回退

- **probe 开销**：`sandbox_effective()` 每次调用 `get_backend()`，后者调用 `backend.probe()`。probe 在现有实现中可缓存（`BwrapBackend`/`SeatbeltBackend` 的 `probe()` 方法可缓存进程级结果）。需确认现有后端已缓存 probe，否则每个工具调用多一次 `subprocess` 探测。
- **回退**：删除 `sandbox_effective()` 函数，恢复 strategies.py 中的 `tool_args.get("sandbox", True)` 原始判断，行为完全回到修复前。
