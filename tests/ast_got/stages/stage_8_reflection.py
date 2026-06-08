from loguru import logger
from typing import Dict, Any, List

from models import simple_chat_model
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from tests.ast_got.models.graph import AGoTGraph


class HolisticAssessment(BaseModel):
    """AI 综合审计结果"""
    summary: str = Field(description="A concise 1-2 sentence overall assessment of the analysis quality")
    assessment: str = Field(description="A detailed 3-6 sentence integrated quality assessment cross-referencing all audit dimensions")


class ImprovementSuggestion(BaseModel):
    """AI 改进建议"""
    area: str = Field(description="The area or dimension needing improvement")
    suggestion: str = Field(description="A concrete, actionable suggestion for improvement")
    priority: str = Field(description="Priority level: 'high', 'medium', or 'low'")


class ReflectionStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Reflection Stage")

        parameters = context.get("parameters", {})
        composition_result = context.get("composition_result", {})

        audit_results = {
            "passed": [],
            "warnings": [],
            "failures": []
        }

        high_conf_impact_check = self._check_high_confidence_impact_coverage(graph)
        if high_conf_impact_check["status"] == "pass":
            audit_results["passed"].append(high_conf_impact_check)
        elif high_conf_impact_check["status"] == "warning":
            audit_results["warnings"].append(high_conf_impact_check)
        else:
            audit_results["failures"].append(high_conf_impact_check)

        bias_check = self._check_bias_flags(graph)
        if bias_check["status"] == "pass":
            audit_results["passed"].append(bias_check)
        elif bias_check["status"] == "warning":
            audit_results["warnings"].append(bias_check)
        else:
            audit_results["failures"].append(bias_check)

        gaps_check = self._check_knowledge_gaps_addressed(graph, composition_result)
        if gaps_check["status"] == "pass":
            audit_results["passed"].append(gaps_check)
        elif gaps_check["status"] == "warning":
            audit_results["warnings"].append(gaps_check)
        else:
            audit_results["failures"].append(gaps_check)

        falsifiability_check = self._check_falsifiability(graph)
        if falsifiability_check["status"] == "pass":
            audit_results["passed"].append(falsifiability_check)
        elif falsifiability_check["status"] == "warning":
            audit_results["warnings"].append(falsifiability_check)
        else:
            audit_results["failures"].append(falsifiability_check)

        causal_check = self._check_causal_claims(graph)
        if causal_check["status"] == "pass":
            audit_results["passed"].append(causal_check)
        elif causal_check["status"] == "warning":
            audit_results["warnings"].append(causal_check)
        else:
            audit_results["failures"].append(causal_check)

        temporal_check = self._check_temporal_consistency(graph)
        if temporal_check["status"] == "pass":
            audit_results["passed"].append(temporal_check)
        elif temporal_check["status"] == "warning":
            audit_results["warnings"].append(temporal_check)
        else:
            audit_results["failures"].append(temporal_check)

        statistical_check = self._check_statistical_rigor(graph)
        if statistical_check["status"] == "pass":
            audit_results["passed"].append(statistical_check)
        elif statistical_check["status"] == "warning":
            audit_results["warnings"].append(statistical_check)
        else:
            audit_results["failures"].append(statistical_check)

        attribution_check = self._check_collaboration_attributions(graph)
        if attribution_check["status"] == "pass":
            audit_results["passed"].append(attribution_check)
        elif attribution_check["status"] == "warning":
            audit_results["warnings"].append(attribution_check)
        else:
            audit_results["failures"].append(attribution_check)

        passed_count = len(audit_results["passed"])
        warning_count = len(audit_results["warnings"])
        failure_count = len(audit_results["failures"])

        total_checks = passed_count + warning_count + failure_count

        empirical = 0.9 if statistical_check["status"] == "pass" else (0.6 if statistical_check["status"] == "warning" else 0.3)

        theoretical = 0.9 if causal_check["status"] == "pass" else (0.6 if causal_check["status"] == "warning" else 0.3)

        methodology_scores = []
        for check in [falsifiability_check, bias_check]:
            if check["status"] == "pass":
                methodology_scores.append(0.9)
            elif check["status"] == "warning":
                methodology_scores.append(0.6)
            else:
                methodology_scores.append(0.3)
        methodological = sum(methodology_scores) / len(methodology_scores)

        consensus = 0.9 if attribution_check["status"] == "pass" else (0.6 if attribution_check["status"] == "warning" else 0.3)

        final_confidence = [empirical, theoretical, methodological, consensus]

        # --- [AI] 综合质量审计 ---
        holistic = self._ai_holistic_audit(graph, audit_results, final_confidence)
        if holistic:
            audit_results["ai_assessment"] = holistic["assessment"]
            reflection_summary = holistic["summary"]
        else:
            if failure_count > 0:
                reflection_summary = "Analysis has significant weaknesses that should be addressed."
            elif warning_count > 0:
                reflection_summary = "Analysis has some areas that could be improved."
            else:
                reflection_summary = "Analysis appears robust across all audit dimensions."

        # --- [AI] 改进建议 ---
        improvements = self._ai_suggest_improvements(graph, audit_results)
        if improvements:
            audit_results["improvement_suggestions"] = improvements

        logger.info(f"Reflection complete: {passed_count} passed, {warning_count} warnings, {failure_count} failures")

        return {
            "audit_results": audit_results,
            "final_confidence": final_confidence,
            "reflection_summary": reflection_summary,
            "summary": f"Completed self-audit with {passed_count} passed, {warning_count} warnings, {failure_count} failures",
            "metrics": {
                "passed_checks": passed_count,
                "warning_checks": warning_count,
                "failed_checks": failure_count,
                "total_checks": total_checks
            }
        }

    def _check_high_confidence_impact_coverage(self, graph: AGoTGraph) -> Dict[str, Any]:
        high_conf_count = 0
        high_impact_count = 0

        for _, data in graph.graph.nodes(data=True):
            confidence = data.get("confidence", [])
            if confidence and sum(confidence) / len(confidence) >= 0.7:
                high_conf_count += 1

            impact = data.get("impact_score", 0)
            if impact >= 0.7:
                high_impact_count += 1

        total_nodes = graph.graph.number_of_nodes()
        if total_nodes == 0:
            return {
                "test": "high_confidence_impact_coverage",
                "status": "failure",
                "message": "Graph contains no nodes."
            }

        conf_percentage = high_conf_count / total_nodes
        impact_percentage = high_impact_count / total_nodes

        if conf_percentage >= 0.3 and impact_percentage >= 0.2:
            return {
                "test": "high_confidence_impact_coverage",
                "status": "pass",
                "message": f"Good coverage of high-confidence ({conf_percentage:.1%}) and high-impact ({impact_percentage:.1%}) nodes."
            }
        elif conf_percentage >= 0.1 and impact_percentage >= 0.1:
            return {
                "test": "high_confidence_impact_coverage",
                "status": "warning",
                "message": f"Limited coverage of high-confidence ({conf_percentage:.1%}) and high-impact ({impact_percentage:.1%}) nodes."
            }
        else:
            return {
                "test": "high_confidence_impact_coverage",
                "status": "failure",
                "message": f"Poor coverage of high-confidence ({conf_percentage:.1%}) and high-impact ({impact_percentage:.1%}) nodes."
            }

    def _check_bias_flags(self, graph: AGoTGraph) -> Dict[str, Any]:
        flagged_nodes = 0
        serious_bias_nodes = 0

        for _, data in graph.graph.nodes(data=True):
            bias_flags = data.get("bias_flags", [])
            if bias_flags:
                flagged_nodes += 1

                for bias in bias_flags:
                    if isinstance(bias, dict) and bias.get("severity") == "high":
                        serious_bias_nodes += 1

        if serious_bias_nodes > 0:
            return {
                "test": "bias_flags",
                "status": "warning",
                "message": f"Found {serious_bias_nodes} nodes with serious bias flags that may affect results."
            }
        elif flagged_nodes > 0:
            return {
                "test": "bias_flags",
                "status": "pass",
                "message": f"Detected and acknowledged {flagged_nodes} potential biases, none serious."
            }
        else:
            total_nodes = graph.graph.number_of_nodes()
            if total_nodes > 20:
                return {
                    "test": "bias_flags",
                    "status": "warning",
                    "message": "No bias flags detected in a large graph, suggesting insufficient bias assessment."
                }
            else:
                return {
                    "test": "bias_nodes",
                    "status": "pass",
                    "message": "No bias flags detected."
                }

    def _check_knowledge_gaps_addressed(self, graph: AGoTGraph, composition_result: Dict[str, Any]) -> Dict[str, Any]:
        gap_nodes = 0

        for _, data in graph.graph.nodes(data=True):
            if data.get("node_type") == "placeholder_gap":
                gap_nodes += 1

        gaps_addressed = False
        for section in composition_result.get("sections", []):
            if "gap" in section.get("title", "").lower() or "gap" in section.get("type", "").lower():
                gaps_addressed = True
                break

        if gap_nodes > 0 and gaps_addressed:
            return {
                "test": "knowledge_gaps_addressed",
                "status": "pass",
                "message": f"Identified {gap_nodes} knowledge gaps and addressed them in the output."
            }
        elif gap_nodes > 0 and not gaps_addressed:
            return {
                "test": "knowledge_gaps_addressed",
                "status": "warning",
                "message": f"Identified {gap_nodes} knowledge gaps but did not adequately address them in the output."
            }
        else:
            return {
                "test": "knowledge_gaps_addressed",
                "status": "pass",
                "message": "No significant knowledge gaps identified."
            }

    def _check_falsifiability(self, graph: AGoTGraph) -> Dict[str, Any]:
        hypothesis_nodes = 0
        falsifiable_nodes = 0

        for _, data in graph.graph.nodes(data=True):
            if data.get("node_type") == "hypothesis":
                hypothesis_nodes += 1

                if "falsification_criteria" in data and data["falsification_criteria"]:
                    falsifiable_nodes += 1

        if hypothesis_nodes == 0:
            return {
                "test": "falsifiability",
                "status": "warning",
                "message": "No hypothesis nodes found to evaluate falsifiability."
            }

        falsifiable_percentage = falsifiable_nodes / hypothesis_nodes

        if falsifiable_percentage >= 0.8:
            return {
                "test": "falsifiability",
                "status": "pass",
                "message": f"Good falsifiability: {falsifiable_percentage:.1%} of hypotheses have falsification criteria."
            }
        elif falsifiable_percentage >= 0.5:
            return {
                "test": "falsifiability",
                "status": "warning",
                "message": f"Limited falsifiability: only {falsifiable_percentage:.1%} of hypotheses have falsification criteria."
            }
        else:
            return {
                "test": "falsifiability",
                "status": "failure",
                "message": f"Poor falsifiability: only {falsifiable_percentage:.1%} of hypotheses have falsification criteria."
            }

    def _check_causal_claims(self, graph: AGoTGraph) -> Dict[str, Any]:
        causal_edges = 0
        well_supported_causal = 0

        for _, _, data in graph.graph.edges(data=True):
            if data.get("edge_type") == "causal":
                causal_edges += 1

                if "causal_metadata" in data and data["causal_metadata"]:
                    if "confounders" in data["causal_metadata"]:
                        well_supported_causal += 1

        if causal_edges == 0:
            return {
                "test": "causal_claims",
                "status": "pass",
                "message": "No causal claims made in the analysis."
            }

        supported_percentage = well_supported_causal / causal_edges

        if supported_percentage >= 0.8:
            return {
                "test": "causal_claims",
                "status": "pass",
                "message": f"Strong causal validity: {supported_percentage:.1%} of causal claims are well-supported."
            }
        elif supported_percentage >= 0.5:
            return {
                "test": "causal_claims",
                "status": "warning",
                "message": f"Moderate causal validity: only {supported_percentage:.1%} of causal claims are well-supported."
            }
        else:
            return {
                "test": "causal_claims",
                "status": "failure",
                "message": f"Weak causal validity: only {supported_percentage:.1%} of causal claims are well-supported."
            }

    def _check_temporal_consistency(self, graph: AGoTGraph) -> Dict[str, Any]:
        temporal_edges = 0
        consistent_temporal = 0

        for _, _, data in graph.graph.edges(data=True):
            edge_subtype = data.get("edge_subtype") or ""
            if data.get("edge_type") == "temporal" or "temporal" in edge_subtype:
                temporal_edges += 1

                if "temporal_metadata" in data and data["temporal_metadata"]:
                    consistent_temporal += 1

        if temporal_edges == 0:
            return {
                "test": "temporal_consistency",
                "status": "pass",
                "message": "No temporal claims made in the analysis."
            }

        consistent_percentage = consistent_temporal / temporal_edges

        if consistent_percentage >= 0.8:
            return {
                "test": "temporal_consistency",
                "status": "pass",
                "message": f"Good temporal consistency: {consistent_percentage:.1%} of temporal relationships are well-defined."
            }
        elif consistent_percentage >= 0.5:
            return {
                "test": "temporal_consistency",
                "status": "warning",
                "message": f"Moderate temporal consistency: only {consistent_percentage:.1%} of temporal relationships are well-defined."
            }
        else:
            return {
                "test": "temporal_consistency",
                "status": "failure",
                "message": f"Poor temporal consistency: only {consistent_percentage:.1%} of temporal relationships are well-defined."
            }

    def _check_statistical_rigor(self, graph: AGoTGraph) -> Dict[str, Any]:
        evidence_nodes = 0
        powered_nodes = 0

        for _, data in graph.graph.nodes(data=True):
            if data.get("node_type") == "evidence":
                evidence_nodes += 1

                if "statistical_power" in data and data["statistical_power"] >= 0.7:
                    powered_nodes += 1

        if evidence_nodes == 0:
            return {
                "test": "statistical_rigor",
                "status": "warning",
                "message": "No evidence nodes found to evaluate statistical rigor."
            }

        powered_percentage = powered_nodes / evidence_nodes

        if powered_percentage >= 0.8:
            return {
                "test": "statistical_rigor",
                "status": "pass",
                "message": f"Good statistical rigor: {powered_percentage:.1%} of evidence has adequate statistical power."
            }
        elif powered_percentage >= 0.5:
            return {
                "test": "statistical_rigor",
                "status": "warning",
                "message": f"Moderate statistical rigor: only {powered_percentage:.1%} of evidence has adequate statistical power."
            }
        else:
            return {
                "test": "statistical_rigor",
                "status": "failure",
                "message": f"Poor statistical rigor: only {powered_percentage:.1%} of evidence has adequate statistical power."
            }

    def _check_collaboration_attributions(self, graph: AGoTGraph) -> Dict[str, Any]:
        attributed_nodes = 0

        for _, data in graph.graph.nodes(data=True):
            if "attribution" in data and data["attribution"]:
                attributed_nodes += 1

        if attributed_nodes > 0:
            return {
                "test": "collaboration_attributions",
                "status": "pass",
                "message": f"Found {attributed_nodes} nodes with proper attribution metadata."
            }
        else:
            return {
                "test": "collaboration_attributions",
                "status": "pass",
                "message": "No collaboration attributions found, which may be appropriate if single-author analysis."
            }

    # ============================================================
    # AI 辅助方法
    # ============================================================

    def _ai_holistic_audit(self, graph: AGoTGraph,
                            audit_results: Dict[str, Any],
                            final_confidence: List[float]):
        """[AI] 综合 8 项审计检查结果，生成整体质量评估

        返回 {summary, assessment} 字典，失败时返回 None。
        """
        try:
            passed = audit_results.get("passed", [])
            warnings = audit_results.get("warnings", [])
            failures = audit_results.get("failures", [])

            passed_msgs = [c["message"] for c in passed]
            warning_msgs = [c["message"] for c in warnings]
            failure_msgs = [c["message"] for c in failures]

            prompt = HumanMessage(content=f"""
You are a quality auditor for a knowledge graph analysis system. Given the results
of 8 automated audit checks and the final confidence scores across 4 dimensions,
produce an integrated quality assessment.

---
Checks PASSED: {len(passed)}
{chr(10).join('  ✓ ' + m for m in passed_msgs)}

---
Checks with WARNINGS: {len(warnings)}
{chr(10).join('  ⚠ ' + m for m in warning_msgs)}

---
Checks FAILED: {len(failures)}
{chr(10).join('  ✗ ' + m for m in failure_msgs)}

---
Final Confidence Dimensions (empirical, theoretical, methodological, consensus):
{final_confidence}

---
Total nodes: {graph.graph.number_of_nodes()}
Total edges: {graph.graph.number_of_edges()}

Generate:
1. summary: A 1-sentence overall quality verdict
2. assessment: A 3-6 sentence integrated assessment that cross-references issues across
   multiple audit dimensions and explains what they collectively imply
""")

            result = simple_chat_model.with_structured_output(HolisticAssessment).invoke([prompt])
            if isinstance(result, HolisticAssessment) and result.summary:
                return {"summary": result.summary, "assessment": result.assessment}
        except Exception as e:
            logger.warning(f"AI holistic audit failed: {e}")
        return None

    def _ai_suggest_improvements(self, graph: AGoTGraph,
                                 audit_results: Dict[str, Any]):
        """[AI] 根据审计结果生成具体改进建议

        返回改进建议列表，失败时返回空列表。
        """
        try:
            all_checks = (audit_results.get("passed", [])
                         + audit_results.get("warnings", [])
                         + audit_results.get("failures", []))

            check_messages = []
            for c in all_checks:
                status_icon = "✓" if c["status"] == "pass" else ("⚠" if c["status"] == "warning" else "✗")
                check_messages.append(f"{status_icon} [{c['test']}]: {c['message']}")

            prompt = HumanMessage(content=f"""
You are a quality improvement advisor for a knowledge graph analysis system.
Based on the audit results below, suggest concrete, actionable improvements.

Audit Results:
{chr(10).join(check_messages)}

Graph: {graph.graph.number_of_nodes()} nodes, {graph.graph.number_of_edges()} edges

For each area that needs improvement (especially warnings and failures), suggest:
1. area: What audit dimension or aspect needs attention
2. suggestion: A concrete, specific action to address the issue
3. priority: 'high', 'medium', or 'low'

Return up to 5 improvement suggestions.
""")

            result = simple_chat_model.with_structured_output(List[ImprovementSuggestion]).invoke([prompt])
            if isinstance(result, list):
                suggestions = []
                for item in result:
                    if isinstance(item, ImprovementSuggestion):
                        suggestions.append({
                            "area": item.area,
                            "suggestion": item.suggestion,
                            "priority": item.priority
                        })
                return suggestions
        except Exception as e:
            logger.warning(f"AI improvement suggestions failed: {e}")
        return []