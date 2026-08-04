from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")


def trading_session_date(timestamp: datetime, rollover_hour_et: int = 18) -> date:
    local = timestamp.astimezone(NEW_YORK)
    hours_to_midnight = 24 - rollover_hour_et
    return (local + timedelta(hours=hours_to_midnight)).date()
