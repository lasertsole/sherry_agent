"""Post-task session report analyzers and service aggregation."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pub.func.session_report.code_intel_tracker import analyze_code_intel
from pub.func.session_report.context_analyzer import analyze_transcript
from pub.func.session_report.token_tracker import TokenLedger, session_tokens

pytestmark = [pytest.mark.unit]


# -- token tracker -----------------------------------------------------------


def test_session_tokens_sums_usage_metadata():
    messages = [
        AIMessage(
            content="a",
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        ),
        AIMessage(
            content="b",
            usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        ),
    ]
    assert session_tokens(messages) == {"input": 150, "output": 30}


def test_token_ledger_breakdown_without_price(monkeypatch):
    monkeypatch.delenv("SHERRY_SESSION_REPORT_TOKEN_PRICES", raising=False)
    monkeypatch.delenv("SHERRY_EVAL_TOKEN_PRICES", raising=False)
    ledger = TokenLedger("s1")
    ledger.record("s1", {"input": 500, "output": 100}, role="main_agent")
    ledger.record("child", {"input": 200, "output": 50}, role="executor")
    payload = ledger.payload()
    assert payload["grand_total"] == 850
    assert payload["main"]["total"] == 600
    assert payload["children"][0]["role"] == "executor"
    assert "estimated_cost_usd" not in payload


def test_token_ledger_with_price(tmp_path, monkeypatch):
    prices = tmp_path / "prices.json"
    prices.write_text('{"gpt-x": {"input": 0.01, "output": 0.02}}', encoding="utf-8")
    monkeypatch.setenv("SHERRY_SESSION_REPORT_TOKEN_PRICES", str(prices))
    ledger = TokenLedger("s1", model="gpt-x")
    ledger.record("s1", {"input": 1000, "output": 500})
    assert ledger.payload()["estimated_cost_usd"] == pytest.approx(0.02)


# -- context analyzer --------------------------------------------------------


def test_context_hit_rate_scoring():
    messages = [
        HumanMessage(content="Fix the parser bug"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "terminal", "args": {"cmd": "pytest -q"}, "id": "c1"},
                {"name": "todowrite", "args": {}, "id": "c2"},
            ],
        ),
        ToolMessage(content="all passed", tool_call_id="c1"),
    ]
    payload = analyze_transcript(messages, "Fix the parser bug")
    assert payload["total_tool_calls"] == 1  # todowrite unscored
    assert payload["hit_rate"] == 1.0
    assert payload["effective"] == 1


def test_context_hit_rate_wasted_terminal():
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "terminal", "args": {"cmd": "echo hello"}, "id": "c1"},
            ],
        ),
        ToolMessage(content="hello", tool_call_id="c1"),
    ]
    payload = analyze_transcript(messages, "fix parser")
    assert payload["wasted"] == 1
    assert payload["hit_rate"] == 0.0


# -- code intel --------------------------------------------------------------


def test_code_intel_precise_vs_greedy():
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lsp_goto_definition", "args": {}, "id": "c1"},
                {"name": "explore", "args": {}, "id": "c2"},
                {"name": "read_file", "args": {}, "id": "c3"},
                {"name": "read_file", "args": {}, "id": "c4"},
            ],
        ),
    ]
    payload = analyze_code_intel(messages)
    assert payload["precise_calls"] == 2
    assert payload["greedy_calls"] == 2
    assert payload["lsp_vs_grep_ratio"] == 0.5


def test_code_intel_degraded_detection():
    messages = [
        ToolMessage(content="error: lsp binary not found", tool_call_id="c1", name="lsp_hover"),
    ]
    payload = analyze_code_intel(messages)
    assert payload["degraded_calls"]
    assert payload["degraded_calls"][0]["tool"] == "lsp_hover"


# -- service -----------------------------------------------------------------

# Load the service module by FILE PATH, not through ``server.service`` (whose
# package __init__ eagerly imports the whole server surface). In a mixed test
# process other conftests stub parts of that surface; bypassing the package
# keeps this analyzer unit test hermetic.
import importlib.util
from pathlib import Path

_SVC_PATH = Path(__file__).parents[4] / "server" / "service" / "session_report_service.py"
_spec = importlib.util.spec_from_file_location("_session_report_service_under_test", _SVC_PATH)
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)


@pytest.mark.asyncio
async def test_build_report_empty_transcript(monkeypatch):
    async def _empty(_sid):
        return []

    monkeypatch.setattr("agent.state_port.read_messages", _empty)
    report = await svc.build_session_report("s1")
    assert report["session_id"] == "s1"
    assert report["tokens"]["grand_total"] == 0
    assert report["context_hit_rate"]["hit_rate"] == 1.0
    assert report["code_intel"]["available"] is False


@pytest.mark.asyncio
async def test_build_report_full_transcript(monkeypatch):
    messages = [
        HumanMessage(content="Fix the parser"),
        AIMessage(
            content="",
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            tool_calls=[{"name": "terminal", "args": {"cmd": "pytest"}, "id": "c1"}],
        ),
        ToolMessage(content="passed", tool_call_id="c1"),
    ]

    async def _full(_sid):
        return messages

    monkeypatch.setattr("agent.state_port.read_messages", _full)
    report = await svc.build_session_report("s1")
    assert report["tokens"]["grand_total"] == 120
    assert report["context_hit_rate"]["total_tool_calls"] == 1
    assert "model" in report
