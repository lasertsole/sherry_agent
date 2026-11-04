"""Slash-command discovery for the composer.

``GET /commands?session_id=…`` returns the user- and workspace-scoped Markdown
commands. They are prompt templates only — no shell expansion is performed.
"""

from server.trigger.core import app

from server.service.custom_commands_service import list_custom_commands


@app.get("/commands")
async def list_commands_handler(request):
    session_id = str(request.query_params.get("session_id", "") or "")
    return {"commands": list_custom_commands(session_id)}
