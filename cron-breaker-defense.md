# Cron 断路器防御：任务失败断路 + 周期服务退避 + 崩溃循环检测

> Cron 防御 A/B/C + 实现步骤 + 配置汇总

## 5. Cron 防御 A：任务连续失败断路器

### 5.1 openclaw 对应

- `cron-stream-job-owner.ts`：`MAX_CONSECUTIVE_FAILURES=5` → `restartExhausted=true`
- `auto-disable.ts`：`MAX_CONSECUTIVE_RUN_FAILURES=10` → 自动禁用 + 通知

### 5.2 sherry 适配点

`skills/builtin/core/cron/scripts/base.py` 的 `CronService` 类（L105-592）

现有 `_on_cron_job`（L555-586）在 cron job 触发时创建 agent 调用，但**无失败计数、无断路**。

### 5.3 修改文件：`skills/builtin/core/cron/scripts/base.py`

#### 5.3.1 数据结构新增

```python
@dataclass
class CronJobFailureState:
    """Cron 任务失败状态跟踪。"""
    consecutive_failures: int = 0
    last_failure_reason: str = ""
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    is_degraded: bool = False        # 5次失败后标记降级
    is_disabled: bool = False        # 10次失败后自动禁用
    backoff_ms: int = 0              # 当前退避延迟（降级模式）


class CronService:
    # 新增类常量
    DEGRADED_THRESHOLD = 5           # 5次连续失败 → 降级
    DISABLED_THRESHOLD = 10          # 10次连续失败 → 自动禁用
    STABLE_RUN_THRESHOLD_S = 60      # 运行>60s成功 → 重置计数
    DEGRADE_BACKOFF_BASE_MS = 5000   # 降级模式退避基础延迟
    DEGRADE_BACKOFF_MAX_MS = 300000  # 降级模式退避最大延迟（5分钟）
```

#### 5.3.2 失败跟踪

在 `_on_cron_job`（L555-586）中新增失败跟踪：

```python
async def _on_cron_job(self, job: CronJob) -> None:
    """Cron job 触发回调。"""
    failure_state = self._get_failure_state(job.id)

    # 检查是否已自动禁用
    if failure_state.is_disabled:
        logger.info("Cron job %s 已被自动禁用，跳过", job.id)
        return

    # 降级模式：检查退避
    if failure_state.is_degraded:
        import time
        elapsed = time.time() - failure_state.last_failure_time
        if elapsed * 1000 < failure_state.backoff_ms:
            logger.info(
                "Cron job %s 降级模式退避中 (%.1fs/%.1fs)，跳过",
                job.id, elapsed, failure_state.backoff_ms / 1000,
            )
            return

    start_time = time.time()
    try:
        # --- 现有 agent 调用逻辑 ---
        agent = ...  # 创建 agent
        result = await agent.ainvoke(...)
        # 成功
        run_duration = time.time() - start_time

        # 稳定运行重置：运行>60s 成功 → 重置失败计数
        if run_duration >= self.STABLE_RUN_THRESHOLD_S:
            failure_state.consecutive_failures = 0
            failure_state.is_degraded = False
            failure_state.backoff_ms = 0
        else:
            # 短暂运行也成功 → 至少重置计数
            failure_state.consecutive_failures = 0
            failure_state.is_degraded = False
            failure_state.backoff_ms = 0

        failure_state.last_success_time = time.time()
        self._save_failure_state(job.id, failure_state)

    except Exception as e:
        failure_state.consecutive_failures += 1
        failure_state.last_failure_reason = str(e)
        failure_state.last_failure_time = time.time()

        # 5次 → 降级
        if failure_state.consecutive_failures >= self.DEGRADED_THRESHOLD:
            failure_state.is_degraded = True
            failure_state.backoff_ms = min(
                self.DEGRADE_BACKOFF_BASE_MS * (2 ** (failure_state.consecutive_failures - self.DEGRADED_THRESHOLD)),
                self.DEGRADE_BACKOFF_MAX_MS,
            )
            logger.warning(
                "Cron job %s 进入降级模式 (failures=%d, backoff=%.1fs)",
                job.id, failure_state.consecutive_failures,
                failure_state.backoff_ms / 1000,
            )

        # 10次 → 自动禁用
        if failure_state.consecutive_failures >= self.DISABLED_THRESHOLD:
            failure_state.is_disabled = True
            job.enabled = False
            logger.error(
                "Cron job %s 自动禁用 (failures=%d, reason=%s)",
                job.id, failure_state.consecutive_failures, str(e)[:200],
            )
            # 通过 MessageBus 通知
            await self._notify_cron_disabled(job, failure_state)

        self._save_failure_state(job.id, failure_state)
        self._persist_job(job)  # 持久化 job.enabled=False
```

#### 5.3.3 失败状态持久化

```python
def _get_failure_state(self, job_id: str) -> CronJobFailureState:
    """从内存或磁盘加载失败状态。"""
    # 存储在 cron_jobs.json 旁边的 failure_states.json
    # 或存在 job 对象的新字段中
    return self._failure_states.get(job_id, CronJobFailureState())

def _save_failure_state(self, job_id: str, state: CronJobFailureState) -> None:
    self._failure_states[job_id] = state
    # 定期持久化到 failure_states.json

async def _notify_cron_disabled(self, job: CronJob, state: CronJobFailureState) -> None:
    """通过 MessageBus 通知 cron job 被自动禁用。"""
    # 发送消息到 job 的 channel
    from server.service.message_bus import MessageBus
    bus = MessageBus()
    await bus.publish(
        channel_id=job.channel_id,
        content=(
            f"Cron 任务 '{job.name}' 已被自动禁用\n"
            f"原因: 连续 {state.consecutive_failures} 次失败\n"
            f"最近错误: {state.last_failure_reason[:200]}\n"
            f"请检查任务配置或手动重新启用。"
        ),
    )
```

#### 5.3.4 REST API 增强

在 `server/trigger/http/cron.py` 中新增：

```
GET /cron/:id/failure-state    → 查看失败状态
POST /cron/:id/reset-failures  → 重置失败计数（手动恢复降级/禁用的任务）
```

---

## 6. Cron 防御 B：周期服务退避重试

### 6.1 openclaw 对应

`cron-exit-watchers.ts`：跟踪 `consecutiveFailures`，使用可配置的 `retryBackoffMs` 进行指数退避。

### 6.2 sherry 适配点

1. **HeartbeatService**（`skills/builtin/core/heartbeat/scripts/base.py`）
2. **Subagent Sweeper**（`agent/tools/subagent/registry/sweeper.py`）—— 当前未接线

### 6.3 新文件：`runtime/periodic_backoff.py`

```python
"""通用周期服务退避工具。

当周期性后台服务连续失败时，自动拉长轮询间隔。
参考 openclaw cron-exit-watchers.ts 的指数退避重试。
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class PeriodicBackoff:
    """
    周期服务退避管理器。

    工作原理：
    - 成功 → 重置间隔到 base_interval
    - 失败 → 间隔 *= backoff_factor（上限 max_interval）
    - 连续 max_consecutive_failures 后 → 标记 exhausted

    用法：
        backoff = PeriodicBackoff(base_interval=60, backoff_factor=2, max_interval=600)
        # 在循环中：
        try:
            await do_work()
            backoff.record_success()
        except Exception:
            backoff.record_failure()
        await asyncio.sleep(backoff.current_interval)
    """

    base_interval: float = 60.0       # 基础间隔（秒）
    backoff_factor: float = 2.0       # 退避倍数
    max_interval: float = 600.0       # 最大间隔（10分钟）
    max_consecutive_failures: int = 5  # 连续失败上限

    def __post_init__(self):
        self._consecutive_failures: int = 0
        self._current_interval: float = self.base_interval
        self._exhausted: bool = False
        self._last_failure_time: float = 0.0
        self._last_failure_reason: str = ""

    @property
    def current_interval(self) -> float:
        return self._current_interval

    @property
    def is_exhausted(self) -> bool:
        return self._exhausted

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._current_interval = self.base_interval
        self._exhausted = False

    def record_failure(self, reason: str = "") -> bool:
        """记录失败，返回 True 表示已耗尽。"""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        self._last_failure_reason = reason

        self._current_interval = min(
            self.base_interval * (self.backoff_factor ** self._consecutive_failures),
            self.max_interval,
        )

        if self._consecutive_failures >= self.max_consecutive_failures:
            self._exhausted = True
            return True
        return False
```

### 6.4 适配 HeartbeatService

修改 `skills/builtin/core/heartbeat/scripts/base.py`（L53-219）：

```python
class HeartbeatService:
    def __init__(self, ...):
        # ... 现有初始化 ...
        self._backoff = PeriodicBackoff(
            base_interval=self.interval_s,  # 30分钟=1800s
            backoff_factor=2.0,
            max_interval=7200,             # 最大2小时
            max_consecutive_failures=5,
        )

    async def _run_loop(self):
        while self._running:
            try:
                await self._tick()
                self._backoff.record_success()
            except Exception as e:
                is_exhausted = self._backoff.record_failure(str(e))
                logger.warning(
                    "Heartbeat 失败 %d/%d (%.0fs 退避): %s",
                    self._backoff.consecutive_failures,
                    self._backoff.max_consecutive_failures,
                    self._backoff.current_interval,
                    str(e)[:200],
                )
                if is_exhausted:
                    logger.error("Heartbeat 连续失败 %d 次，暂停服务", self._backoff.consecutive_failures)
                    # 暂停服务，等待手动恢复或外部重置
                    return

            await asyncio.sleep(self._backoff.current_interval)
```

### 6.5 适配 Subagent Sweeper（接线 + 退避）

修改 `agent/tools/subagent/registry/sweeper.py`（L1-167）：

```python
# 新增导入
from runtime.periodic_backoff import PeriodicBackoff

_backoff = PeriodicBackoff(
    base_interval=60,          # 60秒（现有 sweeper_interval_seconds）
    backoff_factor=2.0,
    max_interval=300,          # 最大5分钟
    max_consecutive_failures=5,
)

async def _sweep_loop():
    while _running:
        try:
            await _do_sweep()
            _backoff.record_success()
        except Exception as e:
            is_exhausted = _backoff.record_failure(str(e))
            logger.warning(
                "Sweeper 失败 %d/%d (%.0fs 退避): %s",
                _backoff.consecutive_failures,
                _backoff.max_consecutive_failures,
                _backoff.current_interval,
                str(e)[:200],
            )
            if is_exhausted:
                logger.error("Sweeper 连续失败 %d 次，暂停", _backoff.consecutive_failures)
                # 暂停，等待外部恢复
                _running = False
                return

        await asyncio.sleep(_backoff.current_interval)
```

### 6.6 接线 Sweeper

在 `server/__main__.py` 或 `server/trigger/channels/core.py` 中调用 `start_sweeper()`：

```python
# server/trigger/channels/core.py 第89行附近
from agent.tools.subagent.registry import start_sweeper

# 在 channel 启动后
asyncio.get_event_loop().create_task(start_sweeper())
```

---

## 7. Cron 防御 C：崩溃循环断路器

### 7.1 openclaw 对应

`gateway-boot-lifecycle.ts`：

- `GATEWAY_BOOT_LOOP_UNCLEAN_THRESHOLD = 3`
- 5分钟窗口内 3 次不干净启动 → 断路器跳闸
- 跳闸时阻止 auto-start sidecars
- SQLite 持久化启动生命周期记录
- 窗口耗尽后自动恢复

### 7.2 sherry 适配点

`server/__main__.py` 启动流程 + `server/trigger/channels/core.py` channel 启动

### 7.3 新文件：`runtime/crash_loop_breaker.py`

```python
"""崩溃循环断路器。

在进程崩溃后自动重启时，检测是否进入崩溃循环。
5分钟窗口内 3 次不干净退出 → 跳闸，阻止自动启动后台服务。

参考 openclaw gateway-boot-lifecycle.ts：
  GATEWAY_BOOT_LOOP_UNCLEAN_THRESHOLD = 3
  窗口 = 5分钟
"""
from __future__ import annotations

import json
import os
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CrashLoopBreaker:
    """
    崩溃循环断路器。

    工作原理：
    1. 每次启动时记录时间戳到 SQLite/JSON
    2. 检查5分钟窗口内不干净启动次数
    3. 超过阈值 → is_tripped = True
    4. 跳闸时：跳过 CronService/HeartbeatService/Sweeper 自动启动
    5. 窗口耗尽后自动恢复
    """

    UNCLEAN_THRESHOLD = 3           # 3次不干净启动 → 跳闸
    WINDOW_S = 300                   # 5分钟窗口
    RECORD_RETENTION_S = 86400       # 记录保留24小时

    def __init__(self, store_path: Optional[str] = None):
        if store_path is None:
            store_path = str(Path.cwd() / "data" / "boot_lifecycle.json")
        self._store_path = store_path
        self._ensure_store()

    def _ensure_store(self) -> None:
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        if not os.path.exists(self._store_path):
            self._write_records([])

    def _read_records(self) -> list[dict]:
        try:
            with open(self._store_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_records(self, records: list[dict]) -> None:
        with open(self._store_path, "w") as f:
            json.dump(records, f, indent=2)

    def record_boot(self, clean: bool = True, reason: str = "") -> bool:
        """
        记录一次启动。

        Args:
            clean: True=干净退出后启动, False=不干净退出后启动
            reason: 启动原因/上次退出原因

        Returns:
            True 如果断路器已跳闸
        """
        now = time.time()
        records = self._read_records()

        # 清理过期记录
        records = [
            r for r in records
            if now - r["timestamp"] < self.RECORD_RETENTION_S
        ]

        # 记录本次启动
        records.append({
            "timestamp": now,
            "clean": clean,
            "reason": reason[:200] if reason else "",
        })

        # 检查窗口内不干净启动次数
        window_records = [
            r for r in records
            if now - r["timestamp"] < self.WINDOW_S and not r["clean"]
        ]

        is_tripped = len(window_records) >= self.UNCLEAN_THRESHOLD

        if is_tripped:
            logger.error(
                "CrashLoopBreaker: 断路器跳闸！"
                "%d 次不干净启动在 %d 秒窗口内",
                len(window_records), self.WINDOW_S,
            )

        self._write_records(records)
        return is_tripped

    def is_tripped(self) -> bool:
        """检查断路器是否已跳闸（不记录新启动）。"""
        now = time.time()
        records = self._read_records()
        window_records = [
            r for r in records
            if now - r["timestamp"] < self.WINDOW_S and not r["clean"]
        ]
        return len(window_records) >= self.UNCLEAN_THRESHOLD

    def clear(self) -> None:
        """清除所有记录（手动恢复）。"""
        self._write_records([])


_crash_loop_breaker: Optional[CrashLoopBreaker] = None


def get_crash_loop_breaker() -> CrashLoopBreaker:
    global _crash_loop_breaker
    if _crash_loop_breaker is None:
        _crash_loop_breaker = CrashLoopBreaker()
    return _crash_loop_breaker
```

### 7.4 修改：`server/__main__.py`

在服务启动时检查崩溃循环断路器：

```python
# server/__main__.py
import sys
from runtime.crash_loop_breaker import get_crash_loop_breaker

def main():
    # 记录启动
    was_clean = _check_last_exit_was_clean()
    breaker = get_crash_loop_breaker()
    is_tripped = breaker.record_boot(clean=was_clean, reason="startup")

    if is_tripped:
        logger.error(
            "检测到崩溃循环！跳过后台服务自动启动。\n"
            "请检查日志、配置和依赖后手动重启。\n"
            "清除断路器：删除 data/boot_lifecycle.json"
        )
        # 跳过 CronService/HeartbeatService/Sweeper 启动
        # 但仍启动 HTTP 服务（允许用户通过 API 排查）
        _start_http_only()
        return

    # 正常启动
    _start_all_services()
```

### 7.5 干净退出标记

在 `server/__main__.py` 的退出处理中写入干净退出标记：

```python
import atexit

@atexit.register
def _mark_clean_exit():
    """进程正常退出时写入标记文件。"""
    marker = Path.cwd() / "data" / ".clean_exit_marker"
    marker.parent.mkdir(exist_ok=True)
    marker.touch()

def _check_last_exit_was_clean() -> bool:
    """检查上次退出是否干净。"""
    marker = Path.cwd() / "data" / ".clean_exit_marker"
    if marker.exists():
        marker.unlink()  # 消费标记
        return True
    return False  # 无标记 = 上次是不干净退出
```

### 7.6 跳闸后的行为

```
断路器跳闸时：
  ├─ 启动 HTTP 服务（允许 API 访问）
  ├─ 跳过 CronService 自动启动（不导入 cron base 模块）
  ├─ 跳过 HeartbeatService 启动
  ├─ 跳过 Sweeper 启动
  ├─ 跳过 Curator 启动
  └─ 日志输出恢复指引

自愈条件：
  └─ 5分钟窗口耗尽后，下次启动自动恢复（is_tripped=False）

手动恢复：
  └─ 删除 data/boot_lifecycle.json
```

---

## 8. 实现步骤

### 8.1 实现阶段

```
第一阶段：乒乓 + 参数搅动检测（方案 3+4，无依赖）
  ├── 修改 agent/middlewares/tool_guardrails.py
  │   ├── ToolCallGuardrailConfig 新增 ping_pong_* 和 arg_churn_* 字段
  │   ├── _TurnGuardrailState 新增 ping_pong_counts, arg_churn_variants
  │   ├── _evaluate 新增 _evaluate_ping_pong 和 _evaluate_argument_churn
  │   └── 消息生成方法新增 ping_pong 和 argument_churn 描述
  └── 单元测试

第二阶段：恢复模式（方案 6，依赖 3+4 的新 BLOCK 触发）
  ├── 修改 agent/middlewares/tool_guardrails.py
  │   ├── ToolCallGuardrailConfig 新增 recovery_mode_* 字段
  │   ├── _TurnGuardrailState 新增 recovery_mode, recovery_violation_count
  │   ├── _evaluate 修改副作用应用逻辑
  │   └── _wrap_tool_call_precheck 修改恢复模式放行逻辑
  └── 集成测试

第三阶段：Cron 任务断路器（防御 A，独立）
  ├── 修改 skills/builtin/core/cron/scripts/base.py
  │   ├── CronJobFailureState 数据结构
  │   ├── CronService 新增 DEGRADED_THRESHOLD/DISABLED_THRESHOLD
  │   ├── _on_cron_job 新增失败跟踪和断路逻辑
  │   └── _notify_cron_disabled 通知逻辑
  ├── 修改 server/trigger/http/cron.py
  │   └── 新增 reset-failures API
  └── 集成测试

第四阶段：周期服务退避（防御 B，依赖 Sweeper 接线）
  ├── 新增 runtime/periodic_backoff.py
  ├── 修改 skills/builtin/core/heartbeat/scripts/base.py
  │   └── 集成 PeriodicBackoff
  ├── 修改 agent/tools/subagent/registry/sweeper.py
  │   └── 集成 PeriodicBackoff
  ├── 修改 server/trigger/channels/core.py
  │   └── 接线 start_sweeper()
  └── 集成测试

第五阶段：崩溃循环断路器（防御 C，独立）
  ├── 新增 runtime/crash_loop_breaker.py
  ├── 修改 server/__main__.py
  │   └── 启动时检查断路器
  └── 集成测试
```

### 8.2 测试计划

| 测试                                | 类型 | 验证内容                             |
| ----------------------------------- | ---- | ------------------------------------ |
| `test_ping_pong_detection`          | 单元 | A→B→A→B 交替 4轮 → WARN, 6轮 → BLOCK |
| `test_ping_pong_different_progress` | 单元 | A→B 交替但有一方有进展 → 不触发      |
| `test_arg_churn_detection`          | 单元 | 3组参数各3次无进展 → WARN            |
| `test_arg_churn_different_results`  | 单元 | 3组参数但结果不同 → 不触发           |
| `test_recovery_mode_enter`          | 集成 | 首次 BLOCK → recovery_mode=True      |
| `test_recovery_mode_retry`          | 集成 | 恢复模式解除 block → 模型重试成功    |
| `test_recovery_mode_halt`           | 集成 | 恢复模式内 2次 critical → HALT       |
| `test_cron_degraded`                | 集成 | cron 任务 5次失败 → 降级+退避        |
| `test_cron_auto_disable`            | 集成 | cron 任务 10次失败 → 自动禁用+通知   |
| `test_cron_stable_reset`            | 集成 | 降级后运行>60s成功 → 重置            |
| `test_periodic_backoff`             | 单元 | 连续失败 → 间隔翻倍 → 上限           |
| `test_periodic_backoff_success`     | 单元 | 成功 → 重置间隔                      |
| `test_sweeper_wired`                | 集成 | sweeper 被正确启动                   |
| `test_crash_loop_trip`              | 集成 | 3次不干净启动 → 跳闸                 |
| `test_crash_loop_clean`             | 集成 | 干净启动 → 不跳闸                    |
| `test_crash_loop_self_heal`         | 集成 | 窗口耗尽 → 自动恢复                  |

---

## 9. 配置项汇总

### 9.1 ToolCallGuardrailConfig 新增字段

| 参数                              | 默认值 | 说明                       |
| --------------------------------- | ------ | -------------------------- |
| `ping_pong_warn_after`            | 4      | 乒乓交替 4 轮 → WARN       |
| `ping_pong_block_after`           | 6      | 乒乓交替 6 轮 → BLOCK      |
| `arg_churn_min_variants`          | 3      | 参数搅动至少 3 组变体      |
| `arg_churn_min_calls_per_variant` | 3      | 每组至少 3 次调用          |
| `arg_churn_warn_after`            | 3      | 3 变体 → WARN              |
| `arg_churn_block_after`           | 5      | 5 变体 → BLOCK             |
| `recovery_mode_enabled`           | True   | 启用恢复模式               |
| `recovery_max_violations`         | 1      | 恢复期内允许 1 次 critical |

### 9.2 CronService 断路配置

| 参数                      | 默认值 | 说明                      |
| ------------------------- | ------ | ------------------------- |
| `DEGRADED_THRESHOLD`      | 5      | 5次连续失败 → 降级        |
| `DISABLED_THRESHOLD`      | 10     | 10次连续失败 → 自动禁用   |
| `STABLE_RUN_THRESHOLD_S`  | 60     | 运行>60s成功 → 重置计数   |
| `DEGRADE_BACKOFF_BASE_MS` | 5000   | 降级退避基础延迟          |
| `DEGRADE_BACKOFF_MAX_MS`  | 300000 | 降级退避最大延迟（5分钟） |

### 9.3 PeriodicBackoff 配置

| 参数                       | HeartbeatService | Sweeper    | 说明         |
| -------------------------- | ---------------- | ---------- | ------------ |
| `base_interval`            | 1800 (30min)     | 60 (60s)   | 基础间隔     |
| `backoff_factor`           | 2.0              | 2.0        | 退避倍数     |
| `max_interval`             | 7200 (2h)        | 300 (5min) | 最大间隔     |
| `max_consecutive_failures` | 5                | 5          | 连续失败上限 |

### 9.4 CrashLoopBreaker 配置

| 参数                 | 默认值 | 说明                 |
| -------------------- | ------ | -------------------- |
| `UNCLEAN_THRESHOLD`  | 3      | 3次不干净启动 → 跳闸 |
| `WINDOW_S`           | 300    | 5分钟窗口            |
| `RECORD_RETENTION_S` | 86400  | 记录保留24小时       |

### 9.5 新增文件清单

```
runtime/
├── periodic_backoff.py          # PeriodicBackoff 周期服务退避
└── crash_loop_breaker.py         # CrashLoopBreaker 崩溃循环断路器
```

### 9.6 修改文件清单

```
agent/middlewares/tool_guardrails.py           # 方案 3+4+6
skills/builtin/core/cron/scripts/base.py       # Cron 防御 A
server/trigger/http/cron.py                    # Cron 防御 A API
skills/builtin/core/heartbeat/scripts/base.py # Cron 防御 B
agent/tools/subagent/registry/sweeper.py       # Cron 防御 B + 接线
server/trigger/channels/core.py               # Cron 防御 B 接线
server/__main__.py                             # Cron 防御 C
```
