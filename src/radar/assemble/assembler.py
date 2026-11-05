"""M9: merge module outputs into the daily report (docs/data_contract.md)."""
from datetime import UTC, date, datetime

from radar.assemble.validator import check_report, run_status
from radar.models import Banner, DailyReport, Indicator, MarketSnapshot, ModuleRun, SourceInfo

WEEKDAYS_KO = "월화수목금토일"
SOURCE_NAMES = {"yfinance": "yfinance", "fred": "FRED"}


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
) -> DailyReport:
    indicators = _complete(symbols, market)
    warnings = [f"{i.label} {i.kind} 데이터 없음" for i in indicators if i.missing]
    warnings += [f"{i.label} {i.kind} 지연 데이터" for i in indicators if i.stale]
    status = run_status(indicators, modules)

    sources: dict[str, list[str]] = {}
    for indicator in indicators:
        if not indicator.missing:
            sources.setdefault(SOURCE_NAMES.get(indicator.source, indicator.source), []).append(indicator.key)

    report = DailyReport(
        run_date=run_date,
        weekday_ko=WEEKDAYS_KO[run_date.weekday()],
        us_session=us_session,
        us_market_closed=us_market_closed,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        run_status=status,
        banner=_banner(status, us_market_closed, warnings or ["시장 지표 수집 오류"]),
        indicators=indicators,
        sources=[SourceInfo(name=name, items=items) for name, items in sources.items()],
        warnings=warnings,
    )
    check_report(report, [s["key"] for s in symbols["indicators"]])
    return report
