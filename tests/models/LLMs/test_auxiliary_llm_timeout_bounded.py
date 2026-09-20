"""Hermetic bounded-time regression: a stalled auxiliary call must fail fast.

The aux remote client previously carried no ``timeout``, so the OpenAI SDK
default (600 s) applied to every attempt (``aux_max_retries`` + 1 = 3) — one
stalled aux call could wait silently for ~30 min. This test drives the REAL aux
client (``build_auxiliary_llm``) through the real OpenAI SDK against a localhost
endpoint that accepts TCP connections but never writes a response: with
``aux_timeout`` configured, the call must raise an API timeout error in bounded
time instead of waiting on the SDK default.

Placement: separate file from ``test_auxiliary_llm_timeout.py``. That module is
a ``unit`` test that only inspects the constructed client; this one uses the
real HTTP stack against a wire-level fake, so it is marked ``integration`` and
must not be collected into the unit group. No external network is touched.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator

import pytest
from openai import APITimeoutError

from config.features import LLM_CLIENT_DEFAULTS
from models.LLMs.auxiliary_llm.core import build_auxiliary_llm

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

_AUX_TIMEOUT_SECONDS = 2
# aux_timeout x (aux_max_retries + 1) + SDK retry backoff + process overhead.
_BOUNDED_BUDGET_SECONDS = 30
_STUB_HOLD_SECONDS = 60


@contextlib.contextmanager
def _stalled_endpoint() -> Iterator[int]:
    """Yield a localhost port that accepts TCP but never answers.

    Each accepted connection is parked on a threading.Event until the context
    exits, so the OpenAI client blocks in its read phase and the configured
    request timeout is what ends the wait.
    """
    stop = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]

    def _hold(conn: socket.socket) -> None:
        try:
            stop.wait(_STUB_HOLD_SECONDS)
        finally:
            conn.close()

    def _accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                # Listener closed by the context manager exit: expected.
                return
            threading.Thread(target=_hold, args=(conn,), daemon=True).start()

    accept_thread = threading.Thread(target=_accept_loop, daemon=True)
    accept_thread.start()
    try:
        yield port
    finally:
        stop.set()
        listener.close()
        accept_thread.join(timeout=2)


def _configure_remote_aux(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """Point the aux factory at the stalled local endpoint with a 2 s timeout."""
    monkeypatch.setitem(LLM_CLIENT_DEFAULTS, "aux_timeout", _AUX_TIMEOUT_SECONDS)
    monkeypatch.setenv("AUXILIARY_LLM_MODEL_LOCAL", "false")
    monkeypatch.setenv("AUXILIARY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AUXILIARY_LLM_API_NAME", "gpt-4o-mini")
    monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("AUXILIARY_LLM_API_BASE", f"http://127.0.0.1:{port}/v1")


def test_stalled_aux_call_fails_bounded_with_configured_timeout(monkeypatch) -> None:
    """A silent endpoint ends in APITimeoutError well inside the budget."""
    with _stalled_endpoint() as port:
        _configure_remote_aux(monkeypatch, port)
        model = build_auxiliary_llm()
        # A non-default value proves the registry key reaches the client.
        assert model.inner.request_timeout == _AUX_TIMEOUT_SECONDS

        started = time.monotonic()
        with pytest.raises(APITimeoutError):
            model.invoke("ping")
        elapsed = time.monotonic() - started

    assert elapsed < _BOUNDED_BUDGET_SECONDS, (
        f"a stalled aux call must fail within {_BOUNDED_BUDGET_SECONDS}s "
        f"(aux_timeout={_AUX_TIMEOUT_SECONDS}s x 3 attempts), took {elapsed:.1f}s"
    )
    # At least one full timeout window must have elapsed: rules out the call
    # failing instantly for an unrelated reason (DNS, refused connection).
    assert elapsed >= _AUX_TIMEOUT_SECONDS * 0.9, (
        f"expected the configured {_AUX_TIMEOUT_SECONDS}s window to be waited, "
        f"only {elapsed:.1f}s elapsed"
    )
