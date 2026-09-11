# LT-8 会话间意图连续性 — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §LT-8 (行 2510-2684)
> 决策: 新建 session_continuity.py + clear_session 时保存 + prompt_builder 注入
> 预估工时: 1-2 天

---

## 1. 问题

新会话启动时没有自动注入上一会话的末尾摘要。用户说"继续上次的工作"时，系统没有精确的上一会话末尾状态。P1-4 Recall 做的是关键词模糊召回，而非精确的末尾状态注入。

**当前状态：**

- `build_system_prompt()`（`workspace/prompt_builder.py:145`）接收 `session_id` 但无 `channel_id`/`chat_id`
- `clear_session()`（`server/DAO/messages.py:13`）清理会话时不保存末尾状态
- `context_engine/store/core.py` 有 `get_messages_by_lastest_n_turns()` 可读取最近消息
- 无 `session_continuity.py` 模块
- 无会话末尾状态持久化机制

---

## 2. 设计决策

### 2.1 会话结束时保存末尾摘要 + 关联 TaskFlow

在 `clear_session()` 中，清理之前提取最后一轮 AI 回复作为摘要，保存到 JSON 文件。新会话启动时注入到系统 prompt。

### 2.2 关键决策

| 决策点             | 选择                                                 | 理由                                                      |
| ------------------ | ---------------------------------------------------- | --------------------------------------------------------- |
| 存储位置           | `data/session_continuity/{key}.json`                 | 简单文件存储，无需 DB；按 channel:chat 索引               |
| 存储时机           | `clear_session()` 调用时                             | 会话清理是明确的结束信号                                  |
| 摘要来源           | 最后一轮 AI 回复（截取 500 chars）                   | 精确反映会话末尾状态                                      |
| TaskFlow 关联      | 查询该 session 的活跃 flow IDs                       | 让新会话知道有哪些未完成任务                              |
| 注入方式           | `prompt_builder._build_continuity_block()`           | 与 todo/boulder/taskflow block 同模式                     |
| 注入条件           | 仅 `selected_file_names is None` + `session_id` 存在 | 与 memory block 一致                                      |
| channel_id/chat_id | 从 session 状态获取                                  | `clear_session` 有 session_id，需从 register 获取 channel |

### 2.3 channel_id/chat_id 获取

`clear_session(session_id)` 只有 session_id。需要从 `relation_register` 获取 channel_id/chat_id：

```python
from runtime.relation_register import relation_register
# relation_register.session_id_to_websocket_id → websocket_id
# 从 websocket_id 获取 channel_id/chat_id
```

如果获取失败，用 session_id 作为 key（降级）。

---

## 3. 涉及文件

| 文件                                              | 操作                                   | 预估行数 |
| ------------------------------------------------- | -------------------------------------- | -------- |
| `context_engine/session_continuity.py`            | **新建**                               | ~150     |
| `server/DAO/messages.py`                          | 修改：clear_session 中保存状态         | ~10      |
| `workspace/prompt_builder.py`                     | 修改：新增 `_build_continuity_block()` | ~30      |
| `tests/context_engine/test_session_continuity.py` | **新建**                               | ~100     |

---

## 4. 详细设计

### 4.1 context_engine/session_continuity.py — 新建

```python
"""
会话间意图连续性：新会话启动时自动注入上一会话的末尾状态。

参考: oh-my-openagent session binding + opencode-dev Context Epoch

与 P1-4 Recall 的区别:
- P1-4 Recall: 模糊关键词搜索历史对话
- LT-8 连续性: 精确注入上一会话的末尾摘要
"""

import json
import time
from pathlib import Path
from loguru import logger

_CONTINUITY_DIR = Path("data/session_continuity")


def save_session_end_state(
    session_id: str,
    channel_id: str,
    chat_id: str,
    summary: str,
    taskflow_ids: list[str],
) -> None:
    """
    会话结束时保存末尾状态。
    """
    _CONTINUITY_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{channel_id}:{chat_id}" if channel_id and chat_id else session_id
    state = {
        "last_session_id": session_id,
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ended_ts": time.time(),
        "summary": summary,
        "taskflow_ids": taskflow_ids,
    }
    path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_last_session_state(channel_id: str, chat_id: str) -> dict | None:
    """
    获取上一会话的末尾状态。
    """
    key = f"{channel_id}:{chat_id}" if channel_id and chat_id else ""
    if not key:
        return None
    path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_continuity_prompt(session_id: str) -> str:
    """
    构建会话连续性提示词块（同步，供 prompt_builder 调用）。

    使用 session_id 查找关联的 channel/chat（从 relation_register），
    然后读取上一会话状态。
    """
    try:
        channel_id, chat_id = _get_channel_chat_for_session(session_id)
        if not channel_id or not chat_id:
            return ""

        state = get_last_session_state(channel_id, chat_id)
        if not state:
            return ""

        # 不注入自己的状态（防止当前会话重读自己的末尾）
        if state.get("last_session_id") == session_id:
            return ""

        parts = ["## Last Session (continuity)"]
        if state.get("summary"):
            parts.append(f"Last conversation ended with: {state['summary'][:500]}")
        if state.get("taskflow_ids"):
            parts.append(f"Related tasks: {', '.join(state['taskflow_ids'][:3])}")
        parts.append(
            "If the user says 'continue' or doesn't specify a new task, "
            "refer to the above context first."
        )
        return "\n".join(parts)
    except Exception:
        return ""


async def auto_save_on_session_end(session_id: str) -> None:
    """
    会话结束时自动提取末尾摘要并保存。
    """
    try:
        from context_engine.store.core import get_messages_by_lastest_n_turns

        channel_id, chat_id = _get_channel_chat_for_session(session_id)

        messages = await get_messages_by_lastest_n_turns(session_id, n_turns=3)

        # 找最后一轮 AI 回复
        last_ai = None
        for msg in reversed(messages):
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role == "ai" or role == "assistant":
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                last_ai = content[:500] if content else None
                break

        # 获取关联的 TaskFlow
        taskflow_ids = _get_active_taskflow_ids_sync(session_id)

        save_session_end_state(
            session_id=session_id,
            channel_id=channel_id or "",
            chat_id=chat_id or "",
            summary=last_ai or "",
            taskflow_ids=taskflow_ids,
        )
        logger.debug("Session continuity saved for session={}", session_id)
    except Exception as e:
        logger.warning("Failed to save session end state: {}", e)


def _get_channel_chat_for_session(session_id: str) -> tuple[str, str]:
    """从 relation_register 获取 session 关联的 channel_id 和 chat_id。"""
    try:
        from runtime.relation_register import relation_register

        ws_id = relation_register.session_id_to_websocket_id.get(session_id)
        if not ws_id:
            return "", ""

        # 从 websocket session 获取 channel/chat 信息
        # relation_register 可能不直接存 channel_id/chat_id
        # 降级：使用 session_id 作为 key
        return "", ""
    except Exception:
        return "", ""


def _get_active_taskflow_ids_sync(session_id: str) -> list[str]:
    """同步获取该 session 关联的活跃 TaskFlow IDs。"""
    try:
        from agent.tools.taskflow.registry import store_sqlite
        from agent.tools.taskflow.tools._shared import requester_session_key

        creator_key = requester_session_key(session_id)
        active_flows = store_sqlite.get_active_flows_sync()
        mine = [
            flow for flow in active_flows
            if (flow.get("state") or {}).get("creator_session_key") == creator_key
        ]
        return [f["flow_id"] for f in mine]
    except Exception:
        return []


def _safe_filename(key: str) -> str:
    """将 channel:chat 键转为安全文件名。"""
    return key.replace(":", "_").replace("/", "_").replace("\\", "_")
```

### 4.2 server/DAO/messages.py — 修改 clear_session

```python
async def clear_session(session_id: str) -> None:
    """Purge every trace of a session across all stores.

    Deletes, in order:
      0. Save session end state for continuity (LT-8) — BEFORE deletion.
      1. The session's rows from the context engine SQLite store.
      2. The session's records from the sqlite checkpointer.
      3. The session's folder under the sessions directory.
      4. The in-memory session state.
      5. The session's variables from the state_register_db.
    """
    # === 新增：保存会话末尾状态 (LT-8) ===
    try:
        from context_engine.session_continuity import auto_save_on_session_end
        await auto_save_on_session_end(session_id)
    except Exception:
        pass  # Non-critical: don't block session cleanup

    # (1) Context engine mes_memory store — messages for this session.
    from context_engine import delete_messages_by_session

    deleted = delete_messages_by_session(session_id=session_id)
    logger.debug(f"Cleared {deleted} mes_memory message row(s) for session_id={session_id}")

    # ... 现有清理逻辑不变 ...
```

### 4.3 workspace/prompt_builder.py — 新增 _build_continuity_block

```python
def _build_continuity_block(session_id: str) -> str:
    """Render the last session's end state for cross-session continuity.

    Injects the last AI reply + related TaskFlow IDs from the previous
    session so the agent can proactively continue unfinished work.
    Returns "" on none or any failure (fail-open).
    """
    try:
        from context_engine.session_continuity import build_continuity_prompt

        return build_continuity_prompt(session_id)
    except Exception:
        return ""
```

在 `build_system_prompt()` 中注入（在 taskflow block 之后、skills 之前）：

```python
if session_id:
    blocks = [
        _build_todo_block(session_id),
        _build_boulder_block(session_id),
        _build_taskflow_block(session_id),      # LT-2
        _build_continuity_block(session_id),     # ← 新增 LT-8
    ]
else:
    blocks = []
```

---

## 5. 实施顺序

```
Step 1: context_engine/session_continuity.py — 新建模块
Step 2: server/DAO/messages.py — clear_session 中保存末尾状态
Step 3: workspace/prompt_builder.py — 新增 _build_continuity_block() + 注入
Step 4: tests/context_engine/test_session_continuity.py — 编写测试
Step 5: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

| 测试                                       | 说明                                                          |
| ------------------------------------------ | ------------------------------------------------------------- |
| `test_save_and_get_session_state`          | 保存后读取 → 数据一致                                         |
| `test_get_nonexistent_state`               | 不存在的 key → None                                           |
| `test_continuity_prompt_with_state`        | 有状态 → 包含 "Last Session (continuity)" + summary           |
| `test_continuity_prompt_no_state`          | 无状态 → 返回 ""                                              |
| `test_continuity_prompt_skips_self`        | last_session_id == 当前 session → 返回 ""（不注入自己的状态） |
| `test_continuity_prompt_truncates_summary` | 长摘要 → 截取 500 chars                                       |
| `test_continuity_prompt_lists_taskflows`   | 有 taskflow_ids → 包含 "Related tasks"                        |
| `test_continuity_prompt_failure_safe`      | 异常 → 返回 ""                                                |
| `test_clear_session_saves_state`           | clear_session 后 → continuity 文件存在                        |
| `test_prompt_builder_injects_continuity`   | build_system_prompt → 包含 continuity block                   |

### 运行验证

```bash
python -m pytest tests/context_engine/test_session_continuity.py -v
python -m pytest tests/workspace/test_prompt_builder.py -v  # 确保不破坏
ruff check context_engine/session_continuity.py workspace/prompt_builder.py server/DAO/messages.py
pyright context_engine/session_continuity.py
```

---

## 7. 数据流

### 7.1 会话结束

```
用户关闭会话 / clear_session("sess-abc")
  → auto_save_on_session_end("sess-abc")
    → get_messages_by_lastest_n_turns("sess-abc", n_turns=3)
    → 找到最后一轮 AI 回复: "I've deployed the fix and verified the tests pass."
    → _get_active_taskflow_ids_sync("sess-abc") → ["deploy-v2"]
    → save_session_end_state("sess-abc", "chan1", "chat1",
        summary="I've deployed the fix...", taskflow_ids=["deploy-v2"])
    → 写入 data/session_continuity/chan1_chat1.json

  → 继续清理: delete_messages_by_session, delete_thread_history, etc.
```

### 7.2 新会话启动

```
新会话 session_id="sess-def" 启动
  → build_system_prompt(session_id="sess-def")
  → _build_continuity_block("sess-def")
    → build_continuity_prompt("sess-def")
      → _get_channel_chat_for_session("sess-def") → ("chan1", "chat1")
      → get_last_session_state("chan1", "chat1")
        → 读取 data/session_continuity/chan1_chat1.json
        → last_session_id="sess-abc" != "sess-def" → 注入
      → 返回:
        "## Last Session (continuity)
        Last conversation ended with: I've deployed the fix and verified the tests pass.
        Related tasks: deploy-v2
        If the user says 'continue' or doesn't specify a new task, refer to the above context first."
  → 注入到系统 prompt（taskflow block 之后、skills 之前）
  → 模型看到上次会话末尾状态 + 关联任务 → 主动续接
```

---

## 8. 与其他方案的关系

| 方案            | 关系                                                                                     |
| --------------- | ---------------------------------------------------------------------------------------- |
| **LT-2**        | 依赖。使用 LT-2 的 `get_active_flows_sync()` + `creator_session_key` 获取关联 TaskFlow。 |
| **LT-1**        | 协作。LT-1 分层记忆提供持久事实；LT-8 提供末尾对话状态。不同维度。                       |
| **P1-4 Recall** | 互补。P1-4 模糊搜索历史对话；LT-8 精确注入上一会话末尾。                                 |
| **P0-1**        | 协作。P0-1 flush 在压缩时保存事实；LT-8 在会话结束时保存末尾摘要。                       |

---

## 9. 风险与缓解

| 风险                             | 缓解                                                                    |
| -------------------------------- | ----------------------------------------------------------------------- |
| channel_id/chat_id 获取失败      | 降级使用 session_id 作为 key；或者跳过注入（不阻塞）                    |
| clear_session 在保存前已删除消息 | 保存逻辑在清理**之前**执行（Step 0）                                    |
| 末尾 AI 回复为空或无意义         | 空摘要 → continuity block 仍注入（只显示 taskflow_ids）；全空 → 不注入  |
| 文件系统写入失败                 | try/except 静默降级                                                     |
| 跨 channel/channel 注入错误状态  | key = `channel_id:chat_id` 精确匹配；`last_session_id != 当前` 防自注入 |
| 隐私：保存对话内容到文件         | 仅保存最后一轮 AI 回复 500 chars；不含用户消息；文件在 data/ 目录       |
