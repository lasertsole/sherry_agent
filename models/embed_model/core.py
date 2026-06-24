import json
import os
import re
import sys
from pathlib import Path


import requests
import urllib3
from config import ENV_PATH
from langchain_core.embeddings import Embeddings


def _read_dotenv(key: str, default: str = "") -> str:
    """仅从 .env 文件解析，不依赖 os.environ（避免被其他模块的 load_dotenv 副作用影响）。"""
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
        for mobj in re.finditer(rf'^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*?)\s*$', text, re.MULTILINE):
            raw = mobj.group(1)
            raw = raw.strip("\"'").strip()
            if raw:
                return raw
    except Exception:
        pass
    return default


def _abort(msg: str) -> None:
    print(f"[EmbedModel ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def _detect_backend() -> tuple[str, bool, dict | None]:
    """
    检测后端模式并返回配置。
    Returns: (backend_type, has_local, remote_config_or_None)
    """
    raw = _read_dotenv("EMBEDDING_MODEL_LOCAL", "true").strip().lower()
    use_local = raw not in ("", "false", "0", "no")

    if use_local:
        return ("local", True, None)

    # 远程模式 → 校验配置
    provider = _read_dotenv("EMBEDDING_MODEL_PROVIDER")
    api_base = _read_dotenv("EMBEDDING_API_BASE")
    api_key = _read_dotenv("EMBEDDING_API_KEY")
    api_name = _read_dotenv("EMBEDDING_API_NAME")

    if not api_base:
        _abort("EMBEDDING_API_BASE is empty — 远程 embedding 需要设置 API 地址 (例如 https://api.modelarts-maas.com/v1)")
    if not api_key:
        _abort("EMBEDDING_API_KEY is empty — 远程 embedding 需要设置 API Key")
    if not api_name:
        _abort("EMBEDDING_API_NAME is empty — 远程 embedding 需要设置模型名称 (例如 bge-m3)")
    if not provider:
        _abort("EMBEDDING_MODEL_PROVIDER is empty — 远程 embedding 需要设置模型提供商 (例如 openai)")

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return "remote", False, {"provider": provider, "api_base": api_base, "api_key": api_key, "api_name": api_name}


# ─────────────────────────────────────────────
# 1. 模块级初始化（只执行一次）
# ─────────────────────────────────────────────
_backend, _use_local, _remote_config = _detect_backend()
_local_model = None

if _use_local:
    from sentence_transformers import SentenceTransformer

    current_dir = Path(__file__).parent.resolve()
    model_cache_folder = current_dir / "model_weight"

    if model_cache_folder.exists():
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

        actual_model_path = model_cache_folder
        hf_snapshot_dir = model_cache_folder / "models--BAAI--bge-m3" / "snapshots"

        if hf_snapshot_dir.exists():
            snapshot_folders = [f for f in hf_snapshot_dir.iterdir() if f.is_dir()]
            if snapshot_folders:
                actual_model_path = snapshot_folders[0]

        _local_model = SentenceTransformer(actual_model_path.as_posix())
    else:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"

        _local_model = SentenceTransformer("BAAI/bge-m3", cache_folder=model_cache_folder.as_posix())

        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


# ─────────────────────────────────────────────
# 2. CustomEmbedding 类
# ─────────────────────────────────────────────
class CustomEmbedding(Embeddings):
    """嵌入模型封装（自动选择本地 SentenceTransformer / 远程 MaaS API）"""

    def _call_remote_api(self, texts: list[str]) -> dict:
        """调用远程 MaaS embedding API"""
        cfg = _remote_config
        url = f"{cfg['api_base'].rstrip('/')}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }
        payload = {
            "model": cfg["api_name"],
            "input": texts,
            "encoding_format": "float",
        }

        resp = requests.post(url, headers=headers, data=json.dumps(payload), verify=False)
        resp.raise_for_status()
        return resp.json()

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """本地 SentenceTransformer 编码"""
        embeddings = _local_model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    def _embed_remote(self, texts: list[str]) -> list[list[float]]:
        """远程 API 编码"""
        result = self._call_remote_api(texts)
        # 按输入顺序返回 embedding 向量
        data = result.get("data", [])
        # data 数组每项含 index 和 embedding
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """为多个文档生成嵌入向量"""
        if not texts:
            return []

        if _backend == "local":
            return self._embed_local(texts)
        return self._embed_remote(texts)

    def embed_query(self, text: str) -> list[float]:
        """为单个查询生成嵌入向量"""
        if _backend == "local":
            return self._embed_local([text])[0]
        return self._embed_remote([text])[0]


# 单例实例
embed_model: CustomEmbedding = CustomEmbedding()
