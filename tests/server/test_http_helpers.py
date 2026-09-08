"""TDD tests for audit 2.1.6 — shared HTTP response helpers (server/trigger/http/helpers.py).

Pins the exact responses the former per-module copies produced, and that the
endpoint modules still expose them under their original private names.
"""

import json

import pytest

from server.trigger.http.helpers import (
    bad_request,
    not_found,
    ok,
    read_body,
    to_text_response,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, payload=None, *, raises=False):
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._payload


class TestResponseHelpers:
    def test_to_text_response_shape(self):
        payload = {"success": True, "n": 1}
        resp = to_text_response(201, payload)

        assert resp.status_code == 201
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.description == json.dumps(payload, ensure_ascii=False)

    def test_ok_is_200(self):
        resp = ok({"x": "小兰"})
        assert resp.status_code == 200
        assert "小兰" in resp.description

    def test_bad_request_shape(self):
        resp = bad_request("Missing field")
        assert resp.status_code == 400
        assert resp.description == json.dumps(
            {"success": False, "message": "Missing field"}, ensure_ascii=False
        )

    def test_not_found_shape(self):
        resp = not_found("Run 'x' not found")
        assert resp.status_code == 404
        assert json.loads(resp.description) == {"success": False, "message": "Run 'x' not found"}


class TestReadBody:
    def test_dict_body_passes_through(self):
        body = {"task": "x"}
        assert read_body(_FakeRequest(body)) == body

    def test_invalid_json_returns_none(self):
        assert read_body(_FakeRequest(raises=True)) is None

    def test_non_dict_body_returns_none(self):
        assert read_body(_FakeRequest([1, 2])) is None
        assert read_body(_FakeRequest("str")) is None


class TestSiteAliases:
    def test_cron_keeps_private_names(self):
        from server.trigger.http import cron

        assert cron._to_text_response is to_text_response
        assert cron._ok is ok
        assert cron._bad_request is bad_request
        assert cron._not_found is not_found
        assert cron._read_body is read_body

    def test_subagent_keeps_private_names(self):
        from server.trigger.http import subagent

        assert subagent._to_text_response is to_text_response
        assert subagent._ok is ok
        assert subagent._bad_request is bad_request
        assert subagent._not_found is not_found
        assert subagent._read_body is read_body

    def test_skills_json_response_delegates(self):
        from server.trigger.http import skills

        resp = skills._json_response(400, {"success": False, "message": "m"})
        assert resp.status_code == 400
        assert json.loads(resp.description) == {"success": False, "message": "m"}
