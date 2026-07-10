# XpGraph — Experience Graph for AI Agents

[**中文文档**](README.zh.md) | **English**

> **XpGraph** (Experience Graph) is the core knowledge engine of the AI Agent. It distills high-signal experiences from task execution into a structured knowledge graph, enabling cross-session knowledge reuse with minimal token overhead.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Data Flow](#data-flow)
- [Core Concepts](#core-concepts)
- [Knowledge Injection](#knowledge-injection)
- [Experience Distillation Pipeline](#experience-distillation-pipeline)
- [Multi-Role Knowledge Bases](#multi-role-knowledge-bases)
- [Data Model](#data-model)
- [Recall Mechanism](#recall-mechanism)
- [Graph Maintenance](#graph-maintenance)
- [Usage Examples](#usage-examples)
- [Tech Stack](#tech-stack)

---

## Overview

### Design Philosophy

XpGraph is a **distillation-first** knowledge graph system. Unlike traditional RAG that ingests raw conversation messages, XpGraph only stores **pre-distilled experience objects** — high-signal, reusable knowledge extracted via dedicated LLM calls.

XpGraph is a **pure infrastructure layer** with no business dependencies. Business-layer components such as the Distiller and Draft tool are owned by the `subagent` module and call into XpGraph's public API.

| Traditional RAG | XpGraph |
|----------------|---------|
| Ingest raw messages → extract later | Distill experiences → write directly |
| Low signal-to-noise ratio | High signal-to-noise ratio |
| Flat vector retrieval | Structured graph + multi-hop reasoning |
| Single knowledge base | Role-separated knowledge bases (strategy / operation) |
| Static knowledge base | Dynamic evolution, auto-merging, community detection |

### Core Capabilities

1. **Distillation-Based Extraction** — Two active layers: draft tool (Layer 1) + task-end distiller (Layer 3); pre-compaction fork (Layer 2) is planned
2. **Multi-Role Knowledge Bases** — Commander shares with main agent (strategy-level); Worker gets its own DB (operation-level)
3. **Knowledge Injection** — Recalled experiences injected as `AIMessage(content="徊...徊")` after the first HumanMessage
4. **Graph Community Detection** — Leiden algorithm auto-clusters related knowledge domains
5. **Personalized PageRank** — Dynamic node ranking based on query context
6. **Hybrid Retrieval** — Vector similarity + FTS5 full-text search + graph traversal

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       XpGraph Core                               │
├──────────────────┬──────────────┬───────────────────────────────┤
│    Recaller      │    Graph     │           Store               │
├──────────────────┼──────────────┼───────────────────────────────┤
│ • Dual-Path      │ • Community  │ • SQLite (per-role)           │
│ • PPR Rank       │ • PageRank   │ • FTS5                        │
│ • Reranker       │ • Dedup Merge│ • Vector                      │
└──────────────────┴──────────────┴───────────────────────────────┘
                      ↕ called by subagent (Distiller / Draft)
```

> **Note:** The Distiller (`agent/tools/subagent/distiller.py`) and Draft tool (`agent/tools/subagent/draft.py`) were previously part of XpGraph but have been moved to the **subagent** business layer. XpGraph is now a pure infrastructure module with no business logic. The Distiller calls XpGraph's public API (`get_instance`, `ingest_experiences`, etc.) to write distilled experiences.

### Module Responsibilities

| Module | File Path | Core Function |
|--------|-----------|---------------|
| **Extractor** | `extractor/core.py` | Node/edge extraction from pre-distilled input; session-end finalizer |
| **Recaller** | `recaller/core.py` | Dual-path recall (precise + generalized); merge results |
| **Graph** | `graph/*.py` | Community detection, PageRank, dedup, maintenance |
| **Store** | `store/core.py` | SQLite CRUD, vector storage, FTS5 search |
| **Core** | `core.py` | `XpGraphInstance` factory; orchestrate modules |

**Business-layer modules (not part of XpGraph):**

| Module | File Path | Core Function |
|--------|-----------|---------------|
| **Distiller** | `agent/tools/subagent/distiller.py` | Task-end distillation of strategy/operation experiences; edge ingestion via `_ingest_edges()` |
| **Draft** | `agent/tools/subagent/draft.py` | Record key findings during subagent task execution |

---

## Data Flow

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                     Commander Execution                         │
 │                                                                 │
 │  1. Knowledge Injection                                         │
 │     task description → assemble() → AIMessage<徊...徊>          │
 │                                                                 │
 │  2. During Execution                                            │
 │     Agent calls draft tool → insights saved to state_register  │
 │                                                                 │
 │  3. Worker Execution                                            │
 │     Worker drafts merged to Commander session on completion     │
 └────────────────────────┬────────────────────────────────────────┘
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
 ┌─────────────────┐           ┌─────────────────────┐
 │  Worker Task A   │           │   Worker Task B      │
 │                  │           │                      │
 │  Same 2 steps:  │           │  Same 2 steps:      │
 │  injection →    │           │  injection →        │
 │  draft          │           │  draft              │
 └────────┬────────┘           └──────────┬──────────┘
          │                               │
          └───────────────┬───────────────┘
                          ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │                    Task-End Distillation                        │
 │                    (owned by subagent module)                   │
 │                                                                 │
 │  1. Merge worker drafts into Commander session                  │
 │  2. Gather: task description + result + all drafts              │
 │  3. Distill strategy-level experiences → default DB (commander) │
 │  4. Distill operation-level experiences → worker DB             │
 │  5. Ingest both nodes and edges into respective knowledge graphs│
 └─────────────────────────────────────────────────────────────────┘
```

---

## Core Concepts

### Experience Nodes

| Type | Description | Naming Convention | Example |
|------|-------------|-------------------|---------|
| **TASK** | A task or topic the user requested | `verb-object` | `deploy-bilibili-mcp` |
| **SKILL** | Reusable strategy or operation | `tool-action` | `conda-env-create` |
| **EVENT** | One-time error or pitfall | `phenomenon-tool` | `importerror-libgl1` |

### Experience Edges

| Type | Direction Constraint | Meaning | `instruction` Content |
|------|---------------------|---------|----------------------|
| **USED_SKILL** | TASK → SKILL | Task used this skill | Which step, how it was called |
| **SOLVED_BY** | EVENT → SKILL | Error resolved by this skill | Exact command/operation executed |
| **REQUIRES** | SKILL → SKILL | Prerequisite dependency | Why depended, how to determine |
| **PATCHES** | SKILL → SKILL | New skill corrects an old one | Old solution's issue, new fix |
| **CONFLICTS_WITH** | SKILL ↔ SKILL | Mutual exclusion | Conflict symptom, which to choose |

---

## Knowledge Injection

When a Commander or Worker starts a task, relevant experiences are recalled and injected into the message stream as an `AIMessage` with `徊...徊` markers, placed immediately after the first `HumanMessage`.

**Commander (strategy-level, from default DB):**

```python
messages = [
    HumanMessage(content="Deploy a Python web app to Kubernetes"),
    AIMessage(content="徊\n<xp_graph>...strategy knowledge XML...</xp_graph>\n徊")
]
```

**Worker (operation-level, from worker DB):**

```python
messages = [
    HumanMessage(content="Install Python dependencies in a conda environment"),
    AIMessage(content="徊\n<xp_graph>...operation knowledge XML...</xp_graph>\n徊")
]
```

This ensures the agent has access to relevant past experiences without polluting the system prompt or consuming context window space for irrelevant knowledge.

---

## Experience Distillation Pipeline

XpGraph uses a **three-layer distillation** design, with two layers currently active:

### Layer 1: Draft Tool (Active)

The `draft` tool is available to all agents (owned by the `subagent` module). When an agent discovers something worth remembering, it calls:

```python
draft(key_points="The config file must be loaded before init() or it silently defaults to empty",
      category="insight")
```

Drafts are stored in `state_register` and capped at 10 per session. When a Worker task completes, its drafts are **merged into the Commander session** so that all drafts are available for unified distillation.

### Layer 2: Pre-Compaction Fork (Planned — Not Yet Implemented)

When `SummarizationMiddleware` triggers message compression, the middleware would intercept the messages about to be discarded, send them to `auxiliary_llm` with a distillation prompt, and save the extracted insights as additional drafts.

- Commander: **strategy-level** distillation prompt (task decomposition, parallel patterns, dependency pitfalls)
- Worker: **operation-level** distillation prompt (tool usage patterns, API gotchas, error workarounds)

> This layer is currently deferred. The active pipeline relies on Layer 1 (drafts) and Layer 3 (task-end distiller).

### Layer 3: Task-End Distiller (Active)

After the subagent task completes, `distill_and_ingest()` is triggered in the `finally` block (owned by the `subagent` module):

1. Worker drafts are merged into the Commander session
2. Gathers the original task, final result, and all accumulated drafts
3. Calls `auxiliary_llm.with_structured_output(DistillResult)` with role-specific prompts
4. Produces structured `DistillNode` and `DistillEdge` objects
5. Writes strategy-level experiences (nodes + edges) to the default DB, operation-level (nodes + edges) to the worker DB
6. Edge ingestion uses the `_ingest_edges()` helper to resolve node names and create relationships

---

## Multi-Role Knowledge Bases

XpGraph maintains separate SQLite databases for different roles:

| Role | DB Path | Knowledge Level | Shared With |
|------|---------|----------------|-------------|
| `default` | `store/xp_graph/xp_graph.db` | Strategy (task decomposition, scheduling, parallelism) | Main agent + Commander |
| `worker` | `store/xp_graph/worker/xp_graph.db` | Operation (tool usage, API patterns, error fixes) | Workers only |

### XpGraphInstance Factory

```python
from agent.tools.xp_graph import get_instance

commander_memory = get_instance("default")
worker_memory = get_instance("worker")

# Each instance has its own db, recaller, extractor, config
await commander_memory.ingest_experiences(session_id, experiences)
await worker_memory.assemble(task_description)
```

---

## Data Model

### Node (gm_nodes)

```python
class GmNode(BaseModel):
    id: str                      # "n-{timestamp}-{random}"
    type: Literal["TASK", "SKILL", "EVENT"]
    name: str                    # Normalized (lowercase, hyphenated)
    description: str             # One-line summary
    content: str                 # Detailed reusable knowledge
    validated_count: int = 1     # Accumulated on repeat occurrence
    source_sessions: List[str]   # Session IDs where this appeared
    community_id: Optional[str]  # Community cluster ID
    pagerank: float = 0          # Global PageRank score
    created_at: int
    updated_at: int
```

### Edge (gm_edges)

```python
class GmEdge(BaseModel):
    id: str                      # "e-{timestamp}-{random}"
    from_id: str
    to_id: str
    type: str                    # USED_SKILL / SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH
    instruction: str             # How/when to use this relationship
    condition: Optional[str]     # Trigger condition (required for SOLVED_BY)
    session_id: str
    created_at: int
```

### Config (GmConfig)

```python
class GmConfig(BaseModel):
    db_path: str = "xp_graph.db"
    compact_turn_count: int = 7
    recall_max_nodes: int = 6
    recall_max_depth: int = 2
    fresh_tail_count: int = 10
    dedup_threshold: float = 0.90
    pagerank_damping: float = 0.85
    pagerank_iterations: int = 20
    embedding: Embeddings
    llm: BaseChatModel
```

---

## Recall Mechanism

### Dual-Path Recall

```
User Query
  ├─ Precise Path
  │   ├─ Vector search / FTS5 → seed nodes
  │   ├─ Community expansion
  │   ├─ Graph traversal (BFS max_depth=2)
  │   └─ PPR ranking
  │
  └─ Generalized Path
      ├─ Community vector search → matching communities
      ├─ Fetch community representative nodes
      ├─ Graph traversal (BFS max_depth=1)
      └─ PPR ranking

  ↓ Merge & deduplicate
Final result (nodes + edges) → formatted as XML context
```

### Experience → Skill Auto-Promotion

When an EVENT node's `validated_count` reaches the threshold (default 3), the session-end finalizer evaluates whether to promote it to a SKILL node. This happens automatically during `rectification_and_standardization()`.

---

## Graph Maintenance

### Periodic Maintenance

Triggered every N turns (configurable via `compact_turn_count`):

1. Community detection (Leiden algorithm)
2. Community summary generation (LLM + embedding)
3. Cache invalidation

### Session-End Maintenance

Triggered by `rectification_and_standardization()`:

1. Final review (EVENT → SKILL promotion, missing edges, obsolete node marking)
2. Global PageRank update
3. Node deduplication and merging

---

## Usage Examples

### Get a Knowledge Instance

```python
from agent.tools.xp_graph import get_instance

# Commander/main agent (strategy-level)
memory = get_instance("default")

# Worker (operation-level)
memory = get_instance("worker")
```

### Recall and Inject Knowledge

```python
result = await memory.assemble(
    user_text="How to deploy an app with Docker?",
    messages=conversation_history
)

if "system_prompt_addition" in result:
    knowledge_xml = result["system_prompt_addition"]
    # Inject as AIMessage with 徊...徊 markers after first HumanMessage
```

### Ingest Pre-Distilled Experiences (from Subagent Distiller)

```python
from agent.tools.subagent.distiller import distill_and_ingest

await distill_and_ingest(
    task="Deploy a Python app to Kubernetes",
    result="Successfully deployed using helm chart",
    session_id="session_001",
    commander_session_id="commander-session_001",
)
```

### Record a Draft Insight

```python
# Called by the agent as a tool during execution (owned by subagent module)
draft(key_points="Docker build cache must be invalidated when requirements.txt changes",
      category="insight")
```

### Query Statistics

```python
from agent.tools.xp_graph import get_db, all_active_nodes, all_edges

db = get_db()  # default role
nodes = all_active_nodes(db)
edges = all_edges(db)

for node in nodes:
    print(f"[{node.type}] {node.name}: {node.description}")
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Database** | SQLite 3 + FTS5 (per-role) |
| **Vector Storage** | SQLite BLOB field |
| **Graph Algorithm** | igraph + Leiden Algorithm |
| **PageRank** | Custom implementation (Python) |
| **Embedding Model** | BGE/BAAI series |
| **LLM** | auxiliary_llm (distillation), main_llm (agent) |
| **Async Framework** | asyncio |
