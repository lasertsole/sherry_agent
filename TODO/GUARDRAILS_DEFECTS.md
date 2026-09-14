# 为纯读工具标记 idempotent=True

## 背景

`ToolGuardrails` 中间件通过 `is_idempotent` 元数据决定是否计算 `result_hash`，用于检测"反复调用同一读工具得到相同结果"的死循环。以下 6 个纯读工具当前未标记 `idempotent`，导致 guardrails 无法检测它们的 no-progress 循环。

## 待标记工具

| #   | 工具                | 文件                                              | 当前 metadata            | 修改后                                       |
| --- | ------------------- | ------------------------------------------------- | ------------------------ | -------------------------------------------- |
| 1   | `todoread`          | `agent/tools/todolist/tools/todoread.py`          | `{"scope": "main_only"}` | `{"scope": "main_only", "idempotent": True}` |
| 2   | `taskflow_summary`  | `agent/tools/taskflow/tools/taskflow_summary.py`  | `{"scope": "main_only"}` | `{"scope": "main_only", "idempotent": True}` |
| 3   | `taskflow_progress` | `agent/tools/taskflow/tools/taskflow_progress.py` | `{"scope": "main_only"}` | `{"scope": "main_only", "idempotent": True}` |
| 4   | `taskflow_list`     | `agent/tools/taskflow/tools/taskflow_list.py`     | `{"scope": "main_only"}` | `{"scope": "main_only", "idempotent": True}` |
| 5   | `agents_list`       | `agent/tools/subagent/tools/runtime_tools.py:226` | 无                       | `{"idempotent": True}`                       |
| 6   | `subagents_list`    | `agent/tools/subagent/tools/runtime_tools.py:241` | 无                       | `{"idempotent": True}`                       |

## 修改方式

### 1-4: taskflow/todolist 工具

这 4 个工具在 `build_*()` 函数中被批量设置 `metadata={"scope": "main_only"}`，需在批量设置后追加：

- `agent/tools/taskflow/tools/__init__.py:55-57` — 在 `for` 循环后，对 `taskflow_summary`、`taskflow_progress`、`taskflow_list` 追加 `idempotent`
- `agent/tools/todolist/tools/__init__.py:57-59` — 同理对 `todoread` 追加

```python
# taskflow/tools/__init__.py — for 循环后追加
_READ_ONLY_TASKFLOW = {"taskflow_summary", "taskflow_progress", "taskflow_list"}
for t in _TASKFLOW_TOOLS:
    if t.name in _READ_ONLY_TASKFLOW:
        t.metadata = {**t.metadata, "idempotent": True}
```

### 5-6: subagent runtime tools

`agent/tools/subagent/tools/runtime_tools.py:276-277` 已有 `metadata` 设置行，在其后追加：

```python
agents_list_runtime_tool.metadata = {"idempotent": True}
subagents_list_runtime_tool.metadata = {"idempotent": True}
```

## 测试

- 运行 `uv run pytest tests/agent/middlewares/test_tool_guardrails.py -q` 确认 guardrails 测试通过
- 运行 `uv run pytest tests/agent/tools/taskflow/ tests/agent/tools/todolist/ tests/agent/tools/subagent/ -q` 确认工具测试无回归

---

# 为写操作/副作用工具标记 idempotent=False

## 背景

以下 11 个工具（含 5 个 subagent 写工具）当前未标记 `idempotent`，guardrails 默认 `is_idempotent=False`，行为上与显式标记一致。但显式标记可：

1. 防止后续开发者误标为 `True`
2. 让工具元数据自文档化（一眼看出哪些工具有副作用）
3. 与已显式标记的 `write_file`/`patch_file`/`terminal`/`python_repl`/`web_search`/`message_search`/`skill_manage` 保持一致

## 待标记工具

| #   | 工具                   | 文件                                                 | 理由                                                                          |
| --- | ---------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1   | `todowrite`            | `agent/tools/todolist/tools/todowrite.py`            | 写操作，修改 todo 列表                                                        |
| 2   | `taskflow_create`      | `agent/tools/taskflow/tools/taskflow_create.py`      | 创建 flow，有副作用                                                           |
| 3   | `taskflow_run_task`    | `agent/tools/taskflow/tools/taskflow_run_task.py`    | 运行任务，有副作用                                                            |
| 4   | `taskflow_set_waiting` | `agent/tools/taskflow/tools/taskflow_set_waiting.py` | 设置等待，有副作用                                                            |
| 5   | `taskflow_resume`      | `agent/tools/taskflow/tools/taskflow_resume.py`      | 注入结果，有副作用（代码注释说 idempotent on run_id，但那是去重不是结果不变） |
| 6   | `taskflow_finish`      | `agent/tools/taskflow/tools/taskflow_finish.py`      | 完成 flow，有副作用                                                           |
| 7   | `taskflow_fail`        | `agent/tools/taskflow/tools/taskflow_fail.py`        | 标记失败，有副作用                                                            |
| 8   | `taskflow_cancel`      | `agent/tools/taskflow/tools/taskflow_cancel.py`      | 取消 flow，有副作用                                                           |
| 9   | `taskflow_dispatch`    | `agent/tools/taskflow/tools/taskflow_dispatch.py`    | 批量派发，有副作用                                                            |
| 10  | `taskflow_wait_all`    | `agent/tools/taskflow/tools/taskflow_wait_all.py`    | 阻塞等待，结果依赖时序                                                        |
| 11  | `sessions_spawn`       | `agent/tools/subagent/tools/runtime_tools.py:38`     | 派生子代理，写操作                                                            |
| 12  | `sessions_yield`       | `agent/tools/subagent/tools/runtime_tools.py:94`     | 交还结果，写操作                                                              |
| 13  | `sessions_send`        | `agent/tools/subagent/tools/runtime_tools.py:136`    | 发送消息，写操作                                                              |
| 14  | `sessions_kill`        | `agent/tools/subagent/tools/runtime_tools.py:170`    | 终止子代理，写操作                                                            |
| 15  | `sessions_steer`       | `agent/tools/subagent/tools/runtime_tools.py:199`    | 转向子代理，写操作                                                            |

## 修改方式

### 1-10: taskflow/todolist 工具

这些工具在 `build_*()` 函数中被批量设置 `metadata={"scope": "main_only"}`，需在批量设置后追加：

- `agent/tools/taskflow/tools/__init__.py:55-57` — 在 `for` 循环中或循环后，对所有非只读工具追加 `idempotent: False`
- `agent/tools/todolist/tools/__init__.py:57-63` — 对 `todowrite` 追加

```python
# taskflow/tools/__init__.py — for 循环内统一设置
_READ_ONLY_TASKFLOW = {"taskflow_summary", "taskflow_progress", "taskflow_list"}
for t in _TASKFLOW_TOOLS:
    t.handle_tool_error = True
    t.metadata = {
        "scope": "main_only",
        "idempotent": t.name in _READ_ONLY_TASKFLOW,
    }
```

```python
# todolist/tools/__init__.py
todoread.metadata = {"scope": "main_only", "idempotent": True}
todowrite.metadata = {"scope": "main_only", "todo_update": True, "idempotent": False}
```

### 11-15: subagent runtime tools

`agent/tools/subagent/tools/runtime_tools.py:276-277` 已有部分 `metadata` 设置，扩展覆盖全部 5 个写工具：

```python
_WRITE_ONLY_SUBAGENT = {
    "sessions_spawn", "sessions_yield", "sessions_send",
    "sessions_kill", "sessions_steer",
}
for t in _SUBAGENT_RUNTIME_TOOLS:
    if t.name in _WRITE_ONLY_SUBAGENT:
        t.metadata = {**(t.metadata or {}), "idempotent": False}
# 保留已有的 scope 标记
sessions_kill_runtime_tool.metadata = {"scope": "main_only", "idempotent": False}
sessions_steer_runtime_tool.metadata = {"scope": "main_only", "idempotent": False}
```

## 测试

- 运行 `uv run pytest tests/agent/middlewares/test_tool_guardrails.py -q` 确认 guardrails 测试通过
- 运行 `uv run pytest tests/agent/tools/taskflow/ tests/agent/tools/todolist/ tests/agent/tools/subagent/ -q` 确认工具测试无回归
- 运行 `uv run pytest tests/agent/tools/subagent/test_inherited_tool_policy.py -q` 确认 subagent 工具策略测试通过（该测试断言 metadata 内容）

# ToolGuardrails 缺陷清单

> 源码：`agent/middlewares/tool_guardrails.py`（546 行）
> 配置：`config/features/agent_side/tool_guardrails.py`
> 生命周期：`before_agent` 每次调用创建全新 `_TurnGuardrailState()`（line 338），**所有变量在 turn 结束后自动重置**，下一轮 turn 从零开始。以下缺陷均指**同一 turn 内**的行为。

---

## 缺陷 1：`result_hash` 语义错误 — "有 hash" ≠ "无进展"

**位置**：line 368

```python
result_hash = self._result_hash(result_content) if not is_error and is_idempotent else None
```

**问题**：幂等工具成功时 `result_hash` 恒为非 None。下游三个检测机制均以 `result_hash is not None` 作为"无进展"信号，但幂等工具返回**不同结果**时 `result_hash` 仍然非 None，实际是有进展的。

**影响范围**：

| 检测机制               | 使用 `result_hash` 的方式                                                 | 是否误判                           |
| ---------------------- | ------------------------------------------------------------------------- | ---------------------------------- |
| no_progress (line 179) | `if is_idempotent and result_hash is not None` → 匹配**相同** result_hash | 不误判（结果不同则不匹配）         |
| ping_pong (line 253)   | `curr_no = result_hash is not None` → 只看**是否存在**                    | **误判**（结果不同仍判为无进展）   |
| arg_churn (line 279)   | `if result_hash is not None` → 只看**是否存在**                           | **误判**（结果不同仍进入累积分支） |

**根因**：`result_hash` 承担了两个语义：

1. "此工具是否幂等"（line 368 的 `is_idempotent` 条件）
2. "此调用是否有进展"（line 253, 279 的 `is not None` 判定）

但幂等 ≠ 无进展。幂等工具返回变化的结果也是有进展的。

---

## 缺陷 2：`ping_pong` 判定不比较结果是否相同

**位置**：line 253-257

```python
curr_no = result_hash is not None      # 只看"有没有"，不看"变没变"
prev_no = prev_rec.result_hash is not None
pair_key = ",".join(sorted([prev_rec.name, tool_name]))
if curr_no and prev_no:
    gs.ping_pong_counts[pair_key] += 1  # ← 结果不同也递增
```

**问题**：ping_pong 的"无进展"判定是 `result_hash is not None`（是否存在），而非"两次 result_hash 是否相同"（是否变化）。两个幂等工具交替调用、各自返回不同结果（明确有进展），ping_pong 计数仍然递增。

**应改为**：

```python
curr_no = result_hash is not None
prev_no = prev_rec.result_hash is not None
# 只有当结果确实未变时才算"无进展配对"
curr_stagnant = curr_no and result_hash == prev_rec.result_hash if prev_rec.name == tool_name else curr_no
```

或更简洁的方案：在 `_evaluate_pair_pathologies` 入口处，如果当前调用的 `result_hash` 与上一条 record 的 `result_hash` 不同（且工具相同），直接视为有进展，跳过 ping_pong 递增。

---

## 缺陷 3：`arg_churn` 误把"相同参数不同结果"当作 churn

**位置**：line 279-282

```python
if result_hash is not None:
    variant_key = (tool_name, args_hash)      # 只看工具名+参数
    gs.arg_churn_variants[variant_key] += 1    # ← 结果不同也递增
```

**问题**：`variant_key` 是 `(tool_name, args_hash)`，不包含 `result_hash`。同一工具同一参数但返回不同结果（说明底层状态变了），计数仍然递增。

**实际影响**：较低。arg_churn 触发条件是"多个不同 variant 各达到 `min_calls_per_variant`(3) 次"，单个 variant 重复不会触发。但逻辑上仍是错误的——结果变了应该重置该 variant 的计数。

---

## 缺陷 4：`arg_churn_last_result` 是死状态

**位置**：line 67（定义），line 282（写入），line 305（清空）

```python
# line 67 — 定义
arg_churn_last_result: str = ""

# line 282 — 写入
gs.arg_churn_last_result = result_hash

# line 305 — 清空
gs.arg_churn_last_result = ""
```

**问题**：`arg_churn_last_result` 只被写入和清空，从未被**读取**用于任何比较或判定。推测原意是跟踪连续调用的结果是否变化，但比较逻辑从未实现。

---

## 缺陷 5：幂等工具结果变化时不触发任何重置

**位置**：line 178-305 整体

**问题**：三个检测机制的"进展"信号统一依赖 `result_hash is None`（line 253, 279, 368），该信号只在以下情况为 True：

| 场景                             | `result_hash` | 被视为进展？ | 实际是进展？         |
| -------------------------------- | ------------- | ------------ | -------------------- |
| 非幂等工具成功（如 `todowrite`） | None          | 是           | 是 ✓                 |
| 任何工具报错                     | None          | 是           | 否 ✗（报错不是进展） |
| 幂等工具返回相同结果             | 非 None       | 否           | 否 ✓                 |
| 幂等工具返回**不同**结果         | 非 None       | **否**       | **是 ✗**             |

最后一行是核心缺陷。`ping_pong_counts`、`arg_churn_variants` 的重置逻辑都因此失效。

**修复方向**：在 line 368 之后、`_evaluate` 之前，增加一个"结果是否变化"的判定：

```python
# 在 _wrap_tool_call_impl 中，append record 之前
prev_result_hash = gs.records[-1].result_hash if gs.records else None
result_changed = (
    is_idempotent
    and result_hash is not None
    and prev_result_hash is not None
    and result_hash != prev_result_hash
)
```

然后在 `_evaluate` 或 `_evaluate_pair_pathologies` 中，当 `result_changed=True` 时：

- `no_progress_counts`：保持现状（不清空，绝对计数策略是合理的设计选择）
- 清零 `ping_pong_counts`（与 line 268-273 一致）
- 清零当前 variant 的 `arg_churn_variants` 计数

---

## 边界情况分析

### 已正确处理的边界情况

| #   | 边界情况                                             | 处理位置              | 处理方式                                                                                                   |
| --- | ---------------------------------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------- |
| E1  | `tool` 为 None                                       | line 100              | `_is_idempotent` 首行 `tool is not None` 检查，返回 False                                                  |
| E2  | `tool.metadata` 为 None 或非 dict                    | line 100              | `isinstance(getattr(tool, "metadata", None), dict)` 双重防御，返回 False                                   |
| E3  | `metadata["idempotent"]` 键不存在                    | line 101              | `is not None` 检查，缺失时返回 False                                                                       |
| E4  | `metadata["idempotent"]` 为非布尔值（0/""等）        | line 102              | `bool()` 强制转换                                                                                          |
| E5  | `session_id` 缺失或空白                              | line 106, 336-337     | `require_session_id` 抛 RuntimeError；`_before_agent_impl` 额外检查 `.strip()`                             |
| E6  | `result.content` 为 None                             | line 367              | `str(result.content) if result.content else ""` 降级为空字符串                                             |
| E7  | `result.status` 属性缺失                             | line 366              | `getattr(result, "status", None)` 默认 None → `is_error=False`                                             |
| E8  | `tool_call["name"]` 缺失                             | line 361              | `.get("name", "unknown")` 默认值                                                                           |
| E9  | `tool_call["args"]` 缺失                             | line 362              | `.get("args", {})` 默认空字典                                                                              |
| E10 | `gs.records` 首次调用为空                            | line 251              | `if len(gs.records) >= 2` 守卫，跳过 ping_pong                                                             |
| E11 | 动作升级非单调（pair pathology 降级）                | line 266, 300         | `_ACTION_RANK[xx] > _ACTION_RANK[action]` 确保 only-escalate                                               |
| E12 | pair pathology 与已 BLOCK/HALT 重复计算              | line 202              | `if action in (ALLOW, WARN)` 守卫，已 BLOCK/HALT 时跳过 pair 检测                                          |
| E13 | `before_agent` 每轮重置状态                          | line 338              | `set_state(session_id, key, _TurnGuardrailState())` 创建全新实例                                           |
| E14 | WARN 不阻断数据返回                                  | line 477              | `content=f"{result_content}\n\n{warning}"` 在原结果后追加警告，agent 仍可使用数据                          |
| E15 | BLOCK/HALT 丢弃原始结果                              | line 393-398, 434-439 | 构造新的 `ToolMessage(status="error")`，原结果不泄露给 agent                                               |
| E16 | HALT 状态持续到 turn 结束                            | line 135-136, 498-504 | `_evaluate` 入口 + `_wrap_tool_call_precheck` 双重检查，HALT 后所有后续工具调用直接返回错误                |
| E17 | recovery 释放后重新评估                              | line 506-510          | precheck 中 `discard` 后返回 None，工具走完整 `_evaluate` 流程；若结果未变则再次 BLOCK，若结果已变则 ALLOW |
| E18 | `same_tool_failure` WARN 不叠加 `exact_failure` WARN | line 175              | `and action == GuardrailAction.ALLOW` 守卫，已被 exact_failure WARN 时不重复 WARN                          |
| E19 | `ping_pong_counts` 配对打断时全部归零                | line 271-273          | 重置所有 pair key（保守设计，避免残留误报）                                                                |
| E20 | `arg_churn_variants` 非幂等成功时清空                | line 302-304          | `clear()` + 重置 `arg_churn_last_result`                                                                   |

### 未处理的边界情况（潜在缺陷）

---

## 缺陷 6：`tool_call["id"]` 直接索引，无防御

**位置**：line 395, 412, 436, 501, 515

```python
tool_call_id=request.tool_call["id"]  # ← 直接索引，5 处
```

**问题**：`name` 和 `args` 都用了 `.get(key, default)` 防御性取法，但 `id` 用了直接索引 `["id"]`。如果 `tool_call` 字典缺少 `id` 键，抛 KeyError 崩溃。

**影响**：与 line 361-362 的防御风格不一致。`ToolCallRequest` 是 LangGraph 内部对象，正常情况 `id` 一定存在，但代码风格应统一。

**修复**：`.get("id", "")` 或在入口处断言 `id` 存在。

---

## 缺陷 7：`gs.records` 无界增长

**位置**：line 370-377（append），line 180（遍历）

```python
# line 370 — 每次调用都 append，从不清理
gs.records.append(_ToolCallRecord(...))

# line 180 — no_progress 反向遍历整个列表
for rec in reversed(gs.records):
    if rec.name == tool_name and rec.result_hash == result_hash:
        ...
        break
```

**问题**：`records` 列表在整个 turn 内只增不减。对于超长 turn（agent 调用 100+ 次工具）：

- 内存：每条 record 含 name(str) + args_hash(str) + is_error(bool) + result_hash(str|None)，100 条约几 KB，不算大
- 性能：`reversed(gs.records)` 在**无匹配**时扫描整个列表。no_progress 有 `break`，但最坏情况（新 result_hash 首次出现）遍历全部历史记录

**实际影响**：低。LangGraph 单 turn 工具调用通常 <50 次。但如果 agent 被 HALT 后仍被框架调用（不应该），records 会继续增长。

**修复方向**：设置 `max_records` 上限（如 200），超过后丢弃**最近最少使用**的记录而非最旧的。no_progress 检测（line 180）反向遍历查找同工具同 result_hash 的记录，最近使用的记录应保留。最旧的记录可能是唯一匹配项，丢弃会导致 no_progress 漏检。

实现思路：每次 no_progress 的 `break` 命中时，将该 record 移到列表尾部（或用 `OrderedDict` + `move_to_end`），淘汰时从头部丢弃。

---

## 缺陷 8：`StateRegisterMeM.get_state` 返回引用而非副本（TOCTOU）

**位置**：`runtime/state_register.py` line 28-37

```python
def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
    with self._lock:
        if session_id not in self._states:
            return default
        return self._states[session_id].get(key, default)  # ← 返回引用
```

对比 `get_all_states`（line 39-46）返回了 `dict(...)` 快照：

```python
def get_all_states(self, session_id: str) -> dict[str, Any]:
    with self._lock:
        return dict(self._states.get(session_id, {}))  # ← 快照
```

**问题**：`get_state` 返回 `_TurnGuardrailState` 对象的**引用**。guardrails 的 `_wrap_tool_call_impl` 在 line 360 `get_state` 和 line 380 `set_state` 之间修改 `gs` 的字段（line 370-379），这期间**不在锁内**。如果同 session 有并发访问（如 subagent 共享 session_id），可能导致字段级竞争。

**实际影响**：低。LangGraph 中间件通常单线程串行执行。但架构上是不安全的——`StateRegisterMeM` 提供了锁，但 guardrails 绕过了它。

**修复方向**：`get_state` 返回 `copy.deepcopy`，或 guardrails 在 `_get_state` 时深拷贝。

---

## 缺陷 9：`is_error` 判定大小写敏感

**位置**：line 366

```python
is_error = getattr(result, "status", None) == "error"
```

**问题**：只匹配小写 `"error"`。如果 `ToolMessage.status` 为 `"Error"`、`"ERROR"` 或 `"err"`（不同框架版本可能不一致），不被识别为错误，跳过 exact_failure 和 same_tool_failure 检测。

**实际影响**：低。LangChain `ToolMessage.status` 是 `Literal["success", "error", ...]`，框架保证小写。但如果 `handle_tool_error=True` 捕获异常后返回的 `ToolMessage` 使用了不同的大小写，会漏检。

---

## 缺陷 10：`_args_hash` 函数内导入

**位置**：line 114-118

```python
@staticmethod
def _args_hash(args: dict[str, Any]) -> str:
    from agent.middlewares.base import args_hash  # ← 每次调用都导入
    return args_hash(args)
```

**问题**：`import` 在函数体内，每次工具调用都执行。Python 的 import 缓存使其不会重复加载模块，但仍有 `sys.modules` 字典查找开销。

**实际影响**：极低。`sys.modules` 查找是 O(1)。但风格上应提到模块顶层。

**注意**：可能是为了规避循环导入。如果 `agent.middlewares.base` 导入了 `tool_guardrails`，顶层导入会循环。函数内导入延迟到运行时，打破循环。

---

## 缺陷 11：`tool_name="unknown"` 计数器交叉污染

**位置**：line 361

```python
tool_name: str = request.tool_call.get("name", "unknown")
```

**问题**：如果多个工具的 `tool_call` 缺少 `name` 字段（框架 bug），它们都变成 `"unknown"`，共享同一组计数器（`exact_failure_counts["unknown:hash"]`、`same_tool_failure_counts["unknown"]` 等）。一个工具的失败会累加到另一个工具的计数上。

**实际影响**：极低。`name` 缺失是框架级 bug，不应发生。但与其他防御性编码风格不一致。

---

## 缺陷 12：`hashlib.md5` 编码异常未捕获

**位置**：line 121-122

```python
@staticmethod
def _result_hash(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()
```

**问题**：`content.encode()` 默认 UTF-8 编码。如果 `result.content` 包含无法 UTF-8 编码的字节（二进制工具输出？），抛 `UnicodeEncodeError` 崩溃。

**实际影响**：极低。`str(result.content)` 在 line 367 已确保是字符串，Python 字符串的 `.encode()` 默认用 UTF-8 且几乎不会失败（surrogateescape 机制）。

---

## 边界情况汇总

| #      | 边界情况                       | 类型       | 严重度 | 位置                     |
| ------ | ------------------------------ | ---------- | ------ | ------------------------ |
| E1-E20 | 已正确处理的 20 种边界         | 防御性编码 | —      | 见上表                   |
| 6      | `tool_call["id"]` 无防御       | 未处理     | P3     | line 395,412,436,501,515 |
| 7      | `records` 无界增长             | 未处理     | P3     | line 370, 180            |
| 8      | `get_state` 返回引用（TOCTOU） | 未处理     | P2     | state_register.py:28     |
| 9      | `is_error` 大小写敏感          | 未处理     | P3     | line 366                 |
| 10     | 函数内导入 `_args_hash`        | 风格       | P4     | line 116                 |
| 11     | `tool_name="unknown"` 交叉污染 | 未处理     | P4     | line 361                 |
| 12     | `md5` 编码异常未捕获           | 未处理     | P4     | line 122                 |

---

## 缺陷汇总表（含边界情况）

| #   | 缺陷                                          | 位置              | 严重度 | 影响的检测机制               |
| --- | --------------------------------------------- | ----------------- | ------ | ---------------------------- |
| 1   | `result_hash` 语义混淆（存在≠无进展）         | line 368          | P0     | ping_pong, arg_churn         |
| 2   | ping_pong 不比较结果是否相同                  | line 253          | P1     | ping_pong                    |
| 3   | arg_churn 不区分结果变化                      | line 279-282      | P2     | arg_churn                    |
| 4   | `arg_churn_last_result` 死状态                | line 67, 282      | P3     | —                            |
| 5   | 幂等结果变化不触发重置（缺陷 1, 2, 3 的根因） | line 178-305      | P0     | 所有三个机制                 |
| 6   | `tool_call["id"]` 直接索引无防御              | line 395 等       | P3     | 消息构造（KeyError 崩溃）    |
| 7   | `gs.records` 无界增长                         | line 370          | P3     | 内存 / no_progress 遍历性能  |
| 8   | `get_state` 返回引用（TOCTOU 竞争）           | state_register:28 | P2     | 并发安全                     |
| 9   | `is_error` 大小写敏感                         | line 366          | P3     | exact/same_tool_failure 漏检 |
| 10  | 函数内导入 `_args_hash`                       | line 116          | P4     | 风格 / 可维护性              |
| 11  | `tool_name="unknown"` 计数器交叉污染          | line 361          | P4     | 计数器准确性                 |
| 12  | `md5` 编码异常未捕获                          | line 122          | P4     | 崩溃风险                     |

---

## 修复优先级建议

### P0：修复"幂等结果变化 = 进展"判定（缺陷 1, 5）

在 `_wrap_tool_call_impl` 中，append record 之前计算 `result_changed`，传入 `_evaluate`，在三个检测机制中用作重置信号：

```python
# _wrap_tool_call_impl 中
prev_result_hash = gs.records[-1].result_hash if gs.records else None
result_changed = (
    is_idempotent
    and result_hash is not None
    and prev_result_hash is not None
    and result_hash != prev_result_hash
)
```

- `no_progress_counts`：保持现状（不清空，绝对计数策略）
- `ping_pong_counts`：`result_changed=True` 时执行 line 268-273 的重置逻辑
- `arg_churn_variants`：`result_changed=True` 时清空（与非幂等成功一致）

### P1：修复 ping_pong 不比较结果是否相同（缺陷 2）

见缺陷 1 的修复方案——引入 `is_stagnant` 替代 `result_hash is not None`，缺陷 2 自动解决。

### P2：修复 arg_churn 不区分结果变化（缺陷 3）

见缺陷 1 的修复方案——引入 `is_stagnant` 替代 `result_hash is not None`，缺陷 3 自动解决。

### P3：清理死状态和小缺陷（缺陷 4, 6, 7, 9）

- 删除 `arg_churn_last_result` 或实现其比较逻辑
- `tool_call["id"]` 改为 `.get("id", "")` 或入口断言
- `gs.records` 设置上限（如 200 条），超过丢弃最近最少使用的记录
- `is_error` 改为 `str(getattr(result, "status", "")).lower() == "error"`

### P4：风格修复（缺陷 10-12）

- `_args_hash` 的 import 提到模块顶层（需确认无循环导入）
- `tool_name="unknown"` 改为 `"__unnamed__"` 或直接抛错
- `_result_hash` 增加 `errors="surrogateescape"` 参数

---

## 修复状态（2025-09-14）

所有 12 个缺陷已处理，54 个测试全部通过（17 guardrails + 37 工具测试）。

| #   | 缺陷                                  | 修复方式                                                                                      | 状态      |
| --- | ------------------------------------- | --------------------------------------------------------------------------------------------- | --------- |
| 1   | `result_hash` 语义混淆（存在≠无进展） | 引入 `is_stagnant` 字段，在 append 前扫描 `gs.records` 判定是否存在同工具同结果的先前调用     | ✅ 已修复 |
| 2   | ping_pong 不比较结果是否相同          | `curr_no = is_stagnant`，`prev_no = prev_rec.is_stagnant` 替代 `result_hash is not None`      | ✅ 已修复 |
| 3   | arg_churn 不区分结果变化              | `if is_stagnant:` 替代 `if result_hash is not None:` 作为累积分件；非幂等成功仍 `clear()`     | ✅ 已修复 |
| 4   | `arg_churn_last_result` 死状态        | 删除该字段（`is_stagnant` 取代其预期功能）                                                    | ✅ 已修复 |
| 5   | 幂等结果变化不触发重置（根因）        | `is_stagnant=False` 时：ping_pong reset + arg_churn 不递增                                    | ✅ 已修复 |
| 6   | `tool_call["id"]` 直接索引无防御      | 全部 5 处改为 `.get("id", "")`                                                                | ✅ 已修复 |
| 7   | `gs.records` 无界增长                 | `_MAX_RECORDS = 200`，append 后 `gs.records = gs.records[-_MAX_RECORDS:]`                     | ✅ 已修复 |
| 8   | `get_state` 返回引用（TOCTOU 竞争）   | 未修复（影响极低，LangGraph 中间件单线程串行执行）                                            | ⏸ 暂缓    |
| 9   | `is_error` 大小写敏感                 | `str(getattr(result, "status", "")).lower() == "error"`                                       | ✅ 已修复 |
| 10  | 函数内导入 `_args_hash`               | `args_hash` 提升至模块顶层 `from agent.middlewares.base import args_hash, require_session_id` | ✅ 已修复 |
| 11  | `tool_name="unknown"` 计数器交叉污染  | 未修复（影响极低，框架级 bug 不应发生）                                                       | ⏸ 暂缓    |
| 12  | `md5` 编码异常未捕获                  | `content.encode(errors="surrogateescape")`                                                    | ✅ 已修复 |

### 修复细节

#### `is_stagnant` 语义

```python
is_stagnant = False
if is_idempotent and result_hash is not None:
    for rec in reversed(gs.records):  # append 前扫描，不含当前记录
        if rec.name == tool_name and rec.result_hash == result_hash:
            is_stagnant = True
            break
```

- `is_stagnant=True`：同工具同结果的先前调用存在 → 无进展
- `is_stagnant=False`：首次调用、结果变化、非幂等成功、或报错 → 有进展

#### `no_progress` 不受影响

`no_progress` 检测（line 179）扫描 `gs.records`（含当前记录），始终匹配当前记录并递增计数。此行为未改变——计数表示"此 (tool, result_hash) 对被观察到的总次数（含当前）"。

#### 测试调整

- `test_ping_pong_symmetric_warn_and_block`：改用同工具同结果（A:"ha", B:"hb"），抑制 no_progress（`no_progress_warn_after=100`），序列缩短至 9 次（到首次 BLOCK）
- `test_ping_pong_asymmetric_reset`：同上模式，抑制 no_progress
- `test_arg_churn_*`：每 variant 调用从 3 次增至 4 次（首次不 stagnant，后续 3 次 stagnant → 计数 3）
- `test_arg_churn_reset_on_progress`：先调 2 次同结果使计数=1，再非幂等成功验证清空
- `test_wrap_message_routing_ping_pong_warn`：从 5 次增至 7 次（ping_pong 需 pair=4）
- `test_wrap_message_routing_arg_churn_block`：每 variant 4 次调用，索引调整
- `call()` helper：计算 `is_stagnant` 并传入 `_evaluate`
