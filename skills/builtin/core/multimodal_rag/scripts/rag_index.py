"""
rag_index.py — Index all files under a directory into the graph_rag-anything knowledge graph

Usage:
    python rag_index.py <input_folder_path> <classify_folder>

Example:
    python rag_index.py /path/to/documents my_docs
"""

import sys
from pathlib import Path
from loguru import logger
from pydantic import validate_call

current_file = Path(__file__).resolve()
# skills/builtin/core/multimodal_rag/scripts/rag_index.py -> parents[5] = project root
project_root: Path = current_file.parents[5]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# `graph_rag` is a sibling package under this same `scripts/` directory, but
# only reachable under its short, bare absolute import identity when `scripts/`
# itself is on sys.path (see graph_rag/__init__.py for the same setup). Mirror
# the fix in rag_query.py so importing this as
# `skills.builtin.core.multimodal_rag.scripts.rag_index` also resolves.
scripts_dir: Path = current_file.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from config import SRC_DIR
# Import graph_rag FIRST: its __init__ aliases bare `lightrag` onto the
# vendored copy before raganything (which imports `from lightrag import ...`
# at module load) is pulled in transitively via graph_rag.core.
from graph_rag import get_rag_anything
from graph_rag.vendored_raganything import RAGAnything

@validate_call
async def folder_index(input_folder_path: str, classify_folder: str) -> str:
    """Index files in the specified folder into the graph_rag-anything knowledge graph"""
    rag: RAGAnything = await get_rag_anything()

    await rag.process_folder_complete(
        folder_path=input_folder_path,
        output_dir=SRC_DIR / "rag" / "graph_rag" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    suc_mess: str = f"✅ Indexing complete! Folder '{input_folder_path}' added to knowledge graph category '{classify_folder}'"
    logger.debug(suc_mess)
    return suc_mess

@validate_call
async def file_index(input_file_path: str, classify_folder: str) -> None:
    rag: RAGAnything = await get_rag_anything()

    await rag.process_document_complete(
        file_path=input_file_path,
        output_dir=SRC_DIR / "rag" / "graph_rag" / classify_folder / "output",
        parse_method="auto",
        # Use the mineru "pipeline" backend for PDF/docx instead of the default
        # hybrid-engine, which cold-loads the VLM on CPU and is very slow/fragile
        # on this box. Harmless for txt/md (routed to the fallback_txt parser).
        backend="pipeline",
    )
    logger.debug(f"✅ Indexing complete! File '{input_file_path}' added to knowledge graph category '{classify_folder}'")