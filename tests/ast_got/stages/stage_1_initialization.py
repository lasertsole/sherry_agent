import logging
import datetime
from typing import Dict, Any, List
from models import simple_chat_model
from pydantic import BaseModel, Field
from tests.ast_got.models.node import Node
from tests.ast_got.models.graph import AGoTGraph
from langchain_core.runnables import RunnableSerializable
from langchain_core.prompts import SystemMessagePromptTemplate, HumanMessagePromptTemplate, ChatPromptTemplate


logger = logging.getLogger("agot-stage1")


class DomainExtraction(BaseModel):
    """Extract the academic domains involved in the question"""
    domains: List[str] = Field(description="List of academic domains involved in the question, e.g., ['Physics', 'Astronomy']", default=[])

class InitializationStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Initialization Stage")

        query = context.get("query", "")
        parameters = context.get("parameters", {})

        root_node = Node(
            node_id="n0",
            label="Task Understanding",
            node_type="root",
            confidence=[0.9, 0.9, 0.9, 0.9],
            metadata={
                "query": query,
                "timestamp": str(datetime.datetime.now()),
                "provenance": "User query",
                "epistemic_status": "Initial",
                "disciplinary_tags": self._extract_disciplines(query, parameters),
                "layer_id": parameters.get("initial_layer", "root")
            }
        )

        graph.add_node(root_node)

        if "layers" in parameters:
            for layer_id, layer_info in parameters["layers"].items():
                graph.layers[layer_id] = set()

        logger.info(f"Initialization complete: Root node created with ID {root_node.node_id}")

        return {
            "root_node_id": root_node.node_id,
            "summary": "Initialized graph with root node based on query understanding",
            "metrics": {
                "initial_confidence": root_node.confidence
            }
        }

    def _extract_disciplines(self, query: str, parameters: Dict[str, Any]) -> List[str]:
        system_template = "You are a domain planning expert. Please identify the academic domains involved in the user's question."

        system_prompt = SystemMessagePromptTemplate.from_template(system_template)
        human_prompt = HumanMessagePromptTemplate.from_template("{query}")

        chat_prompt = ChatPromptTemplate.from_messages([system_prompt, human_prompt])

        invoker: RunnableSerializable = chat_prompt | simple_chat_model.with_structured_output(DomainExtraction)

        result: DomainExtraction = invoker.invoke({"query": query})

        return result.domains