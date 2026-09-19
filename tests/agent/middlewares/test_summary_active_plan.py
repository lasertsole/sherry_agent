"""Part 1 tests: plan-active detection, prompt injection and Active Plan Notes.

Covered contracts:

* the resolver reads only the two association sources (state ``plan_ref``,
  session todos ``plan_ref``) and deactivates the plan when every todo is
  ``completed`` / ``cancelled``;
* both summary prompt paths (first / update, structured / legacy) carry the
  authoritative plan block; no active plan injects nothing;
* ``SummaryDoc.active_plan_notes`` is inherited verbatim across chained
  compressions, only appended to by the model, capped at the tail, and cleared
  when the plan is no longer active;
* ``latest_user_request`` stays verbatim (no 800-char truncation) and an
  evicted latest request carries its ``[evicted to: <path>]`` pointer;
* the newest human message wins in a multi-message burst; the older evicted
  ones live in ``evicted_refs``.
"""

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import agent.middlewares.summarization.core as summarization_module
import agent.middlewares.summarization.plan_context as plan_context_module
from agent.middlewares.summarization.plan_context import (
    ActivePlan,
    render_plan_context,
    resolve_active_plan,
)
from agent.middlewares.summarization.summary_doc import (
    ACTIVE_PLAN_NOTES_MAX_ITEMS,
    SummaryDoc,
    render_summary_markdown,
)
from pub.func.message.eviction import EVICTED_TO_KEY
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

    def with_structured_output(self, schema, *, method=None, **kwargs):
        return self.runnable

    def invoke(self, prompt, config=None):
        if self.raw_exc is not None:
            raise self.raw_exc
        return SimpleNamespace(text=self.raw_text)

    async def ainvoke(self, prompt, config=None):
        if self.raw_exc is not None:
            raise self.raw_exc
        return SimpleNamespace(text=self.raw_text)


@pytest.fixture
def sid(request):
    session = "tap-" + request.node.name[:30] + "-" + uuid.uuid4().hex[:6]
    yield session
    try:
        state_register_mem.clear_session(session)
    except Exception:  # noqa: S110 - best-effort cleanup
        pass


@pytest.fixture(autouse=True)
def _hermetic_taskflow(monkeypatch):
    """Keep the prompts scoped to this file's contract (no TaskFlow store read)."""
    monkeypatch.setattr(summarization_module, "_get_taskflow_context_sync", lambda _sid: "")


def _plan(**overrides):
    values = dict(
        plan_ref="alpha.md",
        plan_name="alpha",
        display_path="workspace/sessions/s/plans/alpha.md",
        open_items=("fix the widget", "re-run the suite"),
        done_count=1,
        total_count=3,
    )
    values.update(overrides)
    return ActivePlan(**values)


def _todo(content, status, ref="alpha.md"):
    return {"content": content, "status": status, "plan_ref": ref}


class TestResolveActivePlan:
    """Unit: sources 1/2 + the all-done completion gate."""

    def _patch(self, monkeypatch, *, ref="", todos=()):
        import agent.tools.todolist.registry.store_sqlite as store

        monkeypatch.setattr(
            plan_context_module.state_register_db,
            "get_state",
            lambda sid, key, default=None: ref if key == "plan_ref" else default,
        )
        monkeypatch.setattr(store, "get_todos_sync", lambda _sid: list(todos))

    def test_state_ref_with_open_todos_is_active(self, monkeypatch):
        self._patch(
            monkeypatch,
            ref="alpha.md",
            todos=[_todo("done step", "completed"), _todo("open step", "pending")],
        )
        plan = resolve_active_plan("s1")
        assert plan is not None
        assert plan.plan_name == "alpha"
        assert plan.display_path == "alpha.md"
        assert plan.open_items == ("open step",)
        assert (plan.done_count, plan.total_count) == (1, 2)

    def test_todo_ref_is_used_when_state_has_none(self, monkeypatch):
        self._patch(
            monkeypatch,
            ref="",
            todos=[_todo("todo-only step", "in_progress", ref="beta.md")],
        )
        plan = resolve_active_plan("s1")
        assert plan is not None
        assert plan.plan_name == "beta"
        assert plan.open_items == ("todo-only step",)

    def test_all_done_todos_deactivate_the_plan(self, monkeypatch):
        self._patch(
            monkeypatch,
            ref="alpha.md",
            todos=[
                _todo("done", "completed"),
                _todo("dropped", "cancelled"),
            ],
        )
        assert resolve_active_plan("s1") is None

    def test_no_plan_ref_is_none(self, monkeypatch):
        self._patch(monkeypatch, ref="", todos=[_todo("unlinked", "pending", ref=None)])
        assert resolve_active_plan("s1") is None

    def test_empty_or_broken_store_is_fail_open(self, monkeypatch):
        import agent.tools.todolist.registry.store_sqlite as store

        def _boom(_sid):
            raise RuntimeError("todos store down")

        monkeypatch.setattr(
            plan_context_module.state_register_db,
            "get_state",
            lambda sid, key, default=None: "alpha.md" if key == "plan_ref" else default,
        )
        monkeypatch.setattr(store, "get_todos_sync", _boom)
        plan = resolve_active_plan("s1")
        assert plan is not None  # no todos readable -> plan assumed active
        assert plan.open_items == ()
        assert resolve_active_plan("") is None

    def test_render_block_has_path_name_and_open_items(self):
        block = render_plan_context(_plan())
        assert block.startswith("## Active Plan (authoritative)")
        assert "- Plan: alpha" in block
        assert "- File: workspace/sessions/s/plans/alpha.md" in block
        assert "- Todo progress: 1/3 done" in block
        assert "1. fix the widget" in block
        assert "2. re-run the suite" in block

    def test_render_bounds_open_items(self):
        plan = _plan(open_items=tuple(f"item-{i}" for i in range(10)), done_count=0, total_count=10)
        block = render_plan_context(plan)
        assert "8. item-7" in block
        assert "item-9" not in block
        assert "(+2 more)" in block

    def test_render_needs_no_open_items(self):
        assert "- Open todos: (none)" in render_plan_context(
            _plan(open_items=(), done_count=3, total_count=3)
        )


class TestPlanPromptInjection:
    """Both prompt paths carry the block; no plan injects nothing."""

    def test_first_and_update_prompts_carry_the_plan_block(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: _plan())
        mw = summarization_module.Summarization(model=_StubModel())

        structured_first = mw._build_structured_summary_prompt("CONV", None, None, session_id=sid)
        structured_update = mw._build_structured_summary_prompt(
            "CONV", None, "PRIOR", session_id=sid
        )
        legacy_first = mw._build_summary_prompt("CONV", None, session_id=sid)
        legacy_update = mw._build_summary_prompt("CONV", "PRIOR", session_id=sid)

        for prompt in (structured_first, structured_update, legacy_first, legacy_update):
            assert "## Active Plan (authoritative)" in prompt
            assert "- Plan: alpha" in prompt
            assert "workspace/sessions/s/plans/alpha.md" in prompt
            assert "1. fix the widget" in prompt

    def test_no_active_plan_injects_no_block(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)
        mw = summarization_module.Summarization(model=_StubModel())
        prompt = mw._build_structured_summary_prompt("CONV", None, None, session_id=sid)
        assert "## Active Plan (authoritative)" not in prompt

    def test_plan_context_failure_is_fail_open(self, sid, monkeypatch):
        def _boom(_sid):
            raise RuntimeError("plan store down")

        monkeypatch.setattr(summarization_module, "resolve_active_plan", _boom)
        mw = summarization_module.Summarization(model=_StubModel())
        assert summarization_module._get_plan_context_sync(sid) == ""
        prompt = mw._build_structured_summary_prompt("CONV", None, None, session_id=sid)
        assert "## Active Plan (authoritative)" not in prompt
        assert "CONV" in prompt


class TestActivePlanNotesChain:
    """The pipeline owns the array: inherit verbatim, append, cap, clear."""

    @staticmethod
    def _active(monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: _plan())

    @staticmethod
    def _no_plan(monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)

    def test_notes_survive_five_chained_compressions_verbatim(self, sid, monkeypatch):
        self._active(monkeypatch)
        model = _StubModel(structured=SummaryDoc(goal="g0", active_plan_notes=["note-A", "note-B"]))
        mw = summarization_module.Summarization(model=model)

        doc = mw._create_summary(
            [HumanMessage(content="q0"), AIMessage(content="a0")], session_id=sid
        )
        assert isinstance(doc, SummaryDoc)
        pair = mw._build_new_messages(doc)
        for index in range(5):
            # The model returns no notes at all; only the chain carries them.
            model.runnable.result = SummaryDoc(goal=f"g{index + 1}")
            doc = mw._create_summary([*pair, HumanMessage(content=f"q{index + 1}")], session_id=sid)
            assert isinstance(doc, SummaryDoc)
            assert doc.active_plan_notes == ["note-A", "note-B"]
            pair = mw._build_new_messages(doc)

        stored = mw._extract_previous_doc(pair)
        assert stored is not None
        assert stored.active_plan_notes == ["note-A", "note-B"]
        rendered = mw._extract_previous_summary(pair)
        assert rendered is not None
        assert rendered.count("- note-A") == 1
        assert rendered.count("- note-B") == 1

    def test_new_lesson_is_appended_and_existing_entries_untouched(self, sid, monkeypatch):
        self._active(monkeypatch)
        model = _StubModel(structured=SummaryDoc(active_plan_notes=["old-1", "old-2"]))
        mw = summarization_module.Summarization(model=model)

        first = mw._create_summary(
            [HumanMessage(content="q1"), AIMessage(content="a1")], session_id=sid
        )
        assert isinstance(first, SummaryDoc)
        pair = mw._build_new_messages(first)

        model.runnable.result = SummaryDoc(goal="g", active_plan_notes=["new-3"])
        second = mw._create_summary([*pair, HumanMessage(content="q2")], session_id=sid)
        assert isinstance(second, SummaryDoc)
        assert second.active_plan_notes == ["old-1", "old-2", "new-3"]
        rendered = render_summary_markdown(second)
        assert rendered.index("- old-1") < rendered.index("- old-2") < rendered.index("- new-3")

    def test_note_cap_evicts_the_oldest_with_annotation(self, sid, monkeypatch):
        self._active(monkeypatch)
        prior = [f"n{i}" for i in range(ACTIVE_PLAN_NOTES_MAX_ITEMS)]
        model = _StubModel(structured=SummaryDoc(active_plan_notes=prior))
        mw = summarization_module.Summarization(model=model)

        first = mw._create_summary(
            [HumanMessage(content="q1"), AIMessage(content="a1")], session_id=sid
        )
        assert isinstance(first, SummaryDoc)
        pair = mw._build_new_messages(first)

        newest = f"n{ACTIVE_PLAN_NOTES_MAX_ITEMS}"
        model.runnable.result = SummaryDoc(active_plan_notes=[newest])
        second = mw._create_summary([*pair, HumanMessage(content="q2")], session_id=sid)
        assert isinstance(second, SummaryDoc)
        assert second.active_plan_notes == [*prior, newest]  # pre-cap document state

        capped_pair = mw._build_new_messages(second)
        stored = mw._extract_previous_doc(capped_pair)
        assert stored is not None
        assert stored.active_plan_notes == [*prior[1:], newest]
        chain_content = capped_pair[1].content
        assert "(1 earlier items omitted for brevity)" in chain_content
        assert "- n0\n" not in chain_content
        assert chain_content.count(newest) == 1

    def test_completed_plan_clears_notes_and_section(self, sid, monkeypatch):
        self._active(monkeypatch)
        model = _StubModel(structured=SummaryDoc(active_plan_notes=["plan-lesson"]))
        mw = summarization_module.Summarization(model=model)

        first = mw._create_summary(
            [HumanMessage(content="q1"), AIMessage(content="a1")], session_id=sid
        )
        assert isinstance(first, SummaryDoc)
        assert first.active_plan_notes == ["plan-lesson"]
        pair = mw._build_new_messages(first)

        # The plan's todos became all-completed: the resolver returns None.
        self._no_plan(monkeypatch)
        model.runnable.result = SummaryDoc(goal="done")
        second = mw._create_summary([*pair, HumanMessage(content="q2")], session_id=sid)
        assert isinstance(second, SummaryDoc)
        assert second.active_plan_notes == []
        assert "## Active Plan Notes" not in render_summary_markdown(second)
        stored = mw._extract_previous_doc(mw._build_new_messages(second))
        assert stored is not None
        assert stored.active_plan_notes == []

    def test_no_active_plan_keeps_notes_empty(self, sid, monkeypatch):
        self._no_plan(monkeypatch)
        model = _StubModel(structured=SummaryDoc(active_plan_notes=["hallucinated"]))
        mw = summarization_module.Summarization(model=model)
        doc = mw._create_summary(
            [HumanMessage(content="q"), AIMessage(content="a")], session_id=sid
        )
        assert isinstance(doc, SummaryDoc)
        assert doc.active_plan_notes == []
        assert "## Active Plan Notes" not in render_summary_markdown(doc)


class TestLatestRequestVerbatimAndEviction:
    """Verbatim latest request + the eviction pointer contract."""

    _LONG = "REQ-" + ("x" * 1400)  # > LATEST_USER_REQUEST_MAX_CHARS (800)

    def test_evicted_latest_request_is_verbatim_with_pointer(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)
        evicted_to = "/sessions/s/evicted/human-1.md"
        model = _StubModel(structured=SummaryDoc(latest_user_request=self._LONG))
        mw = summarization_module.Summarization(model=model)
        messages = [
            HumanMessage(content=self._LONG, additional_kwargs={EVICTED_TO_KEY: evicted_to}),
            AIMessage(content="working on it"),
        ]

        doc = mw._create_summary(messages, session_id=sid)
        assert isinstance(doc, SummaryDoc)
        assert doc.latest_user_request == f"{self._LONG}\n[evicted to: {evicted_to}]"
        rendered = render_summary_markdown(doc)
        assert self._LONG in rendered
        assert f"[evicted to: {evicted_to}]" in rendered

        # The aux prompt serialized the FULL state text — no 800-char truncation
        # and no model-view preview standing in for the state content.
        prompt = model.runnable.prompts[0]
        assert self._LONG in prompt
        assert "<prior-summary-json>" not in prompt

    def test_multi_message_keeps_the_last_request_and_older_evictions(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)
        first_text = "A" * 1500
        second_text = "B" * 1500
        last_text = "综合提问：请把前面两条长消息合并成一个结论。"
        model = _StubModel(structured=SummaryDoc(latest_user_request=last_text))
        mw = summarization_module.Summarization(model=model)
        messages = [
            HumanMessage(content=first_text, additional_kwargs={EVICTED_TO_KEY: "/evicted/a.md"}),
            HumanMessage(content=second_text, additional_kwargs={EVICTED_TO_KEY: "/evicted/b.md"}),
            HumanMessage(content=last_text),
        ]

        doc = mw._create_summary(messages, session_id=sid)
        assert isinstance(doc, SummaryDoc)
        # The newest message is the request; it was not evicted, so no pointer.
        assert doc.latest_user_request == last_text
        assert doc.evicted_refs == ["/evicted/a.md", "/evicted/b.md"]
        rendered = render_summary_markdown(doc)
        assert last_text in rendered
        assert "## Evicted References" in rendered
        assert "/evicted/a.md" in rendered
        assert "/evicted/b.md" in rendered

        prompt = model.runnable.prompts[0]
        assert first_text in prompt
        assert second_text in prompt
        assert last_text in prompt

    def test_untagged_latest_request_gets_no_pointer(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)
        model = _StubModel(structured=SummaryDoc(latest_user_request="plain question"))
        mw = summarization_module.Summarization(model=model)
        doc = mw._create_summary([HumanMessage(content="plain question")], session_id=sid)
        assert isinstance(doc, SummaryDoc)
        assert doc.latest_user_request == "plain question"
        assert "evicted to" not in render_summary_markdown(doc)

    def test_internal_injection_does_not_detach_the_evicted_pointer(self, sid, monkeypatch):
        monkeypatch.setattr(summarization_module, "resolve_active_plan", lambda _sid: None)
        evicted_to = "/evicted/user-request.md"
        model = _StubModel(structured=SummaryDoc(latest_user_request=self._LONG))
        mw = summarization_module.Summarization(model=model)
        messages = [
            HumanMessage(content=self._LONG, additional_kwargs={EVICTED_TO_KEY: evicted_to}),
            HumanMessage(
                content="<task intent steering>",
                metadata={"origin": "task_intent", "internal": True},
            ),
        ]
        doc = mw._create_summary(messages, session_id=sid)
        assert isinstance(doc, SummaryDoc)
        assert doc.latest_user_request == f"{self._LONG}\n[evicted to: {evicted_to}]"
