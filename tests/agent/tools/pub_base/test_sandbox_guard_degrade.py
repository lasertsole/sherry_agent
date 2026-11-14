"""The AUTO-degrade scope guard: no approval path means fail closed.

``SANDBOX_POLICY=auto`` without a usable OS backend degrades to an unsandboxed
run, which is only acceptable where a human can approve it (the main turn's HITL
card). A subagent / background caller has no such middleware, so the mixin must
refuse instead of leaning on the regex blacklist alone.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import ToolException

import agent.tools.pub_base.sandbox as sandbox
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin

pytestmark = [pytest.mark.unit]


class _Guard(SandboxGuardMixin):
    metadata: dict

    def __init__(self, scope: str) -> None:
        self.metadata = {"caller_scope": scope}


@pytest.fixture()
def knobs(monkeypatch: pytest.MonkeyPatch):
    """Control the policy and whether a usable backend exists."""

    def _set(policy: SandboxPolicy, usable: bool) -> None:
        monkeypatch.setattr("agent.tools.pub_base.sandbox_guard.read_policy", lambda: policy)
        monkeypatch.setattr(sandbox, "sandbox_effective", lambda requested=True: usable)

    return _set


class TestDegradeGuard:
    @pytest.mark.parametrize("scope", ["subagent", "background", "scheduler"])
    def test_a_non_main_caller_is_refused_without_a_backend(self, knobs, scope):
        knobs(SandboxPolicy.AUTO, usable=False)

        with pytest.raises(ToolException) as excinfo:
            _Guard(scope)._deny_unsandboxed_degrade(True)

        message = str(excinfo.value)
        assert "no operator approval path" in message
        assert scope in message
        assert "SANDBOX_POLICY=off" in message

    def test_main_keeps_the_hitl_gated_degrade(self, knobs):
        knobs(SandboxPolicy.AUTO, usable=False)

        _Guard("main")._deny_unsandboxed_degrade(True)  # no raise

    def test_a_usable_backend_allows_every_scope(self, knobs):
        knobs(SandboxPolicy.AUTO, usable=True)

        _Guard("subagent")._deny_unsandboxed_degrade(True)  # no raise

    def test_off_is_an_explicit_operator_choice(self, knobs):
        knobs(SandboxPolicy.OFF, usable=False)

        _Guard("subagent")._deny_unsandboxed_degrade(True)  # no raise

    def test_required_is_left_to_the_backend_error(self, knobs):
        knobs(SandboxPolicy.REQUIRED, usable=False)

        # get_backend raises for REQUIRED; this guard must not pre-empt it.
        _Guard("subagent")._deny_unsandboxed_degrade(True)

    def test_an_unrequested_sandbox_is_not_gated(self, knobs):
        knobs(SandboxPolicy.AUTO, usable=False)

        _Guard("subagent")._deny_unsandboxed_degrade(False)  # no raise
