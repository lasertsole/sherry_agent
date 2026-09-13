"""WebSocket stream retry and flattening tunables."""

from typing import TypedDict


class WsStreamConfig(TypedDict):
    """WebSocket stream retry and flattening tunables."""

    max_continuation_retries: int
    max_reasoning_only_retries: int
    drain_error_backoff_s: float
    max_flatten_depth: int
    stream_diag_headers: tuple[str, ...]


WS_STREAM: WsStreamConfig = {
    "max_continuation_retries": 4,
    "max_reasoning_only_retries": 2,
    "drain_error_backoff_s": 1.0,
    "max_flatten_depth": 5,
    "stream_diag_headers": (
        "cf-ray",
        "cf-cache-status",
        "x-request-id",
        "x-openrouter-provider",
        "x-openrouter-model",
        "server",
        "via",
        "x-vercel-id",
    ),
}
