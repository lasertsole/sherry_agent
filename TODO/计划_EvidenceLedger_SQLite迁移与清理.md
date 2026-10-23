# 计划：Evidence Ledger 清理机制（参考 hermes-agent SQLite + 保留期方案）

- 建档日期：2026-10-23
- 状态：**待评审（v2，重写）**。v1 设计了 JSONL 原子重写清理方案，复杂度高；核实 hermes-agent 后发现其用 SQLite + 惰性保留期清理，行级 DELETE 天然支持清理，不需要读全量重写。本版改用 hermes 方案。
- **文档范围**：(1) evidence ledger 从 JSONL 迁移到 SQLite（WAL 模式）；(2) 参照 hermes 的三层保留期清理（age TTL + per-session 上限 + 全局上限），每次记录时惰性触发；(3) session 删除时 SQLite DELETE 清理；(4) boot 孤儿 sweep 清理。
- **硬约束**：现有 evidence ledger API 兼容（`read_all`/`read_for_keys`/`for_session`/`mark_stale_for_path` 签名不变，实现换底）；fail-open；并发安全（WAL + busy_timeout）。
- 关联代码：
  - 改造对象：
    - `agent/tools/pub_base/evidence_ledger.py:30-162`（EvidenceLedger 类——JSONL→SQLite 重写）
    - `server/service/session_cleanup_service.py:53-147`（`purge_session_data`——增 evidence 清理步骤）
    - `server/service/orphan_session_sweep.py`（boot sweep——增 evidence 清理）
  - hermes-agent 参考（已核实）：
    - `agent/verification_evidence.py:58-122`（SQLite schema + WAL + busy_timeout）
    - `agent/verification_evidence.py:321-380`（`_prune_old_events` 三层保留期——age/session/global）
    - `agent/verification_evidence.py:430-492`（`record_terminal_result`——记录时触发清理）
    - `agent/verification_evidence.py:495-546`（`mark_workspace_edited`——编辑标 stale）
    - `agent/verification_evidence.py:549-618`（`verification_status`——时间戳 staleness）
  - 关联 TODO：
    - `计划_跨Subagent文件冲突检测.md` §2——cross-session staleness 依赖共享文件，清理是 §2 的隐含前置

---

## 0. 背景（已核实）

### 0.1 sherry 现状

| 维度 | sherry | hermes-agent |
|---|---|---|
| 存储 | JSONL（`evidence-ledger.jsonl`，append-only） | SQLite（`verification_evidence.db`，WAL） |
| scope | per-session_id 单维度 | per-(session_id, project_root) 双维度 |
| staleness | substring 匹配（`file_path in command`） | 时间戳比较（`last_edit_at > evidence.created_at`） |
| 完成门 | 硬阻断（Gate B 阻止 finish） | 软门（verify-on-stop nudge，续写消息） |
| 生命周期清理 | **无** | ✅ 三层保留期（30 天 TTL + per-session 100 条 + 全局 1 万条），惰性触发 |
| 跨 session 失效 | ❌ | ❌ |

sherry 的 JSONL 存储**不支持行级 DELETE**——清理只能读全量→过滤→原子重写，这是 v1 清理计划复杂度高的根本原因。hermes 用 SQLite，`DELETE FROM ... WHERE created_at < ?` 一步搞定。

### 0.2 hermes 的三层保留期设计（`verification_evidence.py:27-29, 321-380`）

```python
_MAX_EVIDENCE_AGE_DAYS = 30               # 30 天 TTL
_MAX_EVENTS_PER_SESSION_ROOT = 100         # 每 (session, root) 最多 100 条
_MAX_TOTAL_UNREFERENCED_EVENTS = 10_000   # 全局无引用事件上限
```

`_prune_old_events` 在每次 `record_terminal_result` 调用时**惰性触发**——不需要独立的清理任务/定时器/周期压缩。清理是记录时的顺带行为，零额外调度。

### 0.3 为什么迁移到 SQLite

| 维度 | JSONL（当前） | SQLite（迁移后） |
|---|---|---|
| 行级删除 | ❌ 只能读全量→过滤→原子重写 | ✅ `DELETE WHERE ...` 一步 |
| 并发写 | 排他锁（`file_write_lock`）序列化所有写 | WAL 模式——读写并发，`busy_timeout=5000` 自动重试 |
| 查询过滤 | `read_all()` 全量读再 Python 过滤 | `SELECT ... WHERE session_id = ?` 索引查询 |
| 保留期清理 | 每次清理都要重写整个文件 | `DELETE WHERE created_at < ?` O(log N) |
| session 删除清理 | 原子重写（锁持有期间阻塞所有写） | `DELETE WHERE session_id = ?` |
| 文件大小 | 只增不减，无上限 | 保留期+上限控制 |

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：API 兼容 | `read_all`/`read_for_keys`/`read_for_session`/`for_session`/`mark_stale_for_path`/`append` 签名不变，实现换底 | 调用方零改动 |
| **F2**：fail-open | 清理/迁移失败不阻断 session 删除/boot/记录 | 异常只 log |
| **F3**：并发安全 | WAL 模式 + `busy_timeout=5000`——读写并发，写写自动重试 | 并发追加不丢 |
| **F4**：数据迁移 | 现有 JSONL 数据迁移到 SQLite（首次启动时一次性） | 旧 JSONL 行全部在 SQLite 中 |
| **F5**：配置可控 | 保留期参数可配置 | feature flag off 时行为同当前（只不清理） |

---

## 2. 任务一：JSONL → SQLite 迁移

### 2.1 目标

将 evidence ledger 存储从 JSONL 迁移到 SQLite，API 签名不变，实现换底。

### 2.2 改动清单

- [ ] **2.2.1** `agent/tools/pub_base/evidence_ledger.py` 重写 `EvidenceLedger` 类：

  **Schema**（参考 hermes `verification_evidence.py:73-122`）：
  ```sql
  CREATE TABLE IF NOT EXISTS evidence_events (
      rowid INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL,          -- UTC ISO（兼容现有 append 时 stamp）
      session_id TEXT NOT NULL,
      event TEXT,                       -- None=验证行, "stale"=失效行
      file_path TEXT,                   -- stale 行的 file_path
      command TEXT,                     -- 验证行的 command
      status TEXT,                      -- 验证行的 status (pass/fail)
      exit_code INTEGER,                -- 验证行的 exit_code
      source_session TEXT,              -- cross-session stale 的写入方（§2 预留）
      scope TEXT DEFAULT 'session',     -- 'session' | 'cross_session'（§2 预留）
      payload_json TEXT                 -- 其他字段 JSON（兼容未来扩展）
  );
  CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence_events(session_id);
  CREATE INDEX IF NOT EXISTS idx_evidence_timestamp ON evidence_events(timestamp);
  ```

  **DB 路径**：`resolve_evidence_ledger_path()` 改为返回 `.db` 路径（`SRC_DIR/data/evidence-ledger.db`），或复用现有路径去 `.jsonl` 后缀加 `.db`。

  **WAL + busy_timeout**（参考 hermes `:62-70`）：
  ```python
  PRAGMA journal_mode=WAL;
  PRAGMA busy_timeout=5000;
  ```

- [ ] **2.2.2** API 实现换底（签名不变）：
  - `append(entry)` → `INSERT INTO evidence_events (...)`（参数化，不再需要 `file_write_lock`——SQLite 自己处理并发）
  - `read_all()` → `SELECT * FROM evidence_events ORDER BY rowid`
  - `read_for_keys(session_keys)` → `SELECT * FROM evidence_events WHERE session_id IN (...) ORDER BY rowid`（索引查询，不再全量读）
  - `read_for_session(session_key)` → `SELECT * FROM evidence_events WHERE session_id = ?`
  - `mark_stale_for_path(file_path, session_key)` → `INSERT ... (event='stale', file_path=...)`
  - `for_session(session_key)` → 不变（返回 `SessionEvidenceLedger` 视图，内部调 `read_for_session`）

- [ ] **2.2.3** 一次性数据迁移：
  ```python
  def _migrate_jsonl_to_sqlite(self):
      """首次启动时将旧 JSONL 数据导入 SQLite。"""
      jsonl_path = cls.LEDGER_PATH.replace(".db", ".jsonl")
      if not os.path.exists(jsonl_path):
          return  # 无旧文件，跳过
      rows = []  # 读 JSONL
      with open(jsonl_path, encoding="utf-8") as f:
          for line in f:
              row = json.loads(line.strip())
              rows.append(row)
      if rows:
          # 批量 INSERT
          conn.executemany("INSERT INTO evidence_events (...) VALUES (...)", rows)
          conn.commit()
      # 迁移成功后重命名旧文件（不删，留备份）
      os.rename(jsonl_path, jsonl_path + ".migrated")
  ```
  - 在 `_ensure()`（首次初始化 schema 时）检查旧 JSONL 是否存在，存在则迁移
  - 迁移失败不阻断——fail-open，旧 JSONL 保留，SQLite 空表开始（无历史 evidence，不影响新记录）

- [ ] **2.2.4** 移除 `file_write_lock` 依赖：
  - `append` 不再需要 `file_write_lock`——SQLite WAL + busy_timeout 处理并发
  - `evidence_ledger.py:26` 的 `from agent.tools.pub_base import file_write_lock` 可移除

**验收**：F1 + F3 + F4。新建 `tests/agent/tools/pub_base/test_evidence_sqlite.py`（API 兼容、并发写、数据迁移）。

---

## 3. 任务二：保留期清理（参照 hermes 三层策略）

### 3.1 目标

参照 hermes 的 `_prune_old_events`，在每次 `append` 时惰性触发保留期清理。

### 3.2 改动清单

- [ ] **3.2.1** 新增配置 `config/features/agent_side/evidence_ledger.py`：
  ```python
  from typing import TypedDict

  class EvidenceLedgerConfig(TypedDict):
      # 保留期（参照 hermes verification_evidence.py:27-29）
      max_age_days: int            # 默认 30——超龄行删除
      max_events_per_session: int   # 默认 100——每 session 保留最新 N 条
      max_total_events: int        # 默认 10000——全局上限
      # 清理开关
      pruning_enabled: bool        # 默认 True——False 时不清理（退化为只增不减）
      # session 删除清理
      purge_on_session_delete: bool # 默认 True
      # boot sweep 清理
      purge_on_boot_sweep: bool     # 默认 True

  EVIDENCE_LEDGER: EvidenceLedgerConfig = {
      "max_age_days": 30,
      "max_events_per_session": 100,
      "max_total_events": 10000,
      "pruning_enabled": True,
      "purge_on_session_delete": True,
      "purge_on_boot_sweep": True,
  }
  ```

- [ ] **3.2.2** `EvidenceLedger` 新增 `_prune_old_events` 方法（参照 hermes `:321-380`）：
  ```python
  @classmethod
  def _prune_old_events(cls, conn) -> int:
      """三层保留期清理——在每次 append 后惰性调用。

      (1) age: DELETE WHERE timestamp < datetime('now', '-N days')
      (2) per-session: 每 session 保留最新 max_events_per_session 条，多余 DELETE
      (3) global: 总行数 > max_total_events 时删最旧的
      """
      if not EVIDENCE_LEDGER["pruning_enabled"]:
          return 0
      removed = 0
      # (1) age TTL
      cursor = conn.execute(
          "DELETE FROM evidence_events WHERE timestamp < ?",
          ((datetime.now(UTC) - timedelta(days=EVIDENCE_LEDGER["max_age_days"])).isoformat(),),
      )
      removed += cursor.rowcount
      # (2) per-session cap
      conn.execute(
          "DELETE FROM evidence_events WHERE rowid IN ("
          "  SELECT rowid FROM evidence_events e1 WHERE ("
          "    SELECT COUNT(*) FROM evidence_events e2 "
          "    WHERE e2.session_id = e1.session_id AND e2.rowid > e1.rowid"
          "  ) >= ? )",
          (EVIDENCE_LEDGER["max_events_per_session"],),
      )
      # (3) global cap
      conn.execute(
          "DELETE FROM evidence_events WHERE rowid IN ("
          "  SELECT rowid FROM evidence_events ORDER BY rowid DESC "
          "  LIMIT -1 OFFSET ? )",
          (EVIDENCE_LEDGER["max_total_events"],),
      )
      conn.commit()
      return removed
  ```

- [ ] **3.2.3** `append` 方法在 INSERT 后调 `_prune_old_events`：
  ```python
  @classmethod
  def append(cls, entry: dict) -> None:
      record = dict(entry)
      record["timestamp"] = datetime.now(UTC).isoformat()
      with cls._get_conn() as conn:
          conn.execute("INSERT INTO evidence_events (...) VALUES (...)", ...)
          conn.commit()
          cls._prune_old_events(conn)  # 惰性清理
  ```
  - 清理是 INSERT 的顺带行为，不需要独立定时器/调度器
  - fail-open：`_prune_old_events` 异常只 log，不影响 INSERT

**验收**：F2 + F5。新建 `tests/agent/tools/pub_base/test_evidence_pruning.py`（age TTL、per-session cap、global cap、pruning_enabled=False 时不清理）。

---

## 4. 任务三：session 删除清理

### 4.1 目标

session 删除时从 SQLite 清理其 evidence 行——`DELETE WHERE session_id = ?`，一步完成。

### 4.2 改动清单

- [ ] **4.2.1** `EvidenceLedger` 新增 `purge_session(session_id: str) -> int`：
  ```python
  @classmethod
  def purge_session(cls, session_id: str) -> int:
      """删除一个 session 的所有 evidence 行。fail-open。"""
      try:
          with cls._get_conn() as conn:
              cursor = conn.execute(
                  "DELETE FROM evidence_events WHERE session_id = ?",
                  (session_id,),
              )
              conn.commit()
              return cursor.rowcount
          except Exception:
              logger.warning("evidence purge_session failed (fail-open)",
                          session_id=session_id, exc_info=True)
              return 0
  ```
  - 对比 v1：不需要读全量→过滤→原子重写，一条 DELETE 搞定
  - 索引 `idx_evidence_session` 保证 O(log N + M)（M = 匹配行数）

- [ ] **4.2.2** `server/service/session_cleanup_service.py:53-147` `purge_session_data` 增步骤 (8)：
  ```python
  # (8) Evidence ledger — DELETE this session's rows (best-effort).
  try:
      from agent.tools.pub_base.evidence_ledger import EvidenceLedger
      removed = await asyncio.to_thread(EvidenceLedger.purge_session, session_id)
      if removed:
          logger.debug(f"Deleted {removed} evidence row(s) for session_id={session_id}")
  except Exception as e:
      logger.warning(f"Failed to purge evidence for session_id={session_id}: {e}")
  ```
  - 插入在步骤 (6) `clear_all_register_sessions` 之后
  - `asyncio.to_thread` 避免 SQLite IO 阻塞事件循环

- [ ] **4.2.3** 子 agent 级联清理自动覆盖——`purge_subagent_subtree_for_session`（`:150-235`）对每个 child 调 `purge_session_data`（`:206`），步骤 (8) 自动对每个 child 生效

**验收**：F2。session 删除后该 session 的 evidence 行从 SQLite 中消失。

---

## 5. 任务四：boot 孤儿 sweep 清理

### 5.1 目标

boot 时 `orphan_session_sweep.py` 删除无主 session 子树时，同步清理其 evidence。

### 5.2 改动清单

- [ ] **5.2.1** `server/service/orphan_session_sweep.py`：确认其删除路径是否经过 `purge_session_data`：
  - 如果是：步骤 (8) 自动覆盖，无需额外改动
  - 如果不是（直接 rmtree）：在 rmtree 后补 `EvidenceLedger.purge_session(orphan_session_id)`
- [ ] **5.2.2** 额外清理：boot 时扫描 SQLite 中所有 `session_id`，对照现存 session 列表，清理已不存在的 session 的 evidence：
  ```python
  # boot sweep 额外步骤：
  # SELECT DISTINCT session_id FROM evidence_events
  # 对照 session 列表，不存在的 → purge_session
  ```
  - 这捕获了"session 被 API 删除但未经过 `purge_session_data`"的遗漏
  - fail-open

**验收**：F2。boot 后孤儿 session 的 evidence 行已清除。

---

## 6. 执行顺序

```
§2 JSONL → SQLite 迁移（1.5-2 天）  ← 基础，后续都依赖
  ├─ SQLite schema + WAL + busy_timeout
  ├─ API 换底（签名不变）
  ├─ 一次性数据迁移
  └─ 移除 file_write_lock 依赖
      │
      ↓ （SQLite 行级 DELETE 就绪）
§3 保留期清理（0.5-1 天）           ← hermes 范式，惰性触发
  ├─ config/features/evidence_ledger.py
  ├─ _prune_old_events（三层：age + per-session + global）
  └─ append 后调 _prune_old_events
      │
      ↓ （可与 §3 并行）
§4 session 删除清理（0.5 天）       ← DELETE WHERE session_id=?
  ├─ EvidenceLedger.purge_session
  └─ purge_session_data 增步骤 (8)
      │
      ↓
§5 boot 孤儿 sweep（0.5 天）        ← 兜底

总工程量：3-4 天
```

**对比 v1**：v1 是 JSONL + 原子重写，需要 2.5-3 天且机制复杂（读全量→过滤→temp→os.replace+排他锁）；v2 是 SQLite + DELETE，3-4 天但含存储迁移，清理本身只需一条 SQL。长期维护成本远低于 v1。

---

## 7. 验收标准

### 7.1 硬约束验收

- [ ] **F1**：调用方零改动——`read_all`/`read_for_keys`/`for_session`/`mark_stale_for_path`/`append` 签名不变。
- [ ] **F2**：清理/迁移失败不阻断 session 删除/boot/记录。
- [ ] **F3**：WAL + busy_timeout——并发读写不冲突，并发写自动重试。
- [ ] **F4**：旧 JSONL 数据全部迁移到 SQLite（迁移后旧文件保留 `.migrated` 后缀）。
- [ ] **F5**：`pruning_enabled=False` 时不清理（退化为只增不减）。

### 7.2 功能验收

- [ ] 旧 JSONL 行在 SQLite 中可查到（数据迁移）。
- [ ] 30 天前的 evidence 行被自动删除（age TTL）。
- [ ] 每 session 超过 100 条的旧行被删除（per-session cap）。
- [ ] 全局超过 10000 条时最旧行被删除（global cap）。
- [ ] session 删除后该 session 的 evidence 行从 SQLite 中消失。
- [ ] boot sweep 后孤儿 session 的 evidence 清除。
- [ ] 并发追加 + 清理不冲突（WAL）。

### 7.3 回归验收

- [ ] 现有 `tests/agent/tools/pub_base/` 全套通过（API 兼容）。
- [ ] 现有 `tests/server/test_session_cleanup*.py` 通过。
- [ ] evidence 相关的 completion judge / step judge / taskflow_finish 行为不变。

---

## 8. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| per-plan ledger | 与 §2 cross-session staleness 目标互斥；hermes 实践证明共享+session key + 保留期清理够用 |
| hermes 的 project-root 维度 | sherry 的 project_dir 是 session 级绑定（`runtime/session/project_dir.py`），session_id 已隐含项目上下文，不需要额外 root 维度 |
| hermes 的时间戳 staleness | sherry 的 substring 匹配虽有问题但已落地，改 staleness 机制是独立改造（另见 §2 计划），不在本计划范围 |
| hermes 的 verify-on-stop 软门 | sherry 的 Gate B 硬阻断是现有行为，不改完成门策略 |
| hermes 的 kanban swarm verifier | 那是 hermes 的 prompt-instructed LLM 门，sherry 有自己的 completion judge + step judge |

---

## 9. 关键参考

### sherry 内部（改造对象）
- `agent/tools/pub_base/evidence_ledger.py:30-162`（EvidenceLedger 类——JSONL→SQLite 重写）
- `agent/tools/pub_base/evidence_recorder.py:84-95,98-151`（`invalidates_evidence`——不需改，调 `mark_stale_for_path` 签名不变）
- `agent/tools/taskflow/evidence_collector.py:15-40`（staleness 派生——不需改，调 `read_for_keys` 签名不变）
- `server/service/session_cleanup_service.py:53-147`（`purge_session_data`——增步骤 8）
- `server/service/orphan_session_sweep.py`（boot sweep——接入点）
- `config/path.py:354-361`（`resolve_evidence_ledger_path`——路径改 .db）

### hermes-agent 参考（已核实）
- `agent/verification_evidence.py:27-29`（三层保留期常量：30 天 / 100 条 / 1 万条）
- `agent/verification_evidence.py:58-70`（SQLite + WAL + busy_timeout=5000）
- `agent/verification_evidence.py:73-122`（三表 schema：events + state + meta）
- `agent/verification_evidence.py:321-380`（`_prune_old_events`——惰性清理范式）
- `agent/verification_evidence.py:430-492`（`record_terminal_result`——记录时触发清理）
- `agent/verification_evidence.py:549-618`（`verification_status`——时间戳 staleness 参考，不在本计划范围）

### 关联 TODO
- `计划_跨Subagent文件冲突检测.md` §2——cross-session staleness 依赖共享文件；本计划迁移到 SQLite 后 §2 的 `mark_stale_for_path_all_sessions` 改为 `SELECT DISTINCT session_id` 扫描，比 JSONL `read_all()` 快得多
