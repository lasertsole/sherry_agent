# 计划：GraphRAG entities_vdb 加 FTS5 混合检索 + 删除 bypass mode + 合并 vendored 目录

- 日期：2026-11-26
- 状态：**待评审**
- 来源：
  1. LightRAG 全 mode 纯 vector 检索。entities_vdb 存短标识符（函数名/概念名/文件名），精确匹配比语义相似更准。只给确定变好的 entities_vdb 加 FTS5。
  2. bypass mode 跳过检索直接 LLM 回答——跳过检索不该走 GraphRAG，Sherry 代码从不传 bypass，是上游冗余设计。
  3. vendored_lightrag/ 和 vendored_raganything/ 已被 Sherry 深度改造（path_search、即将加的 FTS5、删除 bypass），vendored_ 前缀不再准确，合并到 graph_rag/ 下。
- 风险等级：**低-中** — FTS5 加影子索引 + 删无用 mode + 目录合并纯重构
- 依赖：无
- 可独立执行：✅

---

## 0. 现有架构（已验证）

### 初始化链路

```
import graph_rag                          → __init__.py: sys.path 插入 + env defaults + sys.modules 别名
await get_lightrag()                      → base.py:72: working_dir = SRC_DIR/"rag"/"store"
  → LightRAG(...)                         → lightrag.py __post_init__: 构造 12 个 storage 对象（同步）
    → entities_vdb = SNKVVectorStorage(namespace="entities", meta_fields={"entity_name","source_id","content","file_path"})
    → relationships_vdb = SNKVVectorStorage(namespace="relationships", meta_fields={"src_id","tgt_id","source_id","content","file_path"})
    → chunks_vdb = SNKVVectorStorage(namespace="chunks", meta_fields={"full_doc_id","content","file_path"})
  → await _lightRAG.initialize_storages() → 逐个 storage.initialize()（打开 SNKV db + 构建 usearch 索引）
```

### 存储路径

- `working_dir` = `SRC_DIR / "rag" / "store"` = `<repo>/src/rag/store`（dev 环境）
- SNKV db: `working_dir/snkv_vec.db`（三个 namespace 共享）
- FTS5 db（新增）: `working_dir/lightrag_fts.db`

### entities_vdb upsert 的数据格式

```python
data = {
    "entity_id_hash": {
        "content": "entity description",       # required
        "entity_name": "JSON parser",          # meta_field, persisted
        "source_id": "chunk_abc",              # meta_field
        "file_path": "src/parser.py",          # meta_field
    },
    ...
}
```

### 检索调用链

```
kg_query() (operate.py:3798)
  → LLM 抽 ll_keywords + hl_keywords
  → _build_query_context() (operate.py:5040)
    → mode == "local": _get_node_data(ll_keywords, ..., entities_vdb, ...)
    → mode == "global": _get_edge_data(hl_keywords, ..., relationships_vdb, ...)
    → mode == "hybrid"/"mix": 两者都跑 + chunks (mix only)
    → mode == "path": _build_path_query_context() → path_search
  → _get_node_data() 调 entities_vdb.query(keywords, top_k)
  → entities_vdb.query() 返回 [{id, distance, entity_name, content, ...}]
```

---

## 1. 设计

### 1.1 只给 entities_vdb 加 FTS5

| Storage | 内容 | FTS5 确定？ | 理由 |
|---------|------|:---:|------|
| entities_vdb | 短标识符（entity_name） | ✅ | 精确匹配 > 语义相似 |
| relationships_vdb | 散文描述 | ❌ | 语义相似是正确工具 |
| chunks_vdb | 长文本 | ❌ | 改变行为特征 |

### 1.2 各 mode 受益

| mode | 搜 entities_vdb | 受益 |
|------|:-:|:---:|
| local | ✅ | ✅ |
| global | ❌ | ❌（不变） |
| hybrid | ✅（via local） | ✅ |
| naive | ❌ | ❌（不变） |
| mix | ✅（via hybrid） | ✅ |
| path | ✅（端点解析） | ✅ |
| ~~bypass~~ | ❌ | 删除 |

### 1.3 混合检索透明化

`SNKVVectorStorage.query()` 返回 FTS5 + vector 合并结果。`_get_node_data`（operate.py）不改——拿到的已经是混合后的。

### 1.4 删除 bypass mode

从 `QueryParam.mode` 的 `Literal` 去掉 `"bypass"`，删 `lightrag.py` 的 bypass 分发分支。

### 1.5 合并 vendored 目录

`vendored_lightrag/` 文件提到 `graph_rag/` 下；`vendored_raganything/` → `graph_rag/raganything/`（保留子目录避免文件名冲突）。~106 处 `graph_rag.vendored_lightrag.xxx` → `graph_rag.xxx`。

---

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `graph_rag/fts_shadow_index.py` | **新建** | FTS5 影子索引 |
| 2 | `vendored_lightrag/kg/snkv_vector_impl.py` | 加 `_fts` + `upsert/delete/query` 同步 | 存储层 |
| 3 | `graph_rag/base.py`（Sherry adapter） | `get_lightrag()` 初始化后 attach FTS 到 entities_vdb | 只给 entities |
| 4 | `config/features/agent_side/graph_rag_path.py` | 追加 FTS 配置 | opt-in |
| 5 | `vendored_lightrag/base.py` | `QueryParam.mode` 删 `"bypass"` | 删 mode |
| 6 | `vendored_lightrag/lightrag.py` | 删 bypass 分发分支 + docstring | 删 mode |
| 7 | `vendored_raganything/query.py` | docstring 去掉 bypass | 删 mode |
| 8 | `graph_rag/` 目录结构 | 合并 vendored_lightrag + vendored_raganything | 扁平化 |

---

## 3. 改动细节

### 3.1 FTS5 影子索引（graph_rag/fts_shadow_index.py — 新建）

```python
"""SQLite FTS5 shadow index for entities_vdb only.

Mirrors entity_name + content + keywords into FTS5 (unicode61 + trigram dual
tables) so entities_vdb.query() can combine keyword matching with vector
similarity. Only entities get FTS5 — relationships (prose) and chunks (long
text) stay pure vector.
"""

import sqlite3
from pathlib import Path
from typing import Any

from pub.func.cjk import contains_cjk


class EntitiesFtsIndex:
    """FTS5 shadow index for entities (unicode61 + trigram dual tables)."""

    def __init__(self, db_path: Path):
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                id, entity_name, content, keywords, tokenize='unicode61'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts_trigram USING fts5(
                id, entity_name, content, keywords, tokenize='trigram'
            );
        """)

    def sync_upsert(self, items: dict[str, dict[str, Any]]) -> None:
        """Mirror SNKV entity upsert into FTS5 (delete + insert per id)."""
        for item_id, data in items.items():
            name = str(data.get("entity_name") or "")
            content = str(data.get("content") or "")
            keywords = str(data.get("keywords") or "")
            for table in ("entities_fts", "entities_fts_trigram"):
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
                self._db.execute(
                    f"INSERT INTO {table} (id, entity_name, content, keywords) "
                    f"VALUES (?, ?, ?, ?)",
                    (item_id, name, content, keywords),
                )
        self._db.commit()

    def sync_delete(self, ids: list[str]) -> None:
        for item_id in ids:
            for table in ("entities_fts", "entities_fts_trigram"):
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        self._db.commit()

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """FTS5 keyword search. CJK → trigram, else → unicode61."""
        query = query.strip()
        if not query:
            return []

        if contains_cjk(query):
            table = "entities_fts_trigram"
            tokens = [f'"{t.replace(chr(34), chr(34)*2)}"' for t in query.split() if t]
            fts_query = " OR ".join(tokens) if tokens else f'"{query}"'
        else:
            table = "entities_fts"
            fts_query = query

        try:
            rows = self._db.execute(
                f"SELECT id, rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k),
            ).fetchall()
            return [{"id": r["id"], "distance": 1.0 / (i + 1)} for i, r in enumerate(rows)]
        except Exception:
            return []

    def count(self) -> int:
        return self._db.execute("SELECT count(*) FROM entities_fts").fetchone()[0]


def merge_fts_vector(
    fts_hits: list[dict[str, Any]],
    vec_hits: list[dict[str, Any]],
    fts_weight: float = 0.4,
    vec_weight: float = 0.6,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Merge FTS5 and vector results by weighted score."""
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}

    for i, hit in enumerate(fts_hits):
        eid = hit["id"]
        scores[eid] = scores.get(eid, 0) + fts_weight * (1.0 / (i + 1))
        meta[eid] = hit

    for hit in vec_hits:
        eid = hit["id"]
        scores[eid] = scores.get(eid, 0) + vec_weight * hit.get("distance", 0)
        if eid not in meta:
            meta[eid] = hit

    merged = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    return [{**meta[eid], "distance": score} for eid, score in merged]
```

### 3.2 SNKVVectorStorage 加可选 FTS（snkv_vector_impl.py）

在 `SNKVVectorStorage` dataclass 上加字段和方法：

```python
# 新增字段（dataclass field，默认 None，不影响现有构造）
_fts: Any = field(default=None, repr=False)

def attach_fts(self, fts_index) -> None:
    """Attach FTS5 shadow index (entities_vdb only)."""
    object.__setattr__(self, "_fts", fts_index)  # dataclass(frozen=False) 直接赋值即可

# upsert 末尾追加（line ~341 之后，return 之前）
if self._fts is not None:
    self._fts.sync_upsert(data)

# delete 方法末尾追加
if self._fts is not None:
    self._fts.sync_delete(ids)

# query 方法改造：提取原逻辑为 _vector_query，query 做混合
async def query(
    self, query: str, top_k: int, query_embedding: list[float] = None
) -> list[dict[str, Any]]:
    # ① Vector cosine（原逻辑）
    vec_hits = await self._vector_query(query, top_k, query_embedding)

    # ② FTS5（仅 entities_vdb 有 _fts）
    if self._fts is None:
        return vec_hits
    fts_hits = self._fts.search(query, top_k)
    if not fts_hits:
        return vec_hits

    # ③ 合并
    from graph_rag.fts_shadow_index import merge_fts_vector
    return merge_fts_vector(fts_hits, vec_hits, top_k=top_k)

async def _vector_query(self, query, top_k, query_embedding):
    """原 query 方法体（cosine similarity），提取为独立方法。"""
    # ... 原 query 的全部代码移到这里 ...
```

**注意**：`SNKVVectorStorage` 是 `@dataclass`，加 `_fts` 字段需要在 `BaseVectorStorage` 的字段之后、`__post_init__` 之前声明。因为 `@final @dataclass`，新增 field 会自动生成 `__init__` 参数，但 `_fts` 默认 `None` 不影响现有构造（`partial(SNKVVectorStorage, global_config=...)` 不传 `_fts`）。

### 3.3 get_lightrag() 初始化后 attach FTS（base.py）

在 `get_lightrag()` 的 `await _lightRAG.initialize_storages()` 之后：

```python
async def get_lightrag() -> LightRAG:
    """获取 LightRAG 单例实例"""
    working_dir: str = (SRC_DIR / "rag" / "store").resolve().as_posix()
    _lightRAG = LightRAG(
        working_dir=working_dir,
        llm_model_func=_llm_model_func,
        embedding_func=EmbeddingFunc(
            embedding_dim=1024,
            max_token_size=8192,
            func=_embedding_func,
        ),
        rerank_model_func=_rerank_model_func,
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
    )
    await _lightRAG.initialize_storages()

    # Attach FTS5 shadow index to entities_vdb only
    from config.features import GRAPH_RAG_PATH
    if GRAPH_RAG_PATH.get("fts_hybrid_enabled", False):
        from graph_rag.fts_shadow_index import EntitiesFtsIndex
        fts_db = Path(working_dir) / "lightrag_fts.db"
        entities_fts = EntitiesFtsIndex(fts_db)
        _lightRAG.entities_vdb.attach_fts(entities_fts)
        # relationships_vdb 和 chunks_vdb 不 attach → 纯 vector

    return _lightRAG
```

### 3.4 配置（graph_rag_path.py）

在 `GraphRagPathConfig` TypedDict 里追加：

```python
class GraphRagPathConfig(TypedDict):
    # ... existing fields ...

    # FTS5 hybrid retrieval (entities_vdb only)
    fts_hybrid_enabled: bool
    """Master switch for FTS5+Vector hybrid entity retrieval. Off = pure vector."""
    fts_hybrid_weight: float
    """FTS5 score weight (default 0.4)."""
    fts_hybrid_vec_weight: float
    """Vector score weight (default 0.6)."""
```

在 `config/features/agent_side/graph_rag_path.py` 的默认值 dict 里追加：

```python
"fts_hybrid_enabled": False,
"fts_hybrid_weight": 0.4,
"fts_hybrid_vec_weight": 0.6,
```

### 3.5 删除 bypass mode

#### QueryParam.mode（base.py:83）

```python
# 改前
mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass", "path"] = "mix"

# 改后
mode: Literal["local", "global", "hybrid", "naive", "mix", "path"] = "mix"
```

#### lightrag.py 删除 bypass 分发

- `aquery_data`（line 2080-2088）：删 `elif data_param.mode == "bypass"` 分支
- `aquery_llm`（line 2177+）：删 `elif param.mode == "bypass"` 分支
- docstring（line 1985, 2008）：去掉 bypass 描述

#### vendored_raganything/query.py docstring

4 处 docstring 去掉 `"bypass"` 引用（纯注释，不改逻辑）。

### 3.6 合并 vendored 目录

#### 目标结构

```
graph_rag/
├── __init__.py
├── __init__.pyi
├── sherry_adapter.py        # 原 base.py 改名（LLM/embedding/rerank 适配）
├── core.py
├── path_search.py
├── loop_patch.py
├── ensure_mineru_models.py
├── fts_shadow_index.py      # 新增
├── lightrag_base.py         # 原 vendored_lightrag/base.py 改名（LightRAG base classes）
├── lightrag.py              # 原 vendored_lightrag/lightrag.py
├── operate.py               # 原 vendored_lightrag/operate.py
├── ...                      # 其余 vendored_lightrag/*.py 直接提上来
├── kg/                      # 原 vendored_lightrag/kg/
│   ├── __init__.py
│   ├── snkv_vector_impl.py
│   └── ...
└── raganything/             # 原 vendored_raganything/
    ├── __init__.py
    ├── parser.py
    └── ...
```

#### 文件重命名（解决冲突）

| 原路径 | 新路径 | 理由 |
|--------|--------|------|
| `graph_rag/base.py`（Sherry adapter） | `graph_rag/sherry_adapter.py` | LLM 适配，名字更准确 |
| `vendored_lightrag/base.py`（LightRAG base） | `graph_rag/lightrag_base.py` | LightRAG 的 base classes |

#### import 路径批量替换

```bash
# vendored_lightrag 内部文件
find graph_rag/vendored_lightrag -name "*.py" -exec \
  sed -i 's/graph_rag\.vendored_lightrag\./graph_rag./g' {} +

# graph_rag/ Sherry 文件
sed -i 's/graph_rag\.vendored_lightrag\./graph_rag./g' graph_rag/__init__.py graph_rag/sherry_adapter.py graph_rag/path_search.py graph_rag/loop_patch.py graph_rag/core.py

# base.py 改名后引用更新
sed -i 's/from graph_rag\.vendored_lightrag\.base import/from graph_rag.lightrag_base import/g' graph_rag/*.py graph_rag/kg/*.py
sed -i 's/from \.base import/from .lightrag_base import/g' graph_rag/kg/*.py

# Sherry adapter 改名后引用更新
sed -i 's/from graph_rag\.base import/from graph_rag.sherry_adapter import/g' graph_rag/*.py
sed -i 's/from \.base import get_lightrag/from .sherry_adapter import get_lightrag/g' graph_rag/*.py
```

#### STORAGES 注册表更新（kg/__init__.py）

```python
# 改前
STORAGES = {
    "SNKVKVStorage": "graph_rag.vendored_lightrag.kg.snkv_kv_impl",
    "SNKVVectorStorage": "graph_rag.vendored_lightrag.kg.snkv_vector_impl",
    ...
}

# 改后
STORAGES = {
    "SNKVKVStorage": "graph_rag.kg.snkv_kv_impl",
    "SNKVVectorStorage": "graph_rag.kg.snkv_vector_impl",
    ...
}
```

#### sys.modules 别名更新（__init__.py）

`_alias_vendored_lightrag()` 改为 `_alias_lightrag()`，别名从 `"lightrag": vendored_mod` 改为指向合并后的模块。

`_alias_vendored_raganything()` 的 `spec_from_file_location` 路径改为 `graph_rag/raganything/`。

#### 物理移动文件

```bash
# vendored_lightrag 文件提上来
mv graph_rag/vendored_lightrag/*.py graph_rag/
mv graph_rag/vendored_lightrag/kg graph_rag/
mv graph_rag/vendored_lightrag/chunker graph_rag/
mv graph_rag/vendored_lightrag/parser graph_rag/
mv graph_rag/vendored_lightrag/llm graph_rag/
mv graph_rag/vendored_lightrag/utils* graph_rag/
mv graph_rag/vendored_lightrag/constants.py graph_rag/
# ... 其余文件

# vendored_raganything → raganything/
mv graph_rag/vendored_raganything graph_rag/raganything

# 删空目录
rmdir graph_rag/vendored_lightrag graph_rag/vendored_raganything

# base.py 重命名
mv graph_rag/base.py graph_rag/sherry_adapter.py
mv graph_rag/lightrag_base.py graph_rag/lightrag_base.py  # vendored_lightrag/base.py 已经提上来改名
```

#### __init__.pyi 更新

类型存根同步更新 import 路径。

---

## 4. 不改的东西

| 项 | 理由 |
|----|------|
| `operate.py` | 不改——`entities_vdb.query()` 返回混合结果 |
| `path_search.py` | 不改——端点解析调 `entities_vdb.query()` 自动混合 |
| `relationships_vdb` | 不 attach FTS → 纯 vector |
| `chunks_vdb` | 不 attach FTS → 纯 vector |
| global mode | 不搜 entities_vdb → 不变 |
| naive mode | 不搜 entities_vdb → 不变 |
| 检索逻辑 | 不改——只改存储层 `query()` 返回值 |

---

## 5. 测试

### 单元测试

| 测试 | 内容 |
|------|------|
| `test_entities_fts_schema` | entities_fts + entities_fts_trigram 表创建 |
| `test_entities_fts_upsert` | 写入实体 → FTS5 可搜到 |
| `test_entities_fts_delete` | 删除 → 不再命中 |
| `test_entities_fts_search_ascii` | ASCII 关键词命中（如 `_strip_bom`） |
| `test_entities_fts_search_cjk` | CJK 关键词命中（如"污点检测"） |
| `test_entities_fts_no_match` | 无匹配返回空 |
| `test_snkv_entities_fts_sync` | SNKV upsert → FTS5 同步 |
| `test_query_mixed_entities` | entities_vdb.query() 返回 FTS + vector 合并 |
| `test_query_pure_vector_relationships` | relationships_vdb.query() 仍纯 vector |
| `test_query_pure_vector_chunks` | chunks_vdb.query() 仍纯 vector |
| `test_fts_disabled_pure_vector` | `fts_hybrid_enabled=False` → entities_vdb 也纯 vector |
| `test_merge_weights` | 不同权重影响排序 |
| `test_precise_entity_name_beats_semantic` | `_strip_bom` 查询混合比纯 vector 更准 |
| `test_local_mode_uses_mixed` | local mode 的实体检索是混合的 |
| `test_global_mode_unchanged` | global mode 行为不变 |
| `test_naive_mode_unchanged` | naive mode 行为不变 |
| `test_bypass_mode_removed` | `QueryParam.mode` 不含 `"bypass"` |
| `test_bypass_mode_rejected` | 传 `mode="bypass"` 报错 |

### 回归测试（合并后功能无损）

| 测试 | 内容 |
|------|------|
| `test_import_paths_resolve` | 所有 `graph_rag.xxx` import 解析成功，无残留 `vendored_lightrag` 引用 |
| `test_sys_modules_alias_raganything` | `from raganything import RAGAnything` 仍解析到 `graph_rag.raganything` |
| `test_sys_modules_alias_lightrag` | `from lightrag import LightRAG` 仍解析到合并后的模块 |
| `test_lightrag_singleton_init` | `get_lightrag()` 正常初始化（storage + FTS + entities_vdb attach） |
| `test_fts_disabled_e2e` | `fts_hybrid_enabled=False` 时全 mode 行为和合并前一致 |

### E2E 测试

| 测试 | 内容 |
|------|------|
| `test_document_ingest_e2e` | 上传文档 → 实体抽取 → 图存储 → FTS5 索引同步 → entities_fts 可搜到 |
| `test_query_local_e2e` | local mode 查询返回有效结果 |
| `test_query_global_e2e` | global mode 查询返回有效结果（纯 vector，不变） |
| `test_query_hybrid_e2e` | hybrid mode 查询返回有效结果 |
| `test_query_naive_e2e` | naive mode 查询返回有效结果（纯 vector，不变） |
| `test_query_mix_e2e` | mix mode 查询返回有效结果 |
| `test_query_path_e2e` | path mode 查询返回有效路径结果 |
| `test_http_knowledge_graph_endpoint` | `GET /knowledge-graph` 返回图数据 |
| `test_http_upload_endpoint` | `POST /knowledge-graph/upload` 摄入文档成功 |

### 运行方式

```bash
# 单元测试
uv run --no-sync pytest tests/skills/multimodal_rag/ -q -k "not e2e and not llm_e2e"

# 回归测试（import 路径 + sys.modules + singleton）
uv run --no-sync pytest tests/skills/multimodal_rag/ -q -k "import or alias or singleton or fts_disabled"

# E2E 测试（需要模型可用 + 文档上传）
uv run --no-sync pytest tests/skills/multimodal_rag/ -q -k "e2e" -s

# 全门
uv run --no-sync python tests/run_tests_split.py
```

---

## 6. 风险

| 风险 | 缓解 |
|------|------|
| FTS5 和 SNKV 不一致 | upsert/delete 同步写，同一调用内 |
| SQLite 文件膨胀 | 实体数通常 < 10K，FTS5 < 5MB |
| FTS 无命中时退化 | `fts_hits=[]` → 纯 vector |
| dataclass 加字段破坏现有构造 | `_fts` 默认 None，`partial(SNKVVectorStorage, global_config=...)` 不传 `_fts` |
| 合并后 import 残留 `vendored_lightrag` | `test_import_paths_resolve` 扫描所有 `.py` 文件 grep `vendored` |
| `base.py` 重命名导致 `from .base import` 断裂 | sed 批量替换 `from .base import` → `from .lightrag_base import`（LightRAG 侧）和 `from .sherry_adapter import`（Sherry 侧） |
| sys.modules 别名失效 | `test_sys_modules_alias_*` 验证 `from lightrag import` 和 `from raganything import` 仍解析 |
| 向后兼容 | `fts_hybrid_enabled=False` → 不 attach → 全纯 vector |

### Kill criteria

- 若 FTS5 同步在批量写入时慢，改为批量 sync（攒 100 个再写）
- 若混合精度不如纯 vector，降低 fts_weight 到 0.2
- 若合并后大量 import 断裂难修，退为只删 `vendored_` 前缀但不改目录结构
- 若 dataclass 加字段和 frozen 冲突，用 `object.__setattr__` 绕过

---

## 7. ROI

### 改动量

| 改动 | 行数 |
|------|------|
| fts_shadow_index.py | ~120 行 |
| snkv_vector_impl.py（attach + sync + query 混合） | ~30 行 |
| sherry_adapter.py（get_lightrag attach FTS） | ~10 行 |
| 配置 | ~5 行 |
| 删除 bypass | ~-30 行 |
| 合并目录（import 替换 + 重命名 + sys.modules） | ~0 净增（纯重构） |
| 测试 | 27 个 |
| **总计** | **~135 行净增 + 27 个测试 + ~106 处 import 机械替换** |

### 收益

| 收益 | 影响 |
|------|------|
| local/hybrid/mix/path 实体检索精确匹配 | 函数名/文件名/代码标识符 FTS5 精确命中 |
| global/naive 不变 | 不改不确定的场景 |
| 删除 bypass | 少一个无用 mode |
| 合并 vendored 目录 | import 路径缩短、vendored_ 前缀不再准确 |
| 向后兼容 | `fts_hybrid_enabled=False` 全纯 vector |

### ROI 判断

**高 ROI**。FTS5 ~135 行给确定变好的 entities_vdb 加混合检索，删除 bypass 减少冗余，合并目录是纯重构零逻辑改动。27 个测试覆盖单元 + 回归 + E2E 确保功能无损。
