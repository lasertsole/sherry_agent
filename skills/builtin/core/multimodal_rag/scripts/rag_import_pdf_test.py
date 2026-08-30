"""
rag_import_pdf_test.py — End-to-end: import real PDFs through the mineru parser
(with NO_PROXY override for the Windows system proxy bug), verifying entities land
in the /knowledge-graph.

usage: python skills/builtin/core/multimodal_rag/scripts/rag_import_pdf_test.py
"""

import asyncio
import os
import sys
from pathlib import Path

# overrides the Windows system proxy bug (httpx reads registry proxy 127.0.0.1:7897
# which returns 502 for local mineru-api). Must be set BEFORE mineru CLI subprocess.
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

_project_root = Path(__file__).resolve().parents[5]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from loguru import logger
from config import SRC_DIR

logger.remove()
logger.add(sys.stderr, level="INFO")

from graph_rag import get_rag_anything

DOCS = [
    r"C:\app\code\project\EMA_AI_agent\src\rag\graph_rag\multiformat_test\output\01_company.pdf",
    r"C:\app\code\project\EMA_AI_agent\src\rag\graph_rag\multiformat_test\output\02_market.pdf",
]
CLASSIFY = "multiformat_test"


async def main() -> None:
    # Default parser = "mineru". Pass backend="pipeline" to avoid VLM cold-load on CPU.
    rag = await get_rag_anything(parser="mineru", parse_method="auto")
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
                backend="pipeline",
            )
            logger.info(f"OK: {p.name}")
        except Exception as e:
            logger.error(f"FAIL {p.name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
