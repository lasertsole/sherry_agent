# 计划：plan extraction 独立化（脱离压缩路径 + plan 快照去重）

- 建档日期：2026-10-14
- 状态：方案设计，未开工
- **2026-10-14 重写**：从"压缩时 nudge 修复重复抽取"改为"plan 完成时独立 fork agent"。合并了之前的 plan 快照去重方案。
- 设计原则：
  1. **抽取单位是 plan（整个 todo 列表快照），不是单个 todo**——不拆分经验，nudge agent 必须看到全部 todo 才能抽 class-level 模式
  2. **plan extraction 不需要消息历史**——数据全在 todos + 子 agent runs（含 outcome/error/result_text）+ plan 文件
  3. **plan extraction 不依赖压缩**——在 plan 完成时直接 fork agent，不等上下文窗口填满
  4. **用 aux 模型，不用 main 模型**——输入是结构化数据 + 模板化抽取，StepJudge/CompletionJudge 已验证 aux 能做同类任务
  5. **压缩时的 memory review 机制保持不变**——memory review 仍在压缩路径触发，职责是抢救即将丢弃的消息中的长期记忆。只从压缩路径中移除 plan extraction，memory review 不动。

---

## 0. 现状问题

### 0.1 plan extraction 绑在压缩路径上

```
压缩触发（上下文窗口填满）
  → _fire_compression_nudges(session_id, original_messages, messages_to_summarize)
    → _schedule_compression_nudges(session_id, original_messages)  ← 传完整对话
      → _run_compression_nudges:
          await _nudge_memory(session_id, system_prompt, sanitized)       ← memory review
          if need_plan:
              await _nudge_plan_extraction(session_id, system_prompt, sanitized)  ← plan extraction
```

三个问题：

| 问题 | 原因 | 后果 |
|---|---|---|
| 短会话不触发 | plan extraction 唯一入口在压缩路径 | plan 完成但窗口没满 → 经验永远不抽 |
| 爆上下文窗口 | nudge agent 拿完整对话(~105K) + plan_context(~10K) + prompt(~5K) = ~124K | 131K 窗口差 7K 就爆 |
| 重复抽取 | `nudge_plan_extraction_fired` flag 只在"不再全完成"时重置；completed todo 内容被改（status 不变）时不重置 | 漏抽已变化的 plan |

### 0.2 现有 flag 机制的覆盖与缺口

| 场景 | flag 行为 | 对不对？ |
|---|---|---|
| 5 todo 全完成，首次 | False→True → 抽 | ✅ |
| 同一批，再压缩 | True → 跳 | ✅（没变） |
| 加 3 新 todo → 8 全完成 | 重置→False→True → 抽（传全部 8 个） | ✅（全貌重抽） |
| **completed todo 内容被改（status 不变）** | True（不重置）→ 跳 | **❌ 漏抽** |

---

## 1. 设计

### 1.1 架构：plan extraction 从压缩路径独立

```
压缩路径（只管 memory review）：
  压缩触发
  → _nudge_memory(session_id, system_prompt, messages_to_summarize)
    ← 只传被丢弃的部分（memory review 的职责是抢救即将丢失的消息）

plan 完成路径（独立 fork）：
  aafter_agent（turn 结束时）
    → todos 全完成? + plan snapshot hash 变了?
    → asyncio.create_task(_run_plan_extraction(session_id))  ← fire-and-forget
      → NUDGE lane（不和 main/subagent 竞争）
      → _create_nudge_agent(system_prompt)
      → ainvoke(messages=[HumanMessage(prompt + plan_context)])  ← 零对话历史
      → 抽完存 snapshot hash
```

### 1.2 plan 快照去重

在 plan 完成触发点加 **plan 内容 hash**——todo 列表的指纹（id + content + status + plan_ref）。抽取后存当前 hash。下次触发时比较：变了就重抽（传全部 todo），没变就跳过。

```python
import hashlib, json

def _plan_snapshot_hash(todos: list[dict]) -> str:
    items = sorted([
        {"id": t.get("id", ""), "content": t.get("content", ""),
         "status": t.get("status", ""), "plan_ref": t.get("plan_ref", "")}
        for t in todos
    ], key=lambda x: x["id"])
    return hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
```

### 1.3 State keys

```python
# runtime/session/state_keys.py 新增：
NUDGE_PLAN_SNAPSHOT_HASH = "nudge_plan_snapshot_hash"   # str：上次抽取时的 todo 内容 hash
```

保留现有的 `_PLAN_EXTRACTION_FIRED_KEY`（用于"全完成时是否需要检查"的门控）和 `_PLAN_EXTRACTION_LOCK_KEY`（in-flight 锁）。

`NUDGE_PLAN_SNAPSHOT_HASH` 存在 `state_register_db`（SQLite，跨重启不丢）。`clear_session` 已清所有 state register 变量。

---

## 2. 改动

### 2a：新增 plan 完成触发（核心）

**文件**：`agent/middlewares/summarization/nudges.py`（或新中间件）

在 `aafter_agent`（turn 结束时）检测 todos 全完成 + hash 变了 → fire-and-forget fork：

```python
async def maybe_trigger_plan_extraction(session_id: str) -> None:
    """在 aafter_agent 中调用：检测 plan 完成并 fork 抽取 agent。

    条件链：
    1. todos 非空且全部 completed/cancelled
    2. plan snapshot hash 与上次抽取时不同（内容变了）
    3. 没有 in-flight 的 plan extraction（_PLAN_EXTRACTION_LOCK_KEY）
    4. nudge option 开启（middleware_option("Summarization", "nudge")）
    """
    from agent.middlewares.agent_switch import middleware_option

    if not middleware_option(session_id, "Summarization", "nudge"):
        return

    # 复用现有的 _detect_todo_all_complete（含 flag + hash 门控）
    if not _detect_todo_all_complete(session_id):
        return

    # in-flight 锁
    if state_register_mem.get_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False):
        return

    # fire-and-forget
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # 无 event loop（同步路径），跳过

    task = asyncio.create_task(_run_plan_extraction(session_id))
    _COMPRESSION_NUDGE_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_NUDGE_TASKS.discard)
```

### 2b：`_create_plan_extraction_agent`（aux 模型 + 零对话历史）

**文件**：`agent/middlewares/summarization/nudges.py`

**不改共享的 `_create_nudge_agent`**（memory review 还在用它）。新建专用 agent 工厂，用 aux 模型：

```python
async def _create_plan_extraction_agent(system_prompt: str):
    """plan extraction 专用 agent——aux 模型 + nudge 工具门控。

    不用 build_main_llm：输入是结构化数据（todos + runs + plan 文件），
    任务是模板化抽取（failure_set / success_path / skill patch），
    StepJudge / CompletionJudge 已验证 aux 能做同类任务。
    """
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_auxiliary_llm
    from agent import get_agent_tools

    aux_llm = build_auxiliary_llm(temperature=0)  # ← aux 模型
    agent_tools = get_agent_tools()
    return create_agent(
        model=aux_llm,
        state_schema=StateSchema,
        system_prompt=system_prompt,    # 保留——需要 skills index 决定 patch vs create
        middleware=[
            _NudgeLimitTool(),          # 只放行 nudge: True 的工具（skill_manage, knowledge）
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=list(agent_tools),
    )
```

### 2c：`_run_plan_extraction`（aux 模型 + 零对话历史）

**文件**：`agent/middlewares/summarization/nudges.py`

```python
async def _run_plan_extraction(session_id: str) -> None:
    """独立 fork 的 plan extraction agent。aux 模型 + 零对话历史。"""
    state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, True)
    try:
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt

        system_prompt = _get_and_reload_system_prompt(session_id)
        context = _build_plan_context(session_id)
        if not context:
            return

        context_str = json.dumps(context, ensure_ascii=False, indent=2)
        prompt = _PLAN_EXTRACTION_PROMPT.replace("{plan_context}", context_str)
        prompt = _render_prompt_facts(prompt)

        async with lane_slot(LaneType.NUDGE):
            _agent = await _create_plan_extraction_agent(system_prompt)  # ← aux 模型
            await _agent.ainvoke(input={
                "session_id": session_id,
                "messages": [HumanMessage(content=prompt)],  # ← 只有 instruction + plan_context
            })                                                   #    零对话历史

        # 抽完存 hash（成功后才存 → 失败下次重试）
        todos = get_todos_sync(session_id)
        state_register_db.set_state(
            session_id, StateKey.NUDGE_PLAN_SNAPSHOT_HASH,
            _plan_snapshot_hash(todos))
    except Exception:
        logger.exception("plan extraction failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)
```

### 2d：`_detect_todo_all_complete` 加 hash 检查

**文件**：`agent/middlewares/summarization/nudges.py`

```python
def _detect_todo_all_complete(session_id: str) -> bool:
    todos = get_todos_sync(session_id)
    if not todos:
        return False

    all_done = all(t.get("status") in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
    if not already_fired:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, True)
        return True

    # flag 已设 → 检查 plan 内容是否变化
    current_hash = _plan_snapshot_hash(todos)
    stored_hash = state_register_db.get_state(
        session_id, StateKey.NUDGE_PLAN_SNAPSHOT_HASH, "") or ""
    if current_hash != stored_hash:
        return True  # plan 变了 → 重抽

    return False  # flag 已设 + hash 没变 → 跳过
```

### 2e：压缩路径只保留 memory review（memory review 机制不变）

**文件**：`agent/middlewares/summarization/core.py` + `nudges.py`

压缩路径只移除 plan extraction 调用，**memory review 的触发时机、传参、模型、逻辑全部不动**：

```python
# core.py — _fire_compression_nudges 只删 plan extraction，memory review 调用不变
def _fire_compression_nudges(self, session_id, original_messages, messages_to_summarize):
    # memory review 保持原样（schedule_compression_nudges 仍传 original_messages）
    _schedule_compression_nudges(session_id, original_messages)  # ← 不改
    _schedule_compression_todo_update(session_id, messages_to_summarize)
```

```python
# nudges.py — _run_compression_nudges 只删 plan extraction 分支
async def _run_compression_nudges(session_id, messages):
    system_prompt = _get_and_reload_system_prompt(session_id)
    sanitized = sanitize_tool_use_result_pairing(list(messages))
    await _nudge_memory(session_id, system_prompt, sanitized)     # ← 不改
    # if need_plan: await _nudge_plan_extraction(...)  ← 删除
```

`schedule_compression_nudges` 的 `need_plan` 逻辑（`_PLAN_EXTRACTION_ENABLED and _detect_todo_all_complete`）删除，但 `schedule_compression_nudges` 函数本身保留——它仍是 memory review 的入口。

**memory review 不动的部分**：
- 触发时机：每次压缩时
- 传参：`original_messages`（完整对话）——用户还在考虑是否改为 `messages_to_summarize`，本计划不改
- 模型：`_create_nudge_agent`（main 模型）——不改
- 逻辑：`_nudge_memory` 的 prompt + ainvoke——不改

### 2f：触发点接入 `aafter_agent`

**选项 A（推荐）**：在 `PlanContinuationEnforcer.aafter_agent` 末尾加一行调用：

```python
# agent/middlewares/todo_continuation/core.py
async def aafter_agent(self, state, runtime=None):
    # ... 现有逻辑不变 ...

    # 新增：plan 完成时 fork 抽取 agent
    from agent.middlewares.summarization.nudges import maybe_trigger_plan_extraction
    await maybe_trigger_plan_extraction(session_id)
```

**选项 B**：新建独立中间件 `PlanExtractionTrigger`，注册在 `aafter_agent` 链中。与 PlanContinuationEnforcer 解耦，但多一个中间件。

推荐 A——PlanContinuationEnforcer 已在 `aafter_agent` 检查 todo 状态，加一行调用最小改动。如果 PlanContinuationEnforcer 被 per-session 关闭，plan extraction 也不触发（合理——关掉续跑的用户大概也不需要经验抽取）。

### 2g：`_build_plan_context` 不改

仍然传 `get_todos_sync` 返回的全部 todo + `_read_subagent_runs` 返回的全部子 agent 记录 + plan 文件。**不过滤、不截断**——抽取单位是 plan（全貌），不拆分。

---

## 3. 完整行为矩阵

| 场景 | 触发时机 | agent 收到 | 模型 | hash 比较 | 抽取？ |
|---|---|---|---|---|---|
| 5 todo 全完成（短会话，无压缩） | `aafter_agent` | plan_context（5 todo + runs + plan 文件） | aux | 无 stored hash | ✅ 抽 |
| 5 todo 全完成（有压缩） | `aafter_agent`（不等压缩） | 同上 | aux | 无 stored hash | ✅ 抽 |
| 同一批 5 todo，下一轮 `aafter_agent` | `aafter_agent` | — | — | 相同 | ❌ 跳 |
| 加 3 新 todo → 8 全完成 | `aafter_agent` | plan_context（8 todo + runs + plan 文件） | aux | 不同（8≠5） | ✅ 抽（全貌） |
| completed todo 内容被改 | `aafter_agent` | plan_context（含改过的 todo） | aux | 不同（内容变了） | ✅ 重抽 |
| 抽取失败（LLM 超时） | — | — | — | hash 不存（ainvoke 失败） | 下次 `aafter_agent` 重试 |
| PlanContinuationEnforcer 被关 | — | — | — | — | ❌ 不触发（选项 A） |

---

## 4. token 成本对比

| 项 | 改前 | 改后 |
|---|---|---|
| plan extraction 触发时机 | 压缩时（可能几十轮后） | plan 完成当轮 `aafter_agent` |
| plan extraction 模型 | `build_main_llm()`（如 DeepSeek Chat） | `build_auxiliary_llm(temperature=0)`（如 Qwen 9B Q4 本地 / 便宜远程） |
| plan extraction 输入 | system_prompt(~4K) + 完整对话(~105K) + prompt(~5K) + plan_context(~10K) = **~124K** | system_prompt(~4K) + prompt(~5K) + plan_context(~10K) = **~19K** |
| plan extraction 输出 | ~2K | ~2K（相同） |
| plan extraction 成本 | ~$0.017/次（main 模型） | ~$0.0003/次或免费（aux 本地 / 极便宜远程） |
| 爆窗口风险 | 131K 差 7K | 不可能（19K ≪ 131K） |
| 短会话 | 不抽（压缩不触发） | 抽（`aafter_agent` 触发） |
| **总成本降幅** | — | **输入降 85%，模型降一级，总成本降 ~98%** |

---

## 5. 关键文件清单

| 文件 | 改动 |
|---|---|
| `agent/middlewares/summarization/nudges.py` | 新增 `maybe_trigger_plan_extraction()` + `_run_plan_extraction()` + `_create_plan_extraction_agent()`（aux 模型）+ `_plan_snapshot_hash()`；`_detect_todo_all_complete` 加 hash 检查；`_run_compression_nudges` 删 `if need_plan: _nudge_plan_extraction` 分支（memory review 调用不动）；`schedule_compression_nudges` 删 `need_plan` 逻辑（函数保留，仍是 memory review 入口） |
| `agent/middlewares/summarization/core.py` | `_fire_compression_nudges` 不改——memory review 的调用保持原样 |
| `agent/middlewares/summarization/compression.py` | 不改——`_fire_compression_nudges` 调用点不变 |
| `agent/middlewares/todo_continuation/core.py` | `aafter_agent` 末尾加 `maybe_trigger_plan_extraction(session_id)` |
| `runtime/session/state_keys.py` | 新增 `NUDGE_PLAN_SNAPSHOT_HASH` |

### 不改的

| 项 | 原因 |
|---|---|
| `_nudge_memory` | memory review 机制保持不变（触发时机、传参、模型、逻辑全不动） |
| `schedule_compression_nudges` 函数 | 保留——仍是 memory review 的入口，只删 `need_plan` 逻辑 |
| `_create_nudge_agent` | memory review 还在用它（main 模型）；plan extraction 用新的 `_create_plan_extraction_agent`（aux 模型） |
| `_build_plan_context` | 不过滤——抽取单位是 plan（全貌） |
| `_read_subagent_runs` | 不过滤——nudge 需要全部子 agent 记录理解全貌 |
| `_NudgeLimitTool` | 工具门控不变（plan extraction agent 复用它，只放行 `nudge: True` 的工具） |
| `_PLAN_EXTRACTION_PROMPT` | prompt 不改——收到的是同样的 plan_context |
| todo dict / todo store schema | 不加字段——hash 在 state register 层 |

---

## 6. 测试

| 测试 | 场景 | 验证点 |
|---|---|---|
| `test_extraction_triggers_on_plan_completion` | 5 todo 全完成 → `aafter_agent` | fork agent + 抽取 |
| `test_extraction_uses_aux_model` | mock `build_auxiliary_llm` | agent 用 aux 模型，非 main 模型 |
| `test_extraction_no_messages_passed` | mock `_create_plan_extraction_agent` | ainvoke 的 messages 只有 HumanMessage(prompt)，无对话历史 |
| `test_same_plan_skip` | 同一批 todo 下一轮 `aafter_agent` | hash 相同 → 不 fork |
| `test_new_todos_reextract` | 加 3 todo 全完成 | hash 不同 → fork（传全部 todo） |
| `test_content_changed_reextract` | completed todo 内容被改 | hash 不同 → fork |
| `test_extraction_failed_no_hash` | mock ainvoke 抛异常 | hash 不存 → 下次重试 |
| `test_short_session_extracts` | 5 todo 全完成，窗口未满（无压缩） | `aafter_agent` 触发 → 抽取 |
| `test_compression_no_longer_does_plan_extraction` | 压缩触发 | 只跑 memory review，不跑 plan extraction |
| `test_memory_review_unchanged` | 压缩触发 | memory review 仍收到 `original_messages`、用 main 模型、走 `_create_nudge_agent`（机制不变） |
| `test_backward_compat` | 旧 session 无 hash | `get(..., "")` → 不等 → 首次抽取 |
| `test_enforcer_disabled_no_extraction` | PlanContinuationEnforcer 被关 | `aafter_agent` 不跑 → 不触发抽取 |

---

## 7. 风险

| 风险 | 对策 |
|---|---|
| `aafter_agent` 每轮都检测 todo 状态 | `_detect_todo_all_complete` 是 SQLite 同步读 + hash 计算，成本极低（~1ms） |
| PlanContinuationEnforcer 被关 → extraction 不触发 | 合理——关续跑的用户大概率不需要经验抽取。如需独立，用选项 B 新建中间件 |
| `asyncio.create_task` 的 task 被 GC | 复用 `_COMPRESSION_NUDGE_TASKS` set 持有引用 |
| NUDGE lane 满（4 slot） | fire-and-forget 排队等 lane slot，不阻塞 turn |
| hash 碰撞 | SHA-256 截 16 hex（64 bit），碰撞概率可忽略 |
| 向后兼容 | 旧 session 无 hash → `get(..., "")` → 不等 → 首次抽取（与改前一致） |
| memory review 传参改动 | 不改——memory review 机制保持不变（仍传 `original_messages`、用 main 模型）。如后续要调整传参为 `messages_to_summarize`，另出独立计划 |
