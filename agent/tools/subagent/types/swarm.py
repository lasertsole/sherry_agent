"""Swarm/Collect mode type definitions: execution mode, run state, and group configuration."""

from enum import StrEnum
from pydantic import BaseModel


class SwarmMode(StrEnum):
    """Swarm execution mode. COLLECT = gather results from children; DISTRIBUTE = fan-out tasks to children."""

    COLLECT = "collect"
    DISTRIBUTE = "distribute"


class SwarmRunState(StrEnum):
    """Lifecycle state of a swarm participant."""

    RESERVED = "reserved"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class SwarmGroupConfig(BaseModel):
    """Configuration for a swarm group, controlling concurrency limits and output schema."""

    group_id: str
    max_children_per_group: int = 5
    max_total_per_group: int = 0
    max_concurrent: int = 3
    output_schema: dict | None = None
    fifo_queue: bool = True
