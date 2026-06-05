import os
import sys
from pathlib import Path
from logging import getLogger
from rag import ensure_mineru_models
from loguru import logger as _logger
from config import SRC_DIR, MODELS_DIR
from typing import Any, Dict, List, Optional, Union
from rag.lightrag_snkv import register_with_lightrag
from raganything import RAGAnything, RAGAnythingConfig
from raganything.parser import Parser, register_parser

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from models import vl_model
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

logger = getLogger(__name__)


os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "0"
os.environ["MINERU_TOOLS_CONFIG_JSON"] = (MODELS_DIR / "extract_model/mineru_config.json").resolve().as_posix()

async def _vision_model_func(
    prompt: str,
    system_prompt: str | None = None,
    image_data: bytes | str | None = None,
    **kwargs,
) -> str:
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if image_data is not None:
        b64 = image_data
        if isinstance(b64, bytes):
            b64 = b64.decode("utf-8")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    messages: list[BaseMessage] = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))

    human_message: HumanMessage = HumanMessage(content=user_content)
    messages.append(human_message)
    result = vl_model.invoke(messages)

    return result.content


class FallbackTxtParser(Parser):
    """
    Fallback parser that reads text files directly and returns placeholder
    descriptions for image files. No external dependencies, no model downloads.
    """

    def check_installation(self) -> bool:
        return True

    def parse_text_file(
        self,
        text_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        _ = output_dir, lang, kwargs
        text_path = Path(text_path)
        if not text_path.exists():
            raise FileNotFoundError(f"File does not exist: {text_path}")
        text = text_path.read_text(encoding="utf-8")
        return [{"type": "text", "text": text, "page_idx": 0}]

    def parse_image(
        self,
        image_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
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
        pdf_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        _ = output_dir, method, lang, kwargs
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")
        return [{"type": "text", "text": f"[PDF file: {pdf_path.name}]", "page_idx": 0}]

    def parse_document(
        self,
        file_path: Union[str, Path],
        method: str = "auto",
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
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
    global _rag_anything

    if _rag_anything is None:
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

        from rag import get_lightrag

        lightrag = await get_lightrag()

        config = RAGAnythingConfig(
            parser=parser,
            parse_method=parse_method,
            working_dir=str(SRC_DIR / "rag" / "rag_anything_db"),
        )
        _rag_anything = RAGAnything(
            lightrag=lightrag,
            vision_model_func=_vision_model_func,
            config=config,
        )

        register_with_lightrag(_rag_anything)

    return _rag_anything
