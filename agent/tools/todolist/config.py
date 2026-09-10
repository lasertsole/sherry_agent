"""TodoList shared constants: table name plus the four closed vocabularies.

The todo store keeps only session-scoped checklist state (content/status/
priority/position + delegation + TaskFlow pointers). DAG scheduling
(depends_on/wave) is deliberately absent: dependency edges, unlock logic and
``blocked/ready/dispatched/done`` live in TaskFlow, reached through
``flow_id``/``step_id``.
"""

from enum import StrEnum

TABLE_NAME = "todos"


class TodoStatus(StrEnum):
    """Lifecycle status of one session todo."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoPriority(StrEnum):
    """Relative priority of one session todo."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TodoCategory(StrEnum):
    """Routing category used by the delegation layer (E6)."""

    QUICK = "quick"
    DEEP = "deep"
    ULTRABRAIN = "ultrabrain"
    VISUAL = "visual"
    GIT = "git"
    WRITING = "writing"


class TodoDelegation(StrEnum):
    """Whether a todo is executed inline or fanned out to a subagent."""

    SELF = "self"
    SUBAGENT = "subagent"


VALID_STATUSES = frozenset(member.value for member in TodoStatus)
VALID_PRIORITIES = frozenset(member.value for member in TodoPriority)
VALID_CATEGORIES = frozenset(member.value for member in TodoCategory)
VALID_DELEGATIONS = frozenset(member.value for member in TodoDelegation)

# Applied when a caller omits a column (the schema carries the same defaults).
DEFAULT_STATUS = TodoStatus.PENDING.value
DEFAULT_PRIORITY = TodoPriority.MEDIUM.value
DEFAULT_CATEGORY = TodoCategory.QUICK.value
DEFAULT_DELEGATION = TodoDelegation.SELF.value
