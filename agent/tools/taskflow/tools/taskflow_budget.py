"""taskflow_budget: set and query the token/cost budget for a task flow."""

from langchain_core.tools import tool

from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, terminal_error


@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str:
    """Set or query the token/cost budget for a task flow.

    Actions:
    - 'query': report total_tokens, total_cost, the budget, tokens remaining
      and a status of ok / WARNING (>= 80% used) / EXCEEDED (>= 100% used).
    - 'set': set token_budget (a positive integer); pass expected_revision to
      fail fast on concurrent writers. Rejected for terminal flows.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)

    action = (action or "query").strip().lower()

    if action == "query":
        total_tokens = int(flow["total_tokens"])
        total_cost = float(flow["total_cost"])
        budget = int(flow["token_budget"])
        remaining = budget - total_tokens if budget > 0 else -1
        pct = (total_tokens / budget * 100) if budget > 0 else -1.0

        from config.features import MODEL_PRICING

        warn_threshold = MODEL_PRICING["budget_warn_threshold"]
        status = "ok"
        if budget > 0:
            if total_tokens >= budget:
                status = "EXCEEDED"
            elif total_tokens / budget >= warn_threshold:
                status = "WARNING"

        return (
            f"Budget: flow_id={flow_id}\n"
            f"  tokens: {total_tokens:,}"
            + (f" / {budget:,} ({pct:.1f}%)" if budget > 0 else " (no budget set)")
            + f"\n  cost: ${total_cost:.4f}"
            + (f"\n  remaining: {remaining:,} tokens" if budget > 0 else "")
            + f"\n  status: {status}"
        )

    if action == "set":
        if token_budget is None or token_budget <= 0:
            return "Error: token_budget must be a positive integer for 'set' action."
        if is_terminal(flow["status"]):
            return terminal_error(flow_id, flow["status"])

        revision = (
            int(expected_revision)
            if expected_revision is not None
            else int(flow["expected_revision"])
        )

        try:
            updated = await store_sqlite.update_flow(
                flow_id, revision, token_budget=int(token_budget)
            )
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

        return (
            f"Budget set: flow_id={flow_id}, token_budget={token_budget:,}, "
            f"revision={updated['expected_revision']}"
        )

    return f"Error: unknown action '{action}'. Use 'query' or 'set'."
