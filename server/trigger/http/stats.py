"""Token-usage statistics HTTP endpoint.

Aggregates AI-message token usage from the SQLite ``messages`` table, grouped
by calendar day, so the client's statistics page can render a bar chart
(today / last 7 days / last 30 days).

Endpoint:
    GET /stats/tokens?range=week
        -> {"range": "week", "days": [{"date": "MM-DD", "by_model": [...]}]}
"""
from datetime import date, datetime, timedelta

from loguru import logger

from server.trigger.core import app
from context_engine.store.db import get_db


def _parse_range(raw):
    """Normalize the ``range`` query param to one of day/week/month.

    Unsupported or missing values default to ``week``.
    """
    if raw in ("day", "week", "month"):
        return raw
    return "week"


def _range_bounds(range_key, today):
    """Return (start_date, end_date) inclusive calendar-day boundaries.

    ``today`` is a ``datetime.date``. ``end_date`` is always ``today``.
    """
    if range_key == "day":
        return today, today
    if range_key == "month":
        return today - timedelta(days=29), today
    # week: last 7 calendar days ending today (today-6 .. today)
    return today - timedelta(days=6), today


@app.get("/stats/tokens")
async def stats_tokens_handler(request):
    """Return per-day AI token usage aggregated by model.

    Query params:
        range (str): one of ``day`` (today), ``week`` (last 7 days inclusive
            of today), ``month`` (last 30 days inclusive of today). Default
            ``week``; unsupported values fall back to ``week``.

    Response:
        {
            "range": "week",
            "days": [
                {
                    "date": "MM-DD",
                    "by_model": [
                        {"model_name": "...", "input_tokens": int, "output_tokens": int}
                    ]
                }
            ]
        }
    """
    range_key = _parse_range(request.query_params.get("range", "week"))
    today = date.today()
    start_date, end_date = _range_bounds(range_key, today)

    try:
        db = get_db()
        rows = db.execute(
            """
            SELECT timestamp, model_name, input_tokens, output_tokens
            FROM messages
            WHERE role = 'ai'
              AND input_tokens IS NOT NULL
            """
        ).fetchall()

        # Aggregate per (calendar day, model_name).
        day_model = {}  # date -> {model_name: [input_tokens, output_tokens]}
        for row in rows:
            ts = row["timestamp"]
            model_name = row["model_name"]
            if not model_name:
                continue
            try:
                parsed = datetime.strptime(ts, "%Y%m%d%H%M%S").date()
            except (TypeError, ValueError):
                # Defensively skip rows whose timestamp cannot be parsed.
                continue
            if not (start_date <= parsed <= end_date):
                continue

            bucket = day_model.setdefault(parsed, {})
            entry = bucket.setdefault(model_name, [0, 0])
            entry[0] += row["input_tokens"] or 0
            entry[1] += row["output_tokens"] or 0

        # Build the ascending per-day response, sorted by total tokens desc.
        days = []
        cursor = start_date
        while cursor <= end_date:
            bucket = day_model.get(cursor, {})
            by_model = [
                {
                    "model_name": model_name,
                    "input_tokens": tokens[0],
                    "output_tokens": tokens[1],
                }
                for model_name, tokens in bucket.items()
            ]
            by_model.sort(
                key=lambda m: m["input_tokens"] + m["output_tokens"], reverse=True
            )
            days.append({"date": cursor.strftime("%m-%d"), "by_model": by_model})
            cursor += timedelta(days=1)

        logger.info(
            "Served token stats: range=%s, start=%s, end=%s, days=%d",
            range_key,
            start_date,
            end_date,
            len(days),
        )
        return {"range": range_key, "days": days}
    except Exception as e:  # noqa: BLE001 - surface any backend failure cleanly
        logger.exception("Token stats request failed")
        return {"error": str(e), "days": []}
