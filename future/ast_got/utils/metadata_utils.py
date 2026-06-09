import hashlib
import numpy as np
from loguru import logger
from models.embed_model import embed_model
from typing import Dict, Any, List, Optional


def generate_id(prefix: str, content: str) -> str:
    hash_object = hashlib.md5(content.encode())
    hash_hex = hash_object.hexdigest()[:8]
    return f"{prefix}_{hash_hex}"


def calculate_semantic_overlap(metadata1: Dict[str, Any], metadata2: Dict[str, Any],
                              keys_to_compare: Optional[List[str]] = None) -> float:
    # 排除技术性字段
    exclusions = {'node_id', 'edge_id', 'timestamp', 'revision_history',
                  'confidence', 'impact_score'}

    if not keys_to_compare:
        all_keys = set(metadata1.keys()).union(set(metadata2.keys()))
        keys_to_compare = list(all_keys - exclusions)

    if not keys_to_compare:
        return 0.0

    # 提取两个节点的文本表示
    text1_parts = []
    text2_parts = []

    for key in keys_to_compare:
        val1 = metadata1.get(key)
        val2 = metadata2.get(key)

        # 跳过缺失值
        if val1 is None and val2 is None:
            continue

        # 转换为字符串表示
        str_val1 = _to_semantic_text(key, val1) if val1 is not None else ""
        str_val2 = _to_semantic_text(key, val2) if val2 is not None else ""

        text1_parts.append(f"{key}: {str_val1}")
        text2_parts.append(f"{key}: {str_val2}")

    text1 = "\n".join(text1_parts)
    text2 = "\n".join(text2_parts)

    # 如果都为空，返回0
    if not text1.strip() or not text2.strip():
        return 0.0

    # 生成 embedding
    vec1 = embed_model.embed_query(text1)
    vec2 = embed_model.embed_query(text2)

    # 计算余弦相似度
    similarity = _cosine_similarity(vec1, vec2)

    logger.debug(f"Semantic overlap: {similarity:.3f} | text1_len={len(text1)}, text2_len={len(text2)}")

    return float(similarity)


def _to_semantic_text(key: str, value: Any) -> str:
    """将字段值转换为适合语义比较的文本"""
    if isinstance(value, (list, tuple)):
        # 列表类型：用逗号连接
        return ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        # 字典类型：序列化
        return str(value)
    else:
        return str(value)


def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """计算余弦相似度"""
    v1 = np.array(vec1)
    v2 = np.array(vec2)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(np.dot(v1, v2) / (norm1 * norm2))


def check_falsifiability(criteria: str) -> float:
    if not criteria:
        return 0.0

    testability_phrases = ['experiment', 'measurement', 'observation', 'predict',
                           'test', 'quantify', 'threshold', 'statistical',
                           'validate', 'verify', 'contradict']

    score = 0.0
    for phrase in testability_phrases:
        if phrase in criteria.lower():
            score += 0.1

    if len(criteria) > 100:
        score += 0.2
    elif len(criteria) > 50:
        score += 0.1

    return min(score, 1.0)


def detect_biases(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    biases = []

    confidence = metadata.get('confidence', [])
    if isinstance(confidence, list) and confidence:
        avg_confidence = sum(confidence) / len(confidence)
        if avg_confidence > 0.9:
            biases.append({
                'type': 'confirmation_bias',
                'description': 'Unusually high confidence values may indicate confirmation bias',
                'severity': 'medium'
            })

    timestamp = metadata.get('timestamp', '')
    referenced_sources = metadata.get('provenance', '')
    if timestamp and 'historical' in referenced_sources.lower():
        biases.append({
            'type': 'recency_bias',
            'description': 'Historical sources should be evaluated in their proper context',
            'severity': 'low'
        })

    return biases