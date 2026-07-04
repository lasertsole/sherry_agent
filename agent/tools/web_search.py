import os
import logging
from config import ENV_PATH
from dotenv import load_dotenv

load_dotenv(ENV_PATH, override=True)

logger = logging.getLogger(__name__)

tavily_api_key = os.getenv("TAVILY_API_KEY")


def build_web_search_tool():
    """构建web搜索工具，配置不可用或调用失败时降级为提示工具"""

    from langchain_core.tools import tool
    if tavily_api_key:
        from langchain_tavily import TavilySearch

        web_search = TavilySearch(tavily_api_key=tavily_api_key, max_results=5)
        return web_search
    else:
        @tool
        def web_search(query: str) -> str:
            """Search the web for information. Currently unavailable due to missing API key or configuration."""
            return "Web search is currently unavailable. TAVILY_API_KEY is not configured or the service is unreachable. Please answer without web search."
    web_search.handle_tool_error = True
    return web_search