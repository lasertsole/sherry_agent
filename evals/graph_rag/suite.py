"""RAGAS evaluation suite for the multimodal_rag graph-RAG pipeline.

Self-contained run: builds a small fixture corpus, indexes it through the real
production pipeline (FallbackTxtParser → auxiliary-LLM entity extraction →
SNKV storages), queries it, then scores answers with RAGAS metrics
(Faithfulness, AnswerRelevancy, LLMContextRecall, ContextPrecision).

All artifacts land under evals/results/graph_rag/<run_id>/ — never src/rag/:
- eval_report.json  — aggregate + per-sample scores + collected samples
- ragas_scores.csv  — the full RAGAS per-sample report (result.to_pandas())

Usage:
    uv run python evals/evals.py graph_rag
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _install_vertexai_stub() -> None:
    """Satisfy ragas 0.4.3's import of langchain_community.chat_models.vertexai.

    That module was removed from langchain-community (superseded by the
    langchain-google-vertexai partner package). ragas only references the class
    in a provider factory this project never selects, so an inert stand-in is
    sufficient and the project's pinned langchain-community stays untouched.
    """
    import langchain_community.chat_models  # noqa: F401  (parent package must init first)

    module = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:
        """Inert stand-in — never instantiated by this project's eval runs."""

    setattr(module, "ChatVertexAI", ChatVertexAI)
    sys.modules["langchain_community.chat_models.vertexai"] = module


_install_vertexai_stub()

# graph_rag and the rag scripts resolve only under runtime sys.path entries
# (project root + the skill's scripts dir), so they are imported dynamically.
graph_rag_base = importlib.import_module("graph_rag.base")
graph_rag_core = importlib.import_module("graph_rag.core")
rag_index_module = importlib.import_module("rag_index")
file_index = getattr(importlib.import_module("rag_index"), "file_index")
query = getattr(importlib.import_module("rag_query"), "query")

CORPUS: dict[str, str] = {
    "zenith.txt": (
        "Zenith Dynamics is a robotics company founded by Mira Chen in 2019, headquartered in Shenzhen. "
        "Mira Chen started Zenith Dynamics after leaving DeepPath Labs, where she led the manipulation team. "
        "The company's flagship product, the Atlas-9 warehouse robot, ships to 40 logistics sites worldwide. "
        "Founder Mira Chen remains CEO of Zenith Dynamics today."
    ),
    "deeppath.txt": (
        "DeepPath Labs is an AI research startup co-founded by Rafael Osei and Lin Xiu in 2016, based in Hangzhou. "
        "The lab is known for the GraspNet benchmark and for pioneering compliant manipulation controllers. "
        "Rafael Osei currently serves as chief scientist of DeepPath Labs."
    ),
    "atlas9.txt": (
        "The Atlas-9 warehouse robot, developed by Zenith Dynamics together with Harbin Institute of Technology, "
        "carries a payload of 500 kg and runs 14 hours on a single charge. "
        "It uses a compliant manipulation controller licensed from DeepPath Labs."
    ),
}

QA_PAIRS: list[tuple[str, str]] = [
    ("Who founded Zenith Dynamics?", "Zenith Dynamics was founded by Mira Chen in 2019."),
    (
        "What product does Zenith Dynamics make and where is it deployed?",
        "The Atlas-9 warehouse robot, deployed at 40 logistics sites worldwide.",
    ),
    (
        "Where did Mira Chen work before founding Zenith Dynamics?",
        "At DeepPath Labs, where she led the manipulation team.",
    ),
    ("What is the payload capacity of the Atlas-9 robot?", "500 kg."),
]


async def _collect_samples(work_dir: Path) -> list[dict[str, str | list[str]]]:
    """Index the fixture corpus through the production pipeline and gather
    (question, response, retrieved_contexts, reference) samples for RAGAS."""
    eval_src = work_dir / "src"
    for module in (graph_rag_core, graph_rag_base, rag_index_module):
        setattr(module, "SRC_DIR", eval_src)

    docs_dir = work_dir / "corpus"
    docs_dir.mkdir(parents=True)
    for name, text in CORPUS.items():
        (docs_dir / name).write_text(text, encoding="utf-8")

    for name in CORPUS:
        await file_index(str(docs_dir / name), "evals", parser="fallback_txt")

    graph_rag_pkg = importlib.import_module("graph_rag")
    get_rag_anything = getattr(graph_rag_pkg, "get_rag_anything")
    lightrag_module = importlib.import_module("graph_rag.vendored_lightrag")
    query_param_cls = getattr(lightrag_module, "QueryParam")

    rag = await get_rag_anything(parser="fallback_txt")

    samples: list[dict[str, str | list[str]]] = []
    for question, reference in QA_PAIRS:
        response = await query(question, parser="fallback_txt")
        if response.startswith("[Error]"):
            raise RuntimeError(f"pipeline query failed for {question!r}: {response}")
        raw_context = await rag.lightrag.aquery(
            question, param=query_param_cls(only_need_context=True, mode="mix")
        )
        contexts = [c for c in str(raw_context).split("\n----\n") if c.strip()] or [
            str(raw_context)
        ]
        samples.append(
            {
                "user_input": question,
                "response": response.removeprefix("[Answer] "),
                "retrieved_contexts": contexts,
                "reference": reference,
            }
        )
        print(f"  collected: {question}")
    return samples


def main() -> None:
    """Run the full eval: pipeline collection + RAGAS scoring + JSON report."""
    from models import build_auxiliary_llm, build_embed_model
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        AnswerRelevancy,
        ContextPrecision,
        Faithfulness,
        LLMContextRecall,
    )

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "graph_rag" / run_id
    results_dir.mkdir(parents=True)

    print(f"[evals] run {run_id} — indexing corpus + collecting samples …")
    samples = asyncio.run(_collect_samples(results_dir))

    print(f"[evals] scoring {len(samples)} samples with RAGAS …")
    result = evaluate(
        dataset=EvaluationDataset.from_list(samples),
        metrics=[Faithfulness(), AnswerRelevancy(), LLMContextRecall(), ContextPrecision()],
        llm=LangchainLLMWrapper(build_auxiliary_llm()),
        embeddings=LangchainEmbeddingsWrapper(build_embed_model()),
    )

    # ragas 0.4.3 stubs evaluate() as returning an Executor while the runtime
    # object is a Result with to_pandas(); access it dynamically.
    frame = getattr(result, "to_pandas")()
    metric_columns = ["faithfulness", "answer_relevancy", "context_recall", "context_precision"]
    present = [c for c in metric_columns if c in frame.columns]
    print("\n[evals] per-sample scores:")
    print(frame[["user_input", *present]].to_string(index=False))
    print("\n[evals] aggregate:")
    print(frame[present].mean().to_string())

    report = {
        "run_id": run_id,
        "aggregate": {k: float(frame[k].mean()) for k in present},
        "samples": samples,
        "scores": frame[present].to_dict(orient="records"),
    }
    report_path = results_dir / "eval_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    frame.to_csv(results_dir / "ragas_scores.csv", index=False)
    print(f"\n[evals] RAGAS report: {results_dir / 'ragas_scores.csv'}")
    print(f"[evals] full report:  {report_path}")


if __name__ == "__main__":
    main()
