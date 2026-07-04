---
name: llm-wiki
description: "Karpathy's LLM Wiki: build/query interlinked markdown KB."
version: 3.0.0
author: Hermes Agent (adapted for EMA_AI_agent)
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [wiki, knowledge-base, research, notes, markdown, rag-alternative]
    category: research
    related_skills: [obsidian, arxiv]
---

# Karpathy's LLM Wiki

Build and maintain a persistent, compounding knowledge base as interlinked markdown files.
Based on [Andrej Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Unlike traditional RAG (which rediscovers knowledge from scratch per query), the wiki
compiles knowledge once and keeps it current. Cross-references are already there.
Contradictions have already been flagged. Synthesis reflects everything ingested.

**Division of labor:** The human curates sources and directs analysis. The agent
summarizes, cross-references, files, and maintains consistency.

## When This Skill Activates

Use this skill when the user:
- Asks to create, build, or start a wiki or knowledge base
- Asks to ingest, add, or process a source into their wiki
- Asks a question and an existing wiki is present at the configured path
- Asks to lint, audit, or health-check their wiki
- References their wiki, knowledge base, or "notes" in a research context

## Wiki 目录结构

所有路径均通过脚本动态计算，基于项目根目录下的 `src/data/wiki/`。

```json
{
  "wiki": {
    "root": ["SCHEMA.md", "index.md", "log.md"],
    "raw": {
      "description": "原始资料（只读，不可修改）",
      "articles": "网页文章/网络资料",
      "papers": "论文/研究报告",
      "transcripts": "对话记录/访谈",
      "assets": "图片/附件资源"
    },
    "entities": "人物/组织档案",
    "concepts": "概念/主题解析",
    "comparisons": "对比分析",
    "queries": "查询结果存档"
  }
}
```

## 快速开始

### 查看Wiki路径和结构

```python
from skills.builtin.llm-wiki.scripts import get_wiki_path, print_structure

print(f"Wiki路径: {get_wiki_path()}")
print(f"目录结构: {print_structure()}")
```

### 初始化Wiki

```python
from skills.builtin.llm-wiki.scripts import init_wiki

result = init_wiki()
print(result)
```

### 搜索Wiki内容

```python
from skills.builtin.llm-wiki.scripts import search_wiki

results = search_wiki("关键词")
for r in results:
    print(f"{r['file']} — {r['matches']}处匹配")
```

### 健康检查

```python
from skills.builtin.llm-wiki.scripts import lint_wiki

report = lint_wiki()
print(report)
```

### 保存原始资料

```python
from skills.builtin.llm-wiki.scripts import save_source

result = save_source("资料内容", category="articles", filename="my-source.md")
print(result)
```

## Architecture: Three Layers

```
src/data/wiki/
├── SCHEMA.md           # Conventions, structure rules, domain config
├── index.md            # Sectioned content catalog with one-line summaries
├── log.md              # Chronological action log (append-only, rotated yearly)
├── raw/                # Layer 1: Immutable source material
│   ├── articles/       # Web articles, clippings
│   ├── papers/         # PDFs, arxiv papers
│   ├── transcripts/    # Meeting notes, interviews
│   └── assets/         # Images, diagrams referenced by sources
├── entities/           # Layer 2: Entity pages (people, orgs, products, models)
├── concepts/           # Layer 2: Concept/topic pages
├── comparisons/        # Layer 2: Side-by-side analyses
└── queries/            # Layer 2: Filed query results worth keeping
```

**Layer 1 — Raw Sources:** Immutable. The agent reads but never modifies these.
**Layer 2 — The Wiki:** Agent-owned markdown files. Created, updated, and cross-referenced by the agent.
**Layer 3 — The Schema:** `SCHEMA.md` defines structure, conventions, and tag taxonomy.

## 本项目可用工具说明

| 操作 | 可用工具 | 说明 |
|------|---------|------|
| 读取文件 | `read_file` | 读取任意文件内容 |
| 写入文件 | `write_file` | 创建或覆盖文件 |
| 追加文件 | `write_file(append=true)` | 追加内容到文件末尾 |
| 修改文件 | `patch_file` | 替换文件中的指定内容 |
| 运行命令 | `terminal` | 执行shell命令（创建目录、移动文件等） |
| 运行Python | `python_repl` | 执行Python代码（调用脚本） |
| 搜索网络 | `tavily_search` | 联网搜索信息 |
| 搜索记忆 | `_message_search_tool` | 搜索历史对话 |

## Resuming an Existing Wiki (CRITICAL — do this every session)

When the user has an existing wiki, **always orient yourself before doing anything**:

① **Read `SCHEMA.md`** — understand the domain, conventions, and tag taxonomy.
② **Read `index.md`** — learn what pages exist and their summaries.
③ **Scan recent `log.md`** — read the last 20-30 entries to understand recent activity.

```python
from skills.builtin.llm-wiki.scripts import get_wiki_path

wiki = get_wiki_path()
# 使用 read_file 工具读取
# read_file(str(wiki / "SCHEMA.md"))
# read_file(str(wiki / "index.md"))
# read_file(str(wiki / "log.md"), offset=<last 30 lines>)
```

Only after orientation should you ingest, query, or lint. This prevents:
- Creating duplicate pages for entities that already exist
- Missing cross-references to existing content
- Contradicting the schema's conventions
- Repeating work already logged

## Initializing a New Wiki

When the user asks to create or start a wiki:

1. 调用 `init_wiki()` 创建目录结构
2. Ask the user what domain the wiki covers — be specific
3. Write `SCHEMA.md` customized to the domain (see template below)
4. Write initial `index.md` with sectioned header
5. Write initial `log.md` with creation entry
6. Confirm the wiki is ready and suggest first sources to ingest

### SCHEMA.md Template

Adapt to the user's domain. The schema constrains agent behavior and ensures consistency:

```markdown
# Wiki Schema

## Domain
[What this wiki covers — e.g., "魔女岛回忆", "AI/ML research", "personal health"]

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `witch-island-history.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`
- **Provenance markers:** On pages that synthesize 3+ sources, append `^[raw/articles/source-file.md]`
  at the end of paragraphs whose claims come from a specific source.

## Frontmatter
  ```yaml
  ---
  title: Page Title
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  type: entity | concept | comparison | query | summary
  tags: [from taxonomy below]
  sources: [raw/articles/source-name.md]
  # Optional quality signals:
  confidence: high | medium | low
  contested: true
  contradictions: [other-page-slug]
  ---
  ```

### raw/ Frontmatter

```yaml
---
source_url: https://example.com/article
ingested: YYYY-MM-DD
sha256: <hex digest of the raw content below the frontmatter>
---
```

## Tag Taxonomy
[Define 10-20 top-level tags for the domain. Add new tags here BEFORE using them.]

Rule: every tag on a page must appear in this taxonomy. If a new tag is needed,
add it here first, then use it. This prevents tag sprawl.

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Add to existing page** when a source mentions something already covered
- **DON'T create a page** for passing mentions, minor details, or things outside the domain
- **Split a page** when it exceeds ~200 lines — break into sub-topics with cross-links
- **Archive a page** when its content is fully superseded — move to `_archive/`, remove from index

## Entity Pages
One page per notable entity. Include:
- Overview / what it is
- Key facts and dates
- Relationships to other entities ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions or debates
- Related concepts ([[wikilinks]])

## Comparison Pages
Side-by-side analyses. Include:
- What is being compared and why
- Dimensions of comparison (table format preferred)
- Verdict or synthesis
- Sources

## Update Policy
When new information conflicts with existing content:
1. Check the dates — newer sources generally supersede older ones
2. If genuinely contradictory, note both positions with dates and sources
3. Mark the contradiction in frontmatter: `contradictions: [page-name]`
4. Flag for user review in the lint report
```

### index.md Template

```markdown
# Wiki Index

> Content catalog. Every wiki page listed under its type with a one-line summary.
> Read this first to find relevant pages for any query.
> Last updated: YYYY-MM-DD | Total pages: N

## Entities
<!-- Alphabetical within section -->

## Concepts

## Comparisons

## Queries
```

### log.md Template

```markdown
# Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, update, query, lint, create, archive, delete
> When this file exceeds 500 entries, rotate: rename to log-YYYY.md, start fresh.

## [YYYY-MM-DD] create | Wiki initialized
- Domain: [domain]
- Structure created with SCHEMA.md, index.md, log.md
```

## Core Operations

### 1. Ingest

When the user provides a source (URL, file, paste), integrate it into the wiki:

① **Capture the raw source:**
   - URL → use `tavily_search` to get content, then `save_source()` to store
   - Pasted text → use `save_source()` to store
   - Name the file descriptively

② **Discuss takeaways** with the user.

③ **Check what already exists** — read `index.md` to find existing pages.

④ **Write or update wiki pages:**
   - **New entities/concepts:** Create pages only if they meet the Page Thresholds
   - **Existing pages:** Add new information, update facts, bump `updated` date
   - **Cross-reference:** Every page must link to at least 2 other pages via `[[wikilinks]]`
   - **Tags:** Only use tags from the taxonomy in SCHEMA.md

⑤ **Update navigation:**
   - Add new pages to `index.md`
   - Update "Total pages" count and "Last updated" date
   - Append to `log.md`

⑥ **Report what changed.**

### 2. Query

When the user asks a question about the wiki's domain:

① **Read `index.md`** to identify relevant pages.
② **Read the relevant pages** using `read_file`.
③ **Synthesize an answer** from the compiled knowledge. Cite the wiki pages.
④ **File valuable answers** — create a page in `queries/` or `comparisons/`.
⑤ **Update log.md.**

### 3. Lint

When the user asks to lint, health-check, or audit the wiki:

```python
from skills.builtin.llm-wiki.scripts import lint_wiki

report = lint_wiki()
# 检查 orphan_pages, broken_links, frontmatter_issues, large_pages 等
```

① **Orphan pages:** Pages with no inbound `[[wikilinks]]`.
② **Broken wikilinks:** `[[links]]` pointing to non-existent pages.
③ **Index completeness:** Compare filesystem against index entries.
④ **Frontmatter validation:** Check required fields.
⑤ **Stale content:** Pages with `updated` date >90 days old.
⑥ **Contradictions:** Pages with conflicting claims.
⑦ **Quality signals:** Pages with `confidence: low`.
⑧ **Page size:** Flag pages over 200 lines.
⑨ **Tag audit:** Flag tags not in SCHEMA.md taxonomy.
⑩ **Log rotation:** If log.md exceeds 500 entries, rotate it.
⑪ **Report findings** grouped by severity.
⑫ **Append to log.md.**

## Pitfalls

- **Never modify files in `raw/`** — sources are immutable. Corrections go in wiki pages.
- **Always orient first** — read SCHEMA + index + recent log before any operation.
- **Always update index.md and log.md** — skipping this makes the wiki degrade.
- **Don't create pages for passing mentions** — follow the Page Thresholds.
- **Don't create pages without cross-references** — every page needs 2+ `[[wikilinks]]`.
- **Frontmatter is required** — enables search, filtering, and staleness detection.
- **Tags must come from the taxonomy** — freeform tags decay into noise.
- **Keep pages scannable** — split pages over 200 lines.
- **Rotate the log** — when log.md exceeds 500 entries.
- **Handle contradictions explicitly** — don't silently overwrite.
