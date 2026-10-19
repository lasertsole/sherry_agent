"""Multi-hop relationship benchmark for the graph_rag pipeline.

The RAGAS suite (``evals/graph_rag/suite.py``) measures answer quality on
single-hop factual questions, where the pipeline already scores near-perfect.
This script measures the capability the two GraphRAG plans target instead:
whether the retrieved context carries the *bridge* entities of a multi-hop
relationship question, and whether the answer follows the chain.

Protocol
--------
The corpus is indexed once per ``--work-dir`` (through the real production
pipeline: FallbackTxtParser → auxiliary-LLM extraction → SNKV storage). Each
question is then queried under one or more retrieval modes. Scoring is
deterministic and offline: a question names the bridge tokens its answer must
reach, and the script records whether the context and the final answer contain
them. No judge model participates in the score.

Reuse the same work dir between the "before" and "after" runs of a code change
so both compare against the SAME index:

    uv run --no-sync python evals/graph_rag/multihop_bench.py \
        --work-dir /tmp/graphrag_multihop --modes mix --label before
    # … apply the change …
    uv run --no-sync python evals/graph_rag/multihop_bench.py \
        --work-dir /tmp/graphrag_multihop --modes mix,path --label after

Usage:
    uv run --no-sync python evals/graph_rag/multihop_bench.py --modes mix,path
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

graph_rag_base = importlib.import_module("graph_rag.base")
graph_rag_core = importlib.import_module("graph_rag.core")
rag_index_module = importlib.import_module("rag_index")
file_index = getattr(rag_index_module, "file_index")

# The corpus is deliberately larger than the retrieval window: 20 short
# documents spanning unrelated themes, with look-alike distractor names
# (Titan Software / Titan Alloys, Kestrel Bank / Kepler-7, Aurora Media). A
# multi-hop question must cross theme boundaries through bridge entities that
# neither endpoint's description names.
CORPUS: dict[str, str] = {
    "nova.txt": (
        "Nova Aerospace is a launch-services company based in Trondheim. "
        "Nova Aerospace operates the Kepler-7 launch pad, a polar-orbit facility "
        "on the Norwegian coast. Nova Aerospace was founded in 2009 and employs "
        "260 engineers."
    ),
    "kepler.txt": (
        "The Kepler-7 launch pad uses beryllium clamps machined by Titan Alloys. "
        "The pad's cryogenic lines were refurbished in 2021. Kepler-7 supports up "
        "to twelve launches per year and serves three satellite operators."
    ),
    "titan.txt": (
        "Titan Alloys is a specialty metals supplier in Bergen. Titan Alloys is a "
        "wholly owned subsidiary of Fenwick Holdings. Titan Alloys also sells "
        "titanium plate to shipyards and machine shops."
    ),
    "fenwick.txt": (
        "Fenwick Holdings is a private investment group in Zurich. Fenwick Holdings "
        "acquired Titan Alloys in 2019. The group also holds a minority stake in "
        "Meridian Bank and funds industrial research at Vela University."
    ),
    "vela.txt": (
        "Vela University's materials laboratory licensed a spin-transfer coating "
        "process to Fenwick Holdings in 2018. The coating reduces wear on "
        "beryllium tooling. Vela University publishes its results openly."
    ),
    "aurora_media.txt": (
        "Aurora Media is a publishing house in Lisbon. Aurora Media prints trade "
        "magazines for the maritime industry and runs an astronomy book imprint."
    ),
    "titan_software.txt": (
        "Titan Software builds payroll systems for small businesses. Titan Software "
        "has no connection to metals, mining or aerospace."
    ),
    "kestrel_bank.txt": (
        "Kestrel Bank is a retail bank in Sweden. Kestrel Bank serves private "
        "customers and small firms in the Baltic region."
    ),
    "helios_energy.txt": (
        "Helios Energy operates solar farms in Spain. Helios Energy sells "
        "electricity to regional utilities and invests in battery storage."
    ),
    "polaris_bank.txt": (
        "Polaris Bank is a commercial bank in Oslo. Polaris Bank finances shipping "
        "companies and offshore service vessels."
    ),
    "kepler_press.txt": (
        "Kepler Press publishes astronomy textbooks. Kepler Press is based in "
        "Vienna and distributes through university bookshops."
    ),
    "meridian.txt": (
        "Meridian Bank is an investment bank in Geneva. Meridian Bank co-invests "
        "with Fenwick Holdings in industrial firms and advises on cross-border "
        "acquisitions."
    ),
    "orion_foods.txt": (
        "Orion Foods is a grocery chain in Portugal. Orion Foods sources olive oil "
        "from Alentejo farms and runs 90 stores."
    ),
    "sable_motors.txt": (
        "Sable Motors manufactures electric buses in Poland. Sable Motors buys "
        "battery packs from Volta Cells and sells fleets to Nordic Rail."
    ),
    "volta_cells.txt": (
        "Volta Cells produces lithium battery packs in Gdansk. Volta Cells "
        "supplies Sable Motors and Nordic Rail with traction batteries."
    ),
    "nordic_rail.txt": (
        "Nordic Rail operates freight trains across Scandinavia. Nordic Rail "
        "leases electric locomotives from Sable Motors and hauls timber."
    ),
    "quill_labs.txt": (
        "Quill Labs develops laboratory automation software in Boston. Quill Labs "
        "serves university chemistry departments and hospital labs."
    ),
    "terra_grain.txt": (
        "Terra Grain exports wheat from Ukraine. Terra Grain ships through Baltic "
        "ports and leases silos from local cooperatives."
    ),
    "cinder_steel.txt": (
        "Cinder Steel operates a rolling mill in Sweden. Cinder Steel buys scrap "
        "metal from Baltic recyclers and sells rebar to builders."
    ),
    "vera_pharma.txt": (
        "Vera Pharma develops antivirals in Basel. Vera Pharma licenses compounds "
        "from university labs and runs phase-two trials."
    ),
}

# Each question names the bridge tokens the answer must reach. A token matches
# case-insensitively as a substring, so "kepler" matches "Kepler-7 launch pad".
# ``hops`` is the shortest expected chain across the graph; ``0`` marks a
# single-hop control question where the baseline should already succeed.
QUESTIONS: list[dict] = [
    {
        "question": "How is Nova Aerospace connected to Fenwick Holdings?",
        "bridge_tokens": ["kepler", "titan alloys"],
        "hops": 3,
        "reference": (
            "Nova Aerospace operates the Kepler-7 launch pad; the pad uses beryllium "
            "clamps machined by Titan Alloys; Titan Alloys is owned by Fenwick Holdings."
        ),
    },
    {
        "question": "How is Vela University connected to Nova Aerospace?",
        "bridge_tokens": ["fenwick", "titan alloys", "kepler"],
        "hops": 4,
        "reference": (
            "Vela University licensed a coating process to Fenwick Holdings; Fenwick "
            "owns Titan Alloys; Titan Alloys machines clamps for the Kepler-7 launch "
            "pad; Nova Aerospace operates Kepler-7."
        ),
    },
    {
        "question": "What is the relationship between Meridian Bank and the Kepler-7 launch pad?",
        "bridge_tokens": ["fenwick", "titan alloys"],
        "hops": 3,
        "reference": (
            "Meridian Bank co-invests with Fenwick Holdings; Fenwick owns Titan Alloys; "
            "Titan Alloys machines clamps for the Kepler-7 launch pad."
        ),
    },
    {
        "question": "How is Sable Motors connected to Nordic Rail?",
        "bridge_tokens": ["volta"],
        "hops": 2,
        "reference": (
            "Sable Motors buys battery packs from Volta Cells; Volta Cells supplies Nordic Rail."
        ),
    },
    {
        "question": "Who owns Titan Alloys?",
        "bridge_tokens": [],
        "hops": 0,
        "reference": "Fenwick Holdings owns Titan Alloys.",
    },
    {
        "question": "What does Nova Aerospace operate?",
        "bridge_tokens": [],
        "hops": 0,
        "reference": "Nova Aerospace operates the Kepler-7 launch pad.",
    },
]


def _install(work_dir: Path) -> None:
    eval_src = work_dir / "src"
    for module in (graph_rag_core, graph_rag_base, rag_index_module):
        setattr(module, "SRC_DIR", eval_src)


def _token_hits(text: str, tokens: list[str]) -> list[str]:
    lowered = (text or "").lower()
    return [token for token in tokens if token.lower() in lowered]


async def _index(work_dir: Path) -> None:
    corpus_dir = work_dir / "corpus"
    marker = work_dir / ".indexed"
    if marker.exists():
        print(f"[bench] reusing index in {work_dir}")
        return
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for name, text in CORPUS.items():
        (corpus_dir / name).write_text(text, encoding="utf-8")
    for name in CORPUS:
        await file_index(str(corpus_dir / name), "evals", parser="fallback_txt")
    marker.write_text("ok", encoding="utf-8")


async def _collect(work_dir: Path, modes: list[str], label: str) -> dict:
    # Resolved dynamically: `graph_rag` only exists under the runtime sys.path
    # entries added at the top of this file.
    get_rag_anything = getattr(importlib.import_module("graph_rag"), "get_rag_anything")
    query_param_cls = getattr(importlib.import_module("graph_rag.vendored_lightrag"), "QueryParam")

    rag = await get_rag_anything(parser="fallback_txt")
    lightrag = rag.lightrag

    rows: list[dict] = []
    for item in QUESTIONS:
        question = item["question"]
        row: dict = {
            "question": question,
            "hops": item["hops"],
            "bridge_tokens": item["bridge_tokens"],
            "modes": {},
        }
        for mode in modes:
            print(f"[bench] {mode}: {question}")
            context = await lightrag.aquery(
                question, param=query_param_cls(mode=mode, only_need_context=True)
            )
            answer = ""
            for attempt in range(3):
                answer = str(
                    await lightrag.aquery(question, param=query_param_cls(mode=mode)) or ""
                )
                if answer.strip():
                    break
                # aquery swallows provider failures into an empty string; an
                # empty answer from a working pipeline is a transient failure.
                print(f"[bench] empty answer (attempt {attempt + 1}); retrying")
            context = str(context or "")
            row["modes"][mode] = {
                "context_chars": len(context),
                "answer_chars": len(answer),
                "context_bridge_hits": _token_hits(context, item["bridge_tokens"]),
                "answer_bridge_hits": _token_hits(answer, item["bridge_tokens"]),
                "context_all_bridges": bool(item["bridge_tokens"])
                and len(_token_hits(context, item["bridge_tokens"])) == len(item["bridge_tokens"]),
                "answer_all_bridges": bool(item["bridge_tokens"])
                and len(_token_hits(answer, item["bridge_tokens"])) == len(item["bridge_tokens"]),
                "answer_empty": not answer.strip(),
                "answer": answer,
            }
        rows.append(row)

    summary: dict[str, dict[str, float]] = {}
    for mode in modes:
        graded = [r for r in rows if r["hops"] > 0]
        summary[mode] = {
            "context_bridge_recall": (
                sum(len(r["modes"][mode]["context_bridge_hits"]) for r in graded)
                / max(1, sum(len(r["bridge_tokens"]) for r in graded))
            ),
            "answer_bridge_recall": (
                sum(len(r["modes"][mode]["answer_bridge_hits"]) for r in graded)
                / max(1, sum(len(r["bridge_tokens"]) for r in graded))
            ),
            "context_all_bridges_rate": (
                sum(1 for r in graded if r["modes"][mode]["context_all_bridges"])
                / max(1, len(graded))
            ),
            "answer_all_bridges_rate": (
                sum(1 for r in graded if r["modes"][mode]["answer_all_bridges"])
                / max(1, len(graded))
            ),
            "control_accuracy": (
                sum(
                    1
                    for r in rows
                    if r["hops"] == 0
                    and (
                        "fenwick" in r["modes"][mode]["answer"].lower()
                        if "owns" in r["question"].lower()
                        else "kepler" in r["modes"][mode]["answer"].lower()
                    )
                )
                / max(1, sum(1 for r in rows if r["hops"] == 0))
            ),
        }

    return {"label": label, "rows": rows, "summary": summary}


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=None, help="reusable index directory")
    parser.add_argument("--modes", default="mix", help="comma-separated query modes")
    parser.add_argument("--label", default="run", help="label for this measurement")
    parser.add_argument("--results-dir", default=None, help="where to write the JSON report")
    args = parser.parse_args()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = (
        Path(args.work_dir)
        if args.work_dir
        else REPO_ROOT / "evals" / "results" / "graph_rag" / f"bench_{run_id}"
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    _install(work_dir)

    await _index(work_dir)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    report = await _collect(work_dir, modes, args.label)

    results_dir = Path(args.results_dir) if args.results_dir else work_dir
    report["run_id"] = run_id
    report["work_dir"] = str(work_dir)
    path = results_dir / f"multihop_{args.label}_{run_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[bench] summary:")
    print(json.dumps(report["summary"], indent=2))
    print(f"[bench] report: {path}")


if __name__ == "__main__":
    asyncio.run(_main())
