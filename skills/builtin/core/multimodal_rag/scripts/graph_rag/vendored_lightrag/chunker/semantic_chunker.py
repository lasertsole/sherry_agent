"""Semantic chunker, vendored from ``langchain-experimental`` 0.4.2.

Why it lives here: ``langchain-experimental`` was archived upstream (sunset
notice, 2026) and this project removed the dependency — but the ``V``
(``semantic_vector``) chunking strategy uses this class, so the class moved in
with us instead of the strategy losing a capability.

Provenance: ``langchain_experimental.text_splitter.SemanticChunker`` (MIT
License, Copyright (c) LangChain, Inc.), retrieved 2026-10-16 from version
0.4.2 together with the module-level helpers it needs
(``combine_sentences`` / ``calculate_cosine_distances`` /
``BREAKPOINT_DEFAULTS``). Behaviour is identical to upstream's by construction;
``tests/skills/builtin/core/multimodal_rag/test_semantic_chunker_parity.py``
pins it against chunk lists produced by the upstream class BEFORE the removal.

Only the surface this project uses is carried over: construction, the private
grouping methods ``_calculate_sentence_distances`` /
``_threshold_from_clusters`` / ``_calculate_breakpoint_threshold`` (the
vendored ``semantic_vector`` chunker re-implements ``split_text`` on top of
them to keep source spans), ``split_text``, and the document helpers.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Literal, cast
from collections.abc import Iterable, Sequence

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sklearn.metrics.pairwise import cosine_similarity

__all__ = ["BREAKPOINT_DEFAULTS", "BreakpointThresholdType", "SemanticChunker"]

BreakpointThresholdType = Literal["percentile", "standard_deviation", "interquartile", "gradient"]

#: Default ``breakpoint_threshold_amount`` per threshold type (upstream values).
BREAKPOINT_DEFAULTS: dict[BreakpointThresholdType, float] = {
    "percentile": 95,
    "standard_deviation": 3,
    "interquartile": 1.5,
    "gradient": 95,
}


def combine_sentences(sentences: list[dict], buffer_size: int = 1) -> list[dict]:
    """Combine each sentence with its ``buffer_size`` neighbours on both sides."""
    for i in range(len(sentences)):
        combined_sentence = ""

        for j in range(i - buffer_size, i):
            if j >= 0:
                combined_sentence += sentences[j]["sentence"] + " "

        combined_sentence += sentences[i]["sentence"]

        for j in range(i + 1, i + 1 + buffer_size):
            if j < len(sentences):
                combined_sentence += " " + sentences[j]["sentence"]

        sentences[i]["combined_sentence"] = combined_sentence

    return sentences


def calculate_cosine_distances(sentences: list[dict]) -> tuple[list[float], list[dict]]:
    """Cosine distance between each sentence and the next (upstream formula)."""
    distances = []
    for i in range(len(sentences) - 1):
        embedding_current = sentences[i]["combined_sentence_embedding"]
        embedding_next = sentences[i + 1]["combined_sentence_embedding"]

        similarity = cosine_similarity([embedding_current], [embedding_next])[0][0]
        distance = 1 - similarity

        distances.append(distance)
        sentences[i]["distance_to_next"] = distance

    return distances, sentences


class SemanticChunker:
    """Split text on semantic similarity, exactly as upstream 0.4.2 does."""

    def __init__(
        self,
        embeddings: Embeddings,
        buffer_size: int = 1,
        add_start_index: bool = False,
        breakpoint_threshold_type: BreakpointThresholdType = "percentile",
        breakpoint_threshold_amount: float | None = None,
        number_of_chunks: int | None = None,
        sentence_split_regex: str = r"(?<=[.?!])\s+",
        min_chunk_size: int | None = None,
    ):
        self._add_start_index = add_start_index
        self.embeddings = embeddings
        self.buffer_size = buffer_size
        self.breakpoint_threshold_type = breakpoint_threshold_type
        self.number_of_chunks = number_of_chunks
        self.sentence_split_regex = sentence_split_regex
        if breakpoint_threshold_amount is None:
            self.breakpoint_threshold_amount = BREAKPOINT_DEFAULTS[breakpoint_threshold_type]
        else:
            self.breakpoint_threshold_amount = breakpoint_threshold_amount
        self.min_chunk_size = min_chunk_size

    def _calculate_breakpoint_threshold(self, distances: list[float]) -> tuple[float, list[float]]:
        if self.breakpoint_threshold_type == "percentile":
            return cast(
                float, np.percentile(distances, self.breakpoint_threshold_amount)
            ), distances
        elif self.breakpoint_threshold_type == "standard_deviation":
            return cast(
                float, np.mean(distances) + self.breakpoint_threshold_amount * np.std(distances)
            ), distances
        elif self.breakpoint_threshold_type == "interquartile":
            q1, q3 = np.percentile(distances, [25, 75])
            iqr = q3 - q1

            return np.mean(distances) + self.breakpoint_threshold_amount * iqr, distances
        elif self.breakpoint_threshold_type == "gradient":
            distance_gradient = np.gradient(distances, range(0, len(distances)))
            return cast(
                float, np.percentile(distance_gradient, self.breakpoint_threshold_amount)
            ), cast(list[float], distance_gradient)
        else:
            raise ValueError(
                f"Got unexpected `breakpoint_threshold_type`: {self.breakpoint_threshold_type}"
            )

    def _threshold_from_clusters(self, distances: list[float]) -> float:
        """Threshold that yields ``number_of_chunks`` chunks (inverse percentile)."""
        if self.number_of_chunks is None:
            raise ValueError("This should never be called if `number_of_chunks` is None.")
        x1, y1 = len(distances), 0.0
        x2, y2 = 1.0, 100.0

        x = max(min(self.number_of_chunks, x1), x2)

        if x2 == x1:
            y = y2
        else:
            y = y1 + ((y2 - y1) / (x2 - x1)) * (x - x1)

        y = min(max(y, 0), 100)

        return cast(float, np.percentile(distances, y))

    def _calculate_sentence_distances(
        self, single_sentences_list: list[str]
    ) -> tuple[list[float], list[dict]]:
        """Embed the buffered sentences and return their cosine distances."""
        _sentences = [{"sentence": x, "index": i} for i, x in enumerate(single_sentences_list)]
        sentences = combine_sentences(_sentences, self.buffer_size)
        embeddings = self.embeddings.embed_documents([x["combined_sentence"] for x in sentences])
        for i, sentence in enumerate(sentences):
            sentence["combined_sentence_embedding"] = embeddings[i]

        return calculate_cosine_distances(sentences)

    def _get_single_sentences_list(self, text: str) -> list[str]:
        return re.split(self.sentence_split_regex, text)

    def split_text(self, text: str) -> list[str]:
        single_sentences_list = self._get_single_sentences_list(text)

        # having len(single_sentences_list) == 1 would cause the following
        # np.percentile to fail.
        if len(single_sentences_list) == 1:
            return single_sentences_list
        # similarly, the following np.gradient would fail
        if self.breakpoint_threshold_type == "gradient" and len(single_sentences_list) == 2:
            return single_sentences_list
        distances, sentences = self._calculate_sentence_distances(single_sentences_list)
        if self.number_of_chunks is not None:
            breakpoint_distance_threshold = self._threshold_from_clusters(distances)
            breakpoint_array = distances
        else:
            (
                breakpoint_distance_threshold,
                breakpoint_array,
            ) = self._calculate_breakpoint_threshold(distances)

        indices_above_thresh = [
            i for i, x in enumerate(breakpoint_array) if x > breakpoint_distance_threshold
        ]

        chunks = []
        start_index = 0

        for index in indices_above_thresh:
            end_index = index

            group = sentences[start_index : end_index + 1]
            combined_text = " ".join([d["sentence"] for d in group])
            if self.min_chunk_size is not None and len(combined_text) < self.min_chunk_size:
                continue
            chunks.append(combined_text)

            start_index = index + 1

        if start_index < len(sentences):
            combined_text = " ".join([d["sentence"] for d in sentences[start_index:]])
            chunks.append(combined_text)
        return chunks

    def create_documents(
        self, texts: list[str], metadatas: list[dict] | None = None
    ) -> list[Document]:
        """Create documents from a list of texts."""
        _metadatas = metadatas or [{}] * len(texts)
        documents = []
        for i, text in enumerate(texts):
            start_index = 0
            for chunk in self.split_text(text):
                metadata = copy.deepcopy(_metadatas[i])
                if self._add_start_index:
                    metadata["start_index"] = start_index
                new_doc = Document(page_content=chunk, metadata=metadata)
                documents.append(new_doc)
                start_index += len(chunk)
        return documents

    def split_documents(self, documents: Iterable[Document]) -> list[Document]:
        """Split documents."""
        texts, metadatas = [], []
        for doc in documents:
            texts.append(doc.page_content)
            metadatas.append(doc.metadata)
        return self.create_documents(texts, metadatas=metadatas)

    def transform_documents(
        self, documents: Sequence[Document], **kwargs: Any
    ) -> Sequence[Document]:
        """Transform sequence of documents by splitting them."""
        return self.split_documents(list(documents))
