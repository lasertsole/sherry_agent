#!/usr/bin/env python3
"""
Question Tool — ask the user structured multiple-choice questions via a HITL interrupt.

The model calls ``question`` with 1-4 questions (the ``questions`` array) or the
original single-question fields. Each question carries a short header and 2-6
options; an option may add a ``preview`` (code / mockup) shown side by side.
The tool suspends the graph with LangGraph ``interrupt()`` so the frontend can
render the dialog; the user's answer (or decline) is returned to the model as
the tool result after resume.

Answer transport: a SINGLE question keeps the historical plain-string answer
(``User answered: "..."``). A BATCH (2+ questions) has the frontend serialize
``{"answers": [{question, header, selection, annotation}, ...]}`` into the same
``message`` field; :func:`_format_answer` turns that into one line per question,
appending the user's free-text annotation when present.

Tool metadata flags:
- ``idempotent``: asking a question has no side effects, so guardrails treat
  repeated identical calls as read-only.
- ``skip_heartbeat``: while the graph is suspended waiting for the user, the
  HeartbeatStaleness watchdog must not count the wait as a stalled turn.
- ``nudge``: exempt from skill-review accounting and callable by nudge
  review sub-agents.
"""

import json

from loguru import logger
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing import Any, override
from agent.tools.pub_base import SessionId


class QuestionOption(BaseModel):
    """One selectable option rendered on the question dialog."""

    label: str = Field(description="Short option label (1-5 words) shown on the button.")
    description: str = Field(description="What choosing this option means.")
    preview: str = Field(
        default="",
        description="Optional code/mockup preview shown side-by-side for comparison.",
    )


class QuestionSpec(BaseModel):
    """One question in a multi-question batch."""

    question: str = Field(description="The question text.")
    header: str = Field(
        max_length=30,
        description="Very short label for this question (max 30 chars).",
    )
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=6,
        description="2-6 options. Put the recommended one first with '(Recommended)'.",
    )
    multiple: bool = Field(default=False, description="Allow multi-select.")
    allow_annotation: bool = Field(
        default=True,
        description="Whether to show a free-text annotation box under this question. "
        "The user can type something the options do not cover. Default True.",
    )


class QuestionInput(BaseModel):
    """Schema for the question tool arguments."""

    # New primary field — batch mode (1-4 questions in one dialog).
    questions: list[QuestionSpec] = Field(
        default=[],
        max_length=4,
        description="1-4 questions to ask in one dialog. Use this for batch "
        "clarification. If empty, falls back to the single-question fields below.",
    )
    # Deprecated fallback — single-question mode (also keeps older checkpoints
    # and older model habits working).
    question: str = Field(default="", description="(Deprecated) Use `questions`.")
    header: str = Field(default="", max_length=30, description="(Deprecated)")
    options: list[QuestionOption] = Field(
        default=[],
        max_length=6,
        description="(Deprecated) 2-6 selectable options for the flat question.",
    )
    multiple: bool = Field(
        default=False,
        description="(Deprecated) Whether the user may select multiple options.",
    )
    session_id: SessionId = ""


_DECLINE_MESSAGE = "User declined to answer. Proceed without this information."
_HEADLESS_DECLINE_MESSAGE = (
    "No human is present to answer (this is an automated turn). Proceed without "
    "this information and say what you assumed."
)


def _normalize_questions(
    questions: list[QuestionSpec] | list[dict[str, Any]] | None,
) -> list[QuestionSpec]:
    """Coerce raw dicts (direct ``_ask`` callers) into validated specs."""
    return [QuestionSpec(**q) if isinstance(q, dict) else q for q in (questions or [])]


def _normalize_options(
    options: list[QuestionOption] | list[dict[str, Any]] | None,
) -> list[QuestionOption]:
    """Coerce raw dicts (direct ``_ask`` callers) into validated options."""
    return [QuestionOption(**o) if isinstance(o, dict) else o for o in (options or [])]


def _format_answer(message: str, questions: list[QuestionSpec], is_multi: bool) -> str:
    """Parse the user's answer (plain string or JSON) and format it for the model.

    A single question keeps the historical one-line reply. A batch carries a JSON
    object in the ``message`` field; the fallback for a malformed payload (an
    older frontend, a hand-written resume) is the plain string, so a resume can
    never turn into an error.
    """
    if not is_multi:
        return f'User answered: "{message}". You can now continue.'
    try:
        data = json.loads(message)
        answers = data.get("answers", [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Not the batch shape: treat it as the plain answer.
        return f'User answered: "{message}". You can now continue.'
    if not isinstance(answers, list):
        return f'User answered: "{message}". You can now continue.'

    lines = ["User answered:"]
    for index, spec in enumerate(questions):
        answer = answers[index] if index < len(answers) else {}
        if not isinstance(answer, dict):
            answer = {}
        raw_selection = answer.get("selection", [])
        if isinstance(raw_selection, str):
            selection = [raw_selection] if raw_selection.strip() else []
        elif isinstance(raw_selection, list):
            selection = [str(item) for item in raw_selection if str(item).strip()]
        else:
            selection = []
        value = ", ".join(selection) if selection else "(no selection)"
        annotation = str(answer.get("annotation") or "").strip()
        if annotation:
            # The channel for "something the options do not cover".
            value += f" (note: {annotation})"
        label = spec.header or spec.question
        lines.append(f"• {label} → {value}")
    return "\n".join(lines) + "\nYou can now continue."


class QuestionTool(BaseTool):
    name: str = "question"
    description: str = (
        "Ask the user 1-4 structured multiple-choice questions in one dialog. Use for:\n"
        "- Gathering preferences or requirements\n"
        "- Clarifying ambiguous instructions\n"
        "- Getting decisions on implementation choices\n"
        "Each question has 2-6 options with label + description. Set `preview` on an "
        "option to show a code/mockup side-by-side. Put the recommended option first "
        'with "(Recommended)" in its label.\n'
        "Use the `questions` array for batch mode; the flat `question`/`header`/"
        "`options` fields are deprecated but still work for a single question."
    )
    args_schema: type[BaseModel] = QuestionInput
    metadata: dict = {
        "idempotent": True,
        "skip_heartbeat": True,
        "nudge": True,
        "side_effect_scope": "user_interaction",
    }

    def _ask(
        self,
        question: str = "",
        header: str = "",
        options: list[QuestionOption] | list[dict[str, Any]] | None = None,
        multiple: bool = False,
        session_id: str = "",
        questions: list[QuestionSpec] | list[dict[str, Any]] | None = None,
    ) -> str:
        # A headless turn (cron / auto-turn / system-injected carrier) has nobody
        # to answer: declining must win over payload validation, because
        # suspending the graph there leaves a turn interrupted forever and a
        # schema error would only invite a pointless retry.
        from agent.middlewares.humanInTheLoop.approval_scope import no_operator_in_scope

        if no_operator_in_scope():
            logger.info("[QuestionTool] headless turn — declining instead of suspending")
            return _HEADLESS_DECLINE_MESSAGE

        batch = _normalize_questions(questions)
        if not batch:
            # Deprecated fallback: the flat fields describe one question.
            flat_question = (question or "").strip()
            if not flat_question:
                return "Error: no question provided."
            flat_options = _normalize_options(options)
            if len(flat_options) < 2:
                return "Error: question needs at least 2 options."
            batch = [
                QuestionSpec(
                    question=flat_question,
                    header=header,
                    options=flat_options,
                    multiple=multiple,
                )
            ]
        for spec in batch:
            if len(spec.options) < 2:
                return f'Error: question "{spec.header}" needs at least 2 options.'

        is_multi = len(batch) > 1
        first = batch[0]
        hitl_request = HITLRequest(
            action_requests=[
                ActionRequest(
                    name=self.name,
                    args={
                        "questions": [spec.model_dump() for spec in batch],
                        # Keep the flat first-question fields so an older frontend
                        # (or a checkpoint replayed into one) still renders it.
                        "question": first.question,
                        "header": first.header,
                        "options": [option.model_dump() for option in first.options],
                        "multiple": first.multiple,
                        "_multi": is_multi,
                    },
                    description=first.question,
                )
            ],
            review_configs=[
                ReviewConfig(
                    action_name=self.name,
                    allowed_decisions=["approve", "reject"],
                )
            ],
        )
        logger.debug("[QuestionTool] suspending graph for {} question(s)", len(batch))
        response = interrupt(hitl_request)

        decisions = response.get("decisions", [])
        decision_type = decisions[0].get("type") if decisions else ""
        if decision_type == "yolo" and session_id:
            from agent.middlewares.humanInTheLoop.approval import set_session_yolo

            set_session_yolo(session_id)
        if decision_type in ("approve", "yolo"):
            answer = decisions[0].get("message", "")
            return _format_answer(answer, batch, is_multi)
        return _DECLINE_MESSAGE

    @override
    def _run(
        self,
        questions: list[QuestionSpec] | None = None,
        question: str = "",
        header: str = "",
        options: list[QuestionOption] | None = None,
        multiple: bool = False,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._ask(
            question=question,
            header=header,
            options=options,
            multiple=multiple,
            session_id=session_id,
            questions=questions,
        )

    @override
    async def _arun(
        self,
        questions: list[QuestionSpec] | None = None,
        question: str = "",
        header: str = "",
        options: list[QuestionOption] | None = None,
        multiple: bool = False,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._ask(
            question=question,
            header=header,
            options=options,
            multiple=multiple,
            session_id=session_id,
            questions=questions,
        )


def build_question_tool() -> QuestionTool:
    tool: QuestionTool = QuestionTool()
    tool.handle_tool_error = True
    return tool
