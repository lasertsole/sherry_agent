"""Delivery context: encapsulates all information needed to deliver a sub-agent result back to the parent session."""

from pydantic import BaseModel
from .registry import RunOutcome


class DeliveryContext(BaseModel):
    """Payload passed through the announce pipeline when delivering a sub-agent result."""
    requester_session_key: str
    child_session_key: str
    child_label: str | None = None
    task: str
    result_text: str | None = None
    outcome: RunOutcome = RunOutcome()
    run_id: str
    depth: int = 1
    is_requester_subagent: bool = False
