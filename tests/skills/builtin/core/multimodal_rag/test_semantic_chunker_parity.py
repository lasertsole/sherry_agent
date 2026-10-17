"""Semantic-chunker parity: the vendored class reproduces the archived original.

``langchain-experimental`` was archived and removed from the dependencies; the
``V`` (``semantic_vector``) chunking strategy kept its capability by VENDORING
``SemanticChunker`` into the vendored LightRAG tree
(``chunker/semantic_chunker.py``, upstream 0.4.2, MIT).

This file is the equivalence proof. ``EXPECTED`` and ``MIRROR_EXPECTED`` were
captured by running the UPSTREAM class (and the mirror on top of it) before the
package was removed — a deterministic, hash-free embedding stand-in makes the
outputs reproducible. Any drift in the ported algorithm shows up here rather
than in a chunking surprise at runtime.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from graph_rag.vendored_lightrag.chunker import semantic_vector as semantic_vector_module
from graph_rag.vendored_lightrag.chunker.semantic_chunker import SemanticChunker

pytestmark = [pytest.mark.unit]


#: Deterministic bag-of-words embeddings (crc32, NOT Python's salted ``hash``).
class _FakeEmbeddings:
    def embed_documents(self, texts):
        out = []
        for text in texts:
            vector = [0.0] * 8
            for token in text.lower().split():
                vector[zlib.crc32(token.encode()) % 8] += 1.0
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            out.append([v / norm for v in vector])
        return out

    def embed_query(self, text):
        return self.embed_documents([text])[0]


TEXTS = {
    "three_topics": "The cat sat on the mat. The cat was warm. The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    "two_sentences": "One single sentence here. And another one follows it.",
    "one_sentence": "Just one lonely sentence without any split point",
    "short_with_min_chunk_size": "Alpha beta gamma. Delta epsilon. Zeta eta theta iota. Kappa lambda mu. Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
}

#: ``f"{text}|{config}"`` -> the chunk list the UPSTREAM class produced.
EXPECTED = {
    "one_sentence|buffer_3": ["Just one lonely sentence without any split point"],
    "one_sentence|gradient": ["Just one lonely sentence without any split point"],
    "one_sentence|interquartile": ["Just one lonely sentence without any split point"],
    "one_sentence|min_chunk_size": ["Just one lonely sentence without any split point"],
    "one_sentence|number_of_chunks": ["Just one lonely sentence without any split point"],
    "one_sentence|percentile": ["Just one lonely sentence without any split point"],
    "one_sentence|standard_deviation": ["Just one lonely sentence without any split point"],
    "short_with_min_chunk_size|buffer_3": [
        "Alpha beta gamma.",
        "Delta epsilon. Zeta eta theta iota. Kappa lambda mu. Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|gradient": [
        "Alpha beta gamma. Delta epsilon. Zeta eta theta iota.",
        "Kappa lambda mu. Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|interquartile": [
        "Alpha beta gamma. Delta epsilon. Zeta eta theta iota. Kappa lambda mu.",
        "Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|min_chunk_size": [
        "Alpha beta gamma. Delta epsilon. Zeta eta theta iota. Kappa lambda mu.",
        "Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|number_of_chunks": [
        "Alpha beta gamma.",
        "Delta epsilon. Zeta eta theta iota. Kappa lambda mu.",
        "Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|percentile": [
        "Alpha beta gamma. Delta epsilon. Zeta eta theta iota. Kappa lambda mu.",
        "Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega.",
    ],
    "short_with_min_chunk_size|standard_deviation": [
        "Alpha beta gamma. Delta epsilon. Zeta eta theta iota. Kappa lambda mu. Nu xi omicron pi. Rho sigma tau. Upsilon phi chi psi omega."
    ],
    "three_topics|buffer_3": [
        "The cat sat on the mat. The cat was warm. The cat purred softly. Interest rates rose again in March.",
        "The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|gradient": [
        "The cat sat on the mat.",
        "The cat was warm. The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|interquartile": [
        "The cat sat on the mat. The cat was warm.",
        "The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points.",
        "Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|min_chunk_size": [
        "The cat sat on the mat. The cat was warm.",
        "The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|number_of_chunks": [
        "The cat sat on the mat. The cat was warm.",
        "The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points.",
        "Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|percentile": [
        "The cat sat on the mat. The cat was warm.",
        "The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
    ],
    "three_topics|standard_deviation": [
        "The cat sat on the mat. The cat was warm. The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days."
    ],
    "two_sentences|buffer_3": ["One single sentence here. And another one follows it."],
    "two_sentences|gradient": ["One single sentence here.", "And another one follows it."],
    "two_sentences|interquartile": ["One single sentence here. And another one follows it."],
    "two_sentences|min_chunk_size": ["One single sentence here. And another one follows it."],
    "two_sentences|number_of_chunks": ["One single sentence here. And another one follows it."],
    "two_sentences|percentile": ["One single sentence here. And another one follows it."],
    "two_sentences|standard_deviation": ["One single sentence here. And another one follows it."],
}

MIRROR_TEXT = "The cat sat on the mat. The cat was warm. The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days."
MIRROR_EXPECTED = [
    ["The cat sat on the mat. The cat was warm.", 0, 41],
    [
        "The cat purred softly. Interest rates rose again in March. The central bank raised rates by 25 basis points. Bond yields followed the policy move. The volcano erupted on Tuesday. Ash covered the nearby village. Flights were cancelled for two days.",
        42,
        289,
    ],
]


def _kwargs(suffix: str) -> dict:
    if suffix == "min_chunk_size":
        return {"min_chunk_size": 40}
    if suffix == "number_of_chunks":
        return {"number_of_chunks": 3}
    if suffix == "buffer_3":
        return {"buffer_size": 3}
    return {"breakpoint_threshold_type": suffix}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_the_vendored_chunker_matches_the_archived_original(key: str):
    text_name, _, suffix = key.partition("|")

    chunks = SemanticChunker(embeddings=_FakeEmbeddings(), **_kwargs(suffix)).split_text(
        TEXTS[text_name]
    )

    assert chunks == EXPECTED[key], f"{key} drifted from the frozen upstream output"


def test_the_span_mirror_matches_the_archived_original():
    """The path lightrag actually calls (``_semantic_groups_with_spans``)."""
    splitter = SemanticChunker(embeddings=_FakeEmbeddings())

    groups = [
        tuple(group)
        for group in semantic_vector_module._semantic_groups_with_spans(splitter, MIRROR_TEXT)
    ]

    assert groups == [tuple(group) for group in MIRROR_EXPECTED]


def test_the_v_chunker_is_reachable_without_the_archived_package():
    """The strategy imports and builds its splitter with no extra dependency."""
    assert callable(semantic_vector_module.chunking_by_semantic_vector)


@pytest.mark.asyncio
async def test_the_v_chunker_runs_end_to_end_on_the_local_splitter():
    """The real entry point: async embeddings -> chunks, no archived package.

    Pins the construction path (``SemanticChunker(**kwargs)``) that a static
    import check would miss.
    """

    async def _embed(texts):
        return _FakeEmbeddings().embed_documents(list(texts))

    result = await semantic_vector_module.chunking_by_semantic_vector(
        tokenizer=_StubTokenizer(),
        content=MIRROR_TEXT,
        chunk_token_size=1200,
        embedding_func=_StubEmbeddingFunc(_embed),
    )

    assert result, "the V strategy must produce chunks"
    assert all("tokens" in chunk and "content" in chunk for chunk in result)


class _StubTokenizer:
    """Enough of a tokenizer for the size guard (whitespace counting)."""

    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))

    def decode(self, tokens) -> str:
        return ""


class _StubEmbeddingFunc:
    """Mirror of LightRAG's ``EmbeddingFunc`` (async callable + dimension)."""

    def __init__(self, func):
        self.func = func
        self.embedding_dim = 8
        self.max_token_size = 8192

    async def __call__(self, texts, **kwargs):
        return await self.func(texts)
