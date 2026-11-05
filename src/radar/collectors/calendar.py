"""M3: Forex Factory weekly JSON into released and upcoming events. Never estimates actuals."""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from radar.models import CalendarSnapshot, ReleasedEvent, UpcomingEvent
from radar.retry import with_retry

ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MorningRadar/1.0)"}


def fetch_events(url: str) -> list[dict]:
    response = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def parse_events(raw: list[dict], cfg: dict) -> list[UpcomingEvent]:
    """USD High/Medium events. Korean names come from config; unknown titles stay in English."""
    rule = cfg["primary"]["filter"]
    names = cfg["event_names"]
    events = []
    for item in raw:
        if item.get("country") != rule["country"] or item.get("impact") not in rule["impact"]:
            continue
        name = names.get(item["title"], {})
        events.append(UpcomingEvent(
            time_utc=datetime.fromisoformat(item["date"]).astimezone(UTC),
            event=item["title"],
            event_ko=name.get("ko", item["title"]),
            importance=item["impact"].lower(),
            forecast=item.get("forecast") or None,
            previous=item.get("previous") or None,
            tag_ko=name.get("tag"),
        ))
    return events


def upcoming_window(run_date: date, window_kst: list[str], tz: ZoneInfo) -> tuple[datetime, datetime]:
    (start_h, start_m), (end_h, end_m) = (map(int, t.split(":")) for t in window_kst)
    start = datetime(run_date.year, run_date.month, run_date.day, start_h, start_m, tzinfo=tz)
    end = start.replace(hour=end_h, minute=end_m)
    return start, end if end > start else end + timedelta(days=1)


def split_events(
    events: list[UpcomingEvent], run_date: date, us_session: date, cutoff: datetime,
    window_kst: list[str], tz: ZoneInfo,
) -> CalendarSnapshot:
    start = datetime(us_session.year, us_session.month, us_session.day, tzinfo=ET)
    up_start, up_end = upcoming_window(run_date, window_kst, tz)
    ordered = sorted(events, key=lambda e: (e.time_utc, e.event))
    return CalendarSnapshot(
        status="ok",
        events=ordered,
        released=[ReleasedEvent(**e.model_dump()) for e in ordered if start <= e.time_utc <= cutoff],
        upcoming=[e for e in ordered if up_start <= e.time_utc < up_end],
    )


def collect(
    cfg: dict, run_date: date, us_session: date, cutoff: datetime, window_kst: list[str], tz: ZoneInfo,
    history: list[UpcomingEvent],
) -> CalendarSnapshot:
    """history: events saved by the previous run. The feed only covers this week,
    so on Monday it no longer holds Friday's releases.
    """
    raw = with_retry(lambda: fetch_events(cfg["primary"]["url"]), cfg["fetch_retry_backoff_s"])
    current = parse_events(raw, cfg)
    known = {(e.event, e.time_utc) for e in current}
    merged = current + [e for e in history if (e.event, e.time_utc) not in known]
    return split_events(merged, run_date, us_session, cutoff, window_kst, tz)
