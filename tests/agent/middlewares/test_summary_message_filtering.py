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
    _filter_summary_messages,
    _serialize_for_summary,
)

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
