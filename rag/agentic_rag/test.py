import asyncio
from typing import List
from native import retriever
from rerank import build_rerank_graph
from langchain_core.documents import Document
from self_refection import build_self_refection_graph

async def main():
    question = "樱羽艾玛的生日是什么时候。"
    #
    # async def retrieve_callback(query: str)-> List[Document]:
    #     async def retrieve_callback(query: str) -> List[Document]:
    #         return await retriever.ainvoke(query, k=10, score_threshold=0.5)
    #
    #     query_by_re_writen_graph = build_query_by_re_writen_graph(retrieve_callback = retrieve_callback)
    #     rerank_graph = build_rerank_graph(k = 10)
    #     answers = await query_by_re_writen_graph.ainvoke({"input": query})
    #     answers = answers["output"]
    #
    #     answers = await rerank_graph.ainvoke({"input": {"query": query, "answers": answers}})
    #     return answers["output"]
    #
    #
    # self_refection_graph = build_self_refection_graph(retrieve_callback)
    # print(await self_refection_graph.ainvoke({"question" : question}))

    # async def retrieve_callback(query: str) -> List[Document]:
    #     return await retriever.ainvoke(query, k=10, score_threshold=0.5)
    # question = "樱羽艾玛会变胖吗"
    # from query_transformations.decomposition import generate_queries_decomposition, build_answer_recursively_graph
    # questions = await generate_queries_decomposition.ainvoke({"question":question})
    #
    # answer_recursively_node = build_answer_recursively_graph(retrieve_callback)
    # print(await answer_recursively_node.ainvoke({"input":questions}))

if __name__ == "__main__":
    asyncio.run(main())