"""Per-task token accounting for the external benchmark suites.

The harness's ``_session_tokens`` answers ``{"input": n, "output": n}`` for one
session; this module turns that into the plan's ``tokens.json`` (with an
optional cost column from a price map) and the per-role breakdown when a suite
records subagent sessions. Pricing is opt-in: ``SHERRY_EVAL_TOKEN_PRICES``
points at a JSON file of ``{"model": {"input": usd_per_1k, "output": usd_per_1k}}``;
without it ``cost_usd`` stays absent and nothing else changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["TokenLedger", "load_price_map", "task_tokens_payload"]


def load_price_map() -> dict[str, dict[str, float]]:
    """The optional SHERRY_EVAL_TOKEN_PRICES map (absent = no cost column)."""
    raw = os.environ.get("SHERRY_EVAL_TOKEN_PRICES", "").strip()
    if not raw or not Path(raw).is_file():
        return {}
    try:
        payload = json.loads(Path(raw).read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cost(model: str, tokens: dict[str, Any], prices: dict[str, dict[str, float]]) -> float | None:
    entry = prices.get(model or "")
    if not entry:
        return None
    input_price = float(entry.get("input") or 0.0)
    output_price = float(entry.get("output") or 0.0)
    return round(
        int(tokens.get("input") or 0) / 1000 * input_price
        + int(tokens.get("output") or 0) / 1000 * output_price,
        6,
    )


class TokenLedger:
    """Accumulates one task's token usage across the sessions it spawned.

    ``record(session_id, tokens, role=...)`` — the main session records first;
    every spawned child's session records under ``"subagents"`` with its role.
    ``payload()`` renders the plan's ``tokens.json`` shape.
    """

    def __init__(self, task_id: str, *, model: str = "") -> None:
        """One task's ledger; *model* selects the price-map entry."""
        self.task_id = task_id
        self.model = model
        self.prices = load_price_map()
        self._sessions: list[dict[str, Any]] = []

    def record(
        self,
        session_id: str,
        tokens: dict[str, Any] | None,
        *,
        role: str = "main_agent",
    ) -> None:
        """Fold one session's usage in under its role."""
        usage = {
            "input": int((tokens or {}).get("input") or 0),
            "output": int((tokens or {}).get("output") or 0),
        }
        self._sessions.append(
            {
                "session_id": session_id,
                "role": role,
                "input": usage["input"],
                "output": usage["output"],
            }
        )

    def payload(self) -> dict[str, Any]:
        """The plan's tokens.json shape for this task."""
        main = next((s for s in self._sessions if s["role"] == "main_agent"), None)
        subagents = [s for s in self._sessions if s["role"] != "main_agent"]
        total_input = sum(s["input"] for s in self._sessions)
        total_output = sum(s["output"] for s in self._sessions)
        tokens = {"input": total_input, "output": total_output}
        cost = _cost(self.model, tokens, self.prices)
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "total_input": total_input,
            "total_output": total_output,
            "total_tokens": total_input + total_output,
            "sessions": len(self._sessions),
            "main_agent": main or {"input": 0, "output": 0},
            "subagents": [
                {"role": s["role"], "input": s["input"], "output": s["output"]} for s in subagents
            ],
        }
        if cost is not None:
            payload["cost_usd"] = cost
        return payload


def task_tokens_payload(
    task_id: str, tokens: dict[str, Any] | None, *, model: str = ""
) -> dict[str, Any]:
    """One-session shorthand: the main session's usage rendered as tokens.json."""
    ledger = TokenLedger(task_id, model=model)
    ledger.record(session_id=task_id, tokens=tokens)
    return ledger.payload()
