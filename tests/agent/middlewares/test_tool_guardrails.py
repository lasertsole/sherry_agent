"""Unit tests for agent/middlewares/tool_guardrails.py (Task 1: ping-pong + arg-churn).

Load iron rule: the module is loaded via ``importlib.util.spec_from_file_location``
by absolute path — NEVER ``from agent.middlewares import ...`` (tests/agent/tools/subagent/
conftest.py installs permanent sys.modules stubs that would shadow the real module).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
MODULE_PATH = REPO_ROOT / "agent" / "middlewares" / "tool_guardrails" / "core.py"

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


def _is_stagnant(gs, name, result_hash, is_idempotent):
    """Mirror _wrap_tool_call_impl: scan pre-append for same tool + result."""
    if not is_idempotent or result_hash is None:
        return False
    return any(rec.name == name and rec.result_hash == result_hash for rec in reversed(gs.records))


def call(mw, gs, name, args_hash, result_hash, is_error=False, is_idempotent=True):
    """Mimic production order: record appended BEFORE _evaluate (impl L263-272)."""
    is_stagnant = _is_stagnant(gs, name, result_hash, is_idempotent)
    gs.records.append(
        mod._ToolCallRecord(
            name=name,
            args_hash=args_hash,
            is_error=is_error,
            result_hash=result_hash,
            is_stagnant=is_stagnant,
        )
    )
    return mw._evaluate(gs, name, args_hash, result_hash, is_error, is_idempotent, is_stagnant)


IDEMPOTENT_TOOL = SimpleNamespace(metadata={"idempotent": True})
NON_IDEMPOTENT_TOOL = SimpleNamespace(metadata={"idempotent": False})


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
import pytest

pytestmark = [pytest.mark.unit]


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
    mw = make_mw(no_progress_warn_after=100, no_progress_block_after=200)
    gs = mod._TurnGuardrailState()
    # Alternating A,B with the SAME per-tool result → each call beyond the first
    # is stagnant (no progress), which is what ping-pong now requires.
    seq = [("A", "ha"), ("B", "hb")] * 4 + [("A", "ha")]  # 9 calls: A,B,A,B,A,B,A,B,A
    actions = []
    for i, (name, h) in enumerate(seq, start=1):
        act = call(mw, gs, name, f"args-{name}", h)
        actions.append(act)
        if i == 7:  # pair count 4 == ping_pong_warn_after
            assert act == WARN
            assert gs.last_pathology == ("ping_pong", 4, 6)
        if i == 8:  # pair count 5
            assert act == WARN
            assert gs.last_pathology == ("ping_pong", 5, 6)
    assert all(a == ALLOW for a in actions[:6])  # pair counts 1..3 < warn
    assert actions[8] == BLOCK  # pair count 6 == ping_pong_block_after
    assert "A" in gs.blocked_tools
    assert gs.last_pathology == ("ping_pong", 6, 6)
    assert gs.ping_pong_counts["A,B"] == 6


def test_ping_pong_asymmetric_reset():
    mw = make_mw(no_progress_warn_after=100, no_progress_block_after=200)
    gs = mod._TurnGuardrailState()
    for i, (name, h) in enumerate([("A", "ha"), ("B", "hb")] * 3, start=1):
        assert call(mw, gs, name, f"args-{name}", h) == ALLOW
    assert gs.ping_pong_counts["A,B"] == 3
    assert gs.last_pathology is None
    # asymmetric: B succeeds as non-idempotent (result_hash None) → the compared
    # pair (B,B) resets immediately; stale "A,B" resets on the next call (prev=B has no hash)
    assert call(mw, gs, "B", "args-B", None, is_idempotent=False) == ALLOW
    assert gs.ping_pong_counts["B,B"] == 0
    assert call(mw, gs, "A", "args-A", "ha") == ALLOW  # zeroes stale "A,B"
    assert gs.ping_pong_counts["A,B"] == 0
    # accumulation restarts from 0: 3 fresh qualifying pairs → still below warn(4)
    for name, h in [("B", "hb"), ("A", "ha"), ("B", "hb")]:
        assert call(mw, gs, name, f"args-{name}", h) == ALLOW
    assert gs.ping_pong_counts["A,B"] == 3
    assert gs.last_pathology is None


def test_changed_idempotent_result_is_progress_not_stagnation():
    """Given a repeated tool whose result CHANGED, When evaluated, Then it no
    longer counts as a ping-pong pair nor as an argument-churn variant call."""
    mw = make_mw(no_progress_warn_after=100, no_progress_block_after=200)
    gs = mod._TurnGuardrailState()
    assert call(mw, gs, "A", "args-A", "ha") == ALLOW
    assert call(mw, gs, "B", "args-B", "hb") == ALLOW
    assert call(mw, gs, "A", "args-A", "ha") == ALLOW  # stagnant, but pair not complete
    assert gs.arg_churn_variants == {("A", "args-A"): 1}
    # B now returns a changed result → progress: no pair increment, no variant count
    assert call(mw, gs, "B", "args-B", "hb2") == ALLOW
    assert gs.ping_pong_counts["A,B"] == 0
    assert gs.arg_churn_variants == {("A", "args-A"): 1}


# ------------------------------------------------------------------ new: arg-churn
def test_arg_churn_warn_at_three_variants():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = []
    for v in range(1, 4):
        actions.append(call(mw, gs, "T", f"err-args-{v}", None, is_error=True))
        for _ in range(4):  # first call is not stagnant; the next 3 count
            actions.append(call(mw, gs, "T", f"v-args-{v}", f"h{v}"))
    assert max(gs.ping_pong_counts.values()) <= 2  # errors keep ping-pong suppressed
    assert actions[7] == WARN  # after 2 variants: only legacy no-progress WARN
    assert actions[-1] == WARN  # distinct qualified variants hit 3 → churn WARN
    assert gs.last_pathology == ("argument_churn", 3, 5)
    assert len(gs.arg_churn_variants) == 3
    assert all(c == 3 for c in gs.arg_churn_variants.values())


def test_arg_churn_block_at_five_variants():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    actions = []
    for v in range(1, 6):
        actions.append(call(mw, gs, "T", f"err-args-{v}", None, is_error=True))
        for _ in range(4):
            actions.append(call(mw, gs, "T", f"v-args-{v}", f"h{v}"))
    assert actions[-1] == BLOCK  # distinct qualified variants hit 5 → churn BLOCK
    assert "T" in gs.blocked_tools
    assert gs.last_pathology == ("argument_churn", 5, 5)


def test_arg_churn_reset_on_progress():
    mw = make_mw()
    gs = mod._TurnGuardrailState()
    call(mw, gs, "T", "v-args-1", "h1")  # first sight → not stagnant, no count
    call(mw, gs, "T", "v-args-1", "h1")  # stagnant → variant count 1
    assert gs.arg_churn_variants == {("T", "v-args-1"): 1}
    # non-idempotent success = real progress → churn state fully cleared
    assert call(mw, gs, "T", "v-args-2", None, is_idempotent=False) == ALLOW
    assert gs.arg_churn_variants == {}


# ----------------------------------------------------- new: wrap-level message routing
def test_wrap_message_routing_ping_pong_warn():
    mw = make_mw()
    sess = "sess-pp-warn"
    msgs = []
    for i in range(1, 8):  # 7 calls: pair count reaches ping_pong_warn_after
        name = "A" if i % 2 == 1 else "B"
        msgs.append(wrap_call(mw, sess, name, {"k": name}, "same-output", f"c{i}"))
    assert msgs[0].content == "same-output"  # allow → passthrough
    assert "idempotent no-progress" in msgs[2].content  # call 3: legacy WARN (np=2)
    assert "ping-pong loop" not in msgs[2].content
    assert "idempotent no-progress" in msgs[3].content  # call 4: np=2 for B
    assert "ping-pong loop" in msgs[6].content  # call 7: pair=4 → ping-pong message
    assert "⚠" in msgs[6].content
    gs = mw._get_state(sess)
    assert gs.last_pathology == ("ping_pong", 4, 6)


def test_wrap_message_routing_arg_churn_block():
    mw = make_mw()
    sess = "sess-ac-block"
    outs = []
    for v in range(1, 6):
        outs.append(wrap_call(mw, sess, "T", {"e": v}, f"err{v}", f"e{v}", status="error"))
        for j in range(4):  # first call not stagnant; the next 3 qualify the variant
            outs.append(wrap_call(mw, sess, "T", {"v": v}, f"res{v}", f"s{v}-{j}"))
    assert "argument churn" in outs[14].content  # end of variant 3: churn WARN
    assert "⚠" in outs[14].content
    assert outs[-1].status == "error"  # end of variant 5: churn BLOCK
    assert "argument churn" in outs[-1].content
    assert "🚫" in outs[-1].content
    gs = mw._get_state(sess)
    assert gs.last_pathology == ("argument_churn", 5, 5)
    assert "T" in gs.blocked_tools


def test_records_are_bounded_by_max_records():
    """Given a long turn, When records exceed the cap, Then only the most
    recent _MAX_RECORDS records are retained."""
    mw = make_mw()
    sess = "sess-bounded"
    total = mod._MAX_RECORDS + 25
    for i in range(total):
        wrap_call(mw, sess, "T", {"i": i}, f"content-{i}", f"c{i}", tool=NON_IDEMPOTENT_TOOL)
    gs = mw._get_state(sess)
    assert len(gs.records) == mod._MAX_RECORDS
    assert gs.records[-1].args_hash == mod.args_hash({"i": total - 1})


def test_missing_tool_call_id_does_not_crash():
    """Given a tool_call dict without an id, When the guardrail blocks, Then the
    error ToolMessage is built defensively instead of raising KeyError."""
    mw = make_mw()
    sess = "sess-no-id"
    for i in range(1, 6):
        wrap_call(mw, sess, "T", {"a": 1}, "same", f"c{i}")
    request = ToolCallRequest(
        tool_call={"name": "T", "args": {"a": 1}},
        tool=IDEMPOTENT_TOOL,
        state={"session_id": sess},
        runtime=None,
    )
    out = mw._wrap_tool_call_impl(
        request, ToolMessage(content="same", tool_call_id="c6", name="T", status="success")
    )
    assert out.status == "error"
    assert out.tool_call_id == ""


def test_error_status_is_case_insensitive():
    """Given an uppercase ERROR status, When evaluated, Then it still counts as
    a failure (exact-failure WARN on the second identical call)."""
    mw = make_mw()
    sess = "sess-error-case"
    request = ToolCallRequest(
        tool_call={"name": "T", "args": {"a": 1}, "id": "c1"},
        tool=IDEMPOTENT_TOOL,
        state={"session_id": sess},
        runtime=None,
    )
    # LangChain's ToolMessage.status is a Literal["success", "error"], so the
    # uppercase status is injected with a duck-typed result object.
    upper = SimpleNamespace(content="boom", status="ERROR", tool_call_id="c1", name="T")
    out1 = mw._wrap_tool_call_impl(request, upper)
    assert out1 is upper  # first failure is ALLOW → passthrough
    out2 = wrap_call(mw, sess, "T", {"a": 1}, "boom", "c2", status="error")
    assert "exact failure repetition" in out2.content


# --------------------------------------------------------- new: recovery mode (Task 6)
def _mkreq(sess, name, args, call_id):
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=IDEMPOTENT_TOOL,
        state={"session_id": sess},
        runtime=None,
    )


def _drive_to_first_block(mw, sess, tool="T", args=None, content="same"):
    """5 identical idempotent successes → no-progress BLOCK on the 5th call."""
    args = args if args is not None else {"a": 1}
    outs = []
    for i in range(1, 6):
        outs.append(wrap_call(mw, sess, tool, args, content, f"c{i}"))
    return outs


def test_recovery_first_block_enters_mode_and_precheck_releases():
    mw = make_mw()
    sess = "sess-rec-unblock"
    outs = _drive_to_first_block(mw, sess)
    assert outs[0].content == "same"  # sanity: call 1 passes through untouched
    gs = mw._get_state(sess)
    assert gs.recovery_mode is True  # first BLOCK entered recovery
    assert "T" in gs.blocked_tools
    assert gs.recovery_violation_count == 0
    assert "recovery mode active" in outs[-1].content  # wrap-level recovery BLOCK message
    assert "⚠" in outs[-1].content
    # precheck releases the blocked tool (returns None) and removes it from the set
    assert mw._wrap_tool_call_precheck(_mkreq(sess, "T", {"a": 1}, "c10")) is None
    gs = mw._get_state(sess)
    assert "T" not in gs.blocked_tools


def test_recovery_violations_block_then_halt():
    mw = make_mw()
    sess = "sess-rec-halt"
    _drive_to_first_block(mw, sess)
    gs = mw._get_state(sess)
    assert gs.recovery_mode is True
    # released retry BLOCKs again → violation 1/1, still BLOCK (NOT HALT, max=1), re-blocked
    assert mw._wrap_tool_call_precheck(_mkreq(sess, "T", {"a": 1}, "c6")) is None
    out6 = wrap_call(mw, sess, "T", {"a": 1}, "same", "c6")
    gs = mw._get_state(sess)
    assert gs.recovery_violation_count == 1
    assert gs.halt_decision is None
    assert "T" in gs.blocked_tools  # re-blocked
    assert out6.status == "error"
    assert "recovery mode active" in out6.content
    # released again → 3rd BLOCK exceeds recovery_max_violations=1 → HALT (recovery message)
    assert mw._wrap_tool_call_precheck(_mkreq(sess, "T", {"a": 1}, "c7")) is None
    out7 = wrap_call(mw, sess, "T", {"a": 1}, "same", "c7")
    gs = mw._get_state(sess)
    assert gs.recovery_violation_count == 2
    assert gs.halt_decision == HALT
    assert "recovery mode violation limit exceeded" in out7.content
    assert "🔴" in out7.content


def test_recovery_disabled_legacy_behavior():
    mw = make_mw(recovery_mode_enabled=False)
    sess = "sess-rec-off"
    outs = _drive_to_first_block(mw, sess)
    gs = mw._get_state(sess)
    assert gs.recovery_mode is False  # never enters recovery
    assert gs.recovery_violation_count == 0
    assert "T" in gs.blocked_tools
    assert "recovery mode active" not in outs[-1].content  # legacy block text preserved
    assert "🚫" in outs[-1].content
    # legacy precheck short-circuit: "previously blocked" message, NO release
    blocked = mw._wrap_tool_call_precheck(_mkreq(sess, "T", {"a": 1}, "c10"))
    assert blocked is not None
    assert blocked.status == "error"
    assert "previously blocked" in blocked.content
    gs = mw._get_state(sess)
    assert "T" in gs.blocked_tools  # tool stays blocked — no release


def test_recovery_released_call_participates_in_pathology_counting():
    mw = make_mw()
    sess = "sess-rec-count"
    _drive_to_first_block(mw, sess)
    gs = mw._get_state(sess)
    np_key = f"T:{mod.ToolGuardrails._result_hash('same')}"
    assert gs.no_progress_counts[np_key] == 5
    # release + retry: counter keeps incrementing — released call gets FULL evaluation
    assert mw._wrap_tool_call_precheck(_mkreq(sess, "T", {"a": 1}, "c6")) is None
    out6 = wrap_call(mw, sess, "T", {"a": 1}, "same", "c6")
    gs = mw._get_state(sess)
    assert gs.no_progress_counts[np_key] == 6  # no suppression anywhere
    assert len(gs.records) == 6
    assert out6.status == "error"  # legitimately BLOCKed again by the counter


def test_recovery_state_resets_per_turn():
    mw = make_mw()
    sess = "sess-rec-reset"
    _drive_to_first_block(mw, sess)
    gs = mw._get_state(sess)
    assert gs.recovery_mode is True
    assert gs.blocked_tools
    # before_agent-equivalent per-turn reset (production hook: ToolGuardrails.before_agent)
    mw.before_agent({"session_id": sess}, None)
    fresh = mw._get_state(sess)
    assert fresh is not gs
    assert fresh.recovery_mode is False
    assert fresh.recovery_violation_count == 0
    assert fresh.blocked_tools == set()
