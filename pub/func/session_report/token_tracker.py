"""Per-session token accounting for the session run report.

``read_messages`` answers a session's transcript; this module folds the
``usage_metadata`` of its AIMessages into a main + per-role child breakdown.
Pricing is opt-in: ``SHERRY_SESSION_REPORT_TOKEN_PRICES`` points at a JSON file
of ``{"model": {"input": usd_per_1k, "output": usd_per_1k}}``; without it no
cost field is emitted. The legacy ``SHERRY_EVAL_TOKEN_PRICES`` is also honored
so existing eval setups keep working without a config change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["TokenLedger", "load_price_map", "session_tokens", "task_tokens_payload"]

_PRICE_ENV_VARS = (
    "SHERRY_SESSION_REPORT_TOKEN_PRICES",
    "SHERRY_EVAL_TOKEN_PRICES",
)


def load_price_map() -> dict[str, dict[str, float]]:
    """The optional price map (absent = no cost column)."""
    for name in _PRICE_ENV_VARS:
        raw = os.environ.get(name, "").strip()
        if raw and Path(raw).is_file():
            try:
                payload = json.loads(Path(raw).read_text(encoding="utf-8"))
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


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
    """Accumulates one session's token usage across the sessions it spawned."""

    def __init__(self, session_id: str, *, model: str = "") -> None:
        self.session_id = session_id
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
        """The report's tokens shape for this session."""
        main = next((s for s in self._sessions if s["role"] == "main_agent"), None)
        subagents = [s for s in self._sessions if s["role"] != "main_agent"]
        total_input = sum(s["input"] for s in self._sessions)
        total_output = sum(s["output"] for s in self._sessions)
        tokens = {"input": total_input, "output": total_output}
        cost = _cost(self.model, tokens, self.prices)
        payload: dict[str, Any] = {
            "main": {
                "input": (main or {}).get("input", 0),
                "output": (main or {}).get("output", 0),
                "total": (main or {}).get("input", 0) + (main or {}).get("output", 0),
            },
            "children": [
                {
                    "role": s["role"],
                    "input": s["input"],
                    "output": s["output"],
                    "total": s["input"] + s["output"],
                }
                for s in subagents
            ],
            "grand_total": total_input + total_output,
            "sessions": len(self._sessions),
        }
        if cost is not None:
            payload["estimated_cost_usd"] = cost
        return payload


def session_tokens(messages: list[Any]) -> dict[str, int]:
    """Sum ``usage_metadata`` over a transcript (the eval `_session_tokens` shape)."""
    input_tokens = output_tokens = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    return {"input": input_tokens, "output": output_tokens}


def task_tokens_payload(
    task_id: str, tokens: dict[str, Any] | None, *, model: str = ""
) -> dict[str, Any]:
    """One-session shorthand retained for eval compatibility."""
    ledger = TokenLedger(task_id, model=model)
    ledger.record(session_id=task_id, tokens=tokens)
    return ledger.payload()
