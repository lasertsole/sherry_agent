"""The main chain's middleware names, derived from the specification table.

:data:`MIDDLEWARE_ORDER` is :func:`agent.middlewares.specs.main_middleware_order`
— one projection of the same table that assembles the chain, feeds the
scaffolding baseline, and drives the child chain. It therefore cannot drift
from the registered order. Conditionally registered entries are included in
the declaration order: ``DelegationPressureGate`` is appended only when its
feature flag (or the eval override) is enabled.

Chain membership is a BUILD-TIME contract — a user cannot switch an entry off.
The per-session middleware selection was removed together with the preset
panel's middleware tab: a session config no longer carries a disabled list or
per-middleware options, and every registered entry runs.
"""

from __future__ import annotations

from .specs import main_middleware_order

__all__ = ["MIDDLEWARE_ORDER"]

#: Registration order of the main-agent chain (``agent/core.py::_build_middlewares``).
MIDDLEWARE_ORDER: tuple[str, ...] = main_middleware_order()
