"""
AGoT Checkpoint Manager - 支持 8 个 Stage 的断点续跑

保存/加载策略：
- 使用 networkx.node_link_data() 完整序列化 DiGraph（含所有节点/边属性）
- hyperedges/layers/ibns 单独 JSON 序列化
- 上下文和 reasoning_trace 单独保存
- 每个 Session 一个子目录，以 session_id 命名
- 全部 Stage 执行完成后自动清除存档
"""

import os
import json
import shutil
import networkx as nx
from loguru import logger
from tests.ast_got.models.graph import AGoTGraph
from tests.ast_got.models.hyperedge import Hyperedge


# 存档根目录（与 test 文件同目录）
CHECKPOINT_DIR = os.path.join(os.path.dirname(__file__), ".checkpoints")


class CheckpointManager:
    """断点管理器：保存/加载/清除 Stage 执行状态"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = os.path.join(CHECKPOINT_DIR, session_id)

    # -----------------------------------------------------------
    # 保存
    # -----------------------------------------------------------

    def save(self, stage_index: int, graph: AGoTGraph,
             context: dict, reasoning_trace: list) -> bool:
        """保存 Stage N 完成后的完整执行状态"""
        try:
            os.makedirs(self.session_dir, exist_ok=True)

            # 1) 图结构：networkx 原生序列化（保留所有属性）
            graph_data = nx.node_link_data(graph.graph)
            self._write_json("graph.json", graph_data)

            # 2) Hyperedges
            hyperedge_data = {
                eid: h.to_dict() for eid, h in graph.hyperedges.items()
            }
            self._write_json("hyperedges.json", hyperedge_data)

            # 3) Layers（set → list 以 JSON 兼容）
            layers_data = {
                lid: list(nodes) for lid, nodes in graph.layers.items()
            }
            self._write_json("layers.json", layers_data)

            # 4) IBNs
            self._write_json("ibns.json", list(graph.ibns))

            # 5) 上下文（含不可序列化字段的降级处理）
            self._write_json("context.json", context)

            # 6) 推理轨迹
            self._write_json("reasoning_trace.json", reasoning_trace)

            # 7) 元信息
            self._write_json("meta.json", {
                "session_id": self.session_id,
                "stage_index": stage_index,
                "total_stages": 8
            })

            logger.info(
                f"✅ Checkpoint saved: session={self.session_id[:8]}… "
                f"stage={stage_index + 1}/8"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save checkpoint: {e}")
            return False

    # -----------------------------------------------------------
    # 加载
    # -----------------------------------------------------------

    def load(self) -> dict | None:
        """加载断点，返回 None 表示没有有效断点"""
        if not self._has_valid_checkpoint():
            return None

        try:
            meta = self._read_json("meta.json")
            context = self._read_json("context.json")
            reasoning_trace = self._read_json("reasoning_trace.json")

            # 重建图
            graph = self._rebuild_graph()

            stage_index = meta["stage_index"]
            logger.info(
                f"🔁 Checkpoint loaded: session={self.session_id[:8]}… "
                f"resuming from Stage {stage_index + 2}/8"
            )

            return {
                "stage_index": stage_index,      # 最后完成的 Stage 索引
                "graph": graph,
                "context": context,
                "reasoning_trace": reasoning_trace,
            }

        except Exception as e:
            logger.error(f"❌ Failed to load checkpoint, starting fresh: {e}")
            return None

    def exists(self) -> bool:
        """判断是否存在有效断点"""
        return self._has_valid_checkpoint()

    # -----------------------------------------------------------
    # 清除
    # -----------------------------------------------------------

    def clear(self):
        """全部 Stage 完成后删除存档"""
        if os.path.exists(self.session_dir):
            shutil.rmtree(self.session_dir)
            logger.info(
                f"🧹 Checkpoint cleared: session={self.session_id[:8]}…"
            )

    # -----------------------------------------------------------
    # 内部方法
    # -----------------------------------------------------------

    def _has_valid_checkpoint(self) -> bool:
        """检查所有必需的存档文件是否存在"""
        if not os.path.exists(self.session_dir):
            return False
        required = [
            "meta.json", "graph.json", "hyperedges.json",
            "layers.json", "ibns.json", "context.json"
        ]
        return all(
            os.path.isfile(os.path.join(self.session_dir, f))
            for f in required
        )

    def _rebuild_graph(self) -> AGoTGraph:
        """从存档文件重建完整的 AGoTGraph 对象"""
        graph = AGoTGraph()

        # 1) 重建 DiGraph
        graph_data = self._read_json("graph.json")
        graph.graph = nx.node_link_graph(graph_data, directed=True)

        # 2) 重建 Hyperedges
        hyperedge_data = self._read_json("hyperedges.json")
        for eid, hdata in hyperedge_data.items():
            graph.hyperedges[eid] = Hyperedge.from_dict(hdata.copy())

        # 3) 重建 Layers（list → set）
        layers_data = self._read_json("layers.json")
        graph.layers = {lid: set(nodes) for lid, nodes in layers_data.items()}

        # 4) 重建 IBNs
        ibns_data = self._read_json("ibns.json")
        graph.ibns = set(ibns_data)

        return graph

    def _write_json(self, filename: str, data) -> None:
        """安全写入 JSON 文件"""
        path = os.path.join(self.session_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def _read_json(self, filename: str):
        """安全读取 JSON 文件"""
        path = os.path.join(self.session_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
