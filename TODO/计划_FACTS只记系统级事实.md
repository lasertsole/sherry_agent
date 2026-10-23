# 计划：FACTS.md 改为系统级+跨项目级单文件（放弃 per-project）

- 建档日期：2026-10-23
- 状态：**计划（未开工）**（替代原"双存模型"方案）
- **文档范围**：FACTS.md 只保留全局单文件（`SHERRY_HOME/workspace/memory/FACTS.md`），只记录系统级、跨项目级事实。不创建 per-project FACTS.md——项目级事实困在项目目录里切换后不可见。`.sherry/` 目录不放 FACTS.md。nudge 提示词更新路由规则：只记跨项目事实，项目级的别放 FACTS.md。
- 关联代码：
  - `agent/tools/memory.py:203-219`（`_project_facts_path`——**删除**，不再支持 per-project）
  - `agent/tools/memory.py:221-229`（`_path_for("facts")`——简化为始终返回全局路径）
  - `agent/tools/memory.py:129+`（`MemoryStore`——不需要 scope 参数）
  - `agent/middlewares/summarization/nudges.py:31-51`（`_MEMORY_REVIEW_PROMPT`——更新路由规则）
  - `agent/middlewares/summarization/nudges.py:274-301`（`_PLAN_EXTRACTION_PROMPT` Part 3——同上）
  - `agent/middlewares/summarization/nudges.py:54-63`（`_render_facts_context`——简化，只读全局）
  - `workspace/prompt_builder.py:422-433`（`format_for_system_prompt("facts")`——只返回全局块）
- 关联 TODO：
  - `计划_统一项目隐藏目录到sherry.md` §2.6（FACTS.md 搬到 `.sherry/memory/`——**废弃此方案**，不搬）
  - `计划_会话工作目录不可变.md`（目录不可变——FACTS 不需要处理切换）

---

## 0. 为什么放弃 per-project

### 0.1 核心缺陷：事实困住

per-project FACTS.md 的根本问题——事实写入某个项目的文件后，切换到另一个项目就看不到了。nudge 只能看当前项目 + 全局，看不到其他项目的 FACTS，无法发现和迁移错位事实。

```
Project A 工作 → nudge 判断失误 → 系统级事实写入 Project A 的 FACTS
切换到 Project B → 看不到 Project A 的 FACTS → 事实永远困住
```

### 0.2 解决不了的根本原因

即使用 `[project:hash]` 标签单文件方案，项目级事实仍然需要 per-project 标签管理——增加复杂度。而用户的需求是"只记系统级的、跨项目级的"——项目级事实根本不该进 FACTS.md。

### 0.3 项目级事实去哪

项目级事实（"这个项目的迁移脚本要先跑 alembic"、"这个项目用 pnpm"）不进 FACTS.md——它们应该：
- 模块级教训 → `<module>-notes` skill（已有路由规则）
- 通用项目笔记 → MEMORY.md（已有路由规则）
- 项目级约定 → AGENTS.md（用户手动写，或 `/init` 生成）

FACTS.md 只管"任何项目都可能遇到的系统级坑"——如"uv run pytest needs --no-sync"、"python_repl 30s 超时"。

---

## 1. 设计

### 1.1 存储

**一个文件，全局唯一：**

```
SHERRY_HOME/workspace/memory/FACTS.md
```

不创建 `.sherry/memory/FACTS.md`。`_project_facts_path` 方法删除。`_path_for("facts")` 简化为始终返回全局路径。

### 1.2 内容

只记录系统级、跨项目级事实——任何项目都可能遇到的工具链行为、环境配置、通用命令约束：

```
§ uv run pytest needs --no-sync flag
§ python_repl hits 30s timeout for full suites — split into batches
§ curl -sSL https://... | bash requires -sS not just -s for progress bars
§ git worktree add requires the branch to not exist on the main repo
```

**不记录的**（路由到其他地方）：
- 用户偏好 → USER.md
- 通用 agent 笔记 → MEMORY.md
- 模块级教训 → `<module>-notes` skill
- 项目级约定 → AGENTS.md（用户写）或 MEMORY.md

### 1.3 判断标准

nudge 提示词里的判断指导：

**"换一个项目还会遇到吗？"**
- 是 → FACTS.md（系统级/跨项目）
- 否 → 别进 FACTS.md（路由到 MEMORY.md / USER.md / skill / AGENTS.md）

---

## 2. 改动清单

### 2.1 `agent/tools/memory.py` — 删除 per-project 逻辑

**删除**：
- `_project_facts_path` 方法（line 203-219）
- `_path_for` 里 `target == "facts"` 的 per-project 分支（line 224-228）

**简化 `_path_for`**：

```python
def _path_for(self, target: str, session_id: str | None = None,
              *, for_read: bool = False) -> Path:
    if target == "facts":
        return MEMORY_DIR / "FACTS.md"  # 始终全局
    return self._global_path_for(target)
```

**不需要 `scope` 参数**——`MemoryActionSchema` 不加 scope 字段。`memory` 工具的 `target="facts"` 始终写全局。

### 2.2 `agent/middlewares/summarization/nudges.py` — 更新路由规则

#### 2.2.1 `_MEMORY_REVIEW_PROMPT` — 焦点 3 更新

当前（line 38-40）：
```
"3. Did the work surface a broad pitfall or convention that is NOT bound to a "
"specific file/module — something any future task in this environment could hit? "
"(target: 'facts')\n\n"
```

改为：
```
"3. Did the work surface a SYSTEM-LEVEL or CROSS-PROJECT pitfall — something "
"that ANY project on this machine could hit (not specific to the current "
"project)? Examples: toolchain behavior ('uv run pytest needs --no-sync'), "
"environment constraints ('python_repl hits 30s timeout'), generic command "
"gotchas. (target: 'facts')\n\n"
"IMPORTANT: FACTS.md is for SYSTEM-LEVEL facts only — things true in ANY "
"project. Project-specific facts (e.g. 'this project uses pnpm') do NOT "
"belong in FACTS.md — they go to MEMORY.md or AGENTS.md.\n\n"
```

#### 2.2.2 routing 规则更新

当前（line 41-43）：
```
"Routing: user preferences and traits go to 'user'; general agent-side notes about "
"this environment/project go to 'memory'; broad cross-plan pitfalls and conventions "
"go to 'facts'.\n\n"
```

改为：
```
"Routing: user preferences and traits go to 'user'; project-specific notes "
"and conventions go to 'memory'; SYSTEM-LEVEL/CROSS-PROJECT pitfalls go to "
"'facts' (only if true in ANY project, not just the current one).\n\n"
```

#### 2.2.3 `_PLAN_EXTRACTION_PROMPT` Part 3 — 同样更新

Part 3 的路由规则（line 282-287）当前：
```
- Broad, cross-plan pitfall or convention (not bound to one module) → FACTS.md.
- General agent-side environment/project note → MEMORY.
```

改为：
```
- SYSTEM-LEVEL/CROSS-PROJECT pitfall (true in ANY project, not just this one) → FACTS.md.
- Project-specific pitfall or convention → MEMORY (not FACTS.md).
```

Part 3 的写入指令（line 289）当前：
```
To write: call the memory tool with target="facts" (actions: add / replace /
remove). Keep each entry to one short line — symptom plus avoidance.
```

改为：
```
To write: call the memory tool with target="facts" (actions: add / replace /
remove). Keep each entry to one short line — symptom plus avoidance. ONLY
write system-level/cross-project facts here. If a fact is specific to the
current project, write it to MEMORY.md (target="memory") instead.
```

### 2.3 `workspace/prompt_builder.py` — 简化注入

`format_for_system_prompt("facts")` 不需要 `session_id` 参数——始终读全局 FACTS.md：

```python
# build_system_prompt 里
file_paths.extend(
    content
    for content in (
        provider.format_memory_for_system_prompt("memory"),
        provider.format_memory_for_system_prompt("user"),
        provider.format_memory_for_system_prompt("facts"),  # 始终全局
    )
    if content
)
```

不需要 per-session 快照——全局 FACTS.md 在 boot 时 `load_from_disk()` 加载，与 MEMORY.md/USER.md 一样。

### 2.4 `计划_统一项目隐藏目录到sherry.md` §2.6 — 废弃

原计划把 FACTS.md 搬到 `.sherry/memory/FACTS.md`（per-project）。**废弃此方案**——FACTS.md 留在全局 `SHERRY_HOME/workspace/memory/FACTS.md`。

`.sherry/` 目录不放 FACTS.md——减少项目目录污染。

### 2.5 删除 `_project_facts_path` 相关代码

| 文件 | 行 | 改动 |
|---|---|---|
| `agent/tools/memory.py` | 203-219 | 删 `_project_facts_path` 方法 |
| `agent/tools/memory.py` | 221-229 | 简化 `_path_for("facts")` 为始终返回全局 |
| `agent/tools/memory.py` | `format_live_content` | 不传 session_id 解析 per-project 路径 |
| `agent/tools/memory.py` | `format_for_system_prompt` | 不传 session_id |
| `server/service/memory.py` | `_MemoryFileStore` | 不需要 session_id 解析 FACTS 路径 |

### 2.6 HTTP API

`GET/PUT /memory` 的 `target=facts` 不需要 `session_id` 参数——始终读写全局 FACTS.md。

---

## 3. nudge 提示词最终版

### 3.1 `_MEMORY_REVIEW_PROMPT` 焦点 3

```
3. Did the work surface a SYSTEM-LEVEL or CROSS-PROJECT pitfall — something
that ANY project on this machine could hit (not specific to the current
project)? Examples: toolchain behavior ('uv run pytest needs --no-sync'),
environment constraints ('python_repl hits 30s timeout'), generic command
gotchas. (target: 'facts')

IMPORTANT: FACTS.md is for SYSTEM-LEVEL facts only — things true in ANY
project. Project-specific facts (e.g. 'this project uses pnpm') do NOT
belong in FACTS.md — they go to MEMORY.md or AGENTS.md.

Routing: user preferences and traits go to 'user'; project-specific notes
and conventions go to 'memory'; SYSTEM-LEVEL/CROSS-PROJECT pitfalls go to
'facts' (only if true in ANY project, not just the current one).

Current FACTS.md content (target 'facts', limit {facts_limit} chars):
<facts>
{facts_block}
</facts>

Maintain it like an editor, not an appender: merge entries describing the
same pitfall, replace outdated ones, and prefer saving nothing over
bloating the file (prefer nothing over noise). If a fact is project-
specific, remove it from FACTS.md and add it to MEMORY.md instead.
```

### 3.2 `_PLAN_EXTRACTION_PROMPT` Part 3

```
## Part 3: System-Level Pitfalls → FACTS.md

Not every durable lesson is bound to a module or belongs to a skill class.
SYSTEM-LEVEL pitfalls — toolchain behavior, environment constraints,
generic command gotchas that ANY project on this machine could hit — are
kept in FACTS.md.

Judge each candidate against the routing rules:
- Bound to a specific file / module / test → Part 2 `<module>-notes` skill.
- About the user (preferences, traits, expectations) → USER.
- A reusable class of work or technique → Part 2 skill.
- SYSTEM-LEVEL/CROSS-PROJECT pitfall (true in ANY project) → FACTS.md.
- Project-specific pitfall or convention → MEMORY (not FACTS.md).

To write: call the memory tool with target="facts" (actions: add / replace /
remove). Keep each entry to one short line — symptom plus avoidance. ONLY
write system-level/cross-project facts here. If a fact is specific to the
current project, write it to MEMORY.md (target="memory") instead.

Current FACTS.md content (target 'facts', limit {facts_limit} chars):
<facts>
{facts_block}
</facts>

Maintain it like an editor: merge entries describing the same pitfall, replace
outdated ones, and prefer saving nothing over bloating the file (prefer
nothing over noise). If a fact is project-specific, remove it from FACTS.md
and add it to MEMORY.md instead.
```

### 3.3 迁移规则

nudge 看到当前 FACTS.md 内容时，检查是否有项目级事实错放在里面：

```
If a fact in FACTS.md is project-specific (only true for the current project,
not any project), remove it from FACTS.md (memory action='remove',
target='facts') and add it to MEMORY.md (memory action='add',
target='memory').
```

---

## 4. 容量管理

**不变**——全局 FACTS.md 1,375 字符上限，oldest-first 驱逐。因为只存系统级事实（数量有限），1,375 字符够用。

---

## 5. 验收标准

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S1：只有一个 FACTS.md** | 检查 `.sherry/memory/FACTS.md` | 不存在——FACTS.md 只在 `SHERRY_HOME/workspace/memory/` |
| **S2：`_path_for("facts")` 始终全局** | 不传 session_id 调用 | 返回 `MEMORY_DIR / "FACTS.md"` |
| **S3：`_project_facts_path` 不存在** | 检查 memory.py | 方法已删除 |
| **S4：nudge 只写系统级** | nudge 提示词 | 焦点 3 和 Part 3 都说"SYSTEM-LEVEL/CROSS-PROJECT"，且明确说"project-specific → MEMORY.md" |
| **S5：nudge 迁移规则** | nudge 提示词 | "If a fact is project-specific, remove from FACTS.md and add to MEMORY.md" |
| **S6：系统提示只注入全局 FACTS** | 构建系统提示 | 只有一个 FACTS 块（全局），无项目级块 |
| **S7：冻结快照不变** | mid-session 写入 | 系统提示不变（保护 prefix cache） |
| **S8：HTTP API 不需要 session_id** | `GET/PUT /memory?target=facts` | 不传 session_id 也能正常读写 |
