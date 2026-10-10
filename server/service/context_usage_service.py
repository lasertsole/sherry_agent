"""Current-context accounting for the chat toolbar's usage ring.

The toolbar shows how full the model's context window is, and splitting that
into the four parts the model actually receives:

  - **system prompt** — the session's prompt from the state register (the same
    source the summarization overflow router estimates), minus the skill index
    below: persona files, memory, and the live todo/taskflow/continuity blocks;
  - **skill index** — the ``<available_skills>`` block ``build_system_prompt``
    appends last, located inside the prompt that was actually injected rather
    than re-derived (re-deriving would measure the skills on disk NOW, not the
    set that turn carried);
  - **tool schemas** — the main tool set serialized the way a provider sees it,
    estimated once per process (the set is static);
  - **messages** — the remainder of the prompt: the provider's reported prompt
    size minus the parts above. That reported number is the only ground truth
    for what the model got (it counts the full prompt, including chat
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

from pub.func.message.text_limits import MAX_INLINE_TEXT_CHARS
from config.features import SUMMARIZATION
from context_engine import get_db, get_history_by_turn_page
from models.LLMs.main_llm import max_tokens as main_llm_context_window
from pub.func.estimate_tokens import estimate_json_tokens, estimate_text_tokens
from runtime import StateKey, state_register_mem

from loguru import logger

# The skill index is the only prompt part wrapped in these tags (see
# skills/loader.get_skills_text): they identify it inside an assembled prompt.
_SKILLS_OPEN = "<available_skills>"
_SKILLS_CLOSE = "</available_skills>"


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


def _split_skill_index(prompt: str) -> tuple[str, str]:
    """Split an assembled system prompt into (persona + blocks, skill index).

    ``build_system_prompt`` appends ``get_skills_text(...)`` as the last
    ``\\n\\n``-joined part, and that index is the only part wrapped in
    ``<available_skills>``: locating it by its own tags measures the index the
    turn actually carried, while re-deriving it from disk would measure whatever
    the skill set is now. A prompt without the tags (filtered build, older row)
    reports no index at all instead of inventing one.
    """
    start = prompt.find(_SKILLS_OPEN)
    if start < 0:
        return prompt, ""
    end = prompt.find(_SKILLS_CLOSE, start)
    if end < 0:
        return prompt, ""
    end += len(_SKILLS_CLOSE)
    # Drop the join separators that used to hold the index in place.
    return (prompt[:start] + prompt[end:]).strip("\n"), prompt[start:end]


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
            "system":          int,          # system prompt estimate, skill index excluded
            "skills":          int,          # skill-index estimate
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
    persona_prompt, skill_index = _split_skill_index(prompt) if prompt else ("", "")
    system = estimate_text_tokens(persona_prompt) if persona_prompt else 0
    skills = estimate_text_tokens(skill_index) if skill_index else 0
    try:
        tools = _tool_schema_tokens()
    except Exception as error:  # a tool-set failure must not break the ring
        logger.warning("context usage: tool schema estimate failed: {}", error)
        tools = 0

    # The estimates are character-based and the tool schemas are large, so on a
    # short prompt they can add up to more than the provider reported. Scale them
    # down in that case: the panel must never show a part larger than the whole
    # prompt, and `messages` is the remainder of the reported total.
    estimated = system + skills + tools
    if total and estimated > total:
        scale = total / estimated
        system = int(system * scale)
        skills = int(skills * scale)
        tools = int(tools * scale)
    messages = max(total - system - skills - tools, 0)
    return {
        "window": window,
        "total": total,
        "system": system,
        "skills": skills,
        "tools": tools,
        "messages": messages,
        "cache_hit_ratio": _cache_hit_ratio(session_id),
        "compress_ratio": float(SUMMARIZATION["compression_trigger_ratio"]),
    }


# --------------------------------------------------------------------------
# Context inspection: the CONTENT behind the accounting above.
# --------------------------------------------------------------------------

#: Longest text served for one message: this is a reading surface, not a dump —
#: a giant tool result is clipped with a flag rather than shipped whole.
_MAX_MESSAGE_CHARS = MAX_INLINE_TEXT_CHARS


def _clip(text: str) -> tuple[str, bool]:
    """Bound one message's text; ``(text, truncated)``."""
    if len(text) <= _MAX_MESSAGE_CHARS:
        return text, False
    return text[:_MAX_MESSAGE_CHARS] + "\n…[truncated]", True


def _message_text(content: Any) -> str:
    """A message's content as one string (multimodal blocks are serialized)."""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - a weird payload must not break the view
        return str(content)


def _message_payload(message: Any) -> dict[str, Any]:
    """One transcript row for the viewer: role, text, and why it is there.

    ``origin``/``internal`` ride along because injected carriers (the workspace
    notices, task-intent directives, subagent completions) are indistinguishable
    from user text by content alone — the viewer should say what they are.

    Reasoning rides along too: a thinking model's chain-of-thought arrives under
    ``additional_kwargs["reasoning_content"]`` (the same field the persistence
    layer reads for the chat's collapsible 思考过程 block), and WITHOUT it an AI
    row that only thought and called a tool looks empty in the viewer.
    """
    raw_text = _message_text(getattr(message, "content", ""))
    text, truncated = _clip(raw_text)
    metadata: dict[str, Any] = getattr(message, "metadata", None) or {}
    payload: dict[str, Any] = {
        "role": str(getattr(message, "type", "") or "unknown"),
        "content": text,
        "truncated": truncated,
    }
    additional: dict[str, Any] = getattr(message, "additional_kwargs", None) or {}
    reasoning = str(additional.get("reasoning_content") or "").strip()
    if reasoning:
        payload["reasoning"], reasoning_clipped = _clip(reasoning)
        payload["truncated"] = truncated or reasoning_clipped
    origin = str(metadata.get("origin") or "").strip()
    if origin:
        payload["origin"] = origin
    if metadata.get("internal") is True:
        payload["internal"] = True
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        payload["tool_calls"] = [
            {
                "name": str(call.get("name", "")),
                "args": _clip(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))[0],
            }
            for call in tool_calls
            if isinstance(call, dict)
        ]
    call_id = getattr(message, "tool_call_id", None)
    if call_id:
        payload["tool_call_id"] = str(call_id)
    return payload


async def read_state_messages(session_id: str) -> list[Any]:
    """The session's LIVE message list from the checkpointer.

    That state is exactly what the next model call receives (post-compaction,
    with the evicted tool results already previewed), so it is the one honest
    source for a "what is in the context right now" view.
    """
    from agent.state_port import read_messages

    return await read_messages(session_id)


def _tool_definitions(session_id: str) -> tuple[list[dict[str, Any]], int, bool]:
    """``(definitions, token estimate, selection_active)`` for this session.

    The definitions are the ones the session's model can actually call: the
    process-wide set minus the tools its 预设-工具 tab switched off (REQUIRED
    tools are unioned back in by ``enabled_tool_names``).
    """
    from agent.middlewares.tool_selection.core import enabled_tool_names
    from agent.tools import build_main_tools

    enabled = enabled_tool_names(session_id)
    items: list[dict[str, Any]] = []
    tokens = 0
    for tool in build_main_tools():
        if enabled is not None and tool.name not in enabled:
            continue
        schema: dict[str, Any] = {
            "name": tool.name,
            "description": _clip(str(getattr(tool, "description", "") or ""))[0],
        }
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            schema["parameters"] = args_schema.model_json_schema()
        items.append(schema)
        # The same JSON wire ratio the accounting uses, so the two agree.
        tokens += estimate_json_tokens(json.dumps(schema, ensure_ascii=False))
    items.sort(key=lambda entry: str(entry["name"]))
    return items, tokens, enabled is not None


async def get_context_content(session_id: str) -> dict[str, Any]:
    """The content behind :func:`get_context_usage`: prompt, tools, transcript.

    Response:
        {
            "window":         int,   # context window of the configured main LLM
            "system_prompt":  str,   # the prompt the chain actually injected
            "system_tokens":  int,
            "tools":          [ {name, description, parameters} ],
            "tool_tokens":    int,
            "tool_selection": bool,  # the session narrows the tool set
            "messages":       [ {role, content, origin?, internal?, tool_calls?} ],
            "message_tokens": int,
            "truncated":      int,   # rows clipped by the per-message bound
            "state_error":    str,   # non-empty when the checkpoint read failed
        }
    """
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")
    session = str(session_id)
    prompt = _system_prompt_text(session)
    try:
        tools, tool_tokens, selection = _tool_definitions(session)
    except Exception as error:  # a tool-set failure must not break the viewer
        logger.warning("context inspect: tool definitions failed: {}", error)
        tools, tool_tokens, selection = [], 0, False

    messages: list[dict[str, Any]] = []
    state_error = ""
    try:
        for message in await read_state_messages(session):
            messages.append(_message_payload(message))
    except Exception as error:  # noqa: BLE001 - a cold/absent checkpoint is normal
        logger.warning("context inspect: state read failed: {}", error)
        state_error = str(error) or error.__class__.__name__

    return {
        "window": int(main_llm_context_window or 0),
        "system_prompt": prompt,
        "system_tokens": estimate_text_tokens(prompt) if prompt else 0,
        "tools": tools,
        "tool_tokens": tool_tokens,
        "tool_selection": selection,
        "messages": messages,
        "message_tokens": sum(estimate_text_tokens(row["content"]) for row in messages),
        "truncated": sum(1 for row in messages if row["truncated"]),
        "state_error": state_error,
    }
