"""The compression-time memory review must not come near the window it guards.

The review runs exactly when the context is at its fullest, so two things are
pinned here:

* it is handed the messages about to be DISCARDED (the facts worth keeping live
  there, and they are a fraction of the conversation) — not the whole history;
* the history it does receive is folded by ``digest_history`` (recent tail
  verbatim, everything older as one truncated line per message), and the review
  runs on the AUXILIARY model.

Hermetic: no agent, lane or LLM is built; the seams are monkeypatched.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import agent.middlewares.summarization.nudges as nudge_mod
from agent.middlewares.summarization.digest import (
    DIGEST_ASSISTANT_MAX,
    DIGEST_TAIL,
    DIGEST_TOOL_MAX,
    DIGEST_USER_MAX,
    digest_history,
)

pytestmark = [pytest.mark.unit]


def _history(count: int, *, text: str = "line", role: str = "human") -> list:
    out = []
    for index in range(count):
        content = f"{text}-{index}"
        if role == "human":
            out.append(HumanMessage(content=content))
        elif role == "ai":
            out.append(AIMessage(content=content))
        else:
            out.append(ToolMessage(content=content, tool_call_id=f"t{index}", name="terminal"))
    return out


# ── digest_history ─────────────────────────────────────────────────────────


def test_a_short_history_is_returned_unchanged():
    messages = _history(DIGEST_TAIL)

    digested = digest_history(messages)

    assert digested == messages


def test_older_messages_fold_into_one_digest_line_each():
    messages = _history(DIGEST_TAIL + 6)

    digested = digest_history(messages)

    assert len(digested) == 1 + DIGEST_TAIL
    digest = digested[0]
    assert isinstance(digest, HumanMessage)
    assert "6 messages condensed" in digest.content
    # Every folded message is represented, in order, as ROLE: text.
    for index in range(6):
        assert f"USER: line-{index}" in digest.content
    # The tail is verbatim and in order.
    assert digested[-1].content == f"line-{DIGEST_TAIL + 5}"


def test_long_messages_are_truncated_per_role():
    messages = [
        HumanMessage(content="u" * (DIGEST_USER_MAX + 50)),
        AIMessage(content="a" * (DIGEST_ASSISTANT_MAX + 50)),
        ToolMessage(content="t" * (DIGEST_TOOL_MAX + 50), tool_call_id="t1", name="terminal"),
    ] + _history(DIGEST_TAIL)

    digest = digest_history(messages)[0].content

    assert f"USER: {'u' * DIGEST_USER_MAX}..." in digest
    assert f"ASSISTANT: {'a' * DIGEST_ASSISTANT_MAX}..." in digest
    assert f"TOOL: {'t' * DIGEST_TOOL_MAX}..." in digest


def test_system_and_empty_messages_are_skipped():
    messages = [
        SystemMessage(content="not conversation"),
        HumanMessage(content="   "),
        HumanMessage(content="real"),
    ] + _history(DIGEST_TAIL)

    digest = digest_history(messages)[0].content

    assert "not conversation" not in digest
    assert "USER: real" in digest


def test_block_content_keeps_its_text_blocks():
    multimodal = HumanMessage(
        content=[
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )
    messages = [multimodal] + _history(DIGEST_TAIL)

    digest = digest_history(messages)[0].content

    assert "USER: look at this" in digest
    assert "base64" not in digest


def test_a_tail_of_zero_folds_everything():
    messages = _history(3)

    digested = digest_history(messages, tail=0)

    assert len(digested) == 1
    assert all(f"USER: line-{i}" in digested[0].content for i in range(3))


def test_the_folded_input_is_far_smaller_than_the_raw_history():
    # The plan's budget case: a compaction-sized history — 200 messages of
    # ~2000 chars is ~400K chars, i.e. the ~105K tokens a compaction fires at.
    messages = [
        HumanMessage(content="x" * 2000) if index % 2 else AIMessage(content="y" * 2000)
        for index in range(200)
    ]
    raw_chars = sum(len(m.content) for m in messages)

    digested = digest_history(messages)
    folded_chars = sum(len(str(m.content)) for m in digested)

    assert folded_chars < raw_chars / 4
    # The pass's own budget: 4 chars/token is conservative for English, and the
    # review's prompt + system prompt add a few thousand more.
    assert folded_chars / 4 < 30_000


# ── the review's own wiring ────────────────────────────────────────────────


def _descriptor(monkeypatch, config):
    """Patch the seam: a published model descriptor + a fake create_agent."""
    monkeypatch.setattr(
        "agent.middlewares.thinking_control.core.current_session_model_config",
        lambda session_id: config,
    )
    calls: dict = {}

    def _create_agent(**kwargs):
        calls["agent_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(nudge_mod, "create_agent", _create_agent)
    monkeypatch.setattr("agent.get_agent_tools", lambda: [])
    return calls


@pytest.mark.asyncio
async def test_the_review_agent_rebuilds_the_turns_model_config(monkeypatch):
    """Cache-warm: the same provider/model/thinking/temperature = same body."""
    import models

    sentinel = object()
    profile_kwargs: dict = {}
    monkeypatch.setattr(
        models,
        "build_main_llm_for_profile",
        lambda **kwargs: profile_kwargs.update(kwargs) or sentinel,
    )
    calls = _descriptor(
        monkeypatch,
        {
            "override": {"provider": "openai", "model": "gpt-x", "api_key": "k", "base_url": "u"},
            "thinking_kwargs": {"thinking_level": "high"},
            "temperature": 0.8,
        },
    )

    await nudge_mod._create_memory_review_agent("sess-x", "sys")

    assert profile_kwargs == {
        "provider": "openai",
        "model": "gpt-x",
        "api_key": "k",
        "base_url": "u",
        "temperature": 0.8,
        "thinking_level": "high",
    }
    assert calls["agent_kwargs"]["model"] is sentinel
    assert calls["agent_kwargs"]["system_prompt"] == "sys"


@pytest.mark.asyncio
async def test_the_review_agent_rebuilds_the_env_default_config(monkeypatch):
    """A session with no model override still carries thinking/temperature."""
    import models

    sentinel = object()
    env_kwargs: dict = {}
    monkeypatch.setattr(
        models, "build_main_llm", lambda **kwargs: env_kwargs.update(kwargs) or sentinel
    )
    calls = _descriptor(
        monkeypatch,
        {"override": None, "thinking_kwargs": {"thinking": False}, "temperature": 0.8},
    )

    await nudge_mod._create_memory_review_agent("sess-y", "sys")

    assert env_kwargs == {"temperature": 0.8, "thinking": False}
    assert calls["agent_kwargs"]["model"] is sentinel


@pytest.mark.asyncio
async def test_the_review_agent_without_a_descriptor_builds_the_env_default(monkeypatch):
    """No main-chain call yet in this process: plain env-default model."""
    import models

    sentinel = object()
    monkeypatch.setattr(models, "build_main_llm", lambda *args, **kwargs: sentinel)
    calls = _descriptor(monkeypatch, None)

    await nudge_mod._create_memory_review_agent("sess-z", "sys")

    assert calls["agent_kwargs"]["model"] is sentinel


@pytest.mark.asyncio
async def test_the_review_agent_binds_the_sessions_own_tool_face(monkeypatch):
    """The tool block is part of the cached prefix: same tools, same order."""
    import models

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    monkeypatch.setattr(models, "build_main_llm", lambda *args, **kwargs: object())
    calls = _descriptor(monkeypatch, None)
    monkeypatch.setattr(
        "agent.get_agent_tools",
        lambda: [_Tool("a"), _Tool("b"), _Tool("c")],
    )
    monkeypatch.setattr(
        "agent.middlewares.tool_selection.core.enabled_tool_names",
        lambda session_id: frozenset({"a", "c"}),
    )

    await nudge_mod._create_memory_review_agent("sess-t", "sys")

    names = [tool.name for tool in calls["agent_kwargs"]["tools"]]
    assert names == ["a", "c"], "the session's face, in the same order"


@pytest.mark.asyncio
async def test_the_review_gets_the_discarded_slice_not_the_whole_conversation(monkeypatch):
    captured: list = []

    async def _recorder(session_id: str, system_prompt: str, messages: list) -> None:
        captured.append(list(messages))

    monkeypatch.setattr(nudge_mod, "_nudge_memory", _recorder)
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys",
    )
    discarded = [HumanMessage(content="the message about to be dropped")]
    session_id = "mr-" + uuid.uuid4().hex[:8]

    await nudge_mod._run_memory_review(session_id, discarded)

    assert len(captured) == 1
    assert captured[0] == discarded


@pytest.mark.asyncio
async def test_the_scheduler_passes_the_discarded_slice_through(monkeypatch):
    recorded: list = []

    async def _runner(session_id: str, messages: list) -> None:
        recorded.append((session_id, list(messages)))

    monkeypatch.setattr(nudge_mod, "_run_memory_review", _runner)
    monkeypatch.setattr(
        "agent.middlewares.agent_switch.middleware_option", lambda *args, **kwargs: True
    )
    session_id = "mr-" + uuid.uuid4().hex[:8]
    discarded = [HumanMessage(content="bye")]

    scheduled = nudge_mod.schedule_memory_review(session_id, discarded)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scheduled is True
    assert recorded == [(session_id, discarded)]


@pytest.mark.asyncio
async def test_the_session_switch_off_schedules_nothing(monkeypatch):
    recorded: list = []

    async def _runner(session_id: str, messages: list) -> None:  # pragma: no cover
        recorded.append(session_id)

    monkeypatch.setattr(nudge_mod, "_run_memory_review", _runner)
    monkeypatch.setattr(
        "agent.middlewares.agent_switch.middleware_option", lambda *args, **kwargs: False
    )

    scheduled = nudge_mod.schedule_memory_review("mr-off", [HumanMessage(content="x")])

    assert scheduled is False
    assert recorded == []


def test_the_cache_read_reads_every_provider_shape():
    """The hit-ratio log must work on DeepSeek/Anthropic too, not just OpenAI."""
    openai_shaped = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 5,
            "total_tokens": 105,
            "input_token_details": {"cache_read": 900},
        },
    )
    deepseek_shaped = AIMessage(
        content="ok",
        response_metadata={"token_usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 640}},
    )
    silent = AIMessage(content="ok")

    assert nudge_mod._cache_hit_tokens(openai_shaped) == 900
    assert nudge_mod._cache_hit_tokens(deepseek_shaped) == 640
    assert nudge_mod._cache_hit_tokens(silent) is None


def test_the_skill_tools_carry_no_nudge_marker():
    """Skills belong to the plan-completion extraction, never to the review."""
    from agent.tools.skill_tools import (
        build_skill_list_tool,
        build_skill_manage_tool,
        build_skill_view_tool,
    )

    for builder in (build_skill_list_tool, build_skill_manage_tool, build_skill_view_tool):
        tool = builder()
        assert not (tool.metadata or {}).get("nudge"), tool.name


def test_the_nudge_gate_refuses_the_skill_tools():
    """The review may EXECUTE only the tagged set; skill tools are out."""

    class _Tool:
        def __init__(self, name: str, metadata: dict | None = None) -> None:
            self.name = name
            self.metadata = metadata or {}

    gate = nudge_mod._NudgeLimitTool()
    from agent.tools.memory import build_memory_tool

    assert gate._is_allowed(build_memory_tool()) is True
    for name in ("skill_manage", "skill_list", "skill_view"):
        assert gate._is_allowed(_Tool(name)) is False, name


@pytest.mark.asyncio
async def test_cache_warm_sends_the_discarded_messages_verbatim(monkeypatch):
    """The default: the slice IS the main model's prefix, so it goes as-is."""
    captured: dict = {}

    class _CapturingAgent:
        async def ainvoke(self, input: dict, config=None) -> dict:  # noqa: ARG002
            captured["messages"] = list(input["messages"])
            return {"messages": [AIMessage(content="ok")]}

    async def _factory(session_id: str, system_prompt: str) -> _CapturingAgent:
        return _CapturingAgent()

    monkeypatch.setattr(nudge_mod, "_create_memory_review_agent", _factory)
    monkeypatch.setitem(nudge_mod.NUDGE, "memory_review_mode", "cache_warm")
    discarded = _history(50)

    await nudge_mod._nudge_memory("mr-" + uuid.uuid4().hex[:8], "sys", discarded)

    messages = captured["messages"]
    assert len(messages) == len(discarded) + 1
    assert messages[: len(discarded)] == discarded
    assert messages[-1].content.startswith("Review the conversation")


@pytest.mark.asyncio
async def test_the_digest_mode_folds_the_history_before_the_agent(monkeypatch):
    captured: dict = {}

    class _CapturingAgent:
        async def ainvoke(self, input: dict, config=None) -> dict:  # noqa: ARG002
            captured["messages"] = list(input["messages"])
            return {"messages": [AIMessage(content="ok")]}

    async def _factory(session_id: str, system_prompt: str) -> _CapturingAgent:
        return _CapturingAgent()

    monkeypatch.setattr(nudge_mod, "_create_memory_review_agent", _factory)
    monkeypatch.setitem(nudge_mod.NUDGE, "memory_review_mode", "digest")
    session_id = "mr-" + uuid.uuid4().hex[:8]

    await nudge_mod._nudge_memory(session_id, "sys", _history(50))

    messages = captured["messages"]
    # digest + tail + the review prompt
    assert len(messages) == 1 + DIGEST_TAIL + 1
    assert "messages condensed" in messages[0].content
    assert messages[-1].content.startswith("Review the conversation")
