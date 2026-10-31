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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeEmbedderProfile

_MODEL_DIR = Path(__file__).resolve().parent
_WEIGHT_DIR = _MODEL_DIR / "model_weight"
_TOKENIZER_DIR = _MODEL_DIR / "tokenizer"
#: The user's explicit opt-in. ``auto`` stays OFF until the Model Config download
#: completes and writes this marker; merely having the files is not enough.
_ENABLED_MARKER = _WEIGHT_DIR / ".enabled"


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

    This is the *provisioned* check (used by status and the live smoke); the
    semantic layer's ``auto`` mode additionally requires the user opt-in marker,
    see :func:`code_embedder_enabled`.
    """
    try:
        cfg = _profile(profile)
    except RuntimeError:
        return False
    onnx = _onnx_path(cfg)
    if not onnx.is_file() or onnx.stat().st_size != int(cfg["size_bytes"]):
        return False
    return all((_TOKENIZER_DIR / name).is_file() for name in cfg["tokenizer_files"])


def code_embedder_enabled(profile: CodeEmbedderProfile | None = None) -> bool:
    """True only after the user downloaded the model (the opt-in marker)."""
    return code_embedder_available(profile) and _ENABLED_MARKER.is_file()


def enable_code_embedder(profile: CodeEmbedderProfile | None = None) -> None:
    """Write the explicit enable marker (after a successful download)."""
    cfg = _profile(profile)
    if not code_embedder_available(cfg):
        raise RuntimeError("code embedder files are incomplete; download it first")
    _ENABLED_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _ENABLED_MARKER.write_text(cfg["name"], encoding="utf-8")


def disable_code_embedder() -> None:
    """Remove the opt-in marker; ``auto`` turns OFF until a new download."""
    _ENABLED_MARKER.unlink(missing_ok=True)


def download_code_embedder(
    profile: CodeEmbedderProfile | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download the ONNX weights/tokenizer, reporting ``progress(done, total)``.

    The ONNX body is streamed by ``httpx`` (not ``hf_hub_download``) so the
    settings panel can show a real progress bar; the tokenizer files are small
    and still come from the Hub cache.
    """
    cfg = _profile(profile)
    onnx = _onnx_path(cfg)
    expected = int(cfg["size_bytes"])
    if onnx.is_file() and onnx.stat().st_size == expected:
        if progress is not None:
            progress(expected, expected)
    else:
        if onnx.exists():
            onnx.unlink()
        _download_onnx(cfg, onnx, progress)
    if not onnx.is_file() or onnx.stat().st_size != expected:
        raise RuntimeError(f"code embedder ONNX size mismatch: {onnx} is not {expected} bytes")
    _TOKENIZER_DIR.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import hf_hub_download

    for name in cfg["tokenizer_files"]:
        if not (_TOKENIZER_DIR / name).is_file():
            hf_hub_download(
                cfg["repo_id"],
                name,
                revision=cfg["revision"],
                local_dir=_TOKENIZER_DIR,
            )
    if progress is not None:
        progress(expected, expected)
    return onnx


def _download_onnx(
    cfg: CodeEmbedderProfile,
    onnx: Path,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Stream the ONNX body to ``<onnx>.part``, then atomically replace."""
    import httpx

    onnx.parent.mkdir(parents=True, exist_ok=True)
    part = onnx.with_name(f"{onnx.name}.part")
    url = (
        f"https://huggingface.co/{cfg['repo_id']}/resolve/{cfg['revision']}/{cfg['onnx_filename']}"
    )
    timeout = float(CODE_INTEL_SEMANTIC["code_intel_semantic_download_timeout_seconds"])
    chunk_size = int(CODE_INTEL_SEMANTIC["code_intel_semantic_download_chunk_bytes"])
    downloaded = 0
    logger.info("code embedder: downloading {}", cfg["repo_id"])
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0) or int(cfg["size_bytes"])
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, total)
        part.replace(onnx)
    except Exception:
        part.unlink(missing_ok=True)
        raise


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
    enable_code_embedder()
    logger.info("code embedder ready and enabled: {}", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
