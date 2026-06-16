"""
MinerU VLM model inference interface.

Reuses the model weights already downloaded under models/extract_model/.
Uses mineru-vl-utils (MinerUClient) for high-level document parsing (PDF page -> Markdown).
"""

from __future__ import annotations

import os
import sys
from typing import Any
from pathlib import Path
from loguru import logger
from config import MODELS_DIR

# ---------- project root ----------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ["MINERU_TOOLS_CONFIG_JSON"] = str(
    MODELS_DIR / "extract_model" / "mineru_config.json"
)

# ---------- paths ----------
_VLM_DIR = MODELS_DIR / "extract_model" / "vlm"


def _resolve_vlm_path() -> str:
    """Return the VLM model path; resolve HuggingFace slug for local dir."""
    path = os.environ.get("MINERU_VLM_MODEL_PATH")
    if path:
        return path
    return str(_VLM_DIR)


class MinerUModel:
    """Singleton wrapper around MinerU VLM inference.

    Usage::

        model = MinerUModel.get_instance()
        blocks = model.extract_page(image)
        md = model.extract_page_to_markdown(image)
        model.unload()
    """

    _instance: MinerUModel | None = None

    def __init__(self) -> None:
        self._client: Any = None
        self._backend: str | None = None

    # ---- singleton ----

    @classmethod
    def get_instance(cls) -> MinerUModel:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        inst = cls._instance
        if inst is not None:
            inst.unload()
        cls._instance = None

    # ---- public API ----

    def load(self, backend: str = "transformers", **kwargs: Any) -> Any:
        """Load the MinerU model and return a MinerUClient instance.

        Args:
            backend: ``"transformers"`` (default).
            **kwargs: passed through to the backend initialisation.

        Returns:
            A ``mineru_vl_utils.MinerUClient`` instance.

        Raises:
            ImportError: if ``mineru-vl-utils`` is not installed.
        """
        if self._client is not None and self._backend == backend:
            return self._client

        try:
            from mineru_vl_utils import MinerUClient
        except ImportError:
            raise ImportError(
                "mineru-vl-utils is required. Install with:\n"
                '  pip install "mineru-vl-utils[transformers]"'
            ) from None

        model_path = _resolve_vlm_path()
        logger.info("Loading MinerU VLM from: {}", model_path)

        if backend == "transformers":
            from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

            model = Qwen2VLForConditionalGeneration.from_pretrained(
                model_path, **kwargs
            )
            processor = AutoProcessor.from_pretrained(model_path, use_fast=True)

            self._client = MinerUClient(
                backend="transformers",
                model=model,
                processor=processor,
            )

        else:
            raise ValueError(f"Unsupported backend: {backend!r}, expected 'transformers'")

        self._backend = backend
        logger.info("MinerU VLM loaded (backend={})", backend)
        return self._client

    def unload(self) -> None:
        """Release the loaded model and free GPU memory."""
        self._client = None
        self._backend = None
        import gc
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except ImportError:
            pass
        logger.info("MinerU VLM unloaded.")

    def extract_page(
        self,
        image: Any,
        *,
        backend: str = "transformers",
        image_analysis: bool = False,
        model_kwargs: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse a single page image into structured content with MinerUClient.

        Args:
            image: ``PIL.Image`` or a file path (``str | Path``).
            backend: ``"transformers"`` (default).
            image_analysis: enable image/chart analysis (default ``False``).
            model_kwargs: extra arguments passed to ``load()``.

        Returns:
            A list of content blocks (each a dict with ``type``, ``text`` etc.).
        """
        from PIL import Image as PILImage

        if isinstance(image, (str, Path)):
            image = PILImage.open(str(image))

        client = self.load(backend=backend, **(model_kwargs or {}))

        result: Any = client.two_step_extract(image, image_analysis=image_analysis)
        # Convert ContentBlock dict-like objects to plain dicts
        return [dict(block) for block in result]

    def extract_page_to_markdown(
        self,
        image: Any,
        *,
        backend: str = "transformers",
        image_analysis: bool = False,
        model_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """Parse a single page image and return the result as Markdown.

        Args:
            Same as ``extract_page()``.

        Returns:
            Markdown string (with truncated paragraph merging enabled).
        """
        try:
            from mineru_vl_utils.post_process import json2md
        except ImportError:
            raise ImportError(
                "mineru-vl-utils is required for json2md. Install with:\n"
                '  pip install "mineru-vl-utils[transformers]"'
            ) from None

        content_list = self.extract_page(
            image,
            backend=backend,
            image_analysis=image_analysis,
            model_kwargs=model_kwargs,
        )
        return json2md(content_list)

mineru_model = MinerUModel.get_instance()