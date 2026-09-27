"""Current-context accounting for the chat toolbar's usage ring.

The toolbar shows how full the model's context window is, and splitting that
into the three parts the model actually receives:

  - **system prompt** — the session's prompt from the state register, the same
    source the summarization overflow router estimates;
  - **tool schemas** — the main tool set serialized the way a provider sees it,
    estimated once per process (the set is static);
  - **messages** — the remainder of the prompt: the provider's reported prompt
    size minus the two parts above. That reported number is the only ground
    truth for what the model got (it counts the full prompt, including chat
    templates the server cannot re-serialize exactly), so the message part is
    derived from it rather than from a local re-estimate.

``total`` is the newest AI row's reported ``input_tokens`` (0 when the session
has no finished turn yet); ``window`` is the configured main-LLM context window
(``MAIN_LLM_MAX_TOKEN``, the same value the model profile and the overflow guard
use).
"""

import json
from functools import lru_cache
from typing import Any

from config.features import SUMMARIZATION
from context_engine import get_db, get_history_by_turn_page
from models.LLMs.main_llm import max_tokens as main_llm_context_window
from pub.func.estimate_tokens import estimate_json_tokens, estimate_text_tokens
from runtime import StateKey, state_register_mem

from loguru import logger


@lru_cache(maxsize=1)
def _tool_schema_tokens() -> int:
    """Token estimate of the main tool set's serialized schemas.

    Cached: the tool set is built once per process and its schemas never change,
    so the walk (and the pydantic schema dump) happens on the first request.
    """
    from agent.tools import build_main_tools

    total = 0
    for tool in build_main_tools():
        schema: dict[str, Any] = {"name": tool.name, "description": tool.description}
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            schema["parameters"] = args_schema.model_json_schema()
        # JSON wire ratio: the schemas' structural keys tokenize far cheaper
        # than prose (see estimate_json_tokens) — measuring them as prose made
        # the tool part swallow short prompts whole.
        total += estimate_json_tokens(json.dumps(schema, ensure_ascii=False))
    return total


def _system_prompt_text(session_id: str) -> str:
    """The session's system prompt.

    The state register holds the prompt the middleware chain actually injected,
    but it is only filled once a turn has run in this process; before that the
    builder assembles the same prompt from persona/memory/skills. Failures fall
    back to "no system prompt" — the panel must not fail because a persona file
    is missing.
    """
    prompt = state_register_mem.get_state(session_id, StateKey.SYSTEM_PROMPT, "")
    if isinstance(prompt, str) and prompt:
        return prompt
    try:
        from workspace.prompt_builder import build_system_prompt

        return build_system_prompt(session_id=session_id) or ""
    except Exception as error:
        logger.warning("context usage: system prompt build failed: {}", error)
        return ""


def _reported_prompt_tokens(session_id: str) -> int:
    """Newest reported prompt size for the session (0 when there is none).

    Walks the newest turn's rows backwards and takes the first AI row that
    carries a provider-reported ``input_tokens``.
    """
    rows = get_history_by_turn_page(session_id, 1, 1, 1)
    for row in reversed(rows):
        tokens = row.get("input_tokens")
        if tokens:
            return int(tokens)
    return 0


def _cache_hit_ratio(session_id: str) -> float | None:
    """Session-wide cache hit rate: cached prompt tokens / prompt tokens.

    ``None`` only when NO row carries a cache figure at all (SUM over an all-NULL
    column is NULL): rows written before the column existed, or a provider that
    never reports cached tokens, must read as "unknown" — but a session that
    genuinely reported zero cached tokens reads as 0%.
    """
    try:
        row = (
            get_db()
            .execute(
                # Only turns the provider reported cache numbers for: an older
                # turn with no cache column value is unknown, not a 0% hit — mixing
                # it in would dilute the rate the panel shows.
                "SELECT SUM(input_tokens) AS total, SUM(cache_read_tokens) AS cached, "
                "COUNT(*) AS reported FROM messages "
                "WHERE session_id = ? AND role = 'ai' AND cache_read_tokens IS NOT NULL",
                (session_id,),
            )
            .fetchone()
        )
    except Exception as error:  # the panel must not fail on a storage hiccup
        logger.warning("context usage: cache-hit query failed: {}", error)
        return None
    total = int(row["total"] or 0) if row else 0
    # Keep None distinct from 0: "never reported" vs "reported nothing cached".
    cached = row["cached"] if row else None
    if total <= 0 or cached is None:
        return None
    return min(int(cached) / total, 1.0)


def get_context_usage(session_id: str) -> dict[str, int | float | None]:
    """Return the session's context accounting.

    Response:
        {
            "window":          int,          # context window of the configured main LLM
            "total":           int,          # prompt size the provider reported for the last turn
            "system":          int,          # system prompt estimate
            "tools":           int,          # tool-schema estimate
            "messages":        int,          # remainder (never negative)
            "cache_hit_ratio": float | None, # session-wide cached-prompt share
            "compress_ratio":  float         # pressure at which summarization compacts
        }
    """
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")

    window = int(main_llm_context_window or 0)
    total = _reported_prompt_tokens(session_id)
    prompt = _system_prompt_text(session_id)
    system = estimate_text_tokens(prompt) if prompt else 0
    try:
        tools = _tool_schema_tokens()
    except Exception as error:  # a tool-set failure must not break the ring
        logger.warning("context usage: tool schema estimate failed: {}", error)
        tools = 0

    # The two estimates are character-based and the tool schemas are large, so on
    # a short prompt they can add up to more than the provider reported. Scale
    # them down in that case: the panel must never show a part larger than the
    # whole prompt, and `messages` is the remainder of the reported total.
    estimated = system + tools
    if total and estimated > total:
        scale = total / estimated
        system = int(system * scale)
        tools = int(tools * scale)
    messages = max(total - system - tools, 0)
    return {
        "window": window,
        "total": total,
        "system": system,
        "tools": tools,
        "messages": messages,
        "cache_hit_ratio": _cache_hit_ratio(session_id),
        "compress_ratio": float(SUMMARIZATION["compression_trigger_ratio"]),
    }
