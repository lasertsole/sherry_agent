"""LLM client construction defaults across main/aux/reasoner/local models."""

from typing import TypedDict


class LlmClientDefaultsConfig(TypedDict):
    """LLM client construction defaults across main/aux/reasoner/local models."""

    main_max_retries: int
    main_timeout: int
    main_stream_chunk_timeout: int
    fallback_max_retries: int
    fallback_timeout: int
    aux_max_retries: int
    aux_remote_max_tokens: int
    reasoner_max_retries: int
    reasoner_max_tokens_cap: int
    local_n_ctx: int
    local_temperature: float
    local_max_tokens: int
    local_n_gpu_layers: int
    ittt_remote_temperature: float
    ittt_remote_max_retries: int
    vttt_remote_temperature: float
    vttt_remote_max_retries: int


LLM_CLIENT_DEFAULTS: LlmClientDefaultsConfig = {
    "main_max_retries": 2,
    "main_timeout": 120,
    "main_stream_chunk_timeout": 60,
    "fallback_max_retries": 2,
    "fallback_timeout": 120,
    "aux_max_retries": 2,
    "aux_remote_max_tokens": 121072,
    "reasoner_max_retries": 2,
    "reasoner_max_tokens_cap": 65536,
    "local_n_ctx": 4096,
    "local_temperature": 0.0,
    "local_max_tokens": 4096,
    "local_n_gpu_layers": -1,
    "ittt_remote_temperature": 0.8,
    "ittt_remote_max_retries": 2,
    "vttt_remote_temperature": 0.8,
    "vttt_remote_max_retries": 2,
}
