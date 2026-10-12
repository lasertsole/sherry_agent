# 计划：system-reminder 目录化（统一注入管理 + 预算检查 + 节流）

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：sherry 有 20 个中间件，其中多个各自直接拼字符串塞进 prompt——没有统一目录、没有注入预算、没有节流、没有统一格式。skills_listing 单项 26,597 字符，todo block 每轮都塞（不节流），注入总量无上限。**更严重的是 todo/boulder/taskflow 等动态 block 混在 SystemMessage 里 → 每轮重建 → SystemMessage 每轮变 → prompt cache（KV 缓存）从第一条消息开始全部失效**

## 现状

### 各中间件各自塞东西

| 中间件 | 注入什么 | 注入位置 | 频率 | 节流？ |
|---|---|---|---|---|
| `system_prompt_injection` | 系统 prompt（7 个 block） | SystemMessage | 每轮 | ❌ |
| `TaskIntentMiddleware` | 编排 steering prompt | HumanMessage | 首次触发 + armed 后每轮 short reminder | ✅ armed 标记 |
| `TodoContinuationEnforcer` | 续跑指令 | HumanMessage | 有未完成 todo 时每轮 | ❌ |
| `WorkspaceNoticeMiddleware` | 目录/git 切换通知 | HumanMessage | 切换时一次 | ✅ send-time 比对 |
| `SubagentCompletionDrainMiddleware` | 子代理结果 | HumanMessage | announce 时一次 | ✅ |
| `Summarization` | 压缩摘要对 | Human+AI pair | 压缩时一次 | ✅ |
| `prompt_builder._build_todo_block` | todo 列表 | SystemMessage block | **每轮** | ❌ **不节流** |
| `prompt_builder._build_boulder_block` | boulder 状态 | SystemMessage block | 每轮 | ❌ |
| `prompt_builder._build_taskflow_block` | TaskFlow 看板 | SystemMessage block | 每轮 | ❌ |
| `prompt_builder._build_continuity_block` | 连续性上下文 | SystemMessage block | 每轮 | ❌ |
| `prompt_builder._build_knowledge_block` | 知识库索引 | SystemMessage block | 每轮 | ❌ |

### 问题

1. **无注入预算**——skills_listing 26K + todo + boulder + taskflow + continuity + knowledge = 可能 > 50K 字符/轮，占 131K 窗口的 ~12%
2. **todo block 每轮都塞**——即使 todo 没变、即使模型在做无关的事
3. **无统一格式**——各中间件用不同前缀（`[SYSTEM DIRECTIVE]`、`<sherry-ulw-execute>`、无前缀），模型难以统一区分"系统塞的"和"用户说的"
4. **压缩器不知道什么该保留**——`_build_todo_block` 等在系统提示里（压缩时重建），但 HumanMessage 注入的 steering/continuation/notice 在对话流里——压缩器不知道哪些可丢
5. **无去重/频控**——同一内容可能被多个中间件重复注入
6. **动态 block 在 SystemMessage 里 → 破坏 prompt cache**——todo/boulder/taskflow 等动态内容混在 SystemMessage 里，每轮 todo 状态变了 → SystemMessage 字符串变 → 它是第一条消息 → 整个对话的 prefix cache 从第一条消息开始全部失效。persona（静态）被动态 block 连累，即使 persona 没变也要重算 KV

## 核心改动：动态 block 从 SystemMessage 迁移到 HumanMessage

### 为什么必须迁移

ZCode 的设计：SystemMessage 只放 stable 内容（identity + custom），动态内容（todo/task_status/reminder）作为 `<system-reminder>` 标签的 HumanMessage 注入到对话流里。

```
ZCode 的请求结构：
  [SystemMessage: identity + custom]           ← 永远不变 → cache 永远命中 ✅
  [对话历史：Human + AI + Tool]                  ← 正常对话
  [<system-reminder source="todo_reminder">]   ← 动态注入，在对话流里，不影响 SystemMessage cache ✅
  [HumanMessage: 用户当前消息]                   ← 真实用户输入
```

sherry 的现状：
```
  [SystemMessage: persona + todo + boulder + taskflow + ...]  ← todo 变了全变 → cache 全失效 ❌
  [对话历史]
  [HumanMessage: 用户当前消息]
```

**迁移后**：
```
  [SystemMessage: persona + memory + skills_listing]  ← stable，几乎不变 → cache 命中 ✅
  [对话历史]
  [<system-reminder source="todo_reminder">]          ← 动态 HumanMessage，节流后注入
  [<system-reminder source="taskflow_status">]        ← 动态 HumanMessage
  [HumanMessage: 用户当前消息]
```

SystemMessage 变成 stable（persona + memory + skills_listing），cache 永远命中。动态内容在对话流里注入 → 不影响 SystemMessage 的 cache。

### 哪些 block 迁移、哪些保留

| block | 现在在 SystemMessage 里 | 迁移到 HumanMessage？ | 理由 |
|---|---|---|---|
| persona（SOUL/AGENTS/USER/ROLE） | ✅ | ❌ **保留** | 静态，永不变 → cache 命中 |
| memory files | ✅ | ❌ **保留** | 半静态（压缩后才变）→ cache 大部分时间命中 |
| skills_listing | ✅ | ❌ **保留** | 半静态（加/删技能才变）→ cache 大部分时间命中 |
| project_dir | ✅ | ✅ **迁移** | 动态（切换目录时变） |
| todo block | ✅ | ✅ **迁移** | 动态（todo 状态每次操作变） |
| boulder block | ✅ | ✅ **迁移** | 动态 |
| taskflow block | ✅ | ✅ **迁移** | 动态（step 推进变） |
| knowledge block | ✅ | ✅ **迁移** | 动态 |
| continuity block | ✅ | ✅ **迁移** | 动态（每轮可能变） |

迁移后 SystemMessage = persona + memory + skills_listing（~10K 字符，stable）→ cache 永远命中。6 个动态 block 迁移到对话流。

### 迁移后的注入方式

动态 block 不再在 `prompt_builder.py` 里拼进 SystemMessage，而是通过 `before_model` 钩子注入为 HumanMessage：

```python
# 新中间件：DynamicReminderMiddleware（before_model）
# 替代 prompt_builder 里的 6 个动态 block

class DynamicReminderMiddleware(AgentMiddleware):
    """before_model: 注入动态 <system-reminder> 标签的 HumanMessage。"""

    async def abefore_model(self, state, runtime=None):
        session_id = state.get("session_id", "")
        messages = state.get("messages", [])
        if not messages:
            return None

        # 找最后一条真实用户消息
        last_human = None
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage) and not _is_system_reminder(msg):
                last_human = msg
                break
        if last_human is None or last_human is not messages[-1]:
            return None  # 不是 turn 开头 → 不注入

        # 解析所有动态注入源
        injections = resolve_injections(
            session_id, state, 
            current_turn=_get_turn_count(session_id),
            budget_chars=15_000,  # 动态注入预算（不含 SystemMessage）
        )
        if not injections:
            return None

        # 注入为 <system-reminder> 标签的 HumanMessage（在用户消息之前）
        reminder_msgs = []
        for source_id, content in injections.items():
            wrapped = wrap_reminder(source_id, content)
            reminder_msgs.append(HumanMessage(
                content=wrapped,
                metadata={"origin": "reminder_catalog", "internal": True, 
                         "provenance": source_id},
            ))

        # 在最后一条用户消息之前插入
        # LangGraph 的 before_model 返回 {"messages": [...]} 会追加到 messages 末尾
        # 需要在 before_agent 钩子里插入（在用户消息之前）
        return {"messages": reminder_msgs}
```

**关键**：注入的 HumanMessage 带 `metadata.internal=True` + `origin="reminder_catalog"` + `provenance=source_id`——和现有的 `TaskIntentMiddleware` 注入格式一致，消息存储的 `is_workspace_notice` 扫描器能跳过它们。

### todo_reminder

> **不作为 catalog 独立注入源**。todo_reminder 合并到 `TodoContinuationEnforcer`（改名 `PlanContinuationEnforcer`），在续跑指令后追加 todowrite-usage 提醒。详见 [计划_todo_reminder节流.md](./计划_todo_reminder节流.md)。

### 其他动态 block 的节流

| block | cooldown_turns | only_if_changed | 理由 |
|---|---|---|---|
| ~~todo_reminder~~ | — | — | **不走 catalog**，合并到 `TodoContinuationEnforcer`（详见 [独立计划](./计划_todo_reminder节流.md)）|
| taskflow_status | **5** | True | 距上次 TaskFlow 操作 ≥5 轮才提醒；内容没变不注入 |
| boulder_status | **5** | True | 同上 |
| project_dir_notice | 0 | True | 切换时才注入（builder 内部判断）；内容没变不注入 |
| continuity_context | 0 | True | 每轮检查但只在变化时注入 |
| knowledge_index | 0 | True | 同上 |

### prompt_builder.py 的改动

`build_system_prompt()` 只保留 stable block：

```python
# 现在：
blocks = [
    _build_project_dir_block(session_id),    # 迁移
    _build_todo_block(session_id),           # 迁移
    _build_boulder_block(session_id),        # 迁移
    _build_taskflow_block(session_id),       # 迁移
    _build_knowledge_block(session_id),      # 迁移
    _build_continuity_block(session_id),     # 迁移
]

# 改后：
# 只有 persona + memory + skills_listing 留在 SystemMessage
# 动态 block 全部由 DynamicReminderMiddleware 在 before_model 注入
blocks = []  # SystemMessage 里不再有动态 block
```

### 压缩后的恢复

**现在**：动态 block 在 SystemMessage 里 → 压缩后系统提示重建 → 动态 block 自动恢复（注释原文"survive context compression"）。

**迁移后**：动态 block 在对话流里 → 压缩时可能被丢掉。解决：

1. `reminder_catalog` 注册时声明 `lifecycle = "persisted"`（借鉴 ZCode）→ 压缩器保留这些注入
2. 或者：压缩后下一轮 `before_model` 重新注入（动态 block 是从 live state 读的，不依赖历史——即使旧注入被压缩掉，新轮重新注入即可）

**第二种更简单**——动态 block 的数据来自 live state（todos 表、boulder 文件、taskflow 表），不依赖对话历史里的旧注入。即使旧注入被压缩掉，下一轮 `before_model` 重新从 live state 读并注入即可。这和现在 SystemMessage 每轮重建的逻辑一样——只是注入位置从 SystemMessage 挪到 HumanMessage。

## 设计

### 核心机制：统一注入目录

新建 `agent/middlewares/reminder_catalog.py`（~200 行）：

```python
"""统一注入目录：每个注入点声明元数据，由目录统一调度、节流、预算检查。

不新建注入机制——给现有中间件的注入内容加一层统一目录。
中间件的业务逻辑不动，只改注入方式（从直接拼字符串 → 走目录注册）。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable
from enum import StrEnum


class ReminderChannel(StrEnum):
    """注入内容的 6 种通道。"""
    SYSTEM_PROMPT = "system_prompt"        # SystemMessage 里的 block（每轮重建）
    CURRENT_TURN = "current_turn"          # 当前轮的 HumanMessage 注入
    TOOL_RESULT = "tool_result"            # 工具返回后的追加提示
    HISTORY_CONTINUITY = "history_continuity"  # 压缩后保留的历史上下文
    MID_TURN_EVENT = "mid_turn_event"      # turn 中途的事件（如 WS 断连通知）
    REAL_USER = "real_user"                # 真实用户输入（不进注入预算）


@dataclass
class ReminderSource:
    """一个注入点的元数据声明。"""
    source_id: str                           # 唯一标识，如 "todo_reminder"
    channel: ReminderChannel
    builder: Callable[[str, dict], str | None]  # session_id + state → content | None
    budget_chars: int = 2000                  # 单项上限
    priority: int = 50                        # 100=最高（必保），0=最低（先丢）
    is_meta: bool = True                      # 元信息（非用户输入）
    cooldown_turns: int = 0                   # 节流：每 N 轮最多一次（0=不节流）
    only_if_changed: bool = False             # 只在内容变化时注入（缓存上次内容对比）


# 全局注册表
_SOURCES: dict[str, ReminderSource] = {}
_LAST_INJECTED: dict[str, dict[str, int | str]] = {}  # {session_id: {source_id: turn_count, content_hash}}


def register(source: ReminderSource) -> None:
    _SOURCES[source.source_id] = source


def resolve_injections(session_id: str, state: dict, *, current_turn: int, 
                       budget_chars: int = 30_000) -> dict[str, str]:
    """解析所有注入源，返回 {source_id: content}（已节流 + 预算检查后）。"""
    candidates = []
    
    for source in _SOURCES.values():
        # 1. 节流检查
        last = _LAST_INJECTED.get(session_id, {})
        if source.cooldown_turns > 0:
            last_turn = last.get(f"{source.source_id}_turn", 0)
            if current_turn - last_turn < source.cooldown_turns:
                continue  # 还在冷却期
        
        # 2. 构建 content
        content = source.builder(session_id, state)
        if not content or not content.strip():
            continue
        
        # 3. 变化检查（只在内容变化时注入）
        if source.only_if_changed:
            last_content_hash = last.get(f"{source.source_id}_hash", "")
            current_hash = str(hash(content))
            if current_hash == last_content_hash:
                continue  # 内容没变 → 跳过
        
        # 4. 截断到 budget_chars
        if len(content) > source.budget_chars:
            content = content[:source.budget_chars] + "…"
        
        candidates.append((source, content))
    
    # 5. 预算检查：按优先级排序，超预算时丢弃低优先级
    candidates.sort(key=lambda x: -x[0].priority)
    total = 0
    result = {}
    for source, content in candidates:
        if total + len(content) > budget_chars:
            logger.debug("Injection budget exceeded, skipped: {}", source.source_id)
            continue
        result[source.source_id] = content
        total += len(content)
        # 更新节流/变化缓存
        _LAST_INJECTED.setdefault(session_id, {})
        _LAST_INJECTED[session_id][f"{source.source_id}_turn"] = current_turn
        _LAST_INJECTED[session_id][f"{source.source_id}_hash"] = str(hash(content))
    
    return result


def wrap_reminder(source_id: str, content: str) -> str:
    """统一包装 + 嵌套标签转义。"""
    escaped = content.replace("<system-reminder>", "&lt;system-reminder&gt;")
    escaped = escaped.replace("</system-reminder>", "&lt;/system-reminder&gt;")
    return f'<system-reminder source="{source_id}">\n{escaped}\n</system-reminder>'


def clear_session(session_id: str) -> None:
    """会话结束时清理节流缓存。"""
    _LAST_INJECTED.pop(session_id, None)
```

### 注册现有注入点

```python
# agent/middlewares/reminder_catalog/__init__.py

from .core import register, ReminderSource, ReminderChannel

# ── SystemMessage 里保留的 stable block（不走目录，留在 prompt_builder）──
# persona（SOUL/AGENTS/USER/ROLE）+ memory + skills_listing
# 这三块几乎不变 → SystemMessage 前缀稳定 → prompt cache 命中

# ── HumanMessage 动态注入（从 SystemMessage 迁移出来）──
# 注意：todo_reminder 不走 catalog，合并到 TodoContinuationEnforcer。
# 详见 计划_todo_reminder节流.md

register(ReminderSource(
    source_id="taskflow_status",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_taskflow_block(sid),
    budget_chars=2000,
    priority=75,
    cooldown_turns=5,         # 距上次 TaskFlow 操作 ≥5 轮
    only_if_changed=True,     # 内容没变不注入
))

register(ReminderSource(
    source_id="boulder_status",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_boulder_block(sid),
    budget_chars=1000,
    priority=70,
    cooldown_turns=5,
    only_if_changed=True,
))

register(ReminderSource(
    source_id="project_dir_notice",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_project_dir_block(sid),
    budget_chars=500,
    priority=60,
    cooldown_turns=0,        # 切换时才注入（builder 内部判断）
    only_if_changed=True,
))

register(ReminderSource(
    source_id="continuity_context",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_continuity_block(sid),
    budget_chars=1000,
    priority=65,
    cooldown_turns=0,
    only_if_changed=True,
))

register(ReminderSource(
    source_id="knowledge_index",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_knowledge_block(sid),
    budget_chars=1000,
    priority=50,
    cooldown_turns=0,
    only_if_changed=True,
))

# ── 已有的 HumanMessage 注入（从各自中间件迁移到目录）──
register(ReminderSource(
    source_id="task_intent_steering",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_steering(sid, state),
    budget_chars=2000,
    priority=85,
    cooldown_turns=0,        # armed 后每轮 short reminder（当前行为）
))

register(ReminderSource(
    source_id="todo_continuation",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_continuation(sid, state),
    budget_chars=1500,
    priority=75,
    cooldown_turns=0,        # 有未完成 todo 时每轮（当前行为）
))

register(ReminderSource(
    source_id="workspace_notice",
    channel=ReminderChannel.CURRENT_TURN,
    builder=lambda sid, state: _build_notice(sid, state),
    budget_chars=500,
    priority=60,
    cooldown_turns=0,        # 切换时一次（builder 内部判断）
))
```

### 接入点

#### 1. system_prompt_injection 中间件

`build_system_prompt()` 的 7 个 block 改为走目录：

```python
# 现在：
blocks = [
    _build_project_dir_block(session_id),
    _build_todo_block(session_id),
    _build_boulder_block(session_id),
    _build_taskflow_block(session_id) if ... else "",
    _build_knowledge_block(session_id) if ... else "",
    _build_continuity_block(session_id) if ... else "",
]
# 改为：
injections = resolve_injections(session_id, state, current_turn=turn_count,
                                 budget_chars=30_000)
blocks = [injections[sid] for sid in [
    "todo_block", "boulder_block", "taskflow_block", 
    "continuity_block", "knowledge_block", "skills_listing"
] if sid in injections]
```

#### 2. 其他中间件的 HumanMessage 注入

`TaskIntentMiddleware`、`TodoContinuationEnforcer`、`WorkspaceNoticeMiddleware` 的注入改为走目录：

```python
# 现在：
return {"messages": [HumanMessage(content="[SYSTEM DIRECTIVE: ...]")]}

# 改为：
injections = resolve_injections(session_id, state, current_turn=turn_count)
if "task_intent_steering" in injections:
    content = wrap_reminder("task_intent_steering", injections["task_intent_steering"])
    return {"messages": [HumanMessage(content=content, metadata={"origin": "reminder_catalog", "internal": True})]}
```

#### 3. 预算检查接入

在 `before_model` 钩子里检查注入总量（已在 `resolve_injections` 内部做了）。

#### 4. 压缩器协调

`Summarization` 压缩时，识别 `<system-reminder source="...">` 标签：

```python
# 压缩时：SYSTEM_PROMPT channel 的内容在系统提示里（每轮重建，不压缩）
# CURRENT_TURN channel 的内容在对话流里——按 is_meta + priority 决定保留/压缩
# HISTORY_CONTINUITY channel 的内容优先保留
```

### todo block 节流（具体场景）

**现在**：`_build_todo_block(session_id)` 每轮执行 → todo 列表永远在系统提示里 → 每轮占 ~500-2000 字符。

**改后**：

```python
register(ReminderSource(
    source_id="todo_block",
    ...
    cooldown_turns=0,        # 仍然每轮（todo 是核心上下文，不该节流）
    only_if_changed=False,   # 每轮都注入
))
```

**但可以改成可配置**：

```python
# 如果用户想省 token：
"todo_reminder_cooldown_turns": 5,   # 每 5 轮注入一次

# 或只在变化时注入：
"todo_reminder_only_if_changed": True,  # todo 列表没变就不塞
```

这是**配置选择**，不是硬编码——用户可以在 `sherry.jsonc` 里调。默认保持"每轮"（当前行为，向后兼容）。

### 注入预算示例

```
30,000 字符预算（~7,500 token）

  todo_block         1,500 字符  priority=80  ✅ 保留
  taskflow_block     1,200 字符  priority=75  ✅ 保留
  boulder_block        300 字符  priority=70  ✅ 保留（only_if_changed）
  continuity_block     400 字符  priority=65  ✅ 保留
  skills_listing    8,000 字符  priority=60  ✅ 保留（截断到 8K）
  knowledge_block      200 字符  priority=50  ✅ 保留（only_if_changed）
  ────────────────────────────
  小计: 11,600 字符

  task_intent_steering 1,500 字符  priority=85  ✅ 保留
  todo_continuation   1,200 字符  priority=75  ✅ 保留
  workspace_notice      300 字符  priority=60  ✅ 保留
  ────────────────────────────
  小计: 3,000 字符

  date_change            50 字符  priority=10  ✅ 保留（低但便宜）
  
  总计: 14,650 字符 < 30,000 预算 → 全部保留
```

如果 skills_listing 涨到 50,000 字符（50+ 技能）：

```
  skills_listing    50,000 字符 → 截断到 8,000 字符（budget_chars）
  总计: 22,650 字符 < 30,000 → 仍然全部保留
```

如果再涨（skills 100+ + 多个中间件同时注入）：

```
  总计: 35,000 字符 > 30,000 预算
  → 按优先级丢弃：
     date_change (priority=10) 丢弃 → 省 50 字符
     knowledge_block (priority=50) 丢弃 → 省 200 字符
     仍然超 → skills_listing 从 8,000 截到 5,000 → 省 3,000 字符
  → 现在 31,750 < 30,000？不——继续丢...
```

**关键**：超预算时自动按优先级裁剪——不是无上限地塞。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/middlewares/reminder_catalog.py` | 新建：目录核心（注册 + 解析 + 节流 + 预算 + 包装） | ~200 |
| `agent/middlewares/reminder_catalog/__init__.py` | 新建：注册注入点 | ~80 |
| `agent/middlewares/dynamic_reminder.py` | 新建：DynamicReminderMiddleware（before_model 注入 HumanMessage） | ~80 |
| `workspace/prompt_builder.py` | 修改：SystemMessage 只保留 persona + memory + skills_listing；删除 6 个动态 block | ~30 改动 |
| `agent/middlewares/task_intent/core.py` | 修改：注入走目录 | ~10 改动 |
| `agent/middlewares/todo_continuation/core.py` | 修改：注入走目录；**合并 todo_reminder 逻辑 + 改名 `PlanContinuationEnforcer`**（详见 [独立计划](./计划_todo_reminder节流.md)）| ~30 改动 |
| `agent/middlewares/workspace_notice/core.py` | 修改：注入走目录 | ~10 改动 |
| `agent/core.py` | 修改：注册 DynamicReminderMiddleware | ~5 |
| `config/features/agent_side/reminder_catalog.py` | 新增：配置（预算 + 节流阈值） | ~15 |
| `tests/agent/middlewares/test_reminder_catalog.py` | 新增：节流 + 预算 + 变化检测 + cache 测试 | ~120 |

**合计 ~560 行**

## 验收

- SystemMessage 只含 persona + memory + skills_listing → 两轮之间内容相同 → prompt cache 命中
- todo_reminder 不在 SystemMessage 里 → SystemMessage 不因 todo 变化而变 → cache 不失效
- todo_reminder 合并到 `TodoContinuationEnforcer`，不走 catalog——详见 [计划_todo_reminder节流.md](./计划_todo_reminder节流.md)
- taskflow/boulder/knowledge 内容没变 → `only_if_changed` 跳过（省 token）
- 动态注入总量超 15,000 字符 → 按优先级丢弃低优先级项
- 所有动态注入统一 `<system-reminder source="...">` 包装 + 嵌套转义
- 压缩后动态注入丢失 → 下一轮 `before_model` 从 live state 重新注入（不依赖历史）
- 向后兼容：persona + memory + skills_listing 仍在 SystemMessage（当前行为）

## 不做的事

- **不改 persona/memory/skills_listing 的注入方式**——这三块保留在 SystemMessage 里（stable，cache 友好）
- **不改 system prompt 的 frozen 层**——persona（SOUL/USER/AGENTS.md）不走目录，始终在 SystemMessage 里
- **不做 prompt cache 分段**（§2.3）——本计划让 SystemMessage 变 stable 已是 cache 分段的第一步。stable/dynamic 分段可后续做
- **不强制所有中间件接入**——目录是叠加层，未注册的中间件照常工作。可以分批迁移
- **不做实时注入面板**——前端不展示注入目录。日志记录即可

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/middlewares/reminder_catalog.py` | 新建：目录核心 |
| `agent/middlewares/reminder_catalog/__init__.py` | 新建：注册注入点 |
| `agent/middlewares/dynamic_reminder.py` | 新建：before_model 注入动态 HumanMessage |
| `workspace/prompt_builder.py` | 修改：删除 6 个动态 block，只保留 stable |
| `agent/middlewares/task_intent/core.py` | 修改：注入走目录 |
| `agent/middlewares/todo_continuation/core.py` | 修改：注入走目录 |
| `agent/middlewares/workspace_notice/core.py` | 修改：注入走目录 |
| `agent/core.py` | 修改：注册 DynamicReminderMiddleware |
| `config/features/agent_side/reminder_catalog.py` | 新建：配置 |
| `tests/agent/middlewares/test_reminder_catalog.py` | 新建：测试 |
