"""
llm_wiki scripts package.
Since the skill directory name contains a hyphen (llm_wiki), standard Python package
imports cannot be used; all cross-module references are loaded dynamically via importlib.
"""

import importlib.util
import sys
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent


def _load_module(name, filename):
    """Dynamically load a Python module from the same directory"""
    spec = importlib.util.spec_from_file_location(name, str(_scripts_dir / filename))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolve ``cls.__module__`` through
    # sys.modules, which would be None for a spec-loaded module otherwise.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load each module
_core = _load_module("wiki_core", "core.py")
_search = _load_module("wiki_search", "search.py")
_ingest = _load_module("wiki_ingest", "ingest.py")
_graph = _load_module("wiki_graph", "graph.py")
_graph_query = _load_module("wiki_graph_query", "graph_query.py")

# Export contents from core
get_wiki_path = _core.get_wiki_path
get_wiki_subdir = _core.get_wiki_subdir
init_wiki = _core.init_wiki
wiki_exists = _core.wiki_exists
print_structure = _core.print_structure
WIKI_STRUCTURE = _core.WIKI_STRUCTURE

# Export contents from search
search_wiki = _search.search_wiki
lint_wiki = _search.lint_wiki

# Export contents from ingest
save_source = _ingest.save_source
save_binary_source = _ingest.save_binary_source

# Export contents from the explicit graph layer
build_wiki_graph = _graph.build_wiki_graph
graph_stats = _graph.graph_stats
path = _graph_query.path
god_nodes = _graph_query.god_nodes
explain = _graph_query.explain
communities = _graph_query.communities
query_graph = _graph_query.query

__all__ = [
    "get_wiki_path",
    "get_wiki_subdir",
    "init_wiki",
    "wiki_exists",
    "print_structure",
    "WIKI_STRUCTURE",
    "search_wiki",
    "lint_wiki",
    "save_source",
    "save_binary_source",
    "build_wiki_graph",
    "graph_stats",
    "path",
    "god_nodes",
    "explain",
    "communities",
    "query_graph",
]
