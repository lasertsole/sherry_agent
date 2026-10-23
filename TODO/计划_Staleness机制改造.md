# 计划：Staleness 机制改造——从 substring 匹配到 state 表 + 文件追踪

- 建档日期：2026-10-23
- 状态：**待评审（v2，重写）**。v1 方案 B 用 in-memory 累积器 + evidence 行 `modified_files` 字段；核实 hermes 后发现其有 `verification_state` 表（`changed_paths_json` + `last_edit_at`）但**只用时间戳做 staleness，没用 changed_paths**。v2 改为学 hermes 的 state 表结构，但 staleness 判定用 `changed_paths` 做精确匹配——这是 hermes 记了但没用上的数据。
- **文档范围**：(1) 评估 hermes 纯时间戳 + 三种 sherry 适配方案；(2) 推荐方案 B'（state 表 + changed_paths 精确匹配）；(3) 性价比分析。
- **硬约束**：不破坏现有 `_is_stale` 调用方；Gate B/C 硬阻断行为不变；fail-open；向后兼容旧 evidence 行。
- 关联代码：
  - 改造对象：
    - `agent/tools/taskflow/evidence_collector.py:15-21`（`_is_stale`）
    - `agent/tools/pub_base/evidence_ledger.py:30-162`（EvidenceLedger——增 state 表）
    - `agent/tools/pub_base/evidence_recorder.py:48-95,98-151`（`record_verification_evidence` + `invalidates_evidence`）
  - hermes 参考（已核实）：
    - `agent/verification_evidence.py:100-111`（`verification_state` 表 schema：`changed_paths_json` + `last_edit_at`）
    - `agent/verification_evidence.py:495-546`（`mark_workspace_edited`——累积 changed_paths + 设 last_edit_at）
    - `agent/verification_evidence.py:549-618`（`verification_status`——只用时间戳，**不用 changed_paths**）
  - 关联 TODO：
    - `计划_跨Subagent文件冲突检测.md` §2——cross-session staleness 依赖 `_is_stale`
    - `计划_EvidenceLedger_SQLite迁移与清理.md`——SQLite 迁移先做，state 表是同库新表

---

## 0. 背景（已核实）

### 0.1 sherry 当前 staleness 机制

`evidence_collector.py:15-21`：

```python
def _is_stale(index, row, stale_events):
    command = str(row.get("command") or "")
    return any(
        stale_index > index
        and str(event.get("file_path") or "") in command  # substring 匹配
        for stale_index, event in stale_events
    )
```

**匹配规则**：stale 事件的 `file_path` 必须是 evidence 行 `command` 字段的子串。

### 0.2 盲区

| 场景 | A 的验证命令 | B 写的文件 | `file_path in command` | 结果 |
|---|---|---|---|---|
| B 写 A 的测试文件 | `pytest tests/test_login.py` | `tests/test_login.py` | ✅ | 正确 |
| B 写源文件，A 跑测试 | `pytest tests/test_login.py` | `src/auth/login.py` | ❌ | **盲区** |
| B 写依赖模块 | `pytest tests/test_login.py` | `src/utils/auth.py` | ❌ | **盲区** |

### 0.3 hermes 记了 changed_paths 但没用

hermes 的 `verification_state` 表（`verification_evidence.py:100-111`）：

```sql
CREATE TABLE verification_state (
    session_id TEXT NOT NULL,
    root TEXT NOT NULL,
    last_event_id INTEGER,           -- 指向最新验证事件
    last_edit_at TEXT,               -- 最后编辑时间
    changed_paths_json TEXT DEFAULT '[]',  -- 编辑过的文件列表（累积，上限 200）
    PRIMARY KEY (session_id, root)
)
```

`mark_workspace_edited`（`:495-546`）在文件写入时：
1. 累积 `changed_paths_json`（`merged = sorted((existing | set(changed_paths)))[-200:]`，`:532`）
2. 设 `last_edit_at = now`

但 `verification_status`（`:608-611`）的 staleness 判定**只看时间戳**：

```python
if state["last_edit_at"] and state["last_edit_at"] > evidence["created_at"]:
    status = "stale"
```

**`changed_paths_json` 被 `verification_status` 返回给调用方（`:617`）但 NOT 参与判定**——是信息性的。hermes 记了文件列表但没用它做精确 staleness，只用时间戳做粗粒度判定。

**本计划利用这块"记了但没用"的数据**：state 表记录 `changed_paths`，staleness 用 `changed_paths` 做精确匹配——比 hermes 的纯时间戳精确，比 sherry 的 substring 无盲区。

---

## 1. 三种方案对比

### 方案 A：纯时间戳（hermes 范式）

**机制**：state 表记 `last_edit_at`，staleness = `last_edit_at > evidence.timestamp`。
**盲区**：无。
**误报**：极高——任何编辑标记所有 evidence stale。
**对 sherry Gate B 硬阻断**：致命——B 写任何文件 → 所有 evidence stale → 连锁阻断。
**结论**：sherry 硬阻断下不可行。

### 方案 B'：state 表 + changed_paths 精确匹配（推荐）

**机制**：
- 新增 `verification_state` 表（per session_id）：`changed_paths_json`（编辑过的文件列表）+ `last_edit_at`
- 文件写入时：累积 `changed_paths_json` + 设 `last_edit_at`（学 hermes `mark_workspace_edited`）
- 验证命令记录时：把当前 `changed_paths_json` **快照到 evidence 行**的 `modified_files` 字段，然后**重置** state 的 changed_paths（学 hermes `record_terminal_result` 重置 `last_edit_at = NULL, changed_paths = []`）
- `_is_stale`：stale 事件的 `file_path` 在 evidence 行的 `modified_files` 集合中 → stale（**不用 substring，不用时间戳，用文件集合精确匹配**）

**盲区消除**：
```
A 写 src/auth/login.py → state.changed_paths = ["src/auth/login.py"]
A 跑 pytest tests/test_login.py → evidence 行 modified_files = ["src/auth/login.py"]（快照）
                                  → state.changed_paths 重置为 []
B 写 src/auth/login.py → stale 事件 file_path = "src/auth/login.py"
_is_stale: "src/auth/login.py" in ["src/auth/login.py"] → ✅ True（精确匹配）
```

**对 §2 cross-session**：
- B 写 F → stale 事件 `file_path = F`
- cross-session 扫描：查所有 session 的 evidence 行，凡 F 在其 `modified_files` 中 → stale
- SQLite 下：`SELECT * FROM evidence_events WHERE ? IN (modified_files)`（索引查询）

**对比 v1 方案 B（in-memory 累积器）的改进**：

| 维度 | v1 方案 B（in-memory） | v2 方案 B'（state 表） |
|---|---|---|
| 累积器存储 | in-memory `dict[str, set[str]]` | SQLite `verification_state` 表 |
| 持久性 | 进程重启丢失（但已 stamp 的 evidence 行不受影响） | 进程重启不丢（SQLite 持久化） |
| `last_edit_at` | 无 | ✅ 有（可作 timestamp fallback 或 UI 展示） |
| `changed_paths` 可见性 | 不可见（stamp 后丢弃） | ✅ state 表可查（UI 可展示"改了哪些文件"） |
| 与 hermes 对齐 | 不对齐 | ✅ schema 对齐（future 可直接移植 hermes 模式） |
| 与 SQLite 迁移计划兼容 | 兼容（evidence 行增列） | 更兼容（state 表是同库新表） |

### 方案 C：改进 substring（最小改动）

`_is_stale` 从 `file_path in command` 改为 `Path(file_path).stem in command`。
**结论**：脆弱（假阳性），不根治，作为 interim 可选。

---

## 2. 性价比分析

| 维度 | 方案 A（纯时间戳） | 方案 B'（state + changed_paths） | 方案 C（stem 匹配） |
|---|---|---|---|
| 工程量 | 2-3 天 | 3-4 天 | 0.5 天 |
| 盲区消除 | ✅ 完全 | ✅ 完全 | ⚠️ 部分 |
| 误报率 | 极高 | 低（精确文件匹配） | 中（stem 碰巧匹配） |
| Gate B 硬阻断兼容 | ❌ 致命（连锁阻断） | ✅ 可控 | ✅ 可控 |
| 与 hermes 对齐 | 部分（时间戳部分） | ✅ state 表 schema 对齐 | ❌ |
| `changed_paths` UI 可见 | N/A | ✅ state 表可查 | N/A |
| `last_edit_at` 可用 | ✅ | ✅（可作 fallback） | ❌ |
| 复杂度 | 中 | 中（state 表 + 快照/重置逻辑） | 低 |
| 维护负担 | 低 | 中 | 低 |

**推荐方案 B'**：
1. 精确匹配——只标记修改过相同文件的 evidence，不误报
2. state 表持久化——进程重启不丢累积器
3. `changed_paths` 可暴露给 UI（"上次验证后改了哪些文件"）
4. 与 hermes schema 对齐——future 可直接移植 hermes 的 verify-on-stop 等
5. `last_edit_at` 可作 timestamp fallback——如果 changed_paths 匹配失败，可降级到时间戳（hermes 兼容路径）

**方案 A 在 sherry 下不可行**：Gate B 硬阻断 + 纯时间戳 = B 写任何文件 → 所有 evidence stale → 连锁阻断。

**方案 C 作为 interim**：0.5 天改善部分盲区，后续再上 B'。

---

## 3. 任务一：verification_state 表 + changed_paths 追踪

### 3.1 目标

新增 `verification_state` 表（per session_id），追踪编辑过的文件列表。验证命令记录时快照到 evidence 行，staleness 用文件集合匹配。

### 3.2 前置依赖

- `计划_EvidenceLedger_SQLite迁移与清理.md` SQLite 迁移先做——state 表是同库新表，`CREATE TABLE IF NOT EXISTS verification_state`
- 如果 SQLite 迁移未做，state 表也可放 JSONL 旁的独立 JSON 文件（但 SQLite 更对齐 hermes）

### 3.3 改动清单

- [ ] **3.3.1** `evidence_ledger.py` 新增 `verification_state` 表（学 hermes schema 简化版）：
  ```sql
  CREATE TABLE IF NOT EXISTS verification_state (
      session_id TEXT PRIMARY KEY,
      changed_paths_json TEXT NOT NULL DEFAULT '[]',  -- 编辑过的文件（累积，上限 200）
      last_edit_at TEXT,                               -- 最后编辑时间（UTC ISO）
      last_event_rowid INTEGER                          -- 指向最新 evidence 行（学 hermes last_event_id）
  );
  ```

- [ ] **3.3.2** `evidence_ledger.py` 新增 state 管理方法（学 hermes `mark_workspace_edited` + `record_terminal_result` 的 state 更新）：
  ```python
  @classmethod
  def record_file_edit(cls, file_path: str, session_id: str) -> None:
      """文件写入时调用：累积 changed_paths + 设 last_edit_at。学 hermes mark_workspace_edited。"""
      with cls._get_conn() as conn:
          row = conn.execute(
              "SELECT changed_paths_json FROM verification_state WHERE session_id = ?",
              (session_id,)
          ).fetchone()
          existing = set(json.loads(row["changed_paths_json"]) if row else [])
          merged = sorted(existing | {file_path})[-200:]  # 上限 200，学 hermes
          conn.execute(
              "INSERT INTO verification_state(session_id, changed_paths_json, last_edit_at) "
              "VALUES (?, ?, ?) "
              "ON CONFLICT(session_id) DO UPDATE SET "
              "  changed_paths_json = excluded.changed_paths_json, "
              "  last_edit_at = excluded.last_edit_at",
              (session_id, json.dumps(merged), datetime.now(UTC).isoformat())
          )
          conn.commit()

  @classmethod
  def snapshot_and_reset_changed_paths(cls, session_id: str) -> list[str]:
      """验证命令记录时调用：快照 changed_paths 到 evidence 行，然后重置。学 hermes record_terminal_result。"""
      with cls._get_conn() as conn:
          row = conn.execute(
              "SELECT changed_paths_json FROM verification_state WHERE session_id = ?",
              (session_id,)
          ).fetchone()
          paths = json.loads(row["changed_paths_json"]) if row else []
          # 重置（学 hermes: last_edit_at = NULL, changed_paths = []）
          conn.execute(
              "UPDATE verification_state SET changed_paths_json = '[]', last_edit_at = NULL "
              "WHERE session_id = ?",
              (session_id,)
          )
          conn.commit()
          return paths
  ```

- [ ] **3.3.3** `evidence_ledger.py` 的 `append` 方法增 `modified_files` 字段：
  - 调用方（`evidence_recorder.record_verification_evidence`）在 append 前调 `snapshot_and_reset_changed_paths`，把结果作为 `modified_files` 字段传入
  - SQLite schema：evidence_events 表增 `modified_files_json TEXT DEFAULT '[]'` 列（`ALTER TABLE ADD COLUMN`，向后兼容）
  - JSONL 模式（如未迁移）：evidence 行 dict 增 `modified_files` key

- [ ] **3.3.4** `evidence_recorder.py` 改造：
  - `invalidates_evidence` 装饰器（`:98-151`）：在调 `mark_evidence_stale` 后，**额外调** `EvidenceLedger.record_file_edit(file_path, session_id)`——累积到 state 表
  - `record_verification_evidence`（`:48-79`）：在调 `EvidenceLedger.for_session().append()` 前，调 `EvidenceLedger.snapshot_and_reset_changed_paths(session_id)` 拿到 `modified_files`，传入 append 的 fields：
    ```python
    modified_files = EvidenceLedger.snapshot_and_reset_changed_paths(session_id)
    fields["modified_files"] = modified_files
    EvidenceLedger.for_session(session_id).append(**fields)
    ```

- [ ] **3.3.5** `evidence_collector.py:15-21` 的 `_is_stale` 改造：
  ```python
  def _is_stale(index, row, stale_events):
      """True when a later stale event's file_path is in this row's modified_files."""
      modified = row.get("modified_files") or []
      if not modified:
          # 向后兼容：旧行无 modified_files → 降级到 substring 匹配
          command = str(row.get("command") or "")
          return any(
              stale_index > index and str(event.get("file_path") or "") in command
              for stale_index, event in stale_events
          )
      return any(
          stale_index > index
          and str(event.get("file_path") or "") in modified
          for stale_index, event in stale_events
      )
  ```
  - **向后兼容**：旧 evidence 行无 `modified_files` → 降级到原 substring 匹配（旧行为不变）
  - 新 evidence 行有 `modified_files` → 精确文件集合匹配

### 3.4 风险与约束

- **向后兼容**：旧 evidence 行无 `modified_files` → 降级 substring（旧行为不变）；新行有 `modified_files` → 精确匹配
- **路径规范化**：`record_file_edit` 的 `file_path` 与 stale 事件的 `file_path` 格式需一致——都来自 `invalidates_evidence` 装饰器的同一 `file_path` 参数，**格式一致**
- **并发**：state 表的 `record_file_edit` 和 `snapshot_and_reset_changed_paths` 都在 SQLite WAL 模式下，`busy_timeout=5000` 自动重试
- **state 表清理**：session 删除时清理其 state 行——在 `计划_EvidenceLedger_SQLite迁移与清理.md` 的 `purge_session` 中加 `DELETE FROM verification_state WHERE session_id = ?`

**验收**：向后兼容 + 盲区消除。新建 `tests/agent/tools/pub_base/test_evidence_state.py`。

---

## 4. 任务二：（可选）路径规范化

### 4.1 目标

确保 `changed_paths` 和 stale 事件的 `file_path` 格式一致（相对 project root）。

### 4.2 改动清单

- [ ] **4.2.1** `evidence_recorder.py` 的 `record_file_edit`：把 `file_path` 规范化为相对 project root（复用 `session_workspace_root` + `resolve_workspace_path`）
- [ ] **4.2.2** `mark_stale_for_path` 的 `file_path` 同样规范化

**验收**：不同 subagent 用不同 cwd 写同一文件时，`changed_paths` 里的路径一致。

---

## 5. 执行顺序

```
前置：计划_EvidenceLedger_SQLite迁移与清理.md（SQLite 迁移）—— state 表是同库新表

方案 C（interim，0.5 天）——可选，先改善一部分盲区
  └─ _is_stale 改 stem 匹配
      │
      ↓ （验证方向后）
§3 state 表 + changed_paths 追踪（3-4 天）——方案 B' 核心
  ├─ verification_state 表 schema
  ├─ record_file_edit + snapshot_and_reset_changed_paths
  ├─ append 增 modified_files 字段
  ├─ evidence_recorder 改造（装饰器 + record）
  ├─ _is_stale 改文件集合匹配（向后兼容降级 substring）
  └─ purge_session 增 state 表清理
      │
      ↓ （可选）
§4 路径规范化（0.5 天）

总工程量：3-4 天（方案 B'）/ 0.5 天（方案 C interim）/ 3.5-4.5 天（B'+C）
```

**与关联计划的关系**：
- SQLite 迁移先做 → state 表是 `CREATE TABLE IF NOT EXISTS`，同库新表
- `计划_跨Subagent文件冲突检测.md` §2 先做或后做都行——§2 的 cross-session 扩展改的是 stale 事件的传播范围（per-session → cross-session），本计划改的是 `_is_stale` 的匹配方式（substring → 文件集合），两者正交

---

## 6. 验收标准

- [ ] `verification_state` 表存在且有 `changed_paths_json` / `last_edit_at` 字段。
- [ ] subagent 写 `src/auth/login.py` → state 的 `changed_paths_json` 包含 `"src/auth/login.py"`。
- [ ] subagent 跑 `pytest tests/test_login.py` → evidence 行的 `modified_files` = `["src/auth/login.py"]`（快照）→ state 重置为 `[]`。
- [ ] B 写 `src/auth/login.py` → A 的 evidence 行被标记 stale（`"src/auth/login.py" in ["src/auth/login.py"]` → True）。
- [ ] B 写 `README.md` → A 的 evidence 行**不**被标记 stale（`"README.md" in ["src/auth/login.py"]` → False）。
- [ ] 旧 evidence 行（无 `modified_files`）→ `_is_stale` 降级到 substring 匹配（旧行为不变）。
- [ ] `changed_paths` 上限 200（超过时保留最新 200）。
- [ ] Gate B/C 硬阻断行为不变。
- [ ] `collect_evidence_summary` 输出格式不变。
- [ ] session 删除时 `verification_state` 行也清除。

---

## 7. 不做的事

| 项 | 不做的原因 |
|---|---|
| 方案 A 纯时间戳 | Gate B 硬阻断下误报致命——连锁阻断 |
| Gate B 改软门 | 独立大改动，不在本计划范围 |
| import graph 分析 | 能精确追踪文件间依赖，但需静态分析基础设施，工程量过大 |
| hermes 的 `canonical_command` 命令规范化 | sherry 的 `classify_verification_command` 已有分类逻辑，规范化是独立改进 |
| hermes 的 `scope`（targeted/full） | sherry 的 evidence 不区分 scope，Gate B 对所有 evidence 同等检查 |

---

## 8. 关键参考

### sherry 内部（改造对象）
- `agent/tools/taskflow/evidence_collector.py:15-21`（`_is_stale`——staleness 派生核心）
- `agent/tools/pub_base/evidence_ledger.py:30-162`（EvidenceLedger——增 state 表 + state 方法）
- `agent/tools/pub_base/evidence_recorder.py:48-95,98-151`（`record_verification_evidence` + `invalidates_evidence` 装饰器——增 `record_file_edit` 调用 + `snapshot_and_reset`）
- `agent/tools/taskflow/tools/taskflow_finish.py:132-161,45-125`（Gate C + Gate E——不改造，只确认兼容）

### hermes 参考（已核实）
- `agent/verification_evidence.py:100-111`（`verification_state` 表 schema——state 表设计参考）
- `agent/verification_evidence.py:495-546`（`mark_workspace_edited`——`changed_paths` 累积 + `last_edit_at` 范式）
- `agent/verification_evidence.py:430-492`（`record_terminal_result`——记录时重置 state 的范式）
- `agent/verification_evidence.py:549-618`（`verification_status`——只用时间戳不用 changed_paths 的反面教材）

### 关联 TODO
- `计划_EvidenceLedger_SQLite迁移与清理.md`——SQLite 迁移先做，state 表是同库新表，`purge_session` 增 state 清理
- `计划_跨Subagent文件冲突检测.md` §2——cross-session staleness 改的是传播范围（per-session→cross-session），本计划改的是匹配方式（substring→文件集合），两者正交
