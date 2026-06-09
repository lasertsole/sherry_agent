**中文** | [English](README.md)

---

# Subagent System

> EMA AI Agent 的层级任务分解与并行执行子系统。

## 概述

**Subagent System** 使 EMA AI Agent 能够将复杂任务分解，在后台并行执行子任务，并通过消息总线异步返回结果。它由两个核心层组成：

- **`SubagentManager`** — 单例编排器，管理后台子代理任务的生命周期。
- **`Commander`** — 按任务创建的 LangGraph 智能体，使用程序化编排方式调度工作。

## 架构

```
用户 / 主 Agent
       │
       ▼
┌──────────────────────────────────────┐
│          SubagentManager             │
│  (单例，生命周期编排器)                │
│                                      │
│  - spawn() → 创建后台任务             │
│  - _run_subagent() → 构建并运行       │
│  - cancel_by_session() → 清理         │
│  - start_service() → 事件循环         │
│  - _consume_loop() → 转发结果         │
└──────────┬───────────────────────────┘
            │ 创建
            ▼
┌──────────────────────────────────────┐
│           Commander Agent            │
│  (LangGraph, 按任务实例化)            │
│                                      │
│  工具:                               │
│  ┌──────────────┐  ┌──────────────┐ │
│  │  TodoWriter  │  │    Worker    │ │
│  │(todo.md 管理)│  │(并行执行)     │ │
│  └──────┬───────┘  └──────┬───────┘ │
│         │                 │          │
│  ┌──────┴─────────────────┴───────┐ │
│  │      程序化编排工具集             │ │
│  │  - program_generator            │ │
│  │  - program_runner               │ │
│  │  - program_interrupter          │ │
│  │  - program_resumer              │ │
│  └─────────────────────────────────┘ │
│                                      │
│  中间件:                             │
│  ┌──────────────┐  ┌──────────────┐ │
│  │TodoInjector  │  │TodoCleaner   │ │
│  │(模型调用前)   │  │(智能体结束后) │ │
│  └──────────────┘  └──────────────┘ │
└──────────────────────────────────────┘
```

## 模块结构

```
subagent/
├── __init__.py              # 导出: SubagentManager, subagent_manager
├── core.py                  # SubagentManager — 单例编排器
├── type.py                  # 数据模型: SubAgentOutput, ProgramExecutionResult, RecoveryResult
├── commander/
│   ├── __init__.py          # 导出: build_commander
│   ├── core.py              # build_commander() — 创建 LangGraph 智能体
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — 写入 todo.md 文件
│   │   ├── worker.py        # Worker — 并行子子代理调度
│   │   ├── program_generator.py   # ProgramGenerator — 生成执行程序
│   │   ├── program_runner.py      # ProgramRunner — 执行程序
│   │   ├── program_interrupter.py # ProgramInterrupter — 中断执行
│   │   ├── program_resumer.py     # ProgramResumer — 恢复执行
│   │   ├── cache_manager.py       # CacheManager — 任务缓存管理
│   │   ├── state_manager.py       # StateManager — 执行状态管理
│   │   └── worker_executor.py     # WorkerExecutor — 执行 Worker 任务
│   └── middlewares/
│       ├── __init__.py      # 导出: todo_injector_builder, todo_cleaner_builder
│       ├── todo_injector.py # 模型调用前中间件 — 注入 todo 状态
│       └── todo_cleaner.py  # 智能体结束后中间件 — 归档/删除 todo 文件
├── templates/
│   └── subagent_announce.md # 结果通知的 Jinja2 模板
├── README.md                # 英文版本
└── README.zh.md             # 本文件（中文）
```

## 数据模型

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]         # 任务成功/失败
    finish_reason: str                      # 完成原因（失败时包含错误详情）
    result: str                             # 输出或结果存储路径
```

### `ProgramExecutionResult`

```python
class ProgramExecutionResult(BaseModel):
    status: Literal["completed", "failed", "interrupted", "timeout", "error"]
    strategy_needed: Literal["fast_retry", "gentle_retry", "full_reset", None]
    failed_tasks: list[dict]
    completed_tasks: list[str]
    can_resume: bool
    recommendation: str
    stage: str
```

### `RecoveryResult`

```python
class RecoveryResult(BaseModel):
    status: Literal["resumed", "retrying", "reset", "error", "no_failed_tasks"]
    strategy: str
    failed_tasks: list[str]
    message: str
    can_resume: bool
```

## SubagentManager 生命周期

### 单例模式

`SubagentManager` 使用经典单例模式（`__new__` + `_instance` 守卫）。每次 `SubagentManager()` 调用都返回同一实例。`_initialized` 标志防止重复初始化。

### 事件循环管理

构造时：
1. 尝试 `asyncio.get_running_loop()` — 如果已有运行中的事件循环则复用。
2. 回退到 `asyncio.new_event_loop()` — 创建专用于后台任务的事件循环。

### 生成 → 执行 → 通知

```
spawn(task, session_id)
  │
  ├─ 生成 task_id (UUID，取前8字符)
  ├─ 创建 asyncio.create_task(_run_subagent(...))
  ├─ 在 _running_tasks 和 _session_tasks 中注册
  ├─ 注册 _cleanup 回调（任务完成时从跟踪中移除）
  └─ 返回 "已启动" 消息给调用方

_run_subagent(session_id, task_id, task, label)
  │
  ├─ 通过 build_commander(session_id, task_id) 构建 Commander 智能体
  ├─ agent.ainvoke({messages: [HumanMessage(task)]})
  │     └─ Commander 分解任务、调用工具、返回 SubAgentOutput
  ├─ 使用 subagent_announce.md 模板渲染结果
  ├─ 创建 InboundMessage (channel="system", metadata injected_event="subagent_result")
  └─ 发布到 MessageBus → consumer 转发给用户
```

### 取消

`cancel_by_session(session_id)` — 取消指定会话的所有运行中的后台任务，并通过 `asyncio.gather(return_exceptions=True)` 等待优雅关闭。

### 服务模式

`start_service()` 启动 `_consume_loop()`，其功能：
1. 等待总线上的 `InboundMessage`。
2. 通过角色人设提示词重新人格化结果（system prompt + chat model）。
3. 转发给注册的 `_consumer` 回调。

## Commander 智能体

### 构建

`build_commander(session_id, task_id)` — 确保 `{SESSIONS_DIR}/{session_id}/todo/` 存在，然后构建 LangGraph 智能体：

| 组件 | 详情 |
|------|------|
| **系统提示词** | 关于任务分解、程序化编排、todo 格式和恢复策略的全面指导 |
| **模型** | `chat_model`（整个 Agent 系统共享） |
| **检查点** | `InMemorySaver` — 在会话内保持对话状态 |
| **工具** | `todo_writer` + `worker` + `program_generator` + `program_runner` + `program_interrupter` + `program_resumer` |
| **中间件** | `SummarizationMiddleware`（15条消息触发，保留8条）+ `todo_injector`（模型调用前）+ `todo_cleaner`（智能体结束后） |
| **响应格式** | `SubAgentOutput` 结构化输出 |

### 系统提示词要点

Commander 的角色是"智能任务指挥官"，其行为：

1. **评估复杂度** — 简单任务直接使用 worker。复杂任务使用程序化编排工作流。
2. **分解** — 将工作分解为带有优先级、并行分组标识和清晰描述的子任务。
3. **程序化编排** — 不直接与 Worker 交互，而是生成 Python 可执行程序来编排多个 Worker：
   - 使用 asyncio.TaskGroup 并行执行
   - 顺序执行包装在 try-catch 中
   - 失败并行任务单独重试
   - 失败顺序任务阻塞下游任务
4. **跟踪** — 维护 todo.md 文件，包含状态、结果和进度统计。
5. **多级恢复** — 支持不同的恢复策略：
   - fast_retry: 不做任何修改重试失败任务（适用于超时/网络错误）
   - gentle_retry: 调整任务描述后重试（适用于语义/权限错误）
   - full_reset: 清除所有状态和缓存，从头开始
6. **处理中断** — 支持优雅中断和状态恢复。

## Commander 工具

### TodoWriter (`write_todo`)

- **用途**: 在会话的 todo 目录中写入/更新 `todo/{task_id}.md`。
- **行为**: 每次调用使用完整内容覆盖文件。
- **同步 + 异步**: 支持 `_run`（同步）和 `_arun`（异步）。

### Worker (`worker`)

- **用途**: 并发执行多个独立的子任务（用于简单任务）。
- **输入**: `WorkerArgs.worker_tasks: list[WorkerTask]`
  - 每个 `WorkerTask` 包含: `label`, `description`, `timeout_mins` (5-30, 默认5)。
- **执行模型**:
  - 每个子任务创建一个 `asyncio.create_task`。
  - 通过 `asyncio.gather` 并发运行。
  - 每个子任务智能体是完整的 LangGraph 智能体，具备：
    - Context Engine 集成（`assemble()` 用于记忆检索，`after_turn()` 用于经验抽取）。
    - `build_core_tools()` — 所有可用工具。
    - `SummarizationMiddleware`（20条消息触发，保留10条）。
    - `SubAgentOutput` 响应格式。
    - 通过 `asyncio.wait_for` 实现可配置超时。
- **结果**: 每个子任务返回从 `subagent_announce.md` 渲染的通知字符串。

### ProgramGenerator (`program_generator`)

- **用途**: 根据 todo 列表生成 Python 可执行程序。
- **核心特性**: 创建的程序可以编排多个 Worker 智能体：
  - 使用 asyncio.TaskGroup 并行执行
  - 顺序执行包装在 try-catch 中
  - 失败并行任务单独重试
  - 失败顺序任务阻塞下游任务
  - 成功输出: `print(f"SUCCESS: {label}")`
  - 失败输出: `print(f"FAILED: {label} - {error}")`
  - 缓存命中: `print(f"CACHED: {label}")`
- **输出**: 程序文件保存到 `todo/{task_id}_program.py`

### ProgramRunner (`program_runner`)

- **用途**: 执行生成的 Python 程序并解析输出。
- **输出格式**:
  - status: "completed" | "failed" | "interrupted"
  - strategy_needed: "fast_retry" | "gentle_retry" | "full_reset" | None
  - failed_tasks: [{"label": "...", "error": "..."}]
  - completed_tasks: ["...", "..."]
  - can_resume: true/false
  - recommendation: "..."

### ProgramInterrupter (`program_interrupter`)

- **用途**: 优雅地中断正在运行的程序执行。
- **输出**: 中断状态和用于恢复的保存状态。

### ProgramResumer (`program_resumer`)

- **用途**: 使用不同策略恢复中断的执行。
- **策略**:
  - continue: 从上一个检查点恢复
  - fast_retry: 不做任何修改重试失败任务（适用于超时/网络错误）
  - gentle_retry: 调整任务描述后重试（适用于语义/权限错误）
  - full_reset: 清除所有状态和缓存，从头开始

## Commander 中间件

### TodoInjector（模型调用前）

- **钩子**: `@before_model` — 每次模型调用前运行。
- **功能**: 读取 `todo/{task_id}.md` 并将其内容作为带有 `[SYSTEM CONTEXT - TODO LIST UPDATE]` 标签的 `HumanMessage` 注入。
- **跳过**: 如果 todo 文件不存在或无法读取，返回 `None`（无操作）。

### TodoCleaner（智能体结束后）

- **钩子**: `@after_agent` — 智能体完成运行后执行。
- **功能**: 清理 `todo/{task_id}.md` 文件。
- **模式**:
  - `"delete"` — 直接通过 `os.remove()` 删除文件。
  - `"archive"`（默认）— 通过 `shutil.move()` 移动到 `todo_archive/{task_id}_{timestamp}.md`。

### SummarizationMiddleware

- **触发条件**: 消息数量超过15条。
- **保留**: 缩减到最近的8条消息。
- **模型**: 使用相同的 `chat_model` 进行摘要。

## SubagentTool（外部接口）

位于 `tools/subagent.py` — 一个 LangChain `BaseTool`，允许主 Agent 生成子代理：

```python
class SubagentTool(BaseTool):
    name = "subagent"
    description = "为后台任务执行创建子代理。"

    async def _arun(self, task: str, label: str | None = None) -> str
```

- **仅异步**: `_run()` 抛出 `RuntimeError` 以防止同步调用导致死锁。
- **线程安全**: 使用 `asyncio.run_coroutine_threadsafe()` 将工作调度到 SubagentManager 的事件循环上。
- **需要运行中的事件循环**: 生成前检查 `event_loop.is_running()`。

## 通知模板

`templates/subagent_announce.md` 是一个 Jinja2 风格模板，渲染参数：

```markdown
[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}
finish_reason: {{ finish_reason }}
Result: {{ result }}

请以自然的口吻向用户总结。保持简洁（1-2句）。
不要提及"subagent"或任务ID等技术细节。
```

## 任务生命周期图

```
用户任务请求
       │
       ▼
主 Agent 调用 SubagentTool._arun()
       │
       ▼
SubagentManager.spawn()
  ├── 生成 task_id
  ├── 创建 asyncio 任务 (_run_subagent)
  └── 返回"已启动"给调用方
       │
       ▼
Commander 智能体 (LangGraph)
  ├── 步骤0: 评估复杂度
  ├── 步骤1: 写入 todo.md (TodoWriter)
  ├── 步骤2: 生成执行程序 (ProgramGenerator)
  ├── 步骤3: 执行程序 (ProgramRunner)
  │     └── Worker 1 ──► SUCCESS/FAILED/CACHED
  │     └── Worker 2 ──► SUCCESS/FAILED/CACHED
  │     └── Worker 3 ──► SUCCESS/FAILED/CACHED
  ├── 步骤4: 处理失败 (ProgramResumer)
  ├── 步骤5: 支持中断 (ProgramInterrupter)
  └── 返回 SubAgentOutput
       │
       ▼
SubagentManager._run_subagent()
  ├── 渲染通知模板
  ├── 在总线上创建 InboundMessage
  └── Consumer → 角色人设风格转发给用户
```

## 核心特性

### 程序化编排
Commander 不直接与 Worker 交互，而是生成 Python 可执行程序来编排多个 Worker。这样可以避免当多个 Worker 返回完整结果时导致的上下文膨胀。

### 多级恢复策略
- **快速重试 (fast_retry)**: 适用于超时/网络错误 - 不做任何修改重试失败任务
- **温和重试 (gentle_retry)**: 适用于语义/权限错误 - 分析错误并调整任务描述
- **完全重置 (full_reset)**: 适用于连续失败 - 清除所有缓存和状态，从头开始

### 缓存机制
成功的 Worker 会被缓存，除非程序重新生成，否则不会重新执行。

### 检查点与恢复
每个阶段完成后都会保存检查点以供恢复。用户可以随时中断，稍后恢复继续执行。

## 常见问题

### 为什么 SubagentManager 是单例？
后台任务必须全局跟踪，而非按会话跟踪。单例确保对取消、生命周期管理和事件循环有单一控制点。

### 为什么 SubagentTool 仅支持异步？
主 Agent 可能在不同线程中运行。同步调用会阻塞调用线程并带来死锁风险。`asyncio.run_coroutine_threadsafe()` 提供线程安全的调度。

### 如果子子代理超时会怎样？
`Worker` 工具将每个子任务包装在 `asyncio.wait_for()` 中。超时时，会渲染包含超时时长的失败通知。

### Commander 如何知道下一步做什么？
`TodoInjector` 中间件在每次模型调用前读取 `todo.md` 并注入为上下文，使 Commander 始终看到最新的计划状态。

### 我可以自定义 Commander 的行为吗？
可以 — `commander/core.py` 中的系统提示词是主要控制面。修改提示词可以改变分解策略、编排规则或恢复策略。

### 子子代理失败会怎样？
Commander 决定：重试（快速/温和）、跳过或完全重置。失败记录在 `finish_reason` 字段中，并体现在 todo.md 更新中。

### 结果如何传递给用户？
结果通过 `MessageBus` 作为带有 `injected_event: "subagent_result"` 的 `InboundMessage` 传递。`_consume_loop` 通过角色人设重新人格化消息后展示。

## 技术栈

| 层级 | 技术 |
|------|------|
| 智能体框架 | [LangGraph](https://github.com/langchain-ai/langgraph) (`CompiledStateGraph`) |
| LLM | `chat_model`（项目共享模型，通过 `.env` 配置） |
| 检查点 | `InMemorySaver`（内存型，会话内） |
| 中间件 | `@before_model` / `@after_agent` 装饰器 (`langchain.agents.middleware`) |
| 异步 | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| 数据校验 | Pydantic v2 (`BaseModel`, `Field`, `Literal`) |
| 模板 | 自定义 `render_template_file()`（Jinja2 风格） |
| 消息总线 | 项目内部 `MessageBus` / `InboundMessage` |
| 记忆系统 | Context Engine (`assemble()` / `after_turn()`) |