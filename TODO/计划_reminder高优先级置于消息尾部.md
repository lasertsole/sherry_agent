# 计划：reminder 消息按优先级逆序注入（高优先级置于尾部）

- 建档日期：2026-10-22
- 状态：**已实施**
- **文档范围**：修正 DynamicReminderMiddleware 注入的 `<system-reminder>` 消息排列方向——高优先级 source 原本排在消息列表前部（靠近用户消息），低优先级排在尾部（靠近模型生成点）；reverse 后高优先级置于尾部，利用 LLM recency bias 给最 action-relevant 的视图（plan mode / todo list）最强的注意力。
- **改动规模**：2 个文件，各 1 处改动，共 ~15 行
- 关联代码：
  - `agent/middlewares/reminder_catalog/dynamic.py:78-84`（构建 `HumanMessage` 列表的列表推导）
  - `agent/middlewares/reminder_catalog/core.py:120-183`（`resolve_injections`——预算分配逻辑，**不动**）
  - `agent/middlewares/reminder_catalog/sources.py:43-120`（七个默认 source 的优先级声明，**不动**）
  - `tests/agent/middlewares/test_reminder_catalog.py:211-221`（顺序断言）
  - 关联 TODO：跟其他 TODO 正交

---

## 0. 背景：为什么当前排列不合理

### 0.1 当前排列（改动前）

`resolve_injections`（`core.py:164`）按**降序优先级**排序候选 source：

```python
candidates.sort(key=lambda item: -item[0].priority)
```

然后按这个顺序构建 `result` dict（插入有序），`dynamic.py` 按 dict 遍历顺序构建 `HumanMessage` 列表并追加到消息列表末尾。因为此时消息列表最后一条是用户消息（`dynamic.py:58-64` 确认了这一点才注入），最终排列为：

```
[...历史消息..., 用户消息, plan_mode(85), todo(80), taskflow(75), boulder(70), continuity(65), project_dir(60), knowledge_index(50)]
                  ↑ 高优先级在前                                  低优先级在尾部 ↑
```

### 0.2 问题

LLM 存在 recency bias——上下文尾部的内容获得更强的注意力权重。当前排列把**最低优先级**的 `knowledge_index`（p50）放在了注意力最强的尾部，而**最高优先级**的 `plan_mode`（p85）和 `todo`（p80）被推到了离生成点最远的位置。

### 0.3 排序的真实意图

`core.py` 的排序**不是**为了控制消息位置，而是为了**预算分配**：超 30,000 字符总预算时，从低优先级开始丢弃（`core.py:168-176`）。`ReminderSource` 的 docstring 写的是：

```
priority: Higher wins under the total budget; 100 = must keep.
```

`sources.py` 也说：

```
Budgets and priorities follow the catalogue plan: a live view that a turn may
starve for is cheap and low-priority (knowledge index), one the agent is
actively steering by is expensive and high (todo list, plan mode).
```

消息列表中的位置是预算排序的**副产物**，不是有意设计的注意力排列。reverse 后把消息排列与预算逻辑解耦。

---

## 1. 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：预算分配逻辑不变 | `resolve_injections` 的排序 + 预算丢弃逻辑完全不动；高优先级 source 仍然优先保留 | `test_the_total_budget_drops_the_lowest_priority_source` 通过 |
| **C2**：消息排列方向反转 | 高优先级 source 在 `HumanMessage` 列表末尾（靠近模型生成点） | `test_the_turn_first_call_injects_every_ready_source` 断言更新 |
| **C3**：其他消费者不受影响 | 所有把 reminder 当无序集合处理的消费者（turn_utils / summarization / HITL）行为不变 | 现有测试通过 |
| **C4**：中间件链顺序不变 | `DynamicReminderMiddleware` 在链中的位置不动；`test_middleware_order.py` 通过 | 现有测试通过 |

---

## 2. 改动清单

### 2.1 `agent/middlewares/reminder_catalog/dynamic.py`

**改动位置**：`abefore_model` 方法内构建 `reminders` 列表推导（原 ~78-84 行）

**改动前**：

```python
            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(source_id),
                )
                for source_id, content in injections.items()
            ]
```

**改动后**：

```python
            # High-priority sources land LAST (closest to the model's
            # generation point): LLM recency bias gives stronger attention to
            # the tail of the context, so the most action-relevant views
            # (plan mode, todo list) get the prime tail position.  The sort in
            # resolve_injections is descending-by-priority for budget
            # allocation (drop lowest first); reversing here decouples message
            # ordering from that budget logic.
            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(source_id),
                )
                for source_id, content in reversed(list(injections.items()))
            ]
```

**要点**：
- `injections` 是 `resolve_injections` 返回的 `dict[str, str]`，插入顺序 = 降序优先级（高→低）
- `reversed(list(injections.items()))` 翻转为升序优先级（低→高），高优先级 source 最后进入 `reminders` 列表
- `reminders` 列表追加到消息列表末尾后，高优先级在最尾部

### 2.2 `tests/agent/middlewares/test_reminder_catalog.py`

**改动 1**：模块 docstring（~6-7 行）补充 ordering 说明

**改动前**：

```python
the total budget (lowest priority dropped first), the tag escaping and the
consumer-facing metadata contract.
```

**改动后**：

```python
the total budget (lowest priority dropped first), the message ordering (high
priority at the tail for recency bias), the tag escaping and the
consumer-facing metadata contract.
```

**改动 2**：`TestMiddleware.test_the_turn_first_call_injects_every_ready_source`（~218-221 行）

测试用的合成 source：`alpha`（priority=90）、`beta`（priority=10）。

**改动前**：

```python
        origins = [m.metadata["origin"] for m in out["messages"]]
        assert origins == ["alpha", "beta"], "priority order"
        assert all(m.metadata["provenance"] == "reminder_catalog" for m in out["messages"])
        assert out["messages"][0].content.startswith('<system-reminder source="alpha">')
```

**改动后**：

```python
        origins = [m.metadata["origin"] for m in out["messages"]]
        assert origins == ["beta", "alpha"], "low priority first, high priority at the tail"
        assert all(m.metadata["provenance"] == "reminder_catalog" for m in out["messages"])
        assert out["messages"][-1].content.startswith('<system-reminder source="alpha">')
```

**要点**：
- `["alpha", "beta"]` → `["beta", "alpha"]`：低优先级 beta 在前，高优先级 alpha 在尾
- `out["messages"][0]` → `out["messages"][-1]`：检查 alpha 从列表头部改到尾部
- 断言消息从 "priority order" 改为 "low priority first, high priority at the tail"

---

## 3. 不动的部分

| 文件 | 不动的原因 |
|---|---|
| `core.py:164`（`candidates.sort(key=lambda item: -item[0].priority)`） | 排序服务预算分配——降序保证超预算时先丢弃低优先级 source。reverse 只在 `dynamic.py` 构建 `HumanMessage` 时做，预算逻辑与消息排列解耦 |
| `core.py:120-183`（`resolve_injections` 整体） | 返回的 dict 仍然是高→低插入序；调用方决定遍历方向 |
| `sources.py`（七个 source 的优先级声明） | 优先级数值不变；变的只是高优先级在消息列表中的位置 |
| 中间件链顺序 | `DynamicReminderMiddleware` 的链位置不动——它在 `TaskIntent` 之后、`OutputRepetitionGuard` 之前。`test_middleware_order.py` 不变 |

---

## 4. 改动后的消息排列

```
[...历史消息..., 用户消息, knowledge_index(50), project_dir(60), continuity(65), boulder(70), taskflow(75), todo(80), plan_mode(85)]
                  ↑ 低优先级在前                                      高优先级在尾部 ↑
                                                              ↑ recency bias 最强
```

七个 source 加起来约 9,000 字符，远低于 30,000 总预算上限，实际场景下七个全部注入，最终消息列表尾部固定是 `plan_mode`（p85）——优先级最高的计划模式状态。

---

## 5. 影响面分析（已核实）

### 5.1 消费者依赖检查

所有把 reminder 当无序集合处理的消费者（position-independent）：

| 消费者 | 文件 | 处理方式 | 受影响？ |
|---|---|---|---|
| `split_into_turns` | `pub/func/message/turn_utils.py:16-36` | `is_system_reminder` 跳过，不关心位置 | 否 |
| `_msg_type` | `agent/middlewares/summarization/memory_flush.py:158-168` | 标记为 `"system"` | 否 |
| summary 过滤 | `agent/middlewares/summarization/summary_generation.py:217` | `is_system_reminder` 过滤 | 否 |
| `_is_headless_turn` | `agent/middlewares/humanInTheLoop/approval_scope.py:67-89` | 逆序扫描找最后一个真实 human，跳过 reminder | 否 |

### 5.2 后续中间件检查

`DynamicReminderMiddleware` 之后运行的中间件（`OutputRepetitionGuard` → `MaxTokensBoost` → `ThinkingControl` → `HITL` → `MessagePersistence` → `LLMRetry` → `Summarization` → `InvalidToolCallRepair`）：

- **OutputRepetitionGuard**：对模型*输出*做内容 hash，不关心消息列表中 reminder 的位置
- **Summarization**：按 `is_system_reminder` 分类过滤，position-independent；压缩后调 `reset_session` 重新注入
- **ContextEviction**：注册在 DynamicReminder **之前**，其 `before_model` 看不到 reminder；`wrap_model_call` 只替换带 `EVICTED_TO_KEY` 的 HumanMessage，reminder 不带该 key
- **HITL**：`_is_headless_turn` 逆序扫描找真实 human 消息，跳过 reminder——order-independent

### 5.3 测试回归

```
python -m pytest tests/agent/middlewares/test_reminder_catalog.py -q  →  20 passed
python -m pytest tests/agent/core/test_middleware_order.py \
  tests/agent/middlewares/test_subagent_completion_drain_reminder.py -q  →  6 passed
```

---

## 6. 复现步骤

```bash
# 1. 改 dynamic.py：reversed(list(injections.items()))
# 2. 改 test_reminder_catalog.py：断言顺序 + docstring
# 3. 跑测试
python -m pytest tests/agent/middlewares/test_reminder_catalog.py -q -k "not llm_e2e"
python -m pytest tests/agent/core/test_middleware_order.py -q -k "not llm_e2e"
```

---

## 7. 关键参考

### sherry 内部

- `agent/middlewares/reminder_catalog/core.py:62-86`（`ReminderSource` dataclass——priority 字段 docstring："Higher wins under the total budget"）
- `agent/middlewares/reminder_catalog/core.py:164`（排序行——降序，服务预算分配）
- `agent/middlewares/reminder_catalog/core.py:166-183`（预算遍历——从高优先级开始，超预算跳过低优先级）
- `agent/middlewares/reminder_catalog/dynamic.py:58-64`（注入前提：最后一条必须是真实用户消息）
- `agent/middlewares/reminder_catalog/sources.py:43-120`（七个默认 source 的优先级：85/80/75/70/65/60/50）
- `pub/func/message/system_reminder.py:80-90`（`wrap_reminder`——标签包裹 + 嵌套转义）

### 关联 TODO

- 跟其他 TODO（压缩/检索/任务编排/turn 折叠展示）正交——本改动只影响 reminder 消息在消息列表中的排列方向
