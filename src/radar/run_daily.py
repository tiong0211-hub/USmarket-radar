"""Orchestrator: python -m radar.run_daily --date YYYY-MM-DD [--dry-run] [--no-llm] [--no-deploy] [--force]"""
import argparse
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from radar import config, market_calendar
from radar.assemble.assembler import assemble
from radar.analytics import sentiment as sentiment_module
from radar.assemble.validator import missing_required
from radar.collectors import calendar as calendar_collector
from radar.collectors import market, movers as movers_collector, news
from radar.llm import news_analyst, theme_mapper
from radar.llm.client import LLMClient, LLMFatal, TokenBudget
from radar.models import (
    CalendarSnapshot, MarketSnapshot, ModuleRun, MoversSnapshot, NewsAnalysis, NewsCollection, RunLog,
    ThemeMapping, UpcomingEvent,
)
from radar.render.renderer import render_site

log = logging.getLogger("radar.run_daily")


def _call_with_timeout(fn: Callable, timeout_s: float):
    """Daemon thread: a hung network call must not outlive the module budget or block exit."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"exceeded {timeout_s}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _module(name: str, fn: Callable, timeout_s: float, path: Path, status_of: Callable) -> tuple[BaseModel | None, ModuleRun]:
    """Runs one module and stores its output. The caller reads the result back from the file,
    because modules exchange data only as JSON. Errors log the type only: logs are public.
    """
    started = time.monotonic()

    def elapsed() -> float:
        return round(time.monotonic() - started, 1)

    try:
        result = _call_with_timeout(fn, timeout_s)
    except Exception as exc:
        log.error("%s failed: %s", name, type(exc).__name__)
        return None, ModuleRun(status="FAILED", duration_s=elapsed(), error=type(exc).__name__)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    loaded = type(result).model_validate_json(path.read_text(encoding="utf-8"))
    return loaded, ModuleRun(
        status=status_of(loaded), duration_s=elapsed(), notes=getattr(loaded, "notes", [])
    )


def _previous_calendar_events(data_dir: Path, run_date: date) -> list[UpcomingEvent]:
    """Events saved by the most recent earlier run (the feed only covers this week)."""
    earlier = [p for p in (data_dir / "raw").glob("*/calendar.json") if p.parent.name < str(run_date)]
    if not earlier:
        return []
    latest = max(earlier, key=lambda p: p.parent.name)
    return CalendarSnapshot.model_validate_json(latest.read_text(encoding="utf-8")).events


def run(args: argparse.Namespace, root: Path = config.ROOT) -> tuple[str, int]:
    settings = config.load("settings")
    symbols = config.load("symbols")
    feeds = config.load("feeds")
    models = config.load("models")
    theme_map = config.load("theme_map")
    base = root / ".dry-run" if args.dry_run else root
    data_dir, site_dir = base / "data", base / "site"
    started_at = datetime.now(UTC).replace(microsecond=0)
    run_date: date = args.date
    modules: dict[str, ModuleRun] = {}
    missing: list[str] = []
    llm_usage: dict[str, int] = {}
    us_session = None

    def finish(status: str, code: int) -> tuple[str, int]:
        log_path = data_dir / "logs" / f"{run_date}.run.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            RunLog(
                run_date=run_date, us_session=us_session, started_at=started_at,
                finished_at=datetime.now(UTC).replace(microsecond=0), run_status=status,
                modules=modules, indicators_missing=missing, llm_usage=llm_usage,
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
        log.info("run_status=%s", status)
        return status, code

    kr, us = settings["calendars"]["kr"], settings["calendars"]["us"]
    if not args.force and not market_calendar.is_session(kr, run_date):
        return finish("SKIPPED_HOLIDAY", 0)
    us_session = market_calendar.previous_session(us, run_date)
    closed = market_calendar.us_market_closed(us, run_date)
    close_of = lambda day: market_calendar.close_time(us, day)  # noqa: E731

    tz = ZoneInfo(settings["timezone_display"])
    hour, minute = map(int, settings["schedule"]["start_kst"].split(":"))
    cutoff = datetime(run_date.year, run_date.month, run_date.day, hour, minute, tzinfo=tz)
    cutoff = min(cutoff, datetime.now(UTC))
    news_start = close_of(market_calendar.previous_session(us, us_session))
    raw_dir = data_dir / "raw" / str(run_date)
    timeouts = settings["module_timeouts_s"]
    history = _previous_calendar_events(data_dir, run_date)

    def market_status(s: MarketSnapshot) -> str:
        return "SUCCEEDED" if not s.notes and not any(i.missing or i.stale for i in s.indicators) else "DEGRADED"

    # S1: collectors in parallel. Only M1 is critical, and M9 handles its absence.
    with ThreadPoolExecutor(4) as pool:
        m1 = pool.submit(
            _module, "M1", lambda: market.collect(symbols, settings, us_session, cutoff, close_of),
            settings["market"]["timeout_s"], raw_dir / "market.json", market_status,
        )
        m2 = pool.submit(
            _module, "M2", lambda: news.collect(feeds, news_start, cutoff),
            timeouts["M2"], raw_dir / "news.json",
            lambda c: "DEGRADED" if c.notes else "SUCCEEDED",
        )
        m3 = pool.submit(
            _module, "M3",
            lambda: calendar_collector.collect(
                feeds["calendar"], run_date, us_session, cutoff,
                settings["calendar"]["upcoming_window_kst"], tz, history,
                settings["calendar"]["upcoming_importance"],
            ),
            timeouts["M3"], raw_dir / "calendar.json", lambda _: "SUCCEEDED",
        )
        m4 = pool.submit(
            _module, "M4", lambda: movers_collector.collect(theme_map, symbols, settings, us_session),
            timeouts["M4"], raw_dir / "movers.json", lambda s: "DEGRADED" if s.notes else "SUCCEEDED",
        )
        snapshot, modules["M1"] = m1.result()
        collection, modules["M2"] = m2.result()
        calendar, modules["M3"] = m3.result()
        movers, modules["M4"] = m4.result()

    # S2: M5 is a rule on M1 data and takes microseconds, so it runs inline before M6.
    if snapshot is None:
        sentiment, modules["M5"] = None, ModuleRun(status="SKIPPED", notes=["M1 failed"])
    else:
        sentiment, modules["M5"] = _module(
            "M5", lambda: sentiment_module.compute(snapshot, settings["sentiment"]),
            timeouts["M5"], raw_dir / "sentiment.json", lambda _: "SUCCEEDED",
        )

    budget = TokenBudget(settings["llm_budget"])  # one daily budget across M6 and M7

    # News analysis. Without M2 there is nothing to analyze.
    analysis = None
    if collection is None:
        modules["M6"] = ModuleRun(status="SKIPPED", notes=["no articles: M2 failed"])
    elif args.no_llm:
        analysis = news_analyst.skipped(collection, "--no-llm")
        modules["M6"] = ModuleRun(status="SKIPPED", notes=analysis.notes)
    else:
        def analyze() -> NewsAnalysis:
            try:
                client = LLMClient(budget)
            except LLMFatal as exc:
                failed = news_analyst.skipped(collection, str(exc))
                return failed.model_copy(update={"total_batches": 1, "failed_batches": 1})
            return news_analyst.analyze(
                collection, calendar, us_session, models["news_analyst"], settings, client, models["news_picker"]
            )

        def analysis_status(a: NewsAnalysis) -> str:
            if a.total_batches and a.failed_batches == a.total_batches:
                return "FAILED"
            # Number-guard replacements are routine filtering (kept in notes), not a failed module.
            return "DEGRADED" if a.failed_batches else "SUCCEEDED"

        analysis, modules["M6"] = _module(
            "M6", analyze, timeouts["M6"], raw_dir / "news_analysis.json", analysis_status
        )
        if analysis is None:  # timeout: still show titles
            analysis = news_analyst.skipped(collection, "M6 timed out")

    # S3: theme mapping. Proposals need the LLM; the mapping itself is YAML only.
    mapping = None
    if movers is None:
        modules["M7"] = ModuleRun(status="SKIPPED", notes=["no movers: M4 failed"])
    else:
        key = settings["themes"]["benchmark_key"]
        benchmark = next((i for i in snapshot.indicators if i.key == key), None) if snapshot else None

        def map_themes() -> ThemeMapping:
            client = None
            if not args.no_llm:
                try:
                    client = LLMClient(budget)
                except LLMFatal:  # M6 already records the missing key
                    pass
            return theme_mapper.run(theme_map, movers, analysis, run_date, settings, client,
                                    models["theme_proposer"], file=not args.dry_run, benchmark=benchmark)

        mapping, modules["M7"] = _module(
            "M7", map_themes, timeouts["M7"], raw_dir / "themes.json",
            lambda m: "DEGRADED" if m.excluded or not m.listing_checked or not m.rule_ko else "SUCCEEDED",
        )
        if mapping and mapping.proposals:  # for human review only, see docs/orchestration_spec.md §8
            path = data_dir / "proposals" / f"{run_date}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([p.model_dump() for p in mapping.proposals], ensure_ascii=False, indent=2),
                            encoding="utf-8")
    llm_usage = dict(budget.used)

    try:
        report = assemble(symbols, run_date, us_session, closed, snapshot, modules, collection, calendar, analysis,
                          sentiment, mapping)
        missing = missing_required(report.indicators)
        daily = data_dir / "daily" / f"{run_date}.json"
        daily.parent.mkdir(parents=True, exist_ok=True)
        daily.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        modules["M9"] = ModuleRun(status="SUCCEEDED")
        status = report.run_status
    except Exception as exc:
        log.exception("M9 failed")
        modules["M9"] = ModuleRun(status="FAILED", error=type(exc).__name__)
        status = "FAILED"

    if status != "FAILED":
        try:
            render_site(data_dir, site_dir, settings)
            modules["M10"] = ModuleRun(status="SUCCEEDED")
            return finish(status, 0)
        except Exception as exc:
            log.exception("M10 failed")
            modules["M10"] = ModuleRun(status="FAILED", error=type(exc).__name__)

    # M9/M10 failed: keep the previous page, visibly marked as such.
    try:
        render_site(data_dir, site_dir, settings, keep_previous_for=run_date)
        return finish("FAILED", 0)
    except Exception:
        log.exception("could not rebuild the previous page")
        return finish("FAILED", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar.run_daily")
    parser.add_argument("--date", type=date.fromisoformat, help="KST date, default today")
    parser.add_argument("--dry-run", action="store_true", help="write under .dry-run/ instead of data/ and site/")
    parser.add_argument("--no-llm", action="store_true", help="skip LLM modules: news cards show titles only")
    parser.add_argument("--no-deploy", action="store_true", help="accepted for parity: deploy is a workflow job")
    parser.add_argument("--force", action="store_true", help="ignore the KRX holiday check")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    args.date = args.date or datetime.now(ZoneInfo(config.load("settings")["timezone_display"])).date()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    status, code = run(args)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"run_status={status}\nrun_date={args.date}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
