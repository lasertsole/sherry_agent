# GraphRAG Path Query

The `multimodal_rag` skill's LightRAG engine retrieves entities and relations by
vector similarity, then leaves the multi-hop join to the LLM. This page records
the explicit path layer added on top of that recall, the retrieval-threshold
tuning that shipped with it, and the sparse-recall conclusion that closed the
question (with the measured before/after numbers).

## The gap

`vendored_lightrag` had no path algorithm: `query_nodes_by_vector` and the
relation recall each run one vector search (`top_k=40` by default) and return.
A bridge entity outside the top-k makes a relationship question unanswerable by
traversal even when the graph contains the chain, so "multi-hop" was a
probabilistic outcome of the LLM reasoning over a candidate set.

The entity vectors are dense-only (`BGE-M3`, 1024 dimensions). There is no
sparse channel and no BM25 fallback in the vendored engine.

## The path layer

`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py` is
skill-owned (the vendored tree only gains a dispatch hook in `operate.py`):

- **Endpoint resolution** — three passes in cost order: exact graph node name →
  graph-label containment (`Kepler-7 launch pad` → `Kepler-7 Launch Pad`) →
  entity-vector recall keyed by `compute_mdhash_id(..., prefix="ent-")`.
- **BFS** — all simple paths between the resolved endpoints up to
  `max_depth=3` hops, ordered by `(hops, -average edge weight)` and capped at
  `top_k_paths=10`. A path never repeats a node; a global `max_visit_nodes`
  budget and a per-layer `max_frontier` cap bound the work on dense graphs.
- **Beam fallback** — only when BFS finds nothing: a bidirectional,
  embedding-guided beam (`beam_width=5`, `beam_max_hops=5`) scores each
  neighbour by cosine similarity to the opposite endpoint plus
  `0.2 × edge weight`, and stops at the first meeting node. Its vectors come
  from `SNKVVectorStorage.get_vectors_by_ids`.
- **Formatting** — the LLM prompt receives the explicit chain plus each node's
  and edge's description and the source chunks of the traversed edges; the
  structured payload (`entities` / `relationships` / `chunks` / `references`)
  merges into the normal recall data with a `path_info` metadata block.

Two entry points use it:

- `mode="path"` runs the search exclusively and falls back to `hybrid` recall
  when no path exists, so the mode never answers with an empty context.
- `mix` / `hybrid` questions that read as relationship-shaped (English,
  Chinese, Japanese and Korean patterns; `is_relationship_query`) automatically
  merge a path section into their context.

Every number lives in `config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH`.

## Retrieval thresholds

The LightRAG defaults admitted near-unrelated candidates: `TOP_K=40`,
`CHUNK_TOP_K=20`, `COSINE_THRESHOLD=0.2`, and `MIN_RERANK_SCORE` off. The tuned
defaults are `TOP_K=20`, `CHUNK_TOP_K=12`, `COSINE_THRESHOLD=0.4` and
`MIN_RERANK_SCORE=0.5`, plus the graph size caps
`MAX_SOURCE_IDS_PER_ENTITY=50`, `MAX_SOURCE_IDS_PER_RELATION=50`,
`SOURCE_IDS_LIMIT_METHOD=FIFO` and `RELATED_CHUNK_NUMBER=5`.

They are installed in `graph_rag/__init__.py`, **before** the vendored package
is imported: `QueryParam` and the LightRAG dataclass read environment variables
into class-level defaults at import time, so the old placement in `base.py`
(after its own LightRAG import) silently never took effect.

## Sparse recall: why it is not implemented

The sparse half of the plan is blocked by the embedding layer.
`models/embed_model/text/core.py::CustomEmbedding` produces one dense vector —
locally through llama.cpp `create_embedding`, remotely through an
OpenAI-compatible `/embeddings` endpoint. `SNKVVectorStorage` keeps a single
HNSW cosine index, and `EmbeddingFunc` returns one `ndarray`. BGE-M3's native
sparse output would require `FlagEmbedding` (PyTorch plus several hundred MiB)
or a sparse-capable endpoint; both are rejected for the desktop bundle. Rare
ID/version-string recall therefore remains open, with SQLite FTS5/BM25 as the
documented future option rather than a dual sparse index.

## Measured effect

`evals/graph_rag/multihop_bench.py` indexes a 20-document corpus with
look-alike distractors (102 nodes / 106 edges) and scores six questions:
four relationship questions whose shortest chain is 2-5 hops, plus two
single-hop controls. Both runs query the SAME index.

| Metric (multi-hop questions) | Before, `mix` | After, `mix` | After, `mode="path"` |
| :--------------------------- | :------------ | :----------- | :------------------- |
| Context bridge recall        | 1.000         | 1.000        | 1.000                |
| Answer bridge recall         | 0.875         | 1.000        | 1.000                |
| Answers with all bridges     | 0.750         | 1.000        | 1.000                |
| Total context characters     | 155,720       | 87,351       | 23,834               |
| Single-hop control accuracy  | 1.000         | 1.000        | 1.000                |

Context size falls by `44%` for `mix` and `85%` for `mode="path"` while the
answer bridge recall rises from `0.875` to `1.000`. Path latency on the same
graph is `2-13 ms` per query (BFS `2-5 ms`, beam `11-13 ms`), far inside the
`500 ms` BFS and `2 s` beam targets. The beam found the 4- and 5-hop chains the
3-hop BFS could not, which is why it ships enabled as a fallback.

The single-hop RAGAS suite (`evals/results/graph_rag/20261019_233415`) kept
faithfulness, context recall and context precision at `1.000`; its answer
relevancy read `0.916`, against `0.963` and `0.934` in two pre-change runs —
inside the run-to-run spread, with the correctness metrics unchanged.

Verdict: better than the pre-change pipeline, so both GraphRAG plan documents
were retired.

## Tests

```bash
uv run --no-sync pytest tests/skills/builtin/core/multimodal_rag/test_path_search.py \
  tests/skills/builtin/core/multimodal_rag/test_path_query_integration.py -q
uv run --no-sync python evals/graph_rag/multihop_bench.py --modes mix,path
uv run --group evals python evals/evals.py graph_rag
```

The unit file pins the hop guarantee, cycle safety, the path cap, label and
vector endpoint resolution and the beam fallback; the integration file runs
`mode="path"` and the `mix` auto-merge against real SNKV storages without an
LLM call. The RAGAS suite remains the single-hop answer-quality regression gate.
