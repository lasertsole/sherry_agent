"""
rag_query.py — Query the graph_rag-anything knowledge graph

Usage:
    python rag_query.py "<query_string>"

Example:
    python rag_query.py "What is the relationship between JuXueLi and YuanYe HanNa?"
"""

import sys
from pathlib import Path
from loguru import logger
from pydantic import validate_call

# Note: In Python REPL environment, sys.stdout is a StringIO object without reconfigure()
# Use try/except to handle both environments
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass  # REPL environment (e.g. StringIO) — skip

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
# skills/builtin/core/multimodal_rag/scripts/rag_query.py -> parents[5] = project root
project_root: Path = current_file.parents[5]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# `graph_rag` is a sibling package under this same `scripts/` directory, but
# only reachable under its short, bare absolute import identity when `scripts/`
# itself is on sys.path (see graph_rag/__init__.py for the same setup).
# Without this, importing this module as `skills.builtin.core.multimodal_rag.
# scripts.rag_query` (e.g. from a server handler that first imports
# `...scripts.graph_rag`) fails with `ModuleNotFoundError: No module named
# 'graph_rag'`, because only the project root — not `scripts/` — is on the path
# at that moment.
scripts_dir: Path = current_file.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

# Import graph_rag FIRST: its __init__ aliases bare `lightrag` onto the
# vendored copy before raganything (which imports `from lightrag import ...`
# at module load) is pulled in transitively via graph_rag.core.
from graph_rag import get_rag_anything
from graph_rag.vendored_raganything import RAGAnything

@validate_call
async def query(question: str) -> str:
    """Query the graph_rag-anything knowledge graph"""
    try:
        rag: RAGAnything = await get_rag_anything()
        res = await rag.aquery(question)
        logger.debug(f"[Query] {question}")
        answer = f"[Answer] {res}"
        logger.debug(answer)
        return answer
    except Exception as e:
        err_mes:str = f"[Error] Query failed: {repr(e)}"
        logger.error(err_mes)
        return err_mes