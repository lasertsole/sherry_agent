# AGoT (Augmented Graph of Thoughts) — 8-Stage Reasoning Engine

**English** | [中文](README.zh.md)

---

## Overview

AGoT (Augmented Graph of Thoughts) is a graph-based multi-stage reasoning framework. It decomposes complex problems into 8 sequential stages, incrementally building a knowledge graph to produce structured analytical output.

AGoT represents the thinking process as a **directed graph**: nodes represent thought units (dimensions, hypotheses, evidence), edges represent logical relationships, and **hyperedges** plus interdisciplinary bridges (IBN) enable cross-disciplinary analysis. The graph structure naturally supports pruning, merging, confidence propagation, and ultimately distills validated core conclusions.

---

## Comparison with CoT / ToT / GoT

| Feature | CoT | ToT | GoT | **AGoT** |
|---------|-----|-----|-----|----------|
| **Data Structure** | Linear chain | Tree | Directed acyclic graph | **Directed graph + Hyperedge** |
| **Branching Strategy** | None | BFS | Arbitrary topology | **Staged, multi-dimension decomposition** |
| **Backtracking** | None | Pruning/score-backtrack | Graph aggregation | **Bayesian confidence propagation + pruning** |
| **Cross-disciplinary** | None | None | None | **IBN interdisciplinary bridge nodes** |
| **Confidence Quantification** | None | None | Limited scoring | **4D confidence vector** (empirical/theoretical/methodological/consensus) |
| **Checkpoint Resume** | None | None | None | **Built-in Checkpoint manager** |
| **Self-Reflection** | Limited | Limited | None | **Stage 8 dedicated reflection** (8 quality checks) |
| **Output Format** | Text | Text+path | Graph aggregation | **Structured report + ultra-minimal thinking string** |

### Detailed Comparison

#### CoT (Chain-of-Thought)
CoT unfolds reasoning as a linear chain, each step depending on the previous. **Pro**: simple and direct. **Con**: no branch exploration, no backtracking, no uncertainty quantification.

AGoT improvement: replaces the chain with a graph. Each dimension is an independent branch; confidence propagation makes errors traceable and correctable.

#### ToT (Tree of Thoughts)
ToT introduces tree branching with BFS/DFS search, enabling exploration of multiple reasoning paths with pruning. **Pro**: more flexible than CoT. **Con**: tree structure prevents cross-branch information sharing, and scoring is one-dimensional.

AGoT improvement: hyperedges connect related nodes across different dimensions for cross-branch information fusion; the 4D confidence vector is far more granular than single-dimension scoring.

#### GoT (Graph of Thoughts)
GoT represents reasoning as a directed graph supporting arbitrary topology aggregation. **Pro**: most expressive. **Con**: lacks structured guidance — topology is entirely freeform, results are hard to reproduce, and there is no cross-disciplinary or confidence quantification support.

AGoT improvements:
- **Staged pipeline**: 8 fixed stages provide structured guidance, ensuring reproducible results
- **Multi-discipline injection**: Stage 1 auto-extracts discipline tags, Stage 4 creates IBN nodes for cross-disciplinary bridging
- **Bayesian confidence propagation**: each node carries a 4D confidence vector for precise reliability assessment
- **Reflection audit**: Stage 8 independently checks 8 quality metrics including bias, controversy, and citation gaps

---

## Project Structure

```
tests/ast_got/
├── agot_processor.py         # Main orchestrator — runs 8 stages
├── checkpoint_manager.py     # Checkpoint/resume manager — saves after each stage
├── models/
│   ├── node.py               # Node model (node_id, label, type, confidence)
│   ├── edge.py               # Edge model (source, target, edge_type)
│   ├── hyperedge.py          # Hyperedge model (links multiple nodes)
│   └── graph.py              # AGoTGraph — NetworkX DiGraph wrapper
├── stages/
│   ├── stage_1_initialization.py   # Initialize: root node, discipline tags
│   ├── stage_2_decomposition.py    # Decompose: split task into dimensions
│   ├── stage_3_hypothesis.py       # Hypothesize: verifiable claims per dimension
│   ├── stage_4_evidence.py         # Evidence: search/integrate, Bayesian updates
│   ├── stage_5_pruning.py          # Prune: low-confidence nodes, merge overlap
│   ├── stage_6_subgraph.py         # Subgraph: extract focused subgraphs
│   ├── stage_7_composition.py      # Compose: final analytical output
│   └── stage_8_reflection.py       # Reflect: self-audit & quality assessment
├── utils/
│   ├── visualization.py      # Graph visualization (matplotlib)
│   ├── metadata_utils.py     # Semantic overlap, bias detection, etc.
│   └── math_utils.py         # Bayesian update, entropy, KL divergence
├── test_thinking_quick.py    # Quick test — mock data verification
├── test_thinking_result.py   # Full test — runs all 8 stages
├── AGOT_STAGES.md            # Detailed stage algorithm documentation
├── USAGE_THINKING_RESULT.md  # Thinking result extraction guide
└── OPTIMIZATION_COMPARISON.md# Output compaction optimization
```

---

## 8-Stage Pipeline

```
Query → Stage 1 → Stage 2 → Stage 3 → Stage 4 → Stage 5 → Stage 6 → Stage 7 → Stage 8
                ↓           ↓          ↓          ↓          ↓           ↓
              Root      Dimensions  Hypotheses  Evidence   Pruned     Subgraphs
                                          ↓       Graph      Graph       ↓
                                     Updated                                    Final
                                     Confidence                                  Output
```

| Stage | Name | Description |
|-------|------|-------------|
| 1 | Initialize | Create root node, understand query, extract discipline tags |
| 2 | Decompose | Break problem into analysis dimensions (7 default, supports AI generation) |
| 3 | Hypothesize | Generate verifiable hypotheses per dimension |
| 4 | Evidence | Execute evaluation, integrate evidence, Bayesian confidence update |
| 5 | Prune | Remove low-confidence/low-impact nodes, merge semantic overlaps |
| 6 | Subgraph | Extract focused subgraphs by confidence/impact/discipline |
| 7 | Compose | Synthesize subgraph analyses into structured output |
| 8 | Reflect | 8-point self-audit, output 4-dimension confidence vector |

See [AGOT_STAGES.md](./AGOT_STAGES.md) for detailed stage documentation.

---

## Usage

### Basic

```python
from tests.ast_got.agot_processor import AGoTProcessor

processor = AGoTProcessor()
result = processor.process_query("your question")

# Extract thinking result as ultra-minimal string
thinking = processor.extract_thinking_result(result)
print(thinking)
# → "This analysis explores... [Confidence: 74%]"
```

### Checkpoint Resume

AGoT has built-in checkpoint management: each completed stage auto-saves. If execution is interrupted, re-running the same query resumes from the last checkpoint.

```python
result = processor.process_query("your question")

# If interrupted, next run auto-resumes
# Checkpoints are cleared on successful completion
```

### Passing Parameters

```python
result = processor.process_query(
    query="your question",
    context={"user_info": "..."},          # Extra context
    parameters={"output_dir": "my_output"} # Execution parameters
)
```

---

## Core Concepts

### Node Types

| Type | Description | Created At |
|------|-------------|------------|
| `root` | Problem understanding root | Stage 1 |
| `dimension` | Analysis dimension | Stage 2 |
| `hypothesis` | Research hypothesis | Stage 3 |
| `evidence` | Supporting evidence | Stage 4 |
| `interdisciplinary_bridge` | Cross-discipline bridge (IBN) | Stage 4 |

### Edge Types

| Type | Description |
|------|-------------|
| `decomposition` | root → dimension breakdown |
| `hypothesis` | dimension → hypothesis |
| `supportive` / `correlative` / `causal` / `temporal` | Evidence relations |
| `hyperedge_virtual` | Hyperedge virtual connection |
| `ibn_source` / `ibn_target` | Interdisciplinary bridge |

### Confidence

Confidence is a 4D vector `[empirical, theoretical, methodological, consensus]`, range `[0, 1]`, measuring empirical evidence, theoretical support, methodological rigor, and consensus level respectively.

---

## Visualization

Auto-generated images in `output/` after each stage:

- `stage_StageX_XXXStage.png` — Node & edge graph
- `layers_StageX_XXXStage.png` — Layered structure

### Node Colors

| Color | Node Type |
|-------|-----------|
| 🔴 Red | root |
| 🔵 Cyan | dimension |
| 🔷 Blue | hypothesis |
| 🟢 Green | evidence |
| 🟣 Purple | interdisciplinary_bridge |

### Edge Colors

| Color | Edge Type |
|-------|-----------|
| 🟢 Green | support |
| 🔴 Red | contradiction |
| 🟠 Orange | hyperedge virtual |
| 🟣 Purple | interdisciplinary bridge |

---

## Thinking Result Extraction

`extract_thinking_result()` converts the complex dict output into an **ultra-minimal string** (core conclusions + confidence only), suitable as AI model thinking output.

See [OPTIMIZATION_COMPARISON.md](./OPTIMIZATION_COMPARISON.md) for benchmark comparison, and [USAGE_THINKING_RESULT.md](./USAGE_THINKING_RESULT.md) for the usage guide.

---

## Related Docs

| Document | Description |
|----------|-------------|
| [AGOT_STAGES.md](./AGOT_STAGES.md) | Detailed 8-stage algorithm documentation |
| [USAGE_THINKING_RESULT.md](./USAGE_THINKING_RESULT.md) | Thinking result extraction guide |
| [OPTIMIZATION_COMPARISON.md](./OPTIMIZATION_COMPARISON.md) | Output compaction benchmarks |

## source
Adaptive Graph of Thoughts
https://github.com/SaptaDey/Adaptive-Graph-of-Thoughts-MCP-server