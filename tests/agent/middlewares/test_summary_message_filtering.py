"""Unit tests for ``_filter_summary_messages`` and the async chained-summary
filtering: the prior summary pair must not be re-fed to the summarizer.

The summary pair produced by ``_build_new_messages`` — HumanMessage("What did
we do so far?") + AIMessage(summary body) — is tagged with
``additional_kwargs["lc_source"] == "summarization"`` on BOTH halves.  During a
chained compression the old pair must be stripped from the serialized
``<conversation>`` input: the previous summary text is already injected into
``<prior-summary>`` via ``_extract_previous_summary``, so keeping it in the
conversation doubles tokens and confuses the summarizer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.summarization.core as summarization_module
from agent.middlewares.summarization.core import (
    _build_static_fallback_summary,
    _filter_summary_messages,
    _latest_human_eviction_ref,
    _serialize_for_summary,
)
from pub.func.message.eviction import EVICTED_TO_KEY
from pub.func.message.system_reminder import system_reminder_metadata, wrap_reminder

pytestmark = [pytest.mark.unit]

_LC_SOURCE = summarization_module._SUMMARY_LC_SOURCE


def _summary_pair(body: str) -> list:
    """A tagged pair shaped like ``_build_new_messages`` output."""
    return [
        HumanMessage(
            content="What did we do so far?",
            additional_kwargs={"lc_source": _LC_SOURCE},
        ),
        AIMessage(
            content=f"<summary>\n{body}\n</summary>",
            additional_kwargs={"lc_source": _LC_SOURCE},
        ),
    ]


class StubAuxModel:
    _llm_type = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ainvoke(self, prompt, config=None):  # noqa: ANN001 - test stub
        self.calls.append(prompt)
        return SimpleNamespace(
            text="The assistant merged the previous checkpoint into a fresh summary body."
        )


def _conversation_block(prompt: str) -> str:
    start = prompt.index("<conversation>")
    end = prompt.index("</conversation>") + len("</conversation>")
    return prompt[start:end]


class TestFilterSummaryMessages:
    def test_filter_removes_summary_pair(self):
        normal_human = HumanMessage(content="hello")
        normal_ai = AIMessage(content="hi there")
        pair = _summary_pair("OLD-SUMMARY-BODY-1")
        messages = [normal_human, *pair, normal_ai]

        result = _filter_summary_messages(messages)

        assert result == [normal_human, normal_ai]

    def test_filter_preserves_normal_messages(self):
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            ToolMessage(content="t1", tool_call_id="c1"),
        ]

        result = _filter_summary_messages(messages)

        assert result == messages

    def test_filter_empty_list(self):
        assert _filter_summary_messages([]) == []

    def test_filter_multiple_summary_pairs(self):
        normal = [HumanMessage(content="q0"), AIMessage(content="a0")]
        a1 = AIMessage(content="a1")
        chain = [
            *normal,
            *_summary_pair("CHAIN-1"),
            HumanMessage(content="q1"),
            a1,
            *_summary_pair("CHAIN-2"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
            *_summary_pair("CHAIN-3"),
        ]

        result = _filter_summary_messages(chain)

        assert result == [
            *normal,
            HumanMessage(content="q1"),
            a1,
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
        ]
        assert all(m.additional_kwargs.get("lc_source") != _LC_SOURCE for m in result)

    def test_filter_unmarked_old_human(self):
        """Backward compatibility: pre-fix sessions carry an unmarked human
        half — it is not filtered (and only holds the 6-word question)."""
        unmarked_human = HumanMessage(content="What did we do so far?")
        tagged_ai = AIMessage(
            content="<summary>\nLEGACY-SUMMARY\n</summary>",
            additional_kwargs={"lc_source": _LC_SOURCE},
        )

        result = _filter_summary_messages([unmarked_human, tagged_ai])

        assert result == [unmarked_human]


def _reminder(source_id: str, content: str, *, compress_exclude: bool = True):
    return HumanMessage(
        content=wrap_reminder(source_id, content),
        metadata=system_reminder_metadata(source_id, compress_exclude=compress_exclude),
    )


class TestCompressExcludedReminders:
    def test_filter_drops_excluded_reminders_and_keeps_continuity(self):
        messages = [
            _reminder("todo_block", "TODO-OLD"),
            _reminder("taskflow_status", "BOARD-OLD"),
            _reminder("plan_mode_block", "PLAN-OLD"),
            _reminder("continuity_context", "CONTINUITY-KEEP", compress_exclude=False),
            HumanMessage(content="hello", metadata={"origin": "user"}),
        ]

        filtered = _filter_summary_messages(messages)

        assert len(filtered) == 2
        assert filtered[0].content == wrap_reminder("continuity_context", "CONTINUITY-KEEP")
        assert filtered[1].content == "hello"

    def test_filter_keeps_an_unlabeled_legacy_reminder(self):
        legacy = HumanMessage(
            content=wrap_reminder("todo_block", "OLD-NO-TAG"),
            metadata={
                "origin": "todo_block",
                "internal": True,
                "provenance": "reminder_catalog",
            },
        )

        assert _filter_summary_messages([legacy]) == [legacy]

    def test_serialize_labels_user_continuity_and_system_notice(self):
        continuation = HumanMessage(
            content="[SYSTEM DIRECTIVE: keep going]",
            metadata={"internal": True, "provenance": "continuation"},
        )
        notice = HumanMessage(
            content="[Git 分支/HEAD 已变化] moved to dev",
            metadata={
                "origin": "git_head",
                "internal": True,
                "provenance": "workspace_notice",
            },
        )

        out = _serialize_for_summary(
            [
                HumanMessage(content="hello", metadata={"origin": "user"}),
                _reminder("continuity_context", "CONTINUITY-KEEP", compress_exclude=False),
                _reminder("todo_block", "TODO-NOTICE"),
                notice,
                continuation,
            ]
        )

        assert "[User]: hello" in out
        assert "[Continuity]:" in out
        assert "CONTINUITY-KEEP" in out
        assert "[System notice]: <system-reminder" in out
        assert "[User]: [SYSTEM DIRECTIVE" not in out
        assert "[User]: [Git" not in out

    def test_continuity_is_not_truncated_to_500_chars(self):
        long_text = "C" * 900

        out = _serialize_for_summary(
            [_reminder("continuity_context", long_text, compress_exclude=False)]
        )

        assert long_text in out

    def test_static_fallback_collects_only_real_user_messages(self):
        continuation = HumanMessage(
            content="[SYSTEM DIRECTIVE: keep going]",
            metadata={"internal": True, "provenance": "continuation"},
        )

        out = _build_static_fallback_summary(
            [
                HumanMessage(content="fix the parser", metadata={"origin": "user"}),
                _reminder("todo_block", "TODO-OLD"),
                continuation,
            ]
        )

        assert "## Latest Unresolved User Request\n- fix the parser" in out
        assert "TODO-OLD" not in out
        assert "keep going" not in out

    def test_latest_eviction_ref_skips_injected_messages(self):
        injected = HumanMessage(
            content="notice",
            metadata={
                "origin": "git_head",
                "internal": True,
                "provenance": "workspace_notice",
            },
            additional_kwargs={EVICTED_TO_KEY: "/tmp/injected.txt"},
        )
        user = HumanMessage(
            content="the real request",
            metadata={"origin": "user"},
            additional_kwargs={EVICTED_TO_KEY: "/tmp/user.txt"},
        )

        assert _latest_human_eviction_ref([user, injected]) == "/tmp/user.txt"


class TestAsyncChainedSummaryFiltering:
    """Async mirror of the sync chained-summary assertions in
    ``test_compression_comprehensive.py::TestSummaryMessageFiltering``."""

    def test_acreate_summary_excludes_prior_pair_from_conversation(self):
        stub = StubAuxModel()
        mw = summarization_module.Summarization(model=stub)
        prior_pair = mw._build_new_messages("PRIOR-ASYNC-MARKER-6e2f")
        messages = [
            *prior_pair,
            HumanMessage(content="new question about the task"),
            AIMessage(content="new answer"),
        ]

        # Sanity: without filtering the serialized conversation WOULD leak the
        # prior summary body (non-vacuous regression guard).
        assert "PRIOR-ASYNC-MARKER-6e2f" in _serialize_for_summary(messages)

        asyncio.run(mw._acreate_summary(messages))

        assert len(stub.calls) == 1
        prompt = stub.calls[0]
        assert "<prior-summary>" in prompt
        assert "PRIOR-ASYNC-MARKER-6e2f" in prompt  # chained via <prior-summary>
        conversation = _conversation_block(prompt)
        assert "PRIOR-ASYNC-MARKER-6e2f" not in conversation
        assert "What did we do so far?" not in conversation
        assert "new question about the task" in conversation

    def test_acreate_summary_filters_live_reminders_but_keeps_continuity(self):
        stub = StubAuxModel()
        mw = summarization_module.Summarization(model=stub)
        messages = [
            _reminder("todo_block", "TODO-LEAK"),
            _reminder("taskflow_status", "BOARD-LEAK"),
            _reminder("continuity_context", "CONTINUITY-KEEP", compress_exclude=False),
            HumanMessage(content="please fix the parser", metadata={"origin": "user"}),
            AIMessage(content="working on it"),
        ]

        asyncio.run(mw._acreate_summary(messages))

        conversation = _conversation_block(stub.calls[0])
        assert "TODO-LEAK" not in conversation
        assert "BOARD-LEAK" not in conversation
        assert "[Continuity]:" in conversation
        assert "CONTINUITY-KEEP" in conversation
        assert "[User]: please fix the parser" in conversation
