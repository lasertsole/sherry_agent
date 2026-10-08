---
name: llm-wiki
description: "Karpathy's LLM Wiki: build/query interlinked markdown KB."
version: 3.0.0
author: EMA AI Agent
license: MIT
metadata:
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

## Wiki Directory Structure

All paths are computed dynamically by the scripts, based on `src/data/wiki/` under the project root.

```json
{
  "wiki": {
    "root": ["SCHEMA.md", "index.md", "log.md"],
    "raw": {
      "description": "Raw sources (read-only, never modified)",
      "articles": "Web articles / online sources",
      "papers": "Papers / research reports",
      "transcripts": "Transcripts / interviews",
      "assets": "Images / attachment resources"
    },
    "entities": "Entity profiles (people/orgs)",
    "concepts": "Concept/topic analyses",
    "comparisons": "Side-by-side comparisons",
    "queries": "Filed query results"
  }
}
```

## Quick Start

### Inspect the Wiki path and structure

```python
from skills.builtin.llm_wiki.scripts import get_wiki_path, print_structure

print(f"Wiki path: {get_wiki_path()}")
print(f"Directory structure: {print_structure()}")
```

### Initialize the Wiki

```python
from skills.builtin.llm_wiki.scripts import init_wiki

result = init_wiki()
print(result)
```

### Search the Wiki

```python
from skills.builtin.llm_wiki.scripts import search_wiki

results = search_wiki("keyword")
for r in results:
    print(f"{r['file']} — {r['matches']} matches")
```

### Health Check

```python
from skills.builtin.llm_wiki.scripts import lint_wiki

report = lint_wiki()
print(report)
```

### Save a Raw Source

```python
from skills.builtin.llm_wiki.scripts import save_source

result = save_source("source content", category="articles", filename="my-source.md")
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

## Available Tools in This Project

| Operation | Available tool | Notes |
|-----------|----------------|-------|
| Read a file | `read_file` | Read any file's contents |
| Write a file | `write_file` | Create or overwrite a file |
| Append to a file | `write_file(append=true)` | Append content to the end of a file |
| Modify a file | `patch_file` | Replace specified content in a file |
| Run commands | `terminal` | Execute shell commands (create dirs, move files, ...) |
| Run Python | `python_repl` | Execute Python code (call the scripts) |
| Web search | `tavily_search` | Search the web |
| Search memory | `_message_search_tool` | Search past conversation history |

## Resuming an Existing Wiki (CRITICAL — do this every session)

When the user has an existing wiki, **always orient yourself before doing anything**:

① **Read `SCHEMA.md`** — understand the domain, conventions, and tag taxonomy.
② **Read `index.md`** — learn what pages exist and their summaries.
③ **Scan recent `log.md`** — read the last 20-30 entries to understand recent activity.

```python
from skills.builtin.llm_wiki.scripts import get_wiki_path

wiki = get_wiki_path()
# Read using the read_file tool
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

1. Call `init_wiki()` to create the directory structure
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
[What this wiki covers — e.g., "witch-island-history", "AI/ML research", "personal health"]

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
from skills.builtin.llm_wiki.scripts import lint_wiki

report = lint_wiki()
# Check orphan_pages, broken_links, frontmatter_issues, large_pages, etc.
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
