from typing import List

from langchain_core.tools import BaseTool

from tools import web_search_tool
from .text_rag_tool import text_rag_tool

# 全部工具
def build_all_tools()-> List[BaseTool]:
    return [
        web_search_tool,
        text_rag_tool
    ]

__all__ = [
    "build_all_tools"
]