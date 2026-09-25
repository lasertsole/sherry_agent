import os
from typing import Any
from config import ENV_PATH
from config.features import LLM_CLIENT_DEFAULTS
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from loguru import logger
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.LLMs.reasoning_openai import ReasoningChatOpenAI
from models.LLMs.reasoning_payload import (
    build_reasoning_kwargs,
    build_thinking_floor_kwargs,
    build_thinking_level_kwargs,
    build_thinking_off_kwargs,
    get_thinking_budget,
    is_zhipu_reasoning_model,
)
from models.env_builder import ModelEnvBuilder, clean_client_kwargs

# Load environment variables
load_dotenv(ENV_PATH, override=True)
# ``model_provider``/``api_name`` are read verbatim (no strip) because the
# reasoning-payload dispatch consumes them directly; the builder below reads its
# own copies of the four credential vars for the client config.
api_name = os.getenv("MAIN_LLM_NAME")
model_provider = os.getenv("MAIN_LLM_PROVIDER")
max_tokens = os.getenv("MAIN_LLM_MAX_TOKEN")
if max_tokens:
    max_tokens = int(max_tokens)

# Reasoning/thinking mode is OFF by default (matching the non-reasoning
# behaviour users expect from the main LLM). Set MAIN_LLM_ENABLE_THINKING=true
# in .env to opt in. When enabled, build_reasoning_kwargs maps the switch to the
# correct reasoning payload for the configured MAIN_LLM_PROVIDER:
#   deepseek                         -> extra_body {thinking: enabled} (V3.2+ chat API)
#   openai + compatible gateways     -> reasoning_effort (o-series / gpt-5 only)
#   openai/zhipu + GLM (glm-4.5+)    -> extra_body {thinking: enabled} (bigmodel v4 API)
#   anthropic                        -> thinking + budget_tokens (claude-3-7/4/opus/sonnet only)
#   all other providers / non-reasoning models -> no-op (never a 400 crash)
# Reasoning supports tool calls; the chain-of-thought surfaces on
# AIMessageChunk.additional_kwargs["reasoning_content"] and is streamed to the
# client as {"type": "reasoning"} by server/service/messages.py (see
# reasoning_normalizer.py).
enable_thinking = os.getenv("MAIN_LLM_ENABLE_THINKING", "").strip().lower() == "true"
reasoning_effort = os.getenv("MAIN_LLM_REASONING_EFFORT")

# Output-token budget when thinking is enabled. SAME env/default as
# MaxTokensBoostMiddleware's boost base (agent/middlewares/max_tokens_boost/core.py)
# so the boost sequence always starts above the configured output cap.
OUTPUT_MAX_TOKEN = int(os.getenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "8192"))


def apply_thinking_budget(
    model_config: dict[str, Any], provider: str | None, model_name: str | None, enabled: bool
) -> dict[str, Any]:
    """Inflate ``max_tokens`` by the provider's thinking budget when thinking is on.

    Thinking tokens share the output budget, so the config gets headroom equal
    to :func:`get_thinking_budget`'s value. When thinking is off (or the model
    accepts no reasoning payload) the key stays ABSENT so providers keep
    applying their own default output cap.
    """
    budget = get_thinking_budget(provider, model_name, enabled)
    if budget > 0:
        model_config["max_tokens"] = OUTPUT_MAX_TOKEN + budget
    return model_config


model_config: dict[str, Any] = ModelEnvBuilder(
    {
        "model_provider": "MAIN_LLM_PROVIDER",
        "model": "MAIN_LLM_NAME",
        "api_key": "MAIN_LLM_API_KEY",
        "base_url": "MAIN_LLM_API_BASE",
    },
    strip=False,
).build(
    {
        "temperature": 0,
        "max_retries": LLM_CLIENT_DEFAULTS["main_max_retries"],
        # Explicit bounded window for each LLM request (seconds).
        "timeout": LLM_CLIENT_DEFAULTS["main_timeout"],
        # Max idle gap between streamed chunks before aborting.
        "stream_chunk_timeout": LLM_CLIENT_DEFAULTS["main_stream_chunk_timeout"],
        "profile": {"max_input_tokens": max_tokens},  # Set model context window size
    }
)
# Pristine snapshot of the client config BEFORE the reasoning payload and the
# thinking budget are merged in. Per-session thinking variants rebuild from
# this copy (build_main_llm(thinking=...)), so flipping the switch can both
# add and remove the provider's reasoning keys without leaking the previous
# variant's payload into the next one.
_pristine_config: dict[str, Any] = dict(model_config)
# Map the universal switch to the provider-correct reasoning payload. Returns
# {} (no-op) for providers/models that don't accept one, so it never crashes.
model_config.update(
    build_reasoning_kwargs(
        provider=model_provider,
        model_name=api_name,
        enabled=enable_thinking,
        reasoning_effort=reasoning_effort,
    )
)
apply_thinking_budget(model_config, model_provider, api_name, enable_thinking)
model_config = clean_client_kwargs(model_config)


def _variant_client_config(
    *,
    enabled: bool,
    level: str | None = None,
    thinking_floor: bool = False,
) -> dict[str, Any]:
    """Client config for a forced thinking state, env-independent.

    Rebuilt from the pristine pre-reasoning snapshot so toggling is symmetric:
    enabling adds the provider payload + budget headroom, disabling restores
    the exact config the process would have had with the switch off.

    * ``level`` pins an explicit thinking level (low/high/max) for always-think
      models whose toggle is a selector rather than a switch.
    * ``thinking_floor`` only matters with ``enabled=False``: for always-think
      models that reject the disable payload, it swaps in the minimum thinking
      level instead (the closest possible approximation of "off").
    """
    cfg = dict(_pristine_config)
    if level is not None:
        cfg.update(build_thinking_level_kwargs(model_provider, api_name, level))
        apply_thinking_budget(cfg, model_provider, api_name, True)
    elif enabled:
        cfg.update(
            build_reasoning_kwargs(
                provider=model_provider,
                model_name=api_name,
                enabled=True,
                reasoning_effort=reasoning_effort,
            )
        )
        apply_thinking_budget(cfg, model_provider, api_name, True)
    elif thinking_floor:
        cfg.update(build_thinking_floor_kwargs(model_provider, api_name))
        apply_thinking_budget(cfg, model_provider, api_name, True)
    else:
        # Gateways whose server default is thinking ON (GLM) need an explicit
        # disable payload; an absent param would leave them reasoning.
        cfg.update(build_thinking_off_kwargs(model_provider, api_name))
        apply_thinking_budget(cfg, model_provider, api_name, False)
    return clean_client_kwargs(cfg)


def _build_inner_chat_model_from(config: dict[str, Any]):
    """Construct an inner chat model from an explicit client config.

    GLM (Zhipu bigmodel) via the generic ``openai`` provider must use
    ``ReasoningChatOpenAI``: vanilla ``ChatOpenAI`` drops ``delta.reasoning_content``
    during streaming conversion (langchain-openai does not extract it), which
    silently kills the whole reasoning pipeline even when the thinking payload
    is sent correctly. For every other provider ``init_chat_model`` stays the
    single source of truth.
    """
    if model_provider == "openai" and api_name and is_zhipu_reasoning_model(api_name):
        kwargs = {k: v for k, v in config.items() if k != "model_provider"}
        return ReasoningChatOpenAI(**kwargs)
    return init_chat_model(**config)


def _build_inner_chat_model():
    """Construct the inner chat model for ``build_main_llm``."""
    return _build_inner_chat_model_from(model_config)


def build_main_llm(
    temperature: float | None = None,
    *,
    thinking: bool | None = None,
    thinking_level: str | None = None,
    thinking_floor: bool = False,
):
    """Create a fresh LLM instance bound to the current event loop.

    The module-level ``main_llm`` singleton is created at import time on the
    main thread.  Its internal ``openai.AsyncOpenAI`` → ``httpx.AsyncClient``
    transport pool contains ``asyncio.Lock`` objects that are bound to the
    event loop active at creation time.  When the subagent daemon thread
    tries to use this same client via ``agent.ainvoke()``, those locks
    deadlock silently.

    Call this factory from any async context (e.g. the subagent daemon
    thread) to get a fresh instance whose transport pool is correctly
    bound to the *current* event loop.

    ``thinking`` forces the reasoning payload for THIS instance regardless of
    the ``MAIN_LLM_ENABLE_THINKING`` env default: ``True``/``False`` build the
    thinking-on/off client variant (same provider dispatch, symmetric on/off
    configs), ``None`` keeps the env-derived default. Used by the per-session
    thinking toggle (ThinkingControlMiddleware). With ``thinking=False`` and
    ``thinking_floor=True`` the variant carries the MINIMUM thinking level for
    always-think models that reject the disable payload. ``thinking_level``
    pins an explicit low/high/max level instead (level selector models).
    """
    if thinking is None and thinking_level is None:
        inner = _build_inner_chat_model()
    else:
        inner = _build_inner_chat_model_from(
            _variant_client_config(
                enabled=bool(thinking),
                level=thinking_level,
                thinking_floor=thinking_floor,
            )
        )
    model = NormalizingChatModel(inner=inner)
    if temperature is not None:
        model = model.bind(temperature=temperature)
    return model


def build_fallback_chain():
    """Build the model fallback chain from ``FALLBACK_LLM_{i}_*`` env vars.

    Reads ``FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}`` for i = 1..
    and stops at the first missing NAME. PROVIDER defaults to ``openai``
    (OpenAI-compatible gateways). Candidates are plain client objects — no
    network I/O happens here; a candidate whose provider client cannot be
    constructed is skipped with a warning so an optional fallback never
    breaks agent startup.
    """
    from pub.types.llm import FallbackCandidate

    chain: list[FallbackCandidate] = []
    index = 1
    while True:
        name = os.getenv(f"FALLBACK_LLM_{index}_NAME")
        if not name:
            break
        provider = os.getenv(f"FALLBACK_LLM_{index}_PROVIDER") or "openai"
        candidate_config: dict[str, Any] = {
            "model_provider": provider,
            "model": name,
            "api_key": os.getenv(f"FALLBACK_LLM_{index}_API_KEY"),
            "base_url": os.getenv(f"FALLBACK_LLM_{index}_API_BASE"),
            "temperature": 0,
            "max_retries": LLM_CLIENT_DEFAULTS["fallback_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["fallback_timeout"],
        }
        candidate_config = clean_client_kwargs(candidate_config)
        try:
            inner = init_chat_model(**candidate_config)
        except Exception as exc:
            logger.warning("Skipping unusable fallback LLM {} ({}): {}", index, name, exc)
            index += 1
            continue
        chain.append(
            FallbackCandidate(
                provider=provider, model_name=name, model=NormalizingChatModel(inner=inner)
            )
        )
        index += 1
    return chain
