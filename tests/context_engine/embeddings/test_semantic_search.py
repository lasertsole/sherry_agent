"""Vector semantic search over MesMemory (stubbed embedding backend).

The embed function is replaced with a deterministic stub so the tests pin the
index/retrieve/rank contract without loading the real embed model.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import context_engine.embeddings.indexer as indexer
from context_engine.embeddings import semantic_search
from context_engine.store import add_messages
from context_engine.store import db as store_db

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    import context_engine.store.core as core

    monkeypatch.setattr(core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def stub_embed(monkeypatch):
    """Deterministic embedder: topic keywords map to orthogonal vectors."""

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0, 0.0, 0.0, 0.0]
            lowered = text.lower()
            if "docker" in lowered:
                vector[0] = 1.0
            if "kubernetes" in lowered:
                vector[1] = 1.0
            if "vacation" in lowered:
                vector[2] = 1.0
            if "recipe" in lowered:
                vector[3] = 1.0
            vectors.append(vector)
        return vectors

    monkeypatch.setattr(indexer, "_embed_fn", embed)
    return embed


async def test_semantic_search_ranks_by_topic_similarity(isolated_db, stub_embed):
    # Given messages about distinct topics.
    sid = "p25-rank"
    await add_messages(sid, [HumanMessage(content="fixed the docker networking issue")])
    await add_messages(sid, [AIMessage(content="planned the kubernetes migration")])
    await add_messages(sid, [HumanMessage(content="booked the vacation in okinawa")])

    # When searching semantically for docker-related content.
    matches = await semantic_search("docker networking", limit=3)

    # Then the docker message ranks first with the highest score.
    assert matches, "no semantic matches returned"
    assert "docker" in str(matches[0]["content"]).lower()
    scores = [m["score"] for m in matches]
    assert scores == sorted(scores, reverse=True)


async def test_index_is_idempotent(isolated_db, stub_embed):
    sid = "p25-idem"
    batch = [HumanMessage(content="docker compose up")]
    await add_messages(sid, batch)

    from context_engine.store.db import get_db

    # When semantic search runs (it self-heals the index first).
    again = await semantic_search("docker compose", limit=5)

    # Then exactly one embedding exists and re-indexing did not duplicate it.
    total = (
        get_db()
        .execute("SELECT COUNT(*) FROM message_embeddings WHERE session_id = ?", (sid,))
        .fetchone()[0]
    )
    assert total == 1
    assert again


async def test_session_scoped_search_excludes_other_sessions(isolated_db, stub_embed):
    await add_messages("p25-scope-a", [HumanMessage(content="docker issue resolved")])
    await add_messages("p25-scope-b", [HumanMessage(content="docker issue resolved")])

    matches = await semantic_search("docker", session_id="p25-scope-a", limit=5)

    assert matches
    assert all(m["session_id"] == "p25-scope-a" for m in matches)
