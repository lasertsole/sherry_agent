from .generator import generator
from .answer_grader import answer_grader
from langgraph.graph import END, StateGraph
from langchain_core.documents import Document
from .retrieval_grader import retrieval_grader
from .question_rewriter import question_rewriter
from langgraph.graph.state import CompiledStateGraph
from .hallucination_grader import hallucination_grader
from typing import List, TypedDict, Callable, Awaitable

class GraphState(TypedDict):
    """
    Represents the state of our graph.

    Attributes:
        question: question
        generation: LLM generation
        documents: list of documents
    """
    question : str
    generation : str
    documents : List[str]

def build_self_refection_graph(retrieve_callback: Callable[[str], Awaitable[List[Document]]])-> CompiledStateGraph:
    async def retrieve(state: GraphState):
        """
        Retrieve documents

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, documents, that contains retrieved documents
        """

        question = state["question"]
        documents = await retrieve_callback(question)

        return {"documents": documents, "question": question}

    async def generate(state):
        """
        Generate answer

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, generation, that contains LLM generation
        """
        question = state["question"]
        documents = state["documents"]

        # RAG generation
        generation = await generator.ainvoke({"documents": documents, "question": question})
        return {"documents": documents, "question": question, "generation": generation}

    async def grade_documents(state):
        """
        Determines whether the retrieved documents are relevant to the question.

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Updates documents key with only filtered relevant documents
        """

        question = state["question"]
        documents = state["documents"]

        filtered_docs = []
        for d in documents:
            score = await retrieval_grader.ainvoke({"question": question, "document": d.page_content})
            grade = score.binary_score
            if grade == "yes":
                filtered_docs.append(d)
            else:
                continue
        return {"documents": filtered_docs, "question": question}

    async def transform_query(state):
        """
        Transform the query to produce a better question.

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Updates question key with a re-phrased question
        """

        question = state["question"]
        documents = state["documents"]

        # Re-write question
        better_question = await question_rewriter.ainvoke({"question": question})
        return {"documents": documents, "question": better_question}

    def decide_to_generate(state):
        """
        Determines whether to generate an answer, or re-generate a question.

        Args:
            state (dict): The current graph state

        Returns:
            str: Binary decision for next node to call
        """
        filtered_documents = state["documents"]

        if not filtered_documents:
            return "transform_query"
        else:
            return "generate"

    async def grade_generation_v_documents_and_question(state):
        """
        Determines whether the generation is grounded in the document and answers question.

        Args:
            state (dict): The current graph state

        Returns:
            str: Decision for next node to call
        """
        question = state["question"]
        documents = state["documents"]
        generation = state["generation"]

        score = await hallucination_grader.ainvoke({"documents": documents, "generation": generation})
        grade = score.binary_score

        # Check hallucination
        if grade == "yes":
            # Check question-answering
            score = await answer_grader.ainvoke({"question": question, "generation": generation})
            grade = score.binary_score
            if grade == "yes":
                return "useful"
            else:
                return "not useful"
        else:
            return "not supported"

    workflow = StateGraph(GraphState)


    # Define the nodes
    workflow.add_node("retrieve", retrieve)  # retrieve
    workflow.add_node("grade_documents", grade_documents)  # grade documents
    workflow.add_node("generate", generate)  # generate
    workflow.add_node("transform_query", transform_query)  # transform_query

    # Build graph
    workflow.set_entry_point("retrieve")
    workflow.add_edge("retrieve", "grade_documents")
    workflow.add_conditional_edges(
        "grade_documents",
        decide_to_generate,
        {
            "transform_query": "transform_query",
            "generate": "generate",
        },
    )
    workflow.add_edge("transform_query", "retrieve")
    workflow.add_conditional_edges(
        "generate",
        grade_generation_v_documents_and_question,
        {
            "not supported": "generate",
            "useful": END,
            "not useful": "transform_query",
        },
    )
    # Compile
    graph: CompiledStateGraph = workflow.compile()
    return graph