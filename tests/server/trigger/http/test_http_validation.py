"""Client-input validation at the HTTP boundary: 400, not 500.

The packaged-desktop route probe found five handlers letting a deliberate
``ValueError`` / JSON parse failure escape to the generic 500 funnel. These
tests drive the handlers directly and pin the structured 400 contract.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server.trigger.http.memory_entries as memory_http
from server.trigger.http import media as media_http

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _QueryRequest:
    def __init__(self, query: dict):
        self.query_params = query


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _payload(response) -> dict:
    return json.loads(response.description)


def test_media_without_parameters_answers_400():
    response = asyncio.run(media_http.get_media(_QueryRequest({})))

    assert int(response.status_code) == 400
    assert _payload(response)["message"] == "session_id and filename are required"


def test_media_with_an_unsafe_session_answers_400():
    response = asyncio.run(
        media_http.get_media(_QueryRequest({"session_id": "../../workspace", "filename": "x.png"}))
    )

    assert int(response.status_code) == 400
    assert _payload(response)["message"] == "invalid session_id"


def test_memory_archive_with_a_non_integer_id_answers_400():
    response = asyncio.run(
        memory_http.memory_entries_archive_handler(
            _JsonRequest({"reason": "manual"}),
            {"entry_id": "dummy"},
        )
    )

    assert int(response.status_code) == 400
    assert _payload(response)["message"] == "entry_id must be an integer"
