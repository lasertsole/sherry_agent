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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np
from config.models_settings import get_model_setting
from models.http_client import OpenAICompatibleClient


def _is_local() -> bool:
    raw = os.environ.get("RERANKER_MODEL_LOCAL")
    if raw is None:
        raw = str(get_model_setting("RERANKER_MODEL_LOCAL"))
    raw = str(raw).strip().lower()
    return raw not in ("", "false", "0", "no")


def _ll():
    if not _is_local():
        raise RuntimeError("RERANKER_MODEL_LOCAL=false, local llama_cpp is disabled")
    from llama_cpp import llama_cpp as ll

    return ll


if TYPE_CHECKING:
    import numpy.typing as npt

logger = logging.getLogger(__name__)

#: Sequences scored in one ``llama_encode`` call. The context is created with
#: ``n_seq_max == this value``, so a batch never carries an out-of-range seq id.
#: The batch also stops at the context's ubatch token budget, which keeps each
#: BERT sequence whole (a split inside one sequence would break attention).
#: Measured on bge-reranker-v2-m3 Q8_0: 32 pairs went from ~12.0 s to ~8.4 s
#: (1.4x). Scores differ slightly from the per-pair path (llama.cpp's batched
#: kernels accumulate in a different order — up to ~0.04 on tightly clustered
#: candidates), so near-tie ORDER can shift while the 0.5 filter decisions stayed
#: identical in probes. Each batch shape is itself deterministic.
_BATCH_SEQUENCES = 8

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
        logger.warning(
            "Specified model_path does not exist: %s — falling back to auto-download", resolved
        )

    # Destination path
    dest = _MODEL_WEIGHT_DIR / GGUF_FILENAME
    if dest.is_file():
        logger.info("GGUF model already cached at %s", dest)
        return str(dest)

    # Auto-download
    from huggingface_hub import hf_hub_download

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
        raise ValueError(f"raw shape {raw.shape} != ({n_rows}, {expected_cols})")

    out = np.zeros((n_rows, n_cols), dtype=np.float32)
    for row in range(n_rows):
        row_data = raw[row]
        for b in range(blocks_per_row):
            offset = b * Q8_0_BYTES_PER_BLOCK
            scale = np.frombuffer(row_data[offset : offset + 2].tobytes(), dtype=np.float16)[0]
            quants = np.frombuffer(
                row_data[offset + 2 : offset + Q8_0_BYTES_PER_BLOCK].tobytes(),
                dtype=np.int8,
            ).astype(np.float32)
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
            raise ValueError(f"Expected exactly one tensor named '{name}', found {len(matches)}")
        return matches[0]

    # --- cls.weight (Q8_0 quantised) ---
    cls_weight_t = _get_tensor("cls.weight")
    shape_field = cls_weight_t.shape
    n_rows = int(shape_field[0])
    n_cols = int(shape_field[1])
    logger.info("Dequantising cls.weight (%s × %s, Q8_0) …", n_rows, n_cols)
    W1 = dequantize_q8_0(np.asarray(cls_weight_t.data, dtype=np.uint8), n_rows, n_cols)  # noqa: N806

    # --- cls.bias ---
    b1 = _get_tensor("cls.bias").data.copy().astype(np.float32)

    # --- cls.output.weight & cls.output.bias ---
    W2 = _get_tensor("cls.output.weight").data.copy().astype(np.float32)  # noqa: N806
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


def _plan_batches(
    sequences: list[list[int]], *, max_sequences: int, token_budget: int
) -> list[list[int]]:
    """Group sequence INDICES into encode batches (order preserved).

    A batch closes when it reaches ``max_sequences`` or when the next sequence
    would push it past ``token_budget``. A single sequence larger than the
    budget still goes alone (the caller falls back to the single-sequence
    encode path for it), so a long document never blocks batching for the rest.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0
    for index, tokens in enumerate(sequences):
        length = len(tokens)
        if current and (len(current) >= max_sequences or current_tokens + length > token_budget):
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(index)
        current_tokens += length
        if len(current) >= max_sequences:
            groups.append(current)
            current = []
            current_tokens = 0
    if current:
        groups.append(current)
    return groups


@runtime_checkable
class RerankerProtocol(Protocol):
    """Shared contract of the local GGUF and cloud reranker backends.

    Both backends are selected interchangeably behind ``reranker_model`` and
    expose the same three operations; this protocol makes that contract explicit
    without introducing inheritance.
    """

    def predict(self, query: str, passage: str) -> float: ...

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]: ...

    def rank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
        gap_score: float | None = None,
    ) -> list[dict[str, Any]]: ...

    def filter(
        self,
        query: str,
        documents: list[str],
        gap_score: float = 0.5,
    ) -> list[str]: ...


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
        self._model = ll.llama_load_model_from_file(self._model_path.encode("utf-8"), model_params)
        if not self._model:
            raise RuntimeError(f"Failed to load model from {model_path}")

        # ── Create context ──────────────────────────────────────────
        ctx_params = ll.llama_context_default_params()
        ctx_params.n_ctx = n_ctx
        ctx_params.n_threads = n_threads
        ctx_params.n_threads_batch = n_threads
        ctx_params.pooling_type = ll.LLAMA_POOLING_TYPE_NONE
        # Batched scoring gives every candidate its own seq id; the context
        # must admit that many sequences (the default is 1).
        ctx_params.n_seq_max = _BATCH_SEQUENCES
        self._ctx = ll.llama_new_context_with_model(self._model, ctx_params)
        if not self._ctx:
            raise RuntimeError("Failed to create context")

        # ── Model dimensions ────────────────────────────────────────
        self.n_embd = self._meta.n_embd
        self.n_ctx = ll.llama_n_ctx(self._ctx)
        self._batch_sequences = _BATCH_SEQUENCES
        # One ubatch per encode call keeps every sequence in a batch WHOLE;
        # split a BERT sequence across ubatches and its attention is wrong.
        ubatch = getattr(ll, "llama_n_ubatch", None)
        self._batch_token_budget = (
            max(1, int(ubatch(self._ctx))) if callable(ubatch) else self.n_ctx
        )
        logger.info(
            "Model loaded: n_embd=%d n_ctx=%d batch_sequences=%d batch_token_budget=%d",
            self.n_embd,
            self.n_ctx,
            self._batch_sequences,
            self._batch_token_budget,
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
        """Score a list of (query, passage) pairs (batched when possible)."""
        return self._score_pairs(pairs)

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

        scores = self.predict_scores([(query, doc) for doc in documents])
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

        scores = self.predict_scores([(query, doc) for doc in documents])
        return [doc for doc, sc in zip(documents, scores) if sc >= gap_score]

    # ---- internal ----------------------------------------------------

    def _score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Build input ids, encode in batches and apply the classifier head.

        The returned scores are positionally aligned with *pairs*; a sequence
        whose encode failed keeps the historical neutral 0.5 rather than
        shifting the list.
        """
        if not pairs:
            return []
        sequences = [self._build_input_ids(query, passage) for query, passage in pairs]
        hiddens = self._encode_batch(sequences)
        scores: list[float] = []
        for hidden in hiddens:
            if hidden is None:
                scores.append(0.5)
                continue
            logit = compute_logit(hidden, self._meta.cw)
            scores.append(float(1.0 / (1.0 + np.exp(-logit))))
        return scores

    def _encode_batch(self, sequences: list[list[int]]) -> list[npt.NDArray[np.float32] | None]:
        """Encode many sequences, returning one [CLS] hidden per input (ordered).

        Batches respect the sequence cap and the ubatch token budget; a lone
        sequence (or an over-budget one) uses the single-sequence path. A
        multi-sequence encode that fails falls back to the single path for that
        group, so scoring never fails because batching was unavailable.
        """
        if not sequences:
            return []
        groups = _plan_batches(
            sequences,
            max_sequences=self._batch_sequences,
            token_budget=self._batch_token_budget,
        )
        hiddens: list[npt.NDArray[np.float32] | None] = []
        for group in groups:
            if len(group) == 1:
                hiddens.append(self._encode_single(sequences[group[0]]))
            else:
                hiddens.extend(self._encode_group([sequences[index] for index in group]))
        return hiddens

    def _encode_group(self, sequences: list[list[int]]) -> list[npt.NDArray[np.float32] | None]:
        """Encode one multi-sequence batch, falling back to singles on failure."""
        try:
            encoded = self._native_encode_group(sequences)
        except Exception:  # noqa: BLE001 - batching is an optimization, never a gate
            logger.warning(
                "reranker batched encode failed for %d sequences; using the single path",
                len(sequences),
                exc_info=True,
            )
            encoded = None
        if encoded is not None:
            return encoded
        return [self._encode_single(tokens) for tokens in sequences]

    def _native_encode_group(
        self, sequences: list[list[int]]
    ) -> list[npt.NDArray[np.float32] | None] | None:
        """One ``llama_encode`` over several sequences; ``None`` means fall back.

        Each sequence gets its own seq id and positions starting at 0; every
        token is marked as an output so ``llama_get_embeddings_ith`` can return
        the [CLS] row at each sequence's first token offset.
        """
        ll = _ll()
        total = sum(len(tokens) for tokens in sequences)
        batch = ll.llama_batch_init(total, 0, 1)
        try:
            offsets: list[int] = []
            index = 0
            for seq_id, tokens in enumerate(sequences):
                offsets.append(index)
                for pos, token in enumerate(tokens):
                    batch.token[index] = token
                    batch.pos[index] = pos
                    batch.n_seq_id[index] = 1
                    batch.seq_id[index][0] = seq_id
                    batch.logits[index] = 1
                    index += 1
            batch.n_tokens = total
            if ll.llama_encode(self._ctx, batch) != 0:
                logger.warning(
                    "reranker llama_encode failed for %d sequences (%d tokens)",
                    len(sequences),
                    total,
                )
                return None

            hiddens: list[npt.NDArray[np.float32] | None] = []
            for offset in offsets:
                emb = ll.llama_get_embeddings_ith(self._ctx, offset)
                if not emb:
                    logger.warning("reranker batch: no embedding at token %d", offset)
                    return None
                hiddens.append(
                    np.ctypeslib.as_array(
                        ctypes.cast(emb, ctypes.POINTER(ctypes.c_float * self.n_embd)).contents
                    ).copy()
                )
            return hiddens
        finally:
            ll.llama_batch_free(batch)

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
            False,  # add_special
            False,  # parse_special
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
            except Exception as e:
                logger.debug("llama_free failed: %s", e)
            self._ctx = None
        if hasattr(self, "_model") and self._model is not None:
            try:
                ll.llama_free_model(self._model)
            except Exception as e:
                logger.debug("llama_free_model failed: %s", e)
            self._model = None

    def __del__(self) -> None:
        if getattr(self, "_ctx", None) is not None or getattr(self, "_model", None) is not None:
            self.close()


# ---------------------------------------------------------------------------
# CloudReranker — cloud API backed reranker (mirrors CrossEncoderGGUF API)
# ---------------------------------------------------------------------------


class CloudReranker:
    """OpenAI-compatible cloud reranker.

    Reads ``RERANKER_API_BASE``, ``RERANKER_API_KEY``, ``RERANKER_API_NAME``
    from ``.env`` and provides ``predict()``, ``rank()``, ``filter()`` with
    identical signatures and behaviour to ``CrossEncoderGGUF``.

    The cloud API returns ``relevance_score`` as a sigmoid-normalised
    probability in [0.0, 1.0] — no further normalisation is needed beyond
    hard-clamping.
    """

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_base = (api_base or str(get_model_setting("RERANKER_API_BASE"))).rstrip("/")
        self._api_key = api_key or str(get_model_setting("RERANKER_API_KEY"))
        self._model = model or str(get_model_setting("RERANKER_API_NAME"))
        if not self._api_base or not self._api_key:
            raise RuntimeError(
                "RERANKER_API_BASE and RERANKER_API_KEY must be set in .models "
                "when RERANKER_MODEL_LOCAL=false"
            )
        self._http = OpenAICompatibleClient(self._api_base, self._api_key)

    # ── predict ──────────────────────────────────────────────────────────

    def predict(self, query: str, passage: str) -> float:
        """Score a single query–passage pair. Returns float in [0, 1]."""
        data = self._http.post_json(
            "/rerank",
            {
                "model": self._model,
                "query": query,
                "documents": [passage],
            },
            timeout=30,
        )
        return max(0.0, min(1.0, float(data["results"][0]["relevance_score"])))

    def predict_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score a list of (query, passage) pairs."""
        return [self.predict(q, p) for q, p in pairs]

    # ── rank ─────────────────────────────────────────────────────────────

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
        """
        if not documents:
            return []

        _gap_score: float = 0.0
        if gap_score is not None:
            if gap_score < 0.0 or gap_score > 1.0:
                raise ValueError("gap_score must be between 0.0 and 1.0")
            _gap_score = gap_score

        resp_data = self._http.post_json(
            "/rerank",
            {
                "model": self._model,
                "query": query,
                "documents": documents,
            },
            timeout=60,
        )

        doc_scores: list[tuple[int, float]] = [
            (int(entry["index"]), max(0.0, min(1.0, float(entry["relevance_score"]))))
            for entry in resp_data["results"]
        ]
        ranked = sorted(doc_scores, key=lambda x: x[1], reverse=True)

        if top_k is not None:
            ranked = ranked[:top_k]

        results = [
            {
                "rank": i + 1,
                "corpus_id": idx,
                "document": documents[idx],
                "score": round(score, 4),
            }
            for i, (idx, score) in enumerate(ranked)
            if score >= _gap_score
        ]
        return results

    # ── filter ───────────────────────────────────────────────────────────

    def filter(
        self,
        query: str,
        documents: list[str],
        gap_score: float = 0.5,
    ) -> list[str]:
        """Filter documents, keeping only those with relevance >= *gap_score*.

        Args:
            query: The query string.
            documents: List of candidate document texts.
            gap_score: Minimum score threshold (0.0–1.0); defaults to 0.5.

        Returns:
            Document texts that score >= *gap_score*, in original order.
        """
        if not documents:
            return []

        data = self._http.post_json(
            "/rerank",
            {
                "model": self._model,
                "query": query,
                "documents": documents,
            },
            timeout=60,
        )

        idx_score: list[tuple[int, float]] = [
            (int(e["index"]), max(0.0, min(1.0, float(e["relevance_score"]))))
            for e in data["results"]
        ]
        idx_score.sort(key=lambda x: x[0])
        return [documents[idx] for idx, sc in idx_score if sc >= gap_score]


def reranker_conformance(
    local: CrossEncoderGGUF, cloud: CloudReranker
) -> tuple[RerankerProtocol, RerankerProtocol]:
    """Static conformance probe: both backends satisfy :class:`RerankerProtocol`.

    Returns its inputs unchanged. The return annotation makes a type checker
    prove the two implementations share one contract without inheritance, and a
    runtime test can call it to assert the same.
    """
    return local, cloud


__all__ = ["CrossEncoderGGUF", "CloudReranker", "RerankerProtocol", "reranker_conformance"]
