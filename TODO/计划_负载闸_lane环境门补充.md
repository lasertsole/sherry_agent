# 计划:负载闸 —— SUBAGENT lane 的环境门补充(实现计划)

> 一句话定位:sherry 的 lane 是**计数型** backpressure(SUBAGENT cap=8,管"几个"),
> 负载闸补的是**环境感知**那半边(管"机器还扛得住吗")。两者正交:先过负载闸,再争 lane slot。
> 灵感来自 DSH 工作台(`D:\selfProj\luanyujia-workbench`)的 `负载闸.mjs`
> (CPU 3 次采样均值 + 内存即时读,90% 不派单/95% 停重活,退出码 0/10/20)。

## 一、现状与缺口

**lane 现状**(`runtime/lane/core.py`):`Lane = asyncio.Semaphore + 计数器`,`lane_slot(LaneType.SUBAGENT)` 是 async context manager。cap 来自 `LANE_SYSTEM["subagent_max_concurrent"]`(默认 8,`config/features/infra_side/lane_system.py:43`)。cap 满时 PENDING 排队 FIFO,不拒绝。

**接入点**(`agent/tools/subagent/spawn/core.py:882-926` `_execute_subagent_with_lane`):

```python
async with lane_slot(LaneType.SUBAGENT):          # line 894 争 slot
    current = get_run(run.run_id)                  # line 895 拿到后检查还活着
    if current.execution.status == ExecutionStatus.PENDING:
        current = mark_run_running(run.run_id)     # line 901 promote PENDING→RUNNING
    await _execute_subagent(...)                   # line 908 真跑
```

所有过 SUBAGENT lane 的 run(taskflow auto-dispatch 派的、`sessions_spawn` 显式派的)都走这里——**一处接入,全覆盖**。

**缺口**:机器 CPU/内存已 95% 时, lane 仍会派第 8 个子代理。无任何环境感知 backpressure。

## 二、设计

### 2.1 阈值策略:只硬停 95%,90% 不阻断

DSH 的 90%=停止派单 / 95%=停重活两档。sherry **只借 95% 硬停档**,90% 降级为 advisory:

| 负载 | sherry 行为 | 理由 |
| --- | --- | --- |
| 都 < 90% | 正常进 lane | — |
| 任一 ≥ 90%(且 <95%) | **不阻断**,进 lane 排队,记 `load_warn` 日志 | lane cap=8 已是计数 backpressure;90% 时让 cap 兜底,不重复造门 |
| 任一 ≥ 95% | **硬停**:不进 lane,run 保持 PENDING,推 `load_warning` WS 事件,退避重检 | 机器快爆了,再派只会一起卡死 |

### 2.2 接入:lane_slot 之前的 while 前置门(共享快照,零阻塞)

在 `_execute_subagent_with_lane` 的 `async with lane_slot(...)` **之前**加 while 循环。**关键:读共享负载快照,不直接阻塞采样**(见 §3.1 的 `_get_load_gate`):

```python
async def _execute_subagent_with_lane(...):
    from runtime.lane import LaneType, lane_slot
    from agent.tools.pub_base.load_gate import _get_load_gate
    defer_started = time.monotonic()
    try:
        while True:                                    # ← 前置门
            gate = await _get_load_gate()              # 读缓存快照,零阻塞(过期才 to_thread 采一次)
            if not gate.stop:
                break                                  # ok/warn → 进 lane
            # stop(95%):不进 lane,run 保持 PENDING
            if time.monotonic() - defer_started > LOAD_GATE_MAX_DEFER_S:
                await _fail_run_load_timeout(run)      # ← 显式标失败,不能只 return(见 §2.3)
                return
            await _notify_load_warning(run, gate)      # 推 WS 事件(节流,模块级 set)
            await asyncio.sleep(LOAD_GATE_RETRY_S)     # 退避(默认 30s)
            current = get_run(run.run_id)
            if current is None or _is_run_killed(current):
                return                                 # 等待期间被 kill
        logger.debug("subagent run {} waiting for a SUBAGENT lane slot", run.run_id)
        async with lane_slot(LaneType.SUBAGENT):
            ...  # 原有 promote + execute 不变
    finally:
        from ..registry import remove_task
        remove_task(run.run_id)
```

**为什么放在 lane_slot 之前而不是之后**:放在之前 → stop 时不占 lane slot;放在之后 → stop 时白白消耗一个 slot 再释放。前置门让负载闸与 lane 完全解耦。

### 2.3 退避、max_defer 失败与 kill 响应(审计修订)

- **退避**:`LOAD_GATE_RETRY_S`(默认 30s)。stop 时 sleep 后重检,不忙等。响应延迟最长 ~`retry_s`(共享快照后台/lazy 刷新,采样不额外占路)。
- **max_defer 超时必须显式标失败,不能只 return**:`defer_started` 累计超 `LOAD_GATE_MAX_DEFER_S`(默认 600s)→ 调 `_fail_run_load_timeout(run)`,该函数走 run_manager 的 outcome 终结路径(设 `status=TERMINAL` + `ended_reason="load_timeout"`)。**不能只 return**——只 return 会让 run 变 orphan(task 没了行还在),sweeper 的 `recover_orphaned_runs` + `schedule_orphan_recovery`(`sweeper.py:74,88`)会**重新调度**它 → 又撞负载闸 → 又 max_defer → **死循环**。
  - **待确认**:`mark_run_running`(`run_manager.py:153`)检查 `status in (PENDING, INTERRUPTED)` 才 promote;失败/终结路径(`run_manager.py:195-225` 的 outcome 函数)对 **PENDING** run 是否可用需确认——若 PENDING 不能直接标 TERMINAL,需先 promote 再 fail,或有专门的 cancel 路径(`save_kill_reconciliation` `run_manager.py:310` 可能适用)。落地前先确认这个 API。
- **kill 响应**:sleep 退避期间 run 被 kill → `_is_run_killed(current)` 检查 → 立即 return。**待确认**:PENDING run 被 kill(`save_kill_reconciliation`)时 `execution.status` 变什么——若 kill 不把 PENDING 标 TERMINAL,`_is_run_killed` 不能只查 TERMINAL,需查 `killed_at` 是否非空或 `kill_reconciliation` 是否存在(`SubagentRunRecord` 有 `kill_reconciliation: KillReconciliationState | None` 字段)。

### 2.4 fail-open

`check_load_gate()` 内部:`psutil` 调用异常(库缺失/采样失败)→ 返回 `ok=True` + `logger.warning`。**派单不能因监控工具挂了而停摆**。共享快照初始值也是 ok verdict(`_load_snapshot` 启动时 fail-open)。

### 2.5 判据唯一出处

CPU/内存阈值、采样方式只在 `load_gate.py` 一处定义。配置项只调阈值数字,不在别处重算。

## 三、实现

### 3.1 新文件 `agent/tools/pub_base/load_gate.py`(共享快照 + 零阻塞)

```python
"""SUBAGENT lane 的环境门:派单前检查机器负载,95% 硬停。

判据只在此一处定义。psutil 已是依赖(uv.lock)。fail-open:psutil 异常 → ok。
关键:不直接在 async 路径阻塞采样 —— 用共享快照(_get_load_gate),TTL 内读缓存,
过期才 asyncio.to_thread 采一次(全局共享,所有 run 读同一份)。
"""
import asyncio
import time
import psutil
from dataclasses import dataclass
from loguru import logger
from config.features import LANE_SYSTEM

@dataclass(frozen=True)
class LoadGateVerdict:
    ok: bool
    stop: bool
    cpu: float
    ram: float

# 共享快照:全局负载状态,所有 run 读同一份。初始 fail-open。
_load_snapshot: LoadGateVerdict = LoadGateVerdict(ok=True, stop=False, cpu=0.0, ram=0.0)
_snapshot_at: float = 0.0
_snapshot_lock = asyncio.Lock()

def _sample_cpu_mean(times: int, interval: float) -> float:
    """CPU 3 次采样取均值(单次太抖,仿 DSH)。阻塞调用,只在 to_thread 里跑。"""
    samples = [psutil.cpu_percent(interval=interval) for _ in range(times)]
    return sum(samples) / len(samples)

def check_load_gate() -> LoadGateVerdict:
    """阻塞采样(4.5s)。仅供 to_thread 调用,不要直接在 async 路径调。"""
    if not LANE_SYSTEM["load_gate_enabled"]:
        return LoadGateVerdict(ok=True, stop=False, cpu=0.0, ram=0.0)
    try:
        cpu = _sample_cpu_mean(3, LANE_SYSTEM["load_gate_sample_interval_s"])
        vm = psutil.virtual_memory()
        ram = (vm.total - vm.available) / vm.total * 100.0
    except Exception:
        logger.warning("load gate: psutil sampling failed, fail-open")
        return LoadGateVerdict(ok=True, stop=False, cpu=-1.0, ram=-1.0)
    stop = (cpu >= LANE_SYSTEM["load_gate_cpu_stop"]
            or ram >= LANE_SYSTEM["load_gate_ram_stop"])
    if cpu >= LANE_SYSTEM["load_gate_cpu_warn"] or ram >= LANE_SYSTEM["load_gate_ram_warn"]:
        if not stop:
            logger.warning("load gate: warn cpu={:.0f}% ram={:.0f}% (proceed, lane cap backpressure)", cpu, ram)
    return LoadGateVerdict(ok=not stop, stop=stop, cpu=cpu, ram=ram)

async def _get_load_gate() -> LoadGateVerdict:
    """读共享快照(零阻塞);TTL 过期才 asyncio.to_thread 采一次(锁内 double-check,
    第一个发现过期的 run 采样,其他 run 等锁后读缓存)。"""
    global _load_snapshot, _snapshot_at
    if time.monotonic() - _snapshot_at < LANE_SYSTEM["load_gate_snapshot_ttl_s"]:
        return _load_snapshot
    async with _snapshot_lock:
        if time.monotonic() - _snapshot_at < LANE_SYSTEM["load_gate_snapshot_ttl_s"]:
            return _load_snapshot  # double-check:其他 run 已刷新
        _load_snapshot = await asyncio.to_thread(check_load_gate)
        _snapshot_at = time.monotonic()
        return _load_snapshot
```

**采样开销**:阻塞 4.5s 只发生在 `to_thread`(不阻塞 event loop),且每 `snapshot_ttl_s`(默认 5s)全局只一次——N 个 run 同时进入也只采一次(其他等锁读缓存)。响应延迟:负载降下来后,最长等 `snapshot_ttl_s`(5s 快照刷新)+ `retry_s`(30s 退避)≈ 35s 重检。

### 3.2 改 `agent/tools/subagent/spawn/core.py:882-926`

按 §2.2 骨架改 `_execute_subagent_with_lane`:`async with lane_slot(...)` 之前加 while + `await _get_load_gate()`(零阻塞)+ `_fail_run_load_timeout` + `_notify_load_warning` + kill 检查。

**`_notify_load_warning` 节流**:模块级 `_load_warned_runs: set[str]`(仿 `announce/delivery.py:38 _delivered_keys`),不改 `SubagentRunRecord` schema(该 record 无 generic metadata 字段,`types/registry.py:91-120` 只有具体字段)。同一 run 至多推 1 次 `load_warning`;run 终结时从 set 清理(在 `finally` 的 `remove_task` 旁)。

**`_fail_run_load_timeout(run)`**:走 run_manager 的 outcome 终结路径,设 `status=TERMINAL` + `ended_reason="load_timeout"`。**落地前确认** PENDING run 能否直接走该路径(见 §2.3 待确认)。

**`_is_run_killed(current)`**:不只查 `TERMINAL`——查 `kill_reconciliation is not None` 或 `killed_at`(PENDING run 被 kill 时 status 可能不标 TERMINAL,见 §2.3 待确认)。

### 3.3 配置 `config/features/infra_side/lane_system.py`

`LaneSystemConfig` TypedDict 加字段:

```python
    # ↓ 新增:负载闸(SUBAGENT lane 的环境门)
    load_gate_enabled: bool              # 默认 False(安全灰度;确认 psutil 打包后显式开 True)
    load_gate_cpu_warn: int              # advisory 阈值(默认 90,只记日志不阻断)
    load_gate_cpu_stop: int              # 硬停阈值(默认 95)
    load_gate_ram_warn: int              # 默认 90
    load_gate_ram_stop: int              # 默认 95
    load_gate_sample_interval_s: float   # CPU 单次采样间隔(默认 1.5)
    load_gate_snapshot_ttl_s: float      # 共享快照 TTL(默认 5s;过期才 to_thread 采一次)
    load_gate_retry_s: float             # stop 退避重检间隔(默认 30)
    load_gate_max_defer_s: float         # 累计退避上限,超过显式标失败(默认 600)
```

`LANE_SYSTEM` 默认值:`"load_gate_enabled": False`(安全灰度,与 §四打包坑一致——psutil 打包未确认前不自动开)、`90/95/90/95/1.5/5/30/600`。部署确认 psutil 打包 + 三平台烟测后显式设 `True`。

### 3.4 WS 事件

stop 时推 `load_warning`,复用 `runtime/session/ws_push.py::broadcast_to_session`(与 `budget_warning` 同款通道):

```python
frame = {"event": "load_warning", "session_id": run.requester_session_key,
         "content": {"cpu": gate.cpu, "ram": gate.ram, "run_id": run.run_id}}
await broadcast_to_session(run.requester_session_key, frame)
```

前端(`composables/ws.ts` / `agent-socket-frames.ts`)映射到 `ws:load_warning`,`ChatBox.vue` 渲染紧凑提示(四语言 `chatBox.loadWarning`)。in-memory only,重载消失。

### 3.5 max_defer 失败路径(落地前确认)

`_fail_run_load_timeout` 必须把 PENDING run 标成 TERMINAL(不只 return,否则 sweeper orphan recovery 重调度 → 死循环)。确认事项:
- `run_manager.py:195-225` 的 outcome 终结函数名 + 对 PENDING run 是否可用;
- 若 PENDING 不能直接标 TERMINAL,备选:`save_kill_reconciliation`(`run_manager.py:310`)可能适用(它标记 killed + 设 `killed_at`);
- 最坏情况:先 `mark_run_running` promote 再 fail(多一步,但保证 status 转换合法)。

## 四、边界与不变量

1. **负载闸与 lane cap 正交**:负载闸在 lane_slot **之前**,stop 时不占 slot;90% 区间由 cap 兜底,95% 由负载闸接管,各管一段不重叠。
2. **零阻塞 event loop**:`_get_load_gate()` 读共享快照;阻塞采样只在 `asyncio.to_thread` 里跑,每 `snapshot_ttl_s`(5s)全局一次。N 个 run 同时进入只采一次(锁 + double-check)。
3. **fail-open 到 ok**:psutil 挂了不阻断派单;但 stop(95%)必须真拒绝——不能 fail-open 到 stop。
4. **判据唯一**:CPU/内存阈值只在 `load_gate.py` + `LANE_SYSTEM` 一处。
5. **退避不忙等**:stop 时 sleep `retry_s`(30s),不 while-spin。
6. **max_defer 超时显式标失败**:超 `max_defer_s`(600s)→ `_fail_run_load_timeout` 标 TERMINAL,**不只 return**(否则 sweeper orphan recovery 重调度 → 死循环)。
7. **kill 响应**:sleep 退避期间被 kill → `_is_run_killed`(查 `kill_reconciliation`/`killed_at`,不只查 TERMINAL)→ 立即 return。
8. **不触发执行**:负载闸只决定"进不进 lane",不 re-dispatch、不 spawn 新 run。
9. **`load_gate_enabled=False` = 零行为变更 + 默认安全**:默认 False,psutil 打包未确认的环境不会自动开启。
10. **psutil 打包(win/mac/linux)**:psutil 是传递依赖(`pyproject.toml` 未显式声明)+ 全仓零 `import`,负载闸首次引入。PyInstaller onedir 静态分析可能漏收 → `build_backend.py` 显式加 `hiddenimports`;psutil 带 C 扩展(每平台一个 wheel),三平台 CI 各自构建含当前平台二进制。**落地必做**:`hiddenimports` + 三平台烟测。
11. **step DISPATCHED vs run PENDING 状态不一致(已知,接受)**:负载闸 stop 时,step 已 `DISPATCHED`(`taskflow_dispatch.py:201`)+ `dispatched_at` 已 stamp,但 run 是 PENDING(没开始跑)。`_build_taskflow_block` 的 next_hint(`prompt_builder.py:330`)会显示"step X 派出 N 分钟",主代理可能误以为子代理跑了 N 分钟没回,实际卡在负载闸。**接受**:`taskflow_summary` 不显示 run 状态,但主代理 `taskflow_inspect` 能看到 step DISPATCHED;不加 step 字段区分(破坏"step 状态枚举不变"原则)。文档说明此不一致。
12. **PENDING run 挂 while-sleep 协程上限**:负载高时所有 PENDING run 挂 while-sleep 协程。10 个 step 被挡 = 10 个协程(sleep 不占资源,可接受),靠 `max_defer_s=600s` 兜底,无显式协程数上限。

## 五、测试

落在 `tests/agent/tools/pub_base/test_load_gate.py` + `tests/agent/tools/subagent/spawn/test_load_gate_integration.py`:

1. **阈值三档**(mock `psutil.cpu_percent`/`virtual_memory`):都<90% → `ok=True,stop=False`;91% → `ok=True,stop=False`+warn 日志;96% → `stop=True`。
2. **反例**(DSH 五步法第 4 步):`load_gate_cpu_stop=1` 必然越线 → `stop=True`(验证检查不是假的)。
3. **fail-open**:mock psutil 抛 `Exception` → `ok=True,stop=False` + warning 日志。
4. **`enabled=False`**:`check_load_gate()` 直接返回 ok,不调 psutil。
5. **共享快照零阻塞**:mock `check_load_gate` 返回 stop;N 个并发 `_get_load_gate()` 调用只触发**一次** `to_thread` 采样(锁 + double-check);TTL 内重复调用不重采。
6. **接入集成**:mock `_get_load_gate` 返回 stop → `_execute_subagent_with_lane` 不进 `lane_slot`、run 保持 PENDING、推 1 次 `load_warning`;返回 ok → 正常 promote+execute。
7. **max_defer 显式标失败(不死循环)**:mock 持续 stop + `max_defer_s=2` → 2s 后 `_fail_run_load_timeout` 被调(run 标 TERMINAL + `ended_reason="load_timeout"`),**不只 return**;断言 sweeper 的 `schedule_orphan_recovery` 不会被该 run 触发(它已 TERMINAL 不是 orphan)。
8. **kill 响应**:退避 sleep 期间 run 被 kill(设 `kill_reconciliation`)→ `_is_run_killed` 返回 True → 立即 return,不继续重检、不等到 max_defer。
9. **节流**:持续 stop 多轮 → `load_warning` 只推 1 次(同 run,`_load_warned_runs` set);run 终结后从 set 清理。
10. **三平台打包烟测**:win/mac/linux 各自 `packaging/build_backend.py` 产物跑 `import psutil; psutil.cpu_percent(interval=0.1); psutil.virtual_memory()` 不抛 `ImportError`/`OSError`(C 扩展二进制进 bundle)。`.github/workflows/desktop-build.yml` 三平台 job 各加一步。

测试遵循 `run_tests_split.py` 分组;mock psutil 用 `monkeypatch.setattr`;打包烟测是 CI 步骤(非 pytest,放 `desktop-build.yml`)。

## 六、可选扩展(默认不做)

- **NUDGE lane**(memory review):NUDGE 也吃 LLM 资源,但 memory review 是单次、cache-warm、轻量。暂不接;若观测到拖垮机器,再在 `lane_slot(LaneType.NUDGE)` 前加同款前置门。
- **MAIN lane**:主代理 turn 是用户直接请求,负载闸拦它会让用户感觉"卡住"。不接。
- **动态降 cap**:负载 90% 时把 `subagent_max_concurrent` 从 8 降到 4(`Lane.set_max` 支持热更新,`core.py:155`)。引入"cap 随负载抖动"复杂度,默认不做。

## 七、实施顺序

1. **确认 PENDING 失败 API**(§3.5):读 `run_manager.py:195-310`,确认 PENDING run 走 outcome 终结/kill 路径的函数名 + 可用性。**这是 §2.3 max_defer 不死循环的前提,先确认再写代码**。
2. 写 `load_gate.py`(共享快照 + `check_load_gate` + `_get_load_gate`)+ 单测(§5.1-5.5)——独立,不碰 spawn。
3. 加 `LANE_SYSTEM` 配置字段 + 默认值(`enabled=False`)。
4. 改 `_execute_subagent_with_lane` 接入 while 前置门 + `_fail_run_load_timeout` + `_notify_load_warning` + kill 检查——集成测试(§5.6-5.9)。
5. 前端 `load_warning` 映射 + `ChatBox` 提示渲染。
6. **打包确认**:`build_backend.py` 加 psutil `hiddenimports`;三平台 CI 各跑一次烟测(§5.10)。
7. `enabled=False` 灰度上线 → 确认 psutil 打包 + 烟测通过 → 显式设 `True` 开启 → 观察。

验收:`tests/agent/tools/pub_base/test_load_gate.py` + `tests/agent/tools/subagent/spawn/test_load_gate_integration.py` 全绿;`load_gate_enabled=False` 时既有 spawn 测试零变更;max_defer 超时显式标失败(不死循环);三平台 PyInstaller 产物 `import psutil` 烟测通过。
