"""
rag_query.py — Query the rag_anything-anything knowledge graph

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
# skills/core/rag_anything/scripts/rag_query.py -> parents[4] = project root
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from raganything import RAGAnything
from skills.builtin.core.multimodal_rag.scripts.rag_anything import get_rag_anything

@validate_call
async def query(question: str) -> str:
    """Query the rag_anything-anything knowledge graph"""
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