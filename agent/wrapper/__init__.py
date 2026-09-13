"""Graph wrapper package.

Houses the ordered, pluggable guards that wrap the compiled agent graph:

- ``agent.wrapper.repetition_guard`` — stream-level repetition interception.
- ``agent.wrapper.context_limit`` — context-window budget guard.
- ``agent.wrapper.registry`` — process-global wrapper-chain registry.

Every public name of the three modules is re-exported here so callers can
import from the package directly.
"""

from agent.wrapper.context_limit import (
    COMPRESSION_TRIGGER_RATIO as COMPRESSION_TRIGGER_RATIO,
)
from agent.wrapper.context_limit import (
    ContextLimitGuardWrapper as ContextLimitGuardWrapper,
)
from agent.wrapper.registry import (
    GraphWrapperFactory as GraphWrapperFactory,
)
from agent.wrapper.registry import (
    _DEFAULT_GRAPH_WRAPPER_FACTORIES as _DEFAULT_GRAPH_WRAPPER_FACTORIES,
)
from agent.wrapper.registry import (
    _GRAPH_WRAPPER_FACTORIES as _GRAPH_WRAPPER_FACTORIES,
)
from agent.wrapper.registry import (
    _default_context_limit as _default_context_limit,
)
from agent.wrapper.registry import (
    _default_repetition_guard as _default_repetition_guard,
)
from agent.wrapper.registry import (
    apply_graph_wrappers as apply_graph_wrappers,
)
from agent.wrapper.registry import (
    register_graph_wrapper as register_graph_wrapper,
)
from agent.wrapper.registry import (
    reset_graph_wrappers as reset_graph_wrappers,
)
from agent.wrapper.registry import (
    unregister_graph_wrapper as unregister_graph_wrapper,
)
from agent.wrapper.repetition_guard import (
    CutState as CutState,
)
from agent.wrapper.repetition_guard import (
    FreshState as FreshState,
)
from agent.wrapper.repetition_guard import (
    ModelTextState as ModelTextState,
)
from agent.wrapper.repetition_guard import (
    RepetitionGuardWrapper as RepetitionGuardWrapper,
)
from agent.wrapper.repetition_guard import (
    SESSION_STATE_KEYS as SESSION_STATE_KEYS,
)
from agent.wrapper.repetition_guard import (
    StreamGuardMachine as StreamGuardMachine,
)
from agent.wrapper.repetition_guard import (
    StreamGuardState as StreamGuardState,
)
from agent.wrapper.repetition_guard import (
    UpdatesSeenState as UpdatesSeenState,
)

__all__ = [
    "COMPRESSION_TRIGGER_RATIO",
    "ContextLimitGuardWrapper",
    "CutState",
    "FreshState",
    "GraphWrapperFactory",
    "ModelTextState",
    "RepetitionGuardWrapper",
    "SESSION_STATE_KEYS",
    "StreamGuardMachine",
    "StreamGuardState",
    "UpdatesSeenState",
    "_DEFAULT_GRAPH_WRAPPER_FACTORIES",
    "_GRAPH_WRAPPER_FACTORIES",
    "_default_context_limit",
    "_default_repetition_guard",
    "apply_graph_wrappers",
    "register_graph_wrapper",
    "reset_graph_wrappers",
    "unregister_graph_wrapper",
]
