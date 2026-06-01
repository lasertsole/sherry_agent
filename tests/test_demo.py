import os
import sys
import asyncio

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from pathlib import Path
SRC_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

from raganything import RAGAnything
from rag.rag_anything import get_rag_anything

async def main() -> None:
    """Test MinerU PDF parsing with real MinerU pipeline.
    
    Automatically downloads models on first run.
    """

    rag: RAGAnything = await get_rag_anything()

    await rag.process_folder_complete(
        folder_path=r"C:\app\code\project\EMA_AI_agent\tests\rag_anything\src",
        output_dir=SRC_DIR / "rag_anything" / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )

    # Query the parsed content
    res = await rag.aquery(
        "什么东西有关图灵奖"
    )
    print(res)


if __name__ == "__main__":
    # === Select which test to run ===
    # Option A: Use fallback_txt parser (no model download needed, pure text)
    #   asyncio.run(main())

    # Option B: Use real MinerU parser (models must be pre-downloaded)
    asyncio.run(main())
