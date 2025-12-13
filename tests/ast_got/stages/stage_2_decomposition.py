import logging
import datetime
import textwrap
from typing import Dict, Any, List
from models import simple_chat_model
from pydantic import BaseModel, Field
from tests.ast_got.models.node import Node
from tests.ast_got.models.edge import Edge
from tests.ast_got.models.graph import AGoTGraph
from langchain_core.runnables import RunnableSerializable
from langchain_core.prompts import SystemMessagePromptTemplate, HumanMessagePromptTemplate, ChatPromptTemplate
from tests.ast_got.utils.metadata_utils import generate_id

logger = logging.getLogger("agot-stage2")


class DimensionItem(BaseModel):
    """单个分析维度"""
    label: str = Field(description="维度的简短标签，例如：Scope, Objectives")
    description: str = Field(description="该维度的详细描述和分析要求")

class DimensionPlan(BaseModel):
    """维度拆解计划"""
    dimensions: List[DimensionItem] = Field(description="针对该问题定制的分析维度列表")

class DecompositionStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Decomposition Stage")

        root_node_id = context.get("root_node_id")
        if not root_node_id or root_node_id not in graph.graph:
            logger.error("Root node not found")
            return {
                "summary": "Decomposition failed: Root node not found",
                "metrics": {}
            }

        root_node: dict[str, Any] = graph.graph.nodes[root_node_id]
        disciplinary_tags: list[str] = root_node.get("disciplinary_tags", [])

        query: str = context.get("query", "")
        parameters: dict = context.get("parameters", {})


        dimensions: list[dict[str, str]] = context.get("dimensions", [
            {
                "label": "Scope",
                "description": "Define the boundaries of the research question"
            },
            {
                "label": "Objectives",
                "description": "Specific goals to be achieved"
            },
            {
                "label": "Constraints",
                "description": "Limitations and boundaries of the analysis"
            },
            {
                "label": "Data Needs",
                "description": "Information required to address the question"
            },
            {
                "label": "Use Cases",
                "description": "Practical applications of findings"
            },
            {
                "label": "Potential Biases",  # As per P1.17
                "description": "Sources of cognitive or methodological bias"
            },
            {
                "label": "Knowledge Gaps",  # As per P1.15
                "description": "Areas of uncertainty or missing information"
            }
        ])

        dimension_nodes = []
        for i, dim in enumerate(dimensions):
            dim_id = f"dim_{i+1}"

            confidence = parameters.get("dimension_confidence", [0.8, 0.8, 0.8, 0.8])

            dimension_node = Node(
                node_id=dim_id,
                label=dim["label"],
                node_type="dimension",
                confidence=confidence,
                metadata={
                    "description": dim.get("description", ""),
                    "timestamp": str(datetime.datetime.now()),
                    "provenance": "Task decomposition",
                    "disciplinary_tags": disciplinary_tags,
                    "layer_id": parameters.get("dimension_layer", "root")
                }
            )

            graph.add_node(dimension_node)

            edge = Edge(
                edge_id=f"e_root_dim_{i+1}",
                source=root_node_id,
                target=dim_id,
                edge_type="decomposition",
                confidence=0.9,
                metadata={
                    "timestamp": str(datetime.datetime.now()),
                    "edge_subtype": "dimension"
                }
            )

            graph.add_edge(edge)
            dimension_nodes.append(dimension_node)

        logger.info(f"Decomposition complete: Created {len(dimension_nodes)} dimension nodes")

        return {
            "dimension_nodes": [node.node_id for node in dimension_nodes],
            "summary": f"Decomposed task into {len(dimension_nodes)} dimensions",
            "metrics": {
                "dimension_count": len(dimension_nodes)
            }
        }