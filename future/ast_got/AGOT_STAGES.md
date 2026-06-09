# AGoT Algorithm - 8 Stages Documentation

AGoT (Augmented Graph of Thoughts) algorithm is an 8-stage reasoning framework for processing complex queries and generating structured analysis.

---

## Stage Overview

| Stage | Name | Description |
|-------|------|-------------|
| Stage 1 | Initialization | Initialize graph structure and root node |
| Stage 2 | Decomposition | Decompose task into multiple dimensions |
| Stage 3 | Hypothesis | Generate hypotheses for each dimension |
| Stage 4 | Evidence | Integrate evidence and update hypothesis confidence |
| Stage 5 | Pruning | Prune low-confidence nodes, merge similar nodes |
| Stage 6 | Subgraph | Extract focused subgraphs for analysis |
| Stage 7 | Composition | Generate final output |
| Stage 8 | Reflection | Self-audit and quality assessment |

---

## Stage 1: Initialization

### Purpose
Initialize the knowledge graph and create a root node to understand the user query.

### Operations
1. Create root node (node_id: `n0`)
2. Node type: `root`
3. Set initial confidence: `[0.9, 0.9, 0.9, 0.9]`
4. Extract disciplinary tags from query
5. Record timestamp and provenance metadata

### Output Node Types
- **root**: Task understanding root node

### Parameters
- `initial_layer`: Initial layer (default: "root")
- `disciplines`: List of disciplinary fields (optional)

### Default Disciplinary Tags
```
skin_immunology, dermatology, cutaneous_malignancies, ctcl, 
chromosomal_instability, skin_microbiome, cancer_progression, 
therapeutic_targets, genomics, molecular_biology, 
machine_learning, biomedical_llms
```

---

## Stage 2: Decomposition

### Purpose
Decompose complex task into multiple analysis dimensions.

### Operations
1. Starting from root node, create multiple dimension nodes
2. Connect each dimension to root (edge_type: "decomposition")
3. Set description and confidence for each dimension
4. Record timestamp and provenance

### Output Node Types
- **dimension**: Dimension node (7 default dimensions)

### Default Dimensions
1. **Scope** - Define boundaries of research question
2. **Objectives** - Specific goals to achieve
3. **Constraints** - Limitations and boundaries of analysis
4. **Data Needs** - Information required to address question
5. **Use Cases** - Practical applications of findings
6. **Potential Biases** - Sources of cognitive or methodological bias
7. **Knowledge Gaps** - Areas of uncertainty or missing information

### Parameters
- `dimensions`: Custom dimension list
- `dimension_confidence`: Dimension confidence (default: [0.8, 0.8, 0.8, 0.8])
- `dimension_layer`: Dimension layer

---

## Stage 3: Hypothesis

### Purpose
Generate multiple testable hypotheses for each dimension.

### Operations
1. Iterate through each dimension node
2. Generate K hypotheses per dimension (default K=3)
3. Assign to each hypothesis:
   - Random combination of disciplinary tags
   - Falsification criteria
   - Evaluation plan (experiment/search/simulation/meta_analysis)
   - Impact score
4. Connect hypothesis nodes to corresponding dimension (edge_type: "hypothesis")
5. Randomly flag potential biases (confirmation_bias, selection_bias, anchoring_bias)

### Output Node Types
- **hypothesis**: Hypothesis node

### Parameters
- `hypotheses_per_dimension`: Hypotheses per dimension (default: 3-5)
- `hypothesis_confidence`: Hypothesis confidence (default: [0.5, 0.5, 0.5, 0.5])
- `hypothesis_layer`: Hypothesis layer

### Bias Types
- confirmation_bias
- selection_bias
- anchoring_bias

---

## Stage 4: Evidence

### Purpose
Search and integrate evidence for hypotheses, update hypothesis confidence.

### Operations
1. **Iterate** (up to N iterations):
   - Select next hypothesis to evaluate (based on impact and confidence variance)
   - Execute evaluation plan (search/experiment/simulation/meta_analysis)
   - Create evidence nodes
   - Determine edge type (supportive/correlative/causal/temporal)
   - Update confidence using Bayesian update

2. **Create Interdisciplinary Bridges (IBN)**:
   - When evidence and hypothesis come from different disciplines
   - Connect nodes from different domains

3. **Create Hyperedges**:
   - Identify multiple evidence jointly supporting same hypothesis
   - Create higher-order relationships

4. **Calculate Topology Metrics**:
   - Degree centrality
   - Betweenness centrality
   - Closeness centrality
   - Clustering coefficient

### Output Node Types
- **evidence**: Evidence node

### Edge Types
- **supportive**: Supports hypothesis
- **correlative**: Correlated with hypothesis
- **causal**: Causes hypothesis
- **temporal**: Precedes or follows hypothesis

### Parameters
- `evidence_max_iterations`: Maximum iterations (default: 5)

### Output Metrics
- evidence_nodes_created: Number of evidence nodes
- ibns_created: Number of interdisciplinary bridges
- hyperedges_created: Number of hyperedges
- confidence_updates: Number of confidence updates

---

## Stage 5: Pruning

### Purpose
Simplify graph structure, remove low-value nodes, merge similar nodes.

### Operations

#### Pruning
1. Identify nodes with low confidence and low impact
2. Pruning conditions:
   - `min(confidence) < pruning_threshold`
   - `impact_score < impact_threshold`
3. Preserve root and dimension nodes

#### Merging
1. Identify nodes with high semantic overlap
2. Merging conditions:
   - `semantic_overlap >= merging_threshold`
3. Keep nodes with higher confidence and impact
4. Transfer edge relationships
5. Merge disciplinary tags and bias flags
6. Record merge history

### Parameters
- `pruning_threshold`: Pruning threshold (default: 0.2)
- `impact_threshold`: Impact threshold (default: 0.3)
- `merging_threshold`: Merging threshold (default: 0.8)

---

## Stage 6: Subgraph

### Purpose
Extract focused subgraphs from the complete graph for specific analysis.

### Operations
Extract subgraphs based on different criteria:

1. **High Confidence Subgraph**:
   - Filter: `avg(confidence) >= min_confidence`

2. **High Impact Subgraph**:
   - Filter: `impact_score >= min_impact`

3. **Discipline Focus Subgraph**:
   - Filter: Nodes contain specific disciplinary tags

4. **Layer Focus Subgraph**:
   - Filter: Nodes belong to specific layers

5. **Edge Pattern Subgraph**:
   - Filter: Edge types match specified patterns

6. **Interdisciplinary Subgraph**:
   - Include all interdisciplinary bridge nodes and their connections

### Parameters
- `extraction_criteria.min_confidence`: Minimum confidence (default: 0.6)
- `extraction_criteria.min_impact`: Minimum impact (default: 0.5)
- `extraction_criteria.focus_disciplines`: List of focus disciplines
- `extraction_criteria.focus_layers`: List of focus layers
- `extraction_criteria.edge_patterns`: List of edge patterns

---

## Stage 7: Composition

### Purpose
Compose subgraph analysis into final structured output.

### Operations
1. **Executive Summary**: Generate overall analysis overview
2. **Subgraph Analysis**: Generate analysis section for each subgraph
3. **Interdisciplinary Insights**: Summarize interdisciplinary bridge findings
4. **Knowledge Gaps**: Identify research opportunities

### Output Sections
- **Executive Summary**: Overview of analysis
- **High Confidence Analysis**: Analysis of high-confidence nodes
- **High Impact Analysis**: Analysis of high-impact nodes
- **Interdisciplinary Insights**: Summary of IBN findings
- **Knowledge Gaps and Research Opportunities**: Identified gaps

### Parameters
- No additional parameters (uses results from previous stages)

### Output Metrics
- section_count: Number of sections
- citation_count: Number of citations

---

## Stage 8: Reflection

### Purpose
Self-audit the analysis results and assess quality and credibility.

### Operations
Perform 8 audit checks:

1. **High Confidence Impact Coverage**:
   - Check coverage of high-confidence and high-impact nodes
   - Pass: confidence≥30% AND impact≥20%

2. **Bias Flags**:
   - Check for existence of bias flags
   - Warning: Serious biases detected

3. **Knowledge Gaps Addressed**:
   - Check if knowledge gaps are addressed in output

4. **Falsifiability**:
   - Check if hypotheses have falsification criteria
   - Pass: ≥80% hypotheses have falsification criteria

5. **Causal Claims**:
   - Check if causal claims are well-supported
   - Pass: ≥80% causal edges have confounder metadata

6. **Temporal Consistency**:
   - Check if temporal relationships are well-defined

7. **Statistical Rigor**:
   - Check statistical power of evidence
   - Pass: ≥80% evidence has adequate statistical power

8. **Collaboration Attributions**:
   - Check for proper attribution metadata

### Output Metrics
- **final_confidence**: Four-dimensional confidence vector
  - [0] Empirical
  - [1] Theoretical
  - [2] Methodological
  - [3] Consensus
- passed_checks: Number of passed checks
- warning_checks: Number of warnings
- failed_checks: Number of failures

---

## Data Flow

```
Query → Stage 1 → Stage 2 → Stage 3 → Stage 4 → Stage 5 → Stage 6 → Stage 7 → Stage 8
                ↓           ↓          ↓          ↓          ↓           ↓
              Root      Dimensions  Hypotheses  Evidence   Pruned     Subgraphs
                                           ↓       Graph      Graph       ↓
                                      Updated                                    Final
                                      Confidence                                  Output
```

---

## Node Types Summary

| Type | Description | Stage |
|------|-------------|-------|
| root | Task understanding root node | Stage 1 |
| dimension | Analysis dimension | Stage 2 |
| hypothesis | Research hypothesis | Stage 3 |
| evidence | Supporting evidence | Stage 4 |
| interdisciplinary_bridge | Cross-disciplinary bridge | Stage 4 |

---

## Edge Types Summary

| Type | Description |
|------|-------------|
| decomposition | Decomposition relationship |
| hypothesis | Hypothesis relationship |
| supportive | Supports hypothesis |
| correlative | Correlated with hypothesis |
| causal | Causes hypothesis |
| temporal | Temporal relationship |
| hyperedge_virtual | Hyperedge virtual connection |
| ibn_source | Interdisciplinary bridge (source) |
| ibn_target | Interdisciplinary bridge (target) |

---

## Generated Visualization Images

After running the algorithm, images are generated in `output/` directory for each stage:

- `stage_StageX_XXXStage.png` - Node and edge relationship diagram
- `layers_StageX_XXXStage.png` - Layer structure diagram

Node Color Coding:
- 🔴 Red: root
- 🔵 Cyan: dimension
- 🔷 Blue: hypothesis
- 🟢 Green: evidence
- 🟣 Purple: interdisciplinary_bridge

Edge Color Coding:
- 🟢 Green: supports
- 🔴 Red: contradicts
- 🟠 Orange: hyperedge_virtual
- 🟣 Purple: ibn