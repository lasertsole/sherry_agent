"""Shared plumbing for the browser tool family (internal module).

Every verb is a thin shell over the process-wide ``BrowserManager``: the
manager is resolved through ``runtime/hooks.BROWSER_MANAGER`` (``agent/**``
must not import ``server/**``) and the session arrives through
``InjectedState``. All bounds (page caps, navigation/op timeouts,
element/text/screenshot limits) live in ``BROWSER_AGENT`` and are enforced by
the manager, not here.

Error-text contract: a tool NEVER raises a business error to the LLM — the
``guard`` wrapper turns expected failures into readable ``Error: …`` text.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import InjectedState

#: The session id every browser tool takes from graph state.
SessionId = Annotated[str, InjectedState("session_id")]

#: The subagent tool policy drops these unconditionally (see the family
#: docstring: ZCode parity, the browser stays the main agent's hand).
MAIN_ONLY = {"scope": "main_only"}


def resolve_manager() -> Any:
    """The process-wide manager, via the hook registry.

    :raises RuntimeError: no server assembly registered the bridge.
    """
    from runtime import hooks

    resolver = hooks.resolve(hooks.BROWSER_MANAGER)
    if resolver is None:
        raise RuntimeError(
            "the browser bridge is not assembled in this process — start the "
            "server to use the browser tools"
        )
    return resolver()


async def guard(operation) -> Any:
    """Run one manager call, turning expected failures into tool-result text.

    ``RuntimeError`` also covers the server's ``CdpError`` (a ``RuntimeError``
    subclass) — the class itself cannot be imported here: ``agent/**`` must not
    import ``server/**``.

    The value is whatever the operation returned: an error string, a success
    string, or (``browser_screenshot``) a text + image content list.
    """
    try:
        return await operation()
    except KeyError as error:
        return f"Error: {error.args[0] if error.args else error}"
    except (ValueError, PermissionError, RuntimeError) as error:
        return f"Error: {error}"


def page_line(info: dict[str, Any]) -> str:
    """``page p1 · «Title» · https://…`` — the one-line page reference."""
    title = info.get("title") or "(no title)"
    return f"page {info.get('page')} · «{title}» · {info.get('url', '')}"


class BrowserTool(BaseTool):
    """Shared base: every verb is async-only.

    The manager is loop-bound (locks + one WebSocket on the server loop), so a
    sync call must not quietly run on another loop — it fails with a message
    that says where to call from instead. The agent graph invokes tools through
    ``ainvoke``, which uses the overridden ``_arun`` and never touches this.
    """

    def _run(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("browser tools are asynchronous — invoke them through the agent graph")
