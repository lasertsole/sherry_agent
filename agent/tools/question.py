#!/usr/bin/env python3
"""
Question Tool — ask the user a multiple-choice question via a HITL interrupt.

The model calls ``question`` with a short question, a dialog header and 2-6
options. The tool suspends the graph with LangGraph ``interrupt()`` so the
frontend can render the choice dialog; the user's answer (or decline) is
returned to the model as the tool result after resume.

Tool metadata flags:
- ``idempotent``: asking a question has no side effects, so guardrails treat
  repeated identical calls as read-only.
- ``skip_heartbeat``: while the graph is suspended waiting for the user, the
  HeartbeatStaleness watchdog must not count the wait as a stalled turn.
- ``nudge``: exempt from skill-review accounting and callable by nudge
  review sub-agents.
"""

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


class QuestionOption(BaseModel):
    """One selectable option rendered on the question dialog."""

    label: str = Field(description="Short option label (1-5 words) shown on the button.")
    description: str = Field(description="What choosing this option means.")


class QuestionInput(BaseModel):
    """Schema for the question tool arguments."""

    question: str = Field(description="The question to show the user.")
    header: str = Field(
        max_length=30,
        description="Very short dialog title (max 30 characters).",
    )
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=6,
        description="2-6 selectable options. Put the recommended one first with "
        "'(Recommended)' appended to its label.",
    )
    multiple: bool = Field(
        default=False,
        description="Whether the user may select multiple options.",
    )


_DECLINE_MESSAGE = "User declined to answer. Proceed without this information."


class QuestionTool(BaseTool):
    name: str = "question"
    description: str = (
        "Ask the user a question with multiple options when you need to:\n"
        "- Gather user preferences or requirements\n"
        "- Clarify ambiguous instructions\n"
        "- Get a decision on implementation choices\n"
        'If you recommend a specific option, put it first and add "(Recommended)".'
    )
    args_schema: type[BaseModel] = QuestionInput
    metadata: dict = {"idempotent": True, "skip_heartbeat": True, "nudge": True}

    def _ask(
        self, question: str, header: str, options: list[QuestionOption], multiple: bool
    ) -> str:
        hitl_request = HITLRequest(
            action_requests=[
                ActionRequest(
                    name=self.name,
                    args={
                        "question": question,
                        "header": header,
                        "options": [option.model_dump() for option in options],
                        "multiple": multiple,
                    },
                    description=question,
                )
            ],
            review_configs=[
                ReviewConfig(
                    action_name=self.name,
                    allowed_decisions=["approve", "reject"],
                )
            ],
        )
        logger.debug("[QuestionTool] suspending graph for question: {}", header)
        response = interrupt(hitl_request)

        decisions = response.get("decisions", [])
        if decisions and decisions[0].get("type") == "approve":
            answer = decisions[0].get("message", "")
            return f'User answered: "{answer}". You can now continue.'
        return _DECLINE_MESSAGE

    @override
    def _run(
        self,
        question: str,
        header: str,
        options: list[QuestionOption],
        multiple: bool = False,
        **kwargs: Any,
    ) -> str:
        return self._ask(question, header, options, multiple)

    @override
    async def _arun(
        self,
        question: str,
        header: str,
        options: list[QuestionOption],
        multiple: bool = False,
        **kwargs: Any,
    ) -> str:
        return self._ask(question, header, options, multiple)


def build_question_tool() -> QuestionTool:
    tool: QuestionTool = QuestionTool()
    tool.handle_tool_error = True
    return tool
