"""Tests for ``GET /knowledge-graph`` — parameter plumbing and failure shape.

Two defects lived here:

1. ``nest_asyncio.apply()`` ran at import time inside ``graph_rag.core``; Robyn
   serves on uvloop, which ``nest_asyncio`` refuses to patch, so the import
   raised inside the handler and every request answered with
   ``Can't patch loop of type uvloop.Loop``. Guarded in ``graph_rag.loop_patch``.
2. ``max_depth`` / ``max_nodes`` were read as ``int(query.get(key, 3))``. Robyn
   casts a query default to ``str`` and raises ``TypeError`` for an int default,
   so the surrounding ``except`` always produced the fallback: the parameters
   were advertised but never honored. They go through ``query_int`` now.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from server.trigger.http import knowledge_graph as kg_http
from server.trigger.http.helpers import query_int

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

_GRAPH_MODULE = "skills.builtin.core.multimodal_rag.scripts.graph_rag"


class _FakeRequest:
    def __init__(self, query_params: dict | None = None):
        self.query_params = query_params or {}


class _FakeGraph:
    nodes: list = []
    edges: list = []
    is_truncated = False


class _FakeLightrag:
    def __init__(self):
        self.calls: list[dict] = []

    async def get_knowledge_graph(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeGraph()


@pytest.fixture
def fake_lightrag(monkeypatch):
    """Install a stand-in for the heavy graph_rag module the handler imports."""
    lightrag = _FakeLightrag()

    async def _get_lightrag():
        return lightrag

    module = types.ModuleType(_GRAPH_MODULE)
    module.get_lightrag = _get_lightrag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _GRAPH_MODULE, module)
    return lightrag


def _payload(response) -> dict:
    return json.loads(response.description if hasattr(response, "description") else response)


def test_query_parameters_reach_the_graph_reader(fake_lightrag):
    response = asyncio.run(
        kg_http.knowledge_graph_handler(
            _FakeRequest({"node_label": "Alice", "max_depth": "5", "max_nodes": "7"})
        )
    )

    assert fake_lightrag.calls == [{"node_label": "Alice", "max_depth": 5, "max_nodes": 7}]
    assert set(_payload(response)) == {"nodes", "edges", "is_truncated"}


def test_defaults_apply_when_the_parameters_are_absent(fake_lightrag):
    asyncio.run(kg_http.knowledge_graph_handler(_FakeRequest()))
    assert fake_lightrag.calls == [{"node_label": "*", "max_depth": 3, "max_nodes": 1000}]


def test_unparsable_values_fall_back_instead_of_raising(fake_lightrag):
    asyncio.run(
        kg_http.knowledge_graph_handler(_FakeRequest({"max_depth": "deep", "max_nodes": "-4"}))
    )
    # Non-numeric input keeps the documented default; a negative count is clamped.
    assert fake_lightrag.calls == [{"node_label": "*", "max_depth": 3, "max_nodes": 1}]


def test_graph_failure_is_reported_without_a_traceback_shaped_payload(monkeypatch):
    async def _boom():
        raise RuntimeError("graph backend down")

    module = types.ModuleType(_GRAPH_MODULE)
    module.get_lightrag = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _GRAPH_MODULE, module)

    response = asyncio.run(kg_http.knowledge_graph_handler(_FakeRequest()))
    body = _payload(response)
    assert body["error"] == "graph backend down"
    assert body["nodes"] == [] and body["edges"] == []


# ----------------------------------------------------------------------
# the shared helper itself
# ----------------------------------------------------------------------


class _StrCastingQuery(dict):
    """Minimal stand-in for Robyn's QueryParams: a non-str default is rejected."""

    def get(self, key, default=None):  # noqa: D102 - mirror of the Robyn contract
        if default is not None and not isinstance(default, str):
            raise TypeError("argument 'default': 'int' object cannot be cast as 'str'")
        return dict.get(self, key, default)


def test_query_int_survives_robyn_string_casting():
    query = _StrCastingQuery({"lines": "42"})
    assert query_int(query, "lines", 500) == 42
    assert query_int(query, "missing", 500) == 500
    assert query_int(query, "lines", 500, maximum=10) == 10
    assert query_int(query, "lines", 500, minimum=100) == 100
    assert query_int(_StrCastingQuery({"lines": "not-a-number"}), "lines", 500) == 500
