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


import pytest

pytestmark = [pytest.mark.unit]


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


def test_valid_schedule_rejects_every_ms_below_floor():
    assert cron_api._valid_schedule({"kind": "every", "everyMs": 999}) is None


def test_valid_schedule_accepts_every_ms_at_floor():
    schedule = cron_api._valid_schedule({"kind": "every", "everyMs": 1000})

    assert schedule is not None
    assert schedule.every_ms == 1000


def test_valid_schedule_rejects_every_ms_below_floor_as_string():
    assert cron_api._valid_schedule({"kind": "every", "everyMs": "1"}) is None


# ---------------------------------------------------------------------------
# _valid_skills — the `skills` body field contract
# ---------------------------------------------------------------------------


def test_valid_skills_none_and_empty_mean_no_binding():
    assert cron_api._valid_skills(None) is None
    assert cron_api._valid_skills([]) is None
    assert cron_api._valid_skills(["  ", ""]) is None


def test_valid_skills_strips_and_dedups_keeping_order():
    assert cron_api._valid_skills([" b ", "a", "b"]) == ["b", "a"]


def test_valid_skills_rejects_wrong_shapes():
    with pytest.raises(ValueError):
        cron_api._valid_skills("cron")
    with pytest.raises(ValueError):
        cron_api._valid_skills([1, 2])


# ---------------------------------------------------------------------------
# POST /cron + PUT /cron — skills plumbing
# ---------------------------------------------------------------------------


class _SkillsStubService:
    """Stub covering the add/get/remove surface the POST/PUT handlers use."""

    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.added: list[dict] = []
        self.removed: list[str] = []

    def get_job(self, job_id):
        return self.existing

    def list_jobs(self, include_disabled=False):
        return []

    def remove_job(self, job_id):
        self.removed.append(job_id)
        return "removed"

    def add_job(self, **kwargs):
        from skills.builtin.core.cron.scripts.types import CronJob, CronPayload

        self.added.append(kwargs)
        return CronJob(
            id="job-new",
            name=kwargs["name"],
            schedule=kwargs["schedule"],
            payload=CronPayload(
                message=kwargs["message"],
                deliver=kwargs.get("deliver", False),
                channel=kwargs.get("channel"),
                to=kwargs.get("to"),
                skills=kwargs.get("skills"),
            ),
            delete_after_run=kwargs.get("delete_after_run", False),
        )


def _existing_job(skills=None):
    from skills.builtin.core.cron.scripts.types import CronJob, CronPayload, CronSchedule

    return CronJob(
        id="job-1",
        name="existing",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="m", deliver=False, skills=skills),
    )


def test_post_cron_passes_validated_skills(monkeypatch):
    stub = _SkillsStubService()
    monkeypatch.setattr(cron_api, "cron_service", stub)

    resp = _call(
        cron_api.add_cron_job_handler,
        {
            "name": "n",
            "message": "m",
            "schedule": {"kind": "every", "everyMs": 60000},
            "skills": [" beta ", "alpha", "beta"],
        },
    )

    assert resp.status_code == 200
    assert stub.added[0]["skills"] == ["beta", "alpha"]
    assert _payload(resp)["job"]["payload"]["skills"] == ["beta", "alpha"]


def test_post_cron_without_skills_binds_nothing(monkeypatch):
    stub = _SkillsStubService()
    monkeypatch.setattr(cron_api, "cron_service", stub)

    _call(
        cron_api.add_cron_job_handler,
        {"name": "n", "message": "m", "schedule": {"kind": "every", "everyMs": 60000}},
    )

    assert stub.added[0]["skills"] is None
    assert stub.added[0].keys() >= {"skills"}, "the field is always passed (None = no binding)"


def test_post_cron_rejects_malformed_skills(monkeypatch):
    stub = _SkillsStubService()
    monkeypatch.setattr(cron_api, "cron_service", stub)

    resp = _call(
        cron_api.add_cron_job_handler,
        {
            "name": "n",
            "message": "m",
            "schedule": {"kind": "every", "everyMs": 60000},
            "skills": "not-a-list",
        },
    )

    assert resp.status_code == 400
    assert stub.added == [], "a malformed binding must not create the job silently"


def test_put_cron_keeps_existing_skills_when_omitted(monkeypatch):
    stub = _SkillsStubService(existing=_existing_job(skills=["x", "y"]))
    monkeypatch.setattr(cron_api, "cron_service", stub)

    resp = _call(cron_api.update_cron_job_handler, {"id": "job-1", "name": "renamed"})

    assert resp.status_code == 200
    assert stub.added[0]["skills"] == ["x", "y"], "omitting the field preserves the binding"


def test_put_cron_clears_skills_on_explicit_null(monkeypatch):
    stub = _SkillsStubService(existing=_existing_job(skills=["x"]))
    monkeypatch.setattr(cron_api, "cron_service", stub)

    resp = _call(cron_api.update_cron_job_handler, {"id": "job-1", "skills": None})

    assert resp.status_code == 200
    assert stub.added[0]["skills"] is None, "explicit null clears the binding"
    assert _payload(resp)["job"]["payload"]["skills"] is None


def test_put_cron_replaces_skills_with_a_new_list(monkeypatch):
    stub = _SkillsStubService(existing=_existing_job(skills=["old"]))
    monkeypatch.setattr(cron_api, "cron_service", stub)

    _call(cron_api.update_cron_job_handler, {"id": "job-1", "skills": ["new", "old"]})

    assert stub.added[0]["skills"] == ["new", "old"]
