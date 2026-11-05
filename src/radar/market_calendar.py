"""KR/US session lookups. All dates are exchange session labels."""
from datetime import UTC, date, datetime, timedelta

import exchange_calendars as xc
import pandas as pd


def is_session(calendar: str, day: date) -> bool:
    return bool(xc.get_calendar(calendar).is_session(pd.Timestamp(day)))


def previous_session(calendar: str, day: date) -> date:
    """Most recent session strictly before `day`."""
    label = pd.Timestamp(day - timedelta(days=1))
    return xc.get_calendar(calendar).date_to_session(label, direction="previous").date()


def us_market_closed(calendar: str, run_date: date) -> bool:
    """True when the last weekday before run_date was not a session (a holiday, not a weekend)."""
    day = run_date - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return not is_session(calendar, day)


def close_time(calendar: str, day: date) -> datetime:
    """Session close in UTC; midnight UTC when `day` is not a session."""
    cal = xc.get_calendar(calendar)
    if cal.is_session(pd.Timestamp(day)):
        return cal.session_close(pd.Timestamp(day)).to_pydatetime()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
