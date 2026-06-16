"""
rag_query.py — Query the rag-anything knowledge graph

Usage:
    python rag_query.py "<query_string>"

Example:
    python rag_query.py "What is the relationship between JuXueLi and YuanYe HanNa?"
"""

import sys
from pathlib import Path
from loguru import logger

# Note: In Python REPL environment, sys.stdout is a StringIO object without reconfigure()
# Use try/except to handle both environments
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass  # REPL environment (e.g. StringIO) — skip

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
# skills/core/rag/scripts/rag_query.py -> parents[4] = project root
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag import get_rag_anything
from raganything import RAGAnything


async def query(question: str) -> None:
    """Query the rag-anything knowledge graph"""
    try:
        rag: RAGAnything = await get_rag_anything()
        res = await rag.aquery(question)
        logger.info(f"[Query] {question}")
        logger.info(f"[Answer] {res}")
    except Exception as e:
        logger.error(f"[Error] Query failed: {e}")