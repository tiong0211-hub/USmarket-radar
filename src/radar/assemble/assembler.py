"""M9: merge module outputs into the daily report (docs/data_contract.md)."""
from datetime import UTC, date, datetime

from radar import config
from radar.assemble.validator import check_report, run_status
from radar.models import (
    Banner, Calendar, CalendarSnapshot, DailyReport, Indicator, MarketSnapshot, ModuleRun,
    NewsAnalysis, NewsCollection, NewsItem, SourceInfo,
)

WEEKDAYS_KO = "월화수목금토일"
SOURCE_NAMES = {"yfinance": "yfinance", "fred": "FRED"}
MODULE_WARNINGS = {
    "M1": "시장 지표 수집 오류",
    "M2": "일부 뉴스 소스 수집 실패 또는 기사 수 부족",
    "M3": "캘린더 미수집",
    "M6": "뉴스 요약 일부 없음(제목만 표시)",
}


def _complete(symbols: dict, market: MarketSnapshot | None) -> list[Indicator]:
    """One entry per configured indicator, in order. Absent entries become missing."""
    collected = {i.key: i for i in (market.indicators if market else [])}
    return [
        collected.get(spec["key"]) or Indicator(
            key=spec["key"], label=spec["label"], kind=spec["kind"], symbol=spec["symbol"],
            group=spec["group"], required=spec["required"], unit=spec.get("unit"),
            source=spec["source"], missing=True, description_ko=spec.get("description_ko", ""),
        )
        for spec in symbols["indicators"]
    ]


def _displayed_news(analysis: NewsAnalysis | None, settings: dict) -> list[NewsItem]:
    """Importance filter and display limit. Analyzed items always rank above title-only ones."""
    if not analysis:
        return []
    cfg = settings["news"]
    if analysis.picked:  # picker already applied the filter and the limit, one card per event
        by_id = {n.id: n for n in analysis.news}
        return [by_id[i] for i in analysis.picked if i in by_id][: cfg["top_n_display"]]
    eligible = [n for n in analysis.news if n.importance >= cfg["min_importance_display"]]
    eligible.sort(key=lambda n: (bool(n.summary_ko), n.importance, n.published_at), reverse=True)
    return eligible[: cfg["top_n_display"]]


def _calendar(snapshot: CalendarSnapshot | None, analysis: NewsAnalysis | None) -> Calendar:
    if not snapshot:
        return Calendar(status="missing")
    actuals = {a.event: a.actual for a in analysis.calendar_actuals} if analysis else {}
    released = [
        e.model_copy(update={"actual": actuals[e.event], "actual_source": "llm_news"})
        if e.actual is None and e.event in actuals else e
        for e in snapshot.released
    ]
    return Calendar(status=snapshot.status, released=released, upcoming=snapshot.upcoming)


def _banner(status: str, closed: bool, warnings: list[str]) -> Banner | None:
    if status in ("DEGRADED", "FAILED_INDICATORS"):
        return Banner(level="warn", text_ko=f"일부 데이터를 가져오지 못했습니다: {', '.join(warnings)}")
    if closed:
        return Banner(level="info", text_ko="어젯밤 미국장은 휴장했습니다. 직전 거래일 기준입니다.")
    return None


def assemble(
    symbols: dict,
    run_date: date,
    us_session: date,
    us_market_closed: bool,
    market: MarketSnapshot | None,
    modules: dict[str, ModuleRun],
    news: NewsCollection | None = None,
    calendar: CalendarSnapshot | None = None,
    analysis: NewsAnalysis | None = None,
) -> DailyReport:
    indicators = _complete(symbols, market)
    warnings = [f"{i.label} {i.kind} 데이터 없음" for i in indicators if i.missing]
    warnings += [f"{i.label} {i.kind} 지연 데이터" for i in indicators if i.stale]
    for key, text in MODULE_WARNINGS.items():
        failed = key in modules and modules[key].status != "SUCCEEDED"
        if failed and not (key == "M1" and warnings):  # indicator warnings already name M1's gaps
            warnings.append(text)
    status = run_status(indicators, modules)

    sources: dict[str, list[str]] = {}
    for indicator in indicators:
        if not indicator.missing:
            sources.setdefault(SOURCE_NAMES.get(indicator.source, indicator.source), []).append(indicator.key)
    if calendar:
        sources["Forex Factory"] = ["calendar"]
    for feed in news.feeds_ok if news else []:
        sources[f"{feed} RSS"] = ["news"]

    report = DailyReport(
        run_date=run_date,
        weekday_ko=WEEKDAYS_KO[run_date.weekday()],
        us_session=us_session,
        us_market_closed=us_market_closed,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        run_status=status,
        banner=_banner(status, us_market_closed, warnings),
        indicators=indicators,
        calendar=_calendar(calendar, analysis),
        news=_displayed_news(analysis, config.load("settings")),
        sources=[SourceInfo(name=name, items=items) for name, items in sources.items()],
        warnings=warnings,
    )
    check_report(report, [s["key"] for s in symbols["indicators"]])
    return report
