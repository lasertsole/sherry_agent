"""Security primitives shared across the agent layer.

Currently the prompt-injection / exfiltration pattern scanner
(:mod:`agent.security.threat_patterns`). The module is dependency-free on
purpose: every tool surface (web search, terminal, file reads, memory writes)
may call it without dragging the agent graph in.
"""

from .threat_patterns import (
    SCAN_SCOPES as SCAN_SCOPES,
    first_threat_message as first_threat_message,
    scan_for_threats as scan_for_threats,
)
