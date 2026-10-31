"""Live-stream safety filter (keyword rules + optional LLM moderation).

Import surface: the three middlewares the main chain registers, the rule
scanner the HTTP endpoints drive, and the streaming guard the stream path
uses. ``SAFETY_FILTER["enabled"]`` is false by default, so importing this
package costs nothing until the 直播安全 switch is on.
"""

from .config import llm_moderation_active, safety_enabled, safety_filter_config
from .middlewares import InputSafetyFilter, LLMModerationMiddleware, OutputSafetyFilter
from .notify import notify_safety, recent_events, record_event
from .rules import FilterHit, apply_replacements, rules_snapshot, scan, validate_rules, write_rules
from .stream_guard import StreamTextGuard

__all__ = [
    "FilterHit",
    "InputSafetyFilter",
    "LLMModerationMiddleware",
    "OutputSafetyFilter",
    "StreamTextGuard",
    "apply_replacements",
    "llm_moderation_active",
    "notify_safety",
    "recent_events",
    "record_event",
    "rules_snapshot",
    "safety_enabled",
    "safety_filter_config",
    "scan",
    "validate_rules",
    "write_rules",
]
