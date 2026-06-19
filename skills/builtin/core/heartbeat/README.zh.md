# Heartbeat — 定时任务检查服务

[**English**](README.md) | **中文文档**

> **Heartbeat** 是 EMA AI Agent 的定时心跳服务，定期唤醒 Agent 检查 `HEARTBEAT.md` 中是否有待处理任务，并自动执行与通知。

---

## 设计动机

Agent 完成对话后可能进入空闲状态，但外部可能有：
- 等待执行的后台任务（异步工具调用结果）
- 需要定期检查的监控任务
- 需要继续推进的长期工作

Heartbeat 模块提供一个**轻量级定时轮询机制**，让 Agent 在空闲时也能主动工作。

---

## 架构

```
┌─────────────────────────────────────┐
│          HeartbeatService            │
├─────────────────────────────────────┤
│  定时循环 (每 N 秒 tick)              │
│  ├─ Phase 1: 读取 HEARTBEAT.md       │
│  ├─ Phase 2: LLM 决策 (skip/run)     │
│  └─ Phase 3: 执行 + 通知门控          │
└─────────────────────────────────────┘
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `core.py` | 主服务：定时循环、LLM 决策、任务执行触发 |
| `evaluate.py` | 通知门控：判断执行结果是否值得推送给用户 |

---

## 工作流程

```
定时器触发 (默认 30 分钟)
     ↓
读取 HEARTBEAT.md
     ↓
LLM (tool-call) 决策:
  ├─ "skip" → 无任务，静默等待下一 tick
  └─ "run" → 通过 on_execute 回调执行任务
                   ↓
              evaluate_response():
                ├─ True  → on_notify 推送结果给用户
                └─ False → 静默（例程检查、无新内容）
```

### Phase 1: 读取

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

`HEARTBEAT_PATH` 在 `config.py` 中配置，指向项目中的 `HEARTBEAT.md` 文件。文件缺失或为空时跳过本次 tick。

### Phase 2: 决策

通过**虚拟 tool-call** 让 LLM 判断是否有活跃任务，避免了自由文本解析的不确定性：

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "parameters": {
            "action": {"enum": ["skip", "run"]},
            "tasks": {"type": "string"},  # run 时附带任务摘要
        },
        "required": ["action"],
    },
}]
```

LLM 返回 `skip` → 无操作；返回 `run` → 进入 Phase 3。

### Phase 3: 执行 & 通知门控

```python
if action == "run" and self.on_execute:
    response = await self.on_execute(tasks)           # 执行任务
    should_notify = evaluate_response(response, tasks) # 评估是否通知
    if should_notify and self.on_notify:
        await self.on_notify(response)                 # 推送给用户
```

`evaluate_response()` 通过独立的 LLM tool-call 判断响应是否包含**可操作信息**（错误、交付物、用户关心的结果），不通知例程性状态更新。

---

## 使用示例

### 基础用法

```python
from skills.builtin.core.heartbeat import heartbeat_service

# 配置回调
heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier  # async (response: str) -> None

# 启动（默认 30 分钟间隔）
await heartbeat_service.start()
```

### 手动触发

```python
result = await heartbeat_service.trigger_now()
if result:
    print(f"Task result: {result}")
```

### 自定义配置

```python
from skills.builtin.core.heartbeat import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 分钟
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

### 停止

```python
heartbeat_service.stop()
```

---

## 配置项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `interval_s` | 1800 (30 分钟) | 两次 tick 之间的间隔 |
| `enabled` | True | 是否启用心跳 |
| `timezone` | None | LLM 决策时使用的时区（如 "Asia/Shanghai"） |
| `HEARTBEAT_PATH` | 见 config.py | HEARTBEAT.md 文件路径 |

---

## 通知门控策略

`evaluate_response()` 的决策逻辑：

| 应通知 | 不通知 |
|--------|--------|
| 有错误或异常 | 例程检查无异常 |
| 任务交付物完成 | 确认一切正常 |
| 用户明确要求提醒的信息 | 响应为空或无关内容 |

失败时默认 `True`（通知），确保重要消息不被静默丢弃。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 运行时 | Python asyncio |
| LLM 决策 | `simple_chat_model`（bind_tools） |
| 文件读取 | pathlib |
| 配置 | `config.HEARTBEAT_PATH` |
