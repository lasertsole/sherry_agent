import logging
import numpy as np
from .store import PersistentGraph
from typing import List, Tuple, Dict
from models.embed_model.core import embed_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger("MultiHopGraphRAG")

class DynamicGraphBuilder:
    def __init__(self, db_path: str = ":memory:"):
        self.documents: List[str] = []
        self.edges: List[Tuple[int, int, str]] = []
        self._embeddings: List[np.ndarray] | None = None
        self._is_built = False
        self.db_path = db_path

    def add_document(self, text: str) -> int:
        """
        Add a single document. Returns the node_id assigned to this document.
        """
        node_id = len(self.documents)
        self.documents.append(text)
        self._is_built = False
        logger.debug(f"Added document with node_id={node_id}")
        return node_id

    def add_documents(self, texts: List[str]) -> List[int]:
        """
        Add multiple documents. Returns list of node_ids.
        """
        start_id = len(self.documents)
        node_ids = []
        for text in texts:
            node_id = self.add_document(text)
            node_ids.append(node_id)
        logger.info(f"Added {len(texts)} documents, node_ids: {start_id} to {len(self.documents) - 1}")
        return node_ids

    def add_edge(self, source_id: int, target_id: int, relation: str):
        """
        Add a single edge between two nodes.
        Note: Nodes must be added before adding edges.
        """
        if source_id >= len(self.documents) or target_id >= len(self.documents):
            raise ValueError(
                f"Node IDs out of range. source_id={source_id}, target_id={target_id}, "
                f"but only {len(self.documents)} documents added."
            )
        self.edges.append((source_id, target_id, relation))
        self._is_built = False
        logger.debug(f"Added edge: {source_id} -> {target_id} ({relation})")

    def add_edges(self, edge_list: List[Tuple[int, int, str]]):
        """
        Add multiple edges at once.
        """
        for src, tgt, rel in edge_list:
            self.add_edge(src, tgt, rel)
        logger.info(f"Added {len(edge_list)} edges")

    def _compute_embeddings(self):
        """
        Compute embeddings for all documents. Called automatically during build().
        """
        if self._embeddings is not None and not self._is_built:
            return

        if not self.documents:
            raise ValueError("No documents added. Call add_document() or add_documents() first.")

        logger.info(f"Computing embeddings for {len(self.documents)} documents...")
        self._embeddings = embed_model.embed_documents(self.documents)
        logger.info("Embeddings computed successfully")

    def build(self) -> PersistentGraph:
        """
        Build the PersistentGraph from added documents and edges.
        This method can be called multiple times after modifications.
        """
        self._compute_embeddings()

        graph = PersistentGraph()

        # Add all nodes
        for i, (doc, emb) in enumerate(zip(self.documents, self._embeddings)):
            graph.add_node(i, doc, np.array(emb))

        # Add all edges
        for src, tgt, rel in self.edges:
            try:
                graph.add_edge(src, tgt, rel)
            except ValueError as e:
                logger.warning(f"Skipping invalid edge ({src}, {tgt}, {rel}): {e}")

        graph.build_embedding_index()
        self._is_built = True

        stats = graph.get_stats()
        logger.info(
            f"Dynamic graph built: {stats['num_nodes']} nodes, {stats['num_edges']} edges"
        )
        return graph

    def clear(self):
        """
        Clear all documents and edges. Start fresh.
        """
        self.documents.clear()
        self.edges.clear()
        self._embeddings = None
        self._is_built = False
        logger.info("Graph builder cleared")

    def get_stats(self) -> Dict:
        """
        Get current statistics about the builder state.
        """
        return {
            "num_documents": len(self.documents),
            "num_edges": len(self.edges),
            "is_built": self._is_built,
        }
