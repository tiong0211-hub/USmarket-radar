"""Orchestrator: python -m radar.run_daily --date YYYY-MM-DD [--dry-run] [--no-llm] [--no-deploy] [--force]"""
import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from radar import config, market_calendar
from radar.assemble.assembler import assemble
from radar.assemble.validator import missing_required
from radar.collectors import market
from radar.models import MarketSnapshot, ModuleRun, RunLog
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


def _market_module(symbols, settings, us_session, cutoff, us_calendar, raw_dir: Path) -> tuple[MarketSnapshot | None, ModuleRun]:
    started = time.monotonic()

    def elapsed() -> float:
        return round(time.monotonic() - started, 1)

    try:
        snapshot = _call_with_timeout(
            lambda: market.collect(
                symbols, settings, us_session, cutoff,
                lambda day: market_calendar.close_time(us_calendar, day),
            ),
            settings["market"]["timeout_s"],
        )
    except Exception as exc:
        log.exception("M1 failed")
        return None, ModuleRun(status="FAILED", duration_s=elapsed(), error=f"{type(exc).__name__}: {exc}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "market.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    # Read back from the file: modules exchange data only as JSON files.
    snapshot = MarketSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    clean = not snapshot.notes and not any(i.missing or i.stale for i in snapshot.indicators)
    return snapshot, ModuleRun(
        status="SUCCEEDED" if clean else "DEGRADED", duration_s=elapsed(), notes=snapshot.notes
    )


def run(args: argparse.Namespace, root: Path = config.ROOT) -> tuple[str, int]:
    settings = config.load("settings")
    symbols = config.load("symbols")
    base = root / ".dry-run" if args.dry_run else root
    data_dir, site_dir = base / "data", base / "site"
    started_at = datetime.now(UTC).replace(microsecond=0)
    run_date: date = args.date
    modules: dict[str, ModuleRun] = {}
    missing: list[str] = []
    us_session = None

    def finish(status: str, code: int) -> tuple[str, int]:
        log_path = data_dir / "logs" / f"{run_date}.run.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            RunLog(
                run_date=run_date, us_session=us_session, started_at=started_at,
                finished_at=datetime.now(UTC).replace(microsecond=0), run_status=status,
                modules=modules, indicators_missing=missing,
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

    hour, minute = map(int, settings["schedule"]["start_kst"].split(":"))
    cutoff = datetime(
        run_date.year, run_date.month, run_date.day, hour, minute,
        tzinfo=ZoneInfo(settings["timezone_display"]),
    )
    cutoff = min(cutoff, datetime.now(UTC))

    snapshot, modules["M1"] = _market_module(
        symbols, settings, us_session, cutoff, us, data_dir / "raw" / str(run_date)
    )

    try:
        report = assemble(symbols, run_date, us_session, closed, snapshot, modules)
        missing = missing_required(report.indicators)
        daily = data_dir / "daily" / f"{run_date}.json"
        daily.parent.mkdir(parents=True, exist_ok=True)
        daily.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        modules["M9"] = ModuleRun(status="SUCCEEDED")
        status = report.run_status
    except Exception as exc:
        log.exception("M9 failed")
        modules["M9"] = ModuleRun(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        status = "FAILED"

    if status != "FAILED":
        try:
            render_site(data_dir, site_dir, settings)
            modules["M10"] = ModuleRun(status="SUCCEEDED")
            return finish(status, 0)
        except Exception as exc:
            log.exception("M10 failed")
            modules["M10"] = ModuleRun(status="FAILED", error=f"{type(exc).__name__}: {exc}")

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
    parser.add_argument("--no-llm", action="store_true", help="skip LLM modules (P1 has none)")
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
