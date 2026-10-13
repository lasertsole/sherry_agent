"""Real-LLM e2e: multimodal_rag graph pipeline (index → query).

Runs the unmocked production path end to end on a purpose-built text document:
FallbackTxtParser → LightRAG entity/relation extraction (auxiliary LLM) →
SNKV storages → multi-hop aquery (local bge-m3 embeddings + configured reranker).

The pipeline's SRC_DIR binding is redirected into the test's tmp dir, so no
index artifacts touch `src/rag/`.

Environment preconditions — the test SKIPS (never fails) when they are unmet:
  * The embedding backend must be usable. With the default
    ``EMBEDDING_MODEL_LOCAL=true`` this requires the real ``llama-cpp-python``
    distribution, specifically ``llama_cpp.Llama.from_pretrained``, which the
    embedder calls to fetch the bge-m3 GGUF. The llm-e2e CI profile syncs with
    ``--no-install-package llama-cpp-python``, so the local embedding runtime is
    absent there and this test is skipped. A remote embedding backend
    (``EMBEDDING_MODEL_LOCAL=false``) needs no local runtime.
  * Entity extraction needs a reachable auxiliary LLM (cloud API by default, or
    a local GGUF model).
The reranker is optional: LightRAG degrades rerank failures to the original
retrieved chunks (logs and continues).
The precondition is probed against the real capability the pipeline consumes
(see ``_local_embedding_runtime_unavailable``); assertions below are never
relaxed when the precondition holds.

Deselected by default; run explicitly in a dedicated job (costs tokens):

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

pytestmark = [pytest.mark.llm_e2e, pytest.mark.integration]

_TIMEOUT_S = 600

_SAMPLE_DOC = """Zenith Dynamics is a robotics company founded by Mira Chen in 2019, headquartered in Shenzhen.
Mira Chen started Zenith Dynamics after leaving DeepPath Labs, where she led the manipulation team.
The company's flagship product, the Atlas-9 warehouse robot, ships to 40 logistics sites worldwide.
Founder Mira Chen remains CEO of Zenith Dynamics today.
"""


def _local_embedding_runtime_unavailable() -> str | None:
    """Return a skip reason when the configured embedding backend cannot run.

    Binds to the real premise instead of an environment-flag heuristic: the
    pipeline builds its embedder through ``models.build_embed_model()``, whose
    backend is chosen by ``EMBEDDING_MODEL_LOCAL`` (default: local bge-m3 GGUF
    via llama-cpp-python). When the local backend is selected, the importable
    ``llama_cpp.Llama`` must expose ``from_pretrained`` — the classmethod
    ``models.embed_model.core`` calls to fetch the GGUF from Hugging Face.

    Two recognizable conditions falsify that premise (no blanket ``except``):

    * ``llama_cpp`` cannot be imported at all (``ModuleNotFoundError``);
    * ``llama_cpp.Llama`` imports but lacks ``from_pretrained``. That is the
      case when a hermetic test module's bare ``llama_cpp`` stub shadows the
      absent real distribution: ``tests/agent/test_stream_repetition_guard_wrapper.py``
      and ``tests/agent/middlewares/test_output_repetition_guard.py`` each
      register ``Llama = type("Llama", (), {})`` into ``sys.modules`` at
      collection time, and ``setdefault`` lets that stub win whenever the real
      package is not installed.

    Returns ``None`` when the premise holds, including a remote embedding
    backend (``EMBEDDING_MODEL_LOCAL=false``), which needs no local runtime.
    """
    from models.embed_model import core as embed_model_core

    _, use_local, _ = embed_model_core._detect_backend()
    if not use_local:
        return None

    try:
        from llama_cpp import Llama
    except ModuleNotFoundError:
        return (
            "local embedding runtime unavailable: llama-cpp-python is not installed "
            "(the llm-e2e CI profile syncs with --no-install-package llama-cpp-python)"
        )

    if not hasattr(Llama, "from_pretrained"):
        return (
            "local embedding runtime unavailable: the importable llama_cpp.Llama has no "
            "from_pretrained, so the bge-m3 GGUF cannot be fetched/loaded "
            "(a bare llama_cpp test stub shadows the real llama-cpp-python)"
        )
    return None


@pytest.mark.timeout(_TIMEOUT_S)
@pytest.mark.asyncio
async def test_text_doc_index_then_query_returns_grounded_answer(tmp_path, monkeypatch):
    """Given an isolated SRC_DIR and a small text doc,
    when the doc is indexed and the graph is queried about a stated fact,
    then the answer names the fact's subject and carries no pipeline error marker.
    """
    skip_reason = _local_embedding_runtime_unavailable()
    if skip_reason is not None:
        pytest.skip(skip_reason)

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
