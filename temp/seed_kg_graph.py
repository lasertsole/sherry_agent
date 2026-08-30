"""Seed the knowledge-graph store with a small deterministic demo graph.

Reuses the exact working_dir / storage classes / namespace the backend's
``GET /knowledge-graph`` reads (via ``graph_rag.get_lightrag``), but constructs
the LightRAG directly with ``None`` model funcs so *no* LLM / embedding / rerank
model is loaded — only the SNKV SQLite stores are opened. Pure graph-storage
writes never invoke the model callbacks, so this is safe and fast.

Usage:
    python temp/seed_kg_graph.py            # seed (idempotent, upserts)
    python temp/seed_kg_graph.py --clear    # wipe nodes/edges/adj

After seeding, reload http://localhost:3000/knowledge-graph (or click refresh).
"""
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
# graph_rag package lives under the multimodal_rag skill's scripts dir.
sys.path.insert(
    0,
    str(
        PROJECT_ROOT
        / "skills"
        / "builtin"
        / "core"
        / "multimodal_rag"
        / "scripts"
    ),
)


async def main() -> None:
    # Mirror graph_rag/base.get_lightrag config, minus the model funcs. None
    # model funcs are safe here because pure graph-storage writes never invoke
    # them. The vector storage validates an EmbeddingFunc is present at build
    # time, so we supply an inert one (never actually called).
    from config import SRC_DIR
    from graph_rag.vendored_lightrag import LightRAG
    from graph_rag.vendored_lightrag.utils import EmbeddingFunc
    from graph_rag.vendored_lightrag.kg.shared_storage import initialize_pipeline_status

    working_dir = (SRC_DIR / "rag" / "store").resolve().as_posix()
    print(f"working_dir = {working_dir}")

    inert_embed = EmbeddingFunc(
        embedding_dim=1024,
        max_token_size=8192,
        func=lambda *a, **k: None,
    )

    # Constructor requires a non-None llm_model_func (wrapped into per-role
    # wrappers at build time), but pure graph-storage writes never call it.
    async def inert_llm(*args, **kwargs):  # noqa: D103
        return ""

    lightrag = LightRAG(
        working_dir=working_dir,
        llm_model_func=inert_llm,
        embedding_func=inert_embed,
        rerank_model_func=None,
        kv_storage="SNKVKVStorage",
        vector_storage="SNKVVectorStorage",
        graph_storage="SNKVGraphStorage",
        doc_status_storage="SNKVDocStatusStorage",
    )

    await initialize_pipeline_status(workspace=lightrag.workspace)
    await lightrag.initialize_storages()

    g = lightrag.chunk_entity_relation_graph

    clear = "--clear" in sys.argv
    if clear:
        await g.drop()
        print("Graph cleared.")
        await g.index_done_callback()
        return

    # ---- Sherry-detective themed demo graph spanning multiple entity types ----
    # Each node carries LightRAG meta-fields (description / source_id / file_path)
    # so the detail pane's 内容 + 来源文档 sections render with real data.
    nodes = [
        # (id, data)
        (
            "sherry",
            {
                "id": "sherry",
                "entity_type": "person",
                "name": "Sherry",
                "role": "detective",
                "age": 22,
                "description": "Sherry is the city's most gifted young detective, known for her razor-sharp logic, a playful charming surface, and a cold analytical core when on a case. She pairs intuition with methodical deduction and has a near-photographic memory for details.",
                "source_id": "d0e7a1c2-case-file",
                "file_path": "files/case-files/sherry-profile.txt",
            },
        ),
        (
            "juxueli",
            {
                "id": "juxueli",
                "entity_type": "person",
                "name": "Ju Xue Li",
                "role": "colleague",
                "description": "A steady, observant detective partner who documents every scene with care. Her calm temperament balances Sherry's impulsive leaps and she keeps the case file meticulous.",
                "source_id": "f3b2d9e1-partner-log",
                "file_path": "files/case-files/partner-log.txt",
            },
        ),
        (
            "yuanye",
            {
                "id": "yuanye",
                "entity_type": "person",
                "name": "Yuan Ye Han Na",
                "role": "informant",
                "description": "A streetwise informant from the Wharf District who trades rumors for protection. Vagely reliable and always listening, she is the eyes and ears of the docks.",
                "source_id": "a9c8e7d4-informant",
                "file_path": "files/case-files/informant.txt",
            },
        ),
        (
            "police_dept",
            {
                "id": "police_dept",
                "entity_type": "organization",
                "name": "City Police Dept",
                "dept": "homicide",
                "description": "The department responsible for the Shell Case investigation. Its homicide unit oversees the evidence chain and coordinates field detectives.",
                "source_id": "b1e2f3c5-org",
                "file_path": "files/org/police-dept.txt",
            },
        ),
        (
            "harbor_city",
            {
                "id": "harbor_city",
                "entity_type": "location",
                "name": "Harbor City",
                "type": "city",
                "description": "A foggy seaside metropolis where the incident unfolds. Business and smuggling blur at the waterline, and the truth often hides in plain sight.",
                "source_id": "c4d5e6f7-city",
                "file_path": "files/locations/harbor-city.txt",
            },
        ),
        ("wharf_district", {"id": "wharf_district", "entity_type": "location", "name": "Wharf District", "type": "district"}),
        (
            "shell_case",
            {
                "id": "shell_case",
                "entity_type": "event",
                "name": "The Shell Case",
                "status": "open",
                "description": "An active murder investigation centered on a washed-up shells container found at the wharf. The crime has no obvious motive, only layered deception.",
                "source_id": "d0e7a1c2-case-file",
                "file_path": "files/case-files/shell-case.txt",
            },
        ),
        (
            "deception",
            {
                "id": "deception",
                "entity_type": "concept",
                "name": "Deception",
                "nature": "art",
                "description": "The art of misleading with layered lies. The core intellectual theme of the case, where every suspect tells a half-truth.",
                "source_id": "e8f9a0b1-theme",
                "file_path": "files/themes/deception.txt",
            },
        ),
        ("investigation", {"id": "investigation", "entity_type": "task", "name": "Investigation", "phase": "active"}),
        ("deduction", {"id": "deduction", "entity_type": "skill", "name": "Deduction", "level": "expert"}),
        ("magnifier", {"id": "magnifier", "entity_type": "tool", "name": "Magnifier", "owner": "sherry"}),
    ]

    edges = [
        # (src, tgt, data)
        ("sherry", "juxueli", {"relation_type": "partners", "keywords": "partner", "weight": 3}),
        ("sherry", "yuanye", {"relation_type": "knows", "keywords": "acquaintance", "weight": 2}),
        ("sherry", "police_dept", {"relation_type": "works_at", "keywords": "affiliation", "weight": 2}),
        ("sherry", "shell_case", {"relation_type": "investigates", "keywords": "case", "weight": 4}),
        ("sherry", "harbor_city", {"relation_type": "resides_in", "keywords": "location", "weight": 1}),
        ("sherry", "deception", {"relation_type": "expert_in", "keywords": "skill", "weight": 3}),
        ("sherry", "deduction", {"relation_type": "masters", "keywords": "skill", "weight": 3}),
        ("sherry", "magnifier", {"relation_type": "uses", "keywords": "tool", "weight": 1}),
        ("juxueli", "police_dept", {"relation_type": "works_at", "keywords": "affiliation", "weight": 2}),
        ("yuanye", "wharf_district", {"relation_type": "lives_in", "keywords": "location", "weight": 2}),
        ("shell_case", "wharf_district", {"relation_type": "occurs_in", "keywords": "location", "weight": 2}),
        ("shell_case", "deception", {"relation_type": "theme", "keywords": "concept", "weight": 2}),
        ("investigation", "shell_case", {"relation_type": "tracks", "keywords": "case", "weight": 3}),
        ("investigation", "deception", {"relation_type": "probes", "keywords": "concept", "weight": 2}),
        ("deduction", "investigation", {"relation_type": "applies_to", "keywords": "task", "weight": 2}),
    ]

    # Serialize node/edge data to JSON-ish dicts exactly as upsert expects.
    node_rows = [(nid, dict(data)) for nid, data in nodes]
    edge_rows = [(s, t, dict(d)) for s, t, d in edges]

    await g.upsert_nodes_batch(node_rows)
    await g.upsert_edges_batch(edge_rows)
    await g.index_done_callback()

    print(f"Seeded {len(node_rows)} nodes, {len(edge_rows)} edges.")


if __name__ == "__main__":
    asyncio.run(main())
