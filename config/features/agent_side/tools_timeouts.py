"""Per-tool timeouts, retry budgets, and schema size limits."""

from typing import TypedDict


class ToolsTimeoutsConfig(TypedDict):
    """Per-tool timeouts, retry budgets, and schema size limits."""

    web_search_timeout_seconds: int
    web_search_retry_backoff_min_s: int
    web_search_retry_backoff_max_s: int
    web_search_retry_max_attempts: int
    terminal_timeout_seconds: int
    #: Hard ceiling for a per-call ``timeout`` on the terminal tool: a
    #: requested value is clamped to this (compile/install jobs want
    #: minutes; nothing may hang a turn for hours).
    terminal_max_timeout_seconds: int
    #: Terminal stdout at or above this many bytes skips the in-memory read:
    #: the child writes straight to a temp file and only a head/tail preview
    #: comes back (0 = always read in memory, the old behaviour).
    fd_write_threshold_bytes: int
    python_repl_timeout_seconds: int
    ptc_timeout_seconds: int
    sandbox_bwrap_probe_timeout_seconds: int
    message_search_max_session_chars: int
    message_search_checkpoint_fallback_enabled: bool
    skill_view_max_name_length: int
    skill_view_max_description_length: int
    skill_manage_max_name_length: int
    skill_manage_max_description_length: int
    skill_manage_max_skill_content_chars: int
    skill_manage_umbrella_skill_char_target: int
    skill_manage_max_skill_file_bytes: int
    file_tools_read_default_limit: int
    file_tools_read_max_limit: int
    #: ``web_fetch``: wall-clock budget for one public fetch and for one local
    #: (opt-in) fetch, the byte cap on either body, and the character cap on the
    #: markdown handed back to the model. The context-eviction layer is the
    #: second net for whatever still arrives oversized.
    web_fetch_timeout_seconds: int
    web_fetch_local_timeout_seconds: int
    web_fetch_max_bytes: int
    web_fetch_max_output_chars: int
    #: Token budget for ONE ``sessions_messages`` response (the subagent
    #: checkpoint transcript) and for one message inside it. CJK-aware like the
    #: read_file budget: the same number is the same context cost in any language.
    session_messages_token_budget: int
    session_messages_per_message_token_budget: int
    #: Token budget for ONE read_file response. The window is CJK-aware (CJK
    #: costs ~2 chars/token, ASCII ~4), so the same number means the same
    #: context cost in every language; 0 restores the pure line-count window.
    read_file_token_budget: int
    #: Fraction of the budget a page may spend (the rest absorbs the line that
    #: crosses the boundary, the line numbers and the JSON envelope).
    read_file_budget_ratio: float
    file_tools_search_default_limit: int
    file_tools_search_max_limit: int
    file_tools_search_max_context: int
    file_tools_search_max_matches: int
    file_tools_search_time_budget_s: float
    file_tools_search_prune_dirs: list[str]
    question_option_max_length: int
    question_min_length: int
    question_custom_max_length: int
    #: One command typed into the toolbox's user terminal panel (a console, not
    #: the agent's tool): longer than the agent's own bound, because a human is
    #: watching it and can wait for a build step.
    user_terminal_timeout_seconds: int


TOOLS_TIMEOUTS: ToolsTimeoutsConfig = {
    "web_search_timeout_seconds": 15,
    "web_search_retry_backoff_min_s": 5,
    "web_search_retry_backoff_max_s": 45,
    "web_search_retry_max_attempts": 3,
    "terminal_timeout_seconds": 30,
    "terminal_max_timeout_seconds": 600,
    "fd_write_threshold_bytes": 100_000,
    "user_terminal_timeout_seconds": 120,
    "python_repl_timeout_seconds": 30,
    "ptc_timeout_seconds": 120,
    "sandbox_bwrap_probe_timeout_seconds": 3,
    "message_search_max_session_chars": 100_000,
    "message_search_checkpoint_fallback_enabled": True,
    "skill_view_max_name_length": 64,
    "skill_view_max_description_length": 1024,
    "skill_manage_max_name_length": 64,
    "skill_manage_max_description_length": 1024,
    "skill_manage_max_skill_content_chars": 100_000,
    "skill_manage_umbrella_skill_char_target": 15_000,
    "skill_manage_max_skill_file_bytes": 1_048_576,
    "file_tools_read_default_limit": 500,
    "file_tools_read_max_limit": 2000,
    # web_fetch: public pages get 30 s (a slow origin is common), a local dev
    # server 10 s (loopback either answers or is down); 10 MiB body cap matches
    # the media pipeline's fetch, and 50 K output chars is the model-visible cap
    # before eviction takes over.
    "web_fetch_timeout_seconds": 30,
    "web_fetch_local_timeout_seconds": 10,
    "web_fetch_max_bytes": 10_485_760,
    "web_fetch_max_output_chars": 50_000,
    # The subagent-transcript reader: ~8 messages of 500 tokens each, the same
    # order of magnitude as one read_file response.
    "session_messages_token_budget": 4000,
    "session_messages_per_message_token_budget": 500,
    "read_file_token_budget": 4000,
    "read_file_budget_ratio": 0.85,
    "file_tools_search_default_limit": 50,
    "file_tools_search_max_limit": 200,
    "file_tools_search_max_context": 5,
    "file_tools_search_max_matches": 10_000,
    "file_tools_search_time_budget_s": 5.0,
    "file_tools_search_prune_dirs": ["proc", "sys", "dev"],
    "question_option_max_length": 30,
    "question_min_length": 2,
    "question_custom_max_length": 6,
}
