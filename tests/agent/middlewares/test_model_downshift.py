"""ModelDownshift: a session model profile with a smaller context window.

The compression middleware budgets against the session's declared window, so a
switch to a smaller model compacts at the next pre-request check and the
trajectory event carries ``reason=model_downshift``.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import ModelRequest

from agent.middlewares.summarization import overflow as overflow_mod
from agent.middlewares.summarization.compression import CompressionMixin
from agent.middlewares.summarization.overflow import OverflowMixin, ROUTE_COMPACT_ONLY
from runtime import state_register_mem
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]


class _Harness(CompressionMixin, OverflowMixin):
    def __init__(self, ctx: int) -> None:
        self._main_llm_context_window = ctx

    @staticmethod
    def _estimate_tokens(messages) -> int:
        return 0

    @staticmethod
    def _get_reported_tokens(messages) -> int:
        return 0

    def _record_compaction_bookkeeping(self, session_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_session():
    state_register_mem.clear_session("downshift-sess")
    yield
    state_register_mem.clear_session("downshift-sess")


def _set_override(window: str | None) -> None:
    descriptor = {"model": "small-model"}
    if window is not None:
        descriptor["context_window"] = window
    state_register_mem.set_state("downshift-sess", StateKey.LLM_MAIN_MODEL, descriptor)


def test_session_window_uses_the_override(monkeypatch):
    import models.LLMs.main_llm as main_llm

    monkeypatch.setattr(main_llm, "get_max_output_tokens", lambda: None)
    harness = _Harness(131_072)
    _set_override("32768")

    assert harness._session_context_window("downshift-sess") == 32_768
    assert harness._effective_context_window("downshift-sess") == 32_768
    assert harness._calculate_preserve_budget("downshift-sess") == 8_192


def test_missing_override_keeps_the_env_window(monkeypatch):
    import models.LLMs.main_llm as main_llm

    monkeypatch.setattr(main_llm, "get_max_output_tokens", lambda: None)
    harness = _Harness(131_072)

    assert harness._session_context_window("downshift-sess") == 131_072
    assert harness._calculate_preserve_budget("downshift-sess") == 15_000


def test_compact_event_is_labelled_model_downshift(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        overflow_mod,
        "record_trajectory_event",
        lambda session_id, kind, payload: captured.append(payload),
    )
    harness = _Harness(131_072)
    _set_override("32768")

    harness._finish_compact(
        ModelRequest(model=None, messages=[], state={"session_id": "downshift-sess"}),
        ROUTE_COMPACT_ONLY,
        "downshift-sess",
        "T2",
        old_tokens=20_000,
        usable=1_000,
        messages_before=10,
    )

    assert captured
    assert captured[0]["reason"] == "model_downshift"
    assert captured[0]["phase"] == "pre_request"
    assert captured[0]["trigger"] == "T2"


def test_no_override_keeps_context_limit_reason(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        overflow_mod,
        "record_trajectory_event",
        lambda session_id, kind, payload: captured.append(payload),
    )
    harness = _Harness(131_072)

    harness._finish_compact(
        ModelRequest(model=None, messages=[], state={"session_id": "downshift-sess"}),
        ROUTE_COMPACT_ONLY,
        "downshift-sess",
        "T2",
        old_tokens=20_000,
        usable=1_000,
        messages_before=10,
    )

    assert captured[0]["reason"] == "context_limit"
