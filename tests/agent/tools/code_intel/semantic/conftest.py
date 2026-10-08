"""Fake embedding / reranker backends for the hermetic semantic-search tests.

The real bge-m3 GGUF is a 634 MB download and the reranker may be unavailable,
so unit and hermetic e2e tests inject deterministic fakes. The fakes model the
production contract (``embed_documents`` / ``embed_query`` / ``rank``) with a
stable bag-of-tokens embedding, so cosine ordering is meaningful and
reproducible across processes.
"""

from __future__ import annotations

import math
import re
import zlib
from typing import Any

import pytest

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

DEFAULT_FAKE_DIM = 32


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def token_vector(text: str, dim: int) -> list[float]:
    """A deterministic, L2-normalised bag-of-tokens vector."""
    vector = [0.0] * dim
    for token in _tokens(text):
        vector[zlib.crc32(token.encode()) % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class FakeEmbedding:
    """Deterministic embedding backend with a configurable dimension."""

    def __init__(self, dim: int = DEFAULT_FAKE_DIM) -> None:
        self.dim = dim
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return [token_vector(text, self.dim) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return token_vector(text, self.dim)


class FailingEmbedding:
    """Embedding backend whose every call raises (fail-open coverage)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend down")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embedding backend down")


class FakeReranker:
    """Reranker that reverses the candidate order so reordering is observable."""

    def __init__(self) -> None:
        self.calls = 0

    def rank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
        gap_score: float | None = None,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        order = list(range(len(documents)))[::-1]
        if top_k is not None:
            order = order[:top_k]
        return [
            {"rank": i + 1, "corpus_id": idx, "document": documents[idx], "score": 0.9}
            for i, idx in enumerate(order)
        ]


class PassthroughReranker:
    """Reranker that preserves the cosine order (relevance assertions stay stable)."""

    def __init__(self) -> None:
        self.calls = 0

    def rank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
        gap_score: float | None = None,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        order = list(range(len(documents)))
        if top_k is not None:
            order = order[:top_k]
        return [
            {"rank": i + 1, "corpus_id": idx, "document": documents[idx], "score": 0.9}
            for i, idx in enumerate(order)
        ]


class FailingReranker:
    """Reranker whose every call raises (fail-open coverage)."""

    def rank(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("reranker backend down")


@pytest.fixture()
def fake_embed() -> FakeEmbedding:
    return FakeEmbedding()


@pytest.fixture()
def fake_reranker() -> FakeReranker:
    return FakeReranker()
