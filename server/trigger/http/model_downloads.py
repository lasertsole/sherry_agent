"""HTTP surface for local-model prefetch progress (模型配置 local entries)."""

from server.service import model_downloads_service as service
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, read_body


@app.get("/model-downloads/status")
async def model_downloads_status_handler(request) -> dict:
    """Byte/file progress for every local model family."""
    return service.status()


@app.post("/model-downloads/download")
async def model_downloads_start_handler(request):
    """Start one family's prefetch; body ``{"family": "EMBEDDING"}``."""
    body = read_body(request) or {}
    family = str(body.get("family") or "").strip()
    if not family:
        return bad_request("family is required")
    try:
        return service.start(family)
    except KeyError as exc:
        return bad_request(str(exc))
