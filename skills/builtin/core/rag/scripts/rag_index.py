"""
rag_index.py — Index all files under a directory into the rag-anything knowledge graph

Usage:
    python rag_index.py <input_folder_path> <classify_folder>

Example:
    python rag_index.py /path/to/documents my_docs
"""

import sys
from pathlib import Path
from loguru import logger

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
# skills/core/rag/scripts/rag_index.py -> parents[4] = project root
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import SRC_DIR
from rag import get_rag_anything
from raganything import RAGAnything


async def folder_index(input_folder_path: str, classify_folder: str) -> None:
    """Index files in the specified folder into the rag-anything knowledge graph"""
    rag: RAGAnything = await get_rag_anything()

    await rag.process_folder_complete(
        folder_path=input_folder_path,
        output_dir=SRC_DIR / "rag" / "rag_anything" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    logger.info(f"✅ Indexing complete! Folder '{input_folder_path}' added to knowledge graph category '{classify_folder}'")


async def file_index(input_file_path: str, classify_folder: str) -> None:
    rag: RAGAnything = await get_rag_anything()

    await rag.process_document_complete(
        file_path=input_file_path,
        output_dir=SRC_DIR / "rag" / "rag_anything" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    logger.info(f"✅ Indexing complete! File '{input_file_path}' added to knowledge graph category '{classify_folder}'")