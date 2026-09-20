# 🚦 并发 Lane — MAIN · SUBAGENT · NUDGE · NESTED

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Long-Running Tasks](../README.zh.md) 的一部分：进程级并发 Lane（MAIN、SUBAGENT、NUDGE、NESTED），约束子 Agent、后台 nudge 与嵌套回复轮次。

---

## 🚦 并发 Lane

长时工作不会无界地扇出：每个分离出的子 Agent、每次后台 nudge、每个嵌套的 `sessions.send` 回复 turn，都必须先经过一个**进程级 lane**。lane 是 `asyncio.Semaphore` 加 `active`/`queued` 计数器（`runtime/lane/core.py`）；超限的工作**按 FIFO 排队**而不是被拒绝。

| Lane | 约束对象 | 默认并发 | 配置键 |
| :--- | :--- | :--- | :--- |
| `MAIN` | 主 Agent turn（`server/service/input_queue_service.py::_run_executor`） | `min(16, max(8, CPU))`，向上钳制到不低于 `SUBAGENT + NUDGE` → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | 子 Agent 执行（`spawn/core.py`、`control/steer.py`） | `8` | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | 记忆 nudge / 计划抽取 / 压缩后 todo 更新（`agent/middlewares/summarization/nudges.py` 三处） | `4` | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` 回复 turn（串行） | `1` | `LANE_SYSTEM["nested_max_concurrent"]` |

### 配置与校验

Lane 上限位于 `config/features/infra_side/lane_system.py`：

```python
LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),   # 12–16
    "subagent_max_concurrent": 8,
    "nudge_max_concurrent": 4,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}
```

`_resolve_main_concurrency()` 对标 OpenClaw 的 CPU 缩放——`min(16, max(8, CPU))`——再**向上**钳制到硬不变量 `SUBAGENT + NUDGE`（8 + 4 = 12），因此出厂默认值在任何 CPU 数量下都合法。`validate_lane_config()` 由 `install_lane_lifecycle()`（`server/service/lane_lifecycle.py`）在服务器启动时调用一次（绝不在 import 时）；当 `main_max_concurrent < subagent + nudge` 或任一 lane 上限 `< 1` 时抛错。`lane_wait_warn_ms` 控制"等待 slot 过久"的告警，`lane_drain_timeout_seconds` 是 `LaneManager.drain_all()` 的默认超时。`LaneManager.set_concurrency(lane, n)` 热更新上限——in-flight 的 slot 保留自己的 permit，只有新的 acquire 看到新上限。

### 排队语义：`PENDING`

全局并发不是拒绝计数器。`spawn_subagent_direct` 仍执行 per-parent 准入（`validate_spawn_depth`、`validate_concurrent_children`），但超过全局上限的 spawn 会被**接受**并注册为 `ExecutionStatus.PENDING`：`started_at` 保持 `None`，该 run 不持有 lane slot。SUBAGENT lane 包装器（`_execute_subagent_with_lane`，`agent/tools/subagent/spawn/core.py`）等待 slot，然后经 `mark_run_running()` 把 `PENDING → RUNNING`，此时才写入 `started_at`——排队等待永远不计入运行时长。`validate_global_concurrent()` 与 `SubagentConfig.max_concurrent` 仅为向后兼容保留，spawn 流水线不调用。

由于排队中的 child 仍占用一个准入 slot，registry 计数函数把 `RUNNING + PENDING` 都计为活跃（`count_active_runs_for_session`、`count_active_descendant_runs`、`count_all_active_runs`），且 `is_live_unended_run()` 包含 `PENDING`。

### PENDING 生命周期

| 路径 | 行为 |
| :--- | :--- |
| **Kill** | PENDING run 可列出、可 kill（`list_killable_children`）；`cancel_task()` 取消 lane 等待者，`CancelledError` 从 `Lane.acquire()` 传出，既不消耗也不泄漏 permit |
| **Steer** | 拒绝——`steer_subagent_run()` 只接受 RUNNING/INTERRUPTED；被 steer 重启的 run 以 PENDING 重新进入 lane，并在自己的 slot 内被提升 |
| **Sweeper / 孤儿恢复** | `is_live_unended_run()` 包含 PENDING，因此 task 已消失的 PENDING run 是孤儿；`evaluate_recovery_gate()` 将其判为 `"wedged"`，`_recovery_loop()` 直接把它终结为 `TERMINAL`，`ended_reason="pending_orphaned"`（outcome `TIMEOUT`、error `"pending orphaned"`），并运行 announce 流程 |
| **Yield** | `sessions_yield` 把 PENDING child 计为活跃，`wake_yield_if_all_children_settled` 只有在全部结束才唤醒父级；yield 超时涵盖 lane 等待，到期后正常返回 |
| **计数 / 列表** | `control/list.py` 与 `runtime_tools.py` 把 PENDING child 与 RUNNING/INTERRUPTED 一起呈现 |

只要 PENDING run 的 lane task 仍存在，sweeper 扫描就会跳过它（进程只是在排队）；只有 task 丢失（进程重启）才会使其成为孤儿。

### Drain 模式与关闭

`Lane.acquire()` 在等待前先查询 drain 检查回调：当 subagent gateway 报告正在 draining 时，获取者被以 `RuntimeError` 拒绝，且**不消耗 permit**。回调在启动时通过 `set_drain_check(is_gateway_draining)`（`install_lane_lifecycle`）注入，使 `runtime/lane` 无需向上层 import。

退出时，同一 seam 先切换 drain 模式（`set_draining(True)`），再经 `atexit` 执行**有界** drain——`asyncio.run(drain_all_lanes(timeout=0))` 报告最终的各 lane 计数，绝不拖慢进程退出。仓库没有异步关闭路径（Robyn 的 `shutdown_handler` 在 SIGINT/SIGTERM 时从未被调用），因此 in-flight turn 被交给操作系统回收，而不是被等待。

### 可观测性

`GET /lane-status`（`server/trigger/http/lane.py`）返回全部四个 lane 的实时快照：

```json
{"main": {"name": "main", "max_concurrent": 12, "active": 1, "queued": 0},
 "subagent": {"name": "subagent", "max_concurrent": 8, "active": 3, "queued": 2},
 "nudge": {"name": "nudge", "max_concurrent": 4, "active": 0, "queued": 0},
 "nested": {"name": "nested", "max_concurrent": 1, "active": 0, "queued": 0}}
```

### 死锁防护

| 场景 | 机制 |
| :--- | :--- |
| 主 turn 持有 MAIN slot 等待 child 结果 | child 只需要 SUBAGENT slot——各 lane 是相互独立的信号量，不存在跨 lane 等待环（`test_filling_main_does_not_block_subagent`；spawn 路径：`test_subagent_runs_while_main_lane_slot_is_held`）；可选的 `run_timeout_seconds > 0` 额外为卡住的 child 兜底 |
| Nudge 等待 SUBAGENT slot | nudge 不 spawn subagent（工具集受限），且 NUDGE lane 本身就独立 |
| in-flight 期间的 lane 热更新 | `set_concurrency` 只影响新的 acquire |
| `sessions_yield` 等待 PENDING child | yield 超时包含 lane 等待时间，到期后正常返回 |
| Drain 模式 + lane 等待者 | `acquire()` 经 drain 检查拒绝，不消耗 permit |
| Kill PENDING run | `CancelledError` 退出 `Lane.acquire()`，不释放 permit |
| Steer PENDING run | 被状态检查拒绝；只有 RUNNING/INTERRUPTED 可 steer |

### Lane 文件地图

| 文件 | 职责 |
| :--- | :--- |
| `config/features/infra_side/lane_system.py` | `LaneSystemConfig` / `LANE_SYSTEM` + `validate_lane_config()` |
| `runtime/lane/core.py` | `LaneType`、`Lane`、`LaneManager`、`get_lane_manager()`、`lane_slot()`、`set_drain_check()` |
| `server/service/lane_lifecycle.py` | 启动校验、drain 门注册、有界退出 drain |
| `server/trigger/http/lane.py` | `GET /lane-status` |
| `agent/tools/subagent/spawn/core.py` · `control/steer.py` | SUBAGENT lane 包装器 + PENDING → RUNNING 提升 |
| `agent/middlewares/summarization/nudges.py` | 3 处 NUDGE lane 调用点 |
| `agent/tools/subagent/tools/sessions_send.py` | 回复 turn 外层的 NESTED lane |
| `server/service/input_queue_service.py` | `_run_executor` 外层的 MAIN lane |
| `agent/tools/subagent/orphan/recovery.py` | PENDING 孤儿的 `pending_orphaned` 终结 |
| `tests/runtime/lane/` · `tests/server/service/test_main_lane.py` · `tests/agent/tools/subagent/test_{spawn_lane_integration,kill_pending,steer_lane,sweeper_pending,sessions_yield_pending}.py` · `tests/server/trigger/http/test_lane_api.py` | Lane 测试套件 |

### 实现取舍

实现时记录的三处有意取舍：

- **MAIN 向上钳制到不变量。** 仅靠 CPU 缩放（`min(16, max(8, CPU))`）在 CPU ≤ 8 的机器上会得到 8，从而在启动时触发 `validate_lane_config()` 报错；因此默认值向上钳制到 `SUBAGENT + NUDGE`（12），在 4/8/12/16/64 核机器上均合法。
- **`GET /lane-status` 不带 `/api` 前缀。** 初始设计中的 `/api/lane-status` 路由被放弃，改用仓库既有路由惯例：处理器位于 `server/trigger/http/lane.py`，与 `/channels`、`/cron` 等并列。
- **退出 drain 走 `atexit`。** 仓库没有异步关闭 seam（Robyn 的 `shutdown_handler` 在 SIGINT/SIGTERM 时不会被调用），因此有界的 `drain_all(timeout=0)` 只报告最终的各 lane 计数、绝不拖慢退出；in-flight turn 交给操作系统回收。

