"""M1: collect the indicator cards. No interpretation and no gap filling."""
import csv
import io
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import httpx
import yfinance as yf

from radar.models import Indicator, MarketSnapshot
from radar.retry import with_retry

log = logging.getLogger(__name__)

Bars = list[tuple[datetime, float]]
INTRADAY_GROUPS = {"futures", "fx"}  # quoted after the US close


class NoData(Exception):
    pass


def fetch_bars(symbol: str, interval: str, start: date, end: date) -> Bars:
    """Closes with timestamps in exchange time, so .date() is the session label."""
    frame = yf.Ticker(symbol).history(
        start=start, end=end, interval=interval, auto_adjust=False, timeout=15
    )
    closes = frame["Close"].dropna() if "Close" in frame else []
    if len(closes) == 0:
        raise NoData(f"{symbol} {interval}: empty response")
    return [(ts.to_pydatetime(), float(value)) for ts, value in closes.items()]


def fetch_contract(symbol: str) -> str | None:
    expiry = yf.Ticker(symbol).info.get("expireIsoDate")
    return expiry[:7] if expiry else None


def fetch_fred(url: str) -> list[tuple[date, float]]:
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return parse_fred_csv(response.text)


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    rows = list(csv.reader(io.StringIO(text)))[1:]
    return [(date.fromisoformat(d), float(v)) for d, v in rows if v not in ("", ".")]


def front_month(day: date) -> str:
    """Quarterly contract whose third-Friday expiry is on or after `day`."""
    year, month = day.year, day.month
    while True:
        if month % 3 == 0:
            first = date(year, month, 1)
            third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
            if third_friday >= day:
                return f"{year}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _indicator(spec: dict, **values) -> Indicator:
    fields = {k: spec.get(k) for k in ("key", "label", "kind", "symbol", "group", "unit")}
    return Indicator(
        **fields,
        required=spec["required"],
        source=values.pop("source", spec["source"]),
        description_ko=spec.get("description_ko", ""),
        **values,
    )


def _changes(spec: dict, value: float, previous: float) -> dict:
    change = value - previous
    if spec["group"] == "rates":
        return {"change": round(change, 3), "change_bp": round(change * 100)}
    return {"change": round(change, 2), "change_pct": round(change / previous * 100, 2)}


def _check_range(spec: dict, value: float) -> None:
    low, high = spec.get("valid_range", (float("-inf"), float("inf")))
    if not low <= value <= high:
        raise NoData(f"{spec['key']}: {value} outside valid_range")


def build_indicator(
    spec: dict,
    daily: Bars,
    intraday: Bars,
    us_session: date,
    cutoff: datetime,
    close_of: Callable[[date], datetime],
    contract: str | None = None,
    spark_days: int = 5,
) -> Indicator:
    """Spot: us_session close vs the previous close.

    Futures/FX: last quote before cutoff vs the last close before us_session,
    so the change spans the whole US session in every season.
    """
    closes = [(ts.date(), value) for ts, value in daily if ts.date() <= us_session]
    if spec["group"] in INTRADAY_GROUPS:
        before = [value for day, value in closes if day < us_session]
        quotes = [(ts, value) for ts, value in intraday if ts <= cutoff]
        if quotes:
            as_of, value = quotes[-1][0].astimezone(UTC), quotes[-1][1]
        elif closes:
            as_of, value = min(close_of(closes[-1][0]), cutoff), closes[-1][1]
        else:
            raise NoData(f"{spec['key']}: no quote before cutoff")
        if not before:
            raise NoData(f"{spec['key']}: no reference close")
        previous = before[-1]
        spark = [*before[-(spark_days - 1):], value]
        stale = as_of.date() < us_session
    else:
        if len(closes) < 2:
            raise NoData(f"{spec['key']}: fewer than 2 closes")
        (last_day, value), previous = closes[-1], closes[-2][1]
        as_of = min(close_of(last_day), cutoff)  # a run before the close sees a live price
        spark = [v for _, v in closes[-spark_days:]]
        stale = last_day != us_session
    _check_range(spec, value)
    return _indicator(
        spec,
        value=round(value, 2),
        contract=contract,
        spark=[round(v, 2) for v in spark],
        as_of=as_of,
        stale=stale,
        **_changes(spec, value, previous),
    )


def build_from_fred(spec, rows, us_session, close_of, spark_days: int = 5) -> Indicator:
    rows = [(d, v) for d, v in rows if d <= us_session]
    if len(rows) < 2:
        raise NoData(f"{spec['key']}: fewer than 2 FRED rows")
    (last_day, value), previous = rows[-1], rows[-2][1]
    _check_range(spec, value)
    return _indicator(
        spec,
        value=round(value, 2),
        spark=[round(v, 2) for _, v in rows[-spark_days:]],
        as_of=close_of(last_day),
        stale=True,
        source=spec["fallback"]["source"],
        **_changes(spec, value, previous),
    )


def collect(symbols: dict, settings: dict, us_session: date, cutoff: datetime, close_of) -> MarketSnapshot:
    cfg = settings["market"]
    spark_days = symbols["spark_days"]
    start = us_session - timedelta(days=spark_days * 3 + 5)
    end = cutoff.date() + timedelta(days=2)
    intraday_ok = (datetime.now(UTC) - cutoff).days <= cfg["intraday_max_age_days"]

    def retry(fn):
        return with_retry(fn, cfg["retry_backoff_s"], cfg["retry_jitter_s"])

    def one(spec: dict) -> tuple[Indicator, list[str]]:
        notes: list[str] = []
        symbol = spec["symbol"]
        try:
            daily = retry(lambda: fetch_bars(symbol, "1d", start, end))
            intraday: Bars = []
            if spec["group"] in INTRADAY_GROUPS and intraday_ok:
                try:
                    intraday = retry(lambda: fetch_bars(symbol, "5m", cutoff.date() - timedelta(days=5), end))
                except Exception as exc:
                    notes.append(f"{spec['key']}: intraday unavailable ({type(exc).__name__}), using daily close")
            contract = None
            if spec["group"] == "futures":
                try:
                    contract = fetch_contract(symbol)
                except Exception as exc:
                    log.warning("%s: contract lookup failed: %s", symbol, type(exc).__name__)
                contract = contract or front_month(cutoff.date())
            indicator = build_indicator(spec, daily, intraday, us_session, cutoff, close_of, contract, spark_days)
            return indicator, notes
        except Exception as exc:
            notes.append(f"{spec['key']}: {type(exc).__name__}: {exc}")
        if "fallback" in spec:
            try:
                rows = retry(lambda: fetch_fred(spec["fallback"]["url"]))
                notes.append(f"{spec['key']}: using fallback {spec['fallback']['source']}")
                return build_from_fred(spec, rows, us_session, close_of, spark_days), notes
            except Exception as exc:
                notes.append(f"{spec['key']}: fallback failed: {type(exc).__name__}: {exc}")
        return _indicator(spec, missing=True), notes

    with ThreadPoolExecutor(cfg["workers"]) as pool:
        results = list(pool.map(one, symbols["indicators"]))
    return MarketSnapshot(
        indicators=[indicator for indicator, _ in results],
        notes=[note for _, notes in results for note in notes],
    )
