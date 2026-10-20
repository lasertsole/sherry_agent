# 🧠 LLM Wiki Graph Layer

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The design companion to the `llm_wiki` skill: how the explicit graph, the
> multimodal ingest path, and the graph-algorithm queries fit beside the
> markdown knowledge base without changing it.

Source of truth: `skills/builtin/llm_wiki/scripts/{core,ingest,search,graph,graph_query}.py`,
`skills/builtin/llm_wiki/SKILL.md`, and
`config/features/infra_side/skills_tooling.py`.

## Markdown stays the source of truth

The wiki remains a directory of interlinked markdown files. The graph is
DERIVED: `scripts/graph.py` writes `src/data/wiki/.wiki_graph.db` (disposable,
rebuilt from markdown) with three tables — `wiki_nodes`, `wiki_edges`,
`wiki_graph_meta`. `save_source`, `search_wiki`, `init_wiki` and `lint_wiki`
keep their signatures (L1); the graph is an additive layer.

| Edge source | `edge_type` | Confidence |
|-------------|-------------|------------|
| `[[Wikilink]]` | `wikilink` | `EXTRACTED` |
| `[[Target|INFERRED]]` | `wikilink` | `INFERRED` |
| `[inline link](...)` | `inline_link` | `EXTRACTED` |
| `` `code span` `` mention | `mention` | `EXTRACTED` |
| frontmatter `extracted_relations` | the relation's `type` | the relation's `confidence` |

`build_wiki_graph(force=False)` is incremental by file `mtime`: unchanged files
are skipped, a changed file's edges are replaced, and deleting a file removes
its node and every edge that touches it. `|EXTRACTED` is accepted explicitly;
the suffix is optional (a plain wikilink is EXTRACTED). Optional
`extracted_entities` adds nodes; `extracted_relations` adds typed edges.

## Multimodal ingest

`scripts/ingest.py::save_binary_source(content_bytes, content_type, ...)` is
the additive binary path beside `save_source` (text only, unchanged):

| `content_type` | Extractor | `raw/` category |
|----------------|-----------|-----------------|
| `pdf` | pypdf → pdftotext → mineru | `papers` |
| `image` / `png` / `jpg` | `image_to_text` (`itt`) | `assets` |
| `audio` / `mp3` / `wav` | `speech_to_text` (`stt`) | `transcripts` |
| `video` / `mp4` / `mov` | `video_text_to_text` (`vtt`) | `transcripts` |
| `docx` | stdlib ZIP + `word/document.xml` | `articles` |
| `xlsx` | stdlib ZIP + sharedStrings/sheet XML | `articles` |
| `md` / `txt` | decode | `articles` |

One auxiliary-LLM semantic pass extracts entities/relations (fail-open — a
provider failure leaves empty lists), the markdown source is written with the
additive frontmatter, and the graph is refreshed. Missing `pypdf` /
`python-docx` / `openpyxl` never fails the call: the stdlib readers and the
pdftotext/mineru chain degrade instead (L5).

## Graph queries

`scripts/graph_query.py` exposes the read side:

| Function | What it returns |
|----------|-----------------|
| `path(a, b, max_depth=5)` | shortest undirected path + each hop's edge type/confidence |
| `god_nodes(limit=10)` | degree-ranked nodes (pure SQL, no optional dependency) |
| `explain(name)` | neighbours with direction, edge type, confidence and source file |
| `communities()` | networkx Louvain clusters; connected components without networkx |
| `query_graph(question)` | ASCII words + CJK bigrams → node recall → 1-hop subgraph |

`search_wiki(keyword)` is still the grep path and is recommended for literal
strings; `query_graph` is recommended when the question is about relationships.
Tokenization is dependency-free (no jieba); the wiki graph is intentionally not
merged with code_intel or graph_rag — each graph is optimized for its own
storage and query language.

## Lint graph metrics

`lint_wiki()` keeps its original report and appends one `graph` block:
connectivity (`components`, `largest_component`, `isolated_nodes`), the
`EXTRACTED` / `INFERRED` split, `low_confidence_pages` (a page whose edges are
at least `wiki_lint_low_confidence_ratio` inferred), and the `god_nodes` top 10.
The graph section is fail-open: a failed build reports `available: false`
instead of breaking the original ten checks (L7).

## Tests

| Area | Tests |
|------|-------|
| Graph construction, confidence tags, incremental rebuild | `tests/skills/builtin/llm_wiki/test_graph_build.py` |
| Path, god nodes, explain, communities, NL query | `tests/skills/builtin/llm_wiki/test_graph_query.py` |
| Binary ingest + office readers | `tests/skills/builtin/llm_wiki/test_binary_ingest.py` |
| Lint graph metrics | `tests/skills/builtin/llm_wiki/test_lint_graph.py` |
