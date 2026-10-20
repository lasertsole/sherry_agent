"""
llm_wiki search: Wiki search and health check
"""

import os
import re
import glob
import sqlite3
import importlib.util
import sys
from pathlib import Path
from collections import defaultdict
from loguru import logger
from config.features import SKILLS_TOOLING

# Dynamically load the core module
_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location("wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
sys.modules.setdefault("wiki_core", _core)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path
WIKI_STRUCTURE = _core.WIKI_STRUCTURE

# The graph layer is optional from lint's point of view: a failure degrades the
# appended graph section, never the ten checks above it.
_graph_spec = importlib.util.spec_from_file_location("wiki_graph", str(_scripts_dir / "graph.py"))
_graph = importlib.util.module_from_spec(_graph_spec)
sys.modules.setdefault("wiki_graph", _graph)
_graph_spec.loader.exec_module(_graph)
_graph_query_spec = importlib.util.spec_from_file_location(
    "wiki_graph_query", str(_scripts_dir / "graph_query.py")
)
_graph_query = importlib.util.module_from_spec(_graph_query_spec)
sys.modules.setdefault("wiki_graph_query", _graph_query)
_graph_query_spec.loader.exec_module(_graph_query)


def search_wiki(keyword: str) -> list:
    """
    Search all .md files in the wiki by keyword.

    Args:
        keyword: Search keyword

    Returns:
        list: List of matches, each item being {"file": relative path, "matches": number of matching lines}
    """
    wiki_root = get_wiki_path()
    results = []

    if not wiki_root.exists():
        logger.warning(f"Wiki directory does not exist: {wiki_root}")
        return results

    pattern = os.path.join(str(wiki_root), "**", "*.md")
    for fpath in glob.glob(pattern, recursive=True):
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
            if keyword.lower() in content.lower():
                rel_path = os.path.relpath(fpath, str(wiki_root))
                match_count = content.lower().count(keyword.lower())
                results.append({"file": rel_path, "matches": match_count})
        except Exception as e:
            logger.warning(f"Failed to read {fpath}: {e}")

    results.sort(key=lambda x: x["matches"], reverse=True)
    return results


def lint_wiki() -> dict:
    """
    Run a health check on the wiki.

    Returns:
        dict: Check results, containing the various issue categories
    """
    wiki_root = get_wiki_path()
    report = {
        "orphan_pages": [],
        "broken_links": [],
        "index_completeness": {"missing_from_index": [], "extra_in_index": []},
        "frontmatter_issues": [],
        "large_pages": [],
        "total_pages": 0,
        "errors": [],
    }

    if not wiki_root.exists():
        report["errors"].append("Wiki directory does not exist")
        report["graph"] = _graph_report(wiki_root)
        return report

    # Scan all .md files
    all_md_files = []
    pattern = os.path.join(str(wiki_root), "**", "*.md")
    for fpath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(fpath, str(wiki_root))
        # Exclude root-level SCHEMA/index/log
        if rel_path in ("SCHEMA.md", "index.md", "log.md"):
            continue
        all_md_files.append(rel_path)

    report["total_pages"] = len(all_md_files)

    # Build the wikilink reference graph
    inbound_links = defaultdict(set)
    outbound_links = defaultdict(set)

    for fpath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(fpath, str(wiki_root))
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()

            # Extract [[wikilinks]]
            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            for link in links:
                target = link.split("|")[0].strip()
                outbound_links[rel_path].add(target)
                inbound_links[target].add(rel_path)

            # Check frontmatter (skip raw/; raw uses its own frontmatter format)
            if not rel_path.startswith("raw") and content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1]
                    required = ["title", "created", "updated", "type", "tags"]
                    missing = [f for f in required if f not in fm]
                    if missing:
                        report["frontmatter_issues"].append(
                            {"file": rel_path, "missing_fields": missing}
                        )

            # Check file size
            lines = content.split("\n")
            if len(lines) > 200:
                report["large_pages"].append({"file": rel_path, "lines": len(lines)})

        except Exception as e:
            report["errors"].append(f"Failed to process {rel_path}: {e}")

    # Orphan pages: pages with no inbound links (skip raw/)
    for page in all_md_files:
        if page.startswith("raw"):
            continue
        page_name = Path(page).stem
        if page_name not in inbound_links:
            report["orphan_pages"].append(page)

    # Broken links: links pointing to nonexistent pages
    all_page_names = set()
    for p in all_md_files:
        all_page_names.add(Path(p).stem)
    all_page_names.update(["SCHEMA", "index", "log"])

    for src, targets in outbound_links.items():
        for t in targets:
            if t not in all_page_names:
                report["broken_links"].append({"source": src, "target": t})

    # ---- explicit-graph metrics (appended after the original ten checks) ----
    report["graph"] = _graph_report(wiki_root)

    return report


def _graph_report(wiki_root) -> dict:
    """Graph connectivity / confidence / hub metrics; fail-open."""
    try:
        build = _graph.build_wiki_graph(wiki_root=wiki_root)
        stats = _graph.graph_stats(wiki_root=wiki_root)
        if not stats.get("available"):
            return {"available": False, "reason": "graph_not_built", "build": build}
        communities = _graph_query.communities(wiki_root=wiki_root)
        isolated = [community.symbols[0] for community in communities if community.size == 1][:50]
        largest = max((community.size for community in communities), default=0)
        confidence = stats.get("confidence", {})
        total_edges = sum(confidence.values()) or 1
        threshold = float(SKILLS_TOOLING["wiki_lint_low_confidence_ratio"])
        low_confidence_pages: list[dict] = []
        db_path = _graph.get_graph_db_path(wiki_root)
        if db_path.is_file():
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                per_page: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
                for row in conn.execute(
                    "SELECT source_file, confidence, COUNT(*) AS n FROM wiki_edges "
                    "GROUP BY source_file, confidence"
                ):
                    per_page[row["source_file"]][row["confidence"]] += int(row["n"])
                for page, counts in sorted(per_page.items()):
                    page_total = sum(counts.values())
                    inferred = counts.get("INFERRED", 0)
                    if page_total and inferred / page_total >= threshold:
                        low_confidence_pages.append(
                            {
                                "file": page,
                                "inferred": inferred,
                                "total": page_total,
                                "ratio": round(inferred / page_total, 3),
                            }
                        )
            finally:
                conn.close()
        return {
            "available": True,
            "nodes": stats.get("nodes", 0),
            "edges": stats.get("edges", 0),
            "components": len(communities),
            "largest_component": largest,
            "isolated_nodes": isolated,
            "confidence": confidence,
            "inferred_ratio": round(confidence.get("INFERRED", 0) / total_edges, 3),
            "low_confidence_pages": low_confidence_pages,
            "god_nodes": [
                {"name": node.node.name, "degree": node.degree}
                for node in _graph_query.god_nodes(10, wiki_root=wiki_root)
            ],
        }
    except Exception as exc:  # noqa: BLE001 - lint must return a report even when the graph fails
        logger.warning(f"llm_wiki: graph metrics unavailable: {exc}")
        return {"available": False, "error": str(exc)}
