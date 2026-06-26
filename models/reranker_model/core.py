import os
import re
import sys
import json
from typing import Any
from pathlib import Path


import requests
import urllib3
from config import ENV_PATH


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
    print(f"[RerankerModel ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def _detect_backend() -> tuple[str, bool, dict | None]:
    """
    检测后端模式并返回配置。
    Returns: (backend_type, has_local, remote_config_or_None)
    """
    raw = _read_dotenv("RERANKER_MODEL_LOCAL", "true").strip().lower()
    use_local = raw not in ("", "false", "0", "no")

    if use_local:
        return ("local", True, None)

    # 远程模式 → 校验配置
    provider = _read_dotenv("RERANKER_MODEL_PROVIDER")
    api_base = _read_dotenv("RERANKER_API_BASE")
    api_key = _read_dotenv("RERANKER_API_KEY")
    api_name = _read_dotenv("RERANKER_API_NAME")

    if not api_base:
        _abort("RERANKER_API_BASE is empty — 远程 rerank 需要设置 API 地址 (例如 https://api.modelarts-maas.com/v1)")
    if not api_key:
        _abort("RERANKER_API_KEY is empty — 远程 rerank 需要设置 API Key")
    if not api_name:
        _abort("RERANKER_API_NAME is empty — 远程 rerank 需要设置模型名称 (例如 bge-reranker-v2-m3)")
    if not provider:
        _abort("RERANKER_MODEL_PROVIDER is empty — 远程 rerank 需要设置模型提供商 (例如 openai)")

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return ("remote", False, {"provider": provider, "api_base": api_base, "api_key": api_key, "api_name": api_name})


# ─────────────────────────────────────────────
# 1. 模块级初始化（只执行一次）
# ─────────────────────────────────────────────
_backend, _use_local, _remote_config = _detect_backend()
_local_model = None

if _use_local:
    from sentence_transformers import CrossEncoder

    current_dir = Path(__file__).parent.resolve()
    model_cache_folder = current_dir / "model_weight"

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

        _local_model = CrossEncoder(model_name_or_path=actual_model_path.as_posix())
    else:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"

        _local_model = CrossEncoder("BAAI/bge-reranker-v2-m3", cache_folder=model_cache_folder.as_posix())

        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


# ─────────────────────────────────────────────
# 4. RerankerModel 类
# ─────────────────────────────────────────────
class RerankerModel:
    """重排序模型封装（自动选择本地 CrossEncoder / 远程 MaaS API）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

    @property
    def backend(self) -> str:
        return _backend

    # ── 本地调用 ──────────────────────────────
    def _rank_local(
        self, query: str, documents: list[str], top_k: int | None, gap_score: float
    ) -> list[dict[str, Any]]:
        pairs = [[query, doc] for doc in documents]
        scores = _local_model.predict(pairs)

        doc_score_pairs = list(enumerate(scores))
        ranked = sorted(doc_score_pairs, key=lambda x: x[1], reverse=True)

        if top_k is not None:
            ranked = ranked[:top_k]

        results = [
            {
                "rank": i + 1,
                "corpus_id": int(idx),
                "document": documents[idx],
                "score": round(float(score), 4),
            }
            for i, (idx, score) in enumerate(ranked)
            if score >= gap_score
        ]
        return results

    def _filter_local(self, query: str, documents: list[str], gap_score: float) -> list[str]:
        pairs = [[query, doc] for doc in documents]
        scores = _local_model.predict(pairs)

        doc_score_pairs = list(enumerate(scores))
        if gap_score is not None:
            doc_score_pairs = [pair for pair in doc_score_pairs if pair[1] >= gap_score]

        return [documents[idx] for _, (idx, _) in enumerate(doc_score_pairs)]

    # ── 远程调用 ──────────────────────────────
    def _call_remote_api(self, query: str, documents: list[str]) -> dict:
        cfg = _remote_config
        url = f"{cfg['api_base']}/rerank"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }
        payload = {
            "model": cfg["api_name"],
            "query": query,
            "documents": documents,
        }

        resp = requests.post(url, headers=headers, data=json.dumps(payload), verify=False)
        resp.raise_for_status()
        return resp.json()

    def _rank_remote(
        self, query: str, documents: list[str], top_k: int | None, gap_score: float
    ) -> list[dict[str, Any]]:
        result = self._call_remote_api(query, documents)
        ranked = sorted(result["results"], key=lambda x: x["relevance_score"], reverse=True)

        if top_k is not None:
            ranked = ranked[:top_k]

        results = [
            {
                "rank": i + 1,
                "corpus_id": int(item["index"]),
                "document": documents[item["index"]],
                "score": round(float(item["relevance_score"]), 4),
            }
            for i, item in enumerate(ranked)
            if item["relevance_score"] >= gap_score
        ]
        return results

    def _filter_remote(self, query: str, documents: list[str], gap_score: float) -> list[str]:
        result = self._call_remote_api(query, documents)
        filtered = [item for item in result["results"] if item["relevance_score"] >= gap_score]
        return [documents[item["index"]] for item in filtered]

    # ── 公共接口 ──────────────────────────────
    def rank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
        gap_score: float | None = None,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []

        _gap_score: float = 0.0
        if gap_score is not None:
            if gap_score < 0.0 or gap_score > 1.0:
                raise ValueError("gap_score must be between 0.0 and 1.0")
            _gap_score = gap_score

        if _backend == "local":
            return self._rank_local(query, documents, top_k, _gap_score)
        return self._rank_remote(query, documents, top_k, _gap_score)

    def filter(
        self,
        query: str,
        documents: list[str],
        gap_score: float = 0.85,
    ) -> list[str]:
        if not documents:
            return []

        if _backend == "local":
            return self._filter_local(query, documents, gap_score)
        return self._filter_remote(query, documents, gap_score)


# 单例实例
reranker_model: RerankerModel = RerankerModel()
