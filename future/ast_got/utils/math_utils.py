import math
from typing import Any
import datetime


def bayesian_update(prior_confidence: list[float], evidence_confidence: list[float],
                   statistical_power: float, edge_type: str) -> list[float]:
    edge_type_weights = {
        "supportive": 1.0,
        "causal": 1.2,
        "correlative": 0.6,
        "contradictory": -0.8
    }

    weight = edge_type_weights.get(edge_type, 0.5) * statistical_power

    updated_confidence = []
    for i, (prior, evid) in enumerate(zip(prior_confidence, evidence_confidence)):
        update = prior + weight * (evid - prior)
        update = max(0.0, min(1.0, update))
        updated_confidence.append(update)

    return updated_confidence


def calculate_entropy(distribution: list[float]) -> float:
    total = sum(distribution)
    if total == 0:
        return 0

    normalized = [p / total for p in distribution]

    entropy = 0
    for p in normalized:
        if p > 0:
            entropy -= p * math.log2(p)

    return entropy


def calculate_kl_divergence(p: list[float], q: list[float]) -> float:
    p_sum = sum(p)
    q_sum = sum(q)

    if p_sum == 0 or q_sum == 0:
        return float('inf')

    p_norm = [p_i / p_sum for p_i in p]
    q_norm = [q_i / q_sum for q_i in q]

    kl_div = 0
    for p_i, q_i in zip(p_norm, q_norm):
        if p_i > 0 and q_i > 0:
            kl_div += p_i * math.log2(p_i / q_i)

    return kl_div


def calculate_info_gain(graph, node_id: str, evidence_result: dict[str, Any]) -> dict[str, float]:
    if node_id not in graph.graph:
        return {}

    node = graph.graph.nodes[node_id]
    confidence = node.get("confidence", [0.5, 0.5, 0.5, 0.5])

    entropy = calculate_entropy(confidence)

    old_confidence = node.get("old_confidence", [0.5, 0.5, 0.5, 0.5])
    kl_div = calculate_kl_divergence(confidence, old_confidence)

    mdl_complexity = len(list(graph.graph.neighbors(node_id))) * 0.1

    return {
        "entropy": entropy,
        "kl_divergence": kl_div,
        "mdl_complexity": mdl_complexity,
        "timestamp": str(datetime.datetime.now())
    }