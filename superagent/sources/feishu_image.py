"""Feishu image attachments (prototype ``src/sources/feishu-image.ts``)."""

from __future__ import annotations

import re

from superagent.infra.commands import Commands
from superagent.sources.feishu_trigger import FeishuConfig

IMAGE_KEY = re.compile(r"img_[a-zA-Z0-9_]+")


def image_keys(text: str) -> list[str]:
    """Extract Feishu image keys from message text."""
    return IMAGE_KEY.findall(text)


async def read_image(commands: Commands, config: FeishuConfig, key: str) -> bytes:
    """Download one image by key through the bot CLI."""
    if not IMAGE_KEY.fullmatch(key):
        raise ValueError("invalid Feishu image key")
    result = await commands.run([config.cli, "image", "get", key, "--binary"])
    if not result.ok:
        raise RuntimeError(f"feishu image read failed: {result.stderr[:200]}")
    return result.stdout.encode("utf-8")


__all__ = ["IMAGE_KEY", "image_keys", "read_image"]
