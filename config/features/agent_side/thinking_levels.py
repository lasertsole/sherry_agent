"""pi-style 7-level thinking enum + declarative wire-map types.

Sherry's original reasoning control was a hardcoded ``if/else`` strategy chain
keyed on provider ("DeepSeek gets extra_body, OpenAI gets reasoning_effort,
Anthropic gets budget_tokens, GLM gets the DeepSeek payload") with a 3-value
level set (``low`` / ``high`` / ``max``). This module carries the declarative
replacement, modelled on pi's ``Model.thinkingLevelMap`` + ``thinkingFormat``:

* a **seven-level** enum (``off`` + six effort levels), ordered from least to
  most thinking — the order IS the clamp contract;
* ``ThinkingLevelMap`` — one model's per-level wire declaration: a string means
  "send this for this level", ``None`` means "this level does not exist for
  this model" (the clamp skips it), omitted keeps the format's default;
* ``ThinkingFormat`` — the wire shape selector for OpenAI-compatible
  providers; native providers (Anthropic, Google) have their own branches in
  ``models/LLMs/reasoning_payload.py``.

The module is dependency-free by contract (``config/features/**`` never imports
``agent`` / ``server`` / ``models``).
"""

from enum import StrEnum
from typing import TypedDict


class ThinkingLevel(StrEnum):
    """The seven thinking levels, least to most thinking."""

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


#: The six EFFORT levels (everything but ``off``), least to most.
THINKING_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

#: All seven levels in clamp order — ``off`` first, ``max`` last.
EXTENDED_THINKING_LEVELS: tuple[str, ...] = ("off", *THINKING_LEVELS)


class ThinkingLevelMap(TypedDict, total=False):
    """One model's per-level wire declaration.

    Key = pi level, value = wire string | None:

    * ``str`` — this level is supported and maps to this wire value;
    * ``None`` — this level does NOT exist for this model; the clamp skips it;
    * omitted — the format's built-in default mapping applies (see
      ``models.LLMs.reasoning_payload``'s ``_DEFAULT_LEVEL_MAPS``), except for
      ``xhigh`` / ``max``, which must be declared EXPLICITLY to exist (an
      omitted ``max`` is unsupported — pi's rule, kept because a gateway that
      has never heard of a level 400s on it).
    """

    off: str | None
    minimal: str | None
    low: str | None
    medium: str | None
    high: str | None
    xhigh: str | None
    max: str | None


class ThinkingFormat(StrEnum):
    """Wire-format selector for OpenAI-compatible providers.

    Native providers (``anthropic`` / ``google`` / ``bedrock``) do not use this
    enum: their client libraries take first-class kwargs, handled by their own
    branches in ``reasoning_payload.build_thinking_kwargs``.
    """

    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    ZAI = "zai"
    QWEN = "qwen"
    TOGETHER = "together"
    BASETEN = "baseten"
    OPENROUTER = "openrouter"
    CHAT_TEMPLATE = "chat-template"
    QWEN_CHAT_TEMPLATE = "qwen-chat-template"
    STRING_THINKING = "string-thinking"
    ANT_LING = "ant-ling"


class ThinkingConfig(TypedDict):
    """Aggregated thinking-level tuning."""

    default_level: str
    """The level an on/off toggle maps to when it turns thinking ON."""

    min_answer_tokens: int
    """Output headroom a thinking call must leave for the visible answer."""


THINKING_LEVEL_CONFIG: ThinkingConfig = {
    "default_level": "high",
    "min_answer_tokens": 1024,
}
