# EMA Cron — 定时任务服务

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent 系统内的轻量级、基于文件的定时任务服务，支持一次性任务、固定间隔任务和 Cron 表达式任务。任务持久化到项目根目录的 `cron_jobs.json`，由独立的后台服务执行，结果通过消息总线投递到已启用的渠道。

## 功能特性

- 三种调度类型：`at`（一次性）、`every`（固定间隔）、`cron`（Cron 表达式，基于 `croniter`）
- 基于 `cron_jobs.json`（项目根目录）的文件持久化，外部修改后自动重载
- 独立的后台服务线程（拥有自己的 asyncio 事件循环）；自续订定时器精确唤醒到最早的到期任务
- 任务执行会启动一个专属 Agent（主 LLM + 系统提示词 + Python REPL / 读文件 / 写文件工具）
- 结果通过 `MessageBus` 入站队列投递到渠道，并向浏览器 UI 推送尽力而为的 WebSocket `notification` 事件
- 每个任务的执行日志以 JSON Lines 格式追加写入 `logs/output/cron/<job_id>.log`
- 受保护的系统任务（`payload.kind == "system_event"`）无法删除
- Cron 表达式时区支持（通过 `zoneinfo` 使用 IANA 时区名）
- REST API（Robyn）：`GET/POST/PUT/DELETE /cron`、`POST /cron/trigger`、`POST /cron/enable`，供桌面客户端使用

## 模块结构

```
skills/builtin/core/cron/
├── __init__.py
├── SKILL.md             # Agent 技能定义（add / list / remove / set_context 用法）
└── scripts/
    ├── __init__.py      # 公开导出：CronService, cron_service, Cron, cron, types
    ├── base.py          # CronService 单例、cron_jobs.json 读写、定时循环、任务执行
    ├── core.py          # Cron 门面（面向 Agent）：add_job / list_jobs / remove_job / set_context
    ├── types.py         # 数据模型：CronSchedule, CronPayload, CronRunRecord, CronJobState, CronJob, CronStore
    └── README.md        # 本文件
```

本技能目录之外的相关代码：

- [`server/trigger/http/cron.py`](../../../../../server/trigger/http/cron.py) — 封装 `cron_service` 的 REST 端点
- [`../SKILL.md`](../SKILL.md) — Agent 如何调用技能脚本
- `cron_jobs.json` — 项目根目录下的任务存储（`config.ROOT_DIR / "cron_jobs.json"`）
- `logs/output/cron/` — 每任务的执行日志

## 工作原理

1. **服务启动**：服务入口调用 `skills.builtin.core.cron.scripts.base` 中的 `init()`，绑定执行回调并启动名为 `cron-service` 的守护线程（`_start_cron_service_thread`）。该线程创建专属 asyncio 事件循环，运行 `cron_service.start()` 后永久循环。导入 cron 脚本无副作用；`CronService.add_job()` / `register_system_job()` 也会在服务未运行时于调用方的事件循环上懒启动服务。
2. **定时循环**：`_arm_timer()` 调度一次 `asyncio` sleep，直到启用任务中最早的 `nextRunAtMs`；随后 `_on_timer()` 重新加载存储（获取外部修改），执行所有 `nextRunAtMs <= now` 的已启用任务，保存存储并重新续订定时器。
3. **执行**（`_execute_job`）：通过 `set_on_job` 注册的回调（即 `_on_cron_job`）运行任务；记录任务的 `lastStatus` / `lastError`，推送 WS 通知并追加一条执行日志。一次性（`at`）任务随后被删除（若 `deleteAfterRun`）或禁用；周期任务重新计算下次运行时间。

**结果投递**（`base.py` 中的 `_on_cron_job`）：

1. 以 `create_agent(system_prompt=build_system_prompt(), model=build_main_llm(), tools=[build_python_repl_tool(), build_read_file_tool(), build_write_file_tool()])` 构建一个全新 Agent，并以任务的 `payload.message` 作为 `HumanMessage` 调用。
2. Agent 的最终消息以 `InboundMessage(channel=payload.channel, sender_id="cron tool", chat_id=payload.to, content=result)` 的形式发布到消息总线。
3. 渠道入站消费者（`server/trigger/channels/core.py`）按已启用渠道处理该消息，并通过 `channel.send(OutboundMessage(...))` 将生成的回复发送到配置的 `chat_id`。
4. 与此同时，`_push_cron_notification` 向会话 `default`（`CRON_WS_SESSION_ID`）的 WebSocket 发送 `{"event": "notification", "content": "cron: <job name> [<status>]"}`，使浏览器 UI 的通知铃铛实时更新。尽力而为：失败仅记录日志，不会中断流程。

> 注意：`deliver` 字段会随任务存储并通过 API 暴露，但当前执行路径（`_on_cron_job`）无论其取值如何都会把结果发布到总线。消息能否真正到达用户取决于已启用的渠道（见 `plugins/channels/config.json`）。

## 任务存储（`cron_jobs.json`）

任务持久化到项目根目录的 `cron_jobs.json`。服务启动时加载该文件，并在文件修改时间变化时自动重载——可直接编辑该文件批量添加或修改任务，更改将在下一个定时器 tick 生效。

磁盘上的字段使用 camelCase（`base.py` 中的 `_save_store` / `_load_store`）。顶层为 `version`（int）和 `jobs`（数组）。任务示例：

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily_digest",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "atMs": null,
        "everyMs": null,
        "expr": "0 9 * * *",
        "tz": "Asia/Shanghai"
      },
      "payload": {
        "kind": "agent_turn",
        "message": "Summarize today's schedule and important events",
        "deliver": false,
        "channel": null,
        "to": null
      },
      "state": {
        "nextRunAtMs": 1756000000000,
        "lastRunAtMs": null,
        "lastStatus": null,
        "lastError": null
      },
      "createdAtMs": 1755000000000,
      "updatedAtMs": 1755000000000,
      "deleteAfterRun": false
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 唯一任务 ID（`uuid4` 的前 8 位） |
| `name` | `str` | 人类可读的名称 |
| `enabled` | `bool` | 是否启用（默认 `true`） |
| `schedule` | `object` | 何时运行：见下表 |
| `payload` | `object` | 运行什么：见下表 |
| `state` | `object` | 运行时状态：见下表 |
| `createdAtMs` | `int` | 创建时间戳（毫秒） |
| `updatedAtMs` | `int` | 最后更新时间戳（毫秒） |
| `deleteAfterRun` | `bool` | 一次性任务执行后是否删除（默认 `false`） |

**`schedule`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | `"at" \| "every" \| "cron"` | 调度类型 |
| `atMs` | `int \| null` | Unix 时间戳（毫秒）——用于 `kind: "at"` |
| `everyMs` | `int \| null` | 间隔（毫秒）——用于 `kind: "every"` |
| `expr` | `str \| null` | Cron 表达式，如 `"0 9 * * *"`——用于 `kind: "cron"` |
| `tz` | `str \| null` | IANA 时区，如 `"Asia/Shanghai"`——仅可与 `kind: "cron"` 一起使用 |

**`payload`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | `"agent_turn" \| "system_event"` | 负载类型（默认 `"agent_turn"`；通过服务或 API 添加的任务始终为 `agent_turn`） |
| `message` | `str` | 发送给 Agent 的提示消息 |
| `deliver` | `bool` | 投递标志（默认 `false`；见上文说明——当前执行路径不读取该字段） |
| `channel` | `str \| null` | 渠道名称，如 `"qq"` |
| `to` | `str \| null` | 接收方标识（用作 `chat_id`） |

**`state`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `nextRunAtMs` | `int \| null` | 下次计划运行时间（毫秒）；禁用/过期任务为 `null` |
| `lastRunAtMs` | `int \| null` | 上次执行的开始时间（毫秒） |
| `lastStatus` | `"ok" \| "error" \| "skipped" \| null` | 上次执行结果 |
| `lastError` | `str \| null` | 上次错误信息 |

Python 侧对应的模型（`types.py`）使用 snake_case（`at_ms`、`every_ms`、`next_run_at_ms`、`last_run_at_ms`、`last_status`、`last_error`、`created_at_ms`、`updated_at_ms`、`delete_after_run`）。`CronRunRecord` 已导出但目前未被使用。

## 公开 API

### Agent 技能命令（`Cron` 门面，`core.py` 中的 `cron` 单例）

以下是通过 [`../SKILL.md`](../SKILL.md) 暴露给 Agent 的命令，用法为 `from skills.builtin.core.cron.scripts import cron`：

| 命令 | 说明 |
|------|------|
| `cron.set_context(channel, chat_id)` | 设置会话上下文（两者必填且非空），作为后续添加任务的投递目标 |
| `cron.add_job(name=None, message, every_seconds=None, cron_expr=None, tz=None, at=None, deliver=True)` | 添加任务。`every_seconds` / `cron_expr` / `at`（ISO 日期时间）三者必须提供其一。需先调用 `set_context`。`tz` 仅可与 `cron_expr` 同用（默认 `"UTC"`）；无时区的 `at` 时间按 UTC 处理；`at` 任务自动设置 `delete_after_run=True`；`name` 默认取 `message` 前 30 个字符 |
| `cron.list_jobs()` | 人类可读的任务列表：调度时间、系统任务用途与保护标志、上次/下次运行时间 |
| `cron.remove_job(job_id)` | 删除任务；对受保护的系统任务返回友好的错误提示 |

### `CronService`（Python API，`base.py` 中的 `cron_service` 单例）

| 方法 | 说明 |
|------|------|
| `await start()` | 加载存储、重算下次运行时间、保存并续订定时器 |
| `stop()` | 停止服务并取消定时器任务 |
| `set_on_job(callback)` | 注册异步执行回调（由 `init()` 绑定为 `_on_cron_job`） |
| `list_jobs(include_disabled=False)` | 按下次运行时间排序列出任务；仅当 `include_disabled=True` 时包含已禁用任务 |
| `add_job(name, schedule, message, deliver=False, channel=None, to=None, delete_after_run=False)` | 添加任务（`payload.kind` 恒为 `"agent_turn"`）；自动启动服务；返回 `CronJob` |
| `register_system_job(job)` | 按 `id` 幂等地（重新）注册系统任务（当前仓库内无调用方） |
| `remove_job(job_id)` | 返回 `"removed"`、`"protected"`（`payload.kind == "system_event"`）或 `"not_found"` |
| `enable_job(job_id, enabled=True)` | 启用/禁用；重算或清空 `nextRunAtMs` |
| `await run_job(job_id, force=False)` | 立即运行；已禁用任务除非 `force=True` 否则跳过 |
| `get_job(job_id)` | 按 ID 获取任务，找不到返回 `None` |
| `status()` | 返回 `{"enabled": bool, "jobs": int, "next_wake_at_ms": int \| None}` |

### HTTP REST API（`server/trigger/http/cron.py`，后端地址 `http://127.0.0.1:8080`）

| 端点 | 说明 |
|------|------|
| `GET /cron?include_disabled=false` | 列出任务（camelCase JSON） |
| `POST /cron` | 创建：`{"name", "message", "schedule": {"kind", "atMs"/"everyMs"/"expr"/"tz"}, "deliver", "channel", "to", "delete_after_run"}` |
| `PUT /cron` | 更新：以"删除 + 重建"实现，保留原 `id` 和 `createdAtMs` |
| `POST /cron/trigger` | 立即运行：`{"id", "force"}`（已禁用且未传 `force` 时返回 400） |
| `POST /cron/enable` | 启用/禁用：`{"id", "enabled"}` |
| `DELETE /cron` | 删除：`{"id"}`；受保护的系统任务返回 `403` |

## 使用示例

Agent 技能脚本（见 [`../SKILL.md`](../SKILL.md)）：

```python
from loguru import logger
from skills.builtin.core.cron.scripts import cron

# 添加任务前必须先设置一次会话上下文
cron.set_context(channel="qq", chat_id="group_123456")

# Cron 表达式任务：每天北京时间 9 点
res = cron.add_job(
    name="daily_digest",
    message="Summarize today's schedule and important events",
    cron_expr="0 9 * * *",
    tz="Asia/Shanghai",
)
logger.info(res)

# 固定间隔任务：每 30 分钟一次
res = cron.add_job(
    message="Check today's weather and remind user to bring an umbrella if needed",
    every_seconds=30 * 60,
)

# 一次性任务：显式 ISO 日期时间
res = cron.add_job(message="Say good morning to the user", at="2026-02-12T10:30:00")

logger.info(cron.list_jobs())
# cron.remove_job("a1b2c3d4")
```

Python API：

```python
from skills.builtin.core.cron.scripts import cron_service, CronSchedule

# 服务在首次使用时自动启动；显式启动是可选的
await cron_service.start()

job = cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

jobs = cron_service.list_jobs()
print([j.name for j in jobs])

await cron_service.run_job(job.id, force=True)   # 手动触发
cron_service.remove_job(job.id)                   # "removed" | "protected" | "not_found"
```

HTTP：

```bash
curl http://127.0.0.1:8080/cron
curl -X POST http://127.0.0.1:8080/cron -H "Content-Type: application/json" \
  -d '{"name": "daily_digest", "message": "Summarize today", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}}'
curl -X POST http://127.0.0.1:8080/cron/trigger -H "Content-Type: application/json" -d '{"id": "a1b2c3d4", "force": true}'
```

## 调度语义

| 类型 | 行为 |
|------|------|
| `at` | 在 `atMs` 指定时间执行一次。若计算时时间戳已过期，`nextRunAtMs` 置为 `null`，任务永不触发。执行后删除（`deleteAfterRun=true`）或禁用（`enabled=false`、`nextRunAtMs=null`） |
| `every` | 下次运行 = 当前时间 + `everyMs`，每次执行后重新计算 |
| `cron` | 由 `croniter` 根据表达式计算下次运行时间；基准时间在 `tz` 指定的时区中求值，未指定时使用系统本地时区 |

校验规则：`tz` 仅在 `kind: "cron"` 时可用；未知的 IANA 时区名会被拒绝（`ValueError`），服务层与门面层均如此。

## 受保护的系统任务

`payload.kind == "system_event"` 的任务受保护：`CronService.remove_job()` 拒绝删除（返回 `"protected"`，HTTP `DELETE /cron` 返回 `403`）。技能层还会按名称识别 `dream` 任务，并将其描述为用于长期记忆的 Dream 记忆整理任务。通过 `add_job`（Python、技能或 HTTP）添加的任务始终为 `agent_turn`；`system_event` 任务只能来自 `register_system_job()` 或直接编辑 `cron_jobs.json`。

## 依赖

- `croniter>=6.2.2` — Cron 表达式解析
- Python `zoneinfo` — 时区支持
- `config/` 中没有任何 cron 专属配置项，也不存在 cron 相关的环境变量

## 注意事项

- 执行历史：每次运行会向 `logs/output/cron/<job_id>.log` 追加一行 JSON，包含 `timestamp`、`job_id`、`job_name`、`start_time`、`end_time`、`duration_ms`、`status`、`error`、`message`。没有内存中的运行历史（`CronRunRecord` 为未使用的遗留代码）。
- 对 `cron_jobs.json` 的外部修改通过文件修改时间检测，在下一个定时器 tick 生效；每次执行后存储都会重新保存。
- 服务运行在 `cron-service` 守护线程的独立事件循环上，与主服务器循环相互独立；`run_job()` 和 `start()` 必须在运行中的事件循环内 await。
- WebSocket 通知目标是会话 `"default"`（浏览器客户端会话），因此仅在客户端连接期间才能收到桌面通知。
