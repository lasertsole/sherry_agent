# LT-1 分层记忆存储 — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §LT-1 (行 1678-1868)
> 决策: 方案C（索引可选）+ 扩展现有 memory 工具
> 预估工时: 2 天

---

## 1. 问题

`MemoryStore`（`agent/tools/memory.py:115`）管理两个扁平文件：MEMORY.md（2200 字符硬限）和 USER.md（1375 字符）。所有事实（环境、项目约定、决策、工具经验、用户偏好）竞争同一 2200 字符预算。

长程任务跨多次会话积累大量事实 → 早期关键事实被临时事实挤出 → 用户偏好、项目约定等持久信息丢失。

**当前状态：**

- `workspace/memory/` 仅包含 `MEMORY.md` + `USER.md`，无 `facts/` 目录
- 无 `memory_tiered.py`
- `config/num.py` 无分层记忆配置
- P0-1（Memory Flush）和 P2-3（Facts 提取）均未实现——它们是 facts 层的未来消费者

---

## 2. 设计决策

### 2.1 三层架构

```
Layer 1: MEMORY.md (2200 chars, 完整预算, 注入系统提示词)
         + USER.md (1375 chars, 注入系统提示词)
         + FACTS LISTING (~150 chars, prompt_builder 自动注入, 仅文件名+条目数)
Layer 2: facts/*.md (每文件 4000 chars 上限, 不注入系统提示词, 按需工具读取)
  ├── environment.md   — 环境事实 (OS, Python 版本, 依赖)
  ├── project.md       — 项目约定 (命名规范, 架构决策)
  ├── decisions.md     — 关键决策记录 (为什么选 X 不选 Y)
  ├── user_prefs.md    — 用户偏好深度 (比 USER.md 更详细)
  └── tool_lessons.md  — 工具使用经验 (常见坑, 最佳实践)
Layer 3: mes_memory.db (原始对话历史, 已存在)
```

### 2.2 关键决策

| 决策点              | 选择                            | 理由                                                                           |
| ------------------- | ------------------------------- | ------------------------------------------------------------------------------ |
| MEMORY.md 索引      | **不写入**（方案C）             | 保持完整 2200 字符预算；prompt_builder 另注入一行 facts 文件列表               |
| prompt_builder 注入 | **自动注入 facts 文件列表**     | Agent 知道有哪些 facts 可查（仅文件名+条目数，非内容摘要），约 100-150 字符    |
| 工具入口            | **扩展现有 memory 工具**        | 一个工具管所有记忆，入口统一；新增 `fact_add`/`fact_read`/`fact_search` action |
| 条目分隔符          | `§`（与 MemoryStore 一致）      | 代码复用，行为一致                                                             |
| 线程安全            | 复用 `MemoryStore._file_lock()` | 已有跨平台（fcntl + msvcrt）文件锁实现                                         |
| 容量截断            | pop 最旧条目                    | 与 P0-1 `append_entries()` 设计一致                                            |
| 去重                | 子串精确匹配                    | 简单可靠，避免近似去重误判                                                     |
| 搜索                | 子串匹配（大小写不敏感）        | 初版简单实现，后续可替换为 FTS5                                                |

---

## 3. 涉及文件

| 文件                                      | 操作                       | 预估行数 |
| ----------------------------------------- | -------------------------- | -------- |
| `config/num.py`                           | 修改：新增常量             | ~5       |
| `config/path.py`                          | 修改：新增 `FACTS_DIR`     | ~2       |
| `config/__init__.py`                      | 修改：导出 `FACTS_DIR`     | ~1       |
| `agent/tools/memory_tiered.py`            | **新建**                   | ~120     |
| `agent/tools/memory.py`                   | 修改：扩展工具 action      | ~60      |
| `agent/tools/__init__.py`                 | 修改：导出 `tiered_store`  | ~2       |
| `workspace/prompt_builder.py`             | 修改：注入 facts 文件列表  | ~20      |
| `workspace/memory/facts/`                 | 新建目录 + 5 个空 .md 文件 | —        |
| `tests/agent/tools/test_memory_tiered.py` | **新建**                   | ~120     |

---

## 4. 详细设计

### 4.1 config/num.py — 新增常量

```python
# === Tiered Memory (LT-1) ===
FACTS_CHAR_LIMIT = 4000          # 每个 facts 文件的字符上限
FACTS_INDEX_MAX_CHARS = 200      # 每类 facts 在 prompt 中占用的索引字符上限（预留）
FACTS_CATEGORIES = (
    "environment",
    "project",
    "decisions",
    "user_prefs",
    "tool_lessons",
)
```

### 4.2 config/path.py — 新增路径

```python
FACTS_DIR = MEMORY_DIR / "facts"
```

### 4.3 config/**init**.py — 导出

```python
from .path import (
    ...,
    FACTS_DIR as FACTS_DIR,
)
```

### 4.4 agent/tools/memory_tiered.py — 新建

```python
"""
分层记忆管理器：协调 facts/ 结构化事实层。

facts/ 文件不注入系统提示词，按需通过 memory 工具读取/搜索。
prompt_builder 自动注入一行 facts 文件列表（仅文件名+条目数）。

设计参考: oh-my-openagent Git-backed 多文件记忆 + hermes-agent MemoryStore 分类
"""

from pathlib import Path
from loguru import logger

from config import FACTS_DIR
from config.num import FACTS_CHAR_LIMIT, FACTS_CATEGORIES
from agent.tools.memory import MemoryStore, ENTRY_DELIMITER

_FACTS_FILES: dict[str, Path] = {
    cat: FACTS_DIR / f"{cat}.md" for cat in FACTS_CATEGORIES
}


class TieredMemoryStore:
    """管理 facts/ 目录下的结构化事实文件。"""

    def __init__(self, memory_store: MemoryStore):
        self._memory_store = memory_store
        FACTS_DIR.mkdir(parents=True, exist_ok=True)
        for path in _FACTS_FILES.values():
            path.touch(exist_ok=True)

    def add_fact(self, category: str, fact: str) -> dict:
        """
        将事实写入 facts/<category>.md。
        去重：已存在相同文本则跳过。
        容量：超过 FACTS_CHAR_LIMIT 时 pop 最旧条目。
        """
        if category not in _FACTS_FILES:
            return {"success": False, "error": f"Unknown category '{category}'. Use: {', '.join(FACTS_CATEGORIES)}"}

        fact = fact.strip()
        if not fact:
            return {"success": False, "error": "Fact content cannot be empty."}

        path = _FACTS_FILES[category]

        with self._memory_store._file_lock(path):
            raw = path.read_text(encoding="utf-8") or ""
            entries = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()] if raw.strip() else []

            # 去重
            if fact in entries:
                return {
                    "success": True,
                    "message": "Fact already exists (no duplicate added).",
                    "category": category,
                    "entry_count": len(entries),
                    "usage": f"{len(raw)}/{FACTS_CHAR_LIMIT} chars",
                }

            entries.append(fact)
            combined = ENTRY_DELIMITER.join(entries)

            # 容量截断：pop 最旧
            while len(combined) > FACTS_CHAR_LIMIT and len(entries) > 1:
                entries.pop(0)
                combined = ENTRY_DELIMITER.join(entries)

            path.write_text(combined, encoding="utf-8")

        return {
            "success": True,
            "message": "Fact added.",
            "category": category,
            "entry_count": len(entries),
            "usage": f"{len(combined)}/{FACTS_CHAR_LIMIT} chars",
        }

    def read_facts(self, category: str | None = None) -> dict[str, str]:
        """
        读取 facts 文件内容。
        category=None 返回全部类别。
        """
        result = {}
        if category is not None:
            if category not in _FACTS_FILES:
                return {}
            return {category: _FACTS_FILES[category].read_text(encoding="utf-8") or ""}
        for cat, path in _FACTS_FILES.items():
            result[cat] = path.read_text(encoding="utf-8") or ""
        return result

    def search_facts(self, query: str) -> list[dict]:
        """
        在 facts 文件中搜索匹配查询的条目（子串匹配，大小写不敏感）。
        """
        query_lower = query.lower()
        results = []
        for cat, path in _FACTS_FILES.items():
            content = path.read_text(encoding="utf-8") or ""
            for entry in content.split(ENTRY_DELIMITER):
                entry = entry.strip()
                if entry and query_lower in entry.lower():
                    results.append({"category": cat, "fact": entry})
        return results

    def get_facts_listing(self) -> str:
        """
        生成 facts 文件列表摘要（仅文件名+条目数），用于 prompt_builder 注入。
        仅列出有内容的文件。
        格式: environment (12 entries) · project (5 entries) · ...
        """
        parts = []
        for cat, path in _FACTS_FILES.items():
            content = path.read_text(encoding="utf-8") or ""
            if not content.strip():
                continue
            count = len([e for e in content.split(ENTRY_DELIMITER) if e.strip()])
            parts.append(f"{cat} ({count} entries)")
        if not parts:
            return ""
        return " · ".join(parts)


tiered_store: TieredMemoryStore | None = None


def get_tiered_store() -> TieredMemoryStore:
    """延迟初始化 TieredMemoryStore（需要 MemoryStore 实例）。"""
    global tiered_store
    if tiered_store is None:
        from agent.tools.memory import memory_store
        tiered_store = TieredMemoryStore(memory_store)
    return tiered_store
```

### 4.5 agent/tools/memory.py — 扩展工具

#### MemoryActionSchema 新增 action

```python
class MemoryActionSchema(BaseModel):
    action: Literal["add", "replace", "remove", "fact_add", "fact_read", "fact_search"] = Field(
        description=(
            "The action to perform. "
            "'add'/'replace'/'remove' manage MEMORY.md and USER.md entries. "
            "'fact_add' writes a fact to facts/<target>.md (target=category). "
            "'fact_read' reads facts (target=category or 'all'). "
            "'fact_search' searches facts by substring (content=query)."
        )
    )
    target: str = Field(
        default="memory",
        description=(
            "Which store: 'memory' or 'user' for add/replace/remove; "
            "category name (environment|project|decisions|user_prefs|tool_lessons) for fact_add; "
            "category name or 'all' for fact_read."
        )
    )
    content: str | None = Field(
        default=None,
        description="Entry content (add/replace), fact text (fact_add), or search query (fact_search)."
    )
    old_text: str | None = Field(
        default=None,
        description="Short unique substring for replace/remove. Not used for fact_* actions."
    )
```

#### memory_tool() 新增分支

```python
# 在 memory_tool() 函数中，现有 action 分支之后新增：

elif action == "fact_add":
    if not content:
        return _tool_error("Content is required for 'fact_add' action.", success=False)
    if not target:
        return _tool_error("Target (category) is required for 'fact_add' action.", success=False)
    from agent.tools.memory_tiered import get_tiered_store
    result = get_tiered_store().add_fact(target, content)
    return json.dumps(result, ensure_ascii=False)

elif action == "fact_read":
    cat = None if target == "all" or not target else target
    from agent.tools.memory_tiered import get_tiered_store
    result = get_tiered_store().read_facts(cat)
    return json.dumps({"success": True, "facts": result}, ensure_ascii=False)

elif action == "fact_search":
    if not content:
        return _tool_error("Content (search query) is required for 'fact_search' action.", success=False)
    from agent.tools.memory_tiered import get_tiered_store
    result = get_tiered_store().search_facts(content)
    return json.dumps({"success": True, "results": result, "count": len(result)}, ensure_ascii=False)
```

#### MemoryTool.description 新增说明

在现有 description 末尾追加：

```
\n\n"
"FACTS LAYER (structured, on-demand, not in system prompt):\n"
"Use 'fact_add' to save categorized facts to facts/<category>.md files.\n"
"Categories: environment, project, decisions, user_prefs, tool_lessons.\n"
"Use 'fact_read' (target=category or 'all') to read facts.\n"
"Use 'fact_search' (content=query) to search facts by substring.\n"
"Facts files hold 4000 chars each — use for detailed, categorized knowledge\n"
"that doesn't fit in MEMORY.md's 2200-char budget.\n"
```

### 4.6 workspace/prompt_builder.py — 注入 facts 文件列表

在 `build_system_prompt()` 中，memory block 之后追加：

```python
# --- Facts listing (LT-1) -----------------------------------------
# One-liner listing non-empty facts files so the agent knows what's
# available to read via the memory tool's fact_read/fact_search actions.
# ~100-150 chars, far cheaper than embedding full facts content.
if selected_file_names is None:
    try:
        from agent.tools.memory_tiered import get_tiered_store
        facts_listing = get_tiered_store().get_facts_listing()
        if facts_listing:
            file_paths.append(
                f"FACTS (on-demand, use memory tool with fact_read/fact_search):\n  {facts_listing}"
            )
    except Exception:
        pass  # Non-critical: don't break system prompt if facts layer fails
```

注入位置：在 `memory_store.format_for_system_prompt()` 之后、todo/boulder blocks 之前。

### 4.7 workspace/memory/facts/ — 目录结构

```
workspace/memory/
├── MEMORY.md              # 已有
├── USER.md                # 已有
├── MEMORY.md.lock         # 已有（运行时生成）
├── USER.md.lock           # 已有（运行时生成）
└── facts/                 # 新建
    ├── environment.md     # 空 .md 文件
    ├── project.md
    ├── decisions.md
    ├── user_prefs.md
    └── tool_lessons.md
```

每个文件初始为空。`TieredMemoryStore.__init__()` 调用 `path.touch(exist_ok=True)` 确保文件存在。

---

## 5. 实施顺序

```
Step 1: config/num.py        — 新增常量
Step 2: config/path.py       — 新增 FACTS_DIR
Step 3: config/__init__.py   — 导出 FACTS_DIR
Step 4: 创建 workspace/memory/facts/ 目录 + 5 个空文件
Step 5: agent/tools/memory_tiered.py — 新建 TieredMemoryStore
Step 6: agent/tools/memory.py — 扩展 memory_tool() + schema + description
Step 7: agent/tools/__init__.py — 导出 get_tiered_store
Step 8: workspace/prompt_builder.py — 注入 facts 文件列表
Step 9: tests/agent/tools/test_memory_tiered.py — 编写测试
Step 10: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

### 6.1 单元测试（tests/agent/tools/test_memory_tiered.py）

| 测试                                    | 说明                                                |
| --------------------------------------- | --------------------------------------------------- |
| `test_add_fact_writes_correct_category` | 写入 environment 类别 → 文件内容包含该事实          |
| `test_add_fact_dedup`                   | 写入相同事实 → 返回 "already exists"，文件不变      |
| `test_add_fact_capacity_truncation`     | 写入超过 4000 字符 → pop 最旧条目，总长 ≤ 4000      |
| `test_add_fact_unknown_category`        | 写入未知类别 → 返回 error                           |
| `test_add_fact_empty_content`           | 空内容 → 返回 error                                 |
| `test_read_facts_single_category`       | 读取单个类别 → 返回正确内容                         |
| `test_read_facts_all`                   | target=all → 返回全部 5 个类别                      |
| `test_read_facts_empty_file`            | 空文件 → 返回空字符串                               |
| `test_search_facts_case_insensitive`    | 大小写不敏感搜索                                    |
| `test_search_facts_no_match`            | 无匹配 → 返回空列表                                 |
| `test_search_facts_multiple_categories` | 跨类别匹配                                          |
| `test_get_facts_listing_empty`          | 所有文件空 → 返回空字符串                           |
| `test_get_facts_listing_non_empty`      | 有内容的文件 → 返回 "environment (2 entries) · ..." |

### 6.2 集成测试

| 测试                                        | 说明                                     |
| ------------------------------------------- | ---------------------------------------- |
| `test_memory_tool_fact_add`                 | 通过 memory 工具 action=fact_add 写入    |
| `test_memory_tool_fact_read`                | 通过 memory 工具 action=fact_read 读取   |
| `test_memory_tool_fact_search`              | 通过 memory 工具 action=fact_search 搜索 |
| `test_prompt_builder_injects_facts_listing` | prompt_builder 注入 facts 文件列表       |
| `test_prompt_builder_no_facts_when_empty`   | facts 文件全空 → 不注入 facts listing    |
| `test_facts_do_not_consume_memory_budget`   | 写入 facts 后 MEMORY.md 字符数不变       |

### 6.3 运行验证

```bash
# 运行测试
python -m pytest tests/agent/tools/test_memory_tiered.py -v

# Lint + Typecheck
ruff check agent/tools/memory_tiered.py agent/tools/memory.py workspace/prompt_builder.py
pyright agent/tools/memory_tiered.py agent/tools/memory.py workspace/prompt_builder.py

# 端到端验证：启动 Agent，通过 memory 工具写入 facts，检查 prompt 中包含 facts listing
```

---

## 7. 数据流

### 7.1 写入事实

```
Agent 调用 memory(action="fact_add", target="environment", content="Python 3.12 on Windows")
  → memory_tool() 分发到 fact_add 分支
  → get_tiered_store().add_fact("environment", "Python 3.12 on Windows")
  → TieredMemoryStore 写入 facts/environment.md (§ 分隔, 去重, 容量截断)
  → 返回 {success: true, category: "environment", entry_count: 1, usage: "20/4000 chars"}
```

### 7.2 读取事实

```
Agent 调用 memory(action="fact_read", target="environment")
  → get_tiered_store().read_facts("environment")
  → 读取 facts/environment.md
  → 返回 {success: true, facts: {"environment": "Python 3.12 on Windows\n§\n..."}}
```

### 7.3 搜索事实

```
Agent 调用 memory(action="fact_search", content="python")
  → get_tiered_store().search_facts("python")
  → 遍历 5 个 facts 文件，子串匹配（大小写不敏感）
  → 返回 {success: true, results: [{"category": "environment", "fact": "Python 3.12 on Windows"}], count: 1}
```

### 7.4 系统提示词注入

```
build_system_prompt(session_id=...)
  → 读取 MEMORY.md 快照 (2200 chars 完整预算)
  → 读取 USER.md 快照 (1375 chars)
  → get_tiered_store().get_facts_listing()
    → 遍历 facts/*.md，仅列出有内容的文件
    → 返回 "environment (2 entries) · project (1 entries)"
  → 拼接为 "FACTS (on-demand, use memory tool with fact_read/fact_search):\n  environment (2 entries) · project (1 entries)"
  → 注入到系统提示词中（~100-150 字符）
```

---

## 8. 与其他方案的关系

| 方案                       | 关系                                                                  |
| -------------------------- | --------------------------------------------------------------------- |
| **P0-1 Memory Flush**      | 未实现。实现后将写入 facts/ 层而非 MEMORY.md。LT-1 提供基础设施。     |
| **P2-3 Facts 提取**        | 未实现。实现后将自动从对话中提取事实到 facts/ 层。LT-1 提供写入目标。 |
| **LT-3 摘要退化防护**      | 依赖 LT-1。压缩摘要从分层记忆重建基准线，不依赖前次摘要链。           |
| **LT-4 TaskFlow 知识提取** | 依赖 LT-1 + P0-1。任务完成时提取知识写入 facts/ 层。                  |
| **LT-5 子代理记忆回流**    | 依赖 LT-1。子代理结果中的事实回流到 facts/ 层。                       |
| **LT-6 记忆过期清理**      | 依赖 LT-1。对 facts/ 文件执行 TTL 清理和冲突检测。                    |

---

## 9. 风险与缓解

| 风险                         | 缓解                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------- |
| facts/ 文件无限增长          | 每文件 4000 字符硬限 + pop 最旧条目截断                                                      |
| 并发写入冲突                 | 复用 MemoryStore._file_lock() 跨平台文件锁                                                   |
| prompt_builder 注入失败      | try/except 包裹，静默降级（不影响系统提示词生成）                                            |
| Agent 不知道何时用 facts     | MemoryTool.description 中明确说明 facts 用途和何时使用                                       |
| 与 P0-1/P2-3 集成时 API 变更 | TieredMemoryStore 接口稳定（add_fact/read_facts/search_facts），P0-1/P2-3 作为消费者调用即可 |
