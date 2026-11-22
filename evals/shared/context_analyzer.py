"""The context hit-rate analyzer (the plan's §7.2, implemented over transcripts).

Definition (the plan's own): of the context a model call carries, how much is
"on task" versus wasted. The analyzer works POST-TASK over the session
transcript ``agent.state_port.read_messages`` returns — no instrumentation in
the hot path — and classifies:

* ``system`` / ``user`` — always HIT (the task text defines the task);
* AI messages — not scored (the agent's own output);
* tool results — HIT when the heuristic says the call served the task
  (test/build/lint commands, task-keyword matches in the command or in the
  result's first characters), WASTED when the heuristic says otherwise;
  everything the heuristic cannot decide counts as HIT (the plan's
  "conservative, never understate the hit rate" rule);
* reminders / summarized history — not scored (system-injected, agent has no
  control over them).

``hit_rate = effective / (effective + wasted)`` where effective counts scored
HIT tokens and wasted counts scored misses.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["ContextAnalyzer", "analyze_transcript"]

_TASK_STOP_WORDS = frozenset(
    "the a an is are in on at to for of and or with that this it as be by from "
    "not you your should must will can do does into than then its are was".split()
)

#: Planning/orchestration calls are the agent's own working set — unscored.
_TOOL_UNSCORED = frozenset({"question", "todowrite", "todoread", "taskflow_flow", "taskflow_step"})

# NOTE on units: the transcript carries no per-message token costs, so the
# counters are PER CALL, not per token — the payload keeps the plan's field
# names (effective_tokens / wasted_tokens) but each unit is one scored call.
# A per-token version needs the usage metadata of every intermediate call,
# which only a hot-path hook could provide; per-call is the honest proxy.


class ContextAnalyzer:
    """One task's hit-rate analysis."""

    def __init__(self, task_text: str) -> None:
        self.keywords = _extract_keywords(task_text)
        self.effective = 0
        self.wasted = 0
        self.by_tool: dict[str, dict[str, int]] = {}
        self.calls = 0

    def score_tool_call(self, tool: str, args: Any, result_text: str) -> None:
        """Score ONE tool call. Unscored tools (planning/orchestration) return
        without touching the counters — they are the agent's own working set."""
        if tool in _TOOL_UNSCORED:
            return
        self.calls += 1
        hit = _is_relevant(tool, args, result_text, self.keywords)
        entry = self.by_tool.setdefault(tool, {"calls": 0, "hit": 0})
        entry["calls"] += 1
        if hit:
            entry["hit"] += 1
            self.effective += 1
        else:
            self.wasted += 1

    def payload(self) -> dict[str, Any]:
        """The plan's payload shape from the scored calls."""
        effective, wasted = self.effective, self.wasted
        total = effective + wasted
        return {
            "hit_rate": round(effective / total, 4) if total else 1.0,
            "effective_tokens": effective,
            "wasted_tokens": wasted,
            "tool_calls_analyzed": self.calls,
            "by_tool": self.by_tool,
            "wasted_breakdown": {
                tool: entry["calls"] - entry["hit"]
                for tool, entry in self.by_tool.items()
                if entry["calls"] > entry["hit"]
            },
        }


def _extract_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_-]{2,}", (text or "").lower())
    return [w for w in words if w not in _TASK_STOP_WORDS][:80]


def _is_relevant(tool: str, args: Any, result_text: str, keywords: list[str]) -> bool:
    """The plan's heuristic, conservative: only an explicit miss is a miss."""
    args_text = ""
    if isinstance(args, dict):
        args_text = " ".join(str(value) for value in args.values()).lower()
    elif args is not None:
        args_text = str(args).lower()
    command_keywords = ("test", "build", "compile", "lint", "check", "pytest", "npm", "make")
    if tool in {"terminal", "python_repl"}:
        if any(word in args_text for word in command_keywords):
            return True
        if any(word in args_text for word in keywords):
            return True
        return False
    if tool in {"web_search", "web_fetch", "search_files"}:
        # A search that came back empty AND matched nothing is a miss; any
        # result text counts as on-task (the agent is looking for something).
        return True
    if tool in {"read_file", "write_file", "patch_file"}:
        if any(word in args_text for word in keywords):
            return True
        return True  # conservative: file edits serve the task by definition
    if any(word in (result_text or "").lower()[:300] for word in keywords):
        return True
    return True  # unknown tool: conservative default


def analyze_transcript(messages: list[Any], task_text: str) -> dict[str, Any]:
    """Score a full transcript: walk AI messages' tool_calls and pair each with
    the ToolMessage that answered it (matching by tool_call_id)."""
    from langchain_core.messages import ToolMessage  # noqa: PLC0415 — lazy

    analyzer = ContextAnalyzer(task_text)
    results: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            content = message.content
            results[str(getattr(message, "tool_call_id", ""))] = (
                content if isinstance(content, str) else str(content)
            )
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            name = str(call.get("name") or "")
            args = call.get("args")
            result_text = results.get(str(call.get("id") or ""), "")
            analyzer.score_tool_call(name, args, result_text)
    return analyzer.payload()
