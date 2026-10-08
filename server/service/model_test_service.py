"""Connectivity checks for the env-config model groups (the 测试 button).

The 环境配置 panels let a user type a provider / gateway / key / model for every
online model the agent uses. This module answers the one question those fields
cannot: *does that endpoint actually answer right now* — without writing
anything to ``.env``, so a profile can be verified BEFORE it is applied.

What is tested per group:

* chat groups (``MAIN_LLM`` / ``AUXILIARY_LLM`` / ``REASONER_LLM``) — one
  minimal completion through the same builder the app uses
  (``build_main_llm_for_profile``), so the probe exercises the real client
  (provider quirks, normalizing wrapper, retries) rather than a bare HTTP call;
* ``EMBEDDING`` — one ``/embeddings`` round trip through the same
  OpenAI-compatible client the embedding model uses;
* ``RERANKER`` — one ``/rerank`` call through ``CloudReranker``.

The media groups (``ITTT`` / ``VTTT`` / ``TTI`` / ``STT``) answer
``supported: False``: a meaningful probe needs a real image / audio payload and
would bill the provider for it, so the UI disables the button there instead of
pretending a text ping proves anything.

A failed probe is a RESULT (``ok: False`` + the error text), never an HTTP
error: the route reports "the model said no", which is the diagnosis the user
asked for. Only a malformed request (missing group/params) is a 400.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from collections.abc import Awaitable, Callable

from loguru import logger

__all__ = ["SUPPORTED_GROUPS", "test_model_group"]

#: Groups this module can probe; anything else answers ``supported: False``.
SUPPORTED_GROUPS: tuple[str, ...] = (
    "MAIN_LLM",
    "AUXILIARY_LLM",
    "REASONER_LLM",
    "EMBEDDING",
    "RERANKER",
)

#: How long one probe may take (a wedged gateway must not hold the request).
_PING_TIMEOUT_S = 20.0

#: Longest error / reply text served back to the panel.
_DETAIL_CHARS = 400

_CHAT_GROUPS = ("MAIN_LLM", "AUXILIARY_LLM", "REASONER_LLM")


def _param(params: dict[str, str], *suffixes: str) -> str:
    """The first value whose KEY ends with one of *suffixes* (``("_API_NAME", "_NAME")``).

    The groups spell their keys differently (``MAIN_LLM_NAME`` vs
    ``AUXILIARY_LLM_API_NAME`` vs ``TTI_MODEL_PROVIDER``), so matching on the
    suffix is what lets one probe serve them all.
    """
    for suffix in suffixes:
        for key, value in params.items():
            if key.endswith(suffix):
                text = str(value or "").strip()
                if text:
                    return text
    return ""


def _detail(text: object) -> str:
    """Bound one served detail string (a multi-line provider error is normal)."""
    collapsed = " ".join(str(text or "").split())
    return collapsed[:_DETAIL_CHARS]


async def _ping_chat(params: dict[str, str]) -> str:
    """One minimal completion through the real chat client; returns its reply text."""
    from langchain_core.messages import HumanMessage

    from models.LLMs.main_llm import build_main_llm_for_profile

    model = _param(params, "_API_NAME", "_NAME")
    if not model:
        raise ValueError("model name is required")
    provider = _param(params, "_PROVIDER", "_MODEL_PROVIDER")
    llm = build_main_llm_for_profile(
        provider=provider or None,
        model=model,
        api_key=_param(params, "_API_KEY") or None,
        base_url=_param(params, "_API_BASE") or None,
    )
    reply = await llm.ainvoke([HumanMessage(content="ping")])
    content = getattr(reply, "content", "")
    return _detail(content) or "(empty reply)"


async def _ping_embedding(params: dict[str, str]) -> str:
    """One ``/embeddings`` round trip through the shared OpenAI-compatible client."""
    from models.http_client import OpenAICompatibleClient

    model = _param(params, "_API_NAME", "_NAME")
    base_url = _param(params, "_API_BASE")
    api_key = _param(params, "_API_KEY")
    if not (model and base_url and api_key):
        raise ValueError("api base, api key and model name are required")

    def call() -> dict[str, Any]:
        client = OpenAICompatibleClient(base_url, api_key)
        return client.post_json(
            "/embeddings", {"model": model, "input": ["ping"], "encoding_format": "float"}
        )

    data = await asyncio.to_thread(call)
    vectors = data.get("data") or []
    if not vectors:
        raise RuntimeError("the endpoint answered without an embedding")
    size = len(vectors[0].get("embedding") or [])
    return f"embedding vector of {size} dimensions"


async def _ping_reranker(params: dict[str, str]) -> str:
    """One ``/rerank`` call through the cloud reranker."""
    from models.reranker_model.core import CloudReranker

    base_url = _param(params, "_API_BASE")
    api_key = _param(params, "_API_KEY")
    model = _param(params, "_API_NAME", "_NAME")
    if not (base_url and api_key):
        raise ValueError("api base and api key are required")

    def call() -> float:
        reranker = CloudReranker(api_base=base_url, api_key=api_key, model=model or None)
        return reranker.predict("ping", "pong")

    score = await asyncio.to_thread(call)
    return f"relevance_score={score:.4f}"


async def test_model_group(group: str, params: dict[str, str]) -> dict[str, Any]:
    """Probe one group's endpoint with the caller's (unapplied) parameters.

    :param group: Env group name (``MAIN_LLM``, ``EMBEDDING``, …).
    :param params: The profile's parameters exactly as the panel has them.
    :returns: ``{supported, ok, latency_ms, detail, model}`` — ``detail`` carries
        a short reply preview on success and the provider's own error text on
        failure, so the panel can show WHY a configuration does not work.
    """
    name = str(group or "").strip()
    values = {str(k): str(v) for k, v in (params or {}).items()}
    model = _param(values, "_API_NAME", "_NAME")
    result: dict[str, Any] = {
        "supported": name in SUPPORTED_GROUPS,
        "ok": False,
        "latency_ms": 0,
        "detail": "",
        "model": model,
    }
    if not result["supported"]:
        result["detail"] = "this group cannot be probed without a media payload"
        return result

    probe: Callable[[dict[str, str]], Awaitable[str]]
    if name in _CHAT_GROUPS:
        probe = _ping_chat
    elif name == "EMBEDDING":
        probe = _ping_embedding
    else:
        probe = _ping_reranker

    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(probe(values), timeout=_PING_TIMEOUT_S)
        result["ok"] = True
        result["detail"] = _detail(detail)
    except TimeoutError:
        result["detail"] = f"no answer within {int(_PING_TIMEOUT_S)}s"
    except Exception as error:  # noqa: BLE001 - the failure IS the diagnosis
        logger.info("model test: {} probe failed: {}", name, error)
        result["detail"] = _detail(error) or error.__class__.__name__
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result
