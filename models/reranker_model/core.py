import os
from pathlib import Path
from typing import Any, List, Dict, Optional

# 获取当前所在文件夹
current_dir = Path(__file__).parent.resolve()
model_cache_folder = current_dir / "model_weight"
from sentence_transformers import CrossEncoder
if model_cache_folder.exists():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    actual_model_path = model_cache_folder
    hf_snapshot_dir = model_cache_folder / "models--BAAI--bge-reranker-v2-m3" / "snapshots"

    if hf_snapshot_dir.exists():
        snapshot_folders = [f for f in hf_snapshot_dir.iterdir() if f.is_dir()]
        if snapshot_folders:
            actual_model_path = snapshot_folders[0]

    model = CrossEncoder(model_name_or_path=actual_model_path.as_posix())
else:
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"

    model = CrossEncoder("BAAI/bge-reranker-v2-m3", cache_folder=model_cache_folder.as_posix())

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

class RerankerModel:
    """重排序模型封装"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.model = model
        self._initialized = True

    def rank(
            self,
            query: str,
            documents: List[str],
            top_k: int = None,
            gap_score: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        对文档按与查询的相关性排序

        Args:
            query: 用户查询
            documents: 候选文档列表
            top_k: 返回前k个结果（None表示全部）

        Returns:
            排序后的结果列表，包含文档内容、分数和排名
        """
        if not documents:
            return []

        # 构建查询-文档对
        pairs = [[query, doc] for doc in documents]

        # 计算分数
        scores = self.model.predict(pairs)

        # 配对并排序
        doc_score_pairs = list(enumerate(scores))
        ranked = sorted(doc_score_pairs, key=lambda x: x[1], reverse=True)

        # 截取top_k
        if top_k is not None:
            ranked = ranked[:top_k]

        # 设置分数阈值
        _gap_score: float = 0.0
        if gap_score is not None:
            if gap_score < 0.0 or gap_score > 1.0:
                raise ValueError("gap_score must be between 0.0 and 1.0")
            _gap_score = gap_score

        # 格式化输出（包含文档内容）
        results = [
            {
                "rank": i + 1,
                "corpus_id": int(idx),
                "document": documents[idx],
                "score": round(float(score), 4)
            }
            for i, (idx, score) in enumerate(ranked) if score >= _gap_score
        ]

        return results

    def filter(
            self,
            query: str,
            documents: List[str],
            gap_score: float = 0.85
    ) -> List[str]:
        if not documents:
            return []

        # 构建查询-文档对
        pairs = [[query, doc] for doc in documents]

        # 计算分数
        scores = self.model.predict(pairs)

        # 配对并排序
        doc_score_pairs = list(enumerate(scores))

        if gap_score is not None:
            # 删除分数低于gap_score的文档
            doc_score_pairs = [pair for pair in doc_score_pairs if pair[1] >= gap_score]

        # 格式化输出（包含文档内容）
        return [documents[idx] for i, (idx, score) in enumerate(doc_score_pairs)]

# 单例实例
reranker_model: RerankerModel = RerankerModel()