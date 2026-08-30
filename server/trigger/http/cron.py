"""Robyn HTTP endpoints wrapping the cron scheduled-task service.

The backend cron engine is an in-process Python singleton (`cron_service`,
defined in `skills/builtin/core/cron/scripts/base.py`). These routes expose a
REST API so the Tauri/Nuxt client can list, add, edit, enable/disable, run, and
remove scheduled jobs.

Serialization note: the `CronJob` dataclass persists camelCase field names
(`atMs`, `everyMs`, `nextRunAtMs`, ...) in `cron_jobs.json` and exposes the
same shape to `_load_store`. The routes below rebuild each job via the same
camelCase mapping so the client receives a consistent, JSON-safe payload.
"""

import json

from loguru import logger
from robyn import Response

from server.trigger.core import app
from skills.builtin.core.cron.scripts import cron_service, CronSchedule


# =============================================================================
# Serialization helpers
# =============================================================================


def _to_text_response(status_code: int, payload: dict) -> Response:
    """Build a JSON Robyn Response."""
    return Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload, ensure_ascii=False),
    )


def _ok(payload: dict) -> Response:
    return _to_text_response(200, payload)


def _bad_request(message: str) -> Response:
    return _to_text_response(400, {"success": False, "message": message})


def _not_found(message: str) -> Response:
    return _to_text_response(404, {"success": False, "message": message})


def _job_to_dict(job) -> dict:
    """Convert a `CronJob` dataclass into a camelCase JSON-safe dict."""
    sd = job.schedule
    pd = job.payload
    st = job.state
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": sd.kind,
            "atMs": sd.at_ms,
            "everyMs": sd.every_ms,
            "expr": sd.expr,
            "tz": sd.tz,
        },
        "payload": {
            "kind": pd.kind,
            "message": pd.message,
            "deliver": pd.deliver,
            "channel": pd.channel,
            "to": pd.to,
        },
        "state": {
            "nextRunAtMs": st.next_run_at_ms,
            "lastRunAtMs": st.last_run_at_ms,
            "lastStatus": st.last_status,
            "lastError": st.last_error,
        },
        "createdAtMs": job.created_at_ms,
        "updatedAtMs": job.updated_at_ms,
        "deleteAfterRun": job.delete_after_run,
    }


def _valid_schedule(body: dict) -> CronSchedule | None:
    """Extract and validate the schedule payload, or fall back to defaults.

    Returns a `CronSchedule` on success or `None` on invalid input.
    """
    raw = body.get("schedule") or body
    kind = raw.get("kind", "every")

    if kind not in ("at", "every", "cron"):
        return None

    at_ms = raw.get("atMs")
    every_ms = raw.get("everyMs")
    expr = raw.get("expr")
    tz = raw.get("tz")

    if kind == "at" and at_ms is None:
        return None
    if kind == "every" and not every_ms:
        return None
    if kind == "cron" and not expr:
        return None

    return CronSchedule(
        kind=kind,
        at_ms=int(at_ms) if at_ms is not None else None,
        every_ms=int(every_ms) if every_ms is not None else None,
        expr=expr,
        tz=tz,
    )


def _read_body(request) -> dict | None:
    """Parse a JSON request body defensively."""
    try:
        body = request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


# =============================================================================
# Endpoints
# =============================================================================


@app.get("/cron")
async def list_cron_jobs_handler(request):
    """List cron jobs.

    Optional query param `include_disabled` (default false) includes jobs whose
    `enabled` flag is false. When omitted, only enabled jobs are returned.
    """
    include_raw = request.query_params.get("include_disabled", "false")
    include_disabled = str(include_raw).lower() in ("1", "true", "yes")

    jobs = cron_service.list_jobs(include_disabled=include_disabled)
    result = [_job_to_dict(j) for j in jobs]
    logger.debug(f"Listed cron jobs: count={len(result)}, include_disabled={include_disabled}")
    return _ok({"jobs": result})


@app.post("/cron")
async def add_cron_job_handler(request):
    """Create a new cron job.

    Body: {
      "name": str (required),
      "message": str (required),
      "schedule": {"kind": "at"|"every"|"cron", <fields>},
      "deliver": bool (optional),
      "channel": str | null (optional),
      "to": str | null (optional),
      "delete_after_run": bool (optional)
    }
    """
    body = _read_body(request)
    if body is None:
        logger.warning("Cron add rejected: invalid JSON body")
        return _bad_request("Invalid JSON body")

    name = body.get("name")
    message = body.get("message")
    if not name or not isinstance(name, str) or not name.strip():
        logger.warning("Cron add rejected: missing name")
        return _bad_request("Missing or invalid 'name'")
    if not message or not isinstance(message, str) or not message.strip():
        logger.warning("Cron add rejected: missing message")
        return _bad_request("Missing or invalid 'message'")

    schedule = _valid_schedule(body)
    if schedule is None:
        logger.warning("Cron add rejected: invalid schedule")
        return _bad_request("Invalid schedule; must provide a valid kind/fields")

    try:
        job = cron_service.add_job(
            name=name.strip(),
            schedule=schedule,
            message=message.strip(),
            deliver=bool(body.get("deliver", False)),
            channel=body.get("channel"),
            to=body.get("to"),
            delete_after_run=bool(body.get("delete_after_run", False)),
        )
    except ValueError as e:
        logger.warning("Cron add rejected: %s", e)
        return _bad_request(str(e))
    except Exception as e:
        logger.exception("Cron add failed: name=%s (%s)", name, e)
        return _to_text_response(500, {"success": False, "message": str(e)})

    logger.info(f"Cron job created: name={job.name}, id={job.id}")
    return _ok({"success": True, "job": _job_to_dict(job)})


@app.put("/cron")
async def update_cron_job_handler(request):
    """Update an existing cron job.

    Body: {
      "id": str (required),
      "name": str (optional),
      "message": str (optional),
      "schedule": { ... } (optional),
      "deliver": bool (optional),
      "channel": str | null (optional),
      "to": str | null (optional),
      "delete_after_run": bool (optional)
    }
    Because the cron engine only exposes one-shot operations (add/enable/remove),
    updates are applied by removing the existing job and re-adding it with the
    merged fields while preserving its id and created timestamp.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    job_id = body.get("id")
    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        return _bad_request("Missing or invalid 'id'")

    existing = cron_service.get_job(job_id)
    if existing is None:
        return _not_found(f"Cron job '{job_id}' not found")

    name = body.get("name", existing.name)
    if not name or not isinstance(name, str) or not name.strip():
        return _bad_request("Missing or invalid 'name'")

    message = body.get("message", existing.payload.message)
    if not message or not isinstance(message, str) or not message.strip():
        return _bad_request("Missing or invalid 'message'")

    if "schedule" in body:
        schedule = _valid_schedule(body)
        if schedule is None:
            return _bad_request("Invalid schedule; must provide a valid kind/fields")
    else:
        schedule = existing.schedule

    # Remove then re-add to get the engine to recompute state and persist.
    removed = cron_service.remove_job(job_id)
    if removed != "removed":
        # "protected" or "not_found" — roll back gracefully.
        return _to_text_response(
            400, {"success": False, "message": f"Cannot update job '{job_id}' ({removed})"}
        )

    try:
        job = cron_service.add_job(
            name=name.strip(),
            schedule=schedule,
            message=message.strip(),
            deliver=body.get("deliver", existing.payload.deliver),
            channel=body.get("channel", existing.payload.channel),
            to=body.get("to", existing.payload.to),
            delete_after_run=body.get("delete_after_run", existing.delete_after_run),
        )
    except ValueError as e:
        logger.warning("Cron update rejected: %s", e)
        return _bad_request(str(e))
    except Exception as e:
        logger.exception("Cron update failed: id=%s (%s)", job_id, e)
        return _to_text_response(500, {"success": False, "message": str(e)})

    # add_job assigns a fresh id; restore the original one for a seamless update.
    job.id = job_id
    logger.info(f"Cron job updated: id={job_id}, name={job.name}")
    return _ok({"success": True, "job": _job_to_dict(job)})


@app.post("/cron/trigger")
async def run_cron_job_handler(request):
    """Manually trigger a cron job now.

    Body: {"id": str, "force": bool (optional)}. When `force` is false (default)
    and the job is disabled, the run is skipped and `{success: false}` returned.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    job_id = body.get("id")
    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        return _bad_request("Missing or invalid 'id'")

    existing = cron_service.get_job(job_id)
    if existing is None:
        return _not_found(f"Cron job '{job_id}' not found")

    force = bool(body.get("force", False))
    ok = await cron_service.run_job(job_id, force=force)
    logger.info(f"Cron job run: id={job_id}, force={force}, ok={ok}")
    if not ok:
        return _to_text_response(
            400, {"success": False, "message": "Job is disabled; pass force=true to override"}
        )
    return _ok({"success": True})


@app.post("/cron/enable")
async def enable_cron_job_handler(request):
    """Enable or disable a cron job.

    Body: {"id": str, "enabled": bool (default true)}.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    job_id = body.get("id")
    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        return _bad_request("Missing or invalid 'id'")

    enabled = bool(body.get("enabled", True))
    job = cron_service.enable_job(job_id, enabled=enabled)
    if job is None:
        return _not_found(f"Cron job '{job_id}' not found")

    logger.info(f"Cron job enable: id={job_id}, enabled={enabled}")
    return _ok({"success": True, "job": _job_to_dict(job)})


@app.delete("/cron")
async def delete_cron_job_handler(request):
    """Remove a cron job.

    Body: {"id": str (required)}.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    job_id = body.get("id")
    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        return _bad_request("Missing or invalid 'id'")

    result = cron_service.remove_job(job_id)
    if result == "removed":
        logger.info(f"Cron job removed: id={job_id}")
        return _ok({"success": True})
    if result == "protected":
        return _to_text_response(
            403, {"success": False, "message": "Job is protected and cannot be removed"}
        )
    return _not_found(f"Cron job '{job_id}' not found")
