# Skill Memory — Intelligent Knowledge Graph Memory System

[**中文文档**](README.zh.md) | **English**

> **Skill Memory** is the core memory engine of the EMA AI Agent. It automatically extracts, organizes, and retrieves structured knowledge from conversations, building a dynamically evolving skill knowledge graph.

---

## Table of Contents

- [Overview](#overview)
- [Comparison with Hermes](#comparison-with-hermes)
- [Architecture](#architecture)
- [Workflow](#workflow)
- [Core Mechanisms](#core-mechanisms)
- [Key Components](#key-components)
- [Data Model](#data-model)
- [Recall Mechanism](#recall-mechanism)
- [Graph Maintenance](#graph-maintenance)
- [Usage Examples](#usage-examples)
- [Performance Optimization](#performance-optimization)
- [FAQ](#faq)
- [Tech Stack](#tech-stack)
- [License](#license)

---

## Overview

### Design Philosophy

Skill Memory is a **graph-based knowledge memory system** designed to overcome the limitations of traditional RAG systems:

| Traditional RAG | Skill Memory |
|----------------|-------------|
| Flat vector retrieval | Structured graph + multi-hop reasoning |
| Isolated knowledge chunks | Semantic relationship network between nodes |
| Static knowledge base | Dynamic evolution, self-updating |
| Single recall path | Dual-path parallel recall (precise + generalized) |

### Core Capabilities

1. **Automatic Knowledge Extraction** — Identify TASK/SKILL/EVENT triplets from conversations
2. **Graph Community Detection** — Automatically cluster related knowledge domains using the Leiden algorithm
3. **Personalized PageRank** — Dynamic node ranking based on query context
4. **Hybrid Retrieval** — Vector similarity + FTS5 full-text search + graph traversal
5. **Asynchronous Background Processing** — Incremental updates without blocking the main conversation flow

---

## Comparison with Hermes

Skill Memory and Hermes (Function Calling approach) both aim to teach AI to reuse experience, but their design philosophies are fundamentally different.

### Core Differences

| Dimension | Hermes (Traditional) | Skill Memory (This System) |
|-----------|---------------------|---------------------------|
| **Storage** | Skill text generated per conversation, carried in context | Structured graph nodes + edges, on-demand recall |
| **Retrieval** | None — everything carried in context | Dual-path recall (vector + graph + community) |
| **Deduplication** | None — same skill generated repeatedly | Graph auto-merges similar nodes |
| **Token Cost** | Linear growth with conversations, O(n) | Only recalls relevant nodes, O(k), k ≪ n |
| **Routing** | None — skills in context but no invocation mapping | Routing table (edge `instruction` field) stores invocation methods |
| **Knowledge Evolution** | None — historical skills don't update | validated_count accumulation → auto-promote to SKILL |
| **Long-tail Management** | Context explosion | Graph refinement (community detection, dedup, merging) |

### Token Comparison: Real-World Scenario

Assuming 100 conversation turns, each producing 2 skill nodes:

| Metric | Hermes | Skill Memory |
|--------|--------|-------------|
| Total skills | 200 (all in context) | 200 (in graph, only 6-10 recalled) |
| Context Token | ~50K+ | ~1.5K-3K |
| Redundancy | High (same skill regenerated) | Low (auto-merged) |
| Knowledge Forgetting Risk | High (context window overflow) | Low (persistent storage) |

### Core Advantages

Skill Memory is not a "better Hermes" but a completely different paradigm:

1. **Graph as Routing** — Relationships between nodes (USED_SKILL/SOLVED_BY/PATCHES) naturally form a routing table; the LLM knows how to invoke through the `instruction` field
2. **Experience→Skill Auto-Promotion** — EVENT nodes matched 3+ times are automatically promoted to SKILL without manual intervention
3. **Graph Refinement** — Community detection automatically clusters related domain nodes; PageRank ranking surfaces high-frequency nodes naturally
4. **No Token Bloat** — Only the most relevant nodes for the current query are recalled, no growth with conversation history

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Skill Memory Core                     │
├──────────────┬──────────────┬──────────────┬────────────┤
│  Extractor   │   Recaller   │    Graph     │   Store    │
├──────────────┼──────────────┼──────────────┼────────────┤
│ • LLM Extract│ • Dual-Path  │ • Community  │ • SQLite   │
│ • Node Valid.│ • PPR Rank   │ • PageRank   │ • FTS5     │
│ • Session Tidy│ • Reranker  │ • Dedup Merge│ • Vector   │
└──────────────┴──────────────┴──────────────┴────────────┘
```

### Module Responsibilities

| Module | File Path | Core Function |
|--------|-----------|---------------|
| **Extractor** | `extractor/core.py` | Extract nodes/edges from conversations; final review at session end |
| **Recaller** | `recaller/core.py` | Execute dual-path recall (precise + generalized); merge results |
| **Graph** | `graph/*.py` | Community detection, PageRank computation, graph maintenance |
| **Store** | `store/core.py` | SQLite CRUD, vector storage, FTS5 search |
| **Core** | `core.py` | Orchestrate modules; provide unified API |

---

## Workflow

### 1️⃣ Message Ingestion (Synchronous)

```python
# Called after each turn
from context_engine.skill_memory import ingest_message, after_turn

# Save user/AI messages to database
ingest_message(session_id="session_001", message=user_message)
ingest_message(session_id="session_001", message=ai_message)
```

**What happens:**
- Messages stored in `gm_messages` table
- `turn_index` recorded (turn sequence number)
- ToolMessage filtered out; only human/ai dialogue retained
- Token count estimated (for context length control)

---

### 2️⃣ Knowledge Extraction (Async Background)

```python
# after_turn() automatically triggers async task
async def after_turn(session_id, last_turn_messages):
    # 1. Save messages
    for msg in last_turn_messages:
        ingest_message(session_id, msg)
    
    # 2. Extract knowledge asynchronously (non-blocking)
    asyncio.create_task(run_turn_extract(session_id))
```

**Extractor pipeline:**

```
Dialogue messages → LLM extraction → Node/edge validation → DB insert → Async embedding generation
```

#### Extraction Rules

**Node Types:**

| Type | Description | Naming Convention | Example |
|------|-------------|-------------------|---------|
| **TASK** | A task or topic the user requested | `verb-object` | `deploy-bilibili-mcp` |
| **SKILL** | Reusable operation skill (tool/command/API) | `tool-action` | `conda-env-create` |
| **EVENT** | One-time error or exception | `phenomenon-tool` | `importerror-libgl1` |

**Edge Types:**

| Type | Direction Constraint | Meaning | `instruction` Content |
|------|---------------------|---------|----------------------|
| **USED_SKILL** | TASK → SKILL | Task used this skill | Which step, how it was called |
| **SOLVED_BY** | EVENT → SKILL | Error resolved by this skill | Exact command/operation executed |
| **REQUIRES** | SKILL → SKILL | Prerequisite dependency | Why depended, how to determine |
| **PATCHES** | SKILL → SKILL | New skill corrects an old one | Old solution's issue, new fix |
| **CONFLICTS_WITH** | SKILL ↔ SKILL | Mutual exclusion | Conflict symptom, which to choose |

**Sample extraction prompt:**

```python
# System Prompt excerpt
EXTRACT_SYS = """You are the skill_memory knowledge graph extraction engine...

1. Node extraction:
   - TASK: Specific task the user asked the Agent to complete
   - SKILL: Reusable operation skill with clear trigger conditions and steps
   - EVENT: One-time error or exception

2. Relation extraction (strictly follow direction constraints):
   - USED_SKILL: TASK → SKILL
   - SOLVED_BY: EVENT → SKILL
   - REQUIRES/PATCHES/CONFLICTS_WITH: SKILL → SKILL

Output strict JSON: {"nodes":[...],"edges":[...]}
"""
```

---

### 3️⃣ Graph Assembly (Before Conversation)

```python
# Called before sending to the LLM
from context_engine.skill_memory import assemble

result = await assemble(
    user_text="How to deploy an app with Docker?",
    messages=conversation_history
)

# result structure:
{
    "messages": [...],               # Normalized message list
    "estimated_tokens": 1200,        # Estimated token count
    "system_prompt_addition": "<skill_memory>...</skill_memory>"  # Injected knowledge context
}
```

**Assembly pipeline:**

```
User query → Recaller recalls relevant nodes → Graph traversal expansion → PPR ranking
         → Format as XML context → Inject into system prompt
```

---

### 4️⃣ Knowledge Consolidation at Session End

```python
from context_engine.skill_memory import rectification_and_standardization

# Called at session end
await rectification_and_standardization(session_id="session_001")
```

**What happens:**

1. **Final Review**
   - Promote EVENT to SKILL (if generally useful)
   - Fill in missing cross-node relationships
   - Mark obsolete nodes (outdated due to new findings)

2. **Graph Maintenance**
   - Community detection (Leiden algorithm)
   - Generate community summaries (LLM description + embedding)
   - Global PageRank update
   - Node deduplication and merging

3. **State Cleanup**
   - Clear session runtime state
   - Release memory cache

---

## Core Mechanisms

Skill Memory's core innovation lies in three automated mechanisms: experience-to-skill promotion, graph-based routing tables, and automatic merging of similar nodes.

---

### Experience → Skill Auto-Promotion

**Core idea:** When the same experience (EVENT) is validated multiple times, it is automatically promoted to a reusable skill (SKILL).

#### Promotion Flow

```
EVENT first appears → validated_count = 1
     ↓
EVENT matched again → validated_count += 1
     ↓          (+1 each time it's recalled and confirmed)
validated_count >= threshold (default 3)
     ↓
LLM finalize review:
  - Determines if the experience has general value
  - Checks if a better SKILL already exists
  - Decides whether to promote
     ↓
Pass → Node type changes from EVENT to SKILL
     → SOLVED_BY edge created (EVENT_old → SKILL_new)
     → instruction field records specific operation steps
```

**Key Variables:**

| Variable | Meaning | Default |
|----------|---------|---------|
| `validated_count` | Number of times this node was validated/matched | +1 per repeat occurrence |
| `promotion_threshold` | Threshold to trigger LLM final review | 3 |
| `finalize` | LLM call that performs final review | Triggered in `rectification_and_standardization` |

**Code Logic (extractor/core.py):**

```python
# On node extraction, if name already exists, accumulate validated_count
def upsert_node(db, c, session_id) -> UpsertResult:
    name = normalize_name(c['name'])
    ex = find_by_name(db, name)
    
    if ex:
        # Already exists: increment validation count
        count = ex.validated_count + 1
        # Merge source sessions
        sessions = list(set(ex.source_sessions + [session_id]))
        UPDATE gm_nodes SET validated_count=?, source_sessions=? WHERE id=?
    else:
        # New node, validated_count defaults to 1
        INSERT INTO gm_nodes VALUES (...)
```

```python
# At session end, finalize EVENT nodes with validated_count >= threshold
async def finalize(session_nodes, graph_summary) -> FinalizeResult:
    promoted_skills = []
    new_edges = []
    invalidations = []
    
    for node in session_nodes:
        if node.type == "EVENT" and node.validated_count >= promotion_threshold:
            # LLM review: is this experience worth promoting to SKILL?
            decision = await llm.ainvoke(
                FINALIZE_PROMPT.format(node=node, summary=graph_summary)
            )
            if decision.promote:
                promoted_skills.append(node)
                new_edges.append({
                    "from": node.id,
                    "to": decision.skill_id,
                    "type": "SOLVED_BY",
                    "instruction": decision.instruction
                })
    
    return FinalizeResult(
        promoted_skills=promoted_skills,
        new_edges=new_edges,
        invalidations=invalidations
    )
```

**Design Points:**
- Threshold-triggered LLM final review, not automatic promotion — ensures quality
- validated_count accumulates on every node match (not just on extraction)
- Original EVENT node is retained after promotion (marked old, not deleted) — preserves history
- `source_sessions` records every session ID where it appeared, enabling traceability

---

### Skill Routing Table

**Core idea:** A SKILL node's incoming edges (SOLVED_BY / USED_SKILL) naturally form a routing table, eliminating the need for a separate router model.

#### Routing Table Structure

Routing information is stored in the edge's **`instruction` field**. Each SOLVED_BY or USED_SKILL edge records how to invoke the skill:

```python
# Routing table example (graph query)
def build_routing_table(db, skill_name) -> list[dict]:
    """Query all routing entries for a given SKILL node"""
    cursor.execute("""
        SELECT e.type, e.instruction, e.condition,
               n_from.name AS from_name, n_from.type AS from_type
        FROM gm_edges e
        JOIN gm_nodes n_from ON e.from_id = n_from.id
        WHERE e.to_id = (SELECT id FROM gm_nodes WHERE name = ?)
        AND e.type IN ('SOLVED_BY', 'USED_SKILL')
    """, (skill_name,))
    return cursor.fetchall()
```

**Routing Information Format (instruction field examples):**

| Source Node | Edge Type | instruction (Invocation Method) | condition (Trigger) |
|------------|-----------|-------------------------------|---------------------|
| EVENT:importerror-libgl1 | SOLVED_BY | `apt-get install libgl1-mesa-glx` | ImportError: libGL.so.1 encountered |
| TASK:deploy-bilibili-mcp | USED_SKILL | Step 3: Create Python 3.10 env with conda | When deploying Python projects |
| SKILL:pip-install | REQUIRES | Prerequisite: verify pip is installed | — |

#### How is the Routing Table Built?

No additional steps required. When each edge is created, the `instruction` field already contains invocation information:

```python
# Extractor writes routing info when creating SOLVED_BY edges
{
    "from": "n-event-001",           # EVENT node
    "to": "n-skill-001",             # SKILL node
    "type": "SOLVED_BY",
    "instruction": "conda install -c conda-forge libgl1-mesa-glx",  # Invocation method
    "condition": "ImportError: libGL.so.1"                          # Trigger condition
}
```

#### Using the Routing Table During Recall

```python
# Edges attached to recall results naturally form the routing table
result = await recaller.recall(query="Docker deployment error")
for edge in result.edges:
    if edge.type in ("SOLVED_BY", "USED_SKILL"):
        # instruction can be directly used as an LLM tool invocation directive
        print(f"{edge.from_id} → {edge.to_id}: {edge.instruction}")
```

**Design Points:**
- Routing table is a **byproduct of the graph** — no separate construction or maintenance needed
- The `instruction` field is **structured text** — directly consumable by the LLM
- Multi-hop routing — multi-step chains can be constructed via REQUIRES edges

---

### Automatic Node Merging

**Core idea:** When the semantic similarity of two nodes exceeds a threshold, they are automatically merged into one node, retaining the more complete information.

#### Trigger Timing

1. **On each extraction:** Newly extracted nodes are compared with existing nodes by name and vector
2. **At session end:** Batch check all newly created nodes in this session
3. **During graph maintenance:** Periodic global deduplication scan

#### Merge Flow

```
Node A (name="docker-deploy-error")  ↔  Node B (name="docker-deployment-error")
     ↓
1. Vector similarity computation (cosine similarity)
2. Edge connectivity check (are they already connected?)
     ↓
Similarity >= dedup_threshold (default 0.90)
     ↓
Merge:
  - Keep the node with longer name or higher validated_count
  - validated_count = A.count + B.count
  - content = longer content
  - source_sessions = union(A.sessions, B.sessions)
  - Migrate all B's edges to A
  - Delete B (merged node)
```

**Code Logic:**

```python
def merge_nodes(db, keep_id, merge_id):
    keep = get_node(db, keep_id)
    merge = get_node(db, merge_id)
    
    # 1. Merge attributes (keep longer content, accumulate validation count)
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    
    UPDATE gm_nodes 
    SET content=?, validated_count=?, source_sessions=? 
    WHERE id=?
    
    # 2. Migrate edge relationships
    UPDATE gm_edges SET from_id=? WHERE from_id=?
    UPDATE gm_edges SET to_id=? WHERE to_id=?
    
    # 3. Delete self-loops and duplicate edges
    DELETE FROM gm_edges WHERE from_id = to_id
    DELETE FROM gm_edges 
    WHERE id NOT IN (SELECT MIN(id) GROUP BY from_id, to_id, type)
    
    # 4. Delete merged node
    delete_node(db, merge_id)
```

**Merge Condition Strategy:**

| Strategy | Condition | Priority |
|----------|-----------|----------|
| Normalized names identical | `normalize_name(a) == normalize_name(b)` | Highest (100% merge) |
| Vector similarity ≥ threshold | `cosine(a.embed, b.embed) >= 0.90` | High |
| Same community + high similarity | Same community_id + similarity ≥ 0.85 | Medium |
| Vector similarity only (no community link) | Similarity ≥ 0.95 | Medium |

**Design Points:**
- **Duplicate edge check** during migration (if two edges share from/to/type, only one is kept)
- validated_count accumulates on merge — frequently occurring knowledge naturally gains higher weight
- Name normalization rules: lowercase, hyphens replace spaces, strip punctuation

---

## Key Components

### 🔍 Extractor

**Location:** `extractor/core.py`

**Core methods:**

```python
class Extractor:
    @staticmethod
    async def extract(messages, existing_names) -> ExtractionResult:
        """Extract knowledge graph from conversation"""
        
    @staticmethod
    async def finalize(session_nodes, graph_summary) -> FinalizeResult:
        """Final review before session end"""
```

**Extraction strategy:**

- **Better to over-extract** — Attempt extraction on all conversation content (including discussion, analysis, comparisons)
- **Error-correction tracking** — When the user corrects an AI mistake, both old and new approaches are extracted, linked by a `PATCHES` edge
- **Naming consistency** — Existing node names are provided to the LLM to ensure re-use of the same name for the same thing

---

### 🎯 Recaller

**Location:** `recaller/core.py`

**Dual-path recall architecture:**

```
User Query
  ├─ Precise Path
  │   ├─ Vector search / FTS5 → seed nodes
  │   ├─ Community expansion (same-community nodes)
  │   ├─ Graph traversal (BFS max_depth=2)
  │   └─ PPR ranking
  │
  └─ Generalized Path
      ├─ Community vector search → matching communities
      ├─ Fetch community representative nodes
      ├─ Graph traversal (BFS max_depth=1)
      └─ PPR ranking
  
  ↓ Merge & deduplicate
Final result (nodes + edges)
```

**Code example:**

```python
class Recaller:
    async def recall(self, query: str) -> RecallResult:
        # Two paths execute in parallel
        precise = await self._recall_precise(query, limit)
        generalized = await self._recall_generalized(query, limit)
        
        # Merge & deduplicate
        return self._merge_results(precise, generalized)
```

**Reranker filtering:**

```python
# Secondary filter using reranker after recall
filter_contents = reranker_model.filter(
    query=query,
    candidates=[node.content for node in seeds],
    gap_score=0.5  # threshold
)
```

---

### 🕸️ Graph Engine

#### Community Detection

**Location:** `graph/community.py`

**Algorithm:** Leiden Algorithm (faster and more accurate than Louvain)

```python
def detect_communities(db: Connection) -> CommunityResult:
    # 1. Read graph structure
    cursor.execute("SELECT id FROM gm_nodes")
    cursor.execute("SELECT from_id, to_id FROM gm_edges")
    
    # 2. Build igraph
    g = ig.Graph(len(node_ids), edges, directed=False)
    
    # 3. Leiden partitioning
    partition = leidenalg.find_partition(
        g,
        leidenalg.ModularityVertexPartition,
        n_iterations=2
    )
    
    # 4. Update database
    update_communities(db, final_labels)
```

**Community summary generation:**

```python
async def summarize_communities(db, communities, llm, embed):
    for community_id, member_ids in communities.items():
        # LLM generates description
        summary = await llm.ainvoke(
            COMMUNITY_SUMMARY_SYS + f"Community members:\n{member_text}"
        )
        
        # Generate community embedding
        embedding = await embed.aembed_query(embed_text)
        
        # Save to gm_communities table
        upsert_community_summary(db, community_id, summary, len(member_ids), embedding)
```

**Use cases:**
- Pull entire community's nodes during recall (wider context coverage)
- Generalized recall (return domain overview when user asks "what work has been done")
- Group same-community nodes together during assembly (coherent context)

---

#### PageRank Computation

**Location:** `graph/pagerank.py`

**Two types of PageRank:**

| Type | When Computed | Purpose | Teleport Strategy |
|------|--------------|---------|-------------------|
| **Personalized PPR** | Real-time during recall | Query-relevant node ranking | Return to seed nodes |
| **Global PR** | Batch update at session end | Top-nodes fallback ordering | Uniform distribution |

**Personalized PageRank core logic:**

```python
def personalized_page_rank(db, seed_ids, candidate_ids, cfg):
    # teleport vector: points only to seed nodes
    teleport_weight = 1.0 / len(valid_seeds)
    
    # initial scores: concentrated on seed nodes
    rank = {node_id: teleport_weight if node_id in seed_set else 0.0}
    
    # iterative propagation
    for _ in range(iterations):
        new_rank = {}
        
        # teleport component: return to seed nodes
        for node_id in node_ids:
            new_rank[node_id] = (1 - damping) * teleport_weight if node_id in seed_set else 0.0
        
        # propagation component: gain weight from neighbors
        for node_id, neighbors in adj.items():
            contrib = rank[node_id] / len(neighbors)
            for neighbor in neighbors:
                new_rank[neighbor] += damping * contrib
        
        rank = new_rank
    
    return {'scores': {cid: rank.get(cid, 0.0) for cid in candidate_ids}}
```

**Performance:**
- Thousands of nodes: < 5ms
- O(iterations × edges)
- Graph structure cached for 30 seconds (avoids SQL query on every recall)

---

### 💾 Store

**Location:** `store/core.py`

**Database Schema:**

```sql
-- Nodes table
CREATE TABLE gm_nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,           -- TASK/SKILL/EVENT
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    validated_count INTEGER DEFAULT 1,  -- validation count (accumulated on repeat)
    source_sessions TEXT,               -- JSON array of source sessions
    community_id TEXT,                  -- community ID
    pagerank REAL DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER
);

-- Edges table
CREATE TABLE gm_edges (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,           -- USED_SKILL/SOLVED_BY/...
    instruction TEXT NOT NULL,
    condition TEXT,
    session_id TEXT,
    created_at INTEGER,
    FOREIGN KEY (from_id) REFERENCES gm_nodes(id),
    FOREIGN KEY (to_id) REFERENCES gm_nodes(id)
);

-- Messages table
CREATE TABLE gm_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,           -- human/ai
    content TEXT,                 -- JSON format
    created_at INTEGER
);

-- Vectors table
CREATE TABLE gm_vectors (
    node_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding TEXT NOT NULL,      -- JSON array
    FOREIGN KEY (node_id) REFERENCES gm_nodes(id)
);

-- Community summaries table
CREATE TABLE gm_communities (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    node_count INTEGER,
    embedding TEXT,               -- JSON array
    created_at INTEGER,
    updated_at INTEGER
);

-- FTS5 full-text search
CREATE VIRTUAL TABLE gm_nodes_fts USING fts5(
    text,
    content='gm_nodes',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE gm_nodes_fts_trigram USING fts5(
    text,
    content='gm_nodes',
    content_rowid='rowid',
    tokenize='trigram'  -- supports Chinese tokenization
);
```

**Core CRUD operations:**

```python
# Node UPSERT (auto-deduplication)
def upsert_node(db, c, session_id) -> UpsertResult:
    name = normalize_name(c['name'])
    ex = find_by_name(db, name)
    
    if ex:
        # Already exists: increment validated_count, merge source_sessions
        count = ex.validated_count + 1
        sessions = list(set(ex.source_sessions + [session_id]))
        UPDATE gm_nodes SET validated_count=?, source_sessions=? ...
    else:
        # Create new node
        INSERT INTO gm_nodes VALUES (...)
```

```python
# Graph traversal (recursive CTE)
def graph_walk(db, seed_ids, max_depth):
    walk_sql = """
        WITH RECURSIVE walk(node_id, depth) AS (
            SELECT id, 0 FROM gm_nodes WHERE id IN (?)
            UNION
            SELECT 
                CASE WHEN e.from_id = w.node_id THEN e.to_id ELSE e.from_id END,
                w.depth + 1
            FROM walk w
            JOIN gm_edges e ON (e.from_id = w.node_id OR e.to_id = w.node_id)
            WHERE w.depth < ?
        )
        SELECT DISTINCT node_id FROM walk
    """
```

```python
# Hybrid search (FTS5 + fallback LIKE)
def search_nodes(db, query, limit):
    if fts5_available(db):
        # FTS5 preferred
        sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
    else:
        # LIKE fallback
        sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ? ..."
```

---

## Data Model

### GmNode

```python
class GmNode(BaseModel):
    id: str                      # Unique ID, format "n-{timestamp}-{random}"
    type: Literal["TASK", "SKILL", "EVENT"]
    name: str                    # Normalized name (lowercase, hyphenated)
    description: str             # One-line description of the trigger scenario
    content: str                 # Plain text knowledge content
    validated_count: int = 1     # Validation count (accumulated on repeated occurrence)
    source_sessions: List[str]   # List of source sessions
    community_id: Optional[str]  # Community ID
    pagerank: float = 0          # Global PageRank score
    created_at: int              # Creation timestamp (ms)
    updated_at: int              # Update timestamp (ms)
```

### GmEdge

```python
class GmEdge(BaseModel):
    id: str                      # Unique ID, format "e-{timestamp}-{random}"
    from_id: str                 # Source node ID
    to_id: str                   # Target node ID
    type: str                    # Edge type (5 valid values)
    instruction: str             # Execution steps / invocation method
    condition: Optional[str]     # Trigger condition (required for SOLVED_BY)
    session_id: str              # Source session
    created_at: int              # Creation timestamp
```

### GmConfig

```python
class GmConfig(BaseModel):
    db_path: str = "skill_memory.db"
    compact_turn_count: int = 6       # Community maintenance interval (turns)
    recall_max_nodes: int = 6         # Max nodes to recall
    recall_max_depth: int = 2         # Max graph traversal depth
    fresh_tail_count: int = 10        # Number of fresh tail nodes
    dedup_threshold: float = 0.90     # Deduplication similarity threshold
    pagerank_damping: float = 0.85    # PageRank damping factor
    pagerank_iterations: int = 20     # PageRank iterations
    embedding: Embeddings             # Embedding model
    llm: BaseChatModel                # LLM model
```

---

## Recall Mechanism

### Precise Recall

**Goal:** Find specific triplets highly relevant to the query

**Pipeline:**

```python
async def _recall_precise(query, limit):
    # 1. Vector search for seed nodes
    vec = await embed.aembed_query(query)
    scored = vector_search_with_score(db, vec, ceil(limit/2))
    seeds = [s['node'] for s in scored]
    
    # 2. Fallback to FTS5 if insufficient
    if len(seeds) < 2:
        fts_results = search_nodes(db, query, limit)
        seeds.extend([n for n in fts_results if n.id not in seen_ids])
    
    # 3. Reranker filtering
    filter_contents = reranker_model.filter(query, [s.content for s in seeds], gap_score=0.5)
    seeds = [node_dict[c] for c in filter_contents]
    
    # 4. Community expansion
    expanded_ids = set(seed_ids)
    for seed in seeds:
        peers = get_community_peers(db, seed.id, 2)
        expanded_ids.update(peers)
    
    # 5. Graph traversal
    walk_result = graph_walk(db, list(expanded_ids), max_depth=2)
    
    # 6. PPR ranking
    ppr_result = personalized_page_rank(db, seed_ids, candidate_ids, cfg)
    filtered = sorted(nodes, key=lambda n: ppr_scores[n.id], reverse=True)[:limit]
    
    return {'nodes': filtered, 'edges': ..., 'token_estimate': ...}
```

---

### Generalized Recall

**Goal:** Provide a cross-domain overview, covering knowledge areas the precise path might miss

**Pipeline:**

```python
async def _recall_generalized(query, limit):
    # 1. Community vector search
    vec = await embed.aembed_query(query)
    scored_communities = community_vector_search(db, vec)
    
    if scored_communities:
        community_ids = [c['id'] for c in scored_communities]
        seeds = nodes_by_community_ids(db, community_ids, 3)
    
    # 2. Fallback: time-based community representatives
    if not seeds:
        seeds = community_representatives(db, 2)
    
    # 3. Reranker filtering
    filter_contents = reranker_model.filter(query, [s.content for s in seeds], gap_score=0.5)
    
    # 4. Shallow graph traversal
    walk_result = graph_walk(db, seed_ids, max_depth=1)
    
    # 5. PPR ranking
    ppr_result = personalized_page_rank(db, seed_ids, candidate_ids, cfg)
    filtered = sorted(nodes, key=lambda n: ppr_scores[n.id], reverse=True)[:limit]
    
    return {'nodes': filtered, 'edges': ..., 'token_estimate': ...}
```

---

### Merge Strategy

```python
def _merge_results(precise, generalized):
    node_map = {}
    edge_map = {}
    
    # All precise results included
    for n in precise['nodes']:
        node_map[n.id] = n
    for e in precise['edges']:
        edge_map[e.id] = e
    
    # Generalized results included after deduplication
    for n in generalized['nodes']:
        if n.id not in node_map:
            node_map[n.id] = n
    
    # Merge edges: only keep edges whose both endpoints are in the final node set
    final_ids = set(node_map.keys())
    for e in generalized['edges']:
        if e.id not in edge_map and e.from_id in final_ids and e.to_id in final_ids:
            edge_map[e.id] = e
    
    return {
        'nodes': list(node_map.values()),
        'edges': list(edge_map.values()),
        'token_estimate': ...
    }
```

---

## Graph Maintenance

### Periodic Maintenance

**Trigger:** Every N conversation turns (default: 6)

```python
async def after_turn(session_id, last_turn_messages):
    turns = turn_counter.get(session_id, 0) + 1
    maintain_interval = DEFAULT_CONFIG.compact_turn_count  # 6
    
    if turns >= maintain_interval:
        turn_counter[session_id] = 0
        
        # 1. Clear cache
        invalidate_graph_cache()
        
        # 2. Community detection
        comm = detect_communities(db)
        
        # 3. Generate community summaries
        if comm["communities"]:
            summaries = await summarize_communities(
                db, comm["communities"], DEFAULT_CONFIG.llm, embed
            )
```

---

### Maintenance at Session End

**Trigger:** Call `rectification_and_standardization()` at session end

**Steps:**

```python
async def rectification_and_standardization(session_id):
    # 1. Fetch all nodes in this session
    nodes = get_by_session(db, session_id)
    
    # 2. Build graph summary (Top 20 nodes)
    cursor.execute("SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20")
    summary = ", ".join(f"{n['type']}:{n['name']}(v{n['validated_count']},pr{n['pagerank']})" for n in top_nodes)
    
    # 3. Final review
    fin = await extractor.finalize(session_nodes=nodes, graph_summary=summary)
    
    # 4. Process promoted skills
    for nc in fin.promoted_skills:
        upsert_node(db, {"type": "SKILL", "name": nc.name, ...}, session_id)
    
    # 5. Process new edges
    for ec in fin.new_edges:
        upsert_edge(db, {...})
    
    # 6. Mark invalid nodes
    for node_id in fin.invalidations:
        delete_node(db, node_id)
    
    # 7. Execute graph maintenance
    result = await run_maintenance(db, DEFAULT_CONFIG, DEFAULT_CONFIG.llm, embed)
    
    # 8. Clean up session state
    msg_seq.pop(session_id, None)
    turn_counter.pop(session_id, None)
```

---

### Node Deduplication & Merge

**Strategy:** Based on vector similarity + name normalization

```python
def merge_nodes(db, keep_id, merge_id):
    # 1. Merge attributes (keep longer content, accumulate validation count)
    sessions = list(set(keep.source_sessions + merge.source_sessions))
    count = keep.validated_count + merge.validated_count
    content = keep.content if len(keep.content) >= len(merge.content) else merge.content
    
    # 2. Update retained node
    UPDATE gm_nodes SET content=?, validated_count=?, source_sessions=? WHERE id=?
    
    # 3. Migrate edge relationships
    UPDATE gm_edges SET from_id=? WHERE from_id=?
    UPDATE gm_edges SET to_id=? WHERE to_id=?
    
    # 4. Delete self-loops and duplicate edges
    DELETE FROM gm_edges WHERE from_id = to_id
    DELETE FROM gm_edges WHERE id NOT IN (SELECT MIN(id) GROUP BY from_id, to_id, type)
    
    # 5. Delete merged node
    delete_node(db, merge_id)
```

---

## Usage Examples

### Basic Usage

```python
from context_engine.skill_memory import ingest_message, after_turn, assemble, rectification_and_standardization

# === 1. During conversation ===
for turn in conversation:
    # Save messages
    ingest_message(session_id, user_message)
    ingest_message(session_id, ai_message)
    
    # Async knowledge extraction (background task)
    await after_turn(session_id, [user_message, ai_message])

# === 2. Before sending a request ===
# Assemble context (recall relevant knowledge)
context = await assemble(
    user_text="How to deploy an app with Docker?",
    messages=conversation_history
)

# Inject into system prompt
if "system_prompt_addition" in context:
    system_prompt += "\n\n" + context["system_prompt_addition"]

# === 3. At session end ===
await rectification_and_standardization(session_id)
```

---

### Advanced Usage: Custom Configuration

```python
from context_engine.skill_memory.core import DEFAULT_CONFIG
from models.embed_model.core import embed_model
from models.LLMs.main_llm import main_llm

# Modify configuration
custom_config = DEFAULT_CONFIG.model_copy(update={
    "db_path": "./custom_skill_memory.db",
    "compact_turn_count": 10,  # Maintain every 10 turns
    "recall_max_nodes": 10,  # Recall 10 nodes
    "recall_max_depth": 3,  # Graph traversal depth 3
    "dedup_threshold": 0.95,  # Higher dedup threshold
    "pagerank_iterations": 30,  # More PageRank iterations
})
```

---

### Querying Statistics

```python
from context_engine.skill_memory.store import get_db, get_stats

db = get_db()
stats = get_stats(db)

print(f"Total nodes: {stats['total_nodes']}")
print(f"By type: {stats['by_type']}")
print(f"Total edges: {stats['total_edges']}")
print(f"By edge type: {stats['by_edge_type']}")
print(f"Communities: {stats['communities']}")

# Example output:
# Total nodes: 156
# By type: {'TASK': 45, 'SKILL': 89, 'EVENT': 22}
# Total edges: 234
# By edge type: {'USED_SKILL': 120, 'SOLVED_BY': 67, 'REQUIRES': 30, 'PATCHES': 12, 'CONFLICTS_WITH': 5}
# Communities: 12
```

---

## Performance Optimization

### 1. Async Task Queue

**Location:** `async_task_queue.py`

```python
# Embedding generation doesn't block the main flow
async_task_queue.add_task(recaller.sync_embed(node))
```

**Benefits:**
- Returns immediately after knowledge extraction
- Embeddings generated asynchronously in the background
- Avoids waiting for LLM responses

---

### 2. Graph Structure Cache

```python
_cached: Optional[GraphStructure] = None
CACHE_TTL = 30_000  # 30 seconds

def load_graph(db):
    if _cached and (time.time() * 1000 - _cached['cached_at']) < CACHE_TTL:
        return _cached
    
    # Reload graph structure
    ...
```

**Benefits:**
- Avoids SQL query on every recall
- Same graph structure shared within 30 seconds
- Automatically invalidated after compaction

---

### 3. Vector Hash Deduplication

```python
def sync_embed(node):
    content_hash = hashlib.md5(content.encode()).hexdigest()
    existing_hash = get_vector_hash(db, node.id)
    
    if existing_hash == hash_obj:
        return  # Skip unchanged nodes
```

**Benefits:**
- Avoids redundant embedding computation
- Saves LLM call costs

---

### 4. FTS5 Full-Text Search

```python
# FTS5 preferred (fast)
if fts5_available(db):
    sql = "SELECT n.*, rank FROM gm_nodes_fts MATCH ? ORDER BY rank LIMIT ?"
else:
    # LIKE fallback (slow)
    sql = "SELECT * FROM gm_nodes WHERE name LIKE ? OR content LIKE ?"
```

**Benefits:**
- FTS5 is 10-100x faster than LIKE
- Trigram tokenization supports Chinese
- Index maintained automatically via triggers

---

### 5. Batch Operations

```python
# Batch update PageRank
def update_pageranks(db, scores):
    db.execute("BEGIN")
    for node_id, score in scores.items():
        cursor.execute("UPDATE gm_nodes SET pagerank=? WHERE id=?", (score, node_id))
    db.commit()
```

**Benefits:**
- Reduces transaction overhead
- Improves write performance

---

## FAQ

### Q1: How do I adjust the number of recalled nodes?

```python
from context_engine.skill_memory.core import DEFAULT_CONFIG

DEFAULT_CONFIG.recall_max_nodes = 10  # Default is 6
```

---

### Q2: How do I disable community detection?

Complete disabling is not currently supported, but you can increase the maintenance interval:

```python
DEFAULT_CONFIG.compact_turn_count = 100  # Maintain every 100 turns
```

---

### Q3: How do I view extracted knowledge?

```python
from context_engine.skill_memory.store import get_db, all_active_nodes, all_edges

db = get_db()
nodes = all_active_nodes(db)
edges = all_edges(db)

for node in nodes:
    print(f"[{node.type}] {node.name}: {node.description}")
```

---

### Q4: How do I clean up expired data?

```python
from context_engine.skill_memory.store import get_db, delete_node

db = get_db()

# Delete a specific node (automatically cleans related edges and vectors)
delete_node(db, "n-1234567890-abcde")
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Database** | SQLite 3 + FTS5 |
| **Vector Storage** | SQLite JSON field |
| **Graph Algorithm** | igraph + Leiden Algorithm |
| **PageRank** | Custom implementation (Python) |
| **Embedding Model** | BGE/BAAI series |
| **LLM** | LangChain ChatModel |
| **Async Framework** | asyncio |

---

## License

This project is licensed under the EMA AI Agent open-source license.

---

**Author:** MOYE  
**Last updated:** 2026-05-30
