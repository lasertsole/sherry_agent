---
name: code-wiki
description: "Generate wiki docs + Mermaid diagrams for any codebase. Use when the user asks to document a codebase, generate architecture diagrams, or create a structured wiki for a repo."
---

# Code Wiki Skill

Generate a comprehensive wiki for any codebase — overview, architecture, per-module deep-dives, Mermaid class and sequence diagrams. Inspired by Google CodeWiki, but works on local repos, private repos, and any language. Uses only standard tools (shell, file reading/writing, search); no Docker, no external services, no extra dependencies.

This skill produces **reference documentation** (what/how). It does not produce strategic narrative (why — that's a different skill).

## When to Use

- User says "document this codebase", "generate a wiki", "make architecture diagrams"
- Onboarding to an unfamiliar repo and wants a structured reference
- User points at a GitHub URL and asks for documentation
- Need a stable artifact (markdown + Mermaid) that renders on GitHub

Do NOT use this for:
- Single-file or single-function documentation — just answer directly
- API reference for one specific endpoint — answer inline
- Strategic "why does this exist" narrative — different purpose
- Codebases the user is actively developing in this session — just answer questions as they come

## Quick Reference

| Step | Action |
|---|---|
| 1 | Resolve target — local cwd, given path, or `git clone --depth 50 <url>` to a temp dir |
| 2 | Scan structure — `ls`, `find -maxdepth 3`, manifest files, README |
| 3 | Pick 8–10 modules to document |
| 4 | Write `README.md` (overview + module map) |
| 5 | Write `architecture.md` with Mermaid flowchart |
| 6 | Write per-module docs in `modules/` |
| 7 | Write `diagrams/class-diagram.md` (Mermaid classDiagram) |
| 8 | Write `diagrams/sequences.md` (Mermaid sequenceDiagram, 2–4 workflows) |
| 9 | Write `getting-started.md` |
| 10 | Write `api.md` if applicable, else skip |
| 11 | Write `.codewiki-state.json` |
| 12 | Report paths to user |

## Procedure

### 1. Resolve the target

For a GitHub URL:

```bash
WIKI_TMP=$(mktemp -d)
git clone --depth 50 <url> "$WIKI_TMP/repo"
cd "$WIKI_TMP/repo"
REPO_SHA=$(git rev-parse HEAD)
REPO_NAME=$(basename <url> .git)
```

For a local path (or cwd if none given):

```bash
cd <path>
REPO_SHA=$(git rev-parse HEAD 2>/dev/null || echo "uncommitted")
REPO_NAME=$(basename "$PWD")
```

Then set the output dir:

```bash
OUTPUT_DIR="$HOME/.hermes/wikis/$REPO_NAME"
mkdir -p "$OUTPUT_DIR/modules" "$OUTPUT_DIR/diagrams"
```

### 2. Scan repo structure

```bash
# Shallow tree first
ls -la

# Deeper tree, noise filtered
find . -type d \
  -not -path '*/\.*' \
  -not -path '*/node_modules*' \
  -not -path '*/venv*' \
  -not -path '*/__pycache__*' \
  -not -path '*/dist*' \
  -not -path '*/build*' \
  -not -path '*/target*' \
  -maxdepth 3 | sort
```

Then read the relevant manifests (`package.json`, `pyproject.toml`, `setup.py`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`) and the project README.

### 3. Pick modules to document

Cap initial pass at **8–10 modules**. Heuristics by language:

- Python: top-level packages (dirs with `__init__.py`), plus subsystem dirs
- JS/TS: `src/<subdir>`, top-level workspace dirs
- Rust: each crate in a workspace, or top-level `src/<module>` dirs
- Go: each top-level package directory
- Mixed/unfamiliar: top-level directories that contain source code (not config, not tests)

Prioritize by: imported-from count, LOC, mentions in README / top-level docs.

State the module list to the user before generating per-module docs on big repos.

### 4. Write `README.md`

Read the actual project README plus the top 2–3 entry-point files. Use the `templates/README.md` template.

For link targets in local mode use relative paths. For cloned repos use `https://github.com/<owner>/<repo>/blob/<sha>/<path>` so links survive future commits.

### 5. Write `architecture.md`

Use the `templates/architecture.md` template.

**Mermaid shape semantics:**
- `[]` = component
- `[()]` = database / storage
- `{{}}` = external service
- `(())` = entry point or terminal
- `-->` = sync call, `-.->` = async/event

Cap at ~20 nodes per diagram. Split into sub-diagrams if larger.

### 6. Write per-module docs in `modules/`

For each selected module, inspect its layout with `ls`, identify 3–5 most important files, then read those files (use offset/limit to read only what you need; prefer search for specific symbols).

Use the `templates/module.md` template.

### 7. Write `diagrams/class-diagram.md`

Pick the 5–10 most important classes/types, read them, then write a Mermaid `classDiagram`.

For languages without classes (Go, C, Rust): use the diagram for struct relationships, or skip and explain in prose in architecture.md. Don't force-fit.

### 8. Write `diagrams/sequences.md`

Pick 2–4 of the most important workflows. Trace each call path through the code, then write a Mermaid `sequenceDiagram`.

Don't invent participants. Every box must correspond to a real component the reader can find in the code.

### 9. Write `getting-started.md`

Use the `templates/getting-started.md` template. Fill from manifest files + README.

### 10. Write `api.md` (skip if not applicable)

Only write this if the project is a library or API server. Document each public entry with signature, parameters, return type, one-line description. Group by category.

### 11. Write the state file

```json
{
  "repo_name": "<REPO_NAME>",
  "source_path": "<PWD>",
  "source_sha": "<REPO_SHA>",
  "generated_at": "<ISO timestamp>",
  "generator": "code-wiki skill v0.1.0",
  "modules_documented": []
}
```

### 12. Report to user

State exactly what was generated and where:

```
Generated wiki at ~/.hermes/wikis/<repo-name>/:
  README.md                   project overview, module map
  architecture.md             system architecture + flowchart
  getting-started.md          setup, first run, workflows
  modules/<N files>           per-module deep-dives
  diagrams/class-diagram.md   Mermaid class diagram
  diagrams/sequences.md       Mermaid sequence diagrams
```

## Scope Control

- Initial scan: max depth 3 directories
- Per-module docs: cap at 10 modules unless user expands scope
- Per-file reads: prefer search for symbols + offset/limit over full reads
- Skip vendored code (`vendor/`, `third_party/`, generated code, `_pb2.py`, `.min.js`)

## Re-Run / Update

If `.codewiki-state.json` already exists at the target path:

- Read it for previous SHA and module list
- If source SHA matches: ask user if they want to regenerate or skip
- If SHA differs: offer to regenerate only modules with changed files (`git diff --name-only <old-sha> HEAD`)

## Pitfalls

- **Fabricating components.** Every diagram node and claimed function call must be in the source. Read before writing.
- **Generic AI prose.** "This module is responsible for..." is content-free. Say what it actually does in domain-specific terms.
- **Restating code as prose.** A doc that paraphrases function signatures is worse than just linking to the function.
- **Mermaid > 50 nodes** don't render legibly. Split them.
- **Documenting tests, generated code, or vendored deps** as if they were product code. Skip them.
- **In-repo output without asking.** Default is `~/.hermes/wikis/`. Only write into the repo when explicitly requested.
- **Mermaid special chars need quotes:** `A["Tool / Agent"]` not `A[Tool / Agent]`. `<br>` for line breaks inside a node.
- **Nested code fences.** When writing markdown that contains a Mermaid block, use 4-backtick outer fences.
- **classDiagram generics** render as `~T~` (e.g. `List~Tool~`), not `<T>`.
- **GitHub Mermaid theme is fixed** — don't include `%%{init: ...}%%` blocks.

## Verification

After writing, verify:

1. **Mermaid blocks balance** — opens equal closes per file
2. **All expected files exist** — README.md, architecture.md, getting-started.md, .codewiki-state.json, modules/, diagrams/
3. **Module count matches** what was committed to in Step 3
4. **No fabricated paths** — sanity-check 2–3 source links resolve to real files
