"""The streaming guard: nothing is emitted before it has been scanned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.middlewares.safety_filter import rules as rules_module
from agent.middlewares.safety_filter.stream_guard import StreamTextGuard

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rules_module, "WORKSPACE_DIR", tmp_path)
    rules_module.clear_rules_cache()
    yield tmp_path
    rules_module.clear_rules_cache()


def _rules(workspace: Path, rules: list[dict]) -> None:
    (workspace / "sensitive_words.json").write_text(
        json.dumps({"version": 1, "rules": rules}), encoding="utf-8"
    )
    rules_module.clear_rules_cache()


def _feed_all(guard: StreamTextGuard, deltas: list[str]) -> str:
    emitted = "".join(guard.feed(delta) for delta in deltas)
    return emitted + guard.flush()


class TestHoldBack:
    def test_holds_a_window_then_emits_only_scanned_text(self, workspace: Path):
        _rules(workspace, [])
        guard = StreamTextGuard(window=3)

        assert guard.feed("abc") == ""  # shorter than the window: held
        assert guard.feed("defg") == "abcd"  # 4 keys cleared, 3 held back
        assert guard.flush() == "efg"

    def test_a_block_latches_and_swallows_the_rest(self, workspace: Path):
        # Window >= pattern length: the match is caught before it is emitted.
        _rules(workspace, [{"pattern": "secret", "type": "contains", "action": "block"}])
        guard = StreamTextGuard(window=10)

        emitted = _feed_all(guard, ["the se", "cret is", " out"])

        assert "secret" not in emitted
        assert guard.blocked is True

    def test_a_match_straddling_the_release_boundary_is_cut_on_sight(self, workspace: Path):
        # Window 2 < the 6-char pattern: the match can only complete in the
        # held tail, and the whole-buffer scan latches as soon as it does —
        # nothing of the match (and nothing after it) is emitted.
        _rules(workspace, [{"pattern": "secret", "type": "contains", "action": "block"}])
        guard = StreamTextGuard(window=2)

        emitted = _feed_all(guard, ["xx se", "cret", " tail"])

        assert emitted == "xx "
        assert "secret" not in emitted
        assert "tail" not in emitted
        assert guard.blocked is True

    def test_a_block_inside_the_tail_is_caught_at_flush(self, workspace: Path):
        _rules(workspace, [{"pattern": "secret", "type": "contains", "action": "block"}])
        guard = StreamTextGuard(window=10)

        emitted = guard.feed("hi secret")

        assert emitted == ""
        assert guard.flush() == ""
        assert guard.blocked is True

    def test_a_replace_is_applied_before_emission(self, workspace: Path):
        _rules(
            workspace,
            [
                {
                    "pattern": "secret",
                    "type": "contains",
                    "action": "replace",
                    "replacement": "[x]",
                }
            ],
        )
        guard = StreamTextGuard(window=8)

        emitted = _feed_all(guard, ["the secret", " end"])

        assert emitted == "the [x] end"

    def test_warn_only_reports_and_emits_the_text(self, workspace: Path):
        _rules(workspace, [{"pattern": "secret", "type": "contains", "action": "warn"}])
        guard = StreamTextGuard(window=8)

        emitted = _feed_all(guard, ["a secret"])

        assert emitted == "a secret"
        assert guard.hits and guard.hits[0].action == "warn"
        assert guard.blocked is False

    def test_the_scope_is_output_only(self, workspace: Path):
        _rules(
            workspace,
            [{"pattern": "secret", "type": "contains", "action": "block", "scope": "input"}],
        )
        guard = StreamTextGuard(window=8)

        assert _feed_all(guard, ["a secret"]) == "a secret"
        assert guard.blocked is False
