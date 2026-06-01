import os
from pathlib import Path
from langchain_core.embeddings import Embeddings

# 获取当前所在文件夹
current_dir = Path(__file__).parent.resolve()
model_cache_folder = current_dir / "model_weight"

from sentence_transformers import SentenceTransformer
if model_cache_folder.exists():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    actual_model_path = model_cache_folder
    hf_snapshot_dir = model_cache_folder / "models--BAAI--bge-m3" / "snapshots"

    if hf_snapshot_dir.exists():
        # 获取 snapshots 下的第一个文件夹（通常就是最新的哈希版本）
        snapshot_folders = [f for f in hf_snapshot_dir.iterdir() if f.is_dir()]
        if snapshot_folders:
            actual_model_path = snapshot_folders[0]

    model = SentenceTransformer(actual_model_path.as_posix())
else:
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"

    model = SentenceTransformer("BAAI/bge-m3", cache_folder=model_cache_folder.as_posix())

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


class CustomEmbedding(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """为多个文档生成嵌入向量"""
        # normalize_embeddings=True 可以让余弦相似度计算更准确
        embeddings = model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    def embed_query(self, text: str) -> list[float]:
        """为单个查询生成嵌入向量"""
        embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()

#生成模型对象
embed_model = CustomEmbedding()