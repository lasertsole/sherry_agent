# 与现有系统集成方案

## 现有系统分析

### 当前 subagent 架构 (`agent/tools/subagent/`)

```
SubagentManager (singleton)
  ├── 专用 event loop + daemon thread
  ├── spawn(task, session_id, label) → 后台 asyncio.Task
  ├── _run_subagent() → build_commander() → ainvoke() → bus.publish_inbound()
  ├── cancel_by_session() → 取消所有子任务
  └── _consume_loop() → 从 bus 消费 → LLM 个性化 → 转发给 consumer
```

**核心数据流**:
1. 主 agent 调用 `subagent` 工具 → `SubagentManager.spawn()`
2. 后台线程执行 Commander → Worker → 得到 `SubAgentOutput`
3. 通过 `MessageBus.publish_inbound()` 投递结果
4. `_consume_loop()` 消费结果 → LLM 个性化 → 转发给前端

### 现有基础设施

| 组件 | 路径 | 用途 |
|------|------|------|
| MessageBus | `bus/core.py` | 异步消息总线 |
| InboundMessage | `type/bus.py` | 入站消息模型 |
| Register | `runtime/` | 运行时状态注册 |
| build_commander | `agent/tools/subagent/commander/core.py` | 构建 Commander agent |
| codeact_agent | `agent/codeact/` | Worker agent |
| build_agent_config | `pub_func/` | 构建 agent 配置 |
| ThreadSafeAsyncSqliteSaver | `agent/checkpointer/` | SQLite checkpointer |
| StateSchema | `agent/core.py` | Agent state schema（含 session_id） |
| Middleware 体系 | `agent/middlewares/` | Summarization, ToolGuardrails 等 |

## 集成方案

### 1. 工具注册

在 `agent/tools/__init__.py` 中添加新工具 builder:

```python
from future_subagent import (
    build_sessions_spawn_tool,
    build_sessions_yield_tool,
    build_sessions_send_tool,
    build_agents_list_tool,
    build_subagents_list_tool,
    build_sessions_kill_tool,
    build_sessions_steer_tool,
)

_MAIN_TOOLS_BUILDERS: list[Callable[[], BaseTool]] = [
    # ... 现有工具 ...
    build_subagent_tool,          # 现有，保留
    build_sessions_spawn_tool,    # 新增
    build_sessions_yield_tool,    # 新增
    build_sessions_send_tool,     # 新增
    build_agents_list_tool,       # 新增
    build_subagents_list_tool,    # 新增
    build_sessions_kill_tool,     # 新增
    build_sessions_steer_tool,    # 新增
]
```

### 2. Agent 构建注入

新 subagent 系统需要在 `agent/core.py` 中初始化:

```python
from future_subagent.registry.sweeper import start_sweeper
from future_subagent.registry.state import init_registry

async def built_agent(temperature=0.8):
    # ... 现有初始化 ...

    # 初始化新 future_subagent 系统
    await init_registry()          # 加载 SQLite 持久化数据
    await start_sweeper()          # 启动后台扫描器
```

### 3. 子 Agent 构建

新 subagent 系统的子 agent 构建策略:

```python
# spawn/core.py 中的子 agent 构建

async def _build_child_agent(
    system_prompt: str,
    tools: list | None,
    tool_allow: list[str],
    tool_deny: list[str],
    role: SubagentSessionRole = SubagentSessionRole.LEAF,
) -> CompiledStateGraph:
    """构建子 agent，复用项目现有基础设施。"""
    from agent.core import StateSchema
    from agent.checkpointer import build_async_sqlite_checkpointer
    from agent.tools import build_main_tools

    # 获取全部工具，然后按 allow/deny 过滤
    all_tools = build_main_tools()
    filtered_tools = apply_tool_policy(all_tools, tool_allow, tool_deny)

    # ORCHESTRATOR 用 main_llm（强模型做任务分解），LEAF 用 auxiliary_llm（轻量执行）
    if role == SubagentSessionRole.ORCHESTRATOR:
        child_llm = build_main_llm()
    else:
        child_llm = build_auxiliary_llm()

    child_checkpointer = await build_async_sqlite_checkpointer()
    await child_checkpointer.setup()

    child_agent = create_agent(
        model=child_llm,
        state_schema=StateSchema,
        checkpointer=child_checkpointer,
        tools=filtered_tools,
        middleware=[
            Summarization(trigger=[fraction:0.5, messages:40, tokens:30000]),
            IterationBudget(60),
            ToolGuardrails(),
            ToolCallNormalize(),
            HeartbeatStaleness(),
        ],
    )
    return child_agent
```

### 4. Announce 投递适配

新 subagent 系统的 announce 投递支持双路径路由：

```python
# announce/delivery.py 中的双路径投递

async def deliver_subagent_announcement(ctx: DeliveryContext) -> None:
    from bus import MessageBus
    from type.bus import InboundMessage

    # 根据 ctx.is_requester_subagent 分流
    if ctx.is_requester_subagent:
        # 子→子：内部注入格式
        msg = InboundMessage(
            channel="system",
            sender_id="subagent_internal",
            chat_id="direct",
            content=f"[Subagent Internal] {label}: {status}\n{summary}",
            session_id=ctx.requester_session_key,
            metadata={
                "injected_event": "subagent_internal_update",
                "subagent_run_id": ctx.run_id,
                "internal": True,
            },
        )
    else:
        # 子→用户：完整 completion message
        msg = InboundMessage(
            channel="system",
            sender_id="future_subagent",
            chat_id="direct",
            content=formatted_result,
            session_id=ctx.requester_session_key,
            metadata={
                "injected_event": "subagent_result",
                "subagent_run_id": ctx.run_id,
            },
        )

    bus = MessageBus()
    await bus.publish_inbound(msg)
```

### 5. 共存策略

| 维度 | 现有 subagent | 新 subagent |
|------|--------------|-------------|
| 工具名 | `subagent` | `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list` |
| 管理器 | `SubagentManager` (singleton) | `SubagentRegistry` (dict + SQLite) |
| 投递 | `MessageBus` | `MessageBus`（共用） |
| 子 agent | Commander + Worker | 直接 spawn LangGraph agent |
| 知识图谱 | 有（draft→distill→ingest） | 暂无 |
| 深度 | 单层 | 多层嵌套（默认 3 层） |
| 通信 | 单向回传 | 双向（sessions_send） |
| Kill | cancel_by_session | kill + cascade + arbitration |
| Steer | — | sessions_steer + restart |
| Kill 仲裁 | — | Kill↔completion arbitration |
| 角色体系 | — | MAIN / ORCHESTRATOR / LEAF |

### 6. 后续迁移路径

当新 subagent 系统稳定后:
1. 将现有 `subagent` 工具的内部实现替换为新系统
2. 将经验知识图谱闭环集成到新系统
3. 废弃 `SubagentManager` singleton
4. 统一工具名为 OpenClaw 风格

### 7. 配置合并

新 subagent 配置可合并到现有配置体系:

```yaml
# config.yaml 新增节点
subagent:
  max_spawn_depth: 3
  max_children_per_agent: 5
  run_timeout_seconds: 300
  allow_agents: ["*"]
  announce_retry_max: 3
  sweeper_interval_seconds: 60
  orphan_recovery_delay_seconds: 120
  announce_expiry_ms: 7200000
  announce_hard_expiry_ms: 86400000
  max_announce_retry_count: 10
  stale_unended_threshold_seconds: 7200
  steer_rate_limit_ms: 2000
  archive_after_minutes: 1440
  recent_ended_window_seconds: 1800
  delivery_suspend_soft_cap: 25
  delivery_suspend_hard_cap: 50
  delivery_suspend_target: 10
  lifecycle_grace_period_seconds: 15.0
  attachments_enabled: true
  attachments_max_files: 50
  attachments_max_file_bytes: 1048576
  attachments_max_total_bytes: 5242880
```

读取方式:
```python
from future_subagent.config import SubagentConfig

config = SubagentConfig()  # 默认值
# 或从项目配置加载:
# config = SubagentConfig(**load_config().get("future_subagent", {}))
```
