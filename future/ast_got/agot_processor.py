"""
AGoT Processor - Independent implementation of ASR-GoT algorithm
支持 8 Stage 断点续跑：哪步意外停止，就从哪步继续
完成后自动清除存档，避免污染下次从头执行
"""
import json
import hashlib
from typing import Any
from loguru import logger
from .checkpoint_manager import CheckpointManager
from .utils.visualization import visualize_graph, visualize_layers


class AGoTProcessor:
    """
    AGoT Processor - Independent implementation of the 8-stage reasoning pipeline.
    支持断点续跑，每步完成后自动存档。
    """

    def __init__(self):
        self.session_graphs = {}

        from .stages.stage_1_initialization import InitializationStage
        from .stages.stage_2_decomposition import DecompositionStage
        from .stages.stage_3_hypothesis import HypothesisStage
        from .stages.stage_4_evidence import EvidenceStage
        from .stages.stage_5_pruning import PruningStage
        from .stages.stage_6_subgraph import SubgraphStage
        from .stages.stage_7_composition import CompositionStage
        from .stages.stage_8_reflection import ReflectionStage
        from .models.graph import AGoTGraph

        self.AGoTGraph = AGoTGraph
        self.stages = [
            InitializationStage(),
            DecompositionStage(),
            HypothesisStage(),
            EvidenceStage(),
            PruningStage(),
            SubgraphStage(),
            CompositionStage(),
            ReflectionStage()
        ]

        logger.info("AGoT Processor initialized with 8 stages")

    def process_query(self, query: str, context: dict[str, Any] = None,
                      parameters: dict[str, Any] = None) -> dict[str, Any]:
        """
        Process a query through the full 8-stage AGoT pipeline.
        支持断点续跑：检测存档 -> 跳过已完成 Stage -> 每步存档 -> 完成后清理
        """
        logger.info(f"Processing query: {query[:50]}...")

        context = context or {}
        parameters = parameters or {}

        # ---- 生成确定性 Session ID（相同 query+参数 共享存档） ----
        session_id = self._make_session_id(query, parameters)
        checkpointer = CheckpointManager(session_id)

        # ---- 检测断点 ----
        checkpoint_data = checkpointer.load()
        if checkpoint_data:
            graph = checkpoint_data["graph"]
            stage_context = checkpoint_data["context"]
            reasoning_trace = checkpoint_data["reasoning_trace"]
            start_stage = checkpoint_data["stage_index"] + 1
            logger.info(
                f"Found checkpoint, resuming from Stage {start_stage + 1}/8 "
                f"(session={session_id[:8]}...)"
            )
        else:
            graph = self.AGoTGraph()
            reasoning_trace = []
            start_stage = 0
            stage_context = {
                "query": query,
                "context": context,
                "parameters": parameters,
                "session_id": session_id,
            }
            logger.info(f"Starting fresh (session={session_id[:8]}...)")

        self.session_graphs[session_id] = graph
        output_dir = parameters.get("output_dir", "output")

        # ---- 从断点处继续执行 ----
        for i in range(start_stage, len(self.stages)):
            stage = self.stages[i]
            logger.info(f"Executing Stage {i + 1}: {stage.__class__.__name__}")

            result = stage.execute(graph, stage_context)

            stage_name = f"Stage{i + 1}_{stage.__class__.__name__}"
            visualize_graph(
                graph.graph, graph.hyperedges, stage_name, output_dir=output_dir
            )
            if graph.layers:
                visualize_layers(
                    graph.graph, graph.layers, stage_name, output_dir=output_dir
                )

            reasoning_trace.append({
                "stage": i + 1,
                "name": stage.__class__.__name__,
                "summary": result.get("summary", ""),
                "metrics": result.get("metrics", {}),
            })

            stage_context.update(result)
            print(f"Stage {i + 1} stage_context: {stage_context}")

            # ---- 每一步执行完后写存档 ----
            checkpointer.save(i, graph, stage_context, reasoning_trace)

        # ---- 全部 8 步完成 -> 清除存档 ----
        checkpointer.clear()

        # ---- 构建最终结果 ----
        final_confidence = stage_context.get("final_confidence")
        if final_confidence is None:
            logger.warning(
                "Final confidence not found. Defaulting to [0.5, 0.5, 0.5, 0.5]."
            )
            final_confidence = [0.5, 0.5, 0.5, 0.5]

        final_result = {
            "result": stage_context.get("composition_result", {}),
            "reasoning_trace": reasoning_trace,
            "confidence": final_confidence,
            "graph_state": self.get_graph_state(session_id),
            "processor": "AGoT",
            "uses_original": False,
        }

        logger.info(f"All 8 stages completed for session {session_id[:8]}...")
        return final_result

    def extract_thinking_result(self, final_result: dict[str, Any]) -> str:
        """
        Extract a clean, human-readable thinking result string from the AGoT output.
        
        This method converts the complex dictionary output into a simple string
        that can be directly used as the final thinking result for AI models.
        
        IMPORTANT: Only includes validated conclusions. Pruned/eliminated paths are excluded.
        The output is ULTRA-MINIMALIST - only the core insight and confidence.
        
        Args:
            final_result: The output from process_query()
            
        Returns:
            A formatted string containing ONLY the key insights and validated conclusions
        """
        try:
            composition = final_result.get("result", {})
            if not composition:
                return "No analysis result available."
            
            # Build the thinking result string - ULTRA MINIMALIST
            lines = []
            
            # Executive Summary (THE most important part - contains all validated conclusions)
            sections = composition.get("sections", [])
            for section in sections:
                if section.get("type") == "summary":
                    lines.append(section.get('content', '').strip())
                    break
            
            # Confidence (compact format - single line)
            confidence = final_result.get("confidence", [])
            if confidence and len(confidence) == 4:
                avg_conf = sum(confidence) / len(confidence)
                lines.append(f"\n\n[Confidence: {avg_conf:.0%}]")
            
            return "\n".join(lines)
            
        except Exception as e:
            logger.error(f"Failed to extract thinking result: {e}")
            return f"Error extracting thinking result: {str(e)}"

    def _make_session_id(self, query: str, parameters: dict) -> str:
        """生成确定性 Session ID，使相同 query 可以复用断点"""
        raw = f"{query}:{json.dumps(parameters, sort_keys=True, default=str)}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get_graph_state(self, session_id: str) -> dict[str, Any]:
        """获取指定 Session 的图当前状态"""
        if session_id not in self.session_graphs:
            raise ValueError(f"Session {session_id} not found")
        graph = self.session_graphs[session_id]
        return graph.to_dict()

    def incorporate_feedback(self, session_id: str, feedback: dict[str, Any]) -> None:
        """
        Incorporate user feedback to refine the graph.
        """
        if session_id not in self.session_graphs:
            raise ValueError(f"Session {session_id} not found")

        graph = self.session_graphs[session_id]

        node_id = feedback.get("node_id")
        edge_id = feedback.get("edge_id")
        feedback_type = feedback.get("type")
        feedback_value = feedback.get("value")

        if node_id and feedback_type == "confidence":
            graph.update_node_confidence(node_id, feedback_value)
        elif edge_id and feedback_type == "confidence":
            graph.update_edge_confidence(edge_id, feedback_value)

        logger.info(f"Feedback incorporated for session {session_id}")
