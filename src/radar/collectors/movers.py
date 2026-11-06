"""M4: session change of watched US stocks and sector ETFs. No theme interpretation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from radar.collectors import market
from radar.collectors.market import Bars, NoData
from radar.models import Mover, MoversSnapshot
from radar.retry import with_retry


def watch_list(theme_map: dict, symbols: dict) -> list[dict]:
    """theme_map tickers first, then the extra watchlist and sector ETFs, without duplicates."""
    entries = [{"symbol": t["us_ticker"], "name": t["name"], "kind": "stock"} for t in theme_map["themes"]]
    entries += [{**w, "kind": "stock"} for w in symbols.get("watchlist", [])]
    entries += [{"symbol": e["symbol"], "name": e["label"], "kind": "etf"} for e in symbols.get("sector_etfs", [])]
    unique: dict[str, dict] = {}
    for entry in entries:
        unique.setdefault(entry["symbol"], entry)
    return list(unique.values())


def build_mover(entry: dict, daily: Bars, us_session: date) -> Mover:
    closes = [(ts.date(), v) for ts, v in daily if ts.date() <= us_session]
    if len(closes) < 2 or closes[-1][0] != us_session:
        raise NoData(f"{entry['symbol']}: no close for {us_session}")
    (_, close), previous = closes[-1], closes[-2][1]
    return Mover(symbol=entry["symbol"], name=entry["name"], kind=entry["kind"],
                 close=round(close, 2), change_pct=round((close / previous - 1) * 100, 2))


def collect(theme_map: dict, symbols: dict, settings: dict, us_session: date) -> MoversSnapshot:
    cfg = settings["movers"]
    start, end = us_session - timedelta(days=10), us_session + timedelta(days=1)

    def one(entry: dict) -> Mover | str:
        try:
            daily = with_retry(lambda: market.fetch_bars(entry["symbol"], "1d", start, end), cfg["retry_backoff_s"])
            return build_mover(entry, daily, us_session)
        except Exception as exc:  # a failed ticker is only left out
            return f"{entry['symbol']}: {type(exc).__name__}"

    with ThreadPoolExecutor(cfg["workers"]) as pool:
        results = list(pool.map(one, watch_list(theme_map, symbols)))
    movers = [r for r in results if isinstance(r, Mover)]
    if not movers:
        raise RuntimeError("no movers collected")
    return MoversSnapshot(movers=movers, notes=[r for r in results if isinstance(r, str)])
