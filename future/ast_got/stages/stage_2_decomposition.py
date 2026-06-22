import datetime
import textwrap
from typing import Any
from tests import Node
from loguru import logger
from pydantic import BaseModel, Field
from future.ast_got.models.edge import Edge
from future.ast_got.models.graph import AGoTGraph
from langchain_core.runnables import RunnableSerializable
from langchain_core.prompts import SystemMessagePromptTemplate, HumanMessagePromptTemplate, ChatPromptTemplate


class DimensionItem(BaseModel):
    """单个分析维度"""
    label: str = Field(description="维度的简短标签，例如：Scope, Objectives")
    description: str = Field(description="该维度的详细描述和分析要求")

class DimensionPlan(BaseModel):
    """维度拆解计划"""
    dimensions: list[DimensionItem] = Field(description="针对该问题定制的分析维度列表")

class DecompositionStage:
    def _generate_dimensions(self, query: str, disciplinary_tags: list[str]) -> DimensionPlan:
        """调用模型为问题生成定制化的分析维度"""
        system_template = textwrap.dedent("""\
            你是任务分解专家。请分析用户提出的问题，从多学科视角拆解出合适的分析维度。

            Requirements:
            1. 维度应与问题本身强相关，覆盖不同分析角度
            2. 每个维度给出清晰的 label 和 description
            3. 通常生成 4-8 个维度，根据问题复杂度灵活调整
            4. 参考提供的学科标签，但不要被限制住
            5. 避免过于宽泛或与问题无关的通用维度
        """)

        system_prompt = SystemMessagePromptTemplate.from_template(system_template)
        human_prompt = HumanMessagePromptTemplate.from_template(
            "User Query: {query}\nRelated Disciplines: {disciplines}"
        )

        chat_prompt = ChatPromptTemplate.from_messages([system_prompt, human_prompt])
        invoker: RunnableSerializable = chat_prompt | auxiliary_llm.with_structured_output(DimensionPlan)

        result: DimensionPlan = invoker.invoke({
            "query": query,
            "disciplines": ", ".join(disciplinary_tags),
        })
        return result

    def execute(self, graph: AGoTGraph, context: dict[str, Any]) -> dict[str, Any]:
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


        # 优先用 AI 生成分析维度，失败时回退到默认值
        dimensions: list[dict[str, str]] = context.get("dimensions", None)
        if dimensions is None:
            try:
                ai_dimensions = self._generate_dimensions(query, disciplinary_tags)
                dimensions = [{"label": d.label, "description": d.description} for d in ai_dimensions.dimensions]
                logger.info(f"AI generated {len(dimensions)} dimensions")
            except Exception as e:
                logger.warning(f"AI dimension generation failed, using defaults: {e}")
                dimensions = [
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
                ]

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