# 计划 — code-intel 性能优化（shadow-git 驱动 + 缓存 + 懒加载）

> 目标：消除 code-intel 每次查询的全量 walk+stat 开销。
> **铁律：绝不能影响现有功能。** 所有优化都是 indexer 内部快速路径，fallback 到当前 walk+stat 逻辑。
> shadow-git 只读使用（`shadow_head` + `diff`），不创建/修改 store。

---

## 现状痛点

每次 code-intel 工具调用（`callers` / `callees` / `impact` / `explore` / `path` / `semantic_code_search`）：
- `CodeQuery._ensure_index()` → `CodeIndexer.index_directory()` 全量执行
- 无变更时仍做：`os.walk` 全树 + 每文件 `stat` + `SELECT * FROM index_meta` 全表加载 +
  ~17 条 `CREATE TABLE IF NOT EXISTS` DDL + 5 条 `COUNT(*)` 全表扫描 + 2 次 `sqlite3.connect`
- **零缓存**：同 session 20 次调用 = 20 次全量 walk + 60000 次 stat
- 改 1 文件也触发四张边表全量未解析边 resolve

## 优化总览

| 优先级 | 优化项 | 改动量 | 收益 |
|---|---|---|---|
| P0 | shadow-git 水位线快速路径 | ~40 行 | 消除 walk+stat（无变更时 3000 stat → 1 rev-parse） |
| P0 | schema ready 标记 | ~5 行 | 消除 17 条 DDL |
| P1 | 跳过 COUNT(*) 懒加载 | ~10 行 | 消除 5 条全表扫描 |
| P1 | 查询结果缓存 | ~25 行 | 同 session 重复查询 → 缓存命中 |
| P2 | 持久连接 | ~15 行 | 消除重复 connect |
| P2 | 局部边 resolve | ~30 行 | 改 1 文件不全库 resolve |
| P2 | 外部编辑器采样 stat 兜底 | ~15 行 | shadow-git 盲区 fallback |

---

## Phase 0 — shadow-git 水位线快速路径（P0，核心优化）

### 原理

shadow-git 在 `<root>/.sherry/shadow-git/` 维护一个 bare git 仓库，每轮首次写入前自动 commit。
`shadow_head(root)` 只读返回当前 HEAD hash。code-intel 索引完成后记录该 hash 作为"水位线"，
下次索引时比较：一致 → 零变更，短路返回；不一致 → `git diff --name-only <水位线>..HEAD` 取变更文件，只解析这些。

### Step 0.1 shadow-git 新增 diff API

**文件**：`agent/tools/pub_base/shadow_git.py`

在现有 `diff_changed_paths`（L299-317）旁追加：

```python
def diff_between_commits(
    project_root: Path, from_sha: str, to_sha: str, *, cap: int = 500
) -> list[str]:
    """git diff --name-only <from>..<to>，返回两次快照间变更的项目相对路径。

    只读操作——不创建/修改 store。fail-open：store 不存在或命令失败返回 []。
    """
    store = _store_path(project_root)
    if not store.exists():
        return []
    try:
        result = run_shadow(
            project_root,
            ["diff", "--name-only", f"{from_sha}..{to_sha}"],
            timeout=10,
        )
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return paths[:cap]
    except Exception:
        return []
```

同时确认 `pub/git_head.py::shadow_head(root)` 已存在且只读（不创建 store）。
**不改现有 `diff_changed_paths` / `shadow_head` / `ensure_checkpoint` 等任何函数。**

### Step 0.2 index_meta 表新增水位线列

**文件**：`agent/tools/code_intel/indexer.py`

`_ensure_schema`（L184, L209）的 DDL 中，`index_meta` 表已有 `file_path / mtime / size / language / indexed_at / symbol_count / error`。

新增一张单行元数据表（不改 `index_meta` 本身，避免 schema migration）：

```python
# 加入 _TABLE_DDL（L28-117 区域）
_INDEX_META_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS index_meta_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    shadow_head TEXT,
    last_index_at REAL
)
"""
```

读写 API（新增方法到 `CodeIndexer`）：

```python
def _last_index_head(self, conn) -> str | None:
    row = conn.execute(
        "SELECT shadow_head FROM index_meta_summary WHERE id = 1"
    ).fetchone()
    return row[0] if row else None

def _record_index_head(self, conn, head: str | None) -> None:
    now = time.time()
    conn.execute(
        """INSERT INTO index_meta_summary (id, shadow_head, last_index_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET shadow_head = excluded.shadow_head,
                                         last_index_at = excluded.last_index_at""",
        (head, now),
    )
    conn.commit()
```

### Step 0.3 index_directory 快速路径

**文件**：`agent/tools/code_intel/indexer.py`

当前 `index_directory`（L188）签名：
```python
def index_directory(self, root: Path, *, force: bool = False) -> IndexResult:
```

改为（新增 `changed_hint` 参数，默认 None = 当前行为）：
```python
def index_directory(
    self, root: Path, *, force: bool = False, changed_hint: list[str] | None = None
) -> IndexResult:
```

在 `with self._lock:` 之后、`_index_directory` 之前插入快速路径：

```python
with self._lock:
    conn = self._connect()
    try:
        self._ensure_schema(conn)

        if not force and changed_hint is None:
            # 尝试 shadow-git 快速路径
            git_result = self._try_shadow_fast_path(root, conn)
            if git_result is not None:
                return git_result  # 快速路径命中（零变更或只解析变更文件）

        # fallback：全量 walk（当前逻辑不变）
        result = self._index_directory(root, conn, force=force)

        # 索引完成后记录水位线（只在 walk 路径记录，快速路径内部已记录）
        if not force:
            try:
                from pub.git_head import shadow_head
                self._record_index_head(conn, shadow_head(root))
            except Exception:
                pass

        return result
    finally:
        conn.close()
```

`_try_shadow_fast_path` 实现：

```python
def _try_shadow_fast_path(self, root: Path, conn) -> IndexResult | None:
    """尝试 shadow-git 快速路径。返回 None = 无法确定，fallback 到 walk。"""
    try:
        from pub.git_head import shadow_head
        from agent.tools.pub_base.shadow_git import diff_between_commits
    except Exception:
        return None

    current_head = shadow_head(root)
    if current_head is None:
        return None  # shadow store 不存在 → fallback

    last_head = self._last_index_head(conn)
    if last_head is None:
        return None  # 首次索引（无水位线）→ fallback 全量 walk

    if current_head == last_head:
        # 零变更：短路返回
        return IndexResult(
            changed=False,
            skipped_files=0,
            indexed_files=[],
            deleted_files=[],
            resolved_edges=0,
            truncated=False,
        )

    # 有变更：取 diff，只解析变更文件
    changed = diff_between_commits(root, last_head, current_head)
    if not changed:
        # diff 为空但 HEAD 变了（可能是空 commit）→ 记录水位线，短路
        self._record_index_head(conn, current_head)
        return IndexResult(changed=False, skipped_files=0, ...)

    # 过滤出支持的扩展名
    supported = _supported_extensions()
    changed_supported = [
        rel for rel in changed
        if Path(rel).suffix.lower() in supported
    ]

    if not changed_supported:
        self._record_index_head(conn, current_head)
        return IndexResult(changed=False, skipped_files=0, ...)

    # 只解析变更文件（复用现有 _flush_batch 的解析逻辑）
    result = self._index_specific_files(root, conn, changed_supported)

    # 处理删除：diff --name-only 只返回变更，删除检测需要对比 known
    # 这里复用 _load_known + seen 的逻辑，但只针对 changed_supported
    self._handle_deletions_from_diff(root, conn, changed_supported)

    # 边 resolve（只 resolve 变更文件涉及的未解析边）
    if result.changed:
        self._resolve_edges_incremental(conn, changed_supported)

    self._record_index_head(conn, current_head)
    return result
```

### Step 0.4 提取可复用解析方法（纯重构，行为不变）

**前置子步骤**：在实现 `_index_specific_files` 之前，先从现有 `_index_directory`（L193-280）
和 `_flush_batch`（L290）中提取可复用的私有方法。这是纯重构——**不改变任何行为**，
只是把逻辑拆分到可复用的函数中。

**提取的方法**：

```python
def _parse_single_file(self, path: Path, language: str) -> tuple[list, list, list, list, list]:
    """解析单个文件，返回 (symbols, calls, imports, inherits, mixins)。
    从 _flush_batch L300-315 区域提取（tree-sitter parse + extract 调用）。"""
    source = path.read_bytes()
    return _extract_symbols(source, language, str(path))

def _write_file_symbols(self, conn, rel: str, language: str,
                        symbols: list, calls: list, imports: list,
                        inherits: list, mixins: list) -> int:
    """将解析结果写入 symbols + 4 种 edges 表，返回 symbol_count。
    从 _flush_batch L315-390 区域提取（INSERT symbols/call_edges/import_edges 等）。"""

def _write_file_meta(self, conn, rel: str, stat, language: str,
                     error: str | None, symbol_count: int = 0) -> None:
    """INSERT OR REPLACE index_meta 行。
    从 _write_meta L419-434 提取（已存在，重命名对齐）。"""
```

**重构步骤**：
1. 从 `_index_directory` 的 pending 循环中提取 `_parse_single_file` + `_write_file_symbols`
2. 从 `_flush_batch` 中提取同样的调用（`_flush_batch` 改为调这两个方法）
3. 运行现有 `test_indexer.py` 确认零回归
4. **这是独立的 commit**——纯重构，不含任何 shadow-git 逻辑

### Step 0.5 _index_specific_files 方法

复用 Step 0.4 提取的方法，实现只解析指定文件列表（不 walk）：

```python
def _index_specific_files(self, root: Path, conn, files: list[str]) -> IndexResult:
    """只解析指定文件列表（相对路径），不 walk 全树。"""
    indexed_files = []
    error_files = 0
    skipped_files = 0

    for rel in files:
        path = (root / rel).resolve()
        if not path.exists():
            continue  # 已删除，由 _handle_deletions_from_diff 处理
        try:
            stat = path.stat()
            if stat.st_size > _config_max_file_bytes():
                self._write_meta(conn, rel, stat, language="?", error="file too large")
                error_files += 1
                continue
            language = _language_for_extension(path.suffix)
            symbols, calls, imports, inherits, mixins = _extract_file(path, language)
            self._write_symbols(conn, rel, symbols, calls, imports, inherits, mixins)
            self._write_meta(conn, rel, stat, language=language, error=None,
                             symbol_count=len(symbols))
            indexed_files.append(rel)
        except Exception as exc:
            self._write_meta(conn, rel, path.stat() if path.exists() else None,
                             language="?", error=str(exc))
            error_files += 1

    return IndexResult(
        changed=bool(indexed_files),
        indexed_files=indexed_files,
        error_files=error_files,
        skipped_files=skipped_files,
        deleted_files=[],
        resolved_edges=0,
        truncated=False,
    )
```

**注意**：`_extract_file` / `_write_symbols` / `_write_meta` 是从现有 `_index_directory`（L193-280）
和 `_flush_batch`（L290）中提取的已有逻辑，不是新实现。重构时把它们提取为可复用的私有方法，
`_index_directory` 和 `_index_specific_files` 都调用它们。**不改变 `_index_directory` 的行为。**

### Step 0.6 删除检测

shadow-git 的 `diff --name-only` 包含已删除文件（git 会报 `D` 状态），但 `--name-only` 只给路径
不给状态。用 `--name-status` 区分：

修改 `diff_between_commits` 的返回值，或新增一个带状态的变体：

```python
def diff_between_commits_with_status(
    project_root: Path, from_sha: str, to_sha: str, *, cap: int = 500
) -> list[tuple[str, str, str | None]]:
    """返回 [(status, path, renamed_to), ...]。

    status ∈ {A, M, D, R}（取首字母，R90 → R）。
    path: 主路径（A/M/D 时是文件路径；R 时是旧路径）。
    renamed_to: 仅 R 状态有值（新路径），其他为 None。
    """
    result = run_shadow(
        project_root,
        ["diff", "--name-status", f"{from_sha}..{to_sha}"],
        timeout=10,
    )
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_char = parts[0][0]  # R90 → R, C80 → C
        if status_char == "R" and len(parts) >= 3:
            # R<score>\told_path\tnew_path
            entries.append((status_char, parts[1], parts[2]))
        elif status_char == "C" and len(parts) >= 3:
            # C<score>\told_path\tnew_path (copy)
            entries.append((status_char, parts[1], parts[2]))
        else:
            # A/M/D: \tstatus\tpath
            entries.append((status_char, parts[1], None))
    return entries[:cap]
```

`_try_shadow_fast_path` 中根据 status 分流（使用三元组 `(status, path, renamed_to)`）：
- `A` / `M` → 加入 `to_index` 列表（path）
- `D` → 加入 `to_delete` 列表（path）→ 调用现有 `_drop_many(conn, to_delete)`
- `R` → 旧路径（path）加入 `to_delete` + 新路径（renamed_to）加入 `to_index`
- `C`（copy）→ 新路径（renamed_to）加入 `to_index`（旧路径不变）

### Step 0.7 外部编辑器盲区兜底

shadow-git 只捕获 agent 写入。用户在外部 IDE 改文件不触发 shadow commit。

**采样 stat 兜底**（在快速路径短路返回前，对查询涉及的文件做轻量 stat 检查）：

```python
# 在 _try_shadow_fast_path 的 current_head == last_head 分支中：
if current_head == last_head:
    # 零 shadow 变更，但可能有外部编辑
    if self._external_edit_check(root, conn):
        return None  # 发现有外部变更 → fallback walk
    return IndexResult(changed=False, ...)
```

`_external_edit_check` 只 stat **index_meta 中有记录的、最近被查询过的文件**的 mtime：

```python
def _external_edit_check(self, root: Path, conn) -> bool:
    """采样检查：取 index_meta 中最近 N 个文件的 mtime，和磁盘对比。"""
    rows = conn.execute(
        "SELECT file_path, mtime, size FROM index_meta ORDER BY indexed_at DESC LIMIT 20"
    ).fetchall()
    for rel, stored_mtime, stored_size in rows:
        path = root / rel
        if not path.exists():
            return True  # 文件被外部删除
        stat = path.stat()
        if (stat.st_mtime, stat.st_size) != (stored_mtime, stored_size):
            return True  # 文件被外部修改
    return False
```

**成本**：最多 20 次 stat（而非 3000 次）。如果检测到外部变更，fallback 到全量 walk（当前行为）。

### Step 0.8 测试

**文件**：`tests/agent/tools/code_intel/test_shadow_fast_path.py`

```python
# 测试用例：
# 1. test_no_shadow_store_fallback: shadow store 不存在 → fallback walk，行为同当前
# 2. test_first_index_walk: 无水位线 → fallback 全量 walk，完成后记录水位线
# 3. test_zero_change_short_circuit: HEAD 一致 → IndexResult(changed=False)，不 walk
# 4. test_changed_files_only: HEAD 变了，diff 返回 2 文件 → 只解析这 2 文件，不 walk
# 5. test_deleted_files: diff 返回 D 状态 → _drop_many 被调用
# 6. test_renamed_files: diff 返回 R<score>\told\tnew → 旧路径 drop + 新路径 index
# 7. test_copied_files: diff 返回 C<score>\told\tnew → 新路径 index，旧路径不变
# 8. test_external_edit_detection: HEAD 一致但文件 mtime 变了 → 采样 stat 发现 → fallback walk
# 9. test_external_edit_clean: HEAD 一致且 mtime 一致 → 短路返回
# 10. test_force_bypasses_fast_path: force=True → 跳过快速路径，全量 walk
# 11. test_shadow_git_exception_fallback: shadow_head 抛异常 → fallback walk，不 crash
# 12. test_watermark_recorded_after_walk: 全量 walk 后水位线被记录
# 13. test_watermark_recorded_after_fast_path: 快速路径后水位线被更新
# 14. test_refactor_no_regression: Step 0.4 纯重构后 test_indexer.py 全部通过
```

### Phase 0 验收标准

- [ ] `test_shadow_fast_path.py` 全部通过
- [ ] shadow store 不存在时行为与当前完全一致（fallback walk）
- [ ] `force=True` 时行为与当前完全一致
- [ ] 现有 `test_indexer.py` / `test_query.py` / `test_tools.py` / `test_e2e.py` 零回归
- [ ] `run_tests_split.py` 零回归
- [ ] 手动验证：无变更时 `_ensure_index` 不触发 walk（日志/计时确认）
- [ ] 手动验证：改 1 文件后只解析该文件（日志确认）
- [ ] **不改 `shadow_git.py` 现有函数，只新增 `diff_between_commits` 和 `diff_between_commits_with_status`**
- [ ] **不改 `pub/git_head.py`（只读使用 `shadow_head`）**

---

## Phase 1 — 低成本优化（schema ready + COUNT 懒加载）

**前置条件**：Phase 0 验收通过。

### Step 1.1 schema ready 标记

**文件**：`agent/tools/code_intel/indexer.py`

```python
# 模块级（和 _initialized 等同级）
_schema_ensured: bool = False

def _ensure_schema(self, conn):
    if self._ns.get("_schema_ensured"):
        return
    # ... 现有 DDL 执行 ...
    self._ns["_schema_ensured"] = True
```

**注意**：`_schema_ensured` 是进程级（namespace 级），不是 per-connection。
首次 ensure 后，同进程后续所有 `_ensure_schema` 调用直接返回。
DDL 是 `CREATE TABLE IF NOT EXISTS`，幂等，跳过不影响正确性。

### Step 1.2 COUNT(*) 懒加载

**文件**：`agent/tools/code_intel/indexer.py`

当前 L268-272 每次索引后跑 5 条 `SELECT COUNT(*)`：
```python
result.symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
result.call_edge_count = conn.execute("SELECT COUNT(*) FROM call_edges").fetchone()[0]
# ... 3 more
```

改为**按需查询**——只在调用方需要统计时才查：

```python
# IndexResult 新增 lazy 计数
class IndexResult:
    ...
    _counts: dict[str, int] | None = None

    @property
    def symbol_count(self) -> int:
        if self._counts is None:
            self._counts = self._fetch_counts()
        return self._counts.get("symbols", 0)
```

或者更简单：直接从 `index_meta` 的 `symbol_count` 列聚合（`SELECT SUM(symbol_count) FROM index_meta`），
这比 `COUNT(*) FROM symbols` 快得多（行数少，有索引）。

### Step 1.3 测试

```python
# test_schema_ensured: 第一次 ensure 执行 DDL，第二次直接返回（mock 确认不执行 DDL）
# test_count_lazy: 索引后不立即查 COUNT，只在读取 result.symbol_count 时才查
```

### Phase 1 验收标准

- [ ] schema ready 标记生效：第二次 `_ensure_schema` 不执行 DDL
- [ ] COUNT 懒加载生效：不读统计字段时不查 COUNT
- [ ] 现有测试零回归

---

## Phase 2 — 查询结果缓存

**前置条件**：Phase 0 + Phase 1 验收通过。

### Step 2.1 缓存实现

**文件**：`agent/tools/code_intel/query.py`

在 `CodeQuery` 类中新增 session 级缓存：

```python
class CodeQuery:
    def __init__(self, ...):
        ...
        self._result_cache: dict[tuple, Any] = {}
        self._cache_head: str | None = None

    def _cache_key(self, method: str, *args) -> tuple:
        return (method, *args)

    def _try_cache(self, root: Path, key: tuple) -> Any | _CacheMiss:
        """shadow HEAD 未变时返回缓存，否则返回 _CacheMiss。"""
        try:
            from pub.git_head import shadow_head
            current = shadow_head(root)
        except Exception:
            return _CacheMiss()
        if current is not None and current == self._cache_head:
            return self._result_cache.get(key, _CacheMiss())
        # HEAD 变了或首次 → 清缓存
        self._result_cache.clear()
        self._cache_head = current
        return _CacheMiss()

    def _store_cache(self, key: tuple, value: Any) -> None:
        self._result_cache[key] = value
```

在每个公开方法中包裹：

```python
def callers(self, symbol: str, root: Path | None = None) -> list[SymbolRef]:
    root = self._resolve_root(root)
    key = self._cache_key("callers", symbol, str(root))
    cached = self._try_cache(root, key)
    if not isinstance(cached, _CacheMiss):
        return cached
    self._ensure_index(root)
    result = self._callers_impl(symbol, root)
    self._store_cache(key, result)
    return result
```

**缓存失效条件**：shadow HEAD 变了 → 清整个缓存（简单可靠）。
**缓存范围**：一个 `CodeQuery` 实例 = 一个子 agent session。子 agent 结束时实例销毁，缓存自动释放。

### Step 2.2 测试

```python
# 1. test_cache_hit_same_head: HEAD 不变 → 第二次 callers 返回缓存，不查 DB
# 2. test_cache_invalidate_on_head_change: HEAD 变了 → 缓存清空，重新查询
# 3. test_cache_no_shadow_store: shadow store 不存在 → 不缓存，每次查 DB（行为同当前）
# 4. test_cache_different_methods: callers + callees 各自独立缓存
# 5. test_cache_different_roots: 不同 root 的查询不互相干扰
```

### Phase 2 验收标准

- [ ] 同 session 同查询（HEAD 不变）第二次从缓存返回
- [ ] HEAD 变化后缓存失效
- [ ] shadow store 不存在时不缓存（行为同当前）
- [ ] 现有测试零回归

---

## Phase 3 — 持久连接 + 局部边 resolve

**前置条件**：Phase 2 验收通过。

### Step 3.1 持久连接

**文件**：`agent/tools/code_intel/query.py`

```python
class CodeQuery:
    def __init__(self, ...):
        ...
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=30.0, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout = 30000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
```

**注意**：`check_same_thread=False` 让连接可跨线程使用（`asyncio.to_thread` 场景）。
SQLite WAL 模式下读写连接可并发。`CodeIndexer` 仍用自己的连接（写入），`CodeQuery` 用持久连接（只读）。

### Step 3.2 局部边 resolve

**文件**：`agent/tools/code_intel/indexer.py`

当前 `_resolve_call_edges`（L477+）对**全库**未解析边做名字匹配。改为只 resolve 变更文件涉及的边：

```python
def _resolve_edges_incremental(self, conn, changed_files: list[str]) -> None:
    """只 resolve 变更文件涉及的未解析边。"""
    if not changed_files:
        return

    # 变更文件的新符号 ID
    changed_ids = set(
        row[0] for row in conn.execute(
            "SELECT id FROM symbols WHERE file_path IN (%s)" % ",".join("?" * len(changed_files)),
            changed_files,
        )
    )

    # 只 resolve 从这些符号发出的 + 指向这些符号的未解析边
    # 发出边：caller_file IN changed_files
    self._resolve_call_edges(conn, extra_filter=("caller_file IN changed_files", changed_files))
    # 入边：指向 changed 符号的 callee_name 需要重新匹配
    self._resolve_inbound_edges(conn, changed_ids)
```

**注意**：现有 `_resolve_call_edges` 全量 resolve 保留为 fallback。快速路径用增量版本，
全量 walk 路径用原版。**不改 `_resolve_call_edges` 的实现**，新增 `_resolve_edges_incremental` 调用它时传过滤条件。

### Step 3.3 测试

```python
# 1. test_persistent_connection: 多次查询复用同一连接
# 2. test_connection_close: close() 后下次查询重新连接
# 3. test_incremental_edge_resolve: 改 1 文件 → 只 resolve 该文件的边，不全库
# 4. test_full_resolve_fallback: walk 路径仍用全量 resolve
```

### Phase 3 验收标准

- [ ] 同 session 多次查询复用一个连接
- [ ] 增量 resolve 只处理变更文件的边
- [ ] walk 路径仍用全量 resolve（行为不变）
- [ ] 现有测试零回归

---

## 不变保证清单

| 保证项 | 验证方式 |
|---|---|
| shadow store 不存在时行为完全一致 | `test_no_shadow_store_fallback` + 手动删除 `.sherry/` 后测试 |
| `force=True` 时跳过快速路径，行为完全一致 | `test_force_bypasses_fast_path` |
| `shadow_git.py` 现有函数不被修改 | git diff 确认只新增函数 |
| `pub/git_head.py` 不被修改 | git diff 确认 |
| `index_directory(force=False)` 在 fallback 时行为完全一致 | 现有 `test_indexer.py` 全部通过 |
| `CodeQuery` 公开方法签名不变 | grep 确认签名一致 |
| 缓存在 shadow store 不存在时不启用 | `test_cache_no_shadow_store` |
| 持久连接的 WAL 并发不阻塞 indexer 写入 | 手动并发测试 |

---

## 依赖关系

```
Phase 0 (shadow-git 快速路径 + 外部编辑器兜底)
  │  不依赖任何其他优化
  │  验收: 快速路径测试通过 + 现有测试零回归
  ▼
Phase 1 (schema ready + COUNT 懒加载)
  │  独立于 Phase 0，可并行做
  │  验收: 现有测试零回归
  ▼
Phase 2 (查询结果缓存)
  │  依赖 Phase 0（缓存失效条件 = shadow HEAD 变化）
  │  验收: 缓存测试通过 + 现有测试零回归
  ▼
Phase 3 (持久连接 + 局部边 resolve)
  │  依赖 Phase 0（局部 resolve 依赖变更文件列表）
  │  验收: 并发测试 + 现有测试零回归
```

Phase 0 和 Phase 1 可并行。Phase 2 和 Phase 3 依赖 Phase 0。
任何 phase 失败 → 该 phase 的优化不启用（fallback 到当前 walk+stat 行为）。

---

## 预期效果

| 场景 | 当前 | 优化后（Phase 0+1+2） |
|---|---|---|
| 无变更，3000 文件，1 次 callers | walk + 3000 stat + 5 COUNT + 2 connect ≈ 100-300ms | 1 rev-parse + 20 采样 stat + 1 查询 ≈ 5-15ms |
| 无变更，3000 文件，20 次调用 | 20 × 上述 ≈ 2-6s | 第一次 15ms + 后 19 次缓存命中 ≈ 20ms |
| agent 改 1 文件，1 次 callers | walk + 3000 stat + 1 文件解析 + 全库 4 表 resolve ≈ 200-500ms | 1 diff + 1 文件解析 + 局部 resolve ≈ 20-50ms |
| 外部编辑器改文件 | walk + stat 正常检测 | 采样 stat 发现 → fallback walk（和当前一致） |
| 首次索引（无 shadow store） | walk + 3000 stat + 全量解析 | 同当前（fallback） |
