"""Text-to-image model wrapper (``TTI_*`` family, OpenAI-compatible Images API).

The ``TTI_*`` keys already ship in ``.models``; this module is the missing
client. It calls ``POST /v1/images/generations`` through the OpenAI SDK (the
same dependency the other remote families use), maps the per-model image count
and the response-format guard, and returns decoded bytes plus the on-disk
extension. Persistence is the caller's job — the ``image_generation`` tool
stores the bytes in the content-addressed artifact store.

Model rules (aligned with ZCode):

* ``dall-e-3`` accepts ONE image; ``dall-e-2`` and the gpt-image family accept
  up to ten. ``IMAGE_GEN["max_n"]`` is a hard ceiling on top.
* Older ``dall-e-*`` models FORCE ``response_format="b64_json"``; newer
  ``gpt-image-*`` / ``chatgpt-image-latest`` models omit it.
* A URL response is downloaded (bounded) and treated exactly like ``b64_json``.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger

from config.features import IMAGE_GEN
from config.path import load_env
from models.env_builder import read_env
from models.utils import LazyInstance

load_env()

__all__ = [
    "IMAGE_GEN_MAX_OUTPUT",
    "GeneratedImage",
    "TTIError",
    "TTIModel",
    "build_tti_model",
    "max_images_for",
    "should_force_response_format",
    "tti_configured",
]

#: Hard per-model output cap (a request above it is clamped, never rejected).
IMAGE_GEN_MAX_OUTPUT: dict[str, int] = {
    "dall-e-3": 1,
    "dall-e-2": 10,
    "gpt-image-1": 10,
    "gpt-image-1-mini": 10,
    "gpt-image-1.5": 10,
    "gpt-image-2": 10,
    "chatgpt-image-latest": 10,
}

#: Models that do NOT accept ``response_format`` (they always return b64 JSON).
NEW_IMAGE_MODELS: tuple[str, ...] = (
    "chatgpt-image-latest",
    "gpt-image-1",
    "gpt-image-1-mini",
    "gpt-image-1.5",
    "gpt-image-2",
)

#: A provider URL response is downloaded, not streamed to a client: cap it.
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
MAX_PROMPT_CHARS = 32_000
_EXT_BY_FORMAT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp"}
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
_GPT_IMAGE_PREFIX = re.compile(r"^gpt-image-")


class TTIError(RuntimeError):
    """The image provider could not produce a usable image."""


@dataclass(frozen=True)
class GeneratedImage:
    """One generated image: its bytes and the extension the store should use."""

    data: bytes
    ext: str


@dataclass(frozen=True)
class TTIConfig:
    provider: str
    model: str
    api_base: str
    api_key: str

    @classmethod
    def from_env(cls) -> TTIConfig:
        return cls(
            provider=(read_env("TTI_MODEL_PROVIDER") or "openai").lower(),
            model=read_env("TTI_API_NAME") or "",
            api_base=read_env("TTI_API_BASE") or "",
            api_key=read_env("TTI_API_KEY") or "",
        )


def tti_configured() -> bool:
    """Whether the ``TTI_*`` family names a model and a key."""
    config = TTIConfig.from_env()
    return bool(config.model and config.api_key)


def max_images_for(model: str) -> int:
    """The provider's per-call image cap for *model* (unknown models: 1)."""
    name = str(model or "").strip()
    if name in IMAGE_GEN_MAX_OUTPUT:
        return IMAGE_GEN_MAX_OUTPUT[name]
    if _GPT_IMAGE_PREFIX.match(name) or name == "chatgpt-image-latest":
        return 10
    return 1


def should_force_response_format(model: str) -> bool:
    """True for the older models that require ``response_format='b64_json'``."""
    name = str(model or "").strip()
    if name == "chatgpt-image-latest" or _GPT_IMAGE_PREFIX.match(name):
        return False
    return True


def _value(item: Any, key: str) -> Any:
    """Read an attribute or dict entry from an SDK response item."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _ext_for(output_format: str | None, data: bytes) -> str:
    wanted = str(output_format or "").strip().lower().lstrip(".")
    if wanted in _EXT_BY_FORMAT:
        return _EXT_BY_FORMAT[wanted]
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


class TTIModel:
    """Thin OpenAI-compatible Images API client (sync; the tool offloads it)."""

    def __init__(self, client: Any | None = None, config: TTIConfig | None = None) -> None:
        self._config = config or TTIConfig.from_env()
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            if self._config.provider not in ("", "openai", "openai-compatible"):
                logger.warning(
                    "TTI provider {!r} is treated as OpenAI-compatible",
                    self._config.provider,
                )
            kwargs: dict[str, Any] = {
                "api_key": self._config.api_key or "not-configured",
                "timeout": float(IMAGE_GEN["timeout_s"]),
            }
            if self._config.api_base:
                kwargs["base_url"] = self._config.api_base
            self._client = OpenAI(**kwargs)
        return self._client

    def _request_kwargs(
        self,
        prompt: str,
        *,
        model: str,
        size: str,
        quality: str,
        n: int,
        style: str | None,
        background: str | None,
        output_format: str | None,
        output_compression: int | None,
        moderation: str | None,
        user: str | None,
    ) -> dict[str, Any]:
        newer = not should_force_response_format(model)
        kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "size": size,
        }
        if quality:
            # DALL·E uses standard/hd; gpt-image uses auto/low/medium/high. A
            # legacy value on a newer model is a provider error, so omit it and
            # let the model's own default answer rather than send garbage.
            if not (newer and quality in ("standard", "hd")):
                kwargs["quality"] = quality
        if not newer:
            kwargs["response_format"] = "b64_json"
        if style and not newer:
            kwargs["style"] = style
        if newer:
            for key, value in (
                ("background", background),
                ("output_format", output_format),
                ("output_compression", output_compression),
                ("moderation", moderation),
            ):
                if value is not None and value != "":
                    kwargs[key] = value
        if user:
            kwargs["user"] = user
        return kwargs

    def _download(self, url: str) -> bytes:
        try:
            response = requests.get(url, timeout=float(IMAGE_GEN["timeout_s"]), stream=True)
            response.raise_for_status()
        except requests.RequestException as error:
            raise TTIError(f"could not download the generated image: {error}") from error
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > DOWNLOAD_MAX_BYTES:
                    raise TTIError("the generated image URL exceeded the download cap")
                chunks.append(chunk)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not chunks:
            raise TTIError("the generated image URL returned no bytes")
        return b"".join(chunks)

    def generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        quality: str | None = None,
        n: int = 1,
        model: str | None = None,
        style: str | None = None,
        background: str | None = None,
        output_format: str | None = None,
        output_compression: int | None = None,
        moderation: str | None = None,
        user: str | None = None,
    ) -> list[GeneratedImage]:
        """Generate images and return their bytes + extensions.

        Raises :class:`TTIError` for a missing configuration, an empty prompt,
        an unusable provider response or an over-cap image URL. The caller owns
        persistence.
        """
        text = str(prompt or "").strip()
        if not text:
            raise TTIError("prompt is required")
        if len(text) > MAX_PROMPT_CHARS:
            raise TTIError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
        resolved_model = str(model or self._config.model or "").strip()
        if not resolved_model:
            raise TTIError(
                "no image model is configured — set TTI_API_NAME in 模型配置 (or pass model)"
            )
        if not self._config.api_key and self._client is None:
            raise TTIError("no image API key is configured — set TTI_API_KEY in 模型配置")

        requested = max(1, int(n))
        count = min(requested, max_images_for(resolved_model), int(IMAGE_GEN["max_n"]))
        kwargs = self._request_kwargs(
            text,
            model=resolved_model,
            size=str(size or IMAGE_GEN["default_size"]),
            quality=str(quality if quality is not None else IMAGE_GEN["default_quality"]),
            n=count,
            style=style,
            background=background,
            output_format=output_format,
            output_compression=output_compression,
            moderation=moderation,
            user=user,
        )
        try:
            response = self._get_client().images.generate(**kwargs)
        except TTIError:
            raise
        except Exception as error:  # noqa: BLE001 - provider errors are reported as text
            raise TTIError(f"image generation failed: {error}") from error

        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        items = list(data or [])
        if not items:
            raise TTIError("the provider returned no images")

        images: list[GeneratedImage] = []
        for item in items:
            encoded = _value(item, "b64_json")
            if encoded:
                try:
                    payload = base64.b64decode(str(encoded), validate=False)
                except (binascii.Error, ValueError) as error:
                    raise TTIError(f"the provider returned invalid base64: {error}") from error
            else:
                url = _value(item, "url")
                if not url:
                    raise TTIError("the provider returned neither b64_json nor url")
                payload = self._download(str(url))
            if not payload:
                raise TTIError("the provider returned an empty image")
            images.append(GeneratedImage(data=payload, ext=_ext_for(output_format, payload)))

        logger.info(
            "TTI generated {} image(s): model={} requested_n={} sent_n={}",
            len(images),
            resolved_model,
            requested,
            count,
        )
        return images


def build_tti_model() -> TTIModel:
    return TTIModel()


TTI_model: LazyInstance[TTIModel] = LazyInstance(build_tti_model)
