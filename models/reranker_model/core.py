"""
GGUF-based CrossEncoder reranker using llama-cpp-python low-level C API.

Architecture (BGE-Reranker-v2-M3):
  BERT encoder → [CLS] token → Linear(1024, 1024) + tanh → Linear(1024, 1) → sigmoid

Key design decisions:
  - Uses POOLING_TYPE_NONE to extract raw [CLS] hidden state from llama.cpp
  - Manually dequantizes Q8_0 classifier weights from the GGUF file
  - Applies classifier layers in Python (numpy) — this avoids any llama.cpp
    built-in pooling/classifier path that may have implementation bugs
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from huggingface_hub import hf_hub_download

from config import ENV_PATH

def _read_dotenv(key: str, default: str = "") -> str:
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


def _is_local() -> bool:
    raw = _read_dotenv("RERANKER_MODEL_LOCAL", "true").strip().lower()
    return raw not in ("", "false", "0", "no")


def _ll():
    if not _is_local():
        raise RuntimeError("RERANKER_MODEL_LOCAL=false, local llama_cpp is disabled")
    from llama_cpp import llama_cpp as ll
    return ll

if TYPE_CHECKING:
    import numpy.typing as npt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model download constants
# ---------------------------------------------------------------------------

GGUF_REPO_ID = "gpustack/bge-reranker-v2-m3-GGUF"
GGUF_FILENAME = "bge-reranker-v2-m3-Q8_0.gguf"
_MODEL_WEIGHT_DIR = Path(__file__).parent.resolve() / "model_weight"


def _ensure_model(
    model_path: str | os.PathLike | None = None,
) -> str:
    """Return a valid GGUF model file path.

    If *model_path* is given and exists, return it as-is.
    Otherwise auto-download the default Q8_0 GGUF model from Hugging Face
    into ``model_weight/``.

    The download is skipped if the file already exists locally.
    ``hf_hub_download`` caches to ``model_weight/`` via ``local_dir``.
    """
    if model_path is not None:
        resolved = str(model_path)
        if os.path.isfile(resolved):
            return resolved
        logger.warning("Specified model_path does not exist: %s — falling back to auto-download", resolved)

    # Destination path
    dest = _MODEL_WEIGHT_DIR / GGUF_FILENAME
    if dest.is_file():
        logger.info("GGUF model already cached at %s", dest)
        return str(dest)

    # Auto-download
    _MODEL_WEIGHT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s/%s (%s) …", GGUF_REPO_ID, GGUF_FILENAME, "636 MB Q8_0")
    dest = hf_hub_download(
        repo_id=GGUF_REPO_ID,
        filename=GGUF_FILENAME,
        local_dir=str(_MODEL_WEIGHT_DIR),
        local_dir_use_symlinks=False,
    )
    logger.info("GGUF model downloaded to %s", dest)
    return str(dest)


# ---------------------------------------------------------------------------
# Q8_0 dequantisation helper
# ---------------------------------------------------------------------------

Q8_0_BLOCK_SIZE = 32
Q8_0_BYTES_PER_BLOCK = 2 + 32  # f16 scale + 32 x int8


def dequantize_q8_0(
    raw: npt.NDArray[np.uint8], n_rows: int, n_cols: int
) -> npt.NDArray[np.float32]:
    """Dequantise a Q8_0 quantised weight matrix.

    Layout per 32-element block:
      [f16 scale (2 bytes)] [int8 quants (32 bytes)]
    """
    blocks_per_row = n_cols // Q8_0_BLOCK_SIZE
    expected_cols = blocks_per_row * Q8_0_BYTES_PER_BLOCK
    if raw.shape != (n_rows, expected_cols):
        raise ValueError(
            f"raw shape {raw.shape} != ({n_rows}, {expected_cols})"
        )

    out = np.zeros((n_rows, n_cols), dtype=np.float32)
    for row in range(n_rows):
        row_data = raw[row]
        for b in range(blocks_per_row):
            offset = b * Q8_0_BYTES_PER_BLOCK
            scale = np.frombuffer(
                row_data[offset : offset + 2].tobytes(), dtype=np.float16
            )[0]
            quants = (
                np.frombuffer(
                    row_data[offset + 2 : offset + Q8_0_BYTES_PER_BLOCK].tobytes(),
                    dtype=np.int8,
                )
                .astype(np.float32)
            )
            col_start = b * Q8_0_BLOCK_SIZE
            out[row, col_start : col_start + Q8_0_BLOCK_SIZE] = quants * scale
    return out


# ---------------------------------------------------------------------------
# Classifier weight loader + meta cache
# ---------------------------------------------------------------------------


@dataclass
class ClassifierWeights:
    """Dequantised classifier weights loaded from a GGUF file."""

    W1: npt.NDArray[np.float32]  # (n_embd, n_embd)  — cls.weight
    b1: npt.NDArray[np.float32]  # (n_embd,)          — cls.bias
    W2: npt.NDArray[np.float32]  # (n_embd,)          — cls.output.weight (1D, acts as [1, n_embd])
    b2: npt.NDArray[np.float32]  # (1,)               — cls.output.bias


@dataclass
class _MetaCache:
    """Cached model metadata used across load/unload cycles.

    Once populated, the GGUF file no longer needs to be re-read for
    classifier weights or tokenizer constants.
    """

    cw: ClassifierWeights
    n_embd: int
    cls_token: int
    sep_token: int


_META_CACHE: dict[str, _MetaCache] = {}

_BERT_CLS_TOKEN = 101  # standard BERT [CLS] token id
_BERT_SEP_TOKEN = 102  # standard BERT [SEP] token id


def load_classifier_weights(gguf_path: str | os.PathLike) -> ClassifierWeights:
    """Parse and dequantise classifier tensors from a GGUF file.

    Expected tensors:
      - cls.weight       — Q8_0 quantised  (n_embd, n_embd)
      - cls.bias         — f32             (n_embd,)
      - cls.output.weight — f32            (n_embd,)       (treated as [1, n_embd])
      - cls.output.bias  — f32             (1,)
    """
    import gguf

    reader = gguf.GGUFReader(str(gguf_path))

    def _get_tensor(name: str):
        matches = [t for t in reader.tensors if t.name == name]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one tensor named '{name}', found {len(matches)}"
            )
        return matches[0]

    # --- cls.weight (Q8_0 quantised) ---
    cls_weight_t = _get_tensor("cls.weight")
    shape_field = cls_weight_t.shape
    n_rows = int(shape_field[0])
    n_cols = int(shape_field[1])
    logger.info("Dequantising cls.weight (%s × %s, Q8_0) …", n_rows, n_cols)
    W1 = dequantize_q8_0(
        np.asarray(cls_weight_t.data, dtype=np.uint8), n_rows, n_cols
    )

    # --- cls.bias ---
    b1 = _get_tensor("cls.bias").data.copy().astype(np.float32)

    # --- cls.output.weight & cls.output.bias ---
    W2 = _get_tensor("cls.output.weight").data.copy().astype(np.float32)
    b2 = _get_tensor("cls.output.bias").data.copy().astype(np.float32)

    logger.info(
        "Classifier weights loaded: W1=%s b1=%s W2=%s b2=%s",
        W1.shape,
        b1.shape,
        W2.shape,
        b2.shape,
    )
    return ClassifierWeights(W1=W1, b1=b1, W2=W2, b2=b2)


def _get_meta(model_path: str) -> _MetaCache:
    """Return cached metadata for *model_path*, or parse & cache it."""
    cached = _META_CACHE.get(model_path)
    if cached is not None:
        return cached

    cw = load_classifier_weights(model_path)

    # n_embd from the embedding tensor
    # GGUF shape field is [n_embd, vocab_size] for token_embd.weight
    import gguf
    reader = gguf.GGUFReader(model_path)
    tok_embeds = [t for t in reader.tensors if t.name == "token_embd.weight"]
    n_embd = int(tok_embeds[0].shape[0]) if tok_embeds else 1024

    meta = _MetaCache(
        cw=cw,
        n_embd=n_embd,
        cls_token=_BERT_CLS_TOKEN,
        sep_token=_BERT_SEP_TOKEN,
    )
    _META_CACHE[model_path] = meta
    return meta


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def compute_logit(
    hidden: npt.NDArray[np.float32],  # (n_embd,)
    cw: ClassifierWeights,
) -> float:
    """Apply the classifier head to a single [CLS] hidden state.

    Linear → tanh → Linear → sigmoid → scalar score.
    """
    x = hidden @ cw.W1.T + cw.b1  # (n_embd,)
    x = np.tanh(x)
    logit = float(x @ cw.W2 + cw.b2[0])
    return logit


# ---------------------------------------------------------------------------
# CrossEncoderGGUF
# ---------------------------------------------------------------------------


class CrossEncoderGGUF:
    """A CrossEncoder re-ranker backed by a GGUF model + manually decoded
    classifier head.

    Usage::

        ce = CrossEncoderGGUF(model_path, use_gpu=False)
        score = ce.predict("query text", "passage text")
    """

    def __init__(
        self,
        model_path: str | os.PathLike | None = None,
        use_gpu: bool = True,
        n_ctx: int = 2048,
        n_threads: int = 4,
        _meta: _MetaCache | None = None,
    ) -> None:
        """Initialise and load the GGUF model into memory.

        If *_meta* is provided the classifier weights and token constants
        are taken from the cache — the GGUF file is only memory-mapped,
        not fully re-parsed.
        """
        # Auto-download if not provided
        self._model_path = _ensure_model(model_path)

        # ── Load metadata cache (or build it) ────────────────────────
        if _meta is not None:
            self._meta = _meta
        else:
            self._meta = _get_meta(self._model_path)

        # ── Load model ──────────────────────────────────────────────
        ll = _ll()
        model_params = ll.llama_model_default_params()
        if use_gpu:
            model_params.n_gpu_layers = -1
        self._model = ll.llama_load_model_from_file(
            self._model_path.encode("utf-8"), model_params
        )
        if not self._model:
            raise RuntimeError(f"Failed to load model from {model_path}")

        # ── Create context ──────────────────────────────────────────
        ctx_params = ll.llama_context_default_params()
        ctx_params.n_ctx = n_ctx
        ctx_params.n_threads = n_threads
        ctx_params.n_threads_batch = n_threads
        ctx_params.pooling_type = ll.LLAMA_POOLING_TYPE_NONE
        self._ctx = ll.llama_new_context_with_model(self._model, ctx_params)
        if not self._ctx:
            raise RuntimeError("Failed to create context")

        # ── Model dimensions ────────────────────────────────────────
        self.n_embd = self._meta.n_embd
        self.n_ctx = ll.llama_n_ctx(self._ctx)
        logger.info(
            "Model loaded: n_embd=%d n_ctx=%d",
            self.n_embd,
            self.n_ctx,
        )

        # ── Enable embeddings output ──────────────────────────────
        ll.llama_set_embeddings(self._ctx, True)

        # ── Tokeniser (live from model handle) ─────────────────────
        self._vocab = ll.llama_model_get_vocab(self._model)
        self._cls_token = self._meta.cls_token
        self._sep_token = self._meta.sep_token

        logger.info("CrossEncoderGGUF initialised")

    # ---- public API ---------------------------------------------------

    def predict(self, query: str, passage: str) -> float:
        """Score a single query–passage pair.

        Returns a float in [0, 1] (higher = more relevant).
        """
        tokens = self._build_input_ids(query, passage)
        hidden = self._encode_single(tokens)
        if hidden is None:
            return 0.5

        logit = compute_logit(hidden, self._meta.cw)
        score = 1.0 / (1.0 + np.exp(-logit))
        return score

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score a list of (query, passage) pairs."""
        return [self.predict(q, p) for q, p in pairs]

    def rank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
        gap_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """Score and rank documents by relevance to *query*.

        Args:
            query: The query string.
            documents: List of candidate document texts.
            top_k: If set, only return the top-k highest-scoring documents.
            gap_score: Minimum score threshold (0.0–1.0); documents below this
                       are excluded.

        Returns:
            A list of dicts ordered by descending score, each with keys:
                rank (int), corpus_id (int), document (str), score (float).

        Mirror of ``RerankerModel.rank`` from the old ``models/reranker_model.core``.
        """
        if not documents:
            return []

        _gap_score: float = 0.0
        if gap_score is not None:
            if gap_score < 0.0 or gap_score > 1.0:
                raise ValueError("gap_score must be between 0.0 and 1.0")
            _gap_score = gap_score

        scores = [self.predict(query, doc) for doc in documents]
        doc_scores = list(enumerate(scores))
        ranked = sorted(doc_scores, key=lambda x: x[1], reverse=True)

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
            if score >= _gap_score
        ]
        return results

    def filter(
        self,
        query: str,
        documents: list[str],
        gap_score: float = 0.5,
    ) -> list[str]:
        """Filter documents, keeping only those with relevance ≥ *gap_score*.

        Args:
            query: The query string.
            documents: List of candidate document texts.
            gap_score: Minimum score threshold (0.0–1.0); defaults to 0.5.

        Returns:
            Document texts that score ≥ *gap_score*, in original order.

        Mirror of ``RerankerModel.filter`` from the old ``models/reranker_model.core``.
        """
        if not documents:
            return []

        scores = [self.predict(query, doc) for doc in documents]
        return [doc for doc, sc in zip(documents, scores) if sc >= gap_score]

    # ---- internal ----------------------------------------------------

    def _encode_single(self, tokens: list[int]) -> npt.NDArray[np.float32] | None:
        """Encode a single sequence and return the [CLS] hidden state.

        BERT (encoder-only) models use llama_encode, NOT llama_decode.
        There is no KV cache to clear since causal attention is disabled.

        Uses ``llama_batch_get_one`` which avoids the per-token field
        assignment overhead of ``llama_batch_init``.
        """
        ll = _ll()
        n_tokens = len(tokens)
        tok_arr = (ll.llama_token * n_tokens)(*tokens)
        tok_ptr = ctypes.cast(tok_arr, ctypes.POINTER(ll.llama_token))
        logger.info("_encode_single: n_tokens=%d", n_tokens)
        batch = ll.llama_batch_get_one(tok_ptr, n_tokens, 0, 0)
        logger.info("_encode_single: batch created, calling llama_encode...")
        ret = ll.llama_encode(self._ctx, batch)
        logger.info("_encode_single: llama_encode returned %d", ret)

        if ret != 0:
            logger.warning("llama_encode returned %d (n_tokens=%d)", ret, n_tokens)
            return None

        emb = ll.llama_get_embeddings_ith(self._ctx, 0)
        if not emb:
            logger.warning("No embeddings at position 0")
            return None

        out = np.ctypeslib.as_array(
            ctypes.cast(emb, ctypes.POINTER(ctypes.c_float * self.n_embd)).contents
        ).copy()

        return out

    def _build_input_ids(self, query: str, passage: str) -> list[int]:
        """Build token ids: [CLS] query [SEP] passage [SEP]."""
        q_tokens = self._tokenize(query)
        p_tokens = self._tokenize(passage)
        ids = [self._cls_token] + q_tokens + [self._sep_token] + p_tokens + [self._sep_token]
        # Truncate if too long
        if len(ids) > self.n_ctx:
            ids = ids[: self.n_ctx]
            # Ensure last token is [SEP]
            if ids[-1] != self._sep_token and len(ids) > 1:
                ids[-1] = self._sep_token
        return ids

    def _tokenize(self, text: str) -> list[int]:
        ll = _ll()
        """Tokenise *text* (no special tokens) and return a list of token ids."""
        text_bytes = text.encode("utf-8")
        text_len = len(text_bytes)
        # First call with a conservative buffer
        max_tokens = text_len + 10
        buf = (ll.llama_token * max_tokens)()
        n = ll.llama_tokenize(
            self._vocab,
            text_bytes,
            text_len,
            buf,
            max_tokens,
            False,   # add_special
            False,   # parse_special
        )
        if n < 0:
            ll = _ll()
            # Buffer too small — resize
            max_tokens = -n
            buf = (ll.llama_token * max_tokens)()
            n = ll.llama_tokenize(
                self._vocab,
                text_bytes,
                text_len,
                buf,
                max_tokens,
                False,
                False,
            )
        return [buf[i] for i in range(n)]

    def close(self) -> None:
        ll = _ll()
        """Release native resources."""
        if hasattr(self, "_ctx") and self._ctx is not None:
            try:
                ll.llama_free(self._ctx)
            except Exception:
                pass
            self._ctx = None
        if hasattr(self, "_model") and self._model is not None:
            try:
                ll.llama_free_model(self._model)
            except Exception:
                pass
            self._model = None

    def __del__(self) -> None:
        if getattr(self, "_ctx", None) is not None or getattr(self, "_model", None) is not None:
            self.close()


__all__ = ["CrossEncoderGGUF"]