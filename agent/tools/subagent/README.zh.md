**中文** | [English](README.md)

---

# Subagent System

> 带经验知识图谱集成的层级任务分解与并行执行子系统。

## 概述

**Subagent System** 使 AI Agent 能够将复杂任务分解，在后台并行执行子任务，并通过消息总线异步返回结果。它具备**经验知识图谱（xp_graph）闭环**：草稿 → 蒸馏 → 写入 → 召回 → 组装注入。

核心层：

- **`SubagentManager`** — 单例编排器，管理后台子代理任务的生命周期。
- **`Commander`** — 按任务创建的 LangGraph 智能体，负责计划、分解和调度工作给 Worker。
- **Distiller** — 任务结束后蒸馏引擎，将可复用经验提取写入 xp_graph。
- **Draft 工具** — Agent 可调用的工具，用于在任务执行中记录关键发现。

## 架构

```
用户 / 主 Agent
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│                     SubagentManager                          │
│  (单例，生命周期编排器)                                       │
│                                                              │
│  _run_subagent() 流程:                                      │
│    1. 召回 xp_graph → 注入 AIMessage 到 Commander          │
│    2. Commander 执行任务 (工具: todo_writer, worker, draft) │
│    3. 发布结果到消息总线 (方案 C)                            │
│    4. 蒸馏经验写入知识图谱                                   │
│    5. 清理运行时寄存器                                       │
└──────────────────────────────────────────────────────────────┘
       │ 创建
       ▼
┌──────────────────────────────────────────────────────────────┐
│                      Commander 智能体                        │
│  (LangGraph, 按任务实例化)                                   │
│                                                              │
│  工具:                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
│  │TodoWriter│  │  Worker  │  │  Draft   │                  │
│  │(写入     │  │(并行调度)│  │(记录发现)│                  │
│  │ todo.md) │  │          │  │          │                  │
│  └──────────┘  └────┬─────┘  └──────────┘                 │
│                      │                                       │
│  中间件:              │                                       │
│  ┌───────────────┐   │                                       │
│  │Summarization  │   │                                       │
│  ├───────────────┤   │                                       │
│  │TODOManager    │   │                                       │
│  │(注入+归档)    │   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolCallNorm   │   │                                       │
│  ├───────────────┤   │                                       │
│  │IterationBudget│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolGuardrails │   │                                       │
│  └───────────────┘   │                                       │
└──────────────────────┼──────────────────────────────────────┘
                        │ 调度
                        ▼
                ┌────────────────┐
                │ Worker 智能体   │
                │ (codeact_agent)│
                │ Worker 智能体   │
                │ ... (并行)      │
                └────────────────┘
                        │
                        ▼ 任务结束后
┌──────────────────────────────────────────────────────────────┐
│              经验知识图谱 (xp_graph)                          │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  Draft 工具  │→ │  蒸馏器     │→ │  xp_graph   │         │
│  │(任务中记录  │  │(辅助LLM     │  │(节点/边     │         │
│  │ 关键发现)    │  │  提取经验)  │  │ 向量/FTS5)  │         │
│  └─────────────┘  └─────────────┘  └──────┬──────┘        │
│                                              │ 召回          │
│  ┌───────────────────────────────────────────┘               │
│  │  下次任务: 召回 → 组装 → 作为 AIMessage 注入              │
│  └───────────────────────────────────────────────────────────│
│                                                              │
│  DB 角色:                                                    │
│    default → store/xp_graph/xp_graph.db (策略级)            │
│    worker  → store/xp_graph/worker/xp_graph.db (操作级)     │
└──────────────────────────────────────────────────────────────┘
```

## 模块结构

```
subagent/
├── __init__.py              # 导出: build_subagent_tool
├── base.py                  # SubagentManager — 单例编排器 + 蒸馏流程
├── core.py                  # @tool subagent_tool — 异步生成接口
├── type.py                  # SubAgentOutput — pydantic 数据模型
├── draft.py                 # Draft @tool — 记录关键发现 + 辅助函数
├── distiller.py             # 蒸馏器 — 任务结束后经验蒸馏
├── commander/
│   ├── __init__.py          # 导出: build_commander
│   ├── core.py              # build_commander() — 创建 LangGraph 智能体
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — 写入 todo.md 文件
│   │   └── worker/
│   │       ├── core.py      # Worker — 并行子任务调度
│   │       └── middlewares/
│   │           └── WorkerSummarization.py
│   └── middlewares/
│       └── core.py          # TODOManager — 注入 + 归档 todo 上下文
├── templates/
│   └── subagent_announce.md # 结果通知的 Jinja2 模板
├── README.md
└── README.zh.md
```

## 经验知识图谱闭环

### 流程

```
1. 任务执行: Commander/Worker 调用 draft_tool → state_register_db
2. 任务完成: bus.publish → distill_and_ingest → Register.clear_all
3. 蒸馏: auxiliary_llm 从草稿+结果中提取节点/边
4. 写入: 策略级 → xp_graph("default"), 操作级 → xp_graph("worker")
5. 下次任务: recall(task) → assemble_context → AIMessage 注入
```

### Draft 工具

`draft` 是 Commander、Worker 和主 Agent 均可调用的 `@tool` 函数：

```python
@tool
def draft(
    key_points: str,
    category: Literal["strategy", "obstacle", "tool_pattern", "insight"],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

辅助函数（供蒸馏器使用）：
- `get_drafts(session_id)` — 读取所有草稿
- `append_drafts(session_id, drafts)` — 将 Worker 草稿合并到 Commander session
- `clear_drafts(session_id)` — 蒸馏后清空草稿

### 蒸馏器

`distill_and_ingest()` 在每次 subagent 任务结束后执行（方案 C 顺序）：

1. **策略级蒸馏** → `get_instance("default").ingest_experiences()`（Commander 层面的模式）
2. **操作级蒸馏** → `get_instance("worker").ingest_experiences()`（Worker 层面的技巧）

Worker 草稿在蒸馏前被合并到 Commander session 中。

### 知识图谱注入

在 `agent.ainvoke()` 之前，召回的经验以 `AIMessage` 注入：

```python
messages = [HumanMessage(content=task)]
# 从 xp_graph 召回
if recall_result["nodes"]:
    assembled = assemble_context(db, nodes, edges)
    messages.append(AIMessage(content=f"徊\n{system_prompt}\n\n{xml}\n徊"))
```

- **Commander**: 从 `xp_graph("default")` 召回（策略级）
- **Worker**: 从 `xp_graph("worker")` 召回（操作级）

## 数据模型

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]         # 任务成功/失败
    finish_reason: str                      # 完成原因（失败时包含错误详情）
    result: str                             # 输出或结果存储路径
```

## SubagentManager 生命周期

### 方案 C：发布 → 蒸馏 → 清理

Commander 执行完成后（成功、超时或异常）：

```
1. 发布结果到消息总线（用户立即收到通知）
2. distill_and_ingest()（草稿仍在 state_register_db 中）
3. Register.clear_all_register_sessions()（清理，草稿随之清除）
```

确保用户即时获取结果，同时草稿数据在蒸馏完成前不被清理。

### 生成 → 执行 → 通知

```
spawn(task, session_id)
  │
  ├─ 生成 task_id（基于时间戳）
  ├─ 创建 asyncio 任务 (_run_subagent)
  ├─ 在 _running_tasks 和 _session_tasks 中注册
  ├─ 注册 _cleanup 回调
  └─ 返回 "已启动" 消息

_run_subagent(session_id, task_id, task, label)
  │
  ├─ 召回 commander xp_graph → 构建 messages（含 AIMessage 知识注入）
  ├─ 构建 Commander 智能体
  ├─ agent.ainvoke({messages: [HumanMessage(task), AIMessage(knowledge)]})
  ├─ 使用 subagent_announce.md 模板渲染结果
  ├─ 发布 InboundMessage 到消息总线
  ├─ distill_and_ingest() → 提取经验写入知识图谱
  └─ Register.clear_all_register_sessions()
```

### 服务模式

`start_service()` 启动 `_consume_loop()`，其功能：
1. 等待总线上的 `InboundMessage`。
2. 通过角色人设提示词重新人格化结果。
3. 转发给注册的 `_consumer` 回调。

## Commander 智能体

### 构建

`build_commander()` 构建 LangGraph 智能体：

| 组件 | 详情 |
|------|------|
| **系统提示词** | 任务分解、并行化、动态计划调整、草稿记录 |
| **模型** | `main_llm`（项目共享模型） |
| **检查点** | `InMemorySaver` |
| **工具** | `todo_writer` + `worker` + `draft` |
| **中间件** | `SummarizationMiddleware`（15条触发，保留8条）+ `TODOManager` + `ToolCallNormalize` + `IterationBudget` + `ToolGuardrails` |
| **响应格式** | `SubAgentOutput` 结构化输出 |

## Commander 中间件

### TODOManager（替代了 TodoInjector + TodoCleaner）

- **`abefore_model`**: 读取 `todo/{task_id}.md` 并注入为 `[SYSTEM CONTEXT - TODO LIST UPDATE]`。
- **`aafter_agent`**: 归档 todo 文件到 `todo_archive/` 或删除。

### ToolCallNormalize

修复摘要裁剪消息后产生的孤立 tool_call。

### IterationBudget

限制每次任务的智能体迭代次数。

### ToolGuardrails

验证工具调用是否符合安全规则。

## Worker 智能体

Worker 是 `codeact_agent` 实例（非 LangGraph agent），具备：

- **工具**: `build_without_session_id_tools()`（除 subagent 特有工具外的所有工具，含 `draft`）
- **中间件**: `WorkerSummarization` + `HeartbeatStaleness` + `IterationBudget`
- **响应格式**: `SubAgentOutput`
- **xp_graph 注入**: 执行前从 `xp_graph("worker")` 召回操作级经验
- **草稿合并**: Worker 草稿在 `finally` 块中合并到 Commander session

## 常见问题

### 为什么蒸馏器从 xp_graph 移出？

`distiller.py` 原本在 `xp_graph/extractor/` 中，但它引用了 `draft.py`（subagent 层），形成了反向依赖：`xp_graph`（基础设施）→ `subagent`（业务层）。将蒸馏器移到 `subagent/` 使依赖方向变为单向：`subagent/distiller` → `xp_graph` ✓

### 为什么用方案 C（发布 → 蒸馏 → 清理）？

用户应即时获取结果。蒸馏需要 `state_register_db` 中的草稿数据，如果先 `Register.clear_all` 则草稿丢失。方案 C 兼顾两者：及时交付 + 完整蒸馏。

### 蒸馏失败怎么办？

蒸馏被 `try/except` 包裹，失败仅记录警告日志，不影响已发布给用户的结果。

### Worker 的草稿如何收集？

在 `_arun_task` 的 `finally` 块中，通过 `get_drafts(worker_session_id)` 读取 Worker 草稿，然后通过 `append_drafts(commander_session_id, ...)` 合并到 Commander session。蒸馏器统一从 Commander session 读取。

## 技术栈

| 层级 | 技术 |
|------|------|
| 智能体框架 | LangGraph (`CompiledStateGraph`) + codeact_agent |
| LLM | `main_llm`（共享），`auxiliary_llm`（蒸馏） |
| 检查点 | `InMemorySaver` |
| 中间件 | `@before_model` / `@after_agent` 装饰器 |
| 知识图谱 | `xp_graph`（SQLite + FTS5 + 向量搜索 + PageRank） |
| 异步 | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| 数据校验 | Pydantic v2 |
| 模板 | 自定义 `render_template_file()`（Jinja2 风格） |
| 消息总线 | 项目内部 `MessageBus` / `InboundMessage` |
| 状态管理 | `state_register_db`（SQLite），`state_register_mem`（内存） |
