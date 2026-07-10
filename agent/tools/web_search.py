import os
import asyncio
from loguru import logger
from config import ENV_PATH
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(ENV_PATH, override=True)

tavily_api_key = os.getenv("TAVILY_API_KEY")

WEB_SEARCH_TIMEOUT = 15  # seconds

class WebSearchSchema(BaseModel):
    query: str = Field(description="The search query to look up on the web.")


def build_web_search_tool():
    """构建web搜索工具，配置不可用或调用失败时降级为提示工具"""

    from langchain_core.tools import tool
    if tavily_api_key:
        from langchain_tavily import TavilySearch

        base = TavilySearch(tavily_api_key=tavily_api_key, max_results=5)

        # Wrap _arun with timeout without modifying the original class
        original_arun = base._arun

        async def _arun_with_timeout(*args, **kwargs):
            try:
                return await asyncio.wait_for(
                    original_arun(*args, **kwargs),
                    timeout=WEB_SEARCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("web_search timed out after {}s", WEB_SEARCH_TIMEOUT)
                return (
                    f"Web search timed out after {WEB_SEARCH_TIMEOUT} seconds. "
                    "Please try a more specific query or answer without web search."
                )

        base._arun = _arun_with_timeout
        return base
    else:
        @tool("web_search", args_schema=WebSearchSchema)
        def web_search(query: str) -> str:
            """Search the web for information. Currently unavailable due to missing API key or configuration."""
            return "Web search is currently unavailable. TAVILY_API_KEY is not configured or the service is unreachable. Please answer without web search."
    web_search.handle_tool_error = True
    web_search.metadata = {"idempotent": False}
    return web_search