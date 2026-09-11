# LT-7 压缩摘要与 TaskFlow 状态协调 — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §LT-7 (行 2422-2508)
> 决策: 摘要 prompt 注入 TaskFlow 状态 + 复用 LT-2 sync 查询
> 预估工时: 0.5 天

---

## 1. 问题

压缩生成摘要时不知道 TaskFlow 状态。可能摘要说"用户在调试某个文件"，但 TaskFlow 显示该步骤早已 DONE，当前在进行另一个步骤。摘要与任务状态脱节。

**当前状态：**

- `_create_summary()` / `_acreate_summary()`（`summarization.py:1361/1386`）使用 `_build_summary_prompt()` 构建摘要 prompt
- `_build_summary_prompt()`（行 1342）注入 `<conversation>` + `<prior-summary>` + `_SUMMARY_PROMPT_FIRST`/`_SUMMARY_PROMPT_UPDATE`
- `Summarization.__init__` 不接收 `session_id`（在压缩方法中传入）
- LT-2 提供了 `get_active_flows_sync()`（同步查询活跃 flow）
- 摘要 prompt 当前不包含任何 TaskFlow 状态

---

## 2. 设计决策

### 2.1 在摘要 prompt 中注入 TaskFlow 状态

在 `_build_summary_prompt()` 中，如果 `session_id` 可用，查询该会话的活跃 TaskFlow 状态，注入到摘要 prompt 中作为权威上下文。

### 2.2 关键决策

| 决策点         | 选择                                    | 理由                                           |
| -------------- | --------------------------------------- | ---------------------------------------------- |
| 查询方式       | 复用 LT-2 的 `get_active_flows_sync()`  | 同步函数，`_create_summary` 是同步的；无需异步 |
| 注入位置       | `_build_summary_prompt()` 中            | 集中管理 prompt 构建                           |
| 注入格式       | 简洁结构化（flow_id, status, 步骤进度） | 控制注入字符数（~300-500 chars）               |
| 无 TaskFlow 时 | 不注入                                  | 向后兼容                                       |
| 失败处理       | try/except 返回 ""                      | 不阻塞压缩                                     |

---

## 3. 涉及文件

| 文件                                 | 操作                                                                | 预估行数 |
| ------------------------------------ | ------------------------------------------------------------------- | -------- |
| `agent/middlewares/summarization.py` | 修改：`_build_summary_prompt` + 新增 `_get_taskflow_context_sync()` | ~40      |

无需新建文件或测试文件（可在现有 `test_summarization_comprehensive.py` 中追加测试）。

---

## 4. 详细设计

### 4.1 新增 _get_taskflow_context_sync()

```python
def _get_taskflow_context_sync(session_id: str) -> str:
    """获取当前会话的 TaskFlow 状态摘要，供压缩 prompt 使用（同步）。

    复用 LT-2 的 get_active_flows_sync() + creator_session_key 过滤。
    返回简洁结构化文本，~300-500 chars。
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite as taskflow_store
        from agent.tools.taskflow.tools._shared import (
            requester_session_key,
            step_status,
            steps_summary,
        )

        creator_key = requester_session_key(session_id)
        active_flows = taskflow_store.get_active_flows_sync()

        mine = [
            flow for flow in active_flows
            if (flow.get("state") or {}).get("creator_session_key") == creator_key
        ]
        if not mine:
            return ""

        lines = ["## Current TaskFlow State (authoritative)"]
        for flow in mine[:3]:  # max 3 flows
            state = flow.get("state") or {}
            steps = state.get("steps") or []
            counts = steps_summary(steps)
            total = len(steps)
            done = counts.get("done", 0)
            desc = state.get("description", "")[:60]

            lines.append(
                f"### {flow['flow_id']} ({flow['status']})"
            )
            lines.append(f"Desc: {desc}")
            lines.append(
                f"Progress: {done}/{total} done · "
                + " · ".join(f"{s}={counts.get(s, 0)}" for s in ("dispatched", "ready", "blocked"))
            )

            # 最近 2 个完成的步骤
            done_steps = [s for s in steps if step_status(s) == "done"]
            if done_steps:
                for s in done_steps[-2:]:
                    task_text = (s.get("task", "") or "")[:60]
                    lines.append(f"  ✓ {task_text}")

            # 前 2 个待处理步骤
            pending = [s for s in steps if step_status(s) not in ("done",)]
            if pending:
                for s in pending[:2]:
                    task_text = (s.get("task", "") or "")[:60]
                    icon = {"dispatched": "→", "ready": "○", "blocked": "⊘"}.get(step_status(s), "?")
                    lines.append(f"  {icon} {task_text}")

            if flow.get("wait"):
                lines.append(f"Waiting: {flow['wait'].get('reason', 'unknown')}")

        return "\n".join(lines)
    except Exception:
        return ""
```

### 4.2 修改 _build_summary_prompt()

```python
def _build_summary_prompt(
    self,
    messages_text: str,
    previous_summary: str | None,
    session_id: str = "",
) -> str:
    conversation = (
        f"Here is the conversation so far:\n\n<conversation>\n{messages_text}\n</conversation>"
    )
    parts = [conversation]

    if previous_summary:
        parts.append(
            f"Here is the summary of the conversation before the <conversation> above:\n\n"
            f"<prior-summary>\n{previous_summary}\n</prior-summary>"
        )
        parts.append(_SUMMARY_PROMPT_UPDATE)
    else:
        parts.append(_SUMMARY_PROMPT_FIRST)

    # === 新增：注入 TaskFlow 状态 (LT-7) ===
    if session_id:
        taskflow_ctx = _get_taskflow_context_sync(session_id)
        if taskflow_ctx:
            parts.append(
                f"\n{taskflow_ctx}\n"
            )

    return "\n\n".join(parts)
```

### 4.3 修改 _create_summary() / _acreate_summary()

将 `session_id` 传递给 `_build_summary_prompt()`：

```python
def _create_summary(self, messages_to_summarize: list[AnyMessage], session_id: str = "") -> str:
    # ... 现有逻辑 ...
    prompt = self._build_summary_prompt(serialized, previous_summary, session_id=session_id)
    # ... 现有逻辑 ...


async def _acreate_summary(self, messages_to_summarize: list[AnyMessage], session_id: str = "") -> str:
    # ... 现有逻辑 ...
    prompt = self._build_summary_prompt(serialized, previous_summary, session_id=session_id)
    # ... 现有逻辑 ...
```

### 4.4 修改调用处

在 `_apply_compression()` 和 `_aapply_compression()` 中，将 `session_id` 传入 `_create_summary` / `_acreate_summary`：

```python
# _apply_compression (sync, 行 1623):
summary_text = self._create_summary(messages_to_summarize, session_id=session_id)

# _aapply_compression (async, 行 1697):
summary_text = await self._acreate_summary(messages_to_summarize, session_id=session_id)
```

---

## 5. 实施顺序

```
Step 1: agent/middlewares/summarization.py — 新增 _get_taskflow_context_sync() 模块级函数
Step 2: agent/middlewares/summarization.py — 修改 _build_summary_prompt() 接收 session_id + 注入 TaskFlow 状态
Step 3: agent/middlewares/summarization.py — 修改 _create_summary() / _acreate_summary() 传递 session_id
Step 4: agent/middlewares/summarization.py — 修改 _apply_compression / _aapply_compression 调用处传入 session_id
Step 5: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

在现有 `tests/agent/middlewares/test_summarization_comprehensive.py` 中追加：

| 测试                                         | 说明                                                   |
| -------------------------------------------- | ------------------------------------------------------ |
| `test_summary_prompt_includes_taskflow`      | 有活跃 flow → prompt 包含 "Current TaskFlow State"     |
| `test_summary_prompt_no_taskflow`            | 无活跃 flow → prompt 不包含 TaskFlow 块                |
| `test_summary_prompt_taskflow_step_progress` | prompt 包含 "done=N" 和步骤列表                        |
| `test_summary_prompt_taskflow_failure_safe`  | taskflow store 异常 → prompt 正常生成（不含 TaskFlow） |
| `test_summary_first_vs_update`               | 首次摘要和更新摘要都注入 TaskFlow 状态                 |

### 运行验证

```bash
python -m pytest tests/agent/middlewares/test_summarization_comprehensive.py -v
python -m pytest tests/agent/middlewares/test_e2e_summarization.py -v
ruff check agent/middlewares/summarization.py
pyright agent/middlewares/summarization.py
```

---

## 7. 数据流

```
压缩触发:
  → _aapply_compression(request, session_id="abc")
  → _determine_cutoff(messages) → cutoff=20
  → messages_to_summarize = messages[:20]
  → _acreate_summary(messages_to_summarize, session_id="abc")
    → _build_summary_prompt(serialized, prior_summary, session_id="abc")
      → _get_taskflow_context_sync("abc")
        → get_active_flows_sync() → 找到 flow "deploy-v2" (creator=agent:main:session:abc)
        → 返回:
          "## Current TaskFlow State (authoritative)
          ### deploy-v2 (running)
          Desc: deploy to staging
          Progress: 2/5 done · dispatched=1 · ready=1 · blocked=1
            ✓ build project
            ✓ run unit tests
            → deploy to staging
            ○ run smoke tests"
      → 注入到摘要 prompt 中
    → LLM 生成摘要时知道当前任务状态
    → 摘要准确反映 "deploy-v2 进行到部署阶段"
```

---

## 8. 与其他方案的关系

| 方案     | 关系                                                                                       |
| -------- | ------------------------------------------------------------------------------------------ |
| **LT-2** | 依赖。使用 LT-2 的 `get_active_flows_sync()` + `creator_session_key` 过滤。                |
| **P0-1** | 协作。P0-1 在压缩前 flush 事实；LT-7 在压缩时注入 TaskFlow 状态。互补。                    |
| **LT-3** | 协作。LT-3 从分层记忆重建基准线；LT-7 从 TaskFlow 注入状态。两者都是为摘要提供权威上下文。 |
| **P0-4** | 独立。P0-4 优化非 LLM 策略中的工具输出裁剪；LT-7 优化 LLM 摘要 prompt。                    |

---

## 9. 风险与缓解

| 风险                                       | 缓解                                                     |
| ------------------------------------------ | -------------------------------------------------------- |
| TaskFlow 查询失败                          | try/except 返回 ""；摘要正常生成（不含 TaskFlow 状态）   |
| 注入过多字符撑大摘要 prompt                | 最多 3 个 flow，每个 ~100-150 chars，总计 ~300-450 chars |
| LT-2 未实现时 get_active_flows_sync 不存在 | try/except 兜底；import 失败时跳过                       |
| _build_summary_prompt 签名变更             | `session_id` 默认为 ""，向后兼容（不传则不注入）         |
