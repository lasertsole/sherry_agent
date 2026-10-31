"""Shared utilities for the models/ packages (dedup)."""

import threading
from collections.abc import Callable
from typing import Any

from loguru import logger


class LazyInstance[T]:
    """Defer a singleton's construction until its first attribute access.

    Import-time construction of the vision models resolves (and may download)
    GGUF weights and builds HTTP clients; wrapping the factory in this proxy
    keeps module import side-effect free while preserving the singleton
    contract — the first access constructs once, under a lock, and every later
    access returns the same instance (thread-safe behavior unchanged).
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        """Store *factory*; nothing is built here."""
        self._factory = factory
        self._instance: T | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> T:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._factory()
        return self._instance

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access (``invoke``, ``default``, ...) to the instance."""
        return getattr(self._resolve(), name)


def resolve_gguf_path(
    local_path, hf_repo: str, hf_filename: str, local_dir, fallback_path=None
) -> str:
    """Resolve a GGUF weight path: local hit -> fallback copy -> HF download.

    Shared by auxiliary_llm / ITTT_model / VTTT_model. The
    per-model modules keep thin wrappers passing their own closure constants.

    Args:
        local_path: Target gguf file path (usually local_dir / hf_filename).
        hf_repo: Hugging Face repo id for the download fallback.
        hf_filename: Filename inside the HF repo.
        local_dir: Directory that receives the download / fallback copy.
        fallback_path: Optional alternate local copy to clone from (auxiliary
            model_weight dir) before hitting the network.
    """
    import shutil
    from pathlib import Path as _Path

    local = _Path(local_path)
    if local.is_file():
        return str(local)

    if fallback_path is not None:
        fallback = _Path(fallback_path)
        if fallback.is_file():
            logger.info(f"Copying GGUF from {fallback} -> {local} ...")
            _Path(local_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(fallback), str(local))
            return str(local)

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise ImportError(
            "Model file not found locally and 'huggingface_hub' is not installed. "
            "Run: pip install huggingface_hub"
        ) from None

    logger.info(f"Downloading {hf_repo}/{hf_filename} -> {local_dir} ...")
    hf_hub_download(
        repo_id=hf_repo,
        filename=hf_filename,
        local_dir=str(local_dir),
    )
    return str(local)
