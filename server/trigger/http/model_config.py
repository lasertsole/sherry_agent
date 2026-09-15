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
