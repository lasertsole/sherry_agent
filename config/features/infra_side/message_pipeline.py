"""Message-pipeline caps for last-turn slicing and tool-output dedup."""

from typing import TypedDict


class MessagePipelineConfig(TypedDict):
    """Message-pipeline caps for last-turn slicing and tool-output dedup."""

    slice_last_turn_token_max: int
    tool_output_dedup_default_protected_tools: frozenset[str]


MESSAGE_PIPELINE: MessagePipelineConfig = {
    "slice_last_turn_token_max": 6000,
    "tool_output_dedup_default_protected_tools": frozenset(),
}
