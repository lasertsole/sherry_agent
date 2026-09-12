"""Real-LLM e2e: multimodal_rag graph pipeline (index → query).

Runs the unmocked production path end to end on a purpose-built text document:
FallbackTxtParser → LightRAG entity/relation extraction (auxiliary LLM) →
SNKV storages → multi-hop aquery (local bge-m3 embeddings + configured reranker).

The pipeline's SRC_DIR binding is redirected into the test's tmp dir, so no
index artifacts touch `src/rag/`. Deselected by default; run explicitly in a
dedicated job (costs tokens):

    uv run pytest tests/skills/builtin/core/multimodal_rag/test_rag_e2e.py -m llm_e2e
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import graph_rag.base as graph_rag_base  # noqa: E402
import graph_rag.core as graph_rag_core  # noqa: E402
import rag_index as rag_index_module  # noqa: E402
from rag_index import file_index  # noqa: E402
from rag_query import query  # noqa: E402

pytestmark = [pytest.mark.llm_e2e]

_TIMEOUT_S = 600

_SAMPLE_DOC = """Zenith Dynamics is a robotics company founded by Mira Chen in 2019, headquartered in Shenzhen.
Mira Chen started Zenith Dynamics after leaving DeepPath Labs, where she led the manipulation team.
The company's flagship product, the Atlas-9 warehouse robot, ships to 40 logistics sites worldwide.
Founder Mira Chen remains CEO of Zenith Dynamics today.
"""


@pytest.mark.timeout(_TIMEOUT_S)
@pytest.mark.asyncio
async def test_text_doc_index_then_query_returns_grounded_answer(tmp_path, monkeypatch):
    """Given an isolated SRC_DIR and a small text doc,
    when the doc is indexed and the graph is queried about a stated fact,
    then the answer names the fact's subject and carries no pipeline error marker.
    """
    fake_src = tmp_path / "src"
    for module in (graph_rag_core, graph_rag_base, rag_index_module):
        monkeypatch.setattr(module, "SRC_DIR", fake_src)

    doc_path = tmp_path / "zenith_dynamics.txt"
    doc_path.write_text(_SAMPLE_DOC, encoding="utf-8")

    await file_index(str(doc_path), "e2e_smoke", parser="fallback_txt")
    answer = await query("Who founded Zenith Dynamics?", parser="fallback_txt")

    assert answer, "query returned an empty answer"
    assert not answer.startswith("[Error]"), f"pipeline failed: {answer}"
    assert "Mira Chen" in answer, f"answer is not grounded in the indexed fact: {answer}"
