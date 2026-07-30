# Human-In-The-Loop (HITL) 中间件

**English** | [**中文文档**](README.zh.md)

hermes-agent 管道的完整人机回环中间件。提供多层审批门控，涵盖命令执行（硬线/危险检测）、文件写入、MCP 工具调用、破坏性斜杠命令和平台用户许可——全部通过单一中间件钩子管理。

---

## 目录

- [架构概览](#架构概览)
- [层级参考](#层级参考)
  - [1. 硬线与危险检测](#1-硬线与危险检测)
  - [2. 写入审批门](#2-写入审批门)
  - [3. 中断管理器](#3-中断管理器)
  - [4. MCP 交互许可](#4-mcp-交互许可)
  - [5. 看板故障分类](#5-看板故障分类)
  - [6. 智能审批](#6-智能审批)
  - [7. 配对存储](#7-配对存储)
  - [8. 斜杠确认](#8-斜杠确认)
- [中间件钩子](#中间件钩子)
- [配置](#配置)
- [审批钩子系统](#审批钩子系统)
- [文件结构](#文件结构)

---

## 架构概览

HITL 中间件由七个独立的子门控组成，由 `HumanInTheLoop` 中间件类统一编排：

```
HumanInTheLoop
├── ApprovalPipeline      (approval.py — 分层命令审批)
│   ├── detect_hardline_command()
│   ├── detect_dangerous_command()
│   └── smart_approve()
├── WriteApprovalGate     (gates.py — 文件/内存写入门控)
├── InterruptManager      (gates.py — 按会话中断标志)
├── MCPElicitationConsent (gates.py — MCP 服务器许可)
├── KanbanTriage          (gates.py — 任务故障分类)
├── PairingStore          (gates.py — 平台用户许可)
└── SlashConfirm          (gates.py — 破坏性斜杠确认)
```

每个门控均可独立实例化和测试。`HumanInTheLoop` 中间件将它们串联起来，并通过标准的 `AgentMiddleware` 生命周期钩子（`after_model`、`wrap_tool_call`、`awrap_tool_call`、`abefore_agent`）对外暴露。

---

## 层级参考

### 1. 硬线与危险检测

**文件：** `detection.py`

两个静态模式匹配器，无副作用对命令进行分类：

| 函数 | 用途 |
|---|---|
| `detect_hardline_command(cmd)` | 对照 `HARDLINE_PATTERNS` 检查——必须始终审核的命令（`rm -rf`、`format`、`dd` 等） |
| `detect_dangerous_command(cmd)` | 对照 `DANGEROUS_PATTERNS` 检查——具有高破坏潜力的命令（`DROP TABLE`、`shutdown`、`rm`、强制推送等） |

两者都返回第一个匹配的模式（字符串）或 `None`。

### 2. 写入审批门

**文件：** `gates.py` — 类 `WriteApprovalGate`

管理对文件或内存目标的待定写入操作。每次写入通过唯一 ID 追踪，等待审批或拒绝：

| 方法 | 描述 |
|---|---|
| `request_write(target, content, session_id)` | 提交写入请求等待审批。返回包含追踪 `write_id` 的 `ApprovalResult`。 |
| `approve_write(session_id, write_id)` | 批准待定的写入。 |
| `reject_write(session_id, write_id)` | 拒绝待定的写入。 |
| `get_pending_writes(session_id, target)` | 列出待定写入，可选按目标类型过滤。 |

### 3. 中断管理器

**文件：** `gates.py` — 类 `InterruptManager`

按会话的布尔标志，用于在执行过程中拦截工具调用：

| 方法 | 描述 |
|---|---|
| `set_interrupt(session_id, active=True)` | 设置或清除中断标志。 |
| `is_interrupted(session_id)` | 检查会话是否被中断。 |
| `clear_interrupt(session_id)` | 清除中断标志（便捷别名）。 |

当中断被设置时，`wrap_tool_call` / `awrap_tool_call` 钩子会返回状态为 `"error"` 的 `ToolMessage`，阻止执行。

### 4. MCP 交互许可

**文件：** `gates.py` — 类 `MCPElicitationConsent`

针对可能产生副作用的 MCP（模型上下文协议）服务器：

| 方法 | 描述 |
|---|---|
| `request_consent(server_name, session_id)` | 向用户发起中断，请求对 MCP 服务器交互的明确许可。 |

### 5. 看板故障分类

**文件：** `gates.py` — 类 `KanbanTriage`

追踪任务故障，用于看板式分类升级：

| 方法 | 描述 |
|---|---|
| `report_task_failure(task_id, session_id)` | 注册任务故障。返回 `TriageStatus`（`NEW`、`ACKNOWLEDGED` 或 `RESOLVED`）。如果故障次数超过配置的 `recurrence_limit`，则抛出 `RecurrenceLimitError`。 |
| `resolve_triage(task_id, session_id)` | 将已分类的任务标记为已解决。 |

### 6. 智能审批

**文件：** `approval.py` — 类 `ApprovalPipeline`

可配置的多层审批管道：

| 层级 | 机制 |
|---|---|
| **层 1 — 硬线检测** | 始终拦截的命令（`rm -rf`、`format` 等） |
| **层 2 — 危险检测** | 标记的命令（`DROP TABLE`、`shutdown` 等） |
| **层 3 — 终端模式** | 委托审批策略处理终端命令 |
| **层 4 — 工具审批** | 插件升级的工具审批（`request_tool_approval`） |
| **层 5 — 会话缓存** | 已批准的工具按会话缓存，避免重复提示 |
| **层 6 — 智能审批** | `smart_approve()` — 基于命令内容和上下文的启发式自动批准/自动拒绝 |
| **层 7 — 人工中断** | 回退到 `interrupt()` 等待用户决策 |

管道对外暴露供外部调用：

| 方法 | 描述 |
|---|---|
| `check_command(command, session_id)` | 运行硬线 + 危险检测。返回 `ApprovalResult`。 |
| `check_command_with_approval(command, session_id, prompt_fn)` | 完整管道，包含智能审批和人工中断。 |
| `smart_approve(command)` | 仅运行启发式审批（不含检测或中断）。 |
| `request_tool_approval(name, args, session_id)` | 插件升级的工具审批检查。 |
| `approve_tool_for_session(name, args, session_id)` | 将会话期间已批准的工具缓存。 |

### 7. 配对存储

**文件：** `gates.py` — 类 `PairingStore`

平台级别的用户白名单管理：

| 方法 | 描述 |
|---|---|
| `is_user_allowed(platform, user_id)` | 检查用户是否在指定平台上获得批准。 |
| `approve_user(platform, user_id)` | 将用户添加到白名单。 |
| `revoke_user(platform, user_id)` | 从白名单中移除用户。 |

### 8. 斜杠确认

**文件：** `gates.py` — 类 `SlashConfirm`

针对破坏性斜杠命令（如 `/reset`、`/kill`）的确认门控：

| 方法 | 描述 |
|---|---|
| `confirm_destructive(action, session_id)` | 发起中断，请求用户确认破坏性操作。返回 `ApprovalResult`。 |

---

## 中间件钩子

`HumanInTheLoop` 类通过四个钩子接入代理生命周期：

| 钩子 | 用途 |
|---|---|
| `after_model` / `aafter_model` | 拦截 LLM 输出。对每次工具调用：运行命令审批、写入门控检查、`interrupt_on` 配置检查和插件升级审批。被拦截的工具调用替换为人工的 `ToolMessage` 结果。 |
| `wrap_tool_call` | 在执行任何工具前检查中断标志。如果会话被中断，返回一个错误的 `ToolMessage`。 |
| `awrap_tool_call` | `wrap_tool_call` 的异步变体。 |
| `abefore_agent` / `before_agent` | 重置每轮状态（清除 `turn_interrupted` 标志）。 |

### 中断流程

```
LLM 输出 → after_model
  ├── 硬线/危险检查（层 1-2）
  ├── 写入审批门控（仅内存写入）
  ├── interrupt_on 配置检查
  ├── 插件工具审批（层 4）
  └── 修正后的 tool_calls + 人工 ToolMessage

每个工具调用 → wrap/awrap_tool_call
  └── 中断标志检查 → 拦截或放行
```

---

## 配置

所有配置通过 `HITLConfig` 数据类（定义在 `types.py`）传递：

| 字段 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `mode` | `ApprovalMode` | `STRICT` | `STRICT`、`SMART` 或 `DISABLED` |
| `interrupted_tools` | `dict[str, bool \| dict]` | `{}` | 通过 `interrupt_on` 配置门控的工具名称。每个条目可以是布尔值（默认允许决策 `["approve", "edit", "reject"]`）或包含 `allowed_decisions` 和可选的 `description` 可调用对象的字典。 |
| `interrupt_on` | 已弃用 | — | 已被 `interrupted_tools` 替代。 |
| `write_approval_memory` | `bool` | `False` | 通过 `WriteApprovalGate` 门控内存写入。 |
| `description_prefix` | `str` | `"Agent wants to"` | 人类可读操作描述的前缀。 |
| `kanban_recurrence_limit` | `int` | `5` | `KanbanTriage` 中触发 `RecurrenceLimitError` 的最大故障次数。 |

### 示例

```python
from agent.middlewares.HumanInTheLoop import HumanInTheLoop, HITLConfig, ApprovalMode

middleware = HumanInTheLoop(HITLConfig(
    mode=ApprovalMode.SMART,
    interrupted_tools={
        "terminal": {"allowed_decisions": ["approve", "reject"]},
        "memory": True,
    },
    write_approval_memory=True,
    kanban_recurrence_limit=3,
))
```

---

## 审批钩子系统

注册外部回调，每次审批决策后触发：

```python
def log_approval(session_id: str, result: ApprovalResult):
    print(f"[{session_id}] {result.decision}: {result.reason}")

middleware.register_approval_hook(log_approval)
```

钩子接收会话 ID 和完整的 `ApprovalResult`。所有钩子都包裹在 try/except 中——失败的钩子不会阻塞审批流程。

---

## 文件结构

```
agent/middlewares/HumanInTheLoop/
├── __init__.py        # 公开导出
├── types.py           # 枚举、数据类、配置、存根
├── detection.py       # 硬线 + 危险模式检测
├── approval.py        # 分层审批管道
├── gates.py           # 子门控（写入、中断、MCP、看板、配对、斜杠）
├── core.py            # HumanInTheLoop 中间件类
├── README.md          # 英文文档
└── README.zh.md       # 本文件（中文）
```
