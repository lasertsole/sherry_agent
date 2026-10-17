"""Conservative, syntax-only repair of a tool call's raw JSON arguments.

``langchain_core.messages.tool.default_tool_parser`` files a tool call whose
``arguments`` string does not parse as JSON into ``invalid_tool_calls`` (the raw
string as ``args``); the call then never reaches the ToolNode, and a message
whose ONLY call is invalid looks like a call-less final answer to the agent
loop — the turn ends silently. DeepSeek-class models occasionally emit the
*unquoted* form of a string value — ``{"commands": cd /repo && git log}`` —
which is what this module repairs.

Rules (all must hold, or the raw text is refused):

* Only the JSON *syntax* is repaired: the recovered value is the raw substring,
  byte for byte, so no semantic reinterpretation can occur. A value that looks
  like a JSON token (``{``, ``[``, a digit, ``true``/``false``/``null`` …) is
  refused — quoting it would change its type, not its syntax.
* Only a **trailing** unquoted value is considered; a value followed by another
  member is ambiguous and is refused.
* A candidate whose swallowed value still carries key-like tokens (``"k":``) is
  refused.

Returns the parsed mapping on success, ``None`` when the raw text cannot be
repaired safely — the caller then falls back to a bounded resend or the plain
drop.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["repair_tool_args"]

#: ``"key":`` of a member (the key captured); escaped keys are skipped.
_KEY_COLON = re.compile(r'"([^"\\]*)"\s*:\s*')

#: A quote that may start a following member (``"`` … ``"`` ``:``) — found inside
#: the swallowed value, the cut point is ambiguous.
_KEY_LIKE = re.compile(r'"\s*:')

#: Characters that start a valid JSON token — the "value" is a different parse
#: failure (a Python literal, a nested structure) and quoting changes its type.
_JSON_TOKEN_START = frozenset('"{[-0123456789tfnTFN')


def _try_mapping(text: str) -> dict[str, Any] | None:
    """Strict parse; only a mapping counts (arrays/scalars are refused)."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _strip_trailing_commas(raw: str) -> str | None:
    """Remove commas that sit directly before a closing brace/bracket.

    String-aware: a comma inside a string literal is never touched, so the
    rewrite can only drop true JSON syntax noise. Returns ``None`` when nothing
    changed.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < len(raw) and raw[j] in " \t\r\n":
                j += 1
            if j < len(raw) and raw[j] in "}]":
                changed = True
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out) if changed else None


def _quote_unquoted_tail(raw: str) -> dict[str, Any] | None:
    """Quote a trailing unquoted value: ``{"k": cmd text}`` → ``{"k": "cmd text"}``."""
    close = raw.rfind("}")
    if close <= 0:
        return None
    matches = list(_KEY_COLON.finditer(raw))
    if not matches:
        return None
    member = matches[-1]
    value_start = member.end()
    if value_start >= close:
        return None
    value = raw[value_start:close]
    if not value or value[0] in _JSON_TOKEN_START or value.strip() != value:
        return None
    if _KEY_LIKE.search(value):
        return None
    candidate = f"{raw[:value_start]}{json.dumps(value)}{raw[close:]}"
    parsed = _try_mapping(candidate)
    if parsed is None:
        return None
    key = member.group(1)
    if parsed.get(key) != value:
        return None
    head_keys = {m.group(1) for m in matches[:-1]}
    if set(parsed.keys()) != head_keys | {key}:
        return None
    return parsed


def repair_tool_args(raw: str | None) -> dict[str, Any] | None:
    """Repair a tool call's raw arguments string into a mapping, or refuse.

    Args:
        raw: the raw ``arguments`` string as carried by
            ``AIMessage.invalid_tool_calls``.

    Returns:
        The parsed mapping — for already-valid JSON the strict parse result, for
        a repairable miss the
        quote-only reconstruction. ``None`` when the text is not a string, is
        empty, or cannot be repaired under the rules above.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    parsed = _try_mapping(raw)
    if parsed is not None:
        return parsed

    stripped = _strip_trailing_commas(raw)
    if stripped is not None:
        parsed = _try_mapping(stripped)
        if parsed is not None:
            return parsed

    return _quote_unquoted_tail(raw)
