"""M8: Hero briefing from the assembled draft report.

The model writes words; every number it uses must already be in the digest (M9 checks).
When no model output survives, template() builds a plain briefing from collected data.
"""
import json
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from radar.llm.client import LLMClient, LLMFatal, LLMRetryExhausted, load_prompt
from radar.models import Briefing, BriefingPoint, DailyReport, FocusChip

KST = ZoneInfo("Asia/Seoul")
MINUS = "−"


class _Point(BaseModel):
    title_ko: str
    text_ko: str


class _Output(BaseModel):
    direction: Literal["gap_up", "flat", "gap_down"]
    confidence: Literal["low", "medium", "high"]
    headline_ko: list[str]
    headline_emphasis: str
    points: list[_Point]
    focus_chips: list[str]
    risks_ko: list[str]


def digest(draft: DailyReport) -> dict:
    """Only what the model may cite. Numbers are rounded here so the check compares like with like."""
    return {
        "run_date": draft.run_date.isoformat(),
        "us_market_closed": draft.us_market_closed,
        "indicators": [
            {"key": i.key, "label": i.label, "kind": i.kind, "value": round(i.value, 2), "unit": i.unit,
             "change_pct": None if i.change_pct is None else round(i.change_pct, 2), "change_bp": i.change_bp}
            for i in draft.indicators if not i.missing
        ],
        "sentiment": draft.sentiment and {"score": draft.sentiment.score, "label": draft.sentiment.label,
                                          "vix": round(draft.sentiment.vix, 2)},
        "calendar_released": [
            {"event_ko": e.event_ko, "actual": e.actual, "forecast": e.forecast, "previous": e.previous}
            for e in draft.calendar.released if e.importance == "high"
        ],
        "calendar_upcoming": [
            {"event_ko": e.event_ko, "time_kst": e.time_utc.astimezone(KST).strftime("%H:%M"),
             "forecast": e.forecast, "previous": e.previous}
            for e in draft.calendar.upcoming
        ],
        "top_news": [
            {"title_ko": n.title_ko, "category": n.category, "importance": n.importance, "moves_ko": n.moves_ko,
             "kr_impact": n.kr_impact.model_dump() if n.kr_impact else None}
            for n in draft.news if n.summary_ko
        ],
        "themes": [{"us_ticker": t.us_ticker, "change_pct": round(t.change_pct, 2), "theme": t.theme}
                   for t in draft.themes],
    }


def _chips(labels: list[str], draft: DailyReport) -> list[FocusChip]:
    themes = {t.theme for t in draft.themes}
    return [FocusChip(label=label, anchor="#themes" if label in themes else "#news") for label in labels[:2]]


def write(draft: DailyReport, digest_: dict, settings: dict, client: LLMClient, primary: dict,
          fallback: dict) -> Briefing:
    """Primary model, then the fallback model. Raises LLMRetryExhausted/LLMFatal when both fail."""
    user = json.dumps(digest_, ensure_ascii=False)
    error: Exception | None = None
    for cfg in (primary, fallback):
        try:
            out = client.structured(cfg, "briefing_writer", load_prompt("briefing_writer"), user,
                                    _Output.model_validate)
        except (LLMFatal, LLMRetryExhausted) as exc:
            error = exc
            continue
        return Briefing(
            direction=out.direction, direction_ko=settings["briefing"]["direction_ko"][out.direction],
            confidence=out.confidence, headline_ko=out.headline_ko, headline_emphasis=out.headline_emphasis,
            points=[BriefingPoint(**p.model_dump()) for p in out.points], focus_chips=_chips(out.focus_chips, draft),
            risks_ko=out.risks_ko, source=cfg["model"],
        )
    raise error


def _pct(value: float) -> str:
    return f"{value:+.2f}%".replace("-", MINUS)


def template(draft: DailyReport, settings: dict) -> Briefing:
    """No model: facts from collected data only, no direction call."""
    by_key = {i.key: i for i in draft.indicators if not i.missing}
    focus = draft.themes[0].theme if draft.themes else "시장 지표"
    if draft.us_market_closed:
        headline = ["어젯밤 미국장은 휴장,", "직전 거래일 기준입니다."]
        emphasis = "휴장"
    elif "NDX" in by_key:
        word = "상승" if by_key["NDX"].change_pct >= 0 else "하락"
        headline = [f"미국 기술주 {word} 마감,", f"아침의 초점은 {focus}."]
        emphasis = focus
    else:
        headline = ["밤사이 미국 시장,", "지표부터 확인하세요."]
        emphasis = "지표"
    points = []
    market = [f"{by_key[k].label} {_pct(by_key[k].change_pct)}" for k in ("NDX", "SPX", "SOX") if k in by_key]
    if market:
        points.append(BriefingPoint(title_ko="지수 마감", text_ko=", ".join(market) + "."))
    rates = []
    if "US10Y" in by_key:
        rates.append(f"{by_key['US10Y'].label} {by_key['US10Y'].change_bp:+d}bp".replace("-", MINUS))
    if "USDKRW" in by_key:
        rates.append(f"{by_key['USDKRW'].label} {_pct(by_key['USDKRW'].change_pct)}")
    if rates:
        points.append(BriefingPoint(title_ko="금리·환율", text_ko=", ".join(rates) + "."))
    tonight = [e.event_ko for e in draft.calendar.upcoming]
    points.append(BriefingPoint(title_ko="오늘 밤 일정",
                                text_ko=(", ".join(tonight[:3]) + " 발표 예정.") if tonight else "고영향 지표 발표 없음."))
    chips = [FocusChip(label="시장 지표", anchor="#markets")]
    if draft.themes:
        chips.append(FocusChip(label=focus, anchor="#themes"))
    return Briefing(
        direction="flat", direction_ko=settings["briefing"]["template_direction_ko"], confidence="low",
        headline_ko=headline, headline_emphasis=emphasis, points=points, focus_chips=chips, source="template",
    )
