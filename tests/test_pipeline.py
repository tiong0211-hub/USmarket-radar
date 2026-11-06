"""Offline tests. Bars and the daily report in tests/fixtures are recorded from a real run."""
import argparse
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import openai
import pytest

from radar import config, market_calendar, run_daily
from radar.assemble.assembler import assemble
from radar.assemble.validator import briefing_problems, run_status
from radar.collectors import calendar as calendar_collector
from radar.analytics import sentiment
from radar.collectors import market, movers, news
from radar.llm import client as llm_client
from radar.llm import briefing_writer, news_analyst, theme_mapper
from radar.retry import with_retry
from radar.models import (
    Briefing, BriefingPoint, Calendar, CalendarActual, CalendarSnapshot, DailyReport, FocusChip, Indicator, KrImpact, KrStock,
    MarketSnapshot, ModuleRun, NewsAnalysis, NewsCollection, NewsItem, RawArticle, ReleasedEvent,
    Mover, MoversSnapshot, Sentiment, Theme, UpcomingEvent,
)
from radar.render import renderer

FIXTURES = Path(__file__).parent / "fixtures"
THEME_MAP = config.load("theme_map")
LISTING = {s["code"]: s["name"] for t in THEME_MAP["themes"] for s in t["kr_stocks"]}
SYMBOLS = config.load("symbols")
SETTINGS = config.load("settings")
SPECS = {s["key"]: s for s in SYMBOLS["indicators"]}
SESSION = date(2026, 11, 4)
RUN_DATE = date(2026, 11, 5)
CUTOFF = datetime(2026, 11, 5, 7, 10, tzinfo=timezone(timedelta(hours=9)))
RECORDED = json.loads((FIXTURES / "market_bars.json").read_text(encoding="utf-8"))


RSS = (FIXTURES / "feed_sample.xml").read_bytes()
FF_EVENTS = json.loads((FIXTURES / "ff_calendar.json").read_text(encoding="utf-8"))
FEEDS = config.load("feeds")
MODELS = config.load("models")
FEED = {"name": "Test Feed"}
NEWS_START = datetime(2026, 11, 3, 21, tzinfo=UTC)


def good_item(article_id, title="연준 동결", **overrides):
    return {
        "id": article_id, "relevant": True, "category": "macro_rates", "importance": 5,
        "title_ko": title, "preview_ko": "금리 경로 관찰", "summary_ko": ["첫째", "둘째", "셋째"],
        "kr_impact": {"direction": "neutral", "text": "환율 민감 업종 관찰"}, "us_tickers": [], **overrides,
    }


def picked(id, moves=(), same=()):
    return {"id": id, "move_keys": list(moves), "same_event_ids": list(same)}


def good_briefing(**changes):
    return {"direction": "gap_down", "confidence": "medium", "headline_ko": ["기술주 급락 마감,", "아침의 초점은 반도체."],
            "headline_emphasis": "반도체",
            "points": [{"title_ko": "위험 회피", "text_ko": "지수 전반이 약세로 마감한 상황."},
                       {"title_ko": "금리 하락", "text_ko": "국채 수익률이 내려간 점을 관찰."},
                       {"title_ko": "국내 연결", "text_ko": "반도체 업종 수급 확인."}],
            "focus_chips": ["반도체"], "risks_ko": ["오늘 밤 고용 지표 확인."], **changes}


class FakeAPI:
    """Stands in for openai.OpenAI. `script` maps the call index to an exception or a raw string."""

    def __init__(self, respond=None, script=None, pick=None, propose=None, brief=None):
        self.respond = respond or (lambda articles: {"items": [good_item(a["id"]) for a in articles], "calendar_actuals": []})
        self.pick = pick or (lambda articles: {"picks": [picked(a["id"]) for a in articles if a["eligible"]]})
        self.propose = propose or (lambda payload: {"proposals": []})
        self.brief = brief or (lambda digest: good_briefing())
        self.script = script or {}
        self.calls = 0
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        index = self.calls
        self.calls += 1
        self.requests.append(kwargs)
        scripted = self.script.get(index)
        if isinstance(scripted, Exception):
            raise scripted
        payload = json.loads(kwargs["messages"][1]["content"])
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "theme_proposer":
            answer = self.propose(payload)
        elif name == "briefing_writer":
            answer = self.brief(payload)
        else:
            answer = (self.pick if name == "news_picker" else self.respond)(payload["articles"])
        content = scripted if scripted is not None else json.dumps(answer, ensure_ascii=False)
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=100),
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def api_error(cls, status, headers=None):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.test"), headers=headers or {})
    return cls("secret-looking text sk-test", response=response, body=None)


def close_of(day):
    return market_calendar.close_time("XNYS", day)


def bars(symbol, interval):
    return [(datetime.fromisoformat(ts), value) for ts, value in RECORDED[symbol][interval]]


def report_ok() -> DailyReport:
    return DailyReport.model_validate_json((FIXTURES / "daily_ok.json").read_text(encoding="utf-8"))


def fake_fetch(symbol, interval, start, end):
    return bars(symbol if symbol in RECORDED else "^NDX", interval)  # movers reuse a recorded series


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setitem(SETTINGS["market"], "retry_backoff_s", [0, 0, 0])
    monkeypatch.setitem(SETTINGS["market"], "retry_jitter_s", 0)
    monkeypatch.setitem(SETTINGS["movers"], "retry_backoff_s", [0, 0])
    monkeypatch.setitem(SETTINGS["sentiment"], "min_history", 3)  # recorded VIX bars cover weeks, not a year
    monkeypatch.setattr(theme_mapper, "krx_listing", lambda: dict(LISTING))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda name: {"settings": SETTINGS, "symbols": SYMBOLS, "feeds": {**FEEDS, "min_articles_warning": 1}}.get(name) or real_load(name))
    monkeypatch.setattr(market, "fetch_contract", lambda symbol: "2026-12")
    monkeypatch.setattr(news, "fetch_feed", lambda url: RSS)
    monkeypatch.setattr(calendar_collector, "fetch_events", lambda url: FF_EVENTS)
    monkeypatch.setattr(run_daily, "LLMClient", lambda budget: llm_client.LLMClient(budget, api=FakeAPI()))


def args(day=RUN_DATE, force=False, no_llm=False):
    return argparse.Namespace(date=day, dry_run=False, force=force, no_llm=no_llm)


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
    calls = []

    def fail(status):
        def fn():
            calls.append(status)
            request = httpx.Request("GET", "https://example.test")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
        return fn

    with pytest.raises(httpx.HTTPStatusError):
        with_retry(fail(403), [0, 0, 0], 0)
    with pytest.raises(httpx.HTTPStatusError):
        with_retry(fail(503), [0, 0, 0], 0)
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


# --- M8 briefing -----------------------------------------------------------

def run_with_briefing(tmp_path, monkeypatch, **api):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    fake = FakeAPI(**api)
    monkeypatch.setattr(run_daily, "LLMClient", lambda budget: llm_client.LLMClient(budget, api=fake))
    result = run_daily.run(args(), tmp_path)
    report = DailyReport.model_validate_json((tmp_path / "data/daily/2026-11-05.json").read_text(encoding="utf-8"))
    log = json.loads((tmp_path / "data/logs/2026-11-05.run.json").read_text(encoding="utf-8"))
    return result, report, log, fake


def test_briefing_from_model_is_shown(tmp_path, monkeypatch, fast):
    result, report, log, fake = run_with_briefing(tmp_path, monkeypatch)
    assert result == ("OK", 0) and log["modules"]["M8"]["status"] == "SUCCEEDED"
    assert report.briefing.source == MODELS["briefing_writer"]["model"] and report.briefing.direction_ko == "갭하락 우세"
    html = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "<em>반도체</em>" in html and "아직 없습니다" not in html
    assert "주의할 변수" in html and "오늘 밤 고용 지표 확인." in html
    digest = json.loads((tmp_path / "data/raw/2026-11-05/briefing_digest.json").read_text(encoding="utf-8"))
    assert {i["key"] for i in digest["indicators"]} >= {"NDX", "SPX", "VIX"}


def test_briefing_with_invented_number_falls_back_to_template(tmp_path, monkeypatch, fast):
    invented = good_briefing(points=[{"title_ko": "갭 예측", "text_ko": "코스피 +0.87% 상승 예상."}])
    result, report, log, _ = run_with_briefing(tmp_path, monkeypatch, brief=lambda digest: invented)
    assert result == ("DEGRADED", 0) and report.briefing.source == "template"
    assert any("numbers not in digest: 0.87" in n for n in log["modules"]["M8"]["notes"])
    assert "요약 자동 작성 실패(기본 요약 표시)" in report.warnings


def test_briefing_fallback_model_when_primary_fails(tmp_path, monkeypatch, fast):
    calls = []

    def brief(digest):
        calls.append(1)
        return {"broken": True} if len(calls) <= 2 else good_briefing()  # primary: first try + one retry

    monkeypatch.setattr(llm_client.time, "sleep", lambda _: None)
    result, report, log, _ = run_with_briefing(tmp_path, monkeypatch, brief=brief)
    assert report.briefing.source == MODELS["briefing_fallback"]["model"]
    assert log["modules"]["M8"]["status"] == "DEGRADED" and result[0] == "DEGRADED"


def test_briefing_minus_sign_only_before_numbers():
    assert briefing_writer._minus("나스닥100 -6.36%, 10년물 -6bp, 2026-11-06, A-B") == "나스닥100 −6.36%, 10년물 −6bp, 2026-11-06, A-B"


def test_template_briefing_uses_collected_numbers_only():
    draft = report_ok()
    briefing = briefing_writer.template(draft, SETTINGS)
    assert briefing.source == "template" and briefing.direction == "flat"
    ndx = next(i for i in draft.indicators if i.key == "NDX")
    assert f"{ndx.change_pct:+.2f}%".replace("-", "\u2212") in briefing.points[0].text_ko
    assert briefing_problems(briefing, briefing_writer.digest(draft)) == []


def test_briefing_problems_rules():
    digest = {"run_date": "2026-11-06", "indicators": [{"label": "미국 10년물", "value": 21034.12, "change_bp": -6}], "time": "22:30"}

    def brief(text, emphasis="초점"):
        return Briefing(direction="flat", direction_ko="x", confidence="low", headline_ko=["a,", "초점 b"],
                        headline_emphasis=emphasis, points=[BriefingPoint(title_ko="t", text_ko=text)], source="m")

    assert briefing_problems(brief("11월 6일 22:30, 21,034.12 · 10년물 −6bp"), digest) == []
    assert briefing_problems(brief("2배 상승"), digest) == ["numbers not in digest: 2"]
    assert briefing_problems(brief("x", emphasis="없음"), digest) == ["headline_emphasis is not in headline_ko"]


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


def test_no_llm_shows_titles_and_is_degraded(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    assert run_daily.run(args(no_llm=True), tmp_path) == ("DEGRADED", 0)
    report = DailyReport.model_validate_json((tmp_path / "data/daily/2026-11-05.json").read_text(encoding="utf-8"))
    assert report.news and all(n.importance_label_ko == "요약 없음" and not n.summary_ko for n in report.news)
    assert "뉴스 요약 일부 없음(제목만 표시)" in report.banner.text_ko


def test_missing_api_key_never_fails_the_run(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)

    def no_key(budget):
        raise llm_client.LLMFatal("OPENAI_API_KEY is not set")

    monkeypatch.setattr(run_daily, "LLMClient", no_key)
    assert run_daily.run(args(), tmp_path) == ("DEGRADED", 0)
    log = json.loads((tmp_path / "data/logs/2026-11-05.run.json").read_text(encoding="utf-8"))
    assert log["modules"]["M6"]["status"] == "FAILED"


def test_news_and_calendar_failure_degrade_but_publish(tmp_path, monkeypatch, fast):
    def down(*_):
        raise OSError("down")

    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    monkeypatch.setattr(news, "fetch_feed", down)
    monkeypatch.setattr(calendar_collector, "fetch_events", down)
    assert run_daily.run(args(), tmp_path) == ("DEGRADED", 0)
    report = DailyReport.model_validate_json((tmp_path / "data/daily/2026-11-05.json").read_text(encoding="utf-8"))
    assert report.news == [] and report.calendar.status == "missing"
    assert "캘린더 미수집" in report.warnings
    html = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "미수집 · 데이터 없음" in html


def test_run_log_has_usage_and_no_api_error_text(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    api = FakeAPI(script={0: api_error(openai.AuthenticationError, 401)})
    monkeypatch.setattr(run_daily, "LLMClient", lambda budget: llm_client.LLMClient(budget, api=api))
    run_daily.run(args(), tmp_path)
    for path in (tmp_path / "data").rglob("*.json"):
        assert "sk-test" not in path.read_text(encoding="utf-8")


def test_github_output_is_written(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    monkeypatch.setattr(run_daily, "run", lambda a: ("OK", 0))
    output = tmp_path / "out"
    assert run_daily.main(["--date", "2026-11-05", "--no-llm", "--no-deploy", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "run_status=OK\nrun_date=2026-11-05\n"


def test_end_to_end_shows_verified_themes_and_sentiment(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    verified = {"themes": [{**t, "verified": True} for t in THEME_MAP["themes"]]}
    loader = config.load
    monkeypatch.setattr(config, "load", lambda name: verified if name == "theme_map" else loader(name))
    monkeypatch.setattr(theme_mapper, "leads", lambda *_: True)  # recorded bars are one series; rule tested below
    assert run_daily.run(args(), tmp_path) == ("OK", 0)
    report = DailyReport.model_validate_json((tmp_path / "data/daily/2026-11-05.json").read_text(encoding="utf-8"))
    assert [t.us_ticker for t in report.themes] and report.sentiment is not None
    assert report.themes_rule_ko.startswith("S&P 500(")
    html = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "SK하이닉스" in html and "HBM 공급" in html and "VIX 기반 자체 산출" in html


# --- M4 / M5 / M7 ------------------------------------------------------------

def day_bars(*closes, last=SESSION):
    return [(datetime(last.year, last.month, last.day, 16, tzinfo=UTC) - timedelta(days=len(closes) - 1 - i), c)
            for i, c in enumerate(closes)]


def test_mover_change_and_stale_session():
    entry = {"symbol": "NVDA", "name": "NVIDIA", "kind": "stock"}
    assert movers.build_mover(entry, day_bars(100, 90), SESSION).change_pct == -10.0
    with pytest.raises(market.NoData):
        movers.build_mover(entry, day_bars(100, 90, last=SESSION - timedelta(days=1)), SESSION)
    symbols = {"watchlist": [{"symbol": "NVDA", "name": "dup"}, {"symbol": "AMD", "name": "AMD"}],
               "sector_etfs": [{"symbol": "SOXX", "label": "반도체"}]}
    listed = movers.watch_list(THEME_MAP, symbols)
    assert [e["symbol"] for e in listed][:len(THEME_MAP["themes"])] == [t["us_ticker"] for t in THEME_MAP["themes"]]
    assert [e["symbol"] for e in listed].count("NVDA") == 1 and listed[-1]["kind"] == "etf"


def test_sentiment_percentile_rule_and_bands():
    assert sentiment.score(30, [10, 20, 30, 40]) == 25  # 3 of 4 closes at or below 30
    snap = MarketSnapshot(indicators=report_ok().indicators, history={"VIX": [10.0] * 200})
    result = sentiment.compute(snap, SETTINGS["sentiment"])  # fixture VIX is above every close
    assert result.score == 0 and result.label == "극도의 공포" and result.label_en == "EXTREME FEAR"
    with pytest.raises(ValueError):
        sentiment.compute(snap.model_copy(update={"history": {"VIX": [10.0] * 5}}), SETTINGS["sentiment"])


def mover(symbol, change):
    return Mover(symbol=symbol, name=symbol, kind="stock", close=100, change_pct=change)


def test_mapping_shows_only_verified_listed_leaders():
    theme_map = {"themes": [{**t, "verified": t["us_ticker"] != "LLY"} for t in THEME_MAP["themes"]]}
    # index −3%: TSM beats it but still fell, so it is not a leader
    snap = MoversSnapshot(movers=[mover("NVDA", 2.0), mover("TSM", -1.0), mover("GEV", 4.0), mover("LLY", 9.0)])
    item = news_item(1, 3).model_copy(update={"us_tickers": ["NVDA"], "title_ko": "NVIDIA 실적", "preview_ko": "엔비디아 실적 호조"})
    market_wide = news_item(2, 5).model_copy(update={"us_tickers": ["NVDA"], "preview_ko": "나스닥 급락"})
    listing = {**LISTING, "000150": "다른회사"}
    themes, excluded, notes = theme_mapper.map_themes(theme_map, snap, NewsAnalysis(news=[market_wide, item]), listing, 6,
                                                      -3.0, 1.0)
    assert [t.us_ticker for t in themes] == ["GEV", "NVDA"]
    nvda = themes[1]
    assert nvda.momentum_ko == "엔비디아 실적 호조" and nvda.related_news_ids == ["n_1", "n_2"]  # named first
    assert "000150" not in {s.code for s in nvda.kr_stocks} and excluded == ["NVDA → 000150 두산"]
    assert notes == ["1 theme_map entries not verified: hidden"]
    assert theme_mapper.map_themes(theme_map, snap, None, listing, 6, None, 1.0)[0] == []  # no index, no leaders
    assert not theme_mapper.leads(1.5, 1.0, 1.0) and theme_mapper.leads(2.0, 1.0, 1.0)


def test_proposals_only_for_unmapped_news_movers_and_listed_names():
    snap = MoversSnapshot(movers=[mover("VRT", 14.0), mover("NVDA", 9.0), mover("AAPL", 1.0), mover("MU", 17.0),
                                  mover("AMD", -14.0)])
    news_items = [news_item(i, 5).model_copy(update={"us_tickers": [t]})
                  for i, t in enumerate(["VRT", "NVDA", "AAPL", "AMD"])]
    seen = []

    def propose(payload):
        seen.append(payload)
        return {"proposals": [{"us_ticker": "VRT", "theme": "전력인프라", "is_new_theme": False, "kr_candidates": [
            {"name": "LS ELECTRIC", "code": "999999", "link_type": "same_theme", "rationale_ko": "전력기기", "confidence": "medium"},
            {"name": "없는회사", "code": "123456", "link_type": "supplier", "rationale_ko": "x", "confidence": "medium"},
            {"name": "SK하이닉스", "code": "000660", "link_type": "supplier", "rationale_ko": "x", "confidence": "low"},
        ]}, {"us_ticker": "ZZZ", "theme": "x", "is_new_theme": True, "kr_candidates": []}]}

    client = llm_client.LLMClient(llm_client.TokenBudget(SETTINGS["llm_budget"]), api=FakeAPI(propose=propose))
    proposals = theme_mapper.propose(THEME_MAP, snap, NewsAnalysis(news=news_items), LISTING,
                                     MODELS["theme_proposer"], SETTINGS, client, 0.0)
    # NVDA mapped, AAPL small, MU not in news, AMD fell
    assert [u["us_ticker"] for u in seen[0]["unmapped"]] == ["VRT"]
    assert len(proposals) == 1 and proposals[0].news_ids == ["n_0"]
    assert [(c.name, c.code) for c in proposals[0].kr_candidates] == [("LS ELECTRIC", "010120")]


def test_issue_body_and_no_token_skips_filing(monkeypatch):
    from radar.models import KrCandidate, ThemeProposal
    proposal = ThemeProposal(us_ticker="VRT", name="Vertiv", change_pct=-14.0, theme="전력인프라", is_new_theme=False,
                             kr_candidates=[KrCandidate(name="LS ELECTRIC", code="010120", link_type="same_theme",
                                                        rationale_ko="전력기기", confidence="medium")],
                             news_ids=["n_1"])
    body = theme_mapper.issue_body(proposal, RUN_DATE, {"n_1": "https://a.test/1"})
    assert "VRT" in body and "010120" in body and "https://a.test/1" in body and "theme_map.yaml" in body
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert theme_mapper.file_issues([proposal], RUN_DATE, {}, "theme-proposal")[0].startswith("issues skipped")


# --- M2 -------------------------------------------------------------------

def test_feed_window_cleanup_and_summary_limit():
    found = news.parse_feed(RSS, FEED, NEWS_START, CUTOFF)
    titles = [a.title for a in found]
    assert "Fed holds rates steady at 4.25%" in titles
    assert "Old story outside the window" not in titles
    assert "Story without a date" not in titles
    assert "Bad link story" not in titles
    google = next(a for a in found if a.title == "Stocks rally on chip demand")  # " - WSJ" removed
    assert google.source == "WSJ" and google.summary == ""  # title echo dropped
    long = next(a for a in found if a.title == "Long summary story")
    assert len(long.summary) == 300 and "<" not in long.summary
    assert re.fullmatch(r"n_[0-9a-f]{8}", long.id)


def test_deduplicate_keeps_first_and_catches_near_duplicates():
    def article(title, link):
        return RawArticle(id="n", title=title, link=link, source="S", feed="F", published_at=NEWS_START)

    kept = news.deduplicate([
        article("Fed holds rates steady at 4.25%", "https://a.test/1"),
        article("Fed holds rates steady at 4.25 %!", "https://b.test/2"),
        article("Totally different", "https://a.test/1"),
        article("Oil jumps on supply fears", "https://c.test/3"),
    ])
    assert [a.link for a in kept] == ["https://a.test/1", "https://c.test/3"]


def test_collect_skips_unverified_and_survives_one_feed_failure(monkeypatch, fast):
    urls = []

    def fetch(url):
        urls.append(url)
        if "cnbc" in url:
            raise OSError("down")
        return RSS

    monkeypatch.setattr(news, "fetch_feed", fetch)
    cfg = {**FEEDS, "fetch_retry_backoff_s": []}
    result = news.collect(cfg, NEWS_START, CUTOFF)
    assert not any("yahoo" in url for url in urls)
    assert result.articles and "CNBC Top News" not in result.feeds_ok
    assert any("CNBC Top News" in note for note in result.notes)


def test_collect_raises_when_every_feed_fails(monkeypatch):
    def down(url):
        raise OSError("down")

    monkeypatch.setattr(news, "fetch_feed", down)
    with pytest.raises(RuntimeError):
        news.collect({**FEEDS, "fetch_retry_backoff_s": []}, NEWS_START, CUTOFF)


# --- M3 -------------------------------------------------------------------

KST = timezone(timedelta(hours=9))
KST_ZONE = ZoneInfo("Asia/Seoul")
WINDOW = SETTINGS["calendar"]["upcoming_window_kst"]


def snapshot(history=(), run_date=RUN_DATE, session=SESSION, cutoff=CUTOFF, importance=("high", "medium")):
    return calendar_collector.split_events(
        [*calendar_collector.parse_events(FF_EVENTS, FEEDS["calendar"]), *history],
        run_date, session, cutoff, WINDOW, KST_ZONE, importance,
    )


def test_calendar_filters_usd_high_medium_and_translates():
    events = calendar_collector.parse_events(FF_EVENTS, FEEDS["calendar"])
    assert events and all(e.importance in ("high", "medium") for e in events)
    assert len(events) == sum(1 for e in FF_EVENTS if e["country"] == "USD" and e["impact"] in ("High", "Medium"))
    ism = next(e for e in events if e.event == "ISM Services PMI")
    assert ism.event_ko == "ISM 서비스업 PMI" and ism.tag_ko == "경기"
    assert ism.time_utc == datetime(2026, 11, 4, 15, tzinfo=UTC)


def test_calendar_splits_released_and_tonight():
    result = snapshot()
    assert [e.event for e in result.released] == ["ISM Services PMI"]
    assert result.released[0].actual is None  # the feed has no actuals
    assert [e.event for e in result.upcoming] == ["Unemployment Claims"]  # 22:30 KST tonight
    assert result.status == "ok"
    high_only = snapshot(importance=tuple(SETTINGS["calendar"]["upcoming_importance"]))
    assert high_only.upcoming == [] and high_only.released == result.released  # Claims is medium


def test_unknown_title_stays_in_english_and_blank_forecast_is_none():
    raw = [{"title": "New Event", "country": "USD", "date": "2026-11-05T08:30:00-05:00",
            "impact": "High", "forecast": "", "previous": ""}]
    event = calendar_collector.parse_events(raw, FEEDS["calendar"])[0]
    assert event.event_ko == "New Event" and event.forecast is None and event.tag_ko is None


def test_monday_keeps_friday_releases_from_history(monkeypatch):
    friday = UpcomingEvent(time_utc=datetime(2026, 11, 6, 13, 30, tzinfo=UTC), event="Unemployment Rate",
                           event_ko="실업률", importance="high")
    monkeypatch.setattr(calendar_collector, "fetch_events", lambda url: [])
    monday = date(2026, 11, 9)
    result = calendar_collector.collect(
        {**FEEDS["calendar"], "fetch_retry_backoff_s": []}, monday, date(2026, 11, 6),
        datetime(2026, 11, 9, 7, 10, tzinfo=KST), WINDOW, KST_ZONE, [friday], ["high"],
    )
    assert [e.event for e in result.released] == ["Unemployment Rate"]


def test_orchestrator_reads_only_earlier_calendar_history(tmp_path):
    for day, name in (("2026-11-04", "old"), ("2026-11-05", "today")):
        path = tmp_path / "raw" / day / "calendar.json"
        path.parent.mkdir(parents=True)
        event = UpcomingEvent(time_utc=NEWS_START, event=name, event_ko=name, importance="high")
        path.write_text(CalendarSnapshot(events=[event]).model_dump_json(), encoding="utf-8")
    assert [e.event for e in run_daily._previous_calendar_events(tmp_path, RUN_DATE)] == ["old"]


# --- M6 -------------------------------------------------------------------

def collection(count=3, summary="Rates at 4.25% and payrolls of 197K"):
    return NewsCollection(articles=[
        RawArticle(id=f"n_{i}", title=f"Story {i}", link=f"https://a.test/{i}", summary=summary,
                   source="CNBC", feed="CNBC Top News", published_at=NEWS_START)
        for i in range(count)
    ])


def analyze(api, articles=None, calendar=None, cfg=None):
    client = llm_client.LLMClient(llm_client.TokenBudget(SETTINGS["llm_budget"]), api=api)
    return news_analyst.analyze(
        articles or collection(), calendar, SESSION, cfg or {**MODELS["news_analyst"], "max_concurrency": 1},
        SETTINGS, client,
    )


def test_request_uses_config_model_strict_schema_and_prompt_file():
    api = FakeAPI()
    analyze(api)
    request = api.requests[0]
    assert request["model"] == MODELS["news_analyst"]["model"]
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["messages"][0]["content"] == (config.ROOT / "prompts/news_analyst.md").read_text(encoding="utf-8")


def schema_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from schema_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from schema_objects(value)


@pytest.mark.parametrize("name", ["news_analyst", "news_picker", "theme_proposer"])
def test_schema_meets_openai_strict_rules(name):
    for node in schema_objects(llm_client.load_schema(name)):
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_links_and_times_come_from_m2_not_the_model():
    result = analyze(FakeAPI(respond=lambda articles: {
        "items": [good_item(a["id"], url="https://evil.test", source="Fake", published_at="2000-01-01") for a in articles],
        "calendar_actuals": [],
    }))
    assert all(n.url.startswith("https://a.test/") and n.source == "CNBC" and n.published_at == NEWS_START for n in result.news)
    assert result.news[0].importance_label_ko == "관찰 중요도 높음"


def test_irrelevant_dropped_unknown_ids_ignored_and_skipped_ids_get_titles():
    def respond(articles):
        return {"items": [
            good_item("n_0", relevant=False),
            good_item("n_1"),
            good_item("n_999"),
        ], "calendar_actuals": []}

    result = analyze(FakeAPI(respond=respond))
    assert {n.id for n in result.news} == {"n_1", "n_2"}
    fallback = next(n for n in result.news if n.id == "n_2")
    assert fallback.title_ko == "Story 2" and fallback.importance_label_ko == "요약 없음"


def test_invented_number_falls_back_to_title():
    result = analyze(FakeAPI(respond=lambda articles: {
        "items": [good_item("n_0", title="금리 4.25% 유지"), good_item("n_1", title="엔비디아 12% 급등"),
                  good_item("n_2")],
        "calendar_actuals": [],
    }))
    by_id = {n.id: n for n in result.news}
    assert by_id["n_0"].title_ko == "금리 4.25% 유지"
    assert by_id["n_1"].title_ko == "Story 1" and not by_id["n_1"].summary_ko
    assert any("numbers not in article" in note for note in result.notes)


def test_invalid_output_retried_once_then_title_only():
    api = FakeAPI(script={0: "not json", 1: '{"items": []}'})
    result = analyze(api)
    assert api.calls == 2 and result.failed_batches == 1
    assert all(n.importance_label_ko == "요약 없음" for n in result.news)


def test_rate_limit_retries_with_retry_after_and_server_error_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    api = FakeAPI(script={0: api_error(openai.RateLimitError, 429, {"retry-after": "7"})})
    assert analyze(api).failed_batches == 0 and api.calls == 2 and sleeps == [7.0]
    api = FakeAPI(script={0: api_error(openai.InternalServerError, 500)})
    assert analyze(api).failed_batches == 0 and api.calls == 2


def test_auth_error_stops_remaining_batches_without_leaking_text():
    api = FakeAPI(script={0: api_error(openai.AuthenticationError, 401)})
    result = analyze(api, collection(25))
    assert api.calls == 1 and result.failed_batches == result.total_batches == 3
    assert "sk-test" not in result.model_dump_json()
    assert len(result.news) == 25


def test_budget_guard_stops_calls_before_spending():
    api = FakeAPI()
    client = llm_client.LLMClient(llm_client.TokenBudget({MODELS["news_analyst"]["model"]: 10}), api=api)
    result = news_analyst.analyze(collection(), None, SESSION, MODELS["news_analyst"], SETTINGS, client)
    assert api.calls == 0 and result.failed_batches == 1 and result.news


def test_calendar_actual_requires_known_event_and_number_in_evidence():
    cal = snapshot()
    result = analyze(FakeAPI(respond=lambda articles: {
        "items": [good_item(a["id"]) for a in articles],
        "calendar_actuals": [
            {"event": "ISM Services PMI", "actual": "54.1", "evidence_id": "n_0"},  # not in the article
            {"event": "Unknown Event", "actual": "197K", "evidence_id": "n_0"},
            {"event": "ISM Services PMI", "actual": "197K", "evidence_id": "n_404"},
        ],
    }), calendar=cal)
    assert result.calendar_actuals == []
    good = analyze(FakeAPI(respond=lambda articles: {
        "items": [good_item(a["id"]) for a in articles],
        "calendar_actuals": [{"event": "ISM Services PMI", "actual": "197K", "evidence_id": "n_0"}],
    }), calendar=cal)
    assert [a.actual for a in good.calendar_actuals] == ["197K"]


def test_picker_ranks_by_move_then_coverage_and_m9_shows_that_order(tmp_path):
    """n_5 explains a move; n_2 has wider coverage than n_7. Unknown ids, keys and repeats are dropped."""
    seen = []

    def pick(articles):
        seen.append(articles)
        return {"picks": [picked("n_7"), picked("n_2", same=["n_3", "n_404"]), picked("n_404"),
                          picked("n_5", moves=["NDX", "BOGUS"]), picked("n_5")]}

    api = FakeAPI(pick=pick)
    client = llm_client.LLMClient(llm_client.TokenBudget(SETTINGS["llm_budget"]), api=api)
    articles = collection(12)
    articles = articles.model_copy(update={"articles": [
        a.model_copy(update={"source": f"S{i}"}) for i, a in enumerate(articles.articles)]})
    result = news_analyst.analyze(articles, None, SESSION, {**MODELS["news_analyst"], "max_concurrency": 1},
                                  SETTINGS, client, MODELS["news_picker"], {"NDX": "나스닥100 −5.12%"})
    assert result.picked == ["n_5", "n_2", "n_7"]
    by_id = {n.id: n for n in result.news}
    assert by_id["n_5"].moves_ko == ["나스닥100 −5.12%"] and by_id["n_2"].coverage == 2 and by_id["n_7"].coverage == 1
    assert all("source" in a and "eligible" in a for a in seen[0])
    picker_request = api.requests[-1]
    assert picker_request["messages"][0]["content"] == (config.ROOT / "prompts/news_picker.md").read_text(encoding="utf-8")
    assert json.loads(picker_request["messages"][1]["content"])["moves"] == [{"key": "NDX", "text": "나스닥100 −5.12%"}]
    report = assemble(SYMBOLS, RUN_DATE, SESSION, False, MarketSnapshot(indicators=report_ok().indicators),
                      {"M1": ModuleRun(status="SUCCEEDED")}, analysis=result)
    assert [n.id for n in report.news] == ["n_5", "n_2", "n_7"]
    html = rendered(tmp_path, report)
    assert "나스닥100 −5.12% 관련" in html and "2개 매체 보도" in html


def test_market_moves_skip_futures_and_format_from_data():
    snap = MarketSnapshot(indicators=report_ok().indicators)
    moves = news_analyst.market_moves(snap, MoversSnapshot(movers=[mover("NVDA", -9.26), mover("AAPL", 0.5)]), 1)
    assert "NQ" not in moves and "ES" not in moves and moves["NVDA"] == "NVDA −9.26%"
    assert "AAPL" not in moves and moves["US10Y"].endswith("bp")


def test_picker_failure_falls_back_to_importance_order():
    api = FakeAPI(pick=lambda articles: {"wrong": []})
    client = llm_client.LLMClient(llm_client.TokenBudget(SETTINGS["llm_budget"]), api=api)
    result = news_analyst.analyze(collection(12), None, SESSION, {**MODELS["news_analyst"], "max_concurrency": 1},
                                  SETTINGS, client, MODELS["news_picker"])
    assert result.picked == [] and result.failed_batches == 0
    assert any(note.startswith("picker failed") for note in result.notes)


# --- M9 with news -----------------------------------------------------------

def news_item(i, importance, summary=True):
    return NewsItem(
        id=f"n_{i}", category="macro_rates", importance=importance, importance_label_ko="x",
        title_ko=f"t{i}", preview_ko="", published_at=NEWS_START + timedelta(minutes=i),
        source="S", url="https://a.test", summary_ko=["a", "b", "c"] if summary else [],
    )


def test_display_filter_limit_and_order():
    items = [news_item(i, 3) for i in range(10)] + [news_item(20, 5), news_item(21, 2), news_item(22, 3, summary=False)]
    report = assemble(SYMBOLS, RUN_DATE, SESSION, False, MarketSnapshot(indicators=report_ok().indicators),
                      {"M1": ModuleRun(status="SUCCEEDED")}, analysis=NewsAnalysis(news=items))
    assert len(report.news) == SETTINGS["news"]["top_n_display"]
    assert report.news[0].id == "n_20"
    assert all(n.importance >= 3 and n.summary_ko for n in report.news)


def test_backfilled_actual_is_labeled_and_shown(tmp_path):
    analysis = NewsAnalysis(news=[], calendar_actuals=[CalendarActual(event="ISM Services PMI", actual="54.1", evidence_id="n")])
    report = assemble(SYMBOLS, RUN_DATE, SESSION, False, MarketSnapshot(indicators=report_ok().indicators),
                      {"M1": ModuleRun(status="SUCCEEDED")}, calendar=snapshot(), analysis=analysis)
    released = report.calendar.released[0]
    assert released.actual == "54.1" and released.actual_source == "llm_news"
    html = rendered(tmp_path, report)
    assert "54.1" in html and "실제치는 뉴스 기반" in html
    assert "신규 실업수당 청구건수" in html


@pytest.mark.live
def test_live_news_analyst():
    """Needs OPENAI_API_KEY. Run with: pytest -m live"""
    client = llm_client.LLMClient(llm_client.TokenBudget(SETTINGS["llm_budget"]))
    articles = NewsCollection(articles=news.parse_feed(RSS, FEED, NEWS_START, CUTOFF)[:3])
    result = news_analyst.analyze(articles, snapshot(), SESSION, MODELS["news_analyst"], SETTINGS, client)
    assert result.failed_batches == 0 and result.llm_usage


def test_run_before_us_close_is_timed_at_cutoff_not_close():
    early = datetime(2026, 11, 5, 5, 40, tzinfo=timezone(timedelta(hours=9)))  # US still open
    ndx = market.build_indicator(SPECS["NDX"], bars("^NDX", "1d"), [], SESSION, early, close_of)
    assert ndx.as_of == early


def test_api_key_whitespace_is_stripped_and_bad_keys_fail_fast(monkeypatch):
    budget = llm_client.TokenBudget(SETTINGS["llm_budget"])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key\n")
    assert llm_client.LLMClient(budget).api.api_key == "sk-test-key"
    for bad in ("  ", "sk-﻿test"):
        monkeypatch.setenv("OPENAI_API_KEY", bad)
        with pytest.raises(llm_client.LLMFatal):
            llm_client.LLMClient(budget)


def test_number_guard_alone_does_not_degrade_the_run(tmp_path, monkeypatch, fast):
    monkeypatch.setattr(market, "fetch_bars", fake_fetch)
    api = FakeAPI(respond=lambda articles: {
        "items": [good_item(a["id"], title="99% 급등") for a in articles], "calendar_actuals": [],
    })
    monkeypatch.setattr(run_daily, "LLMClient", lambda budget: llm_client.LLMClient(budget, api=api))
    assert run_daily.run(args(), tmp_path) == ("OK", 0)
    log = json.loads((tmp_path / "data/logs/2026-11-05.run.json").read_text(encoding="utf-8"))
    assert any("numbers not in article" in note for note in log["modules"]["M6"]["notes"])
