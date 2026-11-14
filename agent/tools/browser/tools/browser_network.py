"""``browser_network`` — read failed requests captured on the page."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._shared import MAIN_ONLY, BrowserPageInput, BrowserTool, guard, page_line, resolve_manager


class NetworkInput(BrowserPageInput):
    """Arguments of ``browser_network``."""

    min_status: int = Field(
        400, ge=0, le=599, description="Lowest HTTP status to report (400 = errors only)."
    )
    include_failures: bool = Field(
        True, description="Include requests that never completed (DNS/TLS/abort)."
    )
    limit: int = Field(100, ge=1, le=200, description="Newest requests to return.")


class BrowserNetworkTool(BrowserTool):
    name: str = "browser_network"
    description: str = (
        "Read the failed network requests captured on the session's page since "
        "it was opened or last navigated: HTTP status >= 400 plus requests that "
        "never completed (DNS, TLS, refused, aborted). Only url / status / "
        "failure reason are recorded — never headers or bodies. Newest last."
    )
    args_schema: type[BaseModel] = NetworkInput
    metadata: dict = MAIN_ONLY

    async def _arun(
        self,
        min_status: int = 400,
        include_failures: bool = True,
        limit: int = 100,
        page: str | None = None,
        session_id: str = "",
    ) -> str:
        async def operation() -> str:
            manager = resolve_manager()
            outcome = manager.network_events(
                session_id,
                page,
                min_status=min_status,
                include_failures=include_failures,
                limit=limit,
            )
            if not outcome.get("enabled"):
                return (
                    "browser network capture is disabled "
                    "(SHERRY_BROWSER_DEBUG_CAPTURE=0); navigate after enabling it."
                )
            requests = outcome.get("requests") or []
            header = (
                f"{page_line(outcome)} — network: {len(requests)} of "
                f"{outcome.get('buffered', 0)} buffered failure(s)"
            )
            if not requests:
                return f"{header}\n(no failed requests captured)"
            lines = [header]
            for request in requests:
                method = request.get("method") or "GET"
                url = request.get("url") or ""
                if request.get("kind") == "failure":
                    detail = str(request.get("error") or "request failed")
                    blocked = request.get("blocked_reason")
                    if blocked:
                        detail += f" (blocked: {blocked})"
                    if request.get("canceled"):
                        detail += " (canceled)"
                    lines.append(f"[failed] {method} {url} — {detail}")
                else:
                    resource = request.get("resource_type") or ""
                    lines.append(
                        f"[{request.get('status')}] {method} {url}"
                        + (f" ({resource})" if resource else "")
                    )
            return "\n".join(lines)

        return await guard(operation)
