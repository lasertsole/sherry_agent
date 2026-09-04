from __future__ import annotations
from dataclasses import dataclass
from langchain_core.messages import BaseMessage, HumanMessage


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
        if isinstance(msg, HumanMessage) and i > 0:
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
        remaining = turn.messages[start - turn.start_idx:]
        size = estimator(remaining)
        if size <= budget_tokens:
            return start
    return None
