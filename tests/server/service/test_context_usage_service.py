"""Tests for the chat toolbar's context accounting (window / total / parts)."""

import asyncio
import json

import pytest

from server.service import context_usage_service as service
from server.trigger.http import context_usage as http


class _FakeRequest:
    """Minimal Robyn-request double: only ``query_params`` is read."""

    def __init__(self, query_params=None):
        self.query_params = query_params or {}


@pytest.fixture(autouse=True)
def _no_tool_walk(monkeypatch):
    """Tool schemas are a process-static estimate: pin it instead of walking them."""
    monkeypatch.setattr(service, "_tool_schema_tokens", lambda: 2_000)


def test_usage_splits_the_reported_prompt(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 30_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "系统提示词" * 100,
    )

    usage = service.get_context_usage("s1")

    assert usage["window"] == 128_000
    assert usage["total"] == 30_000
    assert usage["tools"] == 2_000
    assert usage["system"] > 0
    # The message part is the remainder of the reported prompt.
    assert usage["messages"] == 30_000 - usage["system"] - usage["skills"] - 2_000


def test_usage_never_reports_a_negative_message_part(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    # Prompt trimmed to almost nothing while the estimates still add up.
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 100)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "很长的系统提示词" * 500,
    )

    usage = service.get_context_usage("s1")

    assert usage["messages"] == 0
    assert usage["total"] == 100


def test_estimates_are_scaled_to_fit_a_short_prompt(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    # A short prompt (5k reported) against a 2k tool schema and a long system prompt.
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 5_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: "系统提示词" * 1_500,
    )

    usage = service.get_context_usage("s1")

    # Both parts shrink together so their sum fits the reported prompt…
    assert usage["system"] + usage["skills"] + usage["tools"] <= usage["total"]
    # …and the message part takes what is left (nothing here).
    assert usage["messages"] == 5_000 - usage["system"] - usage["skills"] - usage["tools"]
    assert usage["system"] > usage["tools"]  # proportions survive the scaling


def test_usage_without_a_finished_turn_has_no_total(monkeypatch):
    monkeypatch.setattr(service, "main_llm_context_window", 64_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 0)
    monkeypatch.setattr(service, "_system_prompt_text", lambda sid: "")

    usage = service.get_context_usage("s1")

    assert usage == {
        "window": 64_000,
        "total": 0,
        "system": 0,
        "skills": 0,
        "tools": 2_000,
        "messages": 0,
        "cache_hit_ratio": None,
        "compress_ratio": 0.8,
    }


def test_cache_hit_ratio_averages_the_session(monkeypatch):
    class _Row(dict):
        pass

    class _Cursor:
        def fetchone(self):
            return _Row(total=100_000, cached=40_000)

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    assert service._cache_hit_ratio("s1") == 0.4


def test_cache_hit_ratio_is_unknown_without_cached_tokens(monkeypatch):
    class _Cursor:
        def fetchone(self):
            return {"total": 100_000, "cached": None}

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    # Rows written before the column existed, or a provider that never reports
    # cached tokens: the panel must show "unknown", not 0%.
    assert service._cache_hit_ratio("s1") is None


def test_cache_hit_ratio_can_be_a_genuine_zero(monkeypatch):
    class _Cursor:
        def fetchone(self):
            return {"total": 100_000, "cached": 0}

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

    monkeypatch.setattr(service, "get_db", lambda: _Db(), raising=False)

    # A reported "nothing cached" is 0%, not "unknown".
    assert service._cache_hit_ratio("s1") == 0.0


def test_cache_hit_ratio_survives_a_storage_error(monkeypatch):
    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(service, "get_db", _boom, raising=False)

    assert service._cache_hit_ratio("s1") is None


def test_usage_rejects_a_missing_session():
    with pytest.raises(ValueError):
        service.get_context_usage("")
    with pytest.raises(ValueError):
        service.get_context_usage("   ")


def test_tool_estimate_failure_does_not_break_the_ring(monkeypatch):
    def _boom():
        raise RuntimeError("tool build failed")

    monkeypatch.setattr(service, "_tool_schema_tokens", _boom)
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 5_000)
    monkeypatch.setattr(service, "_system_prompt_text", lambda sid: "")

    usage = service.get_context_usage("s1")

    assert usage["tools"] == 0
    assert usage["messages"] == 5_000


def _prompt_with_skills(persona: str, skills: str) -> str:
    """An assembled prompt: build_system_prompt joins the index in LAST."""
    return f"{persona}\n\n{skills}"


def test_skill_index_is_split_out_of_the_system_prompt(monkeypatch):
    persona = "档案与记忆" * 200
    index = (
        "<available_skills>\n"
        "  <skill>\n    <name>code_wiki</name>\n    <description>仓库维基</description>\n"
        "  </skill>\n</available_skills>"
    )
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 9_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: _prompt_with_skills(persona, index),
    )

    usage = service.get_context_usage("s1")

    # The index is measured on its own, and the system part no longer carries it.
    assert usage["skills"] > 0
    assert usage["skills"] < usage["system"]
    assert usage["system"] == service.estimate_text_tokens(persona)
    assert usage["skills"] == service.estimate_text_tokens(index)
    # The four parts still account for the whole reported prompt.
    assert usage["messages"] + usage["system"] + usage["skills"] + usage["tools"] == usage["total"]


def test_prompt_without_a_skill_index_reports_none(monkeypatch):
    # A filtered build (explicit file selection) carries no index: the panel says
    # 0 rather than re-deriving the index from disk and double-counting it.
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 9_000)
    monkeypatch.setattr(
        service.state_register_mem, "get_state", lambda sid, key, default=None: "只有档案" * 100
    )

    usage = service.get_context_usage("s1")

    assert usage["skills"] == 0
    assert usage["system"] > 0


def test_split_skill_index_ignores_an_unterminated_block():
    # A truncated index is not an index: nothing is extracted, nothing is lost.
    assert service._split_skill_index("<available_skills>\n  <skill>") == (
        "<available_skills>\n  <skill>",
        "",
    )
    assert service._split_skill_index("") == ("", "")


def test_http_handler_returns_the_accounting(monkeypatch):
    monkeypatch.setattr(
        http,
        "get_context_usage",
        lambda sid: {"window": 128_000, "total": 10, "system": 1, "tools": 2, "messages": 7},
    )

    response = asyncio.run(http.context_usage_handler(_FakeRequest({"session_id": "s1"})))

    assert response.status_code == 200
    # `ok()` passes the payload through as the response body.
    payload = json.loads(response.description)
    assert payload["window"] == 128_000
    assert payload["messages"] == 7


def test_http_handler_reports_a_missing_session_id(monkeypatch):
    monkeypatch.setattr(http, "get_context_usage", service.get_context_usage)

    response = asyncio.run(http.context_usage_handler(_FakeRequest({})))

    assert response.status_code == 400
    payload = json.loads(response.description)
    assert payload["success"] is False
    assert "session_id" in payload["message"]


# --------------------------------------------------------------------------
# Context inspection (/context/inspect): the content behind the numbers.
# --------------------------------------------------------------------------


@pytest.fixture
def _inspect_env(monkeypatch):
    """Stub every external source: register prompt, tool set, checkpoint state."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    prompt = "系统提示词"
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(
        service.state_register_mem,
        "get_state",
        lambda sid, key, default=None: prompt,
    )
    messages = [
        HumanMessage(content="你好", metadata={"origin": "user"}),
        HumanMessage(
            content="[项目目录已切换] moved",
            metadata={"origin": "project_dir", "internal": True},
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "c1"}],
        ),
        ToolMessage(content="file body", tool_call_id="c1"),
    ]

    async def fake_state(session_id: str):
        return messages

    monkeypatch.setattr(service, "read_state_messages", fake_state)
    return messages


def test_inspect_serves_prompt_tools_and_the_live_transcript(_inspect_env, monkeypatch):
    class _Tool:
        name = "read_file"
        description = "Read a file"

        class args_schema:  # noqa: N801 - mirrors the tool contract's attribute name
            @staticmethod
            def model_json_schema():
                return {"type": "object", "properties": {"path": {"type": "string"}}}

    monkeypatch.setattr("agent.tools.build_main_tools", lambda: [_Tool()])
    monkeypatch.setattr(
        "agent.middlewares.tool_selection.core.enabled_tool_names",
        lambda sid: frozenset({"read_file"}),
    )

    payload = asyncio.run(service.get_context_content("s1"))

    assert payload["window"] == 128_000
    assert payload["system_prompt"] == "系统提示词"
    assert payload["system_tokens"] > 0
    # The tool set is the session's (selection applied) and carries its schema.
    assert [tool["name"] for tool in payload["tools"]] == ["read_file"]
    assert payload["tools"][0]["parameters"]["properties"]["path"]["type"] == "string"
    assert payload["tool_tokens"] > 0
    assert payload["tool_selection"] is True
    # The transcript keeps roles, tool calls and the injected-carrier markers.
    roles = [row["role"] for row in payload["messages"]]
    assert roles == ["human", "human", "ai", "tool"]
    assert payload["messages"][1]["origin"] == "project_dir"
    assert payload["messages"][1]["internal"] is True
    assert payload["messages"][2]["tool_calls"][0]["name"] == "read_file"
    assert payload["messages"][3]["tool_call_id"] == "c1"
    assert payload["message_tokens"] > 0
    assert payload["truncated"] == 0
    assert payload["state_error"] == ""


def test_inspect_serves_each_row_reasoning(_inspect_env, monkeypatch):
    from langchain_core.messages import AIMessage

    async def thinking_state(session_id: str):
        return [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "先看文件，再改一行。"},
                tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "c1"}],
            ),
            AIMessage(content="答完了", additional_kwargs={}),
        ]

    monkeypatch.setattr(service, "read_state_messages", thinking_state)
    monkeypatch.setattr("agent.tools.build_main_tools", lambda: [])
    monkeypatch.setattr(
        "agent.middlewares.tool_selection.core.enabled_tool_names", lambda sid: None
    )

    payload = asyncio.run(service.get_context_content("s1"))

    # A thinking-only row would otherwise read as empty.
    assert payload["messages"][0]["content"] == ""
    assert payload["messages"][0]["reasoning"] == "先看文件，再改一行。"
    # A row without reasoning carries no field at all (the panel hides the block).
    assert "reasoning" not in payload["messages"][1]


def test_inspect_clips_a_giant_message_and_counts_it(_inspect_env, monkeypatch):
    from langchain_core.messages import HumanMessage

    async def huge_state(session_id: str):
        return [HumanMessage(content="x" * (service._MAX_MESSAGE_CHARS + 500))]

    monkeypatch.setattr(service, "read_state_messages", huge_state)
    monkeypatch.setattr("agent.tools.build_main_tools", lambda: [])
    monkeypatch.setattr(
        "agent.middlewares.tool_selection.core.enabled_tool_names", lambda sid: None
    )

    payload = asyncio.run(service.get_context_content("s1"))

    assert payload["truncated"] == 1
    assert payload["messages"][0]["truncated"] is True
    assert payload["messages"][0]["content"].endswith("…[truncated]")
    assert len(payload["messages"][0]["content"]) < service._MAX_MESSAGE_CHARS + 40


def test_inspect_survives_a_failed_state_read(monkeypatch):
    async def boom(session_id: str):
        raise RuntimeError("checkpoint down")

    monkeypatch.setattr(service, "read_state_messages", boom)
    monkeypatch.setattr(service, "main_llm_context_window", 128_000)
    monkeypatch.setattr(service.state_register_mem, "get_state", lambda sid, key, default=None: "P")
    monkeypatch.setattr("agent.tools.build_main_tools", lambda: [])
    monkeypatch.setattr(
        "agent.middlewares.tool_selection.core.enabled_tool_names", lambda sid: None
    )

    payload = asyncio.run(service.get_context_content("s1"))

    assert payload["messages"] == []
    assert "checkpoint down" in payload["state_error"]
    assert payload["system_prompt"] == "P"


def test_inspect_http_handler_serves_the_content(monkeypatch):
    async def stub(sid: str) -> dict:
        return {"window": 100, "system_prompt": "P", "tools": [], "messages": []}

    monkeypatch.setattr(http, "get_context_content", stub)

    response = asyncio.run(http.context_inspect_handler(_FakeRequest({"session_id": "s1"})))

    assert response.status_code == 200
    payload = json.loads(response.description)
    assert payload["system_prompt"] == "P"


def test_inspect_http_handler_reports_a_missing_session_id(monkeypatch):
    monkeypatch.setattr(http, "get_context_content", service.get_context_content)

    response = asyncio.run(http.context_inspect_handler(_FakeRequest({})))

    assert response.status_code == 400
    assert json.loads(response.description)["success"] is False
