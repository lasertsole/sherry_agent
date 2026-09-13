"""MesMemory SQLite connection and FTS query caps."""

from typing import TypedDict


class MesMemoryConfig(TypedDict):
    """MesMemory SQLite connection and FTS query caps."""

    busy_timeout_s: float
    connect_attempts: int
    retry_delay_s: float
    max_query_tokens: int
    max_token_chars: int
    max_wildcard_terms: int


MES_MEMORY: MesMemoryConfig = {
    "busy_timeout_s": 10.0,
    "connect_attempts": 5,
    "retry_delay_s": 0.2,
    "max_query_tokens": 64,
    "max_token_chars": 64,
    "max_wildcard_terms": 4,
}
