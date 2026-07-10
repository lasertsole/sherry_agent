from typing import Literal
from typing import Annotated
from pydantic import BaseModel
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

class XPGraphSchema(BaseModel):
    mode: Literal["draft", "extract", "retrieve"]

@tool("xp_graph", args_schema=XPGraphSchema)
def xp_graph(
    mode: Literal["draft", "extract", "retrieve"],
    session_id: Annotated[str, InjectedState("session_id")] = "",
):
    pass


def build_xp_graph_tool():
    xp_graph.metadata = {"idempotent": False}
    xp_graph.handle_tool_error = True
    return xp_graph