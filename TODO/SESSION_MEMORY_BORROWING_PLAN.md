# 会话级内存架构借鉴方案 — Session Memory Borrowing Plan

> 参考来源: opencode-dev · oh-my-openagent-dev · hermes-agent-main · openclaw
> 配套文件: session-memory-comparison.md
> 日期: 2026-09-10
> **状态 (2026-09-12)**: LT-1/LT-2/P0-1/P0-4/LT-7/LT-8 以及 #3/#4/#6/#11 已实现（参见 `docs/long-running-tasks/` 和 `agent/tools/taskflow/`）。本文档保留作为设计参考。

## 目录

1. [P0-1 预压缩 Memory Flush（来源：openclaw）](#p0-1-预压缩-memory-flush来源openclaw)
2. [P0-2 压缩失败冷却持久化（来源：hermes-agent）](#p0-2-压缩失败冷却持久化来源hermes-agent)
3. [P0-3 压缩锁防并发分裂（来源：hermes-agent）](#p0-3-压缩锁防并发分裂来源hermes-agent)
4. [P0-4 工具输出一行摘要替代（来源：hermes-agent）](#p0-4-工具输出一行摘要替代来源hermes-agent)
5. [P1-1 压缩检查点回溯（来源：openclaw）](#p1-1-压缩检查点回溯来源openclaw)
6. [P1-2 增量幂等持久化标记（来源：hermes-agent）](#p1-2-增量幂等持久化标记来源hermes-agent)
7. [P1-3 活跃上下文投影 + context_eligible（来源：openclaw）](#p1-3-活跃上下文投影--context_eligible来源openclaw)
8. [P1-4 Recall 自动跨会话记忆召回（来源：oh-my-openagent）](#p1-4-recall-自动跨会话记忆召回来源oh-my-openagent)
9. [P1-5 转录树 + 消息分支（来源：openclaw）](#p1-5-转录树--消息分支来源openclaw)
10. [P2-1 事件溯源迁移（来源：opencode-dev）](#p2-1-事件溯源迁移来源opencode-dev)
11. [P2-2 Context Epoch 系统上下文快照（来源：opencode-dev）](#p2-2-context-epoch-系统上下文快照来源opencode-dev)
12. [P2-3 双水位游标 Facts 提取（来源：oh-my-openagent）](#p2-3-双水位游标-facts-提取来源oh-my-openagent)
13. [P2-4 steer/queue 双投递模式（来源：opencode-dev）](#p2-4-steerqueue-双投递模式来源opencode-dev)
14. [P2-5 向量嵌入语义搜索（来源：openclaw）](#p2-5-向量嵌入语义搜索来源openclaw)

---

## P0-1 预压缩 Memory Flush（来源：openclaw）

### 现状

sherry-agent 的 Summarization 中间件在压缩时直接调用辅助 LLM 生成摘要，使用主模型（可能很贵），且压缩与记忆提取是分离的两个过程。被压缩丢弃的消息中的关键信息（用户意图、关键决策、文件变更）永久丢失。

### 目标

在压缩前用独立便宜模型扫描即将被丢弃的消息，将重要上下文持久化到 `MEMORY.md`。压缩时不再丢失关键信息，即使摘要质量差也能从 MEMORY.md 恢复。

### 涉及文件

| 文件                                       | 操作                                              |
| ------------------------------------------ | ------------------------------------------------- |
| `agent/middlewares/summarization.py`       | 修改 `_apply_compression()` 入口，插入 flush 步骤 |
| `agent/tools/memory.py` (新增方法)         | 新增 `flush_context_to_memory()` 方法             |
| `config/num.py`                            | 新增 Memory Flush 相关阈值                        |
| `agent/middlewares/memory_flush.py` (新建) | Memory Flush 逻辑                                 |

### 详细设计

#### 10.1.1 新增配置项

```python
# config/num.py — 新增

# Memory Flush
MEMORY_FLUSH_ENABLED = True
MEMORY_FLUSH_MODEL = os.getenv("MEMORY_FLUSH_MODEL", "")  # 空=不指定，用默认便宜模型
MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS = 8_000   # 被丢弃消息超过此 token 数才触发 flush
MEMORY_FLUSH_FORCE_FLUSH_CHARS = 50_000      # 被丢弃消息超过此字符数强制 flush（无视 token 估算）
MEMORY_FLUSH_OUTPUT_MAX_TOKENS = 2_048       # flush 输出 token 上限
MEMORY_FLUSH_TIMEOUT_SECONDS = 30            # flush 超时（超时则跳过，不阻塞压缩）
```

#### 10.1.2 新建 memory_flush.py

```python
# agent/middlewares/memory_flush.py — 新建

"""
预压缩 Memory Flush：在 LLM 摘要压缩前，用便宜模型从即将被丢弃的消息中
提取关键事实写入 MEMORY.md，确保压缩后仍可通过记忆文件恢复重要上下文。

参考: openclaw compaction.memoryFlush 机制
"""

import logging
from typing import TYPE_CHECKING

from config.num import (
    MEMORY_FLUSH_ENABLED,
    MEMORY_FLUSH_MODEL,
    MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS,
    MEMORY_FLUSH_FORCE_FLUSH_CHARS,
    MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
    MEMORY_FLUSH_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

_FLUSH_PROMPT = """\
你是一个记忆管理助手。以下是对话中被压缩丢弃的历史消息。
请从中提取**持久有用的事实**，写入持久记忆文件。

提取规则:
1. 只提取跨会话有用的事实（用户偏好、项目约定、关键决策、环境事实）
2. 不提取临时任务进度（那些由摘要负责）
3. 每条事实一行，简洁明了
4. 如果没有值得提取的内容，输出空行

输出格式（每行一条，用 § 分隔条目）:
§ <类别>: <事实描述>

类别包括: 环境 | 项目 | 工具 | 用户 | 决策

即将被丢弃的对话:
{discarded_text}
"""


def should_flush(discarded_messages: list, estimated_tokens: int) -> bool:
    """判断是否需要执行 Memory Flush"""
    if not MEMORY_FLUSH_ENABLED:
        return False
    total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
    if total_chars >= MEMORY_FLUSH_FORCE_FLUSH_CHARS:
        return True
    if estimated_tokens >= MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS:
        return True
    return False


async def run_memory_flush(
    discarded_messages: list,
    estimated_tokens: int,
    memory_store,  # agent.tools.memory.MemoryStore
    llm_factory,   # models/llm_factory.py 的工厂函数
) -> bool:
    """
    执行 Memory Flush，将提取的事实写入 MEMORY.md。
    返回 True 表示成功，False 表示跳过或失败。
    """
    if not should_flush(discarded_messages, estimated_tokens):
        return False

    discarded_text = "\n\n".join(
        f"[{m.type if hasattr(m, 'type') else 'unknown'}] {_msg_to_text(m)}"
        for m in discarded_messages
    )

    if not discarded_text.strip():
        return False

    prompt = _FLUSH_PROMPT.format(discarded_text=discarded_text)

    try:
        # 用便宜模型
        llm = llm_factory.get_llm(
            model=MEMORY_FLUSH_MODEL or None,
            max_tokens=MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
            timeout=MEMORY_FLUSH_TIMEOUT_SECONDS,
        )
        response = await llm.ainvoke(prompt)
        extracted = response.content.strip() if hasattr(response, "content") else str(response).strip()

        if not extracted:
            logger.info("Memory Flush: 无可提取事实")
            return False

        # 写入 MEMORY.md（追加，不覆盖）
        memory_store.append_entries(extracted)
        logger.info("Memory Flush: 已写入 %d 字符到 MEMORY.md", len(extracted))
        return True

    except Exception as e:
        logger.warning("Memory Flush 失败（不阻塞压缩）: %s", e)
        return False


def _msg_to_text(msg) -> str:
    """将消息对象转为文本"""
    if isinstance(msg, str):
        return msg
    if hasattr(msg, "content"):
        if isinstance(msg.content, str):
            return msg.content
        if isinstance(msg.content, list):
            return " ".join(
                block.get("text", "") for block in msg.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return str(msg)
```

#### 10.1.3 修改 summarization.py

```python
# agent/middlewares/summarization.py — _apply_compression() 入口修改

# 现有代码:
# async def _apply_compression(self, messages, ...):
#     ...
#     # 非LLM策略
#     await self._run_non_llm_strategies(messages, ...)
#     # LLM摘要
#     ...

# 修改为:
async def _apply_compression(self, messages, ...):
    ...

    # === 新增：预压缩 Memory Flush ===
    from agent.middlewares.memory_flush import run_memory_flush
    discarded = self._determine_cutoff(messages, budget)  # 被丢弃的消息
    est_tokens = self._estimate_tokens(discarded)
    await run_memory_flush(
        discarded_messages=discarded,
        estimated_tokens=est_tokens,
        memory_store=self._memory_store,     # 需在 __init__ 中注入
        llm_factory=self._llm_factory,       # 需在 __init__ 中注入
    )

    # 非LLM策略
    await self._run_non_llm_strategies(messages, ...)
    # LLM摘要
    ...
```

#### 10.1.4 修改 MemoryStore 增加 append_entries

```python
# agent/tools/memory.py — MemoryStore 类新增方法

def append_entries(self, new_entries: str) -> None:
    """
    追加条目到 MEMORY.md（Memory Flush 用）。
    条目以 § 分隔，自动去重。
    """
    with self._file_lock:
        existing = self._read_raw() or ""
        # 简单去重：检查每条新条目是否已存在
        existing_lines = set(existing.split("§"))
        new_lines = [
            line.strip() for line in new_entries.split("§")
            if line.strip() and line.strip() not in existing_lines
        ]
        if not new_lines:
            return
        combined = existing.rstrip() + " § " + " § ".join(new_lines)
        # 字符限制检查
        if len(combined) > MEMORY_MAX_CHARS:
            # 截断最旧的条目
            all_entries = [e.strip() for e in combined.split("§") if e.strip()]
            while len(" § ".join(all_entries)) > MEMORY_MAX_CHARS and len(all_entries) > 1:
                all_entries.pop(0)
            combined = " § ".join(all_entries)
        self._write_raw(combined)
        self._refresh_entries()  # 刷新内存状态
```

### 实施顺序

1. `config/num.py` 新增配置项
2. `agent/middlewares/memory_flush.py` 新建模块
3. `agent/tools/memory.py` 新增 `append_entries()` 方法
4. `agent/middlewares/summarization.py` 在 `_apply_compression()` 入口插入 flush 调用
5. 在 Summarization 中间件的 `__init__` 中注入 `memory_store` 和 `llm_factory`
6. 测试：触发压缩，验证 MEMORY.md 被更新

### 测试计划

- [ ] 单元测试 `test_memory_flush.py`：`should_flush()` 边界条件
- [ ] 单元测试：`append_entries()` 去重和截断逻辑
- [ ] 集成测试：触发压缩，验证 MEMORY.md 包含 flush 提取的条目
- [ ] 集成测试：flush 超时不阻塞压缩流程
- [ ] 集成测试：flush 模型不可用时优雅降级

---

## P0-2 压缩失败冷却持久化（来源：hermes-agent）

### 现状

sherry-agent 的压缩冷却是内存态（`StateRegisterMeM` 中的 `_tick_cooldown()`），重启后冷却丢失，可能导致重启后立即重试之前失败的压缩。

### 目标

将压缩失败冷却时间戳持久化到 SQLite，重启后仍生效，防止无限失败循环。

### 涉及文件

| 文件                                 | 操作                                   |
| ------------------------------------ | -------------------------------------- |
| `runtime/state_register.py`          | `StateRegisterDB` 新增压缩冷却读写方法 |
| `agent/middlewares/summarization.py` | `_tick_cooldown()` 同时写内存和 DB     |
| `context_engine/store/db.py`         | `state_register.db` schema 迁移        |

### 详细设计

#### 10.2.1 StateRegisterDB 新增方法

```python
# runtime/state_register.py — StateRegisterDB 类新增

_COMPRESSION_COOLDOWN_KEY = "compression_cooldown_until"

def set_compression_cooldown(self, session_id: str, cooldown_until_ts: float) -> None:
    """设置压缩冷却到期时间戳（Unix秒）"""
    self._execute(
        "INSERT INTO session_states (session_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id, key) DO UPDATE SET value = excluded.value",
        (session_id, _COMPRESSION_COOLDOWN_KEY, str(cooldown_until_ts)),
    )

def get_compression_cooldown(self, session_id: str) -> float | None:
    """获取压缩冷却到期时间戳，None 表示不在冷却中"""
    row = self._fetchone(
        "SELECT value FROM session_states WHERE session_id = ? AND key = ?",
        (session_id, _COMPRESSION_COOLDOWN_KEY),
    )
    if not row:
        return None
    ts = float(row[0])
    if ts < time.time():
        # 已过期，清理
        self._execute(
            "DELETE FROM session_states WHERE session_id = ? AND key = ?",
            (session_id, _COMPRESSION_COOLDOWN_KEY),
        )
        return None
    return ts

def clear_compression_cooldown(self, session_id: str) -> None:
    """清除压缩冷却（压缩成功后调用）"""
    self._execute(
        "DELETE FROM session_states WHERE session_id = ? AND key = ?",
        (session_id, _COMPRESSION_COOLDOWN_KEY),
    )
```

#### 10.2.2 修改 summarization.py

```python
# agent/middlewares/summarization.py — 修改 _tick_cooldown()

def _tick_cooldown(self, session_id: str, success: bool) -> None:
    """
    压缩后更新冷却状态。
    success=True: 清除冷却
    success=False: 设置 N 回合冷却（内存+DB双写）
    """
    if success:
        # 清除冷却
        self._state_register_mem.set_state(session_id, "_comp_cooldown_rounds", 0)
        self._state_register_db.clear_compression_cooldown(session_id)
    else:
        # 设置冷却
        rounds = COMPACTION_COOLDOWN_ROUNDS
        self._state_register_mem.set_state(session_id, "_comp_cooldown_rounds", rounds)
        # DB持久化：冷却到期时间 = 当前时间 + (rounds × 估计每回合秒数)
        cooldown_until = time.time() + (rounds * 60)  # 粗估每回合60秒
        self._state_register_db.set_compression_cooldown(session_id, cooldown_until)

def _is_in_cooldown(self, session_id: str) -> bool:
    """检查是否在压缩冷却中（内存+DB双重检查）"""
    # 内存检查
    mem_rounds = self._state_register_mem.get_state(session_id, "_comp_cooldown_rounds", 0)
    if mem_rounds > 0:
        return True
    # DB检查（重启后内存丢失但DB仍在）
    db_cooldown = self._state_register_db.get_compression_cooldown(session_id)
    if db_cooldown is not None:
        # 恢复内存冷却
        self._state_register_mem.set_state(session_id, "_comp_cooldown_rounds", 1)
        return True
    return False
```

#### 10.2.3 修改 T1/T2 触发点

```python
# agent/middlewares/summarization.py — T1 preflight 和 T2 wrap_model_call

# 在触发压缩前检查冷却
if self._is_in_cooldown(session_id):
    logger.debug("T1: 跳过压缩，冷却中")
    return ROUTE_FITS  # 跳过压缩
```

### 实施顺序

1. `context_engine/store/db.py` 确认 `state_register.db` 的 `session_states` 表已支持 upsert（已有 schema）
2. `runtime/state_register.py` 新增三个方法
3. `agent/middlewares/summarization.py` 修改 `_tick_cooldown()` 和触发检查
4. 测试

### 测试计划

- [ ] 单元测试：`set/get/clear_compression_cooldown()` 读写正确
- [ ] 单元测试：过期冷却自动清理
- [ ] 集成测试：压缩失败后重启，冷却仍然生效
- [ ] 集成测试：冷却到期后可正常触发压缩

---

## P0-3 压缩锁防并发分裂（来源：hermes-agent）

### 现状

sherry-agent 靠 `TurnRunner` 的 FIFO 队列保证同会话串行，但跨会话恢复（如多实例部署、并发 API 请求）时无保护，可能导致两个实例同时压缩同一会话，造成状态分裂。

### 目标

基于 SQLite 的原子压缩锁，防止并发压缩导致会话状态分裂。

### 涉及文件

| 文件                                          | 操作                                        |
| --------------------------------------------- | ------------------------------------------- |
| `context_engine/store/db.py`                  | `mes_memory.db` 新增 `compression_locks` 表 |
| `agent/middlewares/compaction_lock.py` (新建) | 压缩锁逻辑                                  |
| `agent/middlewares/summarization.py`          | `_apply_compression()` 入口加锁             |

### 详细设计

#### 10.3.1 新增 compression_locks 表

```sql
-- context_engine/store/db.py — 新增建表

CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,          -- 锁持有者标识 (实例ID + PID)
    acquired_at REAL NOT NULL,     -- 获取时间 (Unix秒)
    ttl INTEGER NOT NULL DEFAULT 300,  -- 锁有效期 (秒)
    renew_count INTEGER DEFAULT 0   -- 续期次数
);
```

#### 10.3.2 新建 compaction_lock.py

```python
# agent/middlewares/compaction_lock.py — 新建

"""
压缩锁：基于 SQLite 的原子锁，防止并发压缩导致会话状态分裂。
参考: hermes-agent compression_locks 表 + TTL + 自动续期

简化版（无续期线程）：锁 TTL 300秒，超时自动释放。
对于单实例部署足够；多实例部署后续可增加续期线程。
"""

import logging
import time
import uuid
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 300  # 5分钟
_HOLDER_PREFIX = f"{os.getpid()}-{uuid.getnode()}"  # PID + MAC 地址作为实例标识


class CompactionLockError(Exception):
    """压缩锁相关错误"""
    pass


class CompactionLock:
    """SQLite 原子压缩锁"""

    def __init__(self, db_path: str, ttl: int = _DEFAULT_TTL):
        self._db_path = db_path
        self._ttl = ttl

    @asynccontextmanager
    async def acquire(self, session_id: str, timeout: float = 10.0) -> AsyncIterator[None]:
        """
        获取压缩锁。超时未获取则抛出 CompactionLockError。
        用法: async with lock.acquire(session_id): ...
        """
        holder = f"{_HOLDER_PREFIX}-{session_id}-{int(time.time())}"
        deadline = time.time() + timeout

        while True:
            if self._try_acquire(session_id, holder):
                logger.info("压缩锁已获取: session=%s, holder=%s", session_id, holder)
                try:
                    yield
                finally:
                    self._release(session_id, holder)
                    logger.info("压缩锁已释放: session=%s", session_id)
                return

            # 清理过期锁
            self._cleanup_expired()

            if time.time() >= deadline:
                current = self._get_current_holder(session_id)
                raise CompactionLockError(
                    f"获取压缩锁超时({timeout}s): session={session_id}, "
                    f"当前持有者={current}"
                )

            await asyncio.sleep(0.5)

    def _try_acquire(self, session_id: str, holder: str) -> bool:
        """尝试获取锁（原子 upsert + 冲突检测）"""
        import sqlite3
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            # 先清理自己的过期锁
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND acquired_at + ttl < ?",
                (session_id, time.time()),
            )
            conn.commit()
            # 尝试插入
            conn.execute(
                "INSERT INTO compression_locks (session_id, holder, acquired_at, ttl) "
                "VALUES (?, ?, ?, ?)",
                (session_id, holder, time.time(), self._ttl),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # 已有锁
            return False
        finally:
            conn.close()

    def _release(self, session_id: str, holder: str) -> None:
        """释放锁（只有持有者能释放）"""
        import sqlite3
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
                (session_id, holder),
            )
            conn.commit()
        finally:
            conn.close()

    def _cleanup_expired(self) -> None:
        """清理所有过期锁"""
        import sqlite3
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute(
                "DELETE FROM compression_locks WHERE acquired_at + ttl < ?",
                (time.time(),),
            )
            conn.commit()
        finally:
            conn.close()

    def _get_current_holder(self, session_id: str) -> str | None:
        """获取当前锁持有者"""
        import sqlite3
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            row = conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
```

#### 10.3.3 修改 summarization.py

```python
# agent/middlewares/summarization.py — _apply_compression() 入口

from agent.middlewares.compaction_lock import CompactionLock, CompactionLockError

# __init__ 中初始化:
# self._compaction_lock = CompactionLock(db_path=mes_memory_db_path)

async def _apply_compression(self, messages, session_id, ...):
    try:
        async with self._compaction_lock.acquire(session_id, timeout=10.0):
            # 原有压缩逻辑
            ...
    except CompactionLockError as e:
        logger.warning("压缩锁获取失败，跳过本次压缩: %s", e)
        return messages  # 返回未压缩的消息，下次再试
```

### 实施顺序

1. `context_engine/store/db.py` 新增 `compression_locks` 建表语句
2. `agent/middlewares/compaction_lock.py` 新建模块
3. `agent/middlewares/summarization.py` 在 `_apply_compression()` 入口加锁
4. 测试

### 测试计划

- [ ] 单元测试：正常获取/释放锁
- [ ] 单元测试：并发获取锁只有一个成功
- [ ] 单元测试：锁超时后自动释放
- [ ] 集成测试：压缩流程中锁的正确生命周期

---

## P0-4 工具输出一行摘要替代（来源：hermes-agent）

### 现状

sherry-agent 的 `prune_tool_outputs` 策略是直接删除旧工具输出或截断到 `TOOL_OUTPUT_MAX_CHARS`。模型面对被删除工具输出的空消息，无法理解之前做了什么。

### 目标

为每种工具生成一行摘要替代完整输出，保留语义信息但大幅减少 token。

### 涉及文件

| 文件                                 | 操作                                  |
| ------------------------------------ | ------------------------------------- |
| `agent/middlewares/summarization.py` | `prune_tool_outputs` 步骤改为摘要替代 |

### 详细设计

```python
# agent/middlewares/summarization.py — 新增 _summarize_tool_result()

# 工具摘要模板（按工具名分发）
_TOOL_SUMMARY_TEMPLATES = {
    # 文件操作类
    "read_file": lambda r: f"[read_file] 读取了文件，返回 {len(r)} 字符",
    "write_file": lambda r: f"[write_file] 写入文件完成",
    "edit_file": lambda r: f"[edit_file] 编辑文件完成",
    "list_dir": lambda r: f"[list_dir] 列出目录，{r.count(chr(10))+1} 个条目",

    # 搜索类
    "grep": lambda r: f"[grep] 搜索完成，{r.count(chr(10))+1} 条匹配",
    "glob": lambda r: f"[glob] 匹配 {len(r.strip().splitlines())} 个文件",

    # 代码执行类
    "bash": lambda r: (
        f"[bash] 退出码={_extract_exit_code(r)}, "
        f"输出 {len(r)} 字符"
    ),

    # 通用模板
    "_default": lambda r: (
        f"[tool] 输出 {len(r)} 字符, "
        f"前100字符: {r[:100]}..."
    ),
}


def _summarize_tool_result(self, tool_name: str, result_text: str) -> str:
    """
    为工具输出生成一行摘要。
    参考: hermes-agent _summarize_tool_result()
    """
    template = _TOOL_SUMMARY_TEMPLATES.get(tool_name, _TOOL_SUMMARY_TEMPLATES["_default"])
    try:
        return template(result_text)
    except Exception:
        return f"[{tool_name}] 输出 {len(result_text)} 字符"


async def _prune_tool_outputs(self, messages, budget):
    """
    修改后：不再直接删除，而是替换为一行摘要。
    """
    for msg in messages:
        if msg.type != "tool":
            continue
        tool_name = getattr(msg, "name", "") or ""
        if tool_name in PROTECTED_TOOLS:
            continue  # 受保护工具不裁剪

        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if len(content) <= TOOL_OUTPUT_MAX_CHARS:
            continue  # 未超限，跳过

        # 生成一行摘要替代
        summary = self._summarize_tool_result(tool_name, content)
        msg.content = summary
        # 标记为已裁剪
        if hasattr(msg, "additional_kwargs"):
            msg.additional_kwargs["pruned"] = True
            msg.additional_kwargs["original_length"] = len(content)
```

### 实施顺序

1. 在 `summarization.py` 中新增 `_TOOL_SUMMARY_TEMPLATES` 字典
2. 新增 `_summarize_tool_result()` 方法
3. 修改 `_run_non_llm_strategies()` 中的 `prune_tool_outputs` 步骤
4. 逐步完善各工具的摘要模板

### 测试计划

- [ ] 单元测试：各工具模板生成正确摘要
- [ ] 单元测试：受保护工具不被裁剪
- [ ] 集成测试：压缩后模型仍能理解历史工具操作

---

## P1-1 压缩检查点回溯（来源：openclaw）

### 现状

sherry-agent 压缩后旧消息直接丢失（被摘要替代），无法回溯到压缩前状态。检查点器每会话只保留最新，无法恢复历史状态。

### 目标

压缩时创建检查点，记录压缩前后的消息引用，支持回溯到压缩前状态。

### 涉及文件

| 文件                                 | 操作                                     |
| ------------------------------------ | ---------------------------------------- |
| `context_engine/store/db.py`         | 新增 `compaction_checkpoints` 表         |
| `context_engine/store/core.py`       | 新增检查点 CRUD                          |
| `agent/middlewares/summarization.py` | 压缩成功后写入检查点                     |
| `server/service/messages.py`         | 新增 `restore_compaction_checkpoint` API |

### 详细设计

#### 10.1.1 新增 compaction_checkpoints 表

```sql
-- context_engine/store/db.py — 新增建表

CREATE TABLE IF NOT EXISTS compaction_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    checkpoint_seq INTEGER NOT NULL,          -- 检查点序号（每会话从0递增）
    pre_compaction_turn INTEGER NOT NULL,     -- 压缩前最新 turn_num
    post_compaction_turn INTEGER NOT NULL,    -- 压缩后最新 turn_num
    summary_text TEXT,                         -- 压缩摘要文本
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES messages(session_id)
);
CREATE INDEX IF NOT EXISTS idx_compaction_session ON compaction_checkpoints(session_id, checkpoint_seq);
```

#### 10.1.2 消息表新增软删除标记

```sql
-- context_engine/store/db.py — messages 表新增列（声明式迁移）

ALTER TABLE messages ADD COLUMN compacted INTEGER DEFAULT 0;
-- compacted=0: 活跃消息（进入上下文）
-- compacted=1: 被压缩的消息（保留在磁盘但不进入上下文）

ALTER TABLE messages ADD COLUMN compaction_checkpoint_id INTEGER DEFAULT NULL;
-- 指向所属的压缩检查点
```

#### 10.1.3 修改消息查询

```python
# context_engine/store/core.py — 修改查询

def get_messages_by_lastest_n_turns(session_id, n_turns, ...):
    """修改：默认只返回 compacted=0 的消息"""
    # ... 现有逻辑 ...
    # WHERE session_id = ? AND compacted = 0  ← 新增条件
    # 支持参数 include_compacted=True 恢复全部消息


def get_history_by_turn_page(session_id, ..., include_compacted=False):
    """修改：支持 include_compacted 参数"""
    condition = "session_id = ?"
    if not include_compacted:
        condition += " AND compacted = 0"
```

#### 10.1.4 压缩时写入检查点

```python
# agent/middlewares/summarization.py — _apply_compression() 成功后

# 1. 标记被压缩的消息为 compacted=1
pre_turn = latest_turn_before_compaction
post_turn = latest_turn_after_compaction
checkpoint_id = store.create_compaction_checkpoint(
    session_id=session_id,
    pre_compaction_turn=pre_turn,
    post_compaction_turn=post_turn,
    summary_text=summary_content,
)
store.mark_messages_compacted(session_id, up_to_turn=pre_turn, checkpoint_id=checkpoint_id)

# 2. 压缩摘要消息正常写入（compacted=0, active）
```

#### 10.1.5 回溯 API

```python
# server/service/messages.py — 新增

async def restore_compaction_checkpoint(session_id: str, checkpoint_id: int):
    """
    恢复到压缩检查点：将压缩后的消息标记为 compacted=1，
    将压缩前的消息恢复为 compacted=0。
    """
    checkpoint = store.get_compaction_checkpoint(session_id, checkpoint_id)
    if not checkpoint:
        raise ValueError(f"检查点不存在: {checkpoint_id}")

    # 压缩后的消息标记为 compacted
    store.mark_messages_compacted(
        session_id,
        from_turn=checkpoint["post_compaction_turn"],
        checkpoint_id=checkpoint_id,
    )
    # 压缩前的消息恢复为 active
    store.unmark_messages_compacted(
        session_id,
        up_to_turn=checkpoint["pre_compaction_turn"],
    )
    # 删除摘要消息
    store.delete_summary_message(session_id, checkpoint_id)
```

### 实施顺序

1. `context_engine/store/db.py` 新增表和列
2. `context_engine/store/core.py` 新增检查点 CRUD + 修改查询
3. `agent/middlewares/summarization.py` 压缩后写入检查点
4. `server/service/messages.py` 新增回溯 API
5. 测试

### 测试计划

- [ ] 单元测试：检查点 CRUD
- [ ] 单元测试：`compacted` 标记的正确设置和查询过滤
- [ ] 集成测试：压缩后回溯到检查点，验证消息完整恢复
- [ ] 集成测试：多次压缩后每个检查点可独立回溯

---

## P1-2 增量幂等持久化标记（来源：hermes-agent）

### 现状

sherry-agent 用 `_turn_assign_lock` 和 `ts_ms` 严格递增时间戳防止并发回合合并，但消息写入后无幂等标记，崩溃重试可能重复写入。

### 目标

每个消息写入后标记已持久化，崩溃后重试跳过已标记消息。

### 涉及文件

| 文件                           | 操作                          |
| ------------------------------ | ----------------------------- |
| `context_engine/store/core.py` | `add_messages()` 增加幂等标记 |
| `agent/core.py`                | flush 逻辑过滤已标记消息      |

### 详细设计

```python
# context_engine/store/core.py — 修改 add_messages()

_DB_PERSISTED_KEY = "_db_persisted"
_IDEMPOTENCY_KEY = "idempotency_key"  # 消息幂等键

async def add_messages(session_id, messages, ...):
    """修改：增量幂等持久化"""
    # 过滤已标记的消息
    new_messages = [m for m in messages if not m.get(_DB_PERSISTED_KEY, False)]
    if not new_messages:
        logger.debug("add_messages: 所有消息已持久化，跳过")
        return

    # 生成幂等键（防止重复写入）
    for msg in new_messages:
        if IDEMPOTENCY_KEY not in msg:
            msg[IDEMPOTENCY_KEY] = f"{session_id}-{turn_num}-{ts_ms}-{id(msg)}"

    # 写入数据库（幂等键唯一约束，重复写入被忽略）
    # ... 现有写入逻辑 ...
    # INSERT OR IGNORE INTO messages (..., idempotency_key) VALUES (..., ?)

    # 标记已持久化
    for msg in new_messages:
        msg[_DB_PERSISTED_KEY] = True
```

```python
# context_engine/store/db.py — messages 表新增列

ALTER TABLE messages ADD COLUMN idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency ON messages(idempotency_key);
```

### 实施顺序

1. `context_engine/store/db.py` 新增 `idempotency_key` 列和唯一索引
2. `context_engine/store/core.py` 修改 `add_messages()`
3. 测试

### 测试计划

- [ ] 单元测试：重复调用 `add_messages()` 不重复写入
- [ ] 集成测试：模拟崩溃后重试，无重复消息

---

## P1-3 活跃上下文投影 + context_eligible（来源：openclaw）

### 现状

sherry-agent 加载历史时按 `turn_num` 分页全量加载，所有消息都进入上下文，包括工具中间进度等无意义消息。

### 目标

标记每条消息是否 `context_eligible`，加载历史时只返回 eligible 消息，减少无效 token。

### 涉及文件

| 文件                                 | 操作                                  |
| ------------------------------------ | ------------------------------------- |
| `context_engine/store/db.py`         | messages 表新增 `context_eligible` 列 |
| `context_engine/store/core.py`       | 写入时标记 eligible，查询时过滤       |
| `agent/middlewares/summarization.py` | 标记中间进度消息为 ineligible         |

### 详细设计

```python
# context_engine/store/db.py

ALTER TABLE messages ADD COLUMN context_eligible INTEGER DEFAULT 1;
-- 1=进入上下文, 0=仅存储不进入上下文

# context_engine/store/core.py — 查询修改

def get_messages_by_lastest_n_turns(session_id, n_turns, only_eligible=True):
    condition = "session_id = ? AND compacted = 0"
    if only_eligible:
        condition += " AND context_eligible = 1"
    # ...

# 写入时标记
# 工具中间进度（如 heartbeat、progress 更新）标记为 context_eligible=0
# 最终工具结果标记为 context_eligible=1
```

### 实施顺序

1. `context_engine/store/db.py` 新增 `context_eligible` 列
2. `context_engine/store/core.py` 查询增加过滤
3. 逐步标记哪些消息类型应该 `context_eligible=0`

### 测试计划

- [ ] 单元测试：`only_eligible` 参数正确过滤
- [ ] 集成测试：ineligible 消息不进入 LLM 上下文

---

## P1-4 Recall 自动跨会话记忆召回（来源：oh-my-openagent）

### 现状

sherry-agent 有 FTS5 搜索和 `message_search` 工具，但需要用户/模型主动调用。无自动跨会话记忆召回机制。

### 目标

在回合开始前自动提取最近用户文本的查询术语，搜索历史会话记忆，以隐藏 nudge 注入相关上下文。

### 涉及文件

| 文件                                       | 操作                  |
| ------------------------------------------ | --------------------- |
| `context_engine/recall/planner.py` (新建)  | 词法查询生成          |
| `context_engine/recall/ledger.py` (新建)   | 回忆账本（防重复）    |
| `context_engine/recall/__init__.py` (新建) | 模块入口              |
| `server/service/turn_runner.py`            | 回合开始前注入 recall |

### 详细设计

```python
# context_engine/recall/planner.py — 新建

"""
Recall Planner：从最近 N 轮用户文本提取词法查询。
参考: oh-my-openagent recall/planner.ts
"""

import re
from typing import List

_RECENT_TURNS_FOR_QUERY = 6  # 从最近6轮用户文本提取
_MIN_TERM_LENGTH = 2
_MAX_TERMS = 10


def extract_query_terms(user_texts: List[str]) -> List[str]:
    """
    从用户文本列表中提取词法查询术语。
    返回：单个术语 + 二元短语的列表。
    """
    terms = set()
    for text in user_texts[-_RECENT_TURNS_FOR_QUERY:]:
        # 提取中英文术语
        cjk_terms = re.findall(r'[\u4e00-\u9fff]{2,}', text)
        en_terms = re.findall(r'[a-zA-Z]{2,}', text)
        terms.update(cjk_terms)
        terms.update(en_terms)

    # 生成二元短语（CJK 连续术语）
    bigrams = set()
    for text in user_texts[-_RECENT_TURNS_FOR_QUERY:]:
        cjk_sequences = re.findall(r'[\u4e00-\u9fff]+', text)
        for seq in cjk_sequences:
            for i in range(len(seq) - 1):
                bigrams.add(seq[i:i+2])

    all_terms = list(terms | bigrams)
    return all_terms[:_MAX_TERMS]


async def search_relevant_memories(
    query_terms: List[str],
    session_id: str,
    limit: int = 5,
) -> List[dict]:
    """
    用词法查询搜索历史会话记忆。
    使用 FTS5 搜索（复用现有 context_engine/core.py 的 search_messages）。
    """
    from context_engine.core import search_messages

    if not query_terms:
        return []

    query = " OR ".join(query_terms)
    results = await search_messages(query, exclude_session_id=session_id, limit=limit)
    return results
```

```python
# context_engine/recall/ledger.py — 新建

"""
Recall Ledger：每会话 JSON 文件记录已浮现的记忆路径，防止重复提示。
参考: oh-my-openagent recall/ledger.ts
"""

import json
import os
from pathlib import Path

_LEDGER_DIR = Path("data/recall_ledgers")


def _ledger_path(session_id: str) -> Path:
    return _LEDGER_DIR / f"{session_id}.json"


def get_surfaced(session_id: str) -> dict:
    """获取已浮现的记忆路径"""
    path = _ledger_path(session_id)
    if not path.exists():
        return {"version": 1, "surfaced": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def mark_surfaced(session_id: str, memory_key: str, content_hash: str) -> None:
    """标记记忆路径已浮现"""
    ledger = get_surfaced(session_id)
    ledger["surfaced"][memory_key] = {
        "hash": content_hash,
        "at": __import__("datetime").datetime.now().isoformat(),
    }
    _LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    _ledger_path(session_id).write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_surfaced(session_id: str, memory_key: str, content_hash: str) -> bool:
    """检查记忆路径是否已浮现（且内容未变）"""
    ledger = get_surfaced(session_id)
    entry = ledger["surfaced"].get(memory_key)
    return entry is not None and entry["hash"] == content_hash


def clear_session(session_id: str) -> None:
    """清除会话的回忆账本"""
    path = _ledger_path(session_id)
    if path.exists():
        path.unlink()
```

```python
# server/service/turn_runner.py — 回合开始前注入

async def _before_turn_inject_recall(self, session_id: str, messages: list) -> list:
    """
    回合开始前自动注入跨会话记忆 nudge。
    """
    from context_engine.recall.planner import extract_query_terms, search_relevant_memories
    from context_engine.recall.ledger import is_surfaced, mark_surfaced

    # 提取用户文本
    user_texts = [m.content for m in messages[-_RECENT_TURNS:] if m.type == "human"]
    terms = extract_query_terms(user_texts)

    # 搜索历史记忆
    results = await search_relevant_memories(terms, session_id)

    # 过滤已浮现的
    new_results = []
    for r in results:
        key = f"{r['session_id']}:{r['message_id']}"
        content_hash = str(hash(r["content"]))
        if not is_surfaced(session_id, key, content_hash):
            new_results.append(r)
            mark_surfaced(session_id, key, content_hash)

    if not new_results:
        return messages  # 无新记忆，不注入

    # 构造隐藏 nudge 消息
    nudge_text = "以下是相关历史对话回忆（仅供参考）：\n\n"
    for r in new_results:
        nudge_text += f"--- 会话 {r['session_id']} ---\n{r['content'][:200]}...\n\n"

    from langchain_core.messages import HumanMessage
    nudge_msg = HumanMessage(content=nudge_text)
    nudge_msg.additional_kwargs["hidden"] = True  # 标记为隐藏，前端不显示
    nudge_msg.additional_kwargs["recall_nudge"] = True

    # 插入到消息列表开头（系统提示词之后）
    insert_pos = 1 if messages and messages[0].type == "system" else 0
    messages.insert(insert_pos, nudge_msg)

    return messages
```

### 实施顺序

1. 新建 `context_engine/recall/` 模块
2. `server/service/turn_runner.py` 回合开始前调用 recall
3. 前端适配：隐藏 `recall_nudge` 标记的消息
4. 测试

### 测试计划

- [ ] 单元测试：`extract_query_terms()` 正确提取术语
- [ ] 单元测试：Ledger 的 surfaced 去重逻辑
- [ ] 集成测试：新会话自动回忆相关历史
- [ ] 集成测试：同一记忆不重复注入

---

## P1-5 转录树 + 消息分支（来源：openclaw）

### 现状

sherry-agent 的消息是线性列表，无分支概念。Fork 会话需要全量复制消息。

### 目标

消息通过 `parent_message_id` 构建树结构，支持分支和从任意点 fork。

### 涉及文件

| 文件                           | 操作                                   |
| ------------------------------ | -------------------------------------- |
| `context_engine/store/db.py`   | messages 表新增 `parent_message_id` 列 |
| `context_engine/store/core.py` | 写入时关联父消息，查询改为树遍历       |
| `server/service/messages.py`   | 新增 `fork_from_message` API           |

### 详细设计

```python
# context_engine/store/db.py

ALTER TABLE messages ADD COLUMN parent_message_id INTEGER DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(session_id, parent_message_id);
```

```python
# context_engine/store/core.py — 新增树遍历

def get_message_path_to_root(session_id: str, leaf_message_id: int) -> list:
    """
    从叶子消息回溯到根，返回路径上的所有消息。
    """
    path = []
    current_id = leaf_message_id
    while current_id is not None:
        msg = get_message_by_id(session_id, current_id)
        if not msg:
            break
        path.append(msg)
        current_id = msg["parent_message_id"]
    path.reverse()
    return path


async def fork_from_message(source_session_id: str, target_message_id: int,
                            new_session_id: str) -> None:
    """
    从指定消息创建分支（fork），不复制消息，只记录新的叶子指针。
    """
    # 新会话的初始 leaf 指向源消息
    store.set_session_leaf(new_session_id, target_message_id)
```

### 实施顺序

1. `context_engine/store/db.py` 新增 `parent_message_id` 列
2. `context_engine/store/core.py` 新增树遍历查询
3. `server/service/messages.py` 新增 fork API
4. 测试

### 测试计划

- [ ] 单元测试：树遍历正确回溯到根
- [ ] 集成测试：fork 会话不复制消息

---

## P2-1 事件溯源迁移（来源：opencode-dev）

### 现状

sherry-agent 直接持久化消息和状态，无事件溯源，崩溃后无法重放状态变更。

### 目标

为关键状态变更引入事件持久化，提升可重放性和崩溃恢复。

### 涉及文件

| 文件                            | 操作                             |
| ------------------------------- | -------------------------------- |
| `context_engine/events/` (新建) | 事件类型定义 + 事件存储 + 投影器 |
| `context_engine/store/db.py`    | 新增 `events` 表                 |
| 各写入操作                      | 改为发布事件                     |

### 详细设计

#### 10.2.1.1 事件表

```sql
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,          -- 事件类型枚举
    data TEXT NOT NULL,          -- JSON 事件数据
    seq INTEGER NOT NULL,       -- 会话内序列号
    created_at TEXT NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
```

#### 10.2.1.2 事件类型（渐进式，先覆盖压缩操作）

```python
# context_engine/events/types.py — 新建

from enum import Enum

class EventType(str, Enum):
    # 会话生命周期
    SESSION_CREATED = "session.created"
    SESSION_ENDED = "session.ended"

    # 消息
    MESSAGE_APPENDED = "message.appended"
    MESSAGE_UPDATED = "message.updated"
    MESSAGE_DELETED = "message.deleted"

    # 压缩
    COMPACTION_STARTED = "compaction.started"
    COMPACTION_ENDED = "compaction.ended"
    COMPACTION_FAILED = "compaction.failed"

    # 检查点
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"

    # 状态
    STATE_UPDATED = "state.updated"
```

#### 10.2.1.3 投影器

```python
# context_engine/events/projector.py — 新建

"""
事件投影器：订阅事件，投影到现有 SQLite 表（读模型）。
渐进式迁移：先为压缩操作投影，再扩展到全部操作。
"""

class EventProjector:
    def __init__(self, db_path: str):
        self._db_path = db_path

    async def project(self, event: dict) -> None:
        """将事件投影到读模型"""
        handler = self._handlers.get(event["type"])
        if handler:
            await handler(event)

    # 压缩事件投影
    async def _project_compaction_ended(self, event: dict) -> None:
        data = json.loads(event["data"])
        # 标记消息为 compacted
        # 写入 compaction_checkpoint
        ...
```

### 实施顺序

1. 新建 `context_engine/events/` 模块
2. `context_engine/store/db.py` 新增 `events` 表
3. **渐进迁移**：先为压缩操作引入事件 → 投影 → 验证一致性
4. 逐步扩展到消息写入、状态更新等操作
5. 新增 `replay_events(session_id)` API

### 测试计划

- [ ] 单元测试：事件写入和读取
- [ ] 集成测试：压缩操作通过事件投影后读模型一致
- [ ] 集成测试：`replay_events()` 从事件重建状态

---

## P2-2 Context Epoch 系统上下文快照（来源：opencode-dev）

### 现状

sherry-agent 用 `state_register_db` 冻结系统提示词快照，但无 epoch 概念，压缩后系统上下文管理粗糙。

### 目标

引入 Context Epoch 机制，精确跟踪系统上下文变化，压缩后自动重建系统提示词基线。

### 涉及文件

| 文件                          | 操作                          |
| ----------------------------- | ----------------------------- |
| `runtime/state_register.py`   | 新增 `context_epoch` 表和方法 |
| `workspace/prompt_builder.py` | 通过 epoch 管理基线           |

### 详细设计

```python
# runtime/state_register.py — StateRegisterDB 新增

"""
Context Epoch：系统上下文快照机制。
参考: opencode-dev context-epoch.ts

生命周期:
1. initialize() — 首次创建，生成系统上下文基线
2. prepare() — 加载快照，与当前系统上下文 reconcile/replace
   - 如果 compaction 发生在 epoch 之后，触发 replace
   - 如果系统上下文变化（agent 切换），触发 reconcile
3. replace() — 完全替换 baseline 和 snapshot（压缩后）
4. advance() — 仅更新 snapshot（上下文变化但 baseline 不变）
"""

# state_register.db 新增表
"""
CREATE TABLE IF NOT EXISTS context_epoch (
    session_id TEXT PRIMARY KEY,
    baseline TEXT NOT NULL,          -- 系统上下文基线 JSON
    snapshot TEXT NOT NULL,         -- 上下文快照 JSON
    baseline_seq INTEGER NOT NULL,  -- 基线对应的最新消息 seq
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

class ContextEpoch:
    def __init__(self, db: StateRegisterDB):
        self._db = db

    def initialize(self, session_id: str, system_context: dict) -> None:
        """首次创建 epoch"""
        baseline = json.dumps(system_context)
        self._db.upsert_context_epoch(session_id, baseline, baseline, seq=0)

    def prepare(self, session_id: str, current_context: dict,
                latest_compaction_seq: int | None) -> tuple[dict, str]:
        """
        加载 epoch，与当前上下文对比。
        返回: (effective_context, action)
        action = "ok" | "reconcile" | "replace"
        """
        epoch = self._db.get_context_epoch(session_id)
        if not epoch:
            self.initialize(session_id, current_context)
            return current_context, "ok"

        baseline = json.loads(epoch["baseline"])
        snapshot = json.loads(epoch["snapshot"])

        # 压缩后重建
        if latest_compaction_seq and latest_compaction_seq > epoch["baseline_seq"]:
            self.replace(session_id, current_context, latest_compaction_seq)
            return current_context, "replace"

        # 检查上下文是否变化
        current_json = json.dumps(current_context, sort_keys=True)
        snapshot_json = json.dumps(snapshot, sort_keys=True)
        if current_json != snapshot_json:
            self.advance(session_id, current_context)
            return current_context, "reconcile"

        return snapshot, "ok"

    def replace(self, session_id: str, new_context: dict, seq: int) -> None:
        """完全替换 baseline 和 snapshot（压缩后）"""
        ctx_json = json.dumps(new_context)
        self._db.upsert_context_epoch(session_id, ctx_json, ctx_json, seq)

    def advance(self, session_id: str, new_snapshot: dict) -> None:
        """仅更新 snapshot（上下文变化但 baseline 不变）"""
        self._db.update_context_epoch_snapshot(session_id, json.dumps(new_snapshot))
```

### 实施顺序

1. `runtime/state_register.py` 新增 `context_epoch` 表和 `ContextEpoch` 类
2. `workspace/prompt_builder.py` 在 `build_system_prompt()` 中调用 epoch
3. 压缩成功后调用 `epoch.replace()`
4. Agent 切换时调用 `epoch.advance()`
5. 测试

### 测试计划

- [ ] 单元测试：epoch CRUD
- [ ] 集成测试：压缩后系统提示词自动重建
- [ ] 集成测试：Agent 切换后 snapshot 更新但 baseline 保留

---

## P2-3 双水位游标 Facts 提取（来源：oh-my-openagent）

### 现状

sherry-agent 无自动事实提取，MEMORY.md 完全依赖模型主动调用 memory 工具。

### 目标

对话回合结束后自动将对话片段入队，异步用 LLM 提取持久事实写入 MEMORY.md。双水位游标确保崩溃恢复。

### 涉及文件

| 文件                                       | 操作           |
| ------------------------------------------ | -------------- |
| `context_engine/facts/queue.py` (新建)     | 队列管理       |
| `context_engine/facts/cursor.py` (新建)    | 双水位游标     |
| `context_engine/facts/extractor.py` (新建) | LLM 事实提取   |
| `server/service/turn_runner.py`            | 回合结束后入队 |

### 详细设计

```python
# context_engine/facts/cursor.py — 新建

"""
双水位游标：确保 Facts 提取的崩溃恢复。
参考: oh-my-openagent facts/schema.ts

enqueued_through_*: 单调递增，永不回退（标记已入队的位置）
consumed_through_*: 验证后推进（标记已处理的位置）
"""

import json
from pathlib import Path

_CURSOR_DIR = Path("data/facts_cursors")

def get_cursor(session_id: str) -> dict:
    path = _CURSOR_DIR / f"{session_id}.json"
    if not path.exists():
        return {"enqueued_through_turn": 0, "consumed_through_turn": 0}
    return json.loads(path.read_text(encoding="utf-8"))

def advance_enqueued(session_id: str, turn_num: int) -> None:
    cursor = get_cursor(session_id)
    cursor["enqueued_through_turn"] = max(cursor["enqueued_through_turn"], turn_num)
    _save_cursor(session_id, cursor)

def advance_consumed(session_id: str, turn_num: int) -> None:
    cursor = get_cursor(session_id)
    if turn_num > cursor["enqueued_through_turn"]:
        raise ValueError(f"consumed({turn_num}) > enqueued({cursor['enqueued_through_turn']})")
    cursor["consumed_through_turn"] = max(cursor["consumed_through_turn"], turn_num)
    _save_cursor(session_id, cursor)

def get_pending(session_id: str) -> tuple[int, int]:
    """返回 (consumed+1, enqueued) 的待处理范围"""
    cursor = get_cursor(session_id)
    return cursor["consumed_through_turn"] + 1, cursor["enqueued_through_turn"]
```

```python
# context_engine/facts/extractor.py — 新建

_FACTS_EXTRACTION_PROMPT = """\
从以下对话片段中提取持久有用的事实（跨会话有价值的信息）。

提取规则:
1. 用户偏好和习惯
2. 项目约定和环境事实
3. 关键技术决策和原因
4. 工具使用经验
5. 不提取临时任务进度

对话片段:
{conversation_text}

输出 JSON 数组，每个元素: {"category": "...", "fact": "..."}
"""
```

### 实施顺序

1. 新建 `context_engine/facts/` 模块
2. `server/service/turn_runner.py` 回合结束后入队
3. 独立异步线程消费队列
4. 测试

### 测试计划

- [ ] 单元测试：游标单调递增
- [ ] 集成测试：崩溃后恢复不丢失不重复
- [ ] 集成测试：提取的事实写入 MEMORY.md

---

## P2-4 steer/queue 双投递模式（来源：opencode-dev）

### 现状

sherry-agent 的输入是 FIFO 队列，无法在活跃 turn 中注入引导。用户必须在模型完成当前回合后才能追加输入。

### 目标

支持 steer（实时引导）和 queue（排队）两种投递模式。

### 涉及文件

| 文件                            | 操作                         |
| ------------------------------- | ---------------------------- |
| `server/service/turn_runner.py` | 新增 steer 通道              |
| `agent/core.py`                 | LangGraph 执行循环检查 steer |

### 详细设计

```python
# server/service/turn_runner.py — 新增 steer 通道

"""
steer：在活跃 turn 中注入引导，立即执行。
queue：等待当前 turn 结束后执行（现有 FIFO 逻辑）。
参考: opencode-dev input.ts steer/queue 双投递
"""

# 按会话隔离的 steer 队列
_pending_steers: dict[str, list[dict]] = {}  # [session_id] = [{text, timestamp}]

async def submit_steer(self, session_id: str, text: str) -> None:
    """提交实时引导"""
    self._pending_steers.setdefault(session_id, []).append({
        "text": text,
        "timestamp": time.time(),
    })

def drain_steers(self, session_id: str) -> list[dict]:
    """排空 steer 队列"""
    steers = self._pending_steers.get(session_id, [])
    self._pending_steers[session_id] = []
    return steers
```

### 实施顺序

1. `server/service/turn_runner.py` 新增 steer 通道
2. 修改 LangGraph 执行循环，在工具执行间隙检查 steer
3. 前端适配 steer 输入
4. 测试

### 测试计划

- [ ] 集成测试：模型长时间运行时注入 steer
- [ ] 集成测试：steer 不丢失

---

## P2-5 向量嵌入语义搜索（来源：openclaw）

### 现状

sherry-agent 仅支持 FTS5 关键词搜索，无法语义匹配。

### 目标

为消息生成嵌入向量，支持语义级跨会话搜索。

### 涉及文件

| 文件                                          | 操作             |
| --------------------------------------------- | ---------------- |
| `context_engine/embeddings/store.py` (新建)   | 嵌入存储         |
| `context_engine/embeddings/indexer.py` (新建) | 嵌入生成         |
| `context_engine/embeddings/search.py` (新建)  | 语义搜索         |
| `agent/tools/message_search.py`               | 新增语义搜索模式 |

### 详细设计

```python
# context_engine/store/db.py — 新增表

CREATE TABLE IF NOT EXISTS message_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,          -- 嵌入向量（二进制）
    model TEXT NOT NULL,              -- 嵌入模型名
    dim INTEGER NOT NULL,             -- 向量维度
    created_at TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_session ON message_embeddings(session_id);
```

```python
# context_engine/embeddings/search.py — 新建

async def semantic_search(query_text: str, session_id: str | None = None,
                          limit: int = 5) -> list[dict]:
    """
    语义搜索：用查询文本的嵌入向量搜索相似消息。
    """
    # 1. 生成查询嵌入
    query_embedding = await get_embedding(query_text)

    # 2. 从 SQLite 加载所有嵌入（小规模可行）
    candidates = load_all_embeddings(session_id)

    # 3. 余弦相似度排序
    scored = []
    for c in candidates:
        score = cosine_similarity(query_embedding, c["embedding"])
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [c for _, c in scored[:limit]]
```

### 实施顺序

1. 选择嵌入模型（`text-embedding-3-small` 或本地 `bge-m3`）
2. 新建 `context_engine/embeddings/` 模块
3. 消息写入后异步生成嵌入
4. 扩展 `message_search` 工具
5. 测试

### 测试计划

- [ ] 单元测试：嵌入存储和读取
- [ ] 集成测试：语义搜索返回相关结果
- [ ] 性能测试：大规模消息下的搜索延迟

---

---

## 长程任务专用记忆编排（LT-1 ~ LT-8）

> 以下 8 个方案针对长程任务特有的记忆问题，不属于通用记忆层，而是 TaskFlow/子代理/压缩/记忆之间的编排层。

---

## LT-1 分层记忆存储（来源：oh-my-openagent + hermes-agent）

### 现状

MEMORY.md 硬限 2200 字符（`memory_char_limit=2200`，见 `agent/tools/memory.py:115`）。P0-1（Memory Flush）和 P2-3（Facts 提取）都往 MEMORY.md 追加写入，长程任务跨多次会话后必然溢出。P0-1 的 `append_entries()` 有截断逻辑（pop 最旧条目），但这意味着早期关键事实被挤出——用户偏好、项目约定等持久事实被临时任务事实挤出。

### 目标

建立三层记忆结构：MEMORY.md（精简系统提示词层）→ 结构化记忆文件（按主题/项目分类）→ 原始对话历史（SQLite）。Memory Flush 和 Facts 提取写入结构化记忆文件层，不挤占 MEMORY.md。

### 涉及文件

| 文件                                  | 操作             |
| ------------------------------------- | ---------------- |
| `agent/tools/memory.py`               | 新增分层存储逻辑 |
| `config/num.py`                       | 新增分层记忆配置 |
| `agent/tools/memory_tiered.py` (新建) | 分层记忆管理器   |

### 详细设计

#### 目录结构

```
workspace/memory/
├── MEMORY.md              # 层1: 精简摘要层 (2200字符, 注入系统提示词)
├── USER.md                # 层1: 用户画像 (1375字符, 注入系统提示词)
├── facts/                 # 层2: 结构化事实层 (不注入系统提示词, 按需检索)
│   ├── environment.md     # 环境事实 (OS, Python版本, 依赖等)
│   ├── project.md         # 项目约定 (命名规范, 架构决策等)
│   ├── decisions.md       # 关键决策记录 (为什么选X不选Y)
│   ├── user_prefs.md      # 用户偏好深度 (比USER.md更详细)
│   └── tool_lessons.md    # 工具使用经验 (常见坑, 最佳实践)
│
# 层3: 原始对话历史 (mes_memory.db, 已有)
```

#### 新建 memory_tiered.py

```python
# agent/tools/memory_tiered.py — 新建

"""
分层记忆管理器：协调 MEMORY.md（精简层）和 facts/（结构化层）。
P0-1 Memory Flush 和 P2-3 Facts 提取写入 facts/ 层，不挤占 MEMORY.md。

参考: oh-my-openagent Git-backed多文件记忆 + hermes-agent MemoryStore分类
"""

from pathlib import Path
from config import MEMORY_DIR

_FACTS_DIR = MEMORY_DIR / "facts"
_FACTS_FILES = {
    "environment": _FACTS_DIR / "environment.md",
    "project": _FACTS_DIR / "project.md",
    "decisions": _FACTS_DIR / "decisions.md",
    "user": _FACTS_DIR / "user_prefs.md",
    "tool": _FACTS_DIR / "tool_lessons.md",
}
# 每个文件的字符上限
_FACTS_CHAR_LIMIT = 4000

# MEMORY.md 中为每个 facts 文件保留一行索引摘要
_FACTS_INDEX_MAX_CHARS = 200  # 每类最多200字符索引


class TieredMemoryStore:
    """协调 MEMORY.md 和 facts/ 层的写入。"""

    def __init__(self, memory_store):
        self._memory_store = memory_store  # 原 MemoryStore 实例
        _FACTS_DIR.mkdir(parents=True, exist_ok=True)
        for path in _FACTS_FILES.values():
            path.touch(exist_ok=True)

    def add_fact(self, category: str, fact: str) -> bool:
        """
        将事实写入 facts/<category>.md。
        category: environment | project | decisions | user | tool
        """
        path = _FACTS_FILES.get(category)
        if not path:
            return False

        fact = fact.strip()
        if not fact:
            return False

        with self._memory_store._file_lock(path):
            existing = path.read_text(encoding="utf-8") or ""
            # 去重
            if fact in existing:
                return True
            # 容量检查 + 截断最旧
            combined = existing.rstrip() + f"\n§ {fact}\n"
            if len(combined) > _FACTS_CHAR_LIMIT:
                entries = [e.strip() for e in combined.split("§") if e.strip()]
                while len("§ ".join(entries)) > _FACTS_CHAR_LIMIT and len(entries) > 1:
                    entries.pop(0)
                combined = "§ ".join(entries)
            path.write_text(combined, encoding="utf-8")

        # 更新 MEMORY.md 中的索引摘要
        self._refresh_memory_index(category)
        return True

    def _refresh_memory_index(self, category: str) -> None:
        """
        在 MEMORY.md 中维护 facts 文件的索引摘要。
        格式: § [facts:environment] Python 3.12, Windows, ...
        """
        path = _FACTS_FILES[category]
        content = path.read_text(encoding="utf-8") or ""
        # 取前 N 字符作为摘要
        summary = content.replace("§", "").strip()[:_FACTS_INDEX_MAX_CHARS]
        index_entry = f"[facts:{category}] {summary}"

        # 在 MEMORY.md 中替换或追加该索引条目
        # 查找已存在的 [facts:{category}] 条目并替换
        with self._memory_store._file_lock(self._memory_store._path_for("memory")):
            self._memory_store._reload_target("memory")
            entries = self._memory_store.memory_entries
            found = False
            for i, entry in enumerate(entries):
                if f"[facts:{category}]" in entry:
                    entries[i] = index_entry
                    found = True
                    break
            if not found:
                entries.append(index_entry)
            self._memory_store._set_entries("memory", entries)
            self._memory_store.save_to_disk("memory")

    def read_facts(self, category: str | None = None) -> dict[str, str]:
        """读取 facts 文件内容。category=None 返回全部。"""
        result = {}
        files = _FACTS_FILES if category is None else {category: _FACTS_FILES[category]}
        for cat, path in files.items():
            result[cat] = path.read_text(encoding="utf-8") or ""
        return result

    def search_facts(self, query: str) -> list[dict]:
        """
        在 facts 文件中搜索匹配查询的条目。
        简单子串匹配，后续可替换为 FTS5 或向量搜索。
        """
        results = []
        for cat, path in _FACTS_FILES.items():
            content = path.read_text(encoding="utf-8") or ""
            for entry in content.split("§"):
                entry = entry.strip()
                if entry and query.lower() in entry.lower():
                    results.append({"category": cat, "fact": entry})
        return results
```

#### 修改 P0-1 Memory Flush 和 P2-3 Facts 提取

```python
# agent/middlewares/memory_flush.py — 修改 run_memory_flush()

# 写入改为分层：
#   关键决策 → facts/decisions.md
#   环境事实 → facts/environment.md
#   项目约定 → facts/project.md
#   用户偏好 → facts/user_prefs.md
#   工具经验 → facts/tool_lessons.md
#
# 旧: memory_store.append_entries(extracted)
# 新: tiered_store = TieredMemoryStore(memory_store)
#     for entry in parsed_extracted:
#         tiered_store.add_fact(entry["category"], entry["fact"])
```

### 实施顺序

1. `config/num.py` 新增分层记忆配置
2. `agent/tools/memory_tiered.py` 新建分层管理器
3. 创建 `workspace/memory/facts/` 目录和文件
4. 修改 P0-1 Memory Flush 写入分层
5. 修改 P2-3 Facts 提取写入分层
6. 测试

### 测试计划

- [ ] 单元测试：`add_fact()` 写入正确分类
- [ ] 单元测试：MEMORY.md 索引摘要更新正确
- [ ] 单元测试：facts 文件容量截断
- [ ] 集成测试：大量事实写入不挤占 MEMORY.md 精简层
- [ ] 集成测试：`search_facts()` 返回正确匹配

---

## LT-2 跨会话 TaskFlow 自动续接（来源：openclaw managedFlows）

### 现状

TaskFlow 状态持久化在 SQLite（`taskflow_registry.db`），但新会话不会自动检查"上次有没有未完成的 TaskFlow"。用户隔天回来说"继续"，系统不会主动恢复任务上下文，模型需要手动调用 `taskflow_summary` 才知道有未完成任务。

### 目标

会话启动时自动扫描该 channel/chat 关联的未完成 TaskFlow，将任务摘要注入系统提示词，使模型主动续接。

### 涉及文件

| 文件                                            | 操作                                   |
| ----------------------------------------------- | -------------------------------------- |
| `agent/tools/taskflow/registry/store_sqlite.py` | 新增 `get_active_flows_by_session()`   |
| `workspace/prompt_builder.py`                   | `build_system_prompt()` 注入未完成任务 |
| `server/service/messages.py`                    | 会话创建/恢复时扫描 TaskFlow           |

### 详细设计

```python
# agent/tools/taskflow/registry/store_sqlite.py — 新增

async def get_active_flows_by_channel(channel_id: str, chat_id: str) -> list[dict]:
    """
    查找与指定 channel/chat 关联的未完成 TaskFlow（status=running 或 waiting）。
    """
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT {_SELECT_COLUMNS_SQL_FROM} WHERE child_session_key LIKE ? "
            f"AND status IN ('{TaskFlowStatus.RUNNING.value}', '{TaskFlowStatus.WAITING.value}') "
            f"ORDER BY expected_revision DESC",
            (f"{channel_id}:{chat_id}:%",),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]
```

```python
# workspace/prompt_builder.py — build_system_prompt() 注入

def build_system_prompt(session_id, channel_id, chat_id, ...):
    ...  # 现有逻辑 ...

    # === 新增：注入未完成 TaskFlow 摘要 ===
    active_flows = asyncio.run(
        taskflow_store.get_active_flows_by_channel(channel_id, chat_id)
    )
    if active_flows:
        flow_lines = ["## 上次未完成的任务"]
        for flow in active_flows[:3]:  # 最多3个
            state = flow.get("state", {})
            steps = state.get("steps", [])
            done_steps = [s for s in steps if s.get("result")]
            pending_steps = [s for s in steps if not s.get("result")]
            flow_lines.append(
                f"- TaskFlow '{flow['flow_id']}' (status={flow['status']}): "
                f"{state.get('description', '')} | "
                f"已完成 {len(done_steps)}/{len(steps)} 步 | "
                f"待处理: {[s.get('task', '')[:50] for s in pending_steps[:3]]}"
            )
        flow_lines.append("如果用户说'继续'，请用 taskflow_summary 查看详情并继续执行。")
        system_prompt += "\n\n" + "\n".join(flow_lines)

    return system_prompt
```

### 实施顺序

1. `agent/tools/taskflow/registry/store_sqlite.py` 新增 `get_active_flows_by_channel()`
2. `workspace/prompt_builder.py` 注入未完成任务摘要
3. 测试

### 测试计划

- [ ] 单元测试：`get_active_flows_by_channel()` 正确过滤
- [ ] 集成测试：新会话系统提示词包含未完成任务摘要
- [ ] 集成测试：任务全部完成后不再注入

---

## LT-3 压缩摘要链式退化防护（来源：oh-my-openagent 内存独立于压缩）

### 现状

多次压缩后，摘要 = 前次摘要 + 新对话 → 再摘要，信息逐层稀释。长程任务 20+ 次压缩后，第一次压缩的摘要可能已完全失真。现有 `_SUMMARY_PROMPT_UPDATE` 做链式合并，但无法阻止信息退化。

### 目标

引入"压缩摘要基准线"机制：不依赖前次摘要链，而是从分层记忆（LT-1）和 TaskFlow 状态（LT-2）重建压缩摘要的基准部分，确保关键信息不因多次压缩而退化。

### 涉及文件

| 文件                                 | 操作                                  |
| ------------------------------------ | ------------------------------------- |
| `agent/middlewares/summarization.py` | 修改 `_create_summary()` 基准重建逻辑 |
| `agent/tools/memory_tiered.py`       | 读取分层记忆作为基准                  |

### 详细设计

```python
# agent/middlewares/summarization.py — _create_summary() 修改

async def _create_summary(self, messages, budget, session_id, prior_summary=None):
    """
    修改：在 LLM 摘要 prompt 中注入"基准上下文"，
    基准来自分层记忆 + TaskFlow 状态，不依赖前次摘要链。
    """
    # === 新增：构建基准上下文 ===
    baseline_context = await self._build_baseline_context(session_id)

    prompt_parts = [_SUMMARY_PROMPT_PREFIX]

    # 注入基准上下文（替代前次摘要的权威地位）
    if baseline_context:
        prompt_parts.append(f"\n## 持久基准（来自记忆库，权威）\n{baseline_context}\n")

    # 前次摘要作为参考（非权威，可能已退化）
    if prior_summary:
        prompt_parts.append(
            f"\n## 前次压缩摘要（参考，可能已过时）\n{prior_summary}\n"
        )

    # 当前对话
    conversation_text = self._serialize(messages)
    prompt_parts.append(f"\n## 待压缩对话\n{conversation_text}\n")

    prompt_parts.append(_SUMMARY_PROMPT_SUFFIX)

    prompt = "\n".join(prompt_parts)
    # ... 调用 LLM 生成摘要 ...


async def _build_baseline_context(self, session_id: str) -> str:
    """
    从分层记忆和 TaskFlow 状态构建基准上下文。
    这个基准不依赖压缩链，每次压缩都从源头重建。
    """
    parts = []

    # 分层记忆（LT-1）
    tiered = getattr(self, '_tiered_memory', None)
    if tiered:
        facts = tiered.read_facts()
        for cat, content in facts.items():
            if content.strip():
                parts.append(f"[{cat}] {content.strip()[:500]}")

    # TaskFlow 状态（LT-2）
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store
    # 注意：需要 session_id → channel/chat 映射
    active_flows = await taskflow_store.get_active_flows_by_session(session_id)
    for flow in active_flows[:2]:
        state = flow.get("state", {})
        parts.append(
            f"[task:{flow['flow_id']}] {state.get('description', '')} "
            f"(status={flow['status']})"
        )

    return "\n".join(parts)
```

### 实施顺序

1. `agent/middlewares/summarization.py` 新增 `_build_baseline_context()`
2. 修改 `_create_summary()` 注入基准上下文
3. 在 Summarization `__init__` 中注入 `_tiered_memory`
4. 测试

### 测试计划

- [ ] 集成测试：多次压缩后摘要仍包含分层记忆中的关键事实
- [ ] 集成测试：前次摘要退化不影响基准事实
- [ ] 集成测试：TaskFlow 状态在摘要中正确反映

---

## LT-4 TaskFlow 完成触发知识提取（来源：NUDGE_EXTRACTION_PLAN.md plan-aware 提取）

### 现状

P2-3 Facts 提取在每回合结束后入队，但长程任务的关键学习点往往在任务**完成时**（"做对了什么、做错了什么"），而非每回合。每回合提取会产生大量噪声。

### 目标

TaskFlow 状态变为 DONE/FAILED 时触发一次专项知识提取，用 TaskFlow 的完整 steps/results 作为结构化上下文。

### 涉及文件

| 文件                                             | 操作                  |
| ------------------------------------------------ | --------------------- |
| `agent/tools/taskflow/tools/taskflow_finish.py`  | finish 时触发知识提取 |
| `agent/tools/taskflow/tools/taskflow_fail.py`    | fail 时触发知识提取   |
| `context_engine/facts/task_extraction.py` (新建) | TaskFlow 专项提取     |

### 详细设计

```python
# context_engine/facts/task_extraction.py — 新建

"""
TaskFlow 完成触发的专项知识提取。
参考: NUDGE_EXTRACTION_PLAN.md plan-aware 提取理念

与 P2-3 每回合 Facts 提取的区别:
- P2-3: 每回合提取碎片化事实（用户偏好、环境等）
- LT-4: 任务完成时提取结构化经验（做对了什么、踩了什么坑、下次怎么做更好）
"""

_TASK_EXTRACTION_PROMPT = """\
任务已完成。请从以下任务执行记录中提取持久有用的经验。

任务描述: {description}
最终状态: {status}
执行步骤: {steps}
注入结果: {results}

提取规则:
1. 做对了什么（可复用的方法/流程）
2. 做错了什么（避免重复的坑）
3. 工具使用经验（哪个工具适合哪个场景）
4. 关键决策及原因（为什么这样做）
5. 不提取临时任务进度

输出 JSON: {"category": "...", "fact": "..."}
类别: decisions | tool | project | user
"""


async def extract_task_knowledge(flow: dict) -> list[dict]:
    """
    从已完成的 TaskFlow 提取知识。
    返回: [{"category": "...", "fact": "..."}]
    """
    state = flow.get("state", {})
    prompt = _TASK_EXTRACTION_PROMPT.format(
        description=state.get("description", ""),
        status=flow["status"],
        steps=json.dumps(state.get("steps", []), ensure_ascii=False)[:2000],
        results=json.dumps(state.get("results", []), ensure_ascii=False)[:2000],
    )

    # 用便宜模型
    from config.num import MEMORY_FLUSH_MODEL, MEMORY_FLUSH_OUTPUT_MAX_TOKENS
    llm = llm_factory.get_llm(
        model=MEMORY_FLUSH_MODEL or None,
        max_tokens=MEMORY_FLUSH_OUTPUT_MAX_TOKENS,
        timeout=30,
    )
    response = await llm.ainvoke(prompt)
    # 解析 JSON 并写入分层记忆
    facts = _parse_facts(response.content)
    tiered = TieredMemoryStore(memory_store)
    for fact in facts:
        tiered.add_fact(fact["category"], fact["fact"])
    return facts
```

```python
# agent/tools/taskflow/tools/taskflow_finish.py — 修改

@tool("taskflow_finish")
async def taskflow_finish(flow_id, summary="", expected_revision=None):
    # ... 现有逻辑 ...
    updated = await store_sqlite.update_flow(...)

    # === 新增：触发知识提取 ===
    if updated["status"] == TaskFlowStatus.DONE.value:
        from context_engine.facts.task_extraction import extract_task_knowledge
        try:
            await extract_task_knowledge(updated)
            logger.info("TaskFlow {} 完成后知识提取已触发", flow_id)
        except Exception as e:
            logger.warning("TaskFlow 知识提取失败（不影响 finish）: {}", e)

    return f"TaskFlow finished: ..."
```

### 实施顺序

1. `context_engine/facts/task_extraction.py` 新建模块
2. `taskflow_finish.py` 和 `taskflow_fail.py` 触发提取
3. 测试

### 测试计划

- [ ] 单元测试：prompt 正确格式化
- [ ] 集成测试：finish 后 facts 文件更新
- [ ] 集成测试：fail 也触发提取
- [ ] 集成测试：提取失败不影响 finish/fail 操作

---

## LT-5 子代理记忆回流（来源：oh-my-openagent session binding）

### 现状

子代理有持久会话（`SpawnMode.SESSION`），但子代理的 MEMORY.md 写入不会回流到父会话。长程任务委派子代理后，子代理学到的经验（如"这个项目的测试用 conftest.py 覆盖"）丢失。

### 目标

子代理完成时，将其记忆条目合并回父会话的分层记忆，带去重和容量控制。

### 涉及文件

| 文件                                          | 操作                         |
| --------------------------------------------- | ---------------------------- |
| `agent/tools/subagent/registry/completion.py` | 子代理完成时触发记忆回流     |
| `agent/tools/memory_tiered.py`                | 新增 `merge_from_subagent()` |

### 详细设计

```python
# agent/tools/memory_tiered.py — 新增方法

def merge_from_subagent(self, subagent_memory: dict[str, str]) -> int:
    """
    将子代理的记忆条目合并到父会话的分层记忆。
    subagent_memory: {"environment": "...", "project": "...", ...}

    返回: 新增的条目数
    """
    added = 0
    for category, content in subagent_memory.items():
        if not content or not content.strip():
            continue
        # 按条目拆分
        for entry in content.split("§"):
            entry = entry.strip()
            if entry and self.add_fact(category, entry):
                added += 1
    return added


def export_subagent_memory(self, subagent_session_id: str) -> dict[str, str]:
    """
    导出子代理的记忆（供回流时使用）。
    从子代理的 MEMORY.md 和 facts/ 读取。
    """
    # 子代理有独立的 workspace/memory/ 目录
    # 或者子代理共享父代理的 MemoryStore 实例但写入不同的 key
    # 具体取决于子代理的隔离模式
    ...
```

```python
# agent/tools/subagent/registry/completion.py — 修改

def resolve_finalized_task_state(run: SubagentRunRecord) -> dict:
    # ... 现有逻辑 ...

    # === 新增：触发记忆回流 ===
    # 在子代理完成时，将其记忆合并回父会话
    try:
        from agent.tools.memory_tiered import TieredMemoryStore
        from agent.tools.memory import MemoryStore

        parent_memory = MemoryStore()  # 父会话的 MemoryStore
        parent_memory.load_from_disk()
        tiered = TieredMemoryStore(parent_memory)

        subagent_memory = tiered.export_subagent_memory(run.child_session_key)
        added = tiered.merge_from_subagent(subagent_memory)
        logger.info("子代理 {} 记忆回流 {} 条", run.run_id, added)
    except Exception as e:
        logger.warning("子代理记忆回流失败（不影响完成）: {}", e)

    return result
```

### 实施顺序

1. `agent/tools/memory_tiered.py` 新增 `merge_from_subagent()` 和 `export_subagent_memory()`
2. `agent/tools/subagent/registry/completion.py` 在完成时触发回流
3. 测试

### 测试计划

- [ ] 单元测试：`merge_from_subagent()` 去重正确
- [ ] 集成测试：子代理完成后父会话 facts 更新
- [ ] 集成测试：回流失败不影响子代理完成

---

## LT-6 记忆过期与清理策略（来源：oh-my-openagent reflection + openclaw disk budget）

### 现状

所有方案都是"写入"记忆，没有一个方案解决"何时遗忘"。长程任务运行数月后，MEMORY.md 和 facts 积累大量过时条目（如"项目用 Python 3.9"——后来升级到 3.12 了，旧条目仍在）。oh-my-openagent 有反射引擎 + Dream 触发器，openclaw 有磁盘预算管理 + 高水位修剪。

### 目标

实现记忆生命周期管理：TTL 过期、冲突检测（新条目使旧条目失效）、定期整理。

### 涉及文件

| 文件                                        | 操作                 |
| ------------------------------------------- | -------------------- |
| `context_engine/memory_lifecycle.py` (新建) | 记忆生命周期管理     |
| `agent/tools/memory_tiered.py`              | 条目增加时间戳元数据 |
| `server/service/turn_runner.py`             | 空闲时触发整理       |

### 详细设计

```python
# context_engine/memory_lifecycle.py — 新建

"""
记忆生命周期管理。
参考: oh-my-openagent reflection engine + openclaw disk budget

三个策略:
1. TTL 过期: 条目带时间戳，超过 TTL 的标记为候选删除
2. 冲突检测: 新条目与旧条目语义冲突时，旧条目标记为 superseded
3. 定期整理: 空闲时用 LLM 审查候选删除条目，确认后删除
"""

import time
from pathlib import Path

# 条目格式: "§ [2026-09-10T14:30] <fact text>"
# 时间戳在写入时自动添加

DEFAULT_TTL_DAYS = 90  # 默认 90 天后标记为候选删除
SUPERSEDE_KEYWORDS = {
    "升级": ["之前", "原来是", "旧版"],
    "改为": ["之前", "原来是", "之前用"],
    "不再": ["之前", "曾经"],
    "废弃": ["之前用", "原来用"],
}


def add_timestamp_to_entry(entry: str) -> str:
    """为条目添加时间戳前缀。"""
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
    return f"[{ts}] {entry}"


def extract_timestamp(entry: str) -> float | None:
    """从条目提取时间戳。"""
    import re
    match = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\]", entry)
    if not match:
        return None
    from datetime import datetime
    dt = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M")
    return dt.timestamp()


def find_superseded_entries(new_entry: str, existing_entries: list[str]) -> list[str]:
    """
    检测新条目是否使某些旧条目失效。
    简单规则: 如果新条目包含"升级/改为/不再/废弃"等关键词，
    且旧条目包含对应的"之前/原来"等关键词，则旧条目可能被取代。
    """
    superseded = []
    new_lower = new_entry.lower()

    for old in existing_entries:
        old_lower = old.lower()
        for new_kw, old_kws in SUPERSEDE_KEYWORDS.items():
            if new_kw in new_lower:
                for old_kw in old_kws:
                    if old_kw in old_lower:
                        superseded.append(old)
                        break
    return superseded


async def run_memory_compaction(facts_dir: Path) -> dict:
    """
    空闲时整理记忆：删除过期条目，合并重复条目。
    用 LLM 辅助判断条目是否仍然有效。
    """
    stats = {"expired": 0, "superseded": 0, "merged": 0, "total_before": 0, "total_after": 0}

    for fact_file in facts_dir.glob("*.md"):
        content = fact_file.read_text(encoding="utf-8") or ""
        entries = [e.strip() for e in content.split("§") if e.strip()]
        stats["total_before"] += len(entries)

        kept = []
        now = time.time()
        ttl_seconds = DEFAULT_TTL_DAYS * 86400

        for entry in entries:
            # TTL 检查
            ts = extract_timestamp(entry)
            if ts and (now - ts) > ttl_seconds:
                stats["expired"] += 1
                continue
            kept.append(entry)

        # 冲突检测: 后写入的条目可能取代先前的
        final = []
        for i, entry in enumerate(kept):
            superseded_by = []
            for j, later_entry in enumerate(kept):
                if i >= j:
                    continue
                if entry in find_superseded_entries(later_entry, [entry]):
                    superseded_by.append(later_entry)
            if not superseded_by:
                final.append(entry)
            else:
                stats["superseded"] += 1

        stats["total_after"] += len(final)
        fact_file.write_text("§ ".join(final), encoding="utf-8")

    return stats
```

```python
# server/service/turn_runner.py — 空闲时触发整理

async def _on_idle_compact_memory(self):
    """空闲时触发记忆整理。"""
    from context_engine.memory_lifecycle import run_memory_compaction
    from config import MEMORY_DIR

    facts_dir = MEMORY_DIR / "facts"
    if not facts_dir.exists():
        return

    try:
        stats = await run_memory_compaction(facts_dir)
        logger.info("记忆整理完成: {}", stats)
    except Exception as e:
        logger.warning("记忆整理失败: {}", e)
```

### 实施顺序

1. `context_engine/memory_lifecycle.py` 新建模块
2. `agent/tools/memory_tiered.py` 写入时添加时间戳
3. `agent/tools/memory_tiered.py` 写入时触发冲突检测
4. `server/service/turn_runner.py` 空闲时触发整理
5. 测试

### 测试计划

- [ ] 单元测试：TTL 过期检测
- [ ] 单元测试：冲突检测（"升级到 3.12" → "之前用 3.9" 被标记）
- [ ] 集成测试：整理后 facts 文件条目数减少
- [ ] 集成测试：活跃条目不被误删

---

## LT-7 压缩摘要与 TaskFlow 状态协调（来源：openclaw compaction context）

### 现状

压缩生成摘要时不知道 TaskFlow 状态。可能摘要说"用户在调试某个文件"，但 TaskFlow 显示该步骤早已 DONE，当前在进行另一个步骤。摘要与任务状态脱节。

### 目标

压缩摘要 prompt 中注入当前 TaskFlow 的 steps/results 摘要，使 LLM 生成更准确的压缩摘要。

### 涉及文件

| 文件                                 | 操作                           |
| ------------------------------------ | ------------------------------ |
| `agent/middlewares/summarization.py` | 摘要 prompt 注入 TaskFlow 状态 |

### 详细设计

```python
# agent/middlewares/summarization.py — _create_summary() 修改

async def _create_summary(self, messages, budget, session_id, prior_summary=None):
    # === 新增：注入 TaskFlow 状态到摘要 prompt ===
    taskflow_context = await self._get_taskflow_context(session_id)

    prompt_parts = [_SUMMARY_PROMPT_PREFIX]

    # 注入 TaskFlow 状态
    if taskflow_context:
        prompt_parts.append(
            f"\n## 当前任务状态（权威，优先于对话内容）\n{taskflow_context}\n"
        )

    # ... 其余逻辑不变 ...
```

```python
async def _get_taskflow_context(self, session_id: str) -> str:
    """获取当前会话的 TaskFlow 状态摘要，供压缩 prompt 使用。"""
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store

    try:
        # 获取所有活跃 TaskFlow
        flows = await taskflow_store.get_active_flows_by_session(session_id)
        if not flows:
            return ""

        lines = []
        for flow in flows[:3]:
            state = flow.get("state", {})
            steps = state.get("steps", [])
            results = state.get("results", [])

            done_steps = [s for s in steps if s.get("result")]
            pending_steps = [s for s in steps if not s.get("result")]

            lines.append(f"### TaskFlow: {flow['flow_id']} (status={flow['status']})")
            lines.append(f"描述: {state.get('description', '')}")
            if done_steps:
                lines.append(f"已完成步骤: {len(done_steps)}/{len(steps)}")
                for s in done_steps[-2:]:  # 最近2个完成的
                    lines.append(f"  ✓ {s.get('task', '')[:80]}")
            if pending_steps:
                lines.append(f"待处理步骤: {len(pending_steps)}")
                for s in pending_steps[:3]:
                    lines.append(f"  ○ {s.get('task', '')[:80]}")
            if flow.get("wait"):
                lines.append(f"等待中: {flow['wait'].get('reason', '')}")

        return "\n".join(lines)
    except Exception as e:
        logger.debug("获取 TaskFlow 上下文失败: {}", e)
        return ""
```

### 实施顺序

1. `agent/middlewares/summarization.py` 新增 `_get_taskflow_context()`
2. 修改 `_create_summary()` 注入 TaskFlow 状态
3. 测试

### 测试计划

- [ ] 集成测试：压缩摘要包含 TaskFlow 状态
- [ ] 集成测试：摘要中已完成步骤和待处理步骤正确

---

## LT-8 会话间意图连续性（来源：oh-my-openagent session binding + opencode-dev Context Epoch）

### 现状

P1-4 Recall 做的是关键词匹配的模糊召回。但长程任务最常见的场景是用户明确说"继续上次的工作"——这需要精确的**上一会话末尾状态**注入，而非模糊搜索。新会话启动时没有自动注入上一会话的末尾摘要。

### 目标

会话启动时自动注入上一会话的末尾摘要 + 未完成任务列表，实现精确的跨会话意图连续性。

### 涉及文件

| 文件                                          | 操作                       |
| --------------------------------------------- | -------------------------- |
| `context_engine/session_continuity.py` (新建) | 会话连续性管理             |
| `server/service/messages.py`                  | 会话创建时注入连续性上下文 |
| `workspace/prompt_builder.py`                 | 系统提示词注入连续性块     |

### 详细设计

```python
# context_engine/session_continuity.py — 新建

"""
会话间意图连续性：新会话启动时自动注入上一会话的末尾状态。
参考: oh-my-openagent session binding + opencode-dev Context Epoch

与 P1-4 Recall 的区别:
- P1-4 Recall: 模糊关键词搜索历史对话（"你可能之前聊过XXX"）
- LT-8 连续性: 精确注入上一会话的末尾摘要（"你上次在做YYY，进度到了ZZZ"）
"""

import json
from pathlib import Path
from context_engine.store.core import get_messages_by_lastest_n_turns

_CONTINUITY_DIR = Path("data/session_continuity")


def save_session_end_state(session_id: str, channel_id: str, chat_id: str,
                           summary: str, taskflow_ids: list[str]) -> None:
    """
    会话结束时保存末尾状态。
    在 session end / archive / clear 时调用。
    """
    _CONTINUITY_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{channel_id}:{chat_id}"
    state = {
        "last_session_id": session_id,
        "ended_at": __import__("datetime").datetime.now().isoformat(),
        "summary": summary,  # 最后一轮 AI 回复的摘要
        "taskflow_ids": taskflow_ids,  # 关联的 TaskFlow
    }
    path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_last_session_state(channel_id: str, chat_id: str) -> dict | None:
    """
    获取上一会话的末尾状态。
    在新会话启动时调用，注入系统提示词。
    """
    key = f"{channel_id}:{chat_id}"
    path = _CONTINUITY_DIR / f"{_safe_filename(key)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_continuity_prompt(channel_id: str, chat_id: str) -> str:
    """
    构建会话连续性提示词块。
    """
    state = get_last_session_state(channel_id, chat_id)
    if not state:
        return ""

    parts = ["## 上次会话状态（会话连续性）"]
    if state.get("summary"):
        parts.append(f"上次对话末尾: {state['summary'][:500]}")
    if state.get("taskflow_ids"):
        parts.append(f"关联任务: {', '.join(state['taskflow_ids'][:3])}")
    parts.append(
        "如果用户说'继续'或未指定新任务，请优先参考上述上下文继续上次的工作。"
    )
    return "\n".join(parts)


async def auto_save_on_session_end(session_id: str, channel_id: str,
                                    chat_id: str) -> None:
    """
    会话结束时自动提取末尾摘要并保存。
    从最近 N 轮对话中提取最后一轮 AI 回复作为摘要。
    """
    try:
        messages = await get_messages_by_lastest_n_turns(session_id, n_turns=3)
        # 找最后一轮 AI 回复
        last_ai = None
        for msg in reversed(messages):
            if msg.get("role") == "ai":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                last_ai = content[:500] if content else None
                break

        # 获取关联的 TaskFlow
        from agent.tools.taskflow.registry import store_sqlite as taskflow_store
        flows = await taskflow_store.get_active_flows_by_channel(channel_id, chat_id)
        taskflow_ids = [f["flow_id"] for f in flows]

        save_session_end_state(
            session_id=session_id,
            channel_id=channel_id,
            chat_id=chat_id,
            summary=last_ai or "",
            taskflow_ids=taskflow_ids,
        )
    except Exception as e:
        logger.warning("保存会话末尾状态失败: {}", e)


def _safe_filename(key: str) -> str:
    """将 channel:chat 键转为安全文件名。"""
    return key.replace(":", "_").replace("/", "_").replace("\\", "_")
```

```python
# workspace/prompt_builder.py — 注入连续性

def build_system_prompt(session_id, channel_id, chat_id, ...):
    ...  # 现有逻辑 ...

    # === 新增：会话连续性 ===
    from context_engine.session_continuity import build_continuity_prompt
    continuity = build_continuity_prompt(channel_id, chat_id)
    if continuity:
        system_prompt += "\n\n" + continuity

    return system_prompt
```

```python
# server/service/messages.py — 会话结束时自动保存

async def clear_session(session_id, channel_id, chat_id):
    # === 新增：保存末尾状态 ===
    from context_engine.session_continuity import auto_save_on_session_end
    await auto_save_on_session_end(session_id, channel_id, chat_id)

    # ... 现有清理逻辑 ...
```

### 实施顺序

1. `context_engine/session_continuity.py` 新建模块
2. `workspace/prompt_builder.py` 注入连续性提示词
3. `server/service/messages.py` 会话结束时自动保存
4. 测试

### 测试计划

- [ ] 单元测试：`save/get_last_session_state()` 读写正确
- [ ] 单元测试：`build_continuity_prompt()` 格式正确
- [ ] 集成测试：新会话系统提示词包含上次末尾状态
- [ ] 集成测试：会话结束后状态正确保存
- [ ] 集成测试：用户说"继续"时模型参考上次上下文

---

## 实施总览

### 通用记忆层（P0-P2）

| 编号 | 设计点                    | 优先级 | 涉及文件数 | 新建文件数 | 预估工时 |
| ---- | ------------------------- | ------ | ---------- | ---------- | -------- |
| P0-1 | 预压缩 Memory Flush       | P0     | 4          | 1          | 1-2天    |
| P0-2 | 压缩失败冷却持久化        | P0     | 3          | 0          | 0.5天    |
| P0-3 | 压缩锁防并发分裂          | P0     | 3          | 1          | 1天      |
| P0-4 | 工具输出一行摘要替代      | P0     | 1          | 0          | 0.5天    |
| P1-1 | 压缩检查点回溯            | P1     | 4          | 0          | 2-3天    |
| P1-2 | 增量幂等持久化标记        | P1     | 2          | 0          | 1天      |
| P1-3 | 活跃上下文投影            | P1     | 3          | 0          | 1-2天    |
| P1-4 | Recall 自动跨会话记忆召回 | P1     | 4          | 3          | 2-3天    |
| P1-5 | 转录树 + 消息分支         | P1     | 3          | 0          | 2天      |
| P2-1 | 事件溯源迁移              | P2     | 3+         | 2          | 5-7天    |
| P2-2 | Context Epoch             | P2     | 2          | 0          | 2-3天    |
| P2-3 | 双水位游标 Facts 提取     | P2     | 4          | 3          | 3-4天    |
| P2-4 | steer/queue 双投递        | P2     | 2          | 0          | 3天      |
| P2-5 | 向量嵌入语义搜索          | P2     | 4          | 3          | 3-4天    |

### 长程任务记忆编排层（LT-1 ~ LT-8）

| 编号 | 设计点                       | 优先级 | 涉及文件数 | 新建文件数 | 预估工时 | 依赖项     |
| ---- | ---------------------------- | ------ | ---------- | ---------- | -------- | ---------- |
| LT-1 | 分层记忆存储                 | P0     | 3          | 1          | 2天      | 无         |
| LT-2 | 跨会话 TaskFlow 自动续接     | P0     | 3          | 0          | 1天      | 无         |
| LT-3 | 压缩摘要链式退化防护         | P1     | 2          | 0          | 1-2天    | LT-1, LT-2 |
| LT-4 | TaskFlow 完成触发知识提取    | P1     | 3          | 1          | 1-2天    | LT-1, P0-1 |
| LT-5 | 子代理记忆回流               | P1     | 2          | 0          | 1天      | LT-1       |
| LT-6 | 记忆过期与清理策略           | P1     | 3          | 1          | 2天      | LT-1       |
| LT-7 | 压缩摘要与 TaskFlow 状态协调 | P0     | 1          | 0          | 0.5天    | LT-2       |
| LT-8 | 会话间意图连续性             | P0     | 3          | 1          | 1-2天    | LT-2       |

### 依赖关系图

```
LT-1 (分层记忆) ──┬── LT-3 (摘要退化防护)
                  ├── LT-4 (TaskFlow知识提取)
                  ├── LT-5 (子代理回流)
                  └── LT-6 (记忆过期清理)

LT-2 (TaskFlow续接) ──┬── LT-3 (摘要退化防护)
                      ├── LT-7 (摘要与TaskFlow协调)
                      └── LT-8 (会话间连续性)

P0-1 (Memory Flush) ── LT-4 (TaskFlow知识提取)
P1-4 (Recall) ── 可与 LT-8 互补（模糊召回 + 精确续接）
```

### 推荐实施顺序

```
第一批（P0，无依赖）:
  LT-1 分层记忆存储        (2天)
  LT-2 TaskFlow自动续接    (1天)
  LT-7 摘要与TaskFlow协调  (0.5天)
  LT-8 会话间连续性         (1-2天)
  P0-1~P0-4 通用P0项       (3-4天)

第二批（P1，依赖第一批）:
  LT-3 摘要退化防护         (1-2天) ← 依赖 LT-1, LT-2
  LT-4 TaskFlow知识提取     (1-2天) ← 依赖 LT-1, P0-1
  LT-5 子代理记忆回流       (1天)   ← 依赖 LT-1
  LT-6 记忆过期清理         (2天)   ← 依赖 LT-1
  P1-1~P1-5 通用P1项       (8-11天)

第三批（P2，长期）:
  P2-1~P2-5 通用P2项       (16-21天)
```

> **完整长程任务记忆能力需要**：通用记忆层 14 项 + 长程编排层 8 项 = 共 22 项。
> 通用层解决"记忆怎么存、怎么压、怎么搜"，编排层解决"记忆怎么和 TaskFlow 联动、怎么跨会话续接、怎么不退化、怎么过期"。
> 两层缺一不可——通用层是基础设施，编排层是长程任务的智能核心。
