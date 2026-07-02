from datetime import datetime, timedelta

def generate_tsid(days_offset: int = 0) -> str:
    """
    Generate a timestamp ID: YYYYMMDDHHmmss (e.g. 202602260705).
    Serves as both a human-readable timestamp and a unique identifier.

    Args:
        days_offset: Day offset. 0 = now, -7 = 7 days ago, 7 = 7 days ahead.
    """
    now: datetime = datetime.now() + timedelta(days=days_offset)
    year = now.year
    month = str(now.month).zfill(2)
    day = str(now.day).zfill(2)
    hour = str(now.hour).zfill(2)
    minute = str(now.minute).zfill(2)
    second = str(now.second).zfill(2)

    return f"{year}{month}{day}{hour}{minute}{second}"