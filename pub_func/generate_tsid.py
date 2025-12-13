from datetime import datetime, timedelta

def generate_tsid(days_offset: int = 0) -> str:
    """
    生成时间戳 ID: YYYYMMDDHHmmss（如 202602260705）
    同时作为可读时间和唯一标识

    Args:
        days_offset: 天数偏移量，0表示当前时间，-7表示7天前， 7表示7天后
    """
    now: datetime = datetime.now() + timedelta(days=days_offset)
    year = now.year
    month = str(now.month).zfill(2)
    day = str(now.day).zfill(2)
    hour = str(now.hour).zfill(2)
    minute = str(now.minute).zfill(2)
    second = str(now.second).zfill(2)

    return f"{year}{month}{day}{hour}{minute}{second}"