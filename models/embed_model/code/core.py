"""Code-specific embedder backend (Jina Embeddings v2 Base Code, ONNX).

This module is the ``code/`` half of ``models/embed_model``. It is deliberately
separate from ``text/core.py``: BGE stays the text embedder (memory, RAG),
while the code-intel semantic layer only accepts a code-specific model.

The backend is ONNX rather than llama.cpp/transformers model code so it works
with the repository's transformers 5 pin:

* ``RobertaTokenizerFast`` loads the plain tokenizer files (no remote code);
* ``onnxruntime`` runs ``onnx/model.onnx`` on CPU;
* mean pooling + L2, per the model card.

Nothing loads or downloads at import time; the first embed call prepares the
files and the session. Run ``python -m models.embed_model.code.core --download``
to prefetch the weights.
"""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeEmbedderProfile

_MODEL_DIR = Path(__file__).resolve().parent
_WEIGHT_DIR = _MODEL_DIR / "model_weight"
_TOKENIZER_DIR = _MODEL_DIR / "tokenizer"


def _profile(profile: CodeEmbedderProfile | None = None) -> CodeEmbedderProfile:
    """The explicit profile, or the configured default."""
    if profile is not None:
        return profile
    name = str(CODE_INTEL_SEMANTIC["code_intel_semantic_model"])
    profiles = CODE_INTEL_SEMANTIC["code_intel_semantic_profiles"]
    try:
        return profiles[name]
    except KeyError as exc:
        raise RuntimeError(
            f"unknown code embedder profile {name!r}; configure "
            f"code_intel_semantic_model to one of {sorted(profiles)}"
        ) from exc


def _onnx_path(profile: CodeEmbedderProfile) -> Path:
    return _WEIGHT_DIR / profile["onnx_filename"]


def code_embedder_available(profile: CodeEmbedderProfile | None = None) -> bool:
    """True when every model/tokenizer file exists at the expected size.

    No network, no model load: ``auto`` uses this to stay off on a fresh install
    until the code embedder has been provisioned.
    """
    try:
        cfg = _profile(profile)
    except RuntimeError:
        return False
    onnx = _onnx_path(cfg)
    if not onnx.is_file() or onnx.stat().st_size != int(cfg["size_bytes"]):
        return False
    return all((_TOKENIZER_DIR / name).is_file() for name in cfg["tokenizer_files"])


def download_code_embedder(profile: CodeEmbedderProfile | None = None) -> Path:
    """Download the ONNX weights and tokenizer files, returning the ONNX path."""
    cfg = _profile(profile)
    from huggingface_hub import hf_hub_download

    onnx = _onnx_path(cfg)
    if not onnx.is_file() or onnx.stat().st_size != int(cfg["size_bytes"]):
        if onnx.exists():
            onnx.unlink()
        logger.info("code embedder: downloading {}", cfg["repo_id"])
        downloaded = hf_hub_download(
            cfg["repo_id"],
            cfg["onnx_filename"],
            revision=cfg["revision"],
            local_dir=_WEIGHT_DIR,
        )
        got = Path(downloaded)
        if got.resolve() != onnx.resolve():
            onnx.parent.mkdir(parents=True, exist_ok=True)
            if onnx.exists():
                onnx.unlink()
            got.replace(onnx)
    if not onnx.is_file() or onnx.stat().st_size != int(cfg["size_bytes"]):
        raise RuntimeError(
            f"code embedder ONNX size mismatch: {onnx} is not {int(cfg['size_bytes'])} bytes"
        )
    _TOKENIZER_DIR.mkdir(parents=True, exist_ok=True)
    for name in cfg["tokenizer_files"]:
        if not (_TOKENIZER_DIR / name).is_file():
            hf_hub_download(
                cfg["repo_id"],
                name,
                revision=cfg["revision"],
                local_dir=_TOKENIZER_DIR,
            )
    return onnx


class JinaCodeEmbedder:
    """Lazy ONNX embedder for one code profile."""

    def __init__(self, profile: CodeEmbedderProfile | None = None) -> None:
        self._profile = _profile(profile)
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: set[str] = set()

    @property
    def name(self) -> str:
        """The profile name recorded in ``code_embeddings.model``."""
        return self._profile["name"]

    @property
    def dim(self) -> int:
        """The profile's embedding dimension."""
        return int(self._profile["dim"])

    def is_available(self) -> bool:
        """Whether the files are already provisioned (no download)."""
        return code_embedder_available(self._profile)

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            onnx = download_code_embedder(self._profile)
            try:
                import onnxruntime as ort
                from transformers import RobertaTokenizerFast
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    f"code embedder needs onnxruntime and transformers; missing {exc.name}"
                ) from exc
            tokenizer = RobertaTokenizerFast.from_pretrained(
                str(_TOKENIZER_DIR), local_files_only=True
            )
            session = ort.InferenceSession(str(onnx), providers=["CPUExecutionProvider"])
            self._tokenizer = tokenizer
            self._session = session
            self._input_names = {item.name for item in session.get_inputs()}

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        import numpy as np

        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=int(self._profile["max_length"]),
            return_tensors="np",
        )
        feeds = {name: value for name, value in encoded.items() if name in self._input_names}
        hidden = self._session.run(None, feeds)[0]
        if hidden.ndim == 2:
            pooled = hidden
        else:
            mask = encoded["attention_mask"].astype(np.float32)
            pooled = (hidden * mask[:, :, None]).sum(axis=1) / np.clip(
                mask.sum(axis=1, keepdims=True), 1e-9, None
            )
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-9, None)
        return pooled.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed code chunks."""
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed one natural-language query."""
        return self._embed([text])[0]

    def close(self) -> None:
        """Release the tokenizer/session (the next call reloads them)."""
        self._session = None
        self._tokenizer = None
        self._input_names = set()


def build_code_embed_model(profile: CodeEmbedderProfile | None = None) -> JinaCodeEmbedder:
    """Build the configured code embedder without loading it."""
    return JinaCodeEmbedder(profile)


def main(argv: list[str] | None = None) -> int:
    """CLI: prefetch the code embedder files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="download the ONNX and tokenizer")
    args = parser.parse_args(argv)
    if not args.download:
        parser.print_help()
        return 2
    path = download_code_embedder()
    logger.info("code embedder ready: {}", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
