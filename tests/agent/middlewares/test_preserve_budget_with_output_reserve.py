"""Output-reserve formula: effective context = ctx - max_output (M3)."""

from __future__ import annotations

import pytest

from agent.middlewares.summarization.compression import CompressionMixin
from agent.middlewares.summarization.overflow import OverflowMixin

pytestmark = [pytest.mark.unit]


class _Harness(CompressionMixin, OverflowMixin):
    """The mixin pair with a fixed context window and no real middleware state."""

    def __init__(self, ctx: int) -> None:
        self._main_llm_context_window = ctx

    @staticmethod
    def _estimate_tokens(messages) -> int:
        return 0

    @staticmethod
    def _get_reported_tokens(messages) -> int:
        return 0


def _patch_output(monkeypatch: pytest.MonkeyPatch, value: int | None) -> None:
    import models.LLMs.main_llm as main_llm

    monkeypatch.setattr(main_llm, "get_max_output_tokens", lambda: value)


def test_preserve_budget_uses_effective_context(monkeypatch: pytest.MonkeyPatch):
    _patch_output(monkeypatch, 16384)
    harness = _Harness(32768)

    # (32768 - 16384) * 0.25 = 4096, inside the min/max clamp.
    assert harness._calculate_preserve_budget() == 4096


def test_preserve_budget_without_max_output_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    _patch_output(monkeypatch, None)
    harness = _Harness(32768)

    # M3: legacy ctx * 0.25 = 8192.
    assert harness._calculate_preserve_budget() == 8192


def test_usable_budget_subtracts_both_reserves(monkeypatch: pytest.MonkeyPatch):
    _patch_output(monkeypatch, 16384)
    harness = _Harness(32768)

    # 32768 - 16384 output - 16000 compression reserve = 384.
    assert harness._usable_budget() == 384

    _patch_output(monkeypatch, None)
    assert harness._usable_budget() == 32768 - 16000


def test_preemptive_pressure_uses_the_effective_window(monkeypatch: pytest.MonkeyPatch):
    _patch_output(monkeypatch, 16384)
    harness = _Harness(32768)
    monkeypatch.setattr(harness, "_estimate_tokens", lambda messages: 13000)

    # 13000 / 16384 = 0.79 -> soft overflow (truncate); 13000 / 32768 = 0.40 fits.
    assert harness._preemptive_check([], "s") == "truncate_only"

    _patch_output(monkeypatch, None)
    assert harness._preemptive_check([], "s") is None


def test_reserve_is_clamped_to_the_context_window(monkeypatch: pytest.MonkeyPatch):
    _patch_output(monkeypatch, 40000)
    harness = _Harness(32768)

    assert harness._output_reserve() == 32768
    assert harness._effective_context_window() == 0
    assert harness._calculate_preserve_budget() == 2000  # MIN_PRESERVE_TOKENS
