# Heartbeat — 定时任务检查服务

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat** 是 EMA AI Agent 的定时唤醒服务：每个 tick 读取 [`workspace/HEARTBEAT.md`](../../../../workspace/HEARTBEAT.md)，由辅助 LLM 判断是否存在活跃任务，若有则通过一次专门的 Agent 运行来执行任务，并将结果经通知门控推送。

---

## 设计动机

Agent 完成对话后可能进入空闲状态，但外部仍有待处理的工作：
- 等待执行的任务（由 Agent 或用户写入 HEARTBEAT.md）
- 需要定期检查的监控任务
- 需要继续推进的长期工作

Heartbeat 提供一个**轻量级定时轮询机制**，让 Agent 在空闲期间也能主动工作。

---

## 架构

```
┌──────────────────────────────────────────┐
│            HeartbeatService              │
├──────────────────────────────────────────┤
│  asyncio loop (sleep interval_s → tick)  │
│  ├─ Phase 1: Read HEARTBEAT.md           │
│  ├─ Phase 2: LLM decision (skip/run)     │
│  └─ Phase 3: Execute + notification gate │
└──────────────────────────────────────────┘
```

### 模块职责

| 文件 | 职责 |
|------|------|
| [`scripts/base.py`](scripts/base.py) | `HeartbeatService` 类：asyncio 循环、LLM 决策（`_decide`）、tick 流水线；模块级 `heartbeat_service` 单例 |
| [`scripts/core.py`](scripts/core.py) | HEARTBEAT.md 管理：`ensure_heartbeat_file_exists`、`add_task_to_heartbeat`、`list_active_tasks`、`list_completed_tasks`、`move_task_to_completed`、`remove_tasks_from_completed` / `clear_completed_tasks` |
| [`scripts/evaluate.py`](scripts/evaluate.py) | `evaluate_response()`：通知门控，判断结果是否值得推送 |
| [`server/service/heartbeat.py`](../../../../server/service/heartbeat.py) | 集成层：`process_heartbeat_task`（执行 Agent）、`process_heartbeat_notify`（渠道投递）、文件读写辅助函数 |
| [`server/trigger/channels/core.py`](../../../../server/trigger/channels/core.py) | 绑定 `on_execute` / `on_notify`，并在渠道管理器的事件循环上启动服务 |

---

## HEARTBEAT.md 文件

- 位于 `workspace/HEARTBEAT.md` — 即 [`config/path.py`](../../../../config/path.py) 中的 `HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"`。
- 若文件不存在，`ensure_heartbeat_file_exists()` 会将语言无关的模板 `workspace/template/HEARTBEAT.md` 复制过去。
- 骨架格式（与 `workspace/HEARTBEAT.md` 一致）：

```markdown
# Heartbeat Tasks

## Active Tasks

## Completed
```

解析规则（实现在 `scripts/core.py`）：
- 各小节通过**整行精确匹配** `## Active Tasks` / `## Completed` 定位（找不到小节会抛出 `ValueError`）。
- 小节的**内容行**指非空行，且不以 `<!--`（HTML 注释）开头，统计到下一个 `##` 标题或文件末尾为止。
- 任务是 Markdown 列表项；`add_task_to_heartbeat()` 会给不以 `-` 开头的文本加上 `- [ ] ` 前缀。
- 服务端写入 API 另有 `HEARTBEAT_MAX_CONTENT_LENGTH = 2000` 字符的任务文本上限（标题、空行和 `- ` 标记不计入，与 `heartbeat_content_length()` 保持一致）。

---

## 工作流程

```
start() → asyncio task
   └─ loop: sleep(interval_s) → tick()   # first tick happens after one full interval
        ↓
   Read HEARTBEAT.md (empty/missing → skip tick)
        ↓
   _decide() — auxiliary LLM, virtual tool call:
     ├─ "skip" → log OK, wait for next tick
     └─ "run"  → on_execute(tasks)         # server: one-shot main-LLM agent
                    ↓
              response non-empty → evaluate_response():
                ├─ True  → on_notify(response)   # server: channel delivery
                └─ False → silenced (logged)
```

### Phase 1: 读取

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

- 文件为空 → 跳过本次 tick（debug 日志）。
- 文件缺失 → `read_text()` 抛出 `FileNotFoundError`；循环记录错误并继续下一个周期。

### Phase 2: 决策（`_decide`）

辅助 LLM（来自 `models` 的 `build_auxiliary_llm()`）收到当前时间（`current_time_str(self.timezone)`）和完整的 HEARTBEAT.md 内容，通过**虚拟 tool-call** 给出结论，避免了不可靠的自由文本解析：

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "description": "Report heartbeat decision after reviewing tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["skip", "run"],
                    "description": "skip = nothing to do, run = has active tasks",
                },
                "tasks": {
                    "type": "string",
                    "description": "Natural-language summary of active tasks (required for run)",
                },
            },
            "required": ["action"],
        },
    },
}]
```

- 首选 `bind_tools` 路径；`tool_calls` 为空时按 `skip` 处理。
- 出现 `NotImplementedError`（例如不支持工具调用的本地 GGUF 分支）或其他任何异常时，回退到 `with_structured_output(_HeartbeatDecision)`（Pydantic 模型，`action` 字段受 `^(skip|run)$` 模式约束）。若回退也失败，则默认返回 `("skip", "")`。

### Phase 3: 执行与通知门控

`_tick()` 的实际逻辑（`scripts/base.py`）：

```python
action, tasks = self._decide(content)
if action != "run":
    return  # "Heartbeat: OK (nothing to report)"

if self.on_execute:
    response: str = await self.on_execute(tasks)
    if response:
        should_notify: bool = evaluate_response(response, tasks)
        if should_notify and self.on_notify:
            await self.on_notify(response)
        # else: "Heartbeat: silenced by post-run evaluation"
```

- `run` → `on_execute(tasks)` 执行任务；只有**非空**响应才会交给 `evaluate_response()` 评估；只有评估为真才会到达 `on_notify()`。
- 整个 tick 包裹在 `try/except` 中：异常被记录（`logger.exception`），循环继续运行。

---

## HEARTBEAT.md 任务管理 API（`scripts/core.py`）

面向 Agent 的函数，通过 [SKILL.md](SKILL.md) 暴露给模型：

| 函数 | 行为 |
|---|---|
| `ensure_heartbeat_file_exists()` | 文件不存在时，将 `workspace/template/HEARTBEAT.md` 复制为 `workspace/HEARTBEAT.md` |
| `add_task_to_heartbeat(task_text, index=None)` | 在 `## Active Tasks` 下添加任务；非列表项文本会加上 `- [ ] ` 前缀；`index` 为该小节内容行中的 0 基插入位置（越界抛出 `IndexError`）；`None` 追加到末尾 |
| `list_active_tasks()` / `list_completed_tasks()` | 返回 `## Active Tasks` / `## Completed` 的内容行 |
| `move_task_to_completed(task_text)` | 按子串匹配（去首尾空格后）Active Tasks 中的行；移除第一个匹配行并追加到 `## Completed` 末尾（若该小节为空则紧跟标题之后）；无匹配 → `ValueError` |
| `remove_tasks_from_completed(task_text=None)` | `None` → 移除**全部**内容行；`str` / `list[str]` → 子串匹配并移除匹配行（零匹配 → `ValueError`）；之后压缩小节内连续空行 |
| `clear_completed_tasks(task_text=None)` | `remove_tasks_from_completed` 的别名 |

以上函数均从 `skills.builtin.core.heartbeat.scripts` 导出；包 `skills.builtin.core.heartbeat` 本身只重新导出 `heartbeat_service` 单例。

---

## 服务端集成

服务由渠道层 `server/trigger/channels/core.py` 负责绑定和启动：

```python
heartbeat_service.on_execute = _process_heartbeat_task   # → server.service.process_heartbeat_task
heartbeat_service.on_notify = _process_heartbeat_notify  # → server.service.process_heartbeat_notify
asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)  # channel manager loop
```

**执行（`process_heartbeat_task`）**：
1. `ensure_workspace_system_files()` 确保核心人设文件存在。
2. 构建一次性 `create_agent(model=build_main_llm(), tools=[python_repl, read_file, write_file])`，系统提示词为核心人设（`build_system_prompt(selected_file_names=CORE_SYSTEM_FILE_NAMES)`），任务摘要作为 `HumanMessage`。
3. 取最后一条消息的内容作为结果。
4. 将已执行的任务从 Active 移到 Completed：先 `move_task_to_completed(task)`；若抛出 `ValueError`（任务文本漂移），则回退为移动**所有**剩余活跃任务。
5. 向会话 `default` 推送两个尽力而为的 WebSocket 事件：`heartbeat:updated`（刷新后的文件内容）和 `notification`（以 `heartbeat: ` 为前缀的结果）。失败只记录日志、绝不抛出。内部异常时函数返回 `"Error occurred: {e}"`。

注意分层：上述 WebSocket 事件由 `process_heartbeat_task` 在**每次**成功运行后推送，而**渠道投递**（下文）才是 `evaluate_response()` 门控真正控制的对象。

**投递（`process_heartbeat_notify`）**：读取 `plugins/channels/config.json`；配置中 `"heartbeat": true` 且能解析出 `receiver` 的渠道（来自 `plugins/channels/<name>/config.json`，回退到根配置块）都会通过 `channel_manager.get_channel(name).send(OutboundMessage(...))` 收到结果。

**HTTP API**（`server/trigger/http/heartbeat.py`）：`GET /heartbeat` 返回 `{"HEARTBEAT.md": "<content>"}`（文件缺失时为空字典）；`PUT /heartbeat` 接受 `{"file_to_content": {"HEARTBEAT.md": "..."}}` 并强制执行 2000 字符任务文本上限。

---

## 使用示例

### 基础用法（单例）

```python
from skills.builtin.core.heartbeat import heartbeat_service

heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier        # async (response: str) -> None

await heartbeat_service.start()  # 默认间隔：1800 秒（30 分钟）
```

生产环境中这套绑定位于 `server/trigger/channels/core.py`，运行在渠道管理器的事件循环上。

### 手动触发

```python
result = await heartbeat_service.trigger_now()
```

`trigger_now()` 读取文件、运行 `_decide`，若结果为 `run` 则等待 `on_execute(tasks)` 完成。它**不会**经过通知门控，也**不会**调用 `on_notify`；文件为空、决策为 `skip` 或未设置 `on_execute` 时返回 `None`。

### 自定义配置

```python
from skills.builtin.core.heartbeat.scripts.base import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 分钟
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

（`HeartbeatService` 类定义在 `scripts/base.py`；包的 `__init__.py` 并未重新导出它。）

### 停止

```python
heartbeat_service.stop()  # 置 _running = False 并取消 asyncio 任务
```

---

## 配置项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `interval_s` | `30 * 60`（1800 秒） | 两次 tick 的间隔秒数；循环**先 sleep 再 tick**，因此首次检查发生在 `start()` 后一个完整间隔 |
| `enabled` | `True` | 为 `False` 时，`start()` 记录 "Heartbeat disabled" 并直接返回 |
| `timezone` | `None` | 传给 `current_time_str()`，用于决策提示词中的 "Current Time" 行 |
| `on_execute` / `on_notify` | `None` | 异步回调；未设置时跳过执行 / 投递 |
| `HEARTBEAT_PATH` | `workspace/HEARTBEAT.md` | 定义于 `config/path.py` |
| `HEARTBEAT_TEMPLATE_PATH` | `workspace/template/HEARTBEAT.md` | `ensure_heartbeat_file_exists()` 的模板来源 |
| `HEARTBEAT_MAX_CONTENT_LENGTH` | `2000` | 服务端写入 API（`write_heartbeat_file`）强制的任务文本上限 |

---

## 通知门控策略

`scripts/evaluate.py` 中的 `evaluate_response(response, task_context)` 通过虚拟 `evaluate_notification` 工具（`should_notify` 布尔值，必填；`reason` 字符串）询问辅助 LLM。其系统提示词：

| 通知（`should_notify: true`） | 抑制（`should_notify: false`） |
|--------------------------------|-----------------------------------|
| 可操作的信息 | 例行状态检查、无新内容 |
| 错误 | 确认一切正常 |
| 已完成的交付物 | 基本为空的响应 |
| 用户明确要求提醒的事项 | |

失败行为：未返回工具调用或发生任何异常 → **`True`（通知）**，确保重要消息不会被静默丢弃。与 `_decide` 不同，这里没有 `with_structured_output` 回退。

---

## 勿混淆：`HeartbeatStaleness` 中间件

[`agent/middlewares/heartbeat_staleness.py`](../../../../agent/middlewares/heartbeat_staleness.py) 与本服务同名（"heartbeat"），但属于**完全不同的子系统**：它是针对单轮对话中卡死 Agent 的看门狗。它在 `before_agent` 中通过 `timer_call_register` 启动 1 分钟定时器，跟踪 `(heartbeat_iter, heartbeat_tool)` 进度；空闲状态下连续 7 个周期、或工具执行中连续 20 个周期无进展，则将本轮标记为 killed，使下一次模型/工具调用抛出 `HeartbeatTimeoutError`。它不读取 HEARTBEAT.md，也不属于本服务。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 运行时 | Python asyncio（通过 `asyncio.create_task` 启动单个 `asyncio.Task` 循环） |
| 决策与门控 | 辅助 LLM（`models` 的 `build_auxiliary_llm()`），LangChain 虚拟 tool-call（`bind_tools`）；`_decide` 中有 `with_structured_output` 回退 |
| 文件 I/O | `pathlib` |
| 日志 | `loguru` |
| 校验 | Pydantic（`_HeartbeatDecision` 回退模型） |
| 路径 | `config.path`（`HEARTBEAT_PATH`、`HEARTBEAT_TEMPLATE_PATH`） |
