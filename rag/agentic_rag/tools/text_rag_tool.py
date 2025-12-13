from typing import List
from ..native import retriever
from langchain_core.documents import Document
from langchain_core.tools import StructuredTool
from langgraph.graph.state import CompiledStateGraph
from ..self_refection import build_self_refection_graph

async def text_rag(question:  str) -> str:

    async def retrieve_callback(query: str)-> List[Document]:
        return await retriever.ainvoke(query, k=10, score_threshold=0.5)


    self_refection_graph: CompiledStateGraph = build_self_refection_graph(retrieve_callback)
    documents: List[Document] = await self_refection_graph.ainvoke({"question" : question})

    if documents is not None and len(documents) > 0:
        return "\n".join([doc.page_content for doc in documents])
    else:
        return "No relevant documents found."

text_rag_tool = StructuredTool.from_function(func=text_rag, description="text tage tool.")
text_rag_tool.handle_tool_error = True