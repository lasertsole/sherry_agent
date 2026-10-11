"""``web_search`` — one tool, two interchangeable backends.

Which backend serves a call is resolved per call:

1. the session's explicit choice (``StateKey.SEARCH_BACKEND``, set from the
   客户端 模型联网 panel through ``PUT /sessions/search_backend``);
2. else the global default (``WEB_SEARCH_CONFIG["default_backend"]``);
3. else availability: a default whose provider is not configured switches to the
   other one when THAT is configured. Only "neither is configured" leaves the
   tool unavailable, which is what the stub result says.

Tavily is the richer backend, so it keeps its bounded retry. A Tavily call that
still fails falls back to SearXNG when an instance is configured — and the
session's websocket gets a ``search_backend_fallback`` frame so the client can
say so. SearXNG itself never falls back to Tavily (self-hosted deployments may
have no Tavily key at all), which also keeps the pair from ping-ponging.
"""

import os
import asyncio
import random
from typing import Annotated
from loguru import logger
from config import ENV_PATH
from config.features import TOOLS_TIMEOUTS
from dotenv import load_dotenv
from langgraph.prebuilt.tool_node import InjectedState
from pydantic import BaseModel, Field

from agent.tools.web_search_push import push_search_fallback
from agent.tools.web_search_searxng import searxng_available, searxng_search

# Bound to the feature registry (single source of truth); names preserved.
WEB_SEARCH_TIMEOUT = TOOLS_TIMEOUTS["web_search_timeout_seconds"]
RETRY_BACKOFF_MIN = TOOLS_TIMEOUTS["web_search_retry_backoff_min_s"]
RETRY_BACKOFF_MAX = TOOLS_TIMEOUTS["web_search_retry_backoff_max_s"]
RETRY_MAX_ATTEMPTS = TOOLS_TIMEOUTS["web_search_retry_max_attempts"]

#: Substrings that identify a dead/invalid Tavily key: retrying cannot help
#: (every attempt would be rejected the same way), so the call goes straight to
#: the fallback backend.
_AUTH_ERROR_MARKERS = ("401", "403", "unauthorized", "forbidden", "invalid api key")

#: Substrings identifying a timeout reported INSIDE an error envelope.
_TIMEOUT_MARKERS = ("timeout", "timed out")


def _tavily_api_key() -> str | None:
    """Read the Tavily key at BUILD time, never at import time.

    (``agent/core.py`` moved its own dotenv work into the assembly step for the
    same reason: importing a tool module must not mutate the process env, and a
    key edited while the process runs should be picked up on the next build.)
    """
    load_dotenv(ENV_PATH, override=True)
    # Secret-class value: TAVILY_API_KEY deliberately stays in the gitignored
    # .env (NOT in the tracked sherry.jsonc).
    return os.getenv("TAVILY_API_KEY")


def tavily_available() -> bool:
    """Whether a Tavily key is configured (no network request is made)."""
    return bool(_tavily_api_key())


class WebSearchSchema(BaseModel):
    query: str = Field(description="The search query to look up on the web.")


def _backoff_delay(attempt: int) -> float:
    delay = RETRY_BACKOFF_MIN * (2**attempt)
    jitter = random.uniform(0, delay)
    return min(delay + jitter, RETRY_BACKOFF_MAX)


def _is_auth_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _AUTH_ERROR_MARKERS)


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TIMEOUT_MARKERS)


def _failure_reason(exc: BaseException) -> str:
    """The fallback reason code for one failed attempt."""
    if _is_auth_error(exc):
        return "tavily_auth_failed"
    if _is_timeout_error(exc):
        return "tavily_timeout"
    return "tavily_error"


def _error_envelope(result: object) -> BaseException | None:
    """The exception inside a ``{"error": e}`` envelope, else ``None``.

    ``langchain_tavily`` 0.2.x does NOT let a failed request raise: its ``_arun``
    catches everything (only ``ToolException`` propagates) and RETURNS
    ``{"error": e}`` — verified against the installed 0.2.18, where an invalid
    key answered ``{'error': Exception('Error 401: Unauthorized')}``. A retry /
    fallback wrapper that only catches exceptions therefore never fires on a dead
    key, and the model is handed the raw envelope instead of search results.
    Classifying the envelope here is what makes the fallback real.
    """
    if not isinstance(result, dict):
        return None
    error = result.get("error")
    if isinstance(error, BaseException):
        return error
    if error:
        return RuntimeError(str(error))
    return None


def resolve_preferred_backend(session_id: str) -> str:
    """The backend this call should try first (see the module docstring).

    An explicit choice counts only while its provider is configured: a session
    pinned to SearXNG whose instance was removed from the .env falls back to the
    default resolution instead of answering "SearXNG is not configured".
    """
    explicit = ""
    if session_id:
        from runtime import state_register_mem
        from runtime.session.state_keys import StateKey

        explicit = str(state_register_mem.get_state(session_id, StateKey.SEARCH_BACKEND) or "")
    if explicit == "tavily" and tavily_available():
        return "tavily"
    if explicit == "searxng" and searxng_available():
        return "searxng"

    from config.features import WEB_SEARCH_CONFIG

    default = str(WEB_SEARCH_CONFIG["default_backend"] or "tavily").lower()
    if default not in ("tavily", "searxng"):
        default = "tavily"
    if default == "tavily" and not tavily_available() and searxng_available():
        return "searxng"
    if default == "searxng" and not searxng_available() and tavily_available():
        return "tavily"
    return default


def build_web_search_tool():
    """Build the ``web_search`` tool over whichever backends are configured."""
    from langchain_core.tools import tool

    if tavily_available():
        from langchain_tavily import TavilySearch

        # The input model lives in the submodule (the package re-exports only the
        # tool classes). Importing it loudly is deliberate: a rename upstream must
        # break here, not silently drop the injected session id again.
        from langchain_tavily.tavily_search import TavilySearchInput

        class WebSearchInput(TavilySearchInput):
            """Tavily's own input schema plus the repo's injected session id.

            ``TavilySearch`` is a class-based tool, so it never sees the graph
            state: without this field the tool has NO session id in production
            (LangGraph injects ``InjectedState`` fields only, and the model never
            fills one in), which silently disabled the per-session backend choice
            AND the downgrade notice. Injecting through the input schema is the
            same mechanism the ``@tool``-decorated session-aware tools use, and
            ``tool_call_schema`` strips injected arguments, so the model's view is
            unchanged.
            """

            session_id: Annotated[str, InjectedState("session_id")] = ""

        class BackendAwareTavilySearch(TavilySearch):
            """Tavily with a bounded retry, a timeout, and a SearXNG fallback.

            A SUBCLASS, not a monkeypatched private method: patching
            ``base._arun`` silently dropped the wrapper (and the metadata below)
            the day the library renamed or stopped dispatching through that name.
            Overriding it here fails loudly instead — the override is checked by
            the type system.
            """

            async def _arun(self, *args, **kwargs):
                query = str(kwargs.get("query") or (args[0] if args else ""))
                session_id = str(kwargs.get("session_id") or "")
                preferred = resolve_preferred_backend(session_id)
                if preferred == "searxng":
                    # The session (or the config) picked the self-hosted backend:
                    # it is the primary, and its failures are reported as its own.
                    logger.info("web_search: SearXNG preferred (session={})", session_id or "-")
                    return await asyncio.to_thread(searxng_search, query)

                call_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key in ("query", "max_results", "search_depth", "topic", "include_domains")
                }
                if not call_kwargs.get("query") and args:
                    call_kwargs["query"] = args[0]

                last_error = None
                fallback_reason = "tavily_error"
                for attempt in range(RETRY_MAX_ATTEMPTS):
                    try:
                        result = await asyncio.wait_for(
                            super()._arun(**call_kwargs),
                            timeout=WEB_SEARCH_TIMEOUT,
                        )
                        failure = _error_envelope(result)
                    except TimeoutError:
                        last_error = f"timed out after {WEB_SEARCH_TIMEOUT}s"
                        fallback_reason = "tavily_timeout"
                        logger.warning(
                            "web_search attempt {}/{} {}",
                            attempt + 1,
                            RETRY_MAX_ATTEMPTS,
                            last_error,
                        )
                    except Exception as exc:  # noqa: BLE001 — classified below
                        last_error = str(exc)
                        fallback_reason = _failure_reason(exc)
                        if fallback_reason == "tavily_auth_failed":
                            logger.warning(
                                "Tavily rejected the API key; falling back without retries"
                            )
                            break
                        logger.warning(
                            "web_search attempt {}/{} failed: {}",
                            attempt + 1,
                            RETRY_MAX_ATTEMPTS,
                            last_error,
                        )
                    else:
                        if failure is None:
                            return result
                        # An error ENVELOPE is a failure too (see `_error_envelope`):
                        # classify and retry it exactly like a raise, or a dead key
                        # would be answered with the envelope and never fall back.
                        last_error = str(failure)
                        fallback_reason = _failure_reason(failure)
                        if fallback_reason == "tavily_auth_failed":
                            logger.warning(
                                "Tavily rejected the API key; falling back without retries"
                            )
                            break
                        logger.warning(
                            "web_search attempt {}/{} failed: {}",
                            attempt + 1,
                            RETRY_MAX_ATTEMPTS,
                            last_error,
                        )

                    if attempt < RETRY_MAX_ATTEMPTS - 1:
                        delay = _backoff_delay(attempt)
                        logger.debug("web_search retry in {:.1f}s", delay)
                        await asyncio.sleep(delay)

                if searxng_available():
                    logger.info("web_search: falling back to SearXNG ({})", fallback_reason)
                    await push_search_fallback(session_id, "searxng", fallback_reason)
                    return await asyncio.to_thread(searxng_search, query)

                return (
                    f"Web search failed after {RETRY_MAX_ATTEMPTS} attempts. "
                    f"Last error: {last_error}. "
                    "Please try a more specific query or answer without web search."
                )

        base = BackendAwareTavilySearch(
            tavily_api_key=_tavily_api_key(), max_results=5, args_schema=WebSearchInput
        )
        # ONE tool name across every branch. TavilySearch's own default is
        # ``tavily_search``, which would make the backend swap visible as a
        # DIFFERENT tool (the per-session tool selection, the 工具 tab and the
        # model's tool list all know this tool as ``web_search``). Transcripts
        # persisted before this rename may still carry ``tavily_search`` — the
        # untrusted-output / redaction sets keep covering that name.
        base.name = "web_search"
        # The keyed branch used to return BEFORE the metadata assignment below,
        # so the shipped Tavily tool carried neither flag.
        base.handle_tool_error = True
        base.metadata = {"idempotent": False}
        return base

    elif searxng_available():

        @tool("web_search", args_schema=WebSearchSchema)
        async def web_search(query: str) -> str:
            """Search the web for information through the configured SearXNG instance."""
            return await asyncio.to_thread(searxng_search, query)

        web_search.handle_tool_error = True
        web_search.metadata = {"idempotent": False}
        return web_search

    @tool("web_search", args_schema=WebSearchSchema)
    def web_search(query: str) -> str:
        """Search the web for information. Currently unavailable due to missing API key or configuration."""
        return (
            "Web search is currently unavailable: neither TAVILY_API_KEY nor SEARXNG_URL is "
            "configured. Configure one in the 模型联网 panel (or the .env file) and retry, or "
            "answer without web search."
        )

    web_search.handle_tool_error = True
    web_search.metadata = {"idempotent": False}
    return web_search
