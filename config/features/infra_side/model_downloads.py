"""Local-model download descriptors for the 模型配置 prefetch buttons.

Each entry is an env-config group that has a built-in local entry. The service
streams every missing file into the exact path the runtime resolver expects, so
a prefetch makes the next model build a local hit.
"""

from typing import TypedDict


class LocalModelFile(TypedDict):
    """One file a local model family needs."""

    repo_id: str
    revision: str
    filename: str
    # Relative to ROOT_DIR; the runtime's own model_weight directory.
    target_dir: str
    # Optional existing copy to copy instead of downloading (ITTT/VTTT share
    # the auxiliary GGUF). Relative to ROOT_DIR.
    fallback: str | None
    # 0 = probe the Hugging Face HEAD response for the size.
    size_bytes: int


class LocalModelSpec(TypedDict):
    """The files behind one 模型配置 local entry."""

    label: str
    files: tuple[LocalModelFile, ...]


class ModelDownloadsConfig(TypedDict):
    """Download transport and the local model family registry."""

    timeout_seconds: int
    chunk_bytes: int
    # Minimum spacing between two live progress frames for the same family.
    # Terminal state changes always bypass it.
    progress_push_interval_s: float
    models: dict[str, LocalModelSpec]


MODEL_DOWNLOADS: ModelDownloadsConfig = {
    "timeout_seconds": 1800,
    "chunk_bytes": 1_048_576,
    "progress_push_interval_s": 0.25,
    "models": {
        "AUXILIARY_LLM": {
            "label": "Qwen3.5-9B (auxiliary)",
            "files": (
                {
                    "repo_id": "lmstudio-community/Qwen3.5-9B-GGUF",
                    "revision": "main",
                    "filename": "Qwen3.5-9B-Q4_K_M.gguf",
                    "target_dir": "models/LLMs/auxiliary_llm/model_weight",
                    "fallback": None,
                    "size_bytes": 0,
                },
            ),
        },
        "ITTT": {
            "label": "Qwen3.5-9B (image-to-text)",
            "files": (
                {
                    "repo_id": "lmstudio-community/Qwen3.5-9B-GGUF",
                    "revision": "main",
                    "filename": "Qwen3.5-9B-Q4_K_M.gguf",
                    "target_dir": "models/ITTT_model/model_weight",
                    "fallback": "models/LLMs/auxiliary_llm/model_weight/Qwen3.5-9B-Q4_K_M.gguf",
                    "size_bytes": 0,
                },
                {
                    "repo_id": "lmstudio-community/Qwen3.5-9B-GGUF",
                    "revision": "main",
                    "filename": "mmproj-Qwen3.5-9B-BF16.gguf",
                    "target_dir": "models/ITTT_model/model_weight",
                    "fallback": None,
                    "size_bytes": 0,
                },
            ),
        },
        "VTTT": {
            "label": "Qwen3.5-9B (video-to-text)",
            "files": (
                {
                    "repo_id": "lmstudio-community/Qwen3.5-9B-GGUF",
                    "revision": "main",
                    "filename": "Qwen3.5-9B-Q4_K_M.gguf",
                    "target_dir": "models/VTTT_model/model_weight",
                    "fallback": "models/LLMs/auxiliary_llm/model_weight/Qwen3.5-9B-Q4_K_M.gguf",
                    "size_bytes": 0,
                },
                {
                    "repo_id": "lmstudio-community/Qwen3.5-9B-GGUF",
                    "revision": "main",
                    "filename": "mmproj-Qwen3.5-9B-BF16.gguf",
                    "target_dir": "models/VTTT_model/model_weight",
                    "fallback": None,
                    "size_bytes": 0,
                },
            ),
        },
        "RERANKER": {
            "label": "bge-reranker-v2-m3",
            "files": (
                {
                    "repo_id": "gpustack/bge-reranker-v2-m3-GGUF",
                    "revision": "main",
                    "filename": "bge-reranker-v2-m3-Q8_0.gguf",
                    "target_dir": "models/reranker_model/model_weight",
                    "fallback": None,
                    "size_bytes": 0,
                },
            ),
        },
        "EMBEDDING": {
            "label": "bge-m3",
            "files": (
                {
                    "repo_id": "ggml-org/bge-m3-Q8_0-GGUF",
                    "revision": "main",
                    "filename": "bge-m3-q8_0.gguf",
                    "target_dir": "models/embed_model/text/model_weight",
                    "fallback": None,
                    "size_bytes": 0,
                },
            ),
        },
    },
}
