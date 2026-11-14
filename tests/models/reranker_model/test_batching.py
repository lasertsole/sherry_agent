"""Batched local reranker scoring: planning, ordering and safe fallback.

The GGUF cross-encoder used to call ``llama_encode`` once per candidate. It now
groups candidates into one encode per batch (bounded by a sequence cap and the
context's ubatch token budget) and falls back to the per-pair path whenever the
native batch call fails. These tests run without the model: the planner is pure
and the encode internals are replaced on a bare instance.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from models.reranker_model.core import (
    ClassifierWeights,
    CrossEncoderGGUF,
    _plan_batches,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestPlanBatches:
    def test_sequence_cap_groups_and_preserves_order(self) -> None:
        groups = _plan_batches([[1], [2], [3], [4], [5]], max_sequences=2, token_budget=100)
        assert groups == [[0, 1], [2, 3], [4]]

    def test_token_budget_closes_a_group(self) -> None:
        # 9 + 5 exceeds the 10-token budget; 5 + 4 fits.
        groups = _plan_batches([[0] * 9, [0] * 5, [0] * 4], max_sequences=8, token_budget=10)
        assert groups == [[0], [1, 2]]

    def test_an_over_budget_sequence_still_gets_its_own_batch(self) -> None:
        groups = _plan_batches([[0] * 20, [0] * 20], max_sequences=8, token_budget=10)
        assert groups == [[0], [1]]

    def test_empty_input_has_no_batches(self) -> None:
        assert _plan_batches([], max_sequences=8, token_budget=512) == []


def _bare() -> CrossEncoderGGUF:
    """A model-free instance with an identity-ish classifier head."""
    ce = CrossEncoderGGUF.__new__(CrossEncoderGGUF)
    ce._meta = SimpleNamespace(
        cw=ClassifierWeights(
            W1=np.eye(2, dtype=np.float32),
            b1=np.zeros(2, dtype=np.float32),
            W2=np.ones(2, dtype=np.float32),
            b2=np.zeros(1, dtype=np.float32),
        )
    )
    ce._batch_sequences = 2
    ce._batch_token_budget = 100
    return ce


class TestBatchedScoring:
    def test_score_pairs_keeps_positions_and_neutral_failures(self) -> None:
        ce = _bare()
        seen: list[list[list[int]]] = []
        ce._build_input_ids = lambda _q, passage: [len(passage)]  # type: ignore[method-assign]

        def _encode(sequences):
            seen.append(sequences)
            return [np.array([1.0, 2.0], dtype=np.float32), None]

        ce._encode_batch = _encode  # type: ignore[method-assign]

        scores = ce._score_pairs([("q", "aa"), ("q", "bbb")])

        assert seen == [[[2], [3]]]
        assert len(scores) == 2
        # tanh(1) + tanh(2) through the classifier, sigmoid applied.
        expected = 1.0 / (1.0 + np.exp(-(np.tanh(1.0) + np.tanh(2.0))))
        assert scores[0] == pytest.approx(float(expected))
        # A failed sequence keeps the historical neutral score, never shifting
        # the list.
        assert scores[1] == 0.5

    def test_empty_pairs_score_nothing(self) -> None:
        assert _bare()._score_pairs([]) == []

    def test_encode_batch_uses_groups_and_singles_in_order(self) -> None:
        ce = _bare()
        ce._encode_single = lambda tokens: ("single", tuple(tokens))  # type: ignore[method-assign]
        ce._encode_group = lambda sequences: [  # type: ignore[method-assign]
            ("group", tuple(tokens)) for tokens in sequences
        ]

        encoded = ce._encode_batch([[1], [2], [3], [4], [5]])

        assert encoded == [
            ("group", (1,)),
            ("group", (2,)),
            ("group", (3,)),
            ("group", (4,)),
            ("single", (5,)),
        ]

    def test_a_failed_native_batch_falls_back_to_singles(self) -> None:
        ce = _bare()
        ce._encode_single = lambda tokens: ("single", tuple(tokens))  # type: ignore[method-assign]
        ce._native_encode_group = lambda _sequences: None  # type: ignore[method-assign]

        assert ce._encode_group([[1], [2]]) == [("single", (1,)), ("single", (2,))]

    def test_a_raising_native_batch_falls_back_to_singles(self) -> None:
        ce = _bare()
        ce._encode_single = lambda tokens: ("single", tuple(tokens))  # type: ignore[method-assign]

        def _boom(_sequences):
            raise RuntimeError("llama_encode exploded")

        ce._native_encode_group = _boom  # type: ignore[method-assign]

        assert ce._encode_group([[1], [2]]) == [("single", (1,)), ("single", (2,))]

    def test_rank_and_filter_share_the_batched_scorer(self) -> None:
        ce = _bare()
        seen: list[list[tuple[str, str]]] = []

        def _predict_scores(pairs):
            seen.append(list(pairs))
            return [0.9 if passage == "a" else 0.1 for _query, passage in pairs]

        ce.predict_scores = _predict_scores  # type: ignore[method-assign]

        ranked = ce.rank("q", ["a", "b"])
        filtered = ce.filter("q", ["a", "b"], gap_score=0.5)

        assert [row["corpus_id"] for row in ranked] == [0, 1]
        assert filtered == ["a"]
        assert seen == [[("q", "a"), ("q", "b")], [("q", "a"), ("q", "b")]]
