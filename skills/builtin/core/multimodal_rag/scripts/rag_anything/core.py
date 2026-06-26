import os
import sys
import time
import nest_asyncio
from typing import Any
from pathlib import Path
from loguru import logger
from models import ITTT_model
from config import MODELS_DIR, SRC_DIR
from raganything import RAGAnything, RAGAnythingConfig
from raganything.parser import Parser, register_parser
from skills.builtin.core.multimodal_rag.scripts.rag_anything.ensure_mineru_models import ensure_mineru_models

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# 解决同一事件在不同事件循环的报错
nest_asyncio.apply()

os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"
os.environ["MINERU_TOOLS_CONFIG_JSON"] = (MODELS_DIR / "extract_model/mineru_config.json").resolve().as_posix()


async def _vision_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list = [],
    image_data: bytes | str | None = None,
    messages: list[dict[str, Any]] = [],
    **kwargs,
) -> str:
    # 如果提供了messages格式（用于多模态VLM增强查询），直接使用
    if messages:
        result = ITTT_model.invoke(messages)
        return result.content
    # 传统单图片格式
    elif image_data:
        messages = [
            {"role": "system", "content": system_prompt}
            if system_prompt
            else None,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}"
                        },
                    },
                ],
            }
            if image_data
            else {"role": "user", "content": prompt},
        ]
        result = ITTT_model.invoke(messages)
        return result.content
    else:
        from skills.builtin.core.multimodal_rag.scripts.rag_anything import get_lightrag

        lightrag = await get_lightrag()
        return lightrag.llm_model_func(prompt, system_prompt, history_messages, **kwargs)


class FallbackTxtParser(Parser):
    """
    Fallback parser that reads text files directly and returns placeholder
    descriptions for image files. No external dependencies, no model downloads.
    """

    def check_installation(self) -> bool:
        return True

    def parse_text_file(
        self,
        text_path: str | Path,
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        _ = output_dir, lang, kwargs
        text_path = Path(text_path)
        if not text_path.exists():
            raise FileNotFoundError(f"File does not exist: {text_path}")
        text = text_path.read_text(encoding="utf-8")
        return [{"type": "text", "text": text, "page_idx": 0}]

    def parse_image(
        self,
        image_path: str | Path,
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        _ = output_dir, lang, kwargs
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image file does not exist: {image_path}")
        # Return a placeholder description
        return [
            {
                "type": "text",
                "text": f"[Image file: {image_path.name} ({image_path.stat().st_size} bytes)]",
                "page_idx": 0,
            },
        ]

    def parse_pdf(
        self,
        pdf_path: str | Path,
        output_dir: str | None = None,
        method: str = "auto",
        lang: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        _ = output_dir, method, lang, kwargs
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")
        return [{"type": "text", "text": f"[PDF file: {pdf_path.name}]", "page_idx": 0}]

    def parse_document(
        self,
        file_path: str | Path,
        method: str = "auto",
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        _ = method
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        ext = file_path.suffix.lower()
        if ext in self.TEXT_FORMATS:
            return self.parse_text_file(file_path, output_dir, lang=lang, **kwargs)
        if ext in self.IMAGE_FORMATS:
            return self.parse_image(file_path, output_dir, lang=lang, **kwargs)
        if ext == ".pdf":
            return self.parse_pdf(file_path, output_dir, lang=lang, **kwargs)
        raise ValueError(
            f"Unsupported file format: {ext}. "
            "FallbackTxtParser supports text (.txt, .md), image, and PDF formats."
        )


# Register the fallback parser before creating RAGAnything
register_parser("fallback_txt", FallbackTxtParser)
logger.info("Registered FallbackTxtParser as 'fallback_txt'")


_rag_anything: RAGAnything | None = None


async def get_rag_anything(parser: str = "mineru", parse_method: str = "auto") -> RAGAnything:
    """
    Create (or return cached) RAGAnything instance.

    Args:
        parser: Parser name to use ("mineru", "fallback_txt", etc.)
        parse_method: Parser method ("auto", etc.)
    """
    start_time = time.time()
    logger.info(f"Initializing RAGAnything: parser={parser}, parse_method={parse_method}")

    # Auto-download and configure models
    # Bypass system proxy to avoid SSL errors with hf-mirror.com
    for proxy_var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(proxy_var, None)

    try:
        ensure_mineru_models(source="huggingface")
        logger.debug("Mineru models downloaded from HuggingFace")
    except Exception as e:
        logger.warning(f"Download from huggingface failed: {e}")
        logger.info("Retrying with modelscope ...")
        ensure_mineru_models(source="modelscope")
        logger.debug("Mineru models downloaded from ModelScope")

    # Switch to local model mode
    os.environ["MINERU_MODEL_SOURCE"] = "local"

    # Ensure .venv\Scripts is on PATH so subprocess can find "mineru" CLI
    _venv_scripts = os.path.join(os.path.dirname(sys.executable))
    if _venv_scripts not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _venv_scripts + os.pathsep + os.environ.get("PATH", "")

    from skills.builtin.core.multimodal_rag.scripts.rag_anything import get_lightrag

    lightrag = await get_lightrag()
    logger.debug("LightRAG initialized")

    working_dir: str = (SRC_DIR / "rag" / "store").resolve().as_posix()
    config = RAGAnythingConfig(
        parser = parser,
        parse_method = parse_method,
        working_dir = working_dir,
        parser_output_dir = str(SRC_DIR / "rag/output"),
    )
    _rag_anything = RAGAnything(
        lightrag = lightrag,
        vision_model_func = _vision_model_func,
        config = config,
    )

    elapsed = time.time() - start_time
    logger.info(
        f"RAGAnything initialized successfully: duration={elapsed:.2f}s, "
        f"parser={parser}, working_dir={config.working_dir}"
    )

    return _rag_anything
