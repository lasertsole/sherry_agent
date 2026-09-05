"""Function-level tests for the cron REST endpoints (Task 10).

Covers `POST /cron/failure-state` and `POST /cron/reset-failures` in
`server/trigger/http/cron.py`. No real server is started: the handlers are
plain async functions invoked directly with a fabricated request exposing
only the attribute the handlers read (`json()`), and the module-level
`cron_service` binding is monkeypatched with a stub. pytest's monkeypatch
fixture guarantees every patch is undone after each test.
"""

import asyncio
import json

import server.trigger.http.cron as cron_api

JOB_ID = "job-1"


class _FakeRequest:
    """Minimal request stand-in: handlers only call ``request.json()``."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


class _StubCronService:
    """Stub exposing exactly the CronService API the new handlers consume."""

    def __init__(self, job=object(), failure_state=None, reset_ok=True):
        self.job = job
        self.failure_state = failure_state
        self.reset_ok = reset_ok
        self.calls = []

    def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return self.job

    def get_failure_state(self, job_id):
        self.calls.append(("get_failure_state", job_id))
        return self.failure_state

    def reset_failures(self, job_id):
        self.calls.append(("reset_failures", job_id))
        return self.reset_ok


def _call(handler, body):
    return asyncio.run(handler(_FakeRequest(body)))


def _payload(response):
    return json.loads(response.description)


def _patch_service(monkeypatch, **kwargs):
    stub = _StubCronService(**kwargs)
    monkeypatch.setattr(cron_api, "cron_service", stub)
    return stub


# ---------------------------------------------------------------------------
# POST /cron/failure-state
# ---------------------------------------------------------------------------


def test_failure_state_returns_tracked_state(monkeypatch):
    _patch_service(
        monkeypatch,
        failure_state={
            "consecutive_failures": 3,
            "last_error": "boom",
            "degraded_since": 123456,
            "backoff_ms": 480000,
        },
    )
    resp = _call(cron_api.get_failure_state_handler, {"id": JOB_ID})

    assert resp.status_code == 200
    payload = _payload(resp)
    assert payload["success"] is True
    assert payload["job_id"] == JOB_ID
    assert payload["consecutive_failures"] == 3
    assert payload["last_error"] == "boom"


def test_failure_state_unknown_job_not_found(monkeypatch):
    _patch_service(monkeypatch, job=None)
    resp = _call(cron_api.get_failure_state_handler, {"id": "missing"})

    assert resp.status_code == 404
    assert _payload(resp)["success"] is False


def test_failure_state_zeroed_when_no_records(monkeypatch):
    # Job exists but has never failed: zeroed view, NOT a 404.
    _patch_service(monkeypatch, failure_state=None)
    resp = _call(cron_api.get_failure_state_handler, {"id": JOB_ID})

    assert resp.status_code == 200
    payload = _payload(resp)
    assert payload["success"] is True
    assert payload["consecutive_failures"] == 0
    assert payload["backoff_ms"] == 0
    assert payload["last_error"] is None


def test_failure_state_missing_id_bad_request(monkeypatch):
    _patch_service(monkeypatch)
    resp = _call(cron_api.get_failure_state_handler, {})

    assert resp.status_code == 400


def test_failure_state_invalid_json_bad_request(monkeypatch):
    _patch_service(monkeypatch)
    resp = _call(cron_api.get_failure_state_handler, None)

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /cron/reset-failures
# ---------------------------------------------------------------------------


def test_reset_failures_ok(monkeypatch):
    _patch_service(monkeypatch, reset_ok=True)
    resp = _call(cron_api.reset_failures_handler, {"id": JOB_ID})

    assert resp.status_code == 200
    payload = _payload(resp)
    assert payload["reset"] is True
    assert payload["job_id"] == JOB_ID


def test_reset_failures_unknown_id_not_found(monkeypatch):
    _patch_service(monkeypatch, reset_ok=False)
    resp = _call(cron_api.reset_failures_handler, {"id": "missing"})

    assert resp.status_code == 404
    assert _payload(resp)["success"] is False


def test_reset_failures_missing_id_bad_request(monkeypatch):
    _patch_service(monkeypatch)
    resp = _call(cron_api.reset_failures_handler, {})

    assert resp.status_code == 400
