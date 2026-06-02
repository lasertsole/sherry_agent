# EMA Cron — 定时任务服务

[**English**](README.md) | **中文文档**

EMA AI Agent 系统内的轻量级、基于文件的定时任务模块，支持周期任务、一次性任务和标准 Cron 表达式任务。

## 功能特性

- 三种调度类型：`at`（一次性）、`every`（固定间隔）、`cron`（Cron 表达式）
- 基于 `jobs.json` 文件的持久化存储，支持外部修改后自动重载
- 基于异步定时器的精确触发
- 每个任务独立记录最近 20 次执行历史
- 受保护的系统任务（无法通过 API 删除）
- Cron 表达式时区支持
- 支持将任务结果投递到外部渠道（如 QQ、WhatsApp）

## 模块结构

```
cron/
├── __init__.py    # 公开导出：CronService, cron_service, types
├── core.py        # 核心实现：CronService、任务执行、定时循环
├── types.py       # 数据模型：CronSchedule, CronPayload, CronJob 等
├── jobs.json      # 任务持久化存储（自动管理）
└── README.zh.md   # 本文件
```

## 类型说明

### CronSchedule

定义任务的运行时机。

| 字段       | 类型   | 说明 |
|-----------|--------|------|
| `kind`    | `"at" \| "every" \| "cron"` | 调度类型 |
| `at_ms`   | `int \| None` | 一次性执行的时间戳（毫秒） |
| `every_ms`| `int \| None` | 固定间隔（毫秒） |
| `expr`    | `str \| None` | Cron 表达式，如 `"0 9 * * *"` |
| `tz`      | `str \| None` | 时区，如 `"Asia/Shanghai"`，仅用于 cron 类型 |

### CronPayload

定义任务触发时的行为。

| 字段      | 类型            | 说明 |
|-----------|-----------------|------|
| `kind`    | `"system_event" \| "agent_turn"` | 负载类型 |
| `message` | `str`           | 发送给 Agent 的提示消息 |
| `deliver` | `bool`          | 是否将结果投递到外部渠道 |
| `channel` | `str \| None`   | 渠道名称（如 `"whatsapp"`、`"qq"`） |
| `to`      | `str \| None`   | 接收方标识 |

### CronJob

完整任务定义。

| 字段               | 类型            | 说明 |
|--------------------|-----------------|------|
| `id`               | `str`           | 唯一任务 ID（自动生成） |
| `name`             | `str`           | 人类可读的名称 |
| `enabled`          | `bool`          | 是否启用 |
| `schedule`         | `CronSchedule`  | 调度定义 |
| `payload`          | `CronPayload`   | 行为定义 |
| `delete_after_run` | `bool`          | 一次性任务执行后是否自动删除 |

## 公开 API

### `CronService`（通过 `cron_service` 单例访问）

| 方法 | 说明 |
|------|------|
| `start()` | 启动定时任务服务 |
| `stop()` | 停止定时任务服务 |
| `list_jobs(include_disabled=False)` | 列出所有任务 |
| `add_job(name, schedule, message, ...)` | 添加新任务 |
| `register_system_job(job)` | 注册受保护的系统任务 |
| `remove_job(job_id)` | 删除任务 |
| `enable_job(job_id, enabled=True)` | 启用/禁用任务 |
| `run_job(job_id, force=False)` | 手动触发任务 |
| `get_job(job_id)` | 获取任务详情 |
| `status()` | 获取服务状态 |

## 使用示例

```python
from cron import cron_service, CronSchedule

# 启动服务
await cron_service.start()

# 一次性任务：在指定时间执行
cron_service.add_job(
    name="morning_greeting",
    schedule=CronSchedule(kind="at", at_ms=1700000000000),
    message="Say good morning to the user",
    deliver=True,
    channel="qq",
    to="group_123456",
    delete_after_run=True,
)

# 固定间隔任务：每 30 分钟执行一次
cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

# Cron 任务：每天北京时间 9 点执行
cron_service.add_job(
    name="daily_digest",
    schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
    message="Summarize today's schedule and important events",
)

# 列出所有任务
jobs = cron_service.list_jobs()
for j in jobs:
    print(f"{j.name}: next run at {j.state.next_run_at_ms}")

# 手动触发任务
await cron_service.run_job("job_id_here", force=True)

# 删除任务
cron_service.remove_job("job_id_here")
```

## 任务持久化

所有任务持久化存储在 `jobs.json` 中。服务启动时自动加载，并通过文件修改时间监控实现自动重载——可直接编辑 `jobs.json` 批量添加或修改任务，服务将在下一个 tick 自动生效。

## 调度语义

| 类型 | 行为 |
|------|------|
| `at` | 在指定时间执行一次，执行后自动禁用（若 `delete_after_run=True` 则删除） |
| `every` | 每次执行后间隔固定时间再次触发 |
| `cron` | 使用 `croniter` 根据 Cron 表达式和时区计算下次执行时间 |

## 依赖

- `croniter` — Cron 表达式解析
- Python `zoneinfo` — 时区支持

## 注意事项

- 一次性（`at`）任务执行后默认**禁用**而非删除，如需自动删除请设置 `delete_after_run=True`
- 系统级任务（`payload.kind == "system_event"`）受保护，无法通过 `remove_job()` 删除
- 定时任务依赖 asyncio 事件循环，确保在调用 `await cron_service.start()` 时应用正在运行事件循环
