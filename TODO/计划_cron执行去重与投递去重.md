# 计划：cron 执行去重 + 投递去重

- 建档日期：2026-10-21
- 状态：方案设计，未开工
- 来源：审计发现（cron 无执行去重 + 无投递去重 + 崩溃双发窗口）+ 跨项目调研（openclaw `markCronJobActive` + `buildDirectCronDeliveryIdempotencyKey`）
- 严重性：中（跨 loop 竞态 + 崩溃双发 + 手动触发竞态 → 重复执行 + 重复投递 → 用户收到两条相同回复）
- 借鉴：openclaw `cron/active-jobs.ts`（`markCronJobActive`/`isCronJobActive`）+ `cron/isolated-agent/delivery-dispatch-policy.ts`（`buildDirectCronDeliveryIdempotencyKey`）

---

## 0. 问题

### 0.1 三个双发场景

| # | 场景 | 根因 | 后果 |
|---|---|---|---|
| 1 | **跨 loop 竞态**：scheduled tick 在 cron loop 跑 `_execute_job`（agent 在跑，`next_run_at_ms` 还没 advance），用户点"Run" → `run_job` 在 server loop 跑同一个 job | `_execute_job` 无 running flag，`run_job` 无 reentrancy 检查 | 两个 agent 并发跑同一 job → 两条 `publish_inbound` → 两条回复 |
| 2 | **崩溃双发**：`_on_cron_job` 在 `bus.publish_inbound`（投递结果）之后、`_save_store`（advance `next_run_at_ms`）之前崩溃 | `next_run_at_ms` 没在执行前 advance | 重启后 `_recompute_next_runs` 看到 `next_run_at_ms` 在过去 → 重新 fire → 用户已收到 pre-crash 结果 + 再收一条 |
| 3 | **手动双触发**：用户快速点两次"Run" | `run_job`（`base.py:905-916`）无幂等 | 两次 `_execute_job` → 两条回复 |

### 0.2 投递去重被绕过

`_on_cron_job` 构建 `InboundMessage(...)` 时**不设 `metadata["message_id"]`**（`base.py:605-610`）：

```python
msg: InboundMessage = InboundMessage(
    channel=channel,
    sender_id="cron tool",
    chat_id=to,
    content=res,
)  # ← 无 metadata，client_msg_id = None
```

`submit_user_input` 的 `client_msg_id` 去重（`input_queue_service.py:404-414`）只在 `client_msg_id is not None` 时跑——cron 投递的 `client_msg_id = None`（`channels/core.py:278` 从 `metadata.get("message_id")` 取），所以去重被跳过。

### 0.3 当前 `CronJobState` 无 running flag

`CronJobState`（`types.py:47-54`）只有 `next_run_at_ms` / `last_run_at_ms` / `last_status` / `last_error`——**没有 `running` flag、没有 `last_run_id`、没有投递状态**。`CronRunRecord`（`types.py:37-44`）存在但是死代码。

### 0.4 openclaw 的两层防重

| openclaw 机制 | 位置 | sherry 对标 |
|---|---|---|
| `markCronJobActive(jobId)` / `isCronJobActive(jobId)` | `cron/active-jobs.ts:91,202` | 本计划 §1.1 执行去重 |
| `buildDirectCronDeliveryIdempotencyKey(jobId, runStartedAt, routeIdentity)` | `delivery-dispatch-policy.ts:246` | 本计划 §1.2 投递去重 |
| `isCompletedDirectCronDelivery(id)` | `delivery-dispatch-policy.ts:269` | 本计划 §1.2 |
| `DedupeCache`（TTL/LRU/ownerToken） | `infra/dedupe.ts:27` | 本计划 §1.2 用现有 `client_msg_id` 去重，不新建 cache |

---

## 1. 设计

### 1.1 执行去重——`running` flag + 预推进 `next_run_at_ms`

**文件**：`skills/builtin/core/cron/scripts/types.py` + `base.py`

#### 1.1.1 `CronJobState` 加 `running` + `last_run_id`

```python
# types.py:47-54
@dataclass
class CronJobState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    running: bool = False               # ★ 新增：是否正在执行（跨 loop 防重）
    last_run_id: str | None = None       # ★ 新增：最后一次执行的 slot 标识（崩溃恢复用）
```

**持久化**：`running` 和 `last_run_id` 持久化到 `cron_jobs.json`——`_save_store`（`base.py:316-359`）加这两个字段。重启后能看到"这个 job 在崩溃前正在跑"。

#### 1.1.2 `_execute_job` 加执行去重

```python
# base.py::_execute_job（:450-490 改造）
async def _execute_job(self, job: CronJob) -> None:
    # ★ 执行去重：如果 job 已经在跑，跳过
    if job.state.running:
        logger.debug("[cron] job {} already running, skip", job.id)
        return

    # ★ 生成 run_id（slot 标识）= job_id:slot_ms
    slot_ms = job.state.next_run_at_ms or int(time.time() * 1000)
    run_id = f"cron:{job.id}:{slot_ms}"

    # ★ 预推进：执行前 advance next_run_at_ms + 设 running + 存 run_id + 存盘
    #    崩溃恢复：重启后看到 running=True + last_run_id → 不重复 fire（next_run 已 advance）
    job.state.running = True
    job.state.last_run_id = run_id
    job.state.next_run_at_ms = self._compute_next_run(job, after_ms=slot_ms)
    self._save_store()  # ★ 存盘——在 agent 执行之前

    try:
        # 现有执行逻辑
        result = await self.on_job(job)  # _on_cron_job
        job.state.last_status = "ok"
        job.state.last_run_at_ms = slot_ms
    except Exception as e:
        job.state.last_status = "error"
        job.state.last_error = str(e)[:500]
        logger.exception("[cron] job {} failed", job.id)
    finally:
        # ★ 清 running flag + 存盘
        job.state.running = False
        self._save_store()

    # WS notification（现有逻辑）
    ...
```

**关键改动**：`next_run_at_ms` 在**执行前** advance + 存盘——不是执行后。如果 agent 执行中崩溃，重启后 `next_run_at_ms` 已在将来 → 不重新 fire。

#### 1.1.3 `run_job`（手动触发）也检查 running

```python
# base.py::run_job（:905-916 改造）
async def run_job(self, job_id: str, *, force: bool = False) -> dict:
    job = self._get_job(job_id)
    if job is None:
        return {"ok": False, "error": "not found"}
    if job.state.running and not force:
        return {"ok": False, "error": "already running", "job_id": job_id}
    # force=True 时跳过 running 检查（管理员强制重跑）
    await self._execute_job(job)
    return {"ok": True, "job_id": job_id}
```

#### 1.1.4 崩溃恢复

`start()` → `_recompute_next_runs()`（`base.py:379-397`）加一步：

```python
# _recompute_next_runs 内
for job in store.jobs:
    if job.state.running:
        # 崩溃前正在跑——清 running flag（agent 进程已死）
        job.state.running = False
        # next_run_at_ms 已在执行前 advance → 不重新 fire
        # last_run_id 保留——投递去重用（§1.2）
        logger.info("[cron] job {} was running at crash, cleared running flag", job.id)
    # ... 现有 recompute 逻辑 ...
```

### 1.2 投递去重——`metadata["message_id"]` = run_id

**文件**：`skills/builtin/core/cron/scripts/base.py::_on_cron_job`

**借鉴 openclaw**：`buildDirectCronDeliveryIdempotencyKey(jobId, runStartedAt, routeIdentity)`——`cron:{jobId}:{slotMs}` 作为投递幂等键。

**sherry 复用现有 `client_msg_id` 去重**——不新建 cache，给 `InboundMessage.metadata` 加 `message_id` = `run_id`：

```python
# base.py::_on_cron_job（:603-611 改造）
# run_id 从 _execute_job 传过来（或从 job.state.last_run_id 读）
run_id = job.state.last_run_id or f"cron:{job.id}:{int(time.time()*1000)}"

msg: InboundMessage = InboundMessage(
    channel=channel,
    sender_id="cron tool",
    chat_id=to,
    content=res,
    metadata={"message_id": run_id},  # ★ 投递去重键
)
await bus.publish_inbound(msg)
```

**效果**：`_process_inbound`（`channels/core.py:278`）从 `metadata.get("message_id")` 取 `client_msg_id = run_id`。`submit_user_input`（`input_queue_service.py:404-414`）检查 `client_msg_id is not None` → 命中现有去重：

- 第一次投递：`client_msg_id = "cron:abc123:1697000000000"` → 不在 ACTIVE 行里 → 入队/执行
- 第二次投递（崩溃双发 / 跨 loop 竞态）：相同 `client_msg_id` → 命中 ACTIVE 行 → 返回 `DEDUPED` → 不重复执行

**`run_id` 的全局唯一性**：`cron:{job_id}:{slot_ms}`——`job_id` 是 per-job uuid，`slot_ms` 是 `next_run_at_ms`（每次 fire 不同）。同一 job 不同 slot 的 `run_id` 不同 → 允许合法的重复投递（不同时间 fire 的不同结果）。同一 slot 两次投递的 `run_id` 相同 → 去重。

**不需要新建 `DedupeCache`**——直接复用 `submit_user_input` 的 `client_msg_id` 去重。openclaw 自建 `DedupeCache` 是因为它的投递路径不走 input queue——sherry 的 cron 投递走 `publish_inbound` → `submit_user_input`，已有去重基础设施。

### 1.3 预推进 `next_run_at_ms` 的失败处理

**问题**：`next_run_at_ms` 在执行前 advance——如果 agent 执行失败，`next_run` 已在将来，job 不会立即重试。

**这是正确的**：cron job 失败后等下一个 slot 重试，不立即重试——跟现有行为一致（现有也是执行后 advance，失败后等下个 slot）。唯一区别是 advance 时机从"执行后"提前到"执行前"——对失败处理的语义不变。

**failure breaker 不受影响**：breaker（`base.py:525-558`）计 consecutive failures + 指数退避——它在 `_on_cron_job` 内（`_execute_job` 的 `self.on_job` callback），跟 `next_run_at_ms` advance 时机无关。

### 1.4 配置

**文件**：`config/features/infra_side/cron_service.py`

不需要新配置——`running` flag 和 `last_run_id` 是 `CronJobState` 的字段，不是阈值。如果未来要加"running flag 超时自动清除"（防 stuck running），加：

```python
running_flag_timeout_seconds: int  # 默认 3600（running=True 超过 1h 自动清——防 stuck）
```

**本计划不加**——崩溃恢复（§1.1.4）已经清 `running=False`。只有"不崩溃但 agent 挂死"的场景才需要超时清除——这是 edge case，P2 加。

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `skills/builtin/core/cron/scripts/types.py` | `CronJobState` 加 `running: bool` + `last_run_id: str \| None` |
| `skills/builtin/core/cron/scripts/base.py` | `_execute_job` 加 running 检查 + 预推进 next_run + 清 running；`run_job` 加 running 检查；`_recompute_next_runs` 加崩溃恢复清 running；`_on_cron_job` 加 `metadata["message_id"]=run_id`；`_save_store`/`_load_store` 加 `running`/`last_run_id` 序列化 |
| `tests/skills/builtin/core/cron/test_cron_failure_breaker.py` | 更新 `test_failure_state_never_persisted_to_store`——key set 加 `running`/`last_run_id` |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `submit_user_input` 的 `client_msg_id` 去重 | `server/service/input_queue_service.py:404-414` | cron 投递加 `metadata["message_id"]=run_id` 自动接入——零改动 input queue |
| `_process_inbound` 的 `client_msg_id` 提取 | `server/trigger/channels/core.py:278` | 从 `metadata.get("message_id")` 取——现有逻辑，cron 只需填 metadata |
| `_compute_next_run` | `base.py:182-195` | 预推进 next_run 用——现有函数 |
| `_save_store` / `_load_store` | `base.py:316-359` / `:260-314` | 加两个字段的序列化/反序列化 |
| `_recompute_next_runs` | `base.py:379-397` | 崩溃恢复在这里加清 running |

### 不引入的依赖

- 不新建 `DedupeCache`（复用现有 `client_msg_id` 去重）
- 不新建投递状态表（openclaw 有 `outbound_delivery_queue`——sherry 的 input queue 已够）
- 不加 `running_flag_timeout`（P2 edge case）
- 不改 `submit_user_input`（零改动 input queue 层）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_execute_job_sets_running_flag` | `_execute_job` 开始时 `job.state.running == True` |
| `test_execute_job_clears_running_on_success` | 执行成功后 `running == False` |
| `test_execute_job_clears_running_on_failure` | 执行抛异常后 `running == False` |
| `test_execute_job_skips_if_already_running` | `running == True` 时调 `_execute_job` → 跳过，不调 `on_job` |
| `test_run_job_refuses_if_running` | `run_job` 在 `running == True` 时返回 `{"ok": False, "error": "already running"}` |
| `test_run_job_force_bypasses_running_check` | `run_job(force=True)` 即使 `running == True` 也执行 |
| `test_next_run_advanced_before_execution` | `_execute_job` 后 `next_run_at_ms` 在将来——且是在 `on_job` 调用前 advance 的（mock `on_job` sleep，检查 advance 已存盘） |
| `test_crash_recovery_clears_running` | 模拟 `running == True` + 重启 → `_recompute_next_runs` 清 `running` + 不重新 fire（`next_run` 已 advance） |
| `test_cron_delivery_has_message_id` | `_on_cron_job` 投递的 `InboundMessage.metadata["message_id"]` == `cron:{job_id}:{slot_ms}` |
| `test_cron_delivery_dedup_via_client_msg_id` | 相同 `run_id` 两次 `publish_inbound` → 第二次被 `submit_user_input` 去重（`DEDUPED`） |
| `test_cron_delivery_different_slots_not_deduped` | 不同 `slot_ms` 的 `run_id` 不同 → 不去重（合法的重复投递） |
| `test_running_flag_persisted` | `running == True` 时存盘 → reload 后仍 `True`（崩溃前能看到） |
| `test_last_run_id_persisted` | `last_run_id` 存盘 → reload 后仍能读（投递去重用） |
| `test_state_key_set_includes_running_and_last_run_id` | 更新 `test_failure_state_never_persisted_to_store`——on-disk key set 含 `running`/`lastRunId` |
| `test_manual_double_trigger_deduped` | 快速两次 `run_job` → 第一次执行，第二次返回 `already running` |
| `test_cross_loop_race_deduped` | mock scheduled tick 在跑 + 手动 `run_job` → 第二个被 `running` flag 挡 |

---

## 4. 优先级与分阶段落地

**P0（防最危险的崩溃双发 + 跨 loop 竞态）**：
- §1.1 `CronJobState` 加 `running` + `last_run_id`
- §1.1.2 `_execute_job` 预推进 + running flag
- §1.1.4 崩溃恢复清 running
- §1.2 `metadata["message_id"]=run_id`（投递去重接入现有 `client_msg_id`）

**P1（手动触发防重 + 测试）**：
- §1.1.3 `run_job` 加 running 检查
- 全部测试

---

## 5. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| 预推进 `next_run_at_ms` 后 agent 失败 → job 跳到下个 slot | 这是正确行为——cron 失败后等下个 slot 重试，不立即重试。跟现有行为一致（现有也是执行后 advance） |
| `running` flag stuck（agent 挂死不返回） | 崩溃恢复（§1.1.4）在重启时清 `running`。如果不崩溃但 agent 挂死——P2 加 `running_flag_timeout`（§1.4） |
| `last_run_id` 的 `slot_ms` 跟 `next_run_at_ms` 不一致（时区/漂移） | `slot_ms` = 预推进前的 `next_run_at_ms`——是 job 本来要 fire 的时间戳，不是 wall clock。同一 slot 两次执行（竞态）的 `slot_ms` 相同 → `run_id` 相同 → 去重 |
| `client_msg_id` 全局去重误杀不同 session 的合法投递 | `run_id = cron:{job_id}:{slot_ms}`——`job_id` per-uuid + `slot_ms` per-fire → 全局唯一 per execution slot。不同 job / 不同 slot 的 `run_id` 不同 → 不误杀 |
| 更新 `test_failure_state_never_persisted_to_store` 的 key set | 测试当前 pin `key set == {id,name,enabled,schedule,payload,state,createdAtMs,updatedAtMs,deleteAfterRun}`——加 `running`/`lastRunId` 后 key set 变。测试要更新（`state` 子 dict 内加两个 key） |
| `_save_store` 在执行前调一次（预推进）+ 执行后调一次（清 running）——两次存盘 | 可接受——`cron_jobs.json` 是小文件（几十个 job），JSON 全量写 ~毫秒。openclaw 也每次 mutation 存盘 |

---

## 6. 已知不在本计划范围内

- **`running_flag_timeout`**：不崩溃但 agent 挂死（如 LLM API 卡住无限重试）——`running` 永远 `True`。P2 加超时清除（`running_flag_timeout_seconds` config + sweeper 检查 `running == True` 超时）。
- **投递状态表**：openclaw 有 `outbound_delivery_queue` 跟踪每条投递的 status。sherry 复用 `input_queue` 的 `client_msg_id` 去重——不需要新建投递状态表。
- **per-route 投递去重**：openclaw 的 `buildDirectCronDeliveryIdempotencyKey` 包含 `routeIdentity`（channel/account/thread）。sherry 的 cron 只投递到一个 `to`（`payload.to`），不需要 per-route。
- **cron 执行结果缓存**：不做——cron 每次执行可能产不同结果（时间敏感），缓存无意义。

---

## 7. 数据流

```
执行去重 + 预推进：
  _on_timer → due_jobs → _execute_job(job)
    │
    ├─ 检查 running flag → True? → skip（跨 loop 竞态防住）
    │
    ├─ 生成 run_id = cron:{job_id}:{slot_ms}
    ├─ advance next_run_at_ms + set running=True + set last_run_id + 存盘
    │  ★ 存盘在 agent 执行前——崩溃后 next_run 已在将来，不重复 fire
    │
    ├─ await self.on_job(job) → _on_cron_job
    │   ├─ _assemble_skill_prompt（如果 bound skills）
    │   ├─ create_agent + ainvoke → result
    │   └─ publish_inbound(InboundMessage(metadata={"message_id": run_id}))
    │       │
    │       └→ _process_inbound → submit_user_input
    │           ├─ client_msg_id = run_id（从 metadata 取）
    │           ├─ 第一次 → 不在 ACTIVE 行 → 入队/执行
    │           └─ 第二次（崩溃双发/竞态）→ 命中 ACTIVE → DEDUPED
    │
    └─ finally: set running=False + 存盘

崩溃恢复：
  start() → _recompute_next_runs
    ├─ job.state.running == True? → 清 running（agent 进程已死）
    ├─ next_run_at_ms 已在将来（预推进）→ 不重新 fire
    └─ last_run_id 保留 → 投递去重（如果 pre-crash 投递已到 → DEDUPED）
```

---

## 8. 一句话

借鉴 openclaw 的 `markCronJobActive` + `buildDirectCronDeliveryIdempotencyKey`：① `CronJobState` 加 `running` flag + `last_run_id`——`_execute_job` 执行前检查 running（跨 loop 竞态防住）+ 预推进 `next_run_at_ms` 并存盘（崩溃双发防住——重启后 next_run 已在将来不重复 fire）+ `run_job` 手动触发也检查 running（双触发防住）② 投递去重复用现有 `client_msg_id` 去重——`_on_cron_job` 给 `InboundMessage.metadata["message_id"]` 填 `run_id = cron:{job_id}:{slot_ms}`，`submit_user_input` 自动接入去重（零改动 input queue）。三个双发场景（跨 loop 竞态 / 崩溃双发 / 手动双触发）全堵住。不新建 `DedupeCache`、不新建投递状态表——复用现有基础设施。
