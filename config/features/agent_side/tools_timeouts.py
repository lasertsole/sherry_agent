"""Per-tool timeouts, retry budgets, and schema size limits."""

from typing import TypedDict


class ToolsTimeoutsConfig(TypedDict):
    """Per-tool timeouts, retry budgets, and schema size limits."""

    web_search_timeout_seconds: int
    web_search_retry_backoff_min_s: int
    web_search_retry_backoff_max_s: int
    web_search_retry_max_attempts: int
    terminal_timeout_seconds: int
    python_repl_timeout_seconds: int
    sandbox_bwrap_probe_timeout_seconds: int
    message_search_max_session_chars: int
    skill_view_max_name_length: int
    skill_view_max_description_length: int
    skill_manage_max_name_length: int
    skill_manage_max_description_length: int
    skill_manage_max_skill_content_chars: int
    skill_manage_umbrella_skill_char_target: int
    skill_manage_max_skill_file_bytes: int
    file_tools_read_default_limit: int
    file_tools_read_max_limit: int
    file_tools_search_default_limit: int
    file_tools_search_max_limit: int
    file_tools_search_max_context: int
    question_option_max_length: int
    question_min_length: int
    question_custom_max_length: int


TOOLS_TIMEOUTS: ToolsTimeoutsConfig = {
    "web_search_timeout_seconds": 15,
    "web_search_retry_backoff_min_s": 5,
    "web_search_retry_backoff_max_s": 45,
    "web_search_retry_max_attempts": 3,
    "terminal_timeout_seconds": 30,
    "python_repl_timeout_seconds": 30,
    "sandbox_bwrap_probe_timeout_seconds": 3,
    "message_search_max_session_chars": 100_000,
    "skill_view_max_name_length": 64,
    "skill_view_max_description_length": 1024,
    "skill_manage_max_name_length": 64,
    "skill_manage_max_description_length": 1024,
    "skill_manage_max_skill_content_chars": 100_000,
    "skill_manage_umbrella_skill_char_target": 15_000,
    "skill_manage_max_skill_file_bytes": 1_048_576,
    "file_tools_read_default_limit": 500,
    "file_tools_read_max_limit": 2000,
    "file_tools_search_default_limit": 50,
    "file_tools_search_max_limit": 200,
    "file_tools_search_max_context": 5,
    "question_option_max_length": 30,
    "question_min_length": 2,
    "question_custom_max_length": 6,
}
