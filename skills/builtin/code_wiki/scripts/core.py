"""
code_wiki core: Wiki path management and document operations.
All paths are computed dynamically from the project root; no hardcoded absolute paths.
"""

import sys
import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from loguru import logger

# Dynamically locate the project root (4 levels up from skills/builtin/code_wiki/scripts/)
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


from config.features import SKILLS_TOOLING

WIKI_SUBDIR = SKILLS_TOOLING["wiki_subdir"]  # placed under src/data/wiki/


def get_wiki_path() -> Path:
    """
    Dynamically resolve the code_wiki output root directory.
    Based on src/data/wiki/ under the project root.

    Returns:
        Path: Path object for the wiki root directory
    """
    return project_root / "src" / "data" / WIKI_SUBDIR


def get_repo_wiki_path(repo_name: str) -> Path:
    """
    Get the wiki documentation directory for the given repository.

    Args:
        repo_name: Repository name

    Returns:
        Path: Path object for that repository's wiki directory
    """
    return get_wiki_path() / repo_name


def get_modules_dir(repo_name: str) -> Path:
    """Get the modules/ subdirectory for the given repository."""
    return get_repo_wiki_path(repo_name) / "modules"


def get_diagrams_dir(repo_name: str) -> Path:
    """Get the diagrams/ subdirectory for the given repository."""
    return get_repo_wiki_path(repo_name) / "diagrams"


def init_repo_wiki(repo_name: str) -> dict:
    """
    Initialize the wiki documentation directory structure for a repository.

    Args:
        repo_name: Repository name

    Returns:
        dict: Creation result, containing created_dirs, created_files, errors
    """
    result = {"created_dirs": [], "created_files": [], "errors": []}

    try:
        repo_dir = get_repo_wiki_path(repo_name)
        modules_dir = repo_dir / "modules"
        diagrams_dir = repo_dir / "diagrams"

        repo_dir.mkdir(parents=True, exist_ok=True)
        result["created_dirs"].append(str(repo_dir))

        modules_dir.mkdir(exist_ok=True)
        result["created_dirs"].append(str(modules_dir))

        diagrams_dir.mkdir(exist_ok=True)
        result["created_dirs"].append(str(diagrams_dir))

        logger.debug(f"code_wiki initialized for '{repo_name}' at: {repo_dir}")

    except Exception as e:
        logger.exception(f"code_wiki init failed for '{repo_name}': {e}")
        result["errors"].append(str(e))

    return result


def list_modules(repo_name: str) -> list[dict]:
    """
    List the generated module documents for the given repository.

    Args:
        repo_name: Repository name

    Returns:
        list[dict]: List of module documents, each with name and path
    """
    modules_dir = get_modules_dir(repo_name)
    if not modules_dir.exists():
        return []

    results = []
    for f in sorted(modules_dir.iterdir()):
        if f.suffix == ".md":
            results.append({"name": f.stem, "path": str(f)})
    return results


def repo_wiki_exists(repo_name: str) -> bool:
    """Check whether the wiki docs for the given repository have been initialized."""
    return get_repo_wiki_path(repo_name).exists()


def read_doc(repo_name: str, filename: str) -> str | None:
    """
    Read a document from the given repository's wiki.

    Args:
        repo_name: Repository name
        filename: File name (e.g. "README.md", "architecture.md", "modules/core.md")

    Returns:
        str | None: File content, or None if the file does not exist
    """
    path = get_repo_wiki_path(repo_name) / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_doc(repo_name: str, filename: str, content: str) -> Path:
    """
    Write a document into the given repository's wiki.

    Args:
        repo_name: Repository name
        filename: File name (e.g. "README.md", "modules/core.md")
        content: File content

    Returns:
        Path: Path of the written file
    """
    path = get_repo_wiki_path(repo_name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.debug(f"Written: {path}")
    return path


def get_structure() -> dict:
    """
    Return the code_wiki documentation directory structure definition.

    Returns:
        dict: Directory structure
    """
    return {
        "wiki_root": str(get_wiki_path()),
        "structure": {
            "<repo_name>/": {
                "README.md": "Project overview + module map",
                "architecture.md": "System architecture + Mermaid flowchart",
                "getting-started.md": "Setup, first run, workflows",
                "modules/": {"<module>.md": "Per-module deep-dive"},
                "diagrams/": {
                    "class-diagram.md": "Mermaid class diagram",
                    "sequences.md": "Mermaid sequence diagram",
                },
            }
        },
    }


def print_structure() -> str:
    """Output the directory structure as JSON."""
    return json.dumps(get_structure(), ensure_ascii=False, indent=2)


# ============================================================
# Deterministic export from a code_intel SQLite index (no LLM)
# ============================================================

_EXPORT_LANGUAGES = {"python", "typescript", "tsx", "javascript", "jsx", "rust", "go"}
_EDGE_SPECS = (
    ("call_edges", "caller_id", "callee_id", "calls"),
    ("import_edges", "importer_id", "imported_id", "imports"),
    ("inherit_edges", "child_id", "parent_id", "inherits"),
    ("mixin_edges", "host_id", "mixin_id", "mixes_in"),
)


def _slug(text: str, fallback: str = "module") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or fallback


def _mermaid_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", value) or "node"


def _load_index(db_path: Path) -> dict | None:
    """Read symbols + resolved edges from a code_intel SQLite DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "symbols" not in tables:
            return None
        symbols = [
            dict(row)
            for row in conn.execute(
                "SELECT id, name, kind, file_path, line_start, language FROM symbols "
                "ORDER BY id LIMIT ?",
                (SKILLS_TOOLING["code_wiki_export_max_symbols"],),
            )
        ]
        if not symbols:
            return None
        edges: list[tuple[int, int, str]] = []
        for table, src, dst, label in _EDGE_SPECS:
            if table not in tables:
                continue
            for row in conn.execute(
                f"SELECT {src} AS s, {dst} AS d FROM {table} WHERE {dst} IS NOT NULL"  # noqa: S608
            ):
                edges.append((int(row["s"]), int(row["d"]), label))
        inherits: list[tuple[str, str]] = []
        if "inherit_edges" in tables:
            inherits = [
                (row["child"], row["parent"])
                for row in conn.execute(
                    "SELECT c.name AS child, p.name AS parent FROM inherit_edges e "
                    "JOIN symbols c ON c.id = e.child_id "
                    "JOIN symbols p ON p.id = e.parent_id ORDER BY e.id"
                )
            ]
        mixins: list[tuple[str, str]] = []
        if "mixin_edges" in tables:
            mixins = [
                (row["host"], row["mixin"])
                for row in conn.execute(
                    "SELECT h.name AS host, m.name AS mixin FROM mixin_edges e "
                    "JOIN symbols h ON h.id = e.host_id "
                    "JOIN symbols m ON m.id = e.mixin_id ORDER BY e.id"
                )
            ]
        return {"symbols": symbols, "edges": edges, "inherits": inherits, "mixins": mixins}
    finally:
        conn.close()


def _connected_components(node_ids: set[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    adjacency: dict[int, set[int]] = {node: set() for node in node_ids}
    for src, dst in edges:
        if src in node_ids and dst in node_ids and src != dst:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
    seen: set[int] = set()
    parts: list[list[int]] = []
    for node in sorted(node_ids):
        if node in seen:
            continue
        stack = [node]
        members: list[int] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            members.append(current)
            stack.extend(sorted(adjacency[current] - seen))
        members.sort()
        parts.append(members)
    return parts


def _partition_symbols(
    symbols: list[dict], edges: list[tuple[int, int, str]]
) -> tuple[list[list[int]], str]:
    """Partition symbols into Louvain communities (or connected components)."""
    node_ids = {int(symbol["id"]) for symbol in symbols}
    pairs = [(src, dst) for src, dst, _label in edges if src in node_ids and dst in node_ids]
    try:
        import networkx as nx

        graph = nx.Graph()
        graph.add_nodes_from(sorted(node_ids))
        graph.add_edges_from(sorted(set(pairs)))
        parts = [
            sorted(int(node) for node in part)
            for part in nx.community.louvain_communities(graph, seed=0)
        ]
        engine = "networkx"
    except Exception:  # noqa: BLE001 - a missing/old networkx degrades, never fails
        parts = _connected_components(node_ids, pairs)
        engine = "components"
    parts.sort(key=lambda part: (-len(part), part[0] if part else 0))
    return parts, engine


def _module_name(members: list[int], by_id: dict[int, dict], used: set[str]) -> str:
    files = [by_id[node]["file_path"] for node in members]
    top_levels = Counter(path.split("/", 1)[0] for path in files)
    if top_levels:
        top_dir, count = top_levels.most_common(1)[0]
        label = top_dir if count >= max(1, len(files) // 2) else by_id[members[0]]["name"]
    else:
        label = by_id[members[0]]["name"]
    base = _slug(label, "module")
    slug = base
    suffix = 2
    while slug in used:
        slug = f"{base}-{suffix}"
        suffix += 1
    used.add(slug)
    return slug


def export_wiki_from_index(repo_name: str, db_path: str) -> dict:
    """Export a deterministic markdown wiki from a code_intel SQLite index.

    This is the zero-LLM path: README / architecture / per-module docs / class
    diagram come straight from the symbol and edge tables. ``sequences.md`` is
    left to the LLM-driven path (runtime call flow is not in an AST). When the
    index is missing, empty, or only holds unsupported languages, the caller
    receives ``{"fallback": True, "reason": ...}`` and should use the existing
    LLM-driven procedure.
    """
    path = Path(db_path)
    if not path.is_file():
        return {"fallback": True, "reason": "index_not_found", "db_path": str(path)}
    index = _load_index(path)
    if index is None:
        return {"fallback": True, "reason": "empty_index", "db_path": str(path)}
    symbols = index["symbols"]
    languages = {str(symbol["language"]) for symbol in symbols}
    if not languages & _EXPORT_LANGUAGES:
        return {
            "fallback": True,
            "reason": "unsupported_language",
            "languages": sorted(languages),
        }

    by_id = {int(symbol["id"]): symbol for symbol in symbols}
    degree: Counter[int] = Counter()
    for src, dst, _label in index["edges"]:
        if src in by_id and dst in by_id and src != dst:
            degree[src] += 1
            degree[dst] += 1

    parts, engine = _partition_symbols(symbols, index["edges"])
    parts = parts[: int(SKILLS_TOOLING["code_wiki_export_max_modules"])]
    used_names: set[str] = set()
    modules = [(_module_name(part, by_id, used_names), part) for part in parts if part]
    module_of: dict[int, str] = {}
    for name, members in modules:
        for node in members:
            module_of[node] = name

    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    files: list[str] = []

    # ---- README -------------------------------------------------------
    god = sorted((node for node in by_id), key=lambda node: (-degree[node], by_id[node]["name"]))[
        :10
    ]
    readme = [
        f"# {repo_name}",
        "",
        f"Generated deterministically from the code_intel index on {generated_at} (no LLM pass).",
        "",
        "## Index summary",
        "",
        f"- Files: {len({symbol['file_path'] for symbol in symbols})}",
        f"- Symbols: {len(symbols)}",
        f"- Edges: {len(index['edges'])} "
        f"(calls {sum(1 for _, _, label in index['edges'] if label == 'calls')} / "
        f"imports {sum(1 for _, _, label in index['edges'] if label == 'imports')} / "
        f"inherits {sum(1 for _, _, label in index['edges'] if label == 'inherits')} / "
        f"mixins {sum(1 for _, _, label in index['edges'] if label == 'mixes_in')})",
        f"- Languages: {', '.join(sorted(languages))}",
        f"- Community engine: {engine}",
        "",
        "## Hub symbols (by resolved degree)",
        "",
        "| Symbol | Kind | File | Degree |",
        "| --- | --- | --- | --- |",
    ]
    for node in god:
        symbol = by_id[node]
        readme.append(
            f"| `{symbol['name']}` | {symbol['kind']} | `{symbol['file_path']}` | {degree[node]} |"
        )
    readme += [
        "",
        "## Modules",
        "",
        "| Module | Symbols | Top symbols | Documentation |",
        "| --- | --- | --- | --- |",
    ]
    for name, members in modules:
        top = sorted(members, key=lambda node: (-degree[node], by_id[node]["name"]))[:3]
        readme.append(
            f"| {name} | {len(members)} | "
            f"{', '.join('`' + by_id[node]['name'] + '`' for node in top)} | "
            f"[modules/{name}.md](modules/{name}.md) |"
        )
    files.append(str(write_doc(repo_name, "README.md", "\n".join(readme) + "\n")))

    # ---- architecture -------------------------------------------------
    node_cap = int(SKILLS_TOOLING["code_wiki_export_max_diagram_nodes"])
    included: set[int] = set()
    diagram_modules: list[tuple[str, list[int]]] = []
    for name, members in modules:
        top = sorted(members, key=lambda node: (-degree[node], by_id[node]["name"]))
        selected = [node for node in top if node not in included]
        selected = selected[: max(1, min(6, node_cap - len(included)))]
        if not selected:
            continue
        included.update(selected)
        diagram_modules.append((name, selected))
        if len(included) >= node_cap:
            break

    arch = [
        "# Architecture",
        "",
        f"Generated from the code_intel index ({engine} communities). "
        f"Node count is capped at {node_cap}.",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for name, members in diagram_modules:
        arch.append(f'  subgraph {_mermaid_id("m_" + name)}["{name}"]')
        for node in members:
            symbol = by_id[node]
            label = f"{symbol['name']} ({symbol['kind']})".replace('"', "'")
            arch.append(f'    s{node}["{label}"]')
        arch.append("  end")
    for src, dst, label in index["edges"]:
        if src in included and dst in included and module_of.get(src) != module_of.get(dst):
            arch.append(f"  s{src} -->|{label}| s{dst}")
    arch += ["```", ""]
    files.append(str(write_doc(repo_name, "architecture.md", "\n".join(arch))))

    # ---- per-module docs ----------------------------------------------
    for name, members in modules:
        lines = [
            f"# Module: {name}",
            "",
            f"{len(members)} symbol(s) in this community.",
            "",
            "| Symbol | Kind | File | Line | Degree |",
            "| --- | --- | --- | --- | --- |",
        ]
        for node in sorted(members, key=lambda node: (-degree[node], by_id[node]["name"])):
            symbol = by_id[node]
            lines.append(
                f"| `{symbol['name']}` | {symbol['kind']} | `{symbol['file_path']}` | "
                f"{symbol['line_start']} | {degree[node]} |"
            )
        cross = [
            (src, dst, label)
            for src, dst, label in index["edges"]
            if src in members and dst in by_id and module_of.get(dst) not in (None, name)
        ]
        if cross:
            lines += ["", "## Cross-module edges", ""]
            for src, dst, label in cross[:50]:
                lines.append(f"- `{by_id[src]['name']}` → `{by_id[dst]['name']}` ({label})")
        files.append(str(write_doc(repo_name, f"modules/{name}.md", "\n".join(lines) + "\n")))

    # ---- class diagram ------------------------------------------------
    class_lines = ["# Class Diagram", "", "```mermaid", "classDiagram"]
    edge_cap = int(SKILLS_TOOLING["code_wiki_export_max_class_edges"])
    drawn = 0
    for child, parent in index["inherits"]:
        if drawn >= edge_cap:
            break
        class_lines.append(f"  {_mermaid_id(parent)} <|-- {_mermaid_id(child)}")
        drawn += 1
    for host, mixin in index["mixins"]:
        if drawn >= edge_cap:
            break
        class_lines.append(f"  {_mermaid_id(mixin)} <|.. {_mermaid_id(host)}")
        drawn += 1
    class_lines.append("```")
    if drawn == 0:
        class_lines = [
            "# Class Diagram",
            "",
            "No resolved inheritance or mixin edges were found in the index.",
        ]
    files.append(
        str(write_doc(repo_name, "diagrams/class-diagram.md", "\n".join(class_lines) + "\n"))
    )

    # ---- sequences (LLM-owned) ----------------------------------------
    files.append(
        str(
            write_doc(
                repo_name,
                "diagrams/sequences.md",
                "# Sequence Diagrams\n\n"
                "Runtime call flow is not representable from a static AST index. This file is\n"
                "intentionally generated by the LLM-driven code_wiki path (trace the entry\n"
                "point with `explore`/`callees`, then write the `sequenceDiagram`).\n",
            )
        )
    )

    # ---- state --------------------------------------------------------
    state = {
        "repo_name": repo_name,
        "generated_at": generated_at,
        "generator": "code_intel_export",
        "source_db": str(path),
        "symbols": len(symbols),
        "edges": len(index["edges"]),
        "community_engine": engine,
        "modules_documented": [name for name, _members in modules],
    }
    files.append(
        str(
            write_doc(
                repo_name,
                ".codewiki-state.json",
                json.dumps(state, ensure_ascii=False, indent=2),
            )
        )
    )

    logger.debug("code_wiki: exported {} files for '{}' from {}", len(files), repo_name, path)
    return {
        "fallback": False,
        "generator": "code_intel_export",
        "repo_name": repo_name,
        "db_path": str(path),
        "symbols": len(symbols),
        "edges": len(index["edges"]),
        "modules": [name for name, _members in modules],
        "community_engine": engine,
        "files": files,
    }
