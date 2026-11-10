"""Serve content-addressed media artifacts (``sherry-artifact://`` references).

The store writes ``<SRC_DIR>/<session_id>/artifacts/<family>-<sha256-prefix><ext>``
(see ``agent/tools/pub_base/artifact_store.py``). The session id is part of the
path, so a message can only resolve artifacts of its own session; an unknown
name or a malformed one is refused before any disk access.
"""

from loguru import logger
from robyn import Response

from agent.tools.pub_base.artifact_store import artifact_path, is_valid_artifact_name
from config import SRC_DIR
from pub.func.validator import is_safe_session_id
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found
from server.trigger.http.media import CONTENT_TYPES


@app.get("/artifacts/:session_id/:name")
async def get_artifact(request, path_params):
    """Bytes of one stored artifact (Content-Type from its extension)."""
    session_id = str(path_params.get("session_id") or "")
    name = str(path_params.get("name") or "")
    if not session_id or not is_safe_session_id(session_id):
        return bad_request("invalid session_id")
    if not is_valid_artifact_name(name):
        return bad_request("invalid artifact name")

    path = artifact_path(session_id, name, root=SRC_DIR)
    if path is None:
        return not_found("artifact not found")

    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("artifact read failed: {}: {}", path, exc)
        return not_found("artifact not found")
    content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return Response(status_code=200, headers={"Content-Type": content_type}, description=data)
