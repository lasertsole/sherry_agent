"""Redaction as the model sees it, through the middleware (B1/B2).

The engine's own tests prove the patterns; these prove the wiring and — more
importantly — the two boundaries the default configuration draws:

* tools that leak credentials for a living (``terminal``, ``python_repl``, the
  untrusted set) have their results masked before entering model context;
* file tools do NOT, because the agent edits its own configuration and reading
  «redacted» then writing it back would corrupt the file;
* redaction and the untrusted fence compose: a web result with a credentialed URL
  comes back both masked and fenced.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agent.middlewares.context_eviction.core import ContextEvictionMiddleware
from agent.security.redact import REDACTED
from config.features import TOOL_RESULT_EVICTION

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
_URL = "https://alice:s3cr3tp4ss@example.com/repo"


def _request(session_id: str, tool: str = "terminal") -> ToolCallRequest:
    call = {"name": tool, "args": {}, "id": "c1", "type": "tool_call"}
    return ToolCallRequest(
        tool=call,
        tool_call=call,
        state={"session_id": session_id, "messages": []},
        runtime=None,
    )


def _handler(value):
    def handler(_request: ToolCallRequest):
        return value

    return handler


def _message(content: str, name: str) -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id="c1")


@pytest.mark.parametrize("tool", ["terminal", "python_repl"])
def test_a_leaked_key_is_masked_before_the_model_reads_it(
    isolated_db, isolated_sessions, sid, tool
):
    eviction = ContextEvictionMiddleware()
    raw = f"$ env | grep KEY\nMAIN_LLM_API_KEY={_KEY}"

    response = eviction.wrap_tool_call(_request(sid, tool), _handler(_message(raw, tool)))

    assert _KEY not in response.content
    assert REDACTED in response.content


def test_a_credentialed_url_in_web_results_is_masked_and_fenced(
    isolated_db, isolated_sessions, sid
):
    eviction = ContextEvictionMiddleware()
    raw = f"result: {_URL} (untrusted page)"

    response = eviction.wrap_tool_call(
        _request(sid, "web_search"), _handler(_message(raw, "web_search"))
    )

    assert "s3cr3tp4ss" not in response.content
    assert response.content.startswith('<untrusted_tool_result source="web_search"')
    assert response.content.rstrip().endswith("</untrusted_tool_result>")


@pytest.mark.parametrize("tool", ["read_file", "patch_file", "write_file"])
def test_file_tools_stay_verbatim_so_the_agent_can_edit_its_config(
    isolated_db, isolated_sessions, sid, tool
):
    """Documented trade-off: masking here would make a read-then-write lossy."""
    eviction = ContextEvictionMiddleware()
    raw = f"MAIN_LLM_API_KEY={_KEY}"

    response = eviction.wrap_tool_call(_request(sid, tool), _handler(_message(raw, tool)))

    assert response.content == raw


def test_the_switch_turns_tool_output_redaction_off(
    isolated_db, isolated_sessions, sid, monkeypatch
):
    monkeypatch.setitem(
        ContextEvictionMiddleware.__init__.__globals__["REDACTION"], "tool_output_enabled", False
    )
    eviction = ContextEvictionMiddleware()
    raw = f"MAIN_LLM_API_KEY={_KEY}"

    response = eviction.wrap_tool_call(_request(sid), _handler(_message(raw, "terminal")))

    assert response.content == raw


def test_redaction_runs_after_eviction_and_leaves_the_raw_message_alone(
    isolated_db, isolated_sessions, sid
):
    threshold = TOOL_RESULT_EVICTION["evict_threshold_chars"]
    eviction = ContextEvictionMiddleware()
    raw = f"MAIN_LLM_API_KEY={_KEY}\n" + ("filler line\n" * (threshold // 10))
    original = _message(raw, "terminal")

    response = eviction.wrap_tool_call(_request(sid), _handler(original))

    assert "[evicted to: " in response.content, "expected an eviction preview"
    assert _KEY not in response.content, "the preview still carries the key"
    assert original.content == raw, "the raw message must survive for persistence"
