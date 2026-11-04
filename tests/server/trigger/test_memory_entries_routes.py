"""Route-level checks for the memory-entry API (Robyn TestClient).

Bodies are covered by ``tests/server/service/test_memory_entries_service.py``;
these tests pin the HTTP surface the panel calls: route registration, the
``QueryParams.get`` default-argument contract, and the stats/static route not
being swallowed by ``/memory/entries/:entry_id``.
"""

from __future__ import annotations

import json

import pytest

# Aliased: a bare ``TestClient`` name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

import agent.tools.memory as memory_module
import server.trigger.http.memory_entries  # noqa: F401  (route registration)
from server.trigger.core import app

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)


def test_stats_route_is_not_parsed_as_an_entry_id():
    memory_module.memory_entry_repo.add_entry("memory", "note")

    response = client.get("/memory/entries/stats")

    assert response.status_code == 200
    payload = json.loads(response.text)
    assert payload["targets"]["memory"]["count"] == 1


def test_list_reads_query_parameters_with_robyn_defaults():
    memory_module.memory_entry_repo.add_entry("memory", "note")

    response = client.get(
        "/memory/entries",
        query_params={"target": "memory", "sort": "created", "order": "desc"},
    )

    assert response.status_code == 200
    payload = json.loads(response.text)
    assert payload["total"] == 1
    assert payload["entries"][0]["content"] == "note"


def test_list_rejects_an_unknown_target():
    response = client.get("/memory/entries", query_params={"target": "nope"})

    assert response.status_code == 400
    assert "Invalid target" in response.text


def test_missing_entry_answers_404():
    response = client.get("/memory/entries/999999")

    assert response.status_code == 404
    assert "not found" in response.text


def test_legacy_memory_route_still_reads_the_store():
    memory_module.memory_entry_repo.add_entry("memory", "legacy note")

    response = client.get("/memory")

    assert response.status_code == 200
    assert json.loads(response.text) == {"MEMORY.md": "legacy note"}
