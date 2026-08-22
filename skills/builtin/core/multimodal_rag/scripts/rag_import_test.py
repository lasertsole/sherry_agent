"""
rag_import_test.py — Import multi-format test documents into the RAG knowledge graph
to verify entity relationships appear on the knowledge-graph page.

Usage:
    python skills/builtin/core/multimodal_rag/scripts/rag_import_test.py
"""
import asyncio
import sys
from pathlib import Path
from loguru import logger

# Ensure project root on path (this script lives under skills/builtin/core/multimodal_rag/scripts/
# so project root is parents[5])
_project_root = Path(__file__).resolve().parents[5]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import SRC_DIR
# Import graph_rag FIRST: its __init__ aliases bare `lightrag` onto the
# vendored copy before raganything (which imports `from lightrag import ...`
# at module load) is pulled in transitively via graph_rag.core.
from graph_rag import get_rag_anything
from raganything import RAGAnything

logger.remove()
logger.add(sys.stderr, level="INFO")

DOCS = [
    r"C:\Users\31322\AppData\Local\Temp\opencode\rag_multi_fmt\01_company.txt",
    r"C:\Users\31322\AppData\Local\Temp\opencode\rag_multi_fmt\02_market.md",
    r"C:\Users\31322\AppData\Local\Temp\opencode\rag_multi_fmt\03_products.pdf",
]

CLASSIFY = "multiformat_test"


async def main() -> None:
    rag: RAGAnything = await get_rag_anything(parser="fallback_txt")
    for doc in DOCS:
        p = Path(doc)
        if not p.exists():
            logger.error(f"Missing: {doc}")
            continue
        logger.info(f"=== Indexing {p.suffix} file: {p.name} ===")
        try:
            await rag.process_document_complete(
                file_path=doc,
                output_dir=SRC_DIR / "rag" / "graph_rag" / CLASSIFY / "output",
                parse_method="auto",
            )
            logger.info(f"OK: {p.name}")
        except Exception as e:
            logger.error(f"FAIL {p.name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
