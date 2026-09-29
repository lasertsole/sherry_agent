"""Secret redaction applied to what the model reads (B1/B2).

The engine in ``agent/security/redact.py`` is also wired into the log pipeline
(always on, no switch). This module decides the *tool output* half: which tools'
results are redacted before they reach the model.

The default set is deliberately narrow — the surfaces where a credential is a
leak and not the subject of the work:

* ``terminal`` / ``python_repl`` — they print env vars, ``curl -v`` traces and
  config dumps for a living;
* the untrusted set (``web_search``/``tavily_search``/``message_search``/``mcp_*``),
  because remote content is not something the local session must read verbatim.

File tools (``read_file``/``patch_file``/``write_file``) are NOT in the set: the
agent edits its own configuration, and masking a value there would make the
round trip lossy — it would read «redacted», then write it back. Widening the set
is one line here; narrowing the engine's over-redaction is a decision for the
rule, not for this list.
"""

from typing import TypedDict


class RedactionConfig(TypedDict):
    """Where redaction is applied beyond the log pipeline."""

    # Master switch for the tool-output path. The engine's own SHERRY_REDACT
    # switch still applies underneath (this flag only decides whether the
    # middleware asks at all).
    tool_output_enabled: bool
    # Tools whose result is redacted before entering model context.
    tool_output_tools: frozenset[str]
    # Prefix rule mirroring the untrusted-output policy, so a tool added later
    # (an MCP server) is covered without editing this list.
    tool_output_prefixes: tuple[str, ...]


REDACTION: RedactionConfig = {
    "tool_output_enabled": True,
    "tool_output_tools": frozenset(
        {"terminal", "python_repl", "web_search", "tavily_search", "message_search"}
    ),
    "tool_output_prefixes": ("mcp_",),
}
