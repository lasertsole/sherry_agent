"""Lightweight endpoint exposing current MAX_TOKEN config and validity."""

import os

from config.features import LLM_CLIENT_DEFAULTS, MIN_REQUIRED_MAX_TOKEN
from server.trigger.core import app


@app.get("/model-config")
async def model_config_handler(request) -> dict:
    """Return current MAX_TOKEN values, the minimum threshold, and validity flag.

    Response shape:
        {
            "main_max_token": int | null,
            "aux_max_token": int,
            "min_required": int,
            "valid": bool,
        }
    """
    main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
    aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
    main_val = int(main_raw) if main_raw else None
    aux_val = int(aux_raw) if aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
    return {
        "main_max_token": main_val,
        "aux_max_token": aux_val,
        "min_required": MIN_REQUIRED_MAX_TOKEN,
        "valid": (
            main_val is not None
            and main_val >= MIN_REQUIRED_MAX_TOKEN
            and aux_val >= MIN_REQUIRED_MAX_TOKEN
        ),
    }


@app.post("/model/test")
async def model_test_handler(request):
    """Probe one env-config model group's endpoint with the caller's parameters.

    Body: ``{"group": "MAIN_LLM", "params": {"MAIN_LLM_PROVIDER": "", ...}}`` —
    the panel's own draft, so a profile can be verified before it is applied.

    Response: ``{success: true, supported, ok, latency_ms, detail, model}``.
    A refused probe (the provider said no, or nothing answered in time) is a
    200 with ``ok: false`` and the reason in ``detail``: that IS the diagnosis.
    400 only for a malformed request (missing group / params).
    """
    from server.service.model_test_service import test_model_group
    from server.trigger.http.helpers import bad_request, ok, read_body

    body = read_body(request) or {}
    group = str(body.get("group") or "").strip()
    params = body.get("params")
    if not group:
        return bad_request("group is required")
    if not isinstance(params, dict):
        return bad_request("params must be an object")
    result = await test_model_group(group, {str(k): str(v) for k, v in params.items()})
    return ok({**result, "group": group})
