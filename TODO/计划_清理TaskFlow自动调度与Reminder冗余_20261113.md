# 清理计划：TaskFlow 自动调度与 Reminder 的冗余消除

> 背景：TaskFlow 已有完整的自动派发机制（plan 时自动派发初始就绪步骤、resume 时自动派发下游+兄弟分支、子完成时 announce 管线自动 resume）。部分 reminder 和静态指令仍在催 main agent 做"哪些步骤该并行、该 dispatch"的决策——这些在自动调度开启（默认）时是冗余的，会产生噪音并误导 agent 去做系统已经替它完成的事。

## 问题定位

### 冗余项（3 处）

| # | 位置 | 当前行为 | 为什么冗余 |
|---|------|---------|-----------|
| R1 | `workspace/prompt_builder.py:381-385` | board reminder 里当有多个 ready 步骤时附加 `[N steps ready — parallelize via taskflow_dispatch!]` | auto-dispatch 在 plan/resume 时已自动并发派发这些步骤 |
| R2 | `workspace/AGENTS.md` 第 45 行附近 | 静态指令 `dispatch ready steps with taskflow_step(action="dispatch")` | 正常路径下 plan 和 resume 已自动 dispatch，只在 spawn 失败时才有手动 dispatch 的价值 |
| R3 | `agent/middlewares/reminder_catalog/sources.py:186-196` | `_taskflow_cooldown` 当有 undispatched ready 步骤时缩短到 1 轮 | 假设"ready = 需要模型做 dispatch 决策"，但自动调度后 undispatched ready 要么是瞬态（下一波 resume 会派发）要么是 spawn 失败 |

### 非冗余项（保持不动）

- board 的 `done/total` 进度、flow 状态、`next:` 提示、`inspect` 建议 → 监督信息
- todo list、boulder、continuity、plan-mode、Sisyphus 合约 → 跟踪/验证/跨会话，与并行调度无关
- DelegationPressureGate → 推的是 `sessions_spawn` 委托（读密集型分身），与 TaskFlow DAG 并行正交
- AGENTS.md 的 `depends_on=[...]` 指令 → agent 需自己设计 DAG 结构

## 改造目标

把"催你并行执行"的提醒改为"自动派发失败，需手动重试"的精准提示——只在步骤确实处于 `READY` 且有 spawn 失败记录时才出现，正常路径不再有噪音。

## 任务分解

### T1：board reminder 区分"自动待派发"与"派发失败待重试"（对应 R1）

**文件**：`workspace/prompt_builder.py`，`_build_taskflow_block` 及 `has_undispatched_ready_steps` / `_taskflow_ready_steps`

**当前**：
```python
# _build_taskflow_block (prompt_builder.py:381-385)
if len(ready_steps) > 1:
    ready_text += f" [{len(ready_steps)} steps ready — parallelize via taskflow_dispatch!]"
```

**改为**：
1. 区分两类 ready 步骤：
   - `auto_pending`：状态 `READY`，无 `child_session_key`，无失败记录 → 自动调度会处理（下一波 resume / plan 派发），**不催 agent**
   - `failed_spawn`：状态 `READY`，有 `dispatch_attempts > 0` 或 `last_spawn_error` 记录 → **需要 agent 手动 dispatch 重试**
2. `ready_to_dispatch:` 段只列出 `failed_spawn` 步骤
3. 提示文案从 `parallelize via taskflow_dispatch!` 改为 `dispatch failed — retry via taskflow_step(action='dispatch', step_ids=[...])`
4. `auto_pending` 步骤可以在 board 里以轻量形式展示（如 `pending auto-dispatch: <N> steps`），不催 agent

**前置**：需要在 step 记录中增加 `dispatch_attempts` 字段（或复用已有 `child_session_key` 为空 + 步骤历史来判断 spawn 是否失败过）。确认 step schema 当前是否有这个信息。

### T2：AGENTS.md dispatch 指令加上自动调度说明（对应 R2）

**文件**：`workspace/AGENTS.md`（workspace 下的用户级指令，非 repo 根的 AGENTS.md）

**当前**（第 45 行附近）：
```
dispatch ready steps with taskflow_step(action="dispatch")
```

**改为**：
```
ready steps are auto-dispatched at plan time and on each resume — you normally do not need to call dispatch manually.
Only use taskflow_step(action="dispatch") to retry steps whose auto-dispatch spawn failed (shown as "dispatch failed" in the TaskFlow board).
```

同步检查 repo 根 `AGENTS.md` 中 Task Management 段是否有类似表述，一并修正。

### T3：cooldown 缩短逻辑改为只在 spawn 失败时触发（对应 R3）

**文件**：`agent/middlewares/reminder_catalog/sources.py`，`_taskflow_cooldown` 函数

**当前**：
```python
if has_undispatched_ready_steps:
    cooldown = taskflow_reminder_cooldown_when_ready  # 1 turn
```

**改为**：
```python
if has_failed_spawn_ready_steps:  # 新谓词，只看有失败记录的 ready 步骤
    cooldown = taskflow_reminder_cooldown_when_ready  # 1 turn
# 普通 undispatched ready（auto_pending）保持默认 cooldown，不缩短
```

需要新增 `has_failed_spawn_ready_steps` 谓词（或扩展 `has_undispatched_ready_steps` 的返回值为枚举：`none / auto_pending / failed_spawn`）。

### T4：四语言文档同步

**文件**：`agent/middlewares/README.md`（四语言）、`wiki/long-running-tasks/` 下的相关文档

确认文档中是否有"agent 需要手动 dispatch ready 步骤"的表述，同步修正为"自动派发 + 失败时手动重试"。

### T5：测试

**新增/修改**：
- `tests/agent/middlewares/` 下 board reminder 相关测试：验证正常路径（auto_pending）不出现 `parallelize` 文案，spawn 失败路径出现 `dispatch failed — retry` 文案
- cooldown 测试：验证只有 failed_spawn 时 cooldown 缩短，auto_pending 不缩短
- AGENTS.md parity check（`scripts/check_docs_parity.py`）不受影响，但需确认四语言表述一致

## 验收标准

1. 正常路径（plan → 子完成自动 resume → 下游自动派发）：board reminder 中**不出现** `parallelize via taskflow_dispatch!` 文案
2. spawn 失败路径：board reminder 列出失败步骤，文案为 `dispatch failed — retry via taskflow_step(action='dispatch')`
3. cooldown 缩短只在 spawn 失败时触发
4. AGENTS.md 指令明确说明"通常不需要手动 dispatch"
5. 四语言文档同步
6. 现有测试通过 + 新增测试覆盖两种路径

## 风险与回退

- **风险**：`dispatch_attempts` 字段如果 step schema 当前没有，需要迁移。先用 Agent 调查 step 记录结构，确认是否已有足够信息判断"spawn 失败过"。
- **回退**：所有改动受 config flag 保护（可复用 `auto_dispatch_initial_ready` / `auto_dispatch_all_ready_on_resume`）——如果这两个 flag 设为 `False`（关闭自动调度），则恢复旧的"催 agent dispatch"行为。board reminder 的文案分支应检查这两个 flag 的值。
- **渐进**：可先做 T2（改 AGENTS.md 文案，零代码风险）→ T1（board 文案区分）→ T3（cooldown 逻辑）→ T4（文档）→ T5（测试）。

## 第二部分：BLOCKED 步骤的信号断裂修复

> 背景：调查自动调度时发现 BLOCKED 步骤存在严重的信号断裂——最自动的渠道（子完成通知）把 blocked 报成"成功"或"失败"，board reminder 不显示 blocked 状态，auto-resume 的返回文本被吞掉。agent 在被动场景下大概率不知道 step 被 blocked 了。

### 问题定位

| # | 位置 | 当前行为 | 问题 |
|---|------|---------|------|
| B1 | `agent/tools/subagent/announce/delivery.py:641-693` `_deliver_completion_message` | BUDGET_EXHAUSTED 步骤显示 `"Status: completed successfully"`；judge BLOCK 一个成功 run 也显示 "completed successfully" | 完成通知报喜不报忧，agent 误以为步骤成功 |
| B2 | `agent/tools/subagent/announce/delivery.py:461-535` `_try_auto_resume_taskflow` | `taskflow_resume` 返回的 `outcome: blocked` / `judge: BLOCKED {reason}` 文本只被 log，不送达 agent | 自动 resume 的 blocked 信号被吞掉 |
| B3 | `agent/tools/subagent/announce/delivery.py:204-216` `_resolve_builder_status` | BUDGET_EXHAUSTED → `STATUS_FAILED`，输出 `[subagent:{name} failed]` | 第三路径标记说"失败"，和完成通知的"成功"矛盾，且都不是"blocked" |
| B4 | `workspace/prompt_builder.py:388` `_build_taskflow_block` 的 `next:` 提示 | 只打印 step id 和 task 文本，不打印状态 | blocked 步骤在 `next:` 里和 ready/dispatched 无法区分 |
| B5 | `workspace/prompt_builder.py:305-343` board 不含 blocked 计数 | 只有 `done/total`，无 blocked 计数、无 blocked 步骤列表 | board reminder 完全不暴露 blocked 状态 |
| B6 | `agent/middlewares/reminder_catalog/sources.py:186-196` cooldown 逻辑 | 只在有 undispatched **ready** 步骤时缩短 cooldown，blocked 步骤不触发 | 步骤被 blocked 时反而降低提醒频率，与"需要 agent 决策"矛盾 |
| B7 | `agent/middlewares/todo_continuation/core.py:87-101` `_CONTINUATION_PROMPT` | 文案 "Continue working on the next pending task" | 当唯一剩余工作是 blocked 步骤时，这个指令是误导的——agent 无法直接推进 blocked 步骤 |
| B8 | 级联 blocked dependents | `unlock_dependents` 不解锁 blocked 依赖的下游（设计如此），但下游 blocked steps 无单独通知 | 级联 blocked 的 dependents 完全静默，只能靠 continuation 指令或主动 inspect 发现 |

### 改造目标

让 agent 在**每个渠道**都能准确知道步骤被 blocked 了、原因是什么、该怎么处理。核心原则：auto-resume 路径不再吞掉 blocked 信号，完成通知不再误报"成功"，board 明确显示 blocked 状态。

### 任务分解

#### B-T1：完成通知对 blocked 结果正确报状态（对应 B1+B3）

**文件**：`agent/tools/subagent/announce/delivery.py`（`_deliver_completion_message` 和 `_resolve_builder_status`），以及 `agent/tools/subagent/announce/completion_message.py`

**改动**：
1. `_deliver_completion_message` 的 `status_text` 增加 `budget_exhausted` 分支：
   - `"Status: blocked (budget exhausted — partial result, completion not proven)"`
2. judge BLOCK on an otherwise-OK run：需要 announce 管线能感知 resume 后的 step 状态。方案：`_try_auto_resume_taskflow` 返回值从 bool 扩展为 `(success, step_outcome, reason)`，让 `_deliver_completion_message` 据此设置 `status_text`：
   - `step_outcome == "blocked"` → `"Status: blocked ({reason})"`
3. `_resolve_builder_status` 增加 `STATUS_BLOCKED`，BUDGET_EXHAUSTED / judge BLOCK 映射到它，输出 `[subagent:{name} blocked]`——不再报"failed"也不再报"completed"
4. 三条路径（dual-path InboundMessage / 第三路径注入标记 / 完成消息）统一用 "blocked" 措辞，消除矛盾

**风险**：改 `_try_auto_resume_taskflow` 返回值会影响所有调用者，需确认调用点数量。

#### B-T2：auto-resume 不再吞掉 blocked 返回文本（对应 B2）

**文件**：`agent/tools/subagent/announce/delivery.py:461-535`

**改动**：
1. `taskflow_resume` 的返回文本（包含 `outcome: blocked` / `judge: BLOCKED {reason}`）目前只 log 第一行。改为：当返回文本包含 blocked 信号时，将其摘要附加到完成通知或注入消息中，让 agent 能看到原因
2. 与 B-T1 配合：如果 B-T1 已经通过返回值结构传递了 blocked 状态，这里只需确保 reason 文本不丢失

#### B-T3：board reminder 显示 blocked 步骤和计数（对应 B4+B5）

**文件**：`workspace/prompt_builder.py`（`_build_taskflow_block`、新增 `_taskflow_blocked_steps` 谓词）

**改动**：
1. board 每行 flow 的状态行增加 blocked 计数：
   ```
   - [status] flow_id: "desc" | D/N steps done | blocked: M
   ```
2. 当有 blocked 步骤时，列出 blocked 步骤（类似 ready_to_dispatch 的格式）：
   ```
   blocked: step_id (task) — block_reason
   ```
3. `next:` 提示带上状态：`next: step_id "task" (blocked)` 而非省略状态
4. 新增 `_taskflow_blocked_steps(steps)` 谓词：返回 `status == "blocked"` 的步骤

#### B-T4：cooldown 在有 blocked 步骤时也缩短（对应 B6）

**文件**：`agent/middlewares/reminder_catalog/sources.py`（`_taskflow_cooldown`）

**改动**：
1. 新增 `has_blocked_steps` 谓词（或扩展 `has_undispatched_ready_steps` 的判断范围）
2. 当有 blocked 步骤时，cooldown 同样缩短到 `taskflow_reminder_cooldown_when_ready`（1 轮）——blocked 步骤需要 agent 决策，比 ready 更紧急
3. 与 R3 的改动合并：cooldown 缩短条件 = `has_failed_spawn_ready_steps || has_blocked_steps`

#### B-T5：continuation 指令在 blocked-only 场景用专属文案（对应 B7）

**文件**：`agent/middleflows/todo_continuation/core.py`（`_CONTINUATION_PROMPT` 及注入逻辑）

**改动**：
1. 当 flow 的未完成步骤**全部**是 blocked（无 ready/dispatched）时，用专属 prompt 替代 `_CONTINUATION_PROMPT`：
   ```
   All remaining steps are blocked — you cannot advance them directly.
   Review the block reasons above and decide: replan around the blocker, cancel the blocked branch,
   or provide the missing result via taskflow_step(action='resume') if you can resolve the dependency.
   ```
2. 当有混合（部分 blocked + 部分 ready/dispatched）时，保持原 `_CONTINUATION_PROMPT`，但 `_build_flow_block` 已经会显示每步状态

#### B-T6：级联 blocked dependents 在 board 中可见（对应 B8）

**文件**：`workspace/prompt_builder.py`（`_build_taskflow_block`）

**改动**：
1. blocked 步骤列表中，标注哪些是被上游级联 blocked 的（block_reason 包含上游 step id）：
   ```
   blocked: step_id (task) — waiting on upstream: dep_step_id
   ```
2. 不需要为每个级联 dependents 单独发通知——board 列表已经够 agent 看到全局

#### B-T7：四语言文档同步

**文件**：`agent/middlewares/README.md`（四语言）、`wiki/long-running-tasks/`

确认文档中关于 blocked 步骤的处理说明，同步更新为新的通知行为。

#### B-T8：测试

**新增/修改**：
- announce 管线测试：BUDGET_EXHAUSTED → 完成通知显示 "blocked"；judge BLOCK → 完成通知显示 "blocked"
- board reminder 测试：blocked 步骤出现在 board 中，含 block_reason；`next:` 带状态
- cooldown 测试：blocked 步骤触发 cooldown 缩短
- continuation 测试：blocked-only 场景使用专属 prompt
- 三条通知路径一致性测试（不再矛盾）

### 验收标准

1. BUDGET_EXHAUSTED 步骤的完成通知显示 `blocked`（不再是 "completed successfully"）
2. judge BLOCK 步骤的完成通知显示 `blocked` + reason
3. 三条通知路径（InboundMessage / 第三路径标记 / 完成消息）对 blocked 结果用统一的 "blocked" 措辞
4. board reminder 显示 blocked 步骤列表和 block_reason，`next:` 提示带状态
5. 有 blocked 步骤时 cooldown 缩短到 1 轮
6. blocked-only 场景的 continuation 指令不再说 "continue working"，而是引导 agent 做 replan/cancel/resolve 决策
7. 级联 blocked dependents 在 board 中可见（标注上游依赖）
8. 四语言文档同步
9. 现有测试通过 + 新增测试覆盖各路径

### 风险与回退

- **B-T1 改 `_try_auto_resume_taskflow` 返回值**：需要审计所有调用者（`run_announce_dispatch` 等），确认返回值变更不破坏 fail-open 逻辑。可先用兼容方式：返回 `(bool, dict|None)`，`dict` 携带 `{step_outcome, reason}`，旧代码只读 bool 不受影响。
- **B-T5 专属 prompt**：需确保 continuation gate 的 stagnation 检测和 latch 逻辑兼容新文案分支。
- **渐进顺序**：B-T3（board 显示 blocked，纯渲染层，零风险）→ B-T4（cooldown）→ B-T1+B-T2（完成通知修正，需联动）→ B-T5（continuation 文案）→ B-T6（级联标注）→ B-T7（文档）→ B-T8（测试）。

## 整体执行顺序

两部分可以并行推进，但建议按以下顺序串行以降低风险：

1. **T2**（AGENTS.md 文案，零代码风险）
2. **B-T3**（board 显示 blocked，纯渲染层）
3. **T1**（board 区分 auto_pending / failed_spawn）
4. **B-T4 + T3**（cooldown 逻辑合并改动）
5. **B-T1 + B-T2**（完成通知 + auto-resume 信号修复，联动）
6. **B-T5**（continuation 文案）
7. **B-T6**（级联 blocked 标注）
8. **T4 + B-T7**（四语言文档同步合并）
9. **T5 + B-T8**（测试合并）

## 前置调查（动手前先确认）

- [ ] step 记录 schema 是否有 `dispatch_attempts` / `last_spawn_error` / `child_session_key` 为空 + 历史记录，能判断"spawn 失败过"（R1/T1 依赖）
- [ ] `auto_dispatch_initial_ready=False` 时的回退路径：board 是否应该恢复催 agent dispatch 的旧行为（T1 依赖）
- [ ] repo 根 `AGENTS.md` 的 Task Management 段与 workspace 下的 `AGENTS.md` 是否是同一份或有重叠表述（T2 依赖）
- [ ] `_try_auto_resume_taskflow` 的所有调用者，确认返回值变更的影响范围（B-T1 依赖）
- [ ] step 记录中 block_reason 字段的结构和来源——judge BLOCK vs BUDGET_EXHAUSTED vs 级联 blocked 的 reason 格式是否统一（B-T3/B-T6 依赖）
- [ ] announce 管线中 judge BLOCK 的结果如何传递到 step 状态——确认 `_try_auto_resume_taskflow` 能感知 judge BLOCK（不仅仅是 BUDGET_EXHAUSTED）（B-T1 依赖）
