"""Transport adapter for the panel API (prototype ``src/app/http.ts``).

No Robyn import lives here: the superagent process may be embedded in any
ASGI host.  ``PanelHTTP.serve`` is the minimal callable a host can mount.
"""

from __future__ import annotations

from typing import Any

from superagent.panel.api import PanelAPI


class PanelHTTP:
    """Request adapter around :class:`PanelAPI`."""

    def __init__(self, api: PanelAPI) -> None:
        self.api = api

    async def serve(
        self,
        method: str,
        path: str,
        *,
        raw_body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        host: str = "",
        origin: str = "",
        port: int = 3082,
    ) -> tuple[int, dict[str, Any]]:
        """Handle one request; invalid JSON bodies return 400."""
        body = self.api.parse_body(raw_body)
        if body is None:
            return 400, {"success": False, "message": "invalid JSON body"}
        return self.api.handle(
            method, path, body=body, headers=headers, host=host, origin=origin, port=port
        )


__all__ = ["PanelHTTP"]
