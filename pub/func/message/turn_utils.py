from __future__ import annotations
from dataclasses import dataclass
from langchain_core.messages import BaseMessage, HumanMessage

from pub.func.message.workspace_notice import is_workspace_notice


@dataclass
class Turn:
    start_idx: int
    end_idx: int
    messages: list[BaseMessage]


def split_into_turns(messages: list[BaseMessage]) -> list[Turn]:
    if not messages:
        return []
    turns: list[Turn] = []
    turn_start = 0
    for i, msg in enumerate(messages):
        # An injected workspace notice (working-directory / git change) is a
        # HumanMessage that EXPLAINS the turn it precedes, not a turn of its own:
        # counting it as a boundary adds an empty turn per notice and shifts what
        # the turn-budget walk keeps.
        if isinstance(msg, HumanMessage) and i > 0 and not is_workspace_notice(msg):
            turns.append(Turn(turn_start, i, messages[turn_start:i]))
            turn_start = i
    turns.append(Turn(turn_start, len(messages), messages[turn_start:]))
    return turns


def split_turn(
    turn: Turn,
    budget_tokens: int,
    estimator,
) -> int | None:
    if budget_tokens <= 0:
        return None
    if turn.end_idx - turn.start_idx <= 1:
        return None
    for start in range(turn.start_idx + 1, turn.end_idx):
        remaining = turn.messages[start - turn.start_idx :]
        size = estimator(remaining)
        if size <= budget_tokens:
            return start
    return None
