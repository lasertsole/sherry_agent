import random
import datetime
from loguru import logger
from typing import Dict, Any, List
from models import simple_chat_model
from pydantic import BaseModel, Field
from tests.ast_got.models.node import Node
from tests.ast_got.models.edge import Edge
from tests.ast_got.models.graph import AGoTGraph
from langchain_core.runnables import RunnableSerializable
from langchain_core.prompts import SystemMessagePromptTemplate, HumanMessagePromptTemplate, ChatPromptTemplate

from tests.ast_got.utils.metadata_utils import generate_id, check_falsifiability

class HypothesisItem(BaseModel):
    """单个科学假设"""
    label: str = Field(description="A concise title for the hypothesis")
    description: str = Field(
        description="A detailed description of the hypothesis, including relationships between variables")
    falsification_criteria: str = Field(
        description="Specific criteria or experimental methods to falsify this hypothesis")
    disciplinary_tags: List[str] = Field(description="Disciplinary domain tags relevant to this hypothesis")
    plan_type: str = Field(description="Type of validation plan, e.g., search, experiment, simulation, meta_analysis")
    impact_score: float = Field(description="Importance score of this hypothesis for solving the core problem (0-1)")


class HypothesisPlan(BaseModel):
    """假设生成计划"""
    hypotheses: List[HypothesisItem] = Field(description="针对该维度生成的假设列表")


class HypothesisStage:
    def _generate_hypotheses_for_dimension(self, query: str, dim_label: str, disciplines: List[str], k: int) -> List[
        HypothesisItem]:
        """调用模型为特定维度生成假设"""
        system_template = """你是科学假设生成专家。请根据用户问题和给定的分析维度，生成具有高度可证伪性的科学假设。
        
        Requirements:
        1. Hypotheses must be logically rigorous and explicitly state how they can be falsified (falsification_criteria).
        2. Disciplinary tags should be selected from the provided list or reasonably extended.
        3. The impact_score should reflect the core value of the hypothesis in solving the problem.
        4. Generate {k} distinct hypotheses."""


        system_prompt = SystemMessagePromptTemplate.from_template(system_template)
        human_prompt = HumanMessagePromptTemplate.from_template(
            "User Query: {query}\nCurrent Dimension: {dim_label}\nRelated Disciplines: {disciplines}"
        )

        chat_prompt = ChatPromptTemplate.from_messages([system_prompt, human_prompt])
        invoker: RunnableSerializable = chat_prompt | simple_chat_model.with_structured_output(HypothesisPlan)

        result: HypothesisPlan = invoker.invoke({
            "query": query,
            "dim_label": dim_label,
            "disciplines": ", ".join(disciplines),
            "k": k
        })
        return result.hypotheses

    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Hypothesis Generation Stage")

        dimension_nodes = context.get("dimension_nodes", [])
        if not dimension_nodes:
            logger.error("No dimension nodes found")
            return {
                "summary": "Hypothesis generation failed: No dimensions available",
                "metrics": {}
            }

        query = context.get("query", "")
        parameters = context.get("parameters", {})
        k = parameters.get("hypotheses_per_dimension", random.randint(1, 3))
        k = max(1, min(k, 3))

        all_hypotheses = []
        for dim_id in dimension_nodes:
            if dim_id not in graph.graph:
                logger.warning(f"Dimension node {dim_id} not found in graph")
                continue

            dim_node = graph.graph.nodes[dim_id]
            dim_label = dim_node.get("label", "Unknown dimension")
            all_disciplines = dim_node.get("disciplinary_tags", [])

            try:
                hypothesis_items = self._generate_hypotheses_for_dimension(query, dim_label, all_disciplines, k)

                for i, item in enumerate(hypothesis_items):
                    hypothesis_id = f"hypo_{dim_id}_{i+1}"

                    num_tags = random.randint(1, min(3, len(all_disciplines)))
                    disciplines = random.sample(all_disciplines, num_tags)

                    hypothesis_label = f"Hypothesis {i+1} for {dim_label}"

                    falsifiability = f"This hypothesis can be tested by examining {random.choice(['experimental', 'clinical', 'molecular', 'computational'])} evidence related to {random.choice(['gene expression', 'immune cell populations', 'treatment response', 'microbiome composition'])}."

                    confidence = parameters.get("hypothesis_confidence", [0.5, 0.5, 0.5, 0.5])

                    impact_score = random.uniform(0.3, 0.9)

                    plan_types = ["search", "experiment", "simulation", "meta_analysis"]
                    plan = {
                        "type": random.choice(plan_types),
                        "description": f"Plan to evaluate {hypothesis_label}",
                        "estimated_cost": random.uniform(0.1, 1.0),
                        "estimated_duration": random.uniform(0.1, 1.0)
                    }

                    hypothesis_node = Node(
                        node_id=hypothesis_id,
                        label=hypothesis_label,
                        node_type="hypothesis",
                        confidence=confidence,
                        metadata={
                            "dimension": dim_id,
                            "timestamp": str(datetime.datetime.now()),
                            "provenance": "Hypothesis generation",
                            "disciplinary_tags": item.disciplinary_tags,
                            "falsification_criteria": item.falsification_criteria,
                            "plan": plan,
                            "impact_score": item.impact_score,
                            "bias_flags": [],
                            "layer_id": parameters.get("hypothesis_layer", "root"),
                            "description": item.description  # 存入详细描述
                        }
                    )

                    graph.add_node(hypothesis_node)

                    edge = Edge(
                        edge_id=f"e_{dim_id}_{hypothesis_id}",
                        source=dim_id,
                        target=hypothesis_id,
                        edge_type="hypothesis",
                        confidence=0.8,
                        metadata={
                            "timestamp": str(datetime.datetime.now())
                        }
                    )

                    graph.add_edge(edge)
                    all_hypotheses.append(hypothesis_id)
            except Exception as e:
                logger.error(f"Failed to generate hypotheses for dimension {dim_id}: {e}")

            bias_risk = random.uniform(0, 1)
            if bias_risk > 0.7:
                bias_types = ["confirmation_bias", "selection_bias", "anchoring_bias"]
                bias = {
                    "type": random.choice(bias_types),
                    "description": f"Potential bias detected in hypothesis formulation",
                    "severity": random.choice(["low", "medium", "high"])
                }
                graph.graph.nodes[hypothesis_id]["bias_flags"] = [bias]

        logger.info(f"Hypothesis generation complete: Created {len(all_hypotheses)} hypotheses")

        return {
            "hypotheses": all_hypotheses,
            "summary": f"Generated {len(all_hypotheses)} hypotheses across {len(dimension_nodes)} dimensions",
            "metrics": {
                "hypothesis_count": len(all_hypotheses),
                "hypotheses_per_dimension": len(all_hypotheses) / max(len(dimension_nodes), 1)
            }
        }