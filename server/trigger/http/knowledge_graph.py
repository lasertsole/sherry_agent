"""Knowledge-graph HTTP endpoint.

Serves the LightRAG knowledge graph (stored in SNKV SQLite) to the client's
knowledge-graph page. The graph is produced by the multimodal_rag skill and read
through the shared LightRAG singleton exposed by ``graph_rag.get_lightrag``.

Endpoint:
    GET /knowledge-graph?node_label=*&max_depth=3&max_nodes=1000
        -> {"nodes": [...], "edges": [...], "is_truncated": bool}
"""
from loguru import logger

from server.trigger.core import app


@app.get("/knowledge-graph")
async def knowledge_graph_handler(request):
    """Return the LightRAG knowledge graph as a serializable JSON payload.

    Query params (all optional):
        node_label (str): seed node label to expand from; default "*" (all nodes).
        max_depth  (int): BFS expansion depth limit; default 3.
        max_nodes  (int): maximum number of nodes to return; default 1000.
    """
    query = request.query_params

    node_label = query.get("node_label", "*")
    try:
        max_depth = max(0, int(query.get("max_depth", 3)))
    except (TypeError, ValueError):
        max_depth = 3
    try:
        max_nodes = max(1, int(query.get("max_nodes", 1000)))
    except (TypeError, ValueError):
        max_nodes = 1000

    try:
        from skills.builtin.core.multimodal_rag.scripts.graph_rag import get_lightrag

        lightrag = await get_lightrag()
        graph = await lightrag.get_knowledge_graph(
            node_label=node_label,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

        nodes = [
            {
                "id": n.id,
                "labels": list(n.labels or []),
                "properties": n.properties or {},
            }
            for n in graph.nodes
        ]
        edges = [
            {
                "id": e.id,
                "type": e.type,
                "source": e.source,
                "target": e.target,
                "properties": e.properties or {},
            }
            for e in graph.edges
        ]

        logger.info(
            "Served knowledge graph: node_label=%s, max_depth=%s, max_nodes=%s, "
            "nodes=%d, edges=%d, truncated=%s",
            node_label,
            max_depth,
            max_nodes,
            len(nodes),
            len(edges),
            graph.is_truncated,
        )
        return {
            "nodes": nodes,
            "edges": edges,
            "is_truncated": bool(graph.is_truncated),
        }
    except Exception as e:  # noqa: BLE001 - surface any backend failure cleanly
        logger.exception("Knowledge graph request failed")
        return {"error": str(e), "nodes": [], "edges": [], "is_truncated": False}
