# 循环检测增强：乒乓检测 + 参数搅动 + 恢复模式

> 方案 3（乒乓检测）、方案 4（参数搅动检测）、方案 6（工具循环恢复模式）

## 1. 背景与目标

### 1.1 sherry 现状

#### 工具循环检测现状

`agent/middlewares/tool_guardrails.py`（407行）实现了 3 种病理检测：

| 病理类型       | 检测内容                 | WARN | BLOCK/HALT |
| -------------- | ------------------------ | ---- | ---------- |
| 精确失败重复   | 同工具+同参数反复失败    | 2次  | 5次→BLOCK  |
| 同工具失败累积 | 同工具不同参数反复失败   | 3次  | 8次→HALT   |
| 幂等无进展     | 只读工具返回完全相同结果 | 2次  | 5次→BLOCK  |

**缺失的检测**：

- 两个工具交替重复（A→B→A→B→A→B），单工具视角不触发
- 同一工具多组不同参数，全部无进展（参数搅动）
- BLOCK 后无恢复模式，模型只能换工具或直接 HALT

#### Cron 系统现状

sherry 有 5 套周期调度系统：

| 系统                  | 文件                                                    | 调度类型                               | 状态             |
| --------------------- | ------------------------------------------------------- | -------------------------------------- | ---------------- |
| **CronService**       | `skills/builtin/core/cron/scripts/base.py` (592行)      | at/every/cron 三模式，持久化，REST API | 活跃             |
| **HeartbeatService**  | `skills/builtin/core/heartbeat/scripts/base.py` (219行) | 30分钟固定间隔轮询                     | 活跃             |
| **Subagent Sweeper**  | `agent/tools/subagent/registry/sweeper.py` (167行)      | 60秒固定间隔看门狗                     | **已定义未接线** |
| **Curator**           | `context_engine/curator/__init__.py`                    | 7天间隔条件触发                        | 活跃             |
| **TimerCallRegister** | `runtime/timer_call_register.py` (220行)                | 1-60分钟重复倒计时                     | 活跃             |

**缺失的 Cron 防御**：

- Cron 任务连续失败无断路（openclaw 有 5次→degraded / 10次→auto-disable）
- 周期服务失败无退避（openclaw 有指数退避重试）
- 进程崩溃无循环检测（openclaw 有 5分钟3次不干净启动→跳闸）

### 1.2 目标

```
工具循环检测增强:
  ③ 乒乓检测（Ping-Pong）     → 两个工具交替重复
  ④ 参数搅动检测（Arg Churn） → 多组参数全部无进展
  ⑥ 恢复模式（Recovery）      → BLOCK 后给模型再试机会

Cron 断路器:
  A. 任务连续失败断路器       → 5次degraded / 10次auto-disable
  B. 周期服务退避重试         → 指数退避拉长间隔
  C. 崩溃循环断路器           → 5分钟3次不干净启动→跳闸
```

### 1.3 openclaw 参考来源

| 方案 | openclaw 文件                                  | 核心机制                         |
| ---- | ---------------------------------------------- | -------------------------------- |
| 3    | `tool-loop-detection.ts` 的 `ping_pong` 检测器 | 两工具交替重复，双方无进展       |
| 4    | `tool-loop-argument-churn.ts`                  | 多组参数变体，共享稳定无进展结果 |
| 6    | `agent-loop.ts` 的 `terminateRun` 两段式恢复   | 首次critical→恢复模式，二次→终止 |
| A    | `cron-stream-job-owner.ts` + `auto-disable.ts` | 5次/10次两级断路 + 自动禁用      |
| B    | `cron-exit-watchers.ts`                        | 指数退避重试调度                 |
| C    | `gateway-boot-lifecycle.ts`                    | 5分钟3次不干净启动→跳闸          |

---

## 2. 方案 3：乒乓检测（Ping-Pong Detection）

### 2.1 病理描述

```
A→B→A→B→A→B→A→B...
```

两个不同工具交替重复调用，双方都无进展（都失败或都返回相同结果）。

**为什么现有检测抓不到**：

- 精确失败重复：只看 `tool_name:args_hash`，A和B的 key 不同
- 同工具失败累积：按 `tool_name` 分组，A 单独计数不超阈值，B 同理
- 幂等无进展：按 `tool_name:result_hash` 分组，A 和 B 的 result_hash 不同

### 2.2 修改文件：`agent/middlewares/tool_guardrails.py`

#### 2.2.1 配置新增

在 `ToolCallGuardrailConfig`（L40-L49）中新增：

```python
@dataclass
class ToolCallGuardrailConfig:
    # ... 现有字段不变 ...

    # 方案 3：乒乓检测（两工具交替重复）
    ping_pong_warn_after: int = 4       # 交替 4 轮（8次调用）→ WARN
    ping_pong_block_after: int = 6      # 交替 6 轮（12次调用）→ BLOCK
```

#### 2.2.2 状态新增

在 `_TurnGuardrailState`（L60-L67）中新增：

```python
@dataclass
class _TurnGuardrailState:
    # ... 现有字段不变 ...
    ping_pong_counts: dict[str, int] = field(default_factory=dict)
    # key = "toolA|toolB"（排序后拼接） -> 交替次数
```

#### 2.2.3 检测逻辑

在 `_evaluate` 方法（L119-L200）中，**在现有 3 种病理检测之后、副作用应用之前**，新增第 4 种检测：

```python
def _evaluate(
    self, gs, tool_name, args_hash, result_hash, is_error, is_idempotent,
) -> GuardrailAction:
    # --- 现有步骤 1-3 不变 ---
    # 1. 检查 halt_decision → HALT (L128-L132)
    # 2. 初始化 action = ALLOW (L134)
    # 3A. 错误路径：exact_failure + same_tool_failure (L136-L170)
    # 3B. 成功路径：no_progress (L171-L193)

    # --- 新增步骤 7：乒乓检测 ---
    if action == GuardrailAction.ALLOW or action == GuardrailAction.WARN:
        action = self._evaluate_ping_pong(
            gs, tool_name, is_error, result_hash, action
        )

    # --- 现有步骤 4：应用副作用 (L195-L198) ---
    # ...
    return action


def _evaluate_ping_pong(
    self, gs, tool_name, is_error, result_hash, current_action,
) -> GuardrailAction:
    """检测两个工具交替重复且双方无进展。"""
    records = gs.records
    if len(records) < 2:
        return current_action

    prev = records[-1]   # 上一次调用（当前调用还没追加到 records）
    # 注意：_evaluate 在 _wrap_tool_call_impl 中被调用时，
    # 当前 _ToolCallRecord 已在 L263-270 追加到 gs.records
    # 所以 records[-1] 是当前调用，records[-2] 是上一次调用

    if len(records) < 3:
        return current_action

    curr = records[-1]
    prev = records[-2]

    # 必须是不同工具
    if curr.name == prev.name:
        return current_action

    # 双方都必须无进展
    curr_no_progress = curr.is_error or (
        curr.result_hash is not None and self._has_matching_no_progress(gs, curr)
    )
    prev_no_progress = prev.is_error or (
        prev.result_hash is not None and self._has_matching_no_progress(gs, prev)
    )
    if not (curr_no_progress and prev_no_progress):
        return current_action

    # 构造交替 key（排序确保 A|B == B|A）
    pair_key = "|".join(sorted([curr.name, prev.name]))
    gs.ping_pong_counts[pair_key] = gs.ping_pong_counts.get(pair_key, 0) + 1
    pp_count = gs.ping_pong_counts[pair_key]

    if self.config.hard_stop_enabled and pp_count >= self.config.ping_pong_block_after:
        return GuardrailAction.HALT
    elif pp_count >= self.config.ping_pong_block_after:
        return GuardrailAction.BLOCK
    elif self.config.warnings_enabled and pp_count >= self.config.ping_pong_warn_after:
        if current_action == GuardrailAction.ALLOW:
            return GuardrailAction.WARN
    return current_action


def _has_matching_no_progress(self, gs, record) -> bool:
    """检查该记录是否在 no_progress_counts 中有计数。"""
    if record.result_hash is None:
        return False
    key = f"{record.name}:{record.result_hash}"
    return gs.no_progress_counts.get(key, 0) > 0
```

#### 2.2.4 警告/阻断消息

在消息生成方法中新增乒乓描述：

```python
@staticmethod
def _warning_message(tool_name, pathology, count, limit) -> str:
    if pathology == "ping_pong":
        return (
            f"[警告] 检测到乒乓循环：{tool_name} 与另一工具交替重复调用 "
            f"且双方均无进展 ({count}/{limit})。请尝试完全不同的方法。"
        )
    # ... 现有消息不变 ...

@staticmethod
def _block_message(tool_name, pathology, count, limit) -> str:
    if pathology == "ping_pong":
        return (
            f"[阻断] 乒乓循环达到上限：{tool_name} 与另一工具交替重复 "
            f"({count}/{limit})。已触发断路器，该工具已被阻断。"
        )
    # ... 现有消息不变 ...
```

#### 2.2.5 病理描述选择

在 `_wrap_tool_call_impl` 中选择 pathology 描述时，新增乒乓判断：

```python
# 在现有 pathology 选择逻辑中新增
if action == GuardrailAction.WARN or action == GuardrailAction.BLOCK:
    if pp_count > 0 and pp_count >= self.config.ping_pong_warn_after:
        pathology = "ping_pong"
        count, limit = pp_count, self.config.ping_pong_block_after
```

---

## 3. 方案 4：参数搅动检测（Argument Churn Detection）

### 3.1 病理描述

```
tool(arg1) → 无进展
tool(arg2) → 无进展
tool(arg3) → 无进展
tool(arg1) → 无进展   (循环回到第一组参数)
tool(arg2) → 无进展
...
```

同一工具用 3+ 组不同参数调用，每组 3+ 次，所有组产生相同的稳定无进展结果。

**与现有检测的区别**：

- 精确失败重复：只看一组参数的重复
- 同工具失败累积：不限参数是否相同，只看总数
- 参数搅动：关注"多组不同参数但结果都一样"的模式

### 3.2 修改文件：`agent/middlewares/tool_guardrails.py`

#### 3.2.1 配置新增

```python
@dataclass
class ToolCallGuardrailConfig:
    # ... 现有字段不变 ...

    # 方案 4：参数搅动检测
    arg_churn_min_variants: int = 3          # 至少 3 组不同参数
    arg_churn_min_calls_per_variant: int = 3  # 每组至少 3 次调用
    arg_churn_warn_after: int = 3            # 3 变体 → WARN
    arg_churn_block_after: int = 5            # 5 变体 → BLOCK
```

#### 3.2.2 状态新增

```python
@dataclass
class _TurnGuardrailState:
    # ... 现有字段不变 ...
    arg_churn_variants: dict[str, dict[str, int]] = field(default_factory=dict)
    # key = tool_name -> {args_hash: call_count}
    arg_churn_stable_result: dict[str, str | None] = field(default_factory=dict)
    # key = tool_name -> 共享的稳定无进展 result_hash（None 表示全是错误）
```

#### 3.2.3 检测逻辑

在 `_evaluate` 方法中，**在乒乓检测之后**，新增第 5 种检测：

```python
def _evaluate(
    self, gs, tool_name, args_hash, result_hash, is_error, is_idempotent,
) -> GuardrailAction:
    # --- 现有步骤 1-3 + 乒乓检测不变 ---

    # --- 新增步骤 8：参数搅动检测 ---
    if action == GuardrailAction.ALLOW or action == GuardrailAction.WARN:
        action = self._evaluate_argument_churn(
            gs, tool_name, args_hash, result_hash, is_error, action
        )

    # --- 现有步骤 4：应用副作用 ---
    # ...
    return action


def _evaluate_argument_churn(
    self, gs, tool_name, args_hash, result_hash, is_error, current_action,
) -> GuardrailAction:
    """检测同一工具多组不同参数全部无进展。"""
    # 更新变体跟踪
    if tool_name not in gs.arg_churn_variants:
        gs.arg_churn_variants[tool_name] = {}
        gs.arg_churn_stable_result[tool_name] = None

    variants = gs.arg_churn_variants[tool_name]
    variants[args_hash] = variants.get(args_hash, 0) + 1

    # 判断本次是否无进展
    is_no_progress = is_error or (
        result_hash is not None
        and gs.no_progress_counts.get(f"{tool_name}:{result_hash}", 0) > 0
    )
    if not is_no_progress:
        # 有进展 → 不是搅动，重置
        gs.arg_churn_variants[tool_name] = {}
        gs.arg_churn_stable_result[tool_name] = None
        return current_action

    # 更新共享稳定结果
    stable = gs.arg_churn_stable_result[tool_name]
    if stable is None:
        gs.arg_churn_stable_result[tool_name] = result_hash if not is_error else "error"
        stable = gs.arg_churn_stable_result[tool_name]
    elif not is_error and result_hash is not None and result_hash != stable:
        # 不同结果 → 不是搅动
        gs.arg_churn_variants[tool_name] = {}
        gs.arg_churn_stable_result[tool_name] = None
        return current_action

    # 统计满足 min_calls_per_variant 的变体数
    qualified_variants = sum(
        1 for count in variants.values()
        if count >= self.config.arg_churn_min_calls_per_variant
    )

    if qualified_variants >= self.config.arg_churn_block_after:
        if self.config.hard_stop_enabled:
            return GuardrailAction.HALT
        return GuardrailAction.BLOCK

    if (self.config.warnings_enabled
            and qualified_variants >= self.config.arg_churn_warn_after
            and current_action == GuardrailAction.ALLOW):
        return GuardrailAction.WARN

    return current_action
```

#### 3.2.4 警告/阻断消息

```python
# 在 _warning_message 和 _block_message 中新增
if pathology == "argument_churn":
    return (
        f"[警告] 参数搅动检测：{tool_name} 用 {count} 组不同参数反复调用"
        f"且全部无进展 ({count}/{limit})。请尝试不同的工具或方法。"
    )
```

---

## 4. 方案 6：工具循环恢复模式

### 4.1 设计理念

当前 `ToolGuardrails` 的升级链：`ALLOW → WARN → BLOCK → HALT`

**问题**：BLOCK 将工具加入 `blocked_tools`，后续该工具直接短路返回 BLOCK。模型只能换工具。但如果模型只是换了参数想再试一次同一个工具，也会被 BLOCK。

**改进**：BLOCK 后进入"恢复模式"：

1. 清空 `blocked_tools`（给模型重新尝试的机会）
2. 如果恢复期内再次触发 critical（BLOCK/HALT）→ 立即 HALT 终止 turn
3. 恢复模式是"宽进严出"：第一次犯错给第二次机会，第二次犯错直接终止

### 4.2 修改文件：`agent/middlewares/tool_guardrails.py`

#### 4.2.1 配置新增

```python
@dataclass
class ToolCallGuardrailConfig:
    # ... 现有字段不变 ...

    # 方案 6：恢复模式
    recovery_mode_enabled: bool = True       # 启用恢复模式
    recovery_max_violations: int = 1         # 恢复期内允许 1 次 critical，第 2 次 → HALT
```

#### 4.2.2 状态新增

```python
@dataclass
class _TurnGuardrailState:
    # ... 现有字段不变 ...
    recovery_mode: bool = False              # 是否处于恢复模式
    recovery_violation_count: int = 0       # 恢复期内再次触发 critical 的次数
```

#### 4.2.3 恢复模式流程

修改 `_evaluate` 的副作用应用部分（L195-L198）：

```python
def _evaluate(
    self, gs, tool_name, args_hash, result_hash, is_error, is_idempotent,
) -> GuardrailAction:
    # --- 步骤 1-3 + 乒乓 + 参数搅动 不变 ---

    # --- 步骤 4：应用副作用（修改后） ---
    if action == GuardrailAction.HALT:
        gs.halt_decision = action

    elif action == GuardrailAction.BLOCK:
        if self.config.recovery_mode_enabled and not gs.recovery_mode:
            # 首次 BLOCK → 进入恢复模式
            gs.recovery_mode = True
            gs.blocked_tools.add(tool_name)
            # 注意：不清空 blocked_tools，而是在 precheck 中特殊处理
            # 恢复模式消息会在 _block_message 中体现
        elif self.config.recovery_mode_enabled and gs.recovery_mode:
            # 恢复模式内再次 BLOCK → 递增违规计数
            gs.recovery_violation_count += 1
            if gs.recovery_violation_count > self.config.recovery_max_violations:
                # 超过允许的违规次数 → 升级为 HALT
                action = GuardrailAction.HALT
                gs.halt_decision = action
            else:
                gs.blocked_tools.add(tool_name)
        else:
            # 恢复模式未启用 → 直接 BLOCK（现有行为）
            gs.blocked_tools.add(tool_name)

    return action
```

#### 4.2.4 修改前置检查

修改 `_wrap_tool_call_precheck`（L352-L381）：

```python
def _wrap_tool_call_precheck(self, request) -> ToolMessage | None:
    session_id = self._get_session_id(request)
    gs = self._get_state(session_id)
    tool_name = request.tool_call["name"]

    # 已 HALT → 直接返回 HALT 消息
    if gs.halt_decision is not None:
        return ToolMessage(
            content=self._halt_message(tool_name, "halted"),
            tool_call_id=request.tool_call["id"],
            name=tool_name, status="error",
        )

    # 恢复模式特殊处理
    if gs.recovery_mode and tool_name in gs.blocked_tools:
        # 恢复模式：解除 block，允许模型重试
        gs.blocked_tools.discard(tool_name)
        # 但记录这是恢复模式下的重试
        # 如果重试仍然触发 BLOCK，会在 _evaluate 中递增 violation_count
        return None  # 放行

    # 非恢复模式：已 blocked 的工具直接返回 BLOCK 消息
    if tool_name in gs.blocked_tools:
        return ToolMessage(
            content=self._block_message(tool_name, "blocked", 0, 0),
            tool_call_id=request.tool_call["id"],
            name=tool_name, status="error",
        )

    return None
```

#### 4.2.5 恢复模式消息

```python
@staticmethod
def _block_message(tool_name, pathology, count, limit) -> str:
    # 恢复模式特殊消息
    if pathology == "blocked" and count == 0:
        return (
            f"[阻断] 工具 {tool_name} 已被阻断。"
            f"已进入恢复模式，请尝试完全不同的方法。"
            f"若再次触发循环，将终止本轮对话。"
        )
    if pathology == "ping_pong":
        # ... 乒乓消息 ...
    if pathology == "argument_churn":
        # ... 参数搅动消息 ...
    # ... 现有消息 ...

@staticmethod
def _halt_message(tool_name, pathology) -> str:
    if pathology == "halted" and gs_recovery:  # 恢复模式内的 HALT
        return (
            f"[终止] 恢复模式内再次触发工具循环。"
            f"工具 {tool_name} 的循环行为未改善，终止本轮对话。"
        )
    # ... 现有消息 ...
```

#### 4.2.6 状态重置

`_before_agent_impl`（L226-L231）已重置整个 `_TurnGuardrailState()`，新增字段自动重置：

```python
@staticmethod
def _before_agent_impl(state: AgentState) -> None:
    session_id = state.get("session_id", "")
    if not session_id.strip():
        raise RuntimeError("ToolGuardrails: session_id is required")
    state_register_mem.set_state(
        session_id, _GUARDRAIL_STATE_KEY, _TurnGuardrailState()
        # _TurnGuardrailState() 初始化所有字段为默认值
        # 包括 recovery_mode=False, recovery_violation_count=0
        # 包括 ping_pong_counts={}, arg_churn_variants={}
    )
```

### 4.3 最终 _evaluate 完整执行顺序

```
1. 检查 halt_decision → HALT (短路)

2. 检查 recovery_mode + blocked_tools:
   ├─ 恢复模式 + 工具 blocked → 解除 block，放行 (短路)
   └─ 非恢复模式 + 工具 blocked → BLOCK (短路)

3. 检查 blocked_tools → BLOCK (短路)

4. 评估 exact_failure (精确失败重复)
   └─ 同工具+同参数失败计数

5. 评估 same_tool_failure (同工具失败累积)
   └─ 同工具失败总数（不限参数）

6. 评估 no_progress (幂等无进展)
   └─ 幂等工具返回相同结果

7. 评估 ping_pong (乒乓检测) [新增]
   └─ 两工具交替重复 + 双方无进展

8. 评估 argument_churn (参数搅动) [新增]
   └─ 多组不同参数 + 全部无进展

9. 应用副作用:
   ├─ HALT → 设置 halt_decision
   └─ BLOCK → 检查 recovery_mode:
        ├─ 首次 → 进入 recovery_mode=True
        │         blocked_tools.add(tool_name)
        └─ 恢复中 → recovery_violation_count += 1
             ├─ count <= max_violations → 继续 BLOCK
             └─ count > max_violations → 升级为 HALT
```

### 4.4 恢复模式场景走查

```
场景：模型反复调用同一工具

1. tool_X(arg1) 失败 → exact_count=1 → ALLOW
2. tool_X(arg1) 失败 → exact_count=2 → WARN
3. tool_X(arg1) 失败 → exact_count=3 → ALLOW (WARN已发)
4. tool_X(arg1) 失败 → exact_count=4 → ALLOW
5. tool_X(arg1) 失败 → exact_count=5 → BLOCK
   → recovery_mode=True, blocked_tools={tool_X}
   → 返回 BLOCK 消息 + "已进入恢复模式"

6. 模型调用 tool_X(arg2):
   precheck: recovery_mode=True, tool_X in blocked_tools
   → 解除 block, 放行
   → tool_X(arg2) 执行 → 成功
   → record_success → 重置（恢复模式不重置，但 no_progress 不触发）
   → 正常返回

   或者:
   → tool_X(arg2) 执行 → 失败
   → exact_count=1 (新参数) → ALLOW
   → same_tool_count=6 → WARN
   → 参数搅动变体=2 → 未达阈值
   → ALLOW + WARN

7. 模型再次调用 tool_X(arg1) 失败:
   → exact_count=6 → BLOCK
   → recovery_mode=True (已在恢复模式)
   → recovery_violation_count=1
   → 1 <= max_violations(1) → 继续 BLOCK
   → 返回 BLOCK 消息 + 警告

8. 模型第三次调用 tool_X(arg1) 失败:
   → exact_count=7 → BLOCK
   → recovery_violation_count=2
   → 2 > max_violations(1) → 升级为 HALT
   → 设置 halt_decision
   → 返回 HALT 消息 + "恢复模式内再次触发循环，终止"
```

---
