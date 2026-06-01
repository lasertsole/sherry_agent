import os
import sys
import asyncio

# 1. 必须在最顶部、任何其他 import 之前清除离线模式
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"

# 2. 如果你在国内，强烈建议同时加上这一行，使用国内镜像源下载 MinerU 模型
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from pathlib import Path
SRC_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

from lightrag import QueryParam
from raganything import RAGAnything
from loguru import logger as _logger
from tests.rag_anything import get_rag_anything


async def main() -> None:
    """Test MinerU PDF parsing with real MinerU pipeline.
    
    Automatically downloads models on first run.
    """
    from tests.rag_anything.ensure_mineru_models import ensure_mineru_models

    # Auto-download and configure models
    # Bypass system proxy to avoid SSL errors with hf-mirror.com
    for proxy_var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(proxy_var, None)

    try:
        ensure_mineru_models(source="huggingface")
    except Exception as e:
        _logger.warning(f"Download from huggingface failed: {e}")
        _logger.info("Retrying with modelscope ...")
        ensure_mineru_models(source="modelscope")

    # Switch to local model mode
    os.environ["MINERU_MODEL_SOURCE"] = "local"

    # Ensure .venv\Scripts is on PATH so subprocess can find "mineru" CLI
    _venv_scripts = os.path.join(os.path.dirname(sys.executable))
    if _venv_scripts not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _venv_scripts + os.pathsep + os.environ.get("PATH", "")

    rag: RAGAnything = await get_rag_anything()

    await rag.process_folder_complete(
        folder_path=r"C:\app\code\project\EMA_AI_agent\tests\rag_anything\src",
        output_dir=SRC_DIR / "rag_anything" / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )

    # Query the parsed content
    res = await rag.lightrag.aquery(
        "什么东西有关图灵奖",
        param=QueryParam(mode="local", only_need_context=True, top_k=5),
    )
    print(res)


if __name__ == "__main__":
    # === Select which test to run ===
    # Option A: Use fallback_txt parser (no model download needed, pure text)
    #   asyncio.run(main())

    # Option B: Use real MinerU parser (models must be pre-downloaded)
    asyncio.run(main())
