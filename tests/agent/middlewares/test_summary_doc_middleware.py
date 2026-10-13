"""Middleware-level tests for the structured summary path (json_mode → json_repair → free-form)."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.summarization.core as summarization_module
from agent.middlewares.summarization.summary_doc import (
    SummaryDoc,
    cap_summary_doc,
    render_summary_markdown,
)
from runtime import state_register_mem

pytestmark = [pytest.mark.module]


class _RunnableStub:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.prompts: list[str] = []

    def _take(self, prompt):
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.result

    def invoke(self, prompt, config=None):
        return self._take(prompt)

    async def ainvoke(self, prompt, config=None):
        return self._take(prompt)


class _StubModel:
    """Aux-shaped stub: structured runnable + raw invoke/ainvoke."""

    _llm_type = "fake"

    def __init__(self, *, structured=None, structured_exc=None, raw_text="", raw_exc=None):
        self.runnable = _RunnableStub(structured, structured_exc)
        self.raw_text = raw_text
        self.raw_exc = raw_exc
        self.methods: list[str | None] = []
        self.raw_prompts: list[str] = []

    def with_structured_output(self, schema, *, method=None, **kwargs):
        self.methods.append(method)
        return self.runnable

    def invoke(self, prompt, config=None):
        self.raw_prompts.append(prompt)
        if self.raw_exc is not None:
            raise self.raw_exc
        return SimpleNamespace(text=self.raw_text)

    async def ainvoke(self, prompt, config=None):
        self.raw_prompts.append(prompt)
        if self.raw_exc is not None:
            raise self.raw_exc
        return SimpleNamespace(text=self.raw_text)


class _PlainStubModel:
    """Legacy-shaped stub: no with_structured_output at all."""

    _llm_type = "fake"

    def __init__(self, text):
        self.text = text
        self.calls: list[str] = []

    def invoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text=self.text)

    async def ainvoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text=self.text)


def _conversation_block(prompt: str) -> str:
    start = prompt.index("<conversation>")
    end = prompt.index("</conversation>") + len("</conversation>")
    return prompt[start:end]


@pytest.fixture
def sid(request):
    session = "tsd-" + request.node.name[:30] + "-" + uuid.uuid4().hex[:6]
    yield session
    try:
        state_register_mem.clear_session(session)
    except Exception:  # noqa: S110 - best-effort cleanup
        pass


class TestStructuredTier:
    def test_json_mode_is_the_structured_method(self):
        model = _StubModel(structured=SummaryDoc(goal="g"))
        mw = summarization_module.Summarization(model=model)
        mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert model.methods == ["json_mode"]

    def test_first_call_returns_a_summary_doc(self):
        model = _StubModel(structured=SummaryDoc(goal="g", completed=["c1"]))
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert isinstance(result, SummaryDoc)
        assert result.goal == "g"

    def test_structured_prompt_carries_conversation_and_json_rules(self):
        model = _StubModel(structured=SummaryDoc(goal="g"))
        mw = summarization_module.Summarization(model=model)
        mw._create_summary([HumanMessage(content="UNIQUE-CONV-77"), AIMessage(content="a")])
        prompt = model.runnable.prompts[0]
        assert "<conversation>" in prompt
        assert "UNIQUE-CONV-77" in prompt
        assert '"latest_user_request"' in prompt
        assert "creating a context checkpoint" in prompt

    def test_async_twin_returns_a_summary_doc(self):
        model = _StubModel(structured=SummaryDoc(goal="async-goal"))
        mw = summarization_module.Summarization(model=model)
        result = asyncio.run(
            mw._acreate_summary([HumanMessage(content="q"), AIMessage(content="a")])
        )
        assert isinstance(result, SummaryDoc)
        assert result.goal == "async-goal"
        assert model.runnable.prompts

    def test_flat_dict_result_is_validated(self):
        model = _StubModel(structured={"goal": "dict-goal"})
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert isinstance(result, SummaryDoc)
        assert result.goal == "dict-goal"


class TestFallbackTiers:
    def test_malformed_json_falls_back_to_json_repair(self):
        model = _StubModel(
            structured_exc=RuntimeError("parser rejected"),
            raw_text='{"goal": "repaired", "completed": ["a", "b"],}',
        )
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert isinstance(result, SummaryDoc)
        assert result.goal == "repaired"
        assert result.completed == ["a", "b"]
        assert len(model.raw_prompts) == 1

    def test_total_failure_falls_back_to_free_form(self):
        prose = (
            "## Goal\n- legacy free-form summary long enough to clear the fifty character gate.\n"
        )
        model = _StubModel(structured_exc=RuntimeError("boom"), raw_text=prose)
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert result == prose.strip()
        assert len(model.raw_prompts) == 2
        assert "Output exactly the Markdown structure" in model.raw_prompts[1]

    def test_free_form_fallback_short_answer_uses_static_summary(self):
        model = _StubModel(structured_exc=RuntimeError("boom"), raw_text="too short")
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert "## Latest Unresolved User Request" in str(result)

    def test_model_without_structured_output_uses_single_free_form_call(self):
        model = _PlainStubModel(
            "Legacy free-form summary body long enough to clear the fifty character gate."
        )
        mw = summarization_module.Summarization(model=model)
        result = mw._create_summary([HumanMessage(content="q"), AIMessage(content="a")])
        assert result == model.text
        assert len(model.calls) == 1


class TestChaining:
    def test_prior_doc_is_fed_as_json_on_update(self):
        model = _StubModel(structured=SummaryDoc(goal="first"))
        mw = summarization_module.Summarization(model=model)

        first = mw._create_summary([HumanMessage(content="q1"), AIMessage(content="a1")])
        pair = mw._build_new_messages(first)
        model.runnable.result = SummaryDoc(goal="second")
        model.runnable.prompts.clear()
        result = mw._create_summary([*pair, HumanMessage(content="q2")])

        prompt = model.runnable.prompts[0]
        assert "<prior-summary-json>" in prompt
        prior_json = prompt.split("<prior-summary-json>")[1].split("</prior-summary-json>")[0]
        assert '"goal":"first"' in prior_json
        assert '"goal":"second"' not in prior_json
        assert "updating a context checkpoint" in prompt
        assert "PRIOR" not in _conversation_block(prompt)
        assert "q2" in _conversation_block(prompt)
        assert isinstance(result, SummaryDoc)
        assert result.goal == "second"

    def test_legacy_markdown_becomes_the_prior_summary_and_output_is_a_doc(self):
        model = _StubModel(structured=SummaryDoc(goal="new-doc"))
        mw = summarization_module.Summarization(model=model)
        legacy_pair = [
            HumanMessage(
                content="What did we do so far?",
                additional_kwargs={"lc_source": "summarization"},
            ),
            AIMessage(
                content="<summary>\n## Goal\n- LEGACY-GOAL-42\n</summary>",
                additional_kwargs={"lc_source": "summarization"},
            ),
        ]
        result = mw._create_summary([*legacy_pair, HumanMessage(content="q")])
        assert isinstance(result, SummaryDoc)
        prompt = model.runnable.prompts[0]
        assert "<prior-summary>" in prompt
        assert "LEGACY-GOAL-42" in prompt
        assert "\n<prior-summary-json>\n" not in prompt

    def test_doc_round_trips_through_build_and_extract(self):
        mw = summarization_module.Summarization(model=_StubModel())
        doc = SummaryDoc(goal="round-trip", completed=["c1"], relevant_files=["a.py: r"])
        pair = mw._build_new_messages(doc)
        assert mw._extract_previous_doc(pair) == cap_summary_doc(doc)
        assert mw._extract_previous_summary(pair) == render_summary_markdown(doc)

    def test_extract_prefers_stored_doc_over_mutated_markdown(self):
        mw = summarization_module.Summarization(model=_StubModel())
        doc = SummaryDoc(goal="doc-wins")
        pair = mw._build_new_messages(doc)
        mutated = pair[1].model_copy(
            update={"content": pair[1].content.replace("doc-wins", "STALE-CONTENT")}
        )
        assert "STALE-CONTENT" not in mw._extract_previous_summary([pair[0], mutated])
        assert "doc-wins" in mw._extract_previous_summary([pair[0], mutated])


class TestOutputPair:
    def test_capped_doc_payload_is_stored(self):
        mw = summarization_module.Summarization(model=_StubModel())
        doc = SummaryDoc(completed=[f"c{i}" for i in range(8)])
        pair = mw._build_new_messages(doc)
        payload = pair[1].additional_kwargs["summary_doc"]
        assert payload["completed"] == doc.completed[-5:]
        assert "(3 earlier items omitted for brevity)" in pair[1].content

    def test_free_form_string_keeps_the_legacy_payload(self):
        mw = summarization_module.Summarization(model=_StubModel())
        pair = mw._build_new_messages("LEGACY-STRING-BODY")
        assert "LEGACY-STRING-BODY" in pair[1].content
        assert "summary_doc" not in pair[1].additional_kwargs


class TestEvictedRefs:
    def test_refs_are_collected_from_tool_previews_and_human_tags(self):
        model = _StubModel(structured=SummaryDoc(goal="g"))
        mw = summarization_module.Summarization(model=model)
        messages = [
            HumanMessage(
                content="huge human message",
                additional_kwargs={"lc_evicted_to": "/sessions/s/evicted/human-1.md"},
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {}, "id": "c1"}],
            ),
            ToolMessage(
                content="[evicted to: /sessions/s/evicted/c1_ab12.txt]\n--- head ...",
                tool_call_id="c1",
            ),
        ]
        result = mw._create_summary(messages)
        assert isinstance(result, SummaryDoc)
        assert result.evicted_refs == [
            "/sessions/s/evicted/human-1.md",
            "/sessions/s/evicted/c1_ab12.txt",
        ]

    def test_refs_are_carried_on_the_chain_and_deduplicated(self):
        model = _StubModel(structured=SummaryDoc(goal="one"))
        mw = summarization_module.Summarization(model=model)
        first = mw._create_summary(
            [
                ToolMessage(
                    content="[evicted to: /sessions/s/evicted/keep.txt]",
                    tool_call_id="c1",
                )
            ]
        )
        pair = mw._build_new_messages(first)
        model.runnable.result = SummaryDoc(goal="two")
        second = mw._create_summary([*pair, HumanMessage(content="next")])
        assert isinstance(second, SummaryDoc)
        assert second.evicted_refs == ["/sessions/s/evicted/keep.txt"]
