"""Sub-agent role and control-scope enums.

Roles are determined by nesting depth:
- depth 0: MAIN (can spawn, controls CHILDREN)
- 0 < depth < max: ORCHESTRATOR (can spawn, controls CHILDREN)
- depth >= max: LEAF (cannot spawn, controls NONE)
"""

from enum import Enum


class SubagentSessionRole(str, Enum):
    """Sub-agent role. MAIN = top-level agent; ORCHESTRATOR = mid-layer that can spawn; LEAF = terminal node that cannot spawn."""
    MAIN = "main"
    ORCHESTRATOR = "orchestrator"
    LEAF = "leaf"


class ControlScope(str, Enum):
    """Control scope. CHILDREN = can control direct child sub-agents; NONE = no control authority."""
    CHILDREN = "children"
    NONE = "none"
