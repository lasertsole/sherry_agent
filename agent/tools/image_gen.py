"""``image_generation`` — text-to-image through the configured TTI family.

The model rules (per-model image cap, the older ``response_format`` guard and the
URL-vs-base64 response handling) live in :mod:`models.TTI_model.core`; this tool
is the agent-facing half: it validates the request sizes, calls the provider off
the event loop, persists every returned image in the content-addressed artifact
store (SHA-256 deduplicated, per-session) and answers markdown image references
the chat's gallery/lightbox renders.

Registered only when the ``TTI_*`` family is configured, and main-agent-only:
image generation costs money and is the operator's model choice, not something a
subagent should invoke on its own.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId, write_artifact
from models.TTI_model import GeneratedImage, TTIError, TTI_model, tti_configured

__all__ = ["ImageGenerationInput", "ImageGenerationTool", "build_image_gen_tool"]

_SCOPE_METADATA: dict[str, Any] = {"scope": "main_only", "idempotent": False}


class ImageGenerationInput(BaseModel):
    """Arguments of ``image_generation``."""

    prompt: str = Field(description="What to draw, in natural language.")
    size: str | None = Field(None, description="Image size, e.g. 1024x1024 (provider-dependent).")
    quality: str | None = Field(
        None,
        description="Model quality: standard/hd for DALL·E, auto/low/medium/high for gpt-image.",
    )
    n: int = Field(1, ge=1, le=10, description="How many images to generate (model-capped).")
    model: str | None = Field(None, description="Override the configured TTI model for this call.")
    style: str | None = Field(None, description="DALL·E style: vivid / natural.")
    background: str | None = Field(
        None, description="gpt-image background: auto / opaque / transparent."
    )
    output_format: str | None = Field(
        None, description="gpt-image output format: png / jpeg / webp."
    )
    output_compression: int | None = Field(
        None, ge=0, le=100, description="gpt-image output compression (0-100)."
    )
    session_id: SessionId = ""


class ImageGenerationTool(BaseTool):
    """Generate an image from a text prompt and persist it as an artifact."""

    name: str = "image_generation"
    description: str = (
        "Generate an image from a text prompt using the configured image model "
        "(DALL·E / GPT-Image). Returns markdown image references; the image is "
        "stored in the session's artifact store. Use it when the user asks you "
        "to draw or create an image. Cost note: each call bills the configured "
        "image provider."
    )
    args_schema: type[BaseModel] = ImageGenerationInput
    metadata: dict = _SCOPE_METADATA

    def _render(self, images: list[GeneratedImage], session_id: str, model: str | None) -> str:
        references: list[str] = []
        failures = 0
        for index, image in enumerate(images, start=1):
            uri = write_artifact(session_id, image.data, media_type="image", ext=image.ext)
            if uri is None:
                failures += 1
                continue
            references.append(f"![generated image {index}]({uri})")
        if not references:
            return (
                "Error: the images were generated but could not be persisted "
                "(the artifact store is unavailable)."
            )
        lines = [f"Generated {len(references)} image(s)"]
        if model:
            lines[0] += f" with {model}"
        lines[0] += ":"
        lines.extend(references)
        if failures:
            lines.append(f"({failures} image(s) could not be persisted and were discarded.)")
        return "\n\n".join(lines)

    def _run(
        self,
        prompt: str,
        size: str | None = None,
        quality: str | None = None,
        n: int = 1,
        model: str | None = None,
        style: str | None = None,
        background: str | None = None,
        output_format: str | None = None,
        output_compression: int | None = None,
        session_id: str = "",
    ) -> str:
        if not str(session_id or "").strip():
            return "Error: session_id is required to persist generated images."
        try:
            images = TTI_model.generate(
                prompt,
                size=size,
                quality=quality,
                n=n,
                model=model,
                style=style,
                background=background,
                output_format=output_format,
                output_compression=output_compression,
            )
        except TTIError as error:
            logger.info("image generation failed: {}", error)
            return f"Error: image generation failed: {error}"
        except Exception as error:  # noqa: BLE001 - the tool boundary returns text
            logger.exception("image generation crashed")
            return f"Error: image generation failed: {type(error).__name__}: {error}"
        return self._render(images, session_id, model)

    async def _arun(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(lambda: self._run(**kwargs))


def build_image_gen_tool() -> BaseTool | list[BaseTool]:
    """The main-agent tool when the TTI family is configured; else no tools."""
    if not tti_configured():
        return []
    return ImageGenerationTool()
