import os
import asyncio
import random
from loguru import logger
from config import ENV_PATH
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(ENV_PATH, override=True)

tavily_api_key = os.getenv("TAVILY_API_KEY")

WEB_SEARCH_TIMEOUT = 15  # seconds
RETRY_BACKOFF_MIN = 5    # seconds
RETRY_BACKOFF_MAX = 45   # seconds
RETRY_MAX_ATTEMPTS = 3


class WebSearchSchema(BaseModel):
    query: str = Field(description="The search query to look up on the web.")


def _backoff_delay(attempt: int) -> float:
    delay = RETRY_BACKOFF_MIN * (2 ** attempt)
    jitter = random.uniform(0, delay)
    return min(delay + jitter, RETRY_BACKOFF_MAX)


def build_web_search_tool():
    """Build web search tool with fallback when unavailable or call fails"""

    from langchain_core.tools import tool
    if tavily_api_key:
        from langchain_tavily import TavilySearch

        base = TavilySearch(tavily_api_key=tavily_api_key, max_results=5)

        original_arun = base._arun

        async def _arun_with_retry(*args, **kwargs):
            last_error = None
            for attempt in range(RETRY_MAX_ATTEMPTS):
                try:
                    return await asyncio.wait_for(
                        original_arun(*args, **kwargs),
                        timeout=WEB_SEARCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    last_error = f"timed out after {WEB_SEARCH_TIMEOUT}s"
                    logger.warning("web_search attempt {}/{} {}", attempt + 1, RETRY_MAX_ATTEMPTS, last_error)
                except Exception as e:
                    last_error = str(e)
                    logger.warning("web_search attempt {}/{} failed: {}", attempt + 1, RETRY_MAX_ATTEMPTS, last_error)

                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    delay = _backoff_delay(attempt)
                    logger.debug("web_search retry in {:.1f}s", delay)
                    await asyncio.sleep(delay)

            return (
                f"Web search failed after {RETRY_MAX_ATTEMPTS} attempts. "
                f"Last error: {last_error}. "
                "Please try a more specific query or answer without web search."
            )

        base._arun = _arun_with_retry
        return base
    else:
        @tool("web_search", args_schema=WebSearchSchema)
        def web_search(query: str) -> str:
            """Search the web for information. Currently unavailable due to missing API key or configuration."""
            return "Web search is currently unavailable. TAVILY_API_KEY is not configured or the service is unreachable. Please answer without web search."
    web_search.handle_tool_error = True
    web_search.metadata = {"idempotent": False}
    return web_search