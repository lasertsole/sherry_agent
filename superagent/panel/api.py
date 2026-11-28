"""Typed panel API (prototype ``src/app/api.ts``).

The API is transport-agnostic: ``PanelAPI.handle`` takes already-parsed
inputs and returns ``(status, payload)``.  ``panel.http`` adapts it to Robyn.
"""

from __future__ import annotations

import json
from typing import Any

from superagent.app.prompts import Prompts
from superagent.app.settings import SettingsStore
from superagent.core.errors import UserError, http_status
from superagent.infra.health import HealthBoard
from superagent.panel.views import overview, settings_page, source_state

MAX_BODY_BYTES = 1_048_576
GUARD_HEADER = "x-orchestrator-request"


class PanelAPI:
    """All management routes of the superagent process."""

    def __init__(
        self,
        *,
        engine: Any,
        settings: SettingsStore,
        prompts: Prompts,
        board: HealthBoard,
        registry: Any,
        executor: Any = None,
        setup: Any = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.prompts = prompts
        self.board = board
        self.registry = registry
        self.executor = executor
        self.setup = setup

    @staticmethod
    def guard(
        headers: dict[str, str] | None,
        *,
        host: str = "",
        origin: str = "",
        port: int = 3082,
    ) -> str | None:
        """Return an error message when the loopback guard fails."""
        values = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        if values.get(GUARD_HEADER) != "1":
            return "missing orchestrator header"
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host and host not in allowed_hosts:
            return "host is not loopback"
        if origin and "localhost" not in origin and "127.0.0.1" not in origin:
            return "cross-origin request"
        return None

    @staticmethod
    def parse_body(raw: str | bytes | None) -> dict[str, Any] | None:
        """Parse a bounded JSON object body."""
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            if len(raw) > MAX_BODY_BYTES:
                return None
            raw = raw.decode("utf-8", "replace")
        if len(raw) > MAX_BODY_BYTES:
            return None
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def handle(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        host: str = "",
        origin: str = "",
        port: int = 3082,
    ) -> tuple[int, dict[str, Any]]:
        """Dispatch one panel request through the loopback guard."""
        error = self.guard(headers, host=host, origin=origin, port=port)
        if error is not None:
            return 403, {"success": False, "message": error}
        try:
            return self._route(method.upper(), path, body or {})
        except UserError as exc:
            return http_status(exc.code), {
                "success": False,
                "code": exc.code,
                "values": exc.values,
            }
        except KeyError:
            return 404, {"success": False, "message": "not found"}

    def _route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/state":
            return 200, self._state()
        if method == "GET" and path == "/settings":
            return 200, self._settings()
        if method == "GET" and path == "/sources":
            return 200, self._sources()
        if method == "GET" and path == "/prompts":
            return 200, self.prompts.current()
        if method == "PUT" and path.startswith("/settings/"):
            return 200, self._save_settings(path.removeprefix("/settings/"), body)
        if method == "PUT" and path == "/prompts":
            return 200, {
                "revision": self.prompts.save(
                    dict(body.get("config", {})),
                    int(body.get("revision", 0)),
                    body.get("baseRevision", body.get("revision")),
                )
            }
        if method == "POST" and path == "/executor/pause":
            paused = bool(body.get("paused", True))
            self.settings.save("paused", paused, self.settings.get("paused").revision)
            return 200, {"paused": paused}
        if method == "POST" and path == "/setup/check":
            return 200, {"restarting": True}
        if method == "POST" and path.startswith("/policy-sync/"):
            source_id = path.removeprefix("/policy-sync/")
            return 200, {"source": source_id, "policy": self.settings.get("policy").value}
        if method == "POST" and path.endswith("/retry"):
            return 200, self._ticket_action(path, "retry_ticket")
        if method == "POST" and path.endswith("/stop"):
            return 200, self._ticket_action(path, "stop_ticket")
        return 404, {"success": False, "message": "not found"}

    def _ticket_action(self, path: str, name: str) -> dict[str, Any]:
        ticket_id = path.removeprefix("/tickets/").removesuffix(f"/{name.removesuffix('_ticket')}")
        getattr(self.engine, name)(ticket_id)
        return {"ticket_id": ticket_id, "action": name}

    def _state(self) -> dict[str, Any]:
        tickets = list(self.engine.repo.tickets.list())
        load = {
            "running": sum(1 for ticket in tickets if ticket.latest_assignment_status == "running"),
            "queued": sum(1 for ticket in tickets if ticket.has_undelivered_input),
            "maxWorkers": getattr(getattr(self.executor, "slots", None), "max", 1),
        }
        setup = self.setup.__dict__ if self.setup is not None else {"ok": True}
        return {
            **overview(tickets, self.board.all(), load, setup),
            "paused": bool(self.settings.get("paused").value),
            "masterId": "superagent-main",
        }

    def _settings(self) -> dict[str, Any]:
        result = settings_page(self.settings.snapshot(), self.prompts.current())
        result["sources"] = [
            {
                "id": item.id,
                "kind": item.kind,
                "enabled": item.enabled,
                "config": item.config,
            }
            for item in self.registry.instances()
        ]
        return result

    def _sources(self) -> dict[str, Any]:
        health = self.board.all()
        return {
            "sources": [source_state(item, health) for item in self.registry.instances()],
            "filtered": [],
            "dropped": [],
        }

    def _save_settings(self, block: str, body: dict[str, Any]) -> dict[str, Any]:
        saved = self.settings.save(block, body.get("value", {}), int(body.get("revision", 0)))
        if block == "policy":
            self.engine.policy_changed(None, saved.value)
        return {"saved": True, "revision": saved.revision, "restarting": False}


__all__ = ["GUARD_HEADER", "MAX_BODY_BYTES", "PanelAPI"]
