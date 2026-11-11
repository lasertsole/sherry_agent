"""Loopback daemons that host the local GGUF models (embed / reranker / chat).

The main process is the orchestration host (WS, middleware chain, every session
state), so the local weights are moved into detached child processes: one daemon
per model family loads its GGUF once, answers loopback JSON and exits after an
idle period. The clients keep the same public surface; only the transport
changes (in-process ``Llama`` → HTTP).

Every value here is a shipped default with an environment override, so a
deployment can move a port or lengthen the cold-start budget without a code
change. The switches exist as an escape hatch: disabling one restores the old
in-process path for that family (no daemon is spawned at all).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypedDict


class LocalDaemonsConfig(TypedDict):
    """Daemon endpoints, cold-start budgets, idle shutdown and kill switches."""

    host: str
    embed_port: int
    reranker_port: int
    auxiliary_port: int
    liveness_timeout_s: float
    embed_ready_wait_s: float
    reranker_ready_wait_s: float
    auxiliary_ready_wait_s: float
    embed_http_timeout_s: float
    reranker_http_timeout_s: float
    auxiliary_http_timeout_s: float
    embed_idle_timeout_s: float
    reranker_idle_timeout_s: float
    auxiliary_idle_timeout_s: float
    embed_daemon_enabled: bool
    reranker_daemon_enabled: bool
    auxiliary_daemon_enabled: bool
    # Auxiliary chat context window. 40 960 is the historical in-process value;
    # the KV cache scales linearly with it, so a deployment that measured a
    # smaller input distribution can lower it without touching the code.
    auxiliary_n_ctx: int
    # Upper bound on a single /embeddings batch (a malformed client must not
    # make the daemon allocate an unbounded prompt batch).
    max_batch_inputs: int


def _int_env(env: Mapping[str, str], key: str, default: int, *, minimum: int = 1) -> int:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _float_env(env: Mapping[str, str], key: str, default: float, *, minimum: float = 0.1) -> float:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _bool_env(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(env.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw not in ("false", "0", "no", "off")


def build_local_daemons(env: Mapping[str, str] | None = None) -> LocalDaemonsConfig:
    """Resolve the daemon configuration from *env* (process env by default)."""
    source: Mapping[str, str] = os.environ if env is None else env
    return {
        "host": "127.0.0.1",
        "embed_port": _int_env(source, "EMBEDDING_DAEMON_PORT", 9012, minimum=1),
        "reranker_port": _int_env(source, "RERANKER_DAEMON_PORT", 9013, minimum=1),
        "auxiliary_port": _int_env(source, "AUXILIARY_LLM_DAEMON_PORT", 9014, minimum=1),
        "liveness_timeout_s": _float_env(source, "LOCAL_DAEMON_LIVENESS_TIMEOUT_S", 1.0),
        # bge-m3 hot loads in ~1.3 s; a cold page cache adds disk I/O.
        "embed_ready_wait_s": _float_env(source, "EMBEDDING_DAEMON_READY_WAIT_S", 20.0),
        # A first use may have to download + parse the 636 MB GGUF.
        "reranker_ready_wait_s": _float_env(source, "RERANKER_DAEMON_READY_WAIT_S", 60.0),
        # A 9B Q4 chat model needs minutes to map/allocate on a cold start.
        "auxiliary_ready_wait_s": _float_env(source, "AUXILIARY_LLM_DAEMON_READY_WAIT_S", 300.0),
        "embed_http_timeout_s": _float_env(source, "EMBEDDING_DAEMON_HTTP_TIMEOUT_S", 120.0),
        "reranker_http_timeout_s": _float_env(source, "RERANKER_DAEMON_HTTP_TIMEOUT_S", 120.0),
        "auxiliary_http_timeout_s": _float_env(
            source, "AUXILIARY_LLM_DAEMON_HTTP_TIMEOUT_S", 600.0
        ),
        "embed_idle_timeout_s": _float_env(
            source, "EMBEDDING_DAEMON_IDLE_TIMEOUT_S", 600.0, minimum=5.0
        ),
        "reranker_idle_timeout_s": _float_env(
            source, "RERANKER_DAEMON_IDLE_TIMEOUT_S", 600.0, minimum=5.0
        ),
        "auxiliary_idle_timeout_s": _float_env(
            source, "AUXILIARY_LLM_DAEMON_IDLE_TIMEOUT_S", 600.0, minimum=5.0
        ),
        "embed_daemon_enabled": _bool_env(source, "EMBEDDING_DAEMON_ENABLED", True),
        "reranker_daemon_enabled": _bool_env(source, "RERANKER_DAEMON_ENABLED", True),
        "auxiliary_daemon_enabled": _bool_env(source, "AUXILIARY_LLM_DAEMON_ENABLED", True),
        "auxiliary_n_ctx": _int_env(source, "AUXILIARY_LLM_N_CTX", 40960, minimum=2048),
        "max_batch_inputs": _int_env(source, "LOCAL_DAEMON_MAX_BATCH_INPUTS", 256, minimum=1),
    }


LOCAL_DAEMONS: LocalDaemonsConfig = build_local_daemons()
