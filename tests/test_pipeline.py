"""Offline tests. Bars and the daily report in tests/fixtures are recorded from a real run."""
import argparse
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from radar import config, market_calendar, run_daily
from radar.assemble.assembler import assemble
from radar.assemble.validator import run_status
from radar.collectors import market
from radar.models import (
    Briefing, BriefingPoint, Calendar, DailyReport, FocusChip, Indicator, KrImpact, KrStock,
    MarketSnapshot, ModuleRun, NewsItem, ReleasedEvent, Sentiment, Theme, UpcomingEvent,
)
from radar.render import renderer

FIXTURES = Path(__file__).parent / "fixtures"
SYMBOLS = config.load("symbols")
SETTINGS = config.load("settings")
SPECS = {s["key"]: s for s in SYMBOLS["indicators"]}
SESSION = date(2026, 11, 4)
RUN_DATE = date(2026, 11, 5)
CUTOFF = datetime(2026, 11, 5, 7, 10, tzinfo=timezone(timedelta(hours=9)))
RECORDED = json.loads((FIXTURES / "market_bars.json").read_text(encoding="utf-8"))


def close_of(day):
    return market_calendar.close_time("XNYS", day)


def bars(symbol, interval):
    return [(datetime.fromisoformat(ts), value) for ts, value in RECORDED[symbol][interval]]


def report_ok() -> DailyReport:
    return DailyReport.model_validate_json((FIXTURES / "daily_ok.json").read_text(encoding="utf-8"))


def fake_fetch(symbol, interval, start, end):
    return bars(symbol, interval)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setitem(SETTINGS["market"], "retry_backoff_s", [0, 0, 0])
    monkeypatch.setitem(SETTINGS["market"], "retry_jitter_s", 0)
    monkeypatch.setattr(run_daily.config, "load", lambda name: {"settings": SETTINGS, "symbols": SYMBOLS}[name])
    monkeypatch.setattr(market, "fetch_contract", lambda symbol: "2026-12")


def args(day=RUN_DATE, force=False):
    return argparse.Namespace(date=day, dry_run=False, force=force)


# --- calendar -------------------------------------------------------------

def test_sessions_and_us_holiday():
    assert market_calendar.previous_session("XNYS", RUN_DATE) == SESSION
    assert market_calendar.previous_session("XNYS", date(2026, 11, 9)) == date(2026, 11, 6)  # Monday
    assert not market_calendar.us_market_closed("XNYS", date(2026, 11, 9))  # weekend only
    assert market_calendar.us_market_closed("XNYS", date(2026, 11, 27))  # Thanksgiving


# --- M1 -------------------------------------------------------------------

def test_spot_uses_session_close_and_previous_close():
    ndx = market.build_indicator(SPECS["NDX"], bars("^NDX", "1d"), [], SESSION, CUTOFF, close_of)
    assert ndx.value == 28437.17 and ndx.change_pct == 0.91 and not ndx.stale
    assert ndx.spark[-1] == ndx.value and len(ndx.spark) == 5
    assert ndx.as_of == datetime(2026, 11, 4, 21, tzinfo=UTC)


def test_futures_ignore_quotes_after_cutoff_and_bars_after_session():
    nq = market.build_indicator(SPECS["NQ"], bars("NQ=F", "1d"), bars("NQ=F", "5m"), SESSION, CUTOFF, close_of, "2026-12")
    assert nq.as_of <= CUTOFF
    assert nq.value == 28646.75 and nq.change == 341.0  # reference is the 11/03 close
    assert nq.spark[-1] == nq.value and nq.contract == "2026-12"


def test_rates_report_bp_only():
    tnx = market.build_indicator(SPECS["US10Y"], bars("^TNX", "1d"), [], SESSION, CUTOFF, close_of)
    assert tnx.change_bp == -6 and tnx.change_pct is None


def test_old_last_bar_is_flagged_stale_not_hidden():
    ndx = market.build_indicator(SPECS["NDX"], bars("^NDX", "1d")[:-1], [], SESSION, CUTOFF, close_of)
    assert ndx.stale and ndx.as_of.date() < SESSION


def test_out_of_range_value_is_rejected():
    daily = [(ts, value * 10) for ts, value in bars("^TNX", "1d")]
    with pytest.raises(market.NoData):
        market.build_indicator(SPECS["US10Y"], daily, [], SESSION, CUTOFF, close_of)


def test_fred_fallback_is_stale_and_parsed():
    rows = market.parse_fred_csv("observation_date,DGS10\n2026-11-02,4.91\n2026-11-03,\n2026-11-04,4.95\n2026-11-05,9\n")
    indicator = market.build_from_fred(SPECS["US10Y"], rows, SESSION, close_of)
    assert indicator.value == 4.95 and indicator.change_bp == 4
    assert indicator.stale and indicator.source == "fred"


def test_front_month_rolls_after_third_friday():
    assert market.front_month(date(2026, 11, 5)) == "2026-12"
    assert market.front_month(date(2026, 12, 18)) == "2026-12"
    assert market.front_month(date(2026, 12, 19)) == "2027-03"


def test_retry_stops_on_client_error_and_retries_server_error():
    import httpx
    calls = []

    def fail(status):
        def fn():
            calls.append(status)
            request = httpx.Request("GET", "https://example.test")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
        return fn

    with pytest.raises(httpx.HTTPStatusError):
        market._with_retry(fail(403), [0, 0, 0], 0)
    with pytest.raises(httpx.HTTPStatusError):
        market._with_retry(fail(503), [0, 0, 0], 0)
    assert calls.count(403) == 1 and calls.count(503) == 4


def test_collect_marks_failures_missing_and_uses_fred(monkeypatch, fast):
    def fetch(symbol, interval, start, end):
        if symbol in ("^SOX", "^TNX"):
            raise market.NoData("down")
        return bars(symbol, interval)

    monkeypatch.setattr(market, "fetch_bars", fetch)
    monkeypatch.setattr(market, "fetch_fred", lambda url: [(date(2026, 11, 3), 4.95), (date(2026, 11, 4), 4.9)])
    snapshot = market.collect(SYMBOLS, SETTINGS, SESSION, CUTOFF, close_of)
    by_key = {i.key: i for i in snapshot.indicators}
    assert [i.key for i in snapshot.indicators] == list(SPECS)
    assert by_key["SOX"].missing and by_key["SOX"].value is None
    assert by_key["US10Y"].source == "fred" and by_key["US10Y"].stale


# --- M9 -------------------------------------------------------------------

def indicators(**overrides):
    result = []
    for i in report_ok().indicators:
        if i.key in overrides:
            i = Indicator(**{**i.model_dump(), "value": None, "change": None, "as_of": None,
                             "change_pct": None, "change_bp": None, "missing": True})
        result.append(i)
    return result


def test_run_status_rules():
    ok = {"M1": ModuleRun(status="SUCCEEDED")}
    degraded = {"M1": ModuleRun(status="DEGRADED")}
    assert run_status(indicators(), ok) == "OK"
    assert run_status(indicators(SOX=1), degraded) == "DEGRADED"
    assert run_status(indicators(VIX=1), degraded) == "FAILED_INDICATORS"


def test_assemble_without_market_output_shows_all_cards_as_missing():
    report = assemble(SYMBOLS, RUN_DATE, SESSION, False, None, {"M1": ModuleRun(status="FAILED")})
    assert [i.key for i in report.indicators] == list(SPECS)
    assert all(i.missing and i.value is None for i in report.indicators)
    assert report.run_status == "FAILED_INDICATORS" and report.banner.level == "warn"


def test_assemble_never_changes_collected_values():
    market_data = MarketSnapshot(indicators=report_ok().indicators)
    report = assemble(SYMBOLS, RUN_DATE, SESSION, False, market_data, {"M1": ModuleRun(status="SUCCEEDED")})
    assert report.indicators == market_data.indicators
    assert report.run_status == "OK" and report.banner is None


def test_us_holiday_banner():
    market_data = MarketSnapshot(indicators=report_ok().indicators)
    report = assemble(SYMBOLS, RUN_DATE, SESSION, True, market_data, {"M1": ModuleRun(status="SUCCEEDED")})
    assert report.banner.level == "info" and "휴장" in report.banner.text_ko


def test_contract_rejects_rates_without_bp_and_futures_without_contract():
    base = report_ok().indicators
    with pytest.raises(ValueError):
        Indicator(**{**base[5].model_dump(), "change_bp": None})
    with pytest.raises(ValueError):
        Indicator(**{**base[1].model_dump(), "contract": None})


# --- M10 ------------------------------------------------------------------

def full_report() -> DailyReport:
    """Synthetic content for template coverage only. Never published."""
    at = datetime(2026, 11, 4, 15, tzinfo=UTC)
    return report_ok().model_copy(update={
        "sentiment": Sentiment(source="VIX", vix=16.6, score=62, label="탐욕", label_en="GREED",
                               method="m", note_ko="VIX 기반 자체 산출 · CNN Fear & Greed 아님"),
        "calendar": Calendar(
            status="ok",
            released=[ReleasedEvent(time_utc=at, event="e", event_ko="테스트 지표", importance="high", previous="1")],
            upcoming=[UpcomingEvent(time_utc=at, event="e", event_ko="예정 지표", importance="medium")],
        ),
        "news": [NewsItem(
            id="n1", category="macro_rates", importance=5, importance_label_ko="관찰 중요도 높음",
            title_ko="<script>alert(1)</script>", preview_ko="p", published_at=at, source="S",
            url="javascript:alert(1)", summary_ko=["a", "b", "c"],
            kr_impact=KrImpact(direction="neutral", text="t"),
        )],
        "themes": [Theme(us_ticker="AAA", name="A", change_pct=-1.5, theme="T",
                         kr_stocks=[KrStock(code="1", name="국내", link="same_theme", relation_ko="동일 업종")])],
        "briefing": Briefing(
            direction="gap_up", direction_ko="갭상승 우세", confidence="low", headline_ko=["첫 줄,", "둘째 강조."],
            headline_emphasis="강조", points=[BriefingPoint(title_ko="제목", text_ko="포인트 하나")],
            focus_chips=[FocusChip(label="칩", anchor="javascript:x")], source="template",
        ),
    })


def write_daily(data_dir, report):
    path = data_dir / "daily" / f"{report.run_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(), encoding="utf-8")


def rendered(tmp_path, report):
    write_daily(tmp_path / "data", report)
    renderer.render_site(tmp_path / "data", tmp_path / "site", SETTINGS)
    return (tmp_path / "site" / "index.html").read_text(encoding="utf-8")


def test_page_meets_display_rules(tmp_path):
    html = rendered(tmp_path, report_ok())
    assert '<meta name="robots" content="noindex,nofollow">' in html
    assert "11월 5일 (목)" in html
    assert SETTINGS["disclaimer_ko"] in html
    assert html.count('class="market-card') == 8
    assert "−6 bp" in html and "−0.34%" in html
    assert "근월물 2026-12" in html
    assert 'property="og:image" content="https://tiong0211-hub.github.io/USmarket-radar/static/og.png"' in html
    for banned in ("PREVIEW", "SAMPLE", "예시", "innerHTML", "<script src"):
        assert banned not in html
    assert (tmp_path / "site" / "static" / "og.png").exists()


def test_external_requests_are_fonts_only(tmp_path):
    html = rendered(tmp_path, report_ok())
    loaded = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert loaded and all("fonts.g" in url for url in loaded)


def test_css_respects_minimum_font_size():
    css = (Path(renderer.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert min(int(n) for n in re.findall(r"font-size:(\d+)px", css)) >= 10
    assert "@import" not in css


def test_missing_indicator_is_shown_gray_not_hidden(tmp_path):
    report = report_ok().model_copy(update={"indicators": indicators(SOX=1, VIX=1)})
    html = rendered(tmp_path, report)
    assert html.count('class="market-card is-missing"') == 2
    assert html.count('<div class="market-value no-data">데이터 없음</div>') == 2
    assert html.count('class="market-card') == 8


def test_feed_text_is_escaped_and_unsafe_links_are_dropped(tmp_path):
    html = rendered(tmp_path, full_report())
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "둘째 <em>강조</em>." in html
    assert 'href="#overview">칩' in html
    assert "▼ −1.50%" in html


def test_archive_links_and_latest_date_win(tmp_path):
    older = report_ok().model_copy(update={"run_date": date(2026, 11, 4), "weekday_ko": "수"})
    write_daily(tmp_path / "data", older)
    html = rendered(tmp_path, report_ok())
    assert 'href="2026-11-04/index.html">어제' in html
    assert "11월 5일" in html
    page = (tmp_path / "site" / "2026-11-05" / "index.html").read_text(encoding="utf-8")
    assert 'href="../2026-11-04/index.html">어제' in page and 'href="../archive/index.html"' in page
    archive = (tmp_path / "site" / "archive" / "index.html").read_text(encoding="utf-8")
    assert archive.index("2026-11-05") < archive.index("2026-11-04")


def test_failed_run_keeps_previous_page_with_error_banner(tmp_path):
    older = report_ok().model_copy(update={"run_date": date(2026, 11, 4), "weekday_ko": "수"})
    write_daily(tmp_path / "data", older)
    write_daily(tmp_path / "data", report_ok())
    renderer.render_site(tmp_path / "data", tmp_path / "site", SETTINGS, keep_previous_for=RUN_DATE)
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "오늘 브리핑 생성에 실패해 어제 내용을 표시합니다." in html and "11월 4일 (수)" in html
    assert not (tmp_path / "site" / "2026-11-05").exists()


# --- Orchestrator ---------------------------------------------------------

def test_end_to_end_ok(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    assert run_daily.run(args(), tmp_path) == ("OK", 0)
    report = DailyReport.model_validate_json((tmp_path / "data/daily/2026-11-05.json").read_text(encoding="utf-8"))
    log = json.loads((tmp_path / "data/logs/2026-11-05.run.json").read_text(encoding="utf-8"))
    assert report.run_status == log["run_status"] == "OK" and log["us_session"] == "2026-11-04"
    assert (tmp_path / "data/raw/2026-11-05/market.json").exists()
    assert (tmp_path / "site/index.html").exists()


def test_all_sources_down_still_publishes_with_failed_indicators(tmp_path, monkeypatch, fast):
    def down(*_, **__):
        raise OSError("network down")

    monkeypatch.setattr(market, "fetch_bars", down)
    monkeypatch.setattr(market, "fetch_fred", down)
    assert run_daily.run(args(), tmp_path) == ("FAILED_INDICATORS", 0)
    html = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert html.count('class="market-card is-missing"') == 8


def test_krx_holiday_skips_and_force_overrides(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    sunday = date(2026, 11, 8)
    assert run_daily.run(args(sunday), tmp_path) == ("SKIPPED_HOLIDAY", 0)
    assert not (tmp_path / "site").exists()
    assert run_daily.run(args(sunday, force=True), tmp_path)[0] != "SKIPPED_HOLIDAY"


def test_render_failure_keeps_previous_page(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    older = report_ok().model_copy(update={"run_date": date(2026, 11, 4), "weekday_ko": "수"})
    write_daily(tmp_path / "data", older)
    original = run_daily.render_site

    def fail_first(data_dir, site_dir, settings, keep_previous_for=None):
        if keep_previous_for is None:
            raise RuntimeError("template bug")
        original(data_dir, site_dir, settings, keep_previous_for=keep_previous_for)

    monkeypatch.setattr(run_daily, "render_site", fail_first)
    assert run_daily.run(args(), tmp_path) == ("FAILED", 0)
    assert "어제 내용을 표시합니다" in (tmp_path / "site/index.html").read_text(encoding="utf-8")


def test_github_output_is_written(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    monkeypatch.setattr(run_daily, "run", lambda a: ("OK", 0))
    output = tmp_path / "out"
    assert run_daily.main(["--date", "2026-11-05", "--no-llm", "--no-deploy", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "run_status=OK\nrun_date=2026-11-05\n"
