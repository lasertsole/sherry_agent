"""The main chain's middleware catalogue + which entries a session may switch off.

Two lists with different jobs:

* :data:`MIDDLEWARE_ORDER` — the chain's middleware names in registration order;
  it is what the 预设-中间件 tab shows and what a session config names. Kept
  beside the chain rather than derived from it (constructing
  ``_build_middlewares`` would build real LLMs); a test pins it against the real
  assembly with the same stubbing trick the order-contract test uses.
* :data:`GATEABLE_MIDDLEWARES` — the entries a session MAY disable. Everything
  else is always on: the scaffolded ``_MAIN_REQUIRED`` safety baseline
  (``scaffolding.py``), :data:`ALWAYS_ON_MIDDLEWARES` (the prompt injector — a
  turn without it has no instructions — and the model/thinking control, which
  IS the model picker), so the 工具/中间件 UI shows them locked.
"""

from __future__ import annotations

from .scaffolding import MAIN_REQUIRED_NAMES

__all__ = [
    "ALWAYS_ON_MIDDLEWARES",
    "GATEABLE_MIDDLEWARES",
    "MIDDLEWARE_ORDER",
    "middleware_catalog",
    "middleware_gateable",
    "middleware_required",
]

#: Registration order of the main-agent chain (``agent/core.py::_build_middlewares``).
MIDDLEWARE_ORDER: tuple[str, ...] = (
    "TodoContinuationEnforcer",
    "system_prompt_injection",
    "ProjectDirNoticeMiddleware",
    "ToolSelectionMiddleware",
    "MultimodalProcessor",
    "IterationBudget",
    "ToolGuardrails",
    "ContextEvictionMiddleware",
    "ToolCallNormalize",
    "PathGuard",
    "SubagentCompletionDrainMiddleware",
    "TaskIntentMiddleware",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "ThinkingControlMiddleware",
    "HeartbeatStaleness",
    "HumanInTheLoop",
    "MessagePersistenceMiddleware",
    "LLMRetryMiddleware",
    "Summarization",
)

#: Entries a session config may turn OFF (each hook early-returns when disabled).
#: ``ProjectDirNoticeMiddleware`` and ``MultimodalProcessor`` are NOT here any
#: more: they joined ``scaffolding._MAIN_REQUIRED``, so the build refuses a chain
#: without them and the service rejects a payload that tries to disable one.
GATEABLE_MIDDLEWARES: frozenset[str] = frozenset(
    {
        "TodoContinuationEnforcer",
        "TaskIntentMiddleware",
        "SubagentCompletionDrainMiddleware",
    }
)

#: Always on beyond the scaffolded required set — see the module docstring.
#: ``ToolSelectionMiddleware`` belongs here too: it has no switch of its own, it
#: is the thing that APPLIES the session's tool selection.
ALWAYS_ON_MIDDLEWARES: frozenset[str] = frozenset(
    {"system_prompt_injection", "ThinkingControlMiddleware", "ToolSelectionMiddleware"}
)


def middleware_required(name: str) -> bool:
    """Whether the entry is locked on (safety baseline or a logical necessity)."""
    return name in MAIN_REQUIRED_NAMES or name in ALWAYS_ON_MIDDLEWARES


def middleware_gateable(name: str) -> bool:
    """Whether a session config may disable the entry."""
    return name in GATEABLE_MIDDLEWARES


def middleware_catalog() -> list[dict[str, object]]:
    """``[{"name": ..., "required": ..., "gateable": ...}]`` in chain order."""
    return [
        {
            "name": name,
            "required": middleware_required(name),
            "gateable": middleware_gateable(name),
        }
        for name in MIDDLEWARE_ORDER
    ]
