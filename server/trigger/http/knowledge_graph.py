"""Knowledge-graph HTTP endpoint.

Serves the LightRAG knowledge graph (stored in SNKV SQLite) to the client's
knowledge-graph page. The graph is produced by the multimodal_rag skill and read
through the shared LightRAG singleton exposed by ``graph_rag.get_lightrag``.

Endpoints:
    GET  /knowledge-graph?node_label=*&max_depth=3&max_nodes=1000
        -> {"nodes": [...], "edges": [...], "is_truncated": bool}

    POST /knowledge-graph/upload  (multipart: file(s))
        -> {"success": bool, "message": str, "files": [{name, ok, error} | ...]}
"""

from pathlib import Path
from typing import Any
from uuid import uuid4

from config import SRC_DIR
from loguru import logger
from robyn import Response

from server.trigger.core import app

# File extensions accepted for knowledge-graph ingestion.
# RAG-Anything (mineru/fallback_txt parsers) natively supports these.
_ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}


def _json_response(status_code: int, payload: dict[str, Any]):
    """Robyn Response pairing a raw JSON string with the JSON content type.

    Robyn's `description` body is a string, so we serialize the dict ourselves.
    """
    import json

    return Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload, ensure_ascii=False),
    )


@app.post("/knowledge-graph/upload")
async def knowledge_graph_upload_handler(request):
    """Accept one or more document files and ingest them into the knowledge graph.

    Request: multipart/form-data, each file part carries a file name. Robyn keys
    ``request.files`` by the multipart *filename* (mirroring ``POST /skills/upload``).

    Each uploaded document is staged to ``src/rag/uploads/<uuid>/`` and ingested via
    the multimodal_rag ``file_index`` pipeline (RAG-Anything ``process_document_complete``
    with ``parse_method="auto"``), which parses PDF/docx/txt and writes entities into the
    same LightRAG store that ``GET /knowledge-graph`` reads from.

    Response: ``{"success": bool, "message": str, "files": [{name, ok, error}]}``.
    """
    files = getattr(request, "files", None)
    if not files:
        return _json_response(
            400,
            {"success": False, "message": "No file uploaded", "files": []},
        )

    results = []
    try:
        for filename, file_info in files.items():
            name = str(filename)
            ext = Path(name).suffix.lower()
            if ext not in _ALLOWED_EXT:
                results.append(
                    {"name": name, "ok": False, "error": f"Unsupported file type '{ext}'"}
                )
                logger.warning("Knowledge-graph upload rejected unsupported type: %s", name)
                continue

            # Stage the uploaded bytes to a unique temp location.
            try:
                blob = file_info
                data = blob if isinstance(blob, bytes) else getattr(blob, "file", b"")
                if isinstance(data, str):
                    data = data.encode("utf-8")
            except Exception as e:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": f"Read error: {e}"})
                continue
            if not data:
                results.append({"name": name, "ok": False, "error": "Empty file"})
                continue

            stage_dir = SRC_DIR / "rag" / "uploads" / uuid4().hex
            stage_dir.mkdir(parents=True, exist_ok=True)
            staged = stage_dir / name
            staged.write_bytes(data)

            try:
                from skills.builtin.core.multimodal_rag.scripts import file_index

                await file_index(str(staged), classify_folder="uploads")
                results.append({"name": name, "ok": True, "error": None})
                logger.info("Knowledge-graph upload ingested: %s", name)
            except Exception as e:  # noqa: BLE001 - surface any backend failure cleanly
                logger.exception("Knowledge-graph ingestion failed for %s", name)
                results.append({"name": name, "ok": False, "error": str(e)})
            finally:
                # Best-effort cleanup of the staged temp file.
                try:
                    for p in stage_dir.rglob("*"):
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    stage_dir.rmdir()
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        logger.exception("Knowledge-graph upload failed")
        return _json_response(
            500,
            {"success": False, "message": str(e), "files": results},
        )

    ok_count = sum(1 for r in results if r["ok"])
    success = ok_count > 0
    message = (
        f"Ingested {ok_count} file(s) into the knowledge graph"
        if success
        else "No files could be ingested"
    )
    return _json_response(
        200 if success else 400,
        {"success": success, "message": message, "files": results},
    )


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
