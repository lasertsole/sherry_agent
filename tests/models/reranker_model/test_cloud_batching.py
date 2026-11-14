"""Cloud reranker scoring: one request per query, original order preserved.

``predict_scores`` used to POST one pair at a time (30 s timeout each), so a
512-pair batch could occupy hours. The API scores many documents per query;
pairs are grouped, duplicate passages are sent once and the positional result
list is rebuilt from the response indices.
"""

from __future__ import annotations

import pytest

from models.reranker_model.core import CloudReranker

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _Http:
    """Records requests and answers one result per document, in input order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []

    def post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        self.calls.append((path, payload, timeout))
        return {
            "results": [
                {"index": index, "relevance_score": 0.1 * (index + 1)}
                for index in range(len(payload["documents"]))
            ]
        }


def _cloud(http: _Http) -> CloudReranker:
    cloud = CloudReranker.__new__(CloudReranker)
    cloud._model = "rerank-model"
    cloud._http = http
    return cloud


def test_one_request_per_query_and_positional_scores():
    http = _Http()
    cloud = _cloud(http)
    pairs = [
        ("q1", "alpha"),
        ("q2", "beta"),
        ("q1", "gamma"),
        ("q1", "alpha"),  # duplicate passage under the same query
    ]

    scores = cloud.predict_scores(pairs)

    assert scores == [0.1, 0.1, 0.2, 0.1]
    assert [call[1]["query"] for call in http.calls] == ["q1", "q2"]
    # q1's duplicate passage is sent once; q2 keeps its own document.
    assert http.calls[0][1]["documents"] == ["alpha", "gamma"]
    assert http.calls[1][1]["documents"] == ["beta"]
    assert all(call[0] == "/rerank" and call[2] == 60 for call in http.calls)


def test_scores_are_clamped_to_the_unit_interval():
    class _OutOfRange(_Http):
        def post_json(self, path, payload, timeout=None):  # noqa: ARG002
            return {"results": [{"index": 0, "relevance_score": 1.7}]}

    scores = _cloud(_OutOfRange()).predict_scores([("q", "doc")])

    assert scores == [1.0]


def test_empty_pairs_make_no_request():
    http = _Http()

    assert _cloud(http).predict_scores([]) == []
    assert http.calls == []
