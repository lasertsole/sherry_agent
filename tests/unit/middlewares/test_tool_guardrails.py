"""Unit tests for agent/middlewares/tool_guardrails.py (Task 1: ping-pong + arg-churn).

Load iron rule: the module is loaded via ``importlib.util.spec_from_file_location``
by absolute path — NEVER ``from agent.middlewares import ...`` (tests/unit/subagent/
conftest.py installs permanent sys.modules stubs that would shadow the real module).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "agent" / "middlewares" / "tool_guardrails.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("tool_guardrails_under_test", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["tool_guardrails_under_test"] = mod
_spec.loader.exec_module(mod)

ALLOW = mod.GuardrailAction.ALLOW
WARN = mod.GuardrailAction.WARN
BLOCK = mod.GuardrailAction.BLOCK
HALT = mod.GuardrailAction.HALT


# --------------------------------------------------------------------------- helpers
def make_mw(**cfg):
    return mod.ToolGuardrails(mod.ToolCallGuardrailConfig(**cfg))


def call(mw, gs, name, args_hash, result_hash, is_error=False, is_idempotent=True):
    """Mimic production order: record appended BEFORE _evaluate (impl L263-272)."""
    gs.records.append(
        mod._ToolCallRecord(
            name=name, args_hash=args_hash, is_error=is_error, result_hash=result_hash
        )
    )
    return mw._evaluate(gs, name, args_hash, result_hash, is_error, is_idempotent)


IDEMPOTENT_TOOL = SimpleNamespace(metadata={"idempotent": True})


def wrap_call(mw, sess, name, args, content, call_id, status="success", tool=IDEMPOTENT_TOOL):
    request = ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=tool,
        state={"session_id": sess},
        runtime=None,
    )
    result = ToolMessage(content=content, tool_call_id=call_id, name=name, status=status)
    return mw._wrap_tool_call_impl(request, result)


# ------------------------------------------------------------- sanity (load contract)
def test_module_sanity_real_class_instantiable():
    mw = mod.ToolGuardrails()
    assert mw is not None
    assert callable(mw._evaluate)
    cfg = mod.ToolCallGuardrailConfig()
    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False


# ---------------------------------------------------------------- regression group
def test_regression_exact_failure_warn_then_block():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = [call(mw, gs, "T", "args1", None, is_error=True) for _ in range(5)]
    assert actions[0] == ALLOW
    assert actions[1] == WARN  # exact_failure_warn_after=2
    assert actions[4] == BLOCK  # exact_failure_block_after=5
    assert "T" in gs.blocked_tools
    assert gs.last_pathology is None  # legacy pathologies never set last_pathology


def test_regression_exact_failure_warn_message():
    mw = make_mw()
    sess = "sess-reg-exact"
    out1 = wrap_call(mw, sess, "T", {"a": 1}, "boom", "c1", status="error")
    assert out1.content == "boom"  # call 1: allow → passthrough
    out2 = wrap_call(mw, sess, "T", {"a": 1}, "boom", "c2", status="error")
    assert "exact failure repetition" in out2.content
    assert "⚠" in out2.content


def test_regression_same_tool_failure_warn_then_halt():
    mw = make_mw(hard_stop_enabled=True)
    gs = mod._TurnGuardrailState()
    actions = [call(mw, gs, "T", f"args{i}", None, is_error=True) for i in range(8)]
    assert actions[1] == ALLOW
    assert actions[2] == WARN  # 3rd call: same_tool_failure_warn_after=3
    assert actions[7] == HALT  # 8th call: same_tool_failure_halt_after=8 (hard_stop)
    assert gs.halt_decision == HALT
    assert max(gs.exact_failure_counts.values()) == 1  # args differ → exact never accumulates


def test_regression_no_progress_warn_then_block():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = [call(mw, gs, "T", "args1", "h1") for _ in range(5)]
    assert actions[0] == ALLOW
    assert actions[1] == WARN  # no_progress_warn_after=2
    assert actions[4] == BLOCK  # no_progress_block_after=5
    assert "T" in gs.blocked_tools
    # at WARN@2 pair count is 1 (<4); at BLOCK@5 the gate skips new steps entirely
    assert gs.last_pathology is None


# ------------------------------------------------------------------- new: ping-pong
def test_ping_pong_symmetric_warn_and_block():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    # alternating A,B with unique result hashes → legacy no-progress never fires
    seq = [("A" if i % 2 == 1 else "B", f"h{i}") for i in range(1, 8)]  # A,B,A,B,A,B,A
    actions = []
    for i, (name, h) in enumerate(seq, start=1):
        act = call(mw, gs, name, f"args-{name}", h)
        actions.append(act)
        if i == 5:  # pair count 4 == ping_pong_warn_after
            assert act == WARN
            assert gs.last_pathology == ("ping_pong", 4, 6)
        if i == 6:  # pair count 5
            assert act == WARN
            assert gs.last_pathology == ("ping_pong", 5, 6)
    assert all(a == ALLOW for a in actions[:4])  # pair counts 1..3 < warn
    assert actions[6] == BLOCK  # pair count 6 == ping_pong_block_after
    assert "A" in gs.blocked_tools
    assert gs.last_pathology == ("ping_pong", 6, 6)
    assert gs.ping_pong_counts["A,B"] == 6


def test_ping_pong_asymmetric_reset():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    for i, name in enumerate(["A", "B", "A", "B"], start=1):  # 3 qualifying pairs
        assert call(mw, gs, name, f"args-{name}", f"h{i}") == ALLOW
    assert gs.ping_pong_counts["A,B"] == 3
    assert gs.last_pathology is None
    # asymmetric: B succeeds as non-idempotent (result_hash None) → the compared
    # pair (B,B) resets immediately; stale "A,B" resets on the next call (prev=B has no hash)
    assert call(mw, gs, "B", "args-B", None, is_idempotent=False) == ALLOW
    assert gs.ping_pong_counts["B,B"] == 0
    assert call(mw, gs, "A", "args-A", "h6") == ALLOW  # zeroes stale "A,B"
    assert gs.ping_pong_counts["A,B"] == 0
    # accumulation restarts from 0: 3 fresh qualifying pairs → still below warn(4)
    for name, h in [("B", "h7"), ("A", "h8"), ("B", "h9")]:
        assert call(mw, gs, name, f"args-{name}", h) == ALLOW
    assert gs.ping_pong_counts["A,B"] == 3
    assert gs.last_pathology is None


# ------------------------------------------------------------------ new: arg-churn
def test_arg_churn_warn_at_three_variants():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = []
    for v in range(1, 4):
        actions.append(call(mw, gs, "T", f"err-args-{v}", None, is_error=True))
        for _ in range(3):
            actions.append(call(mw, gs, "T", f"v-args-{v}", f"h{v}"))
    assert max(gs.ping_pong_counts.values()) <= 2  # errors keep ping-pong suppressed
    assert actions[7] == WARN  # after 2 variants: only legacy no-progress WARN
    assert actions[-1] == WARN  # distinct qualified variants hit 3 → churn WARN
    assert gs.last_pathology == ("argument_churn", 3, 5)
    assert len(gs.arg_churn_variants) == 3
    assert all(c == 3 for c in gs.arg_churn_variants.values())
    assert gs.arg_churn_last_result == "h3"


def test_arg_churn_block_at_five_variants():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = []
    for v in range(1, 6):
        actions.append(call(mw, gs, "T", f"err-args-{v}", None, is_error=True))
        for _ in range(3):
            actions.append(call(mw, gs, "T", f"v-args-{v}", f"h{v}"))
    assert actions[-1] == BLOCK  # distinct qualified variants hit 5 → churn BLOCK
    assert "T" in gs.blocked_tools
    assert gs.last_pathology == ("argument_churn", 5, 5)


def test_arg_churn_reset_on_progress():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    call(mw, gs, "T", "v-args-1", "h1")
    assert gs.arg_churn_variants == {("T", "v-args-1"): 1}
    assert gs.arg_churn_last_result == "h1"
    # non-idempotent success = real progress → churn state fully cleared
    assert call(mw, gs, "T", "v-args-2", None, is_idempotent=False) == ALLOW
    assert gs.arg_churn_variants == {}
    assert gs.arg_churn_last_result == ""


# ----------------------------------------------------- new: wrap-level message routing
def test_wrap_message_routing_ping_pong_warn():
    mw = make_mw()
    sess = "sess-pp-warn"
    msgs = []
    for i in range(1, 6):
        name = "A" if i % 2 == 1 else "B"
        msgs.append(wrap_call(mw, sess, name, {"k": name}, "same-output", f"c{i}"))
    assert msgs[0].content == "same-output"  # allow → passthrough
    assert "idempotent no-progress" in msgs[2].content  # call 3: legacy WARN (pair=2)
    assert "ping-pong loop" not in msgs[2].content
    assert "idempotent no-progress" in msgs[3].content  # call 4: pair=3
    assert "ping-pong loop" in msgs[4].content  # call 5: pair=4 → ping-pong message
    assert "⚠" in msgs[4].content
    gs = mw._get_state(sess)
    assert gs.last_pathology == ("ping_pong", 4, 6)


def test_wrap_message_routing_arg_churn_block():
    mw = make_mw()
    sess = "sess-ac-block"
    outs = []
    for v in range(1, 6):
        outs.append(wrap_call(mw, sess, "T", {"e": v}, f"err{v}", f"e{v}", status="error"))
        for j in range(3):
            outs.append(wrap_call(mw, sess, "T", {"v": v}, f"res{v}", f"s{v}-{j}"))
    assert "argument churn" in outs[11].content  # end of variant 3: churn WARN
    assert "⚠" in outs[11].content
    assert outs[-1].status == "error"  # end of variant 5: churn BLOCK
    assert "argument churn" in outs[-1].content
    assert "🚫" in outs[-1].content
    gs = mw._get_state(sess)
    assert gs.last_pathology == ("argument_churn", 5, 5)
    assert "T" in gs.blocked_tools
