"""Pydantic models for docs/data_contract.md (schema_version 1.1) and the run log."""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, model_validator

RunStatus = Literal["SKIPPED_HOLIDAY", "OK", "DEGRADED", "FAILED_INDICATORS", "FAILED"]
ModuleStatus = Literal["SUCCEEDED", "DEGRADED", "FAILED", "SKIPPED"]


class Indicator(BaseModel):
    key: str
    label: str
    kind: str
    symbol: str
    group: str
    required: bool
    value: float | None = None
    unit: str | None = None
    change: float | None = None
    change_pct: float | None = None
    change_bp: int | None = None
    contract: str | None = None
    spark: list[float] = []
    as_of: AwareDatetime | None = None
    source: str
    stale: bool = False
    missing: bool = False
    description_ko: str = ""

    @model_validator(mode="after")
    def _contract_rules(self):
        if self.missing:
            if self.value is not None:
                raise ValueError(f"{self.key}: missing indicator carries a value")
            return self
        if self.value is None or self.change is None or self.as_of is None:
            raise ValueError(f"{self.key}: value, change and as_of are required")
        if self.group == "rates":
            if self.change_bp is None or self.change_pct is not None:
                raise ValueError(f"{self.key}: rates need change_bp and no change_pct")
        elif self.change_pct is None:
            raise ValueError(f"{self.key}: change_pct is required")
        if self.group == "futures" and not self.contract:
            raise ValueError(f"{self.key}: futures need contract")
        return self


class MarketSnapshot(BaseModel):
    """M1 output (data/raw/<date>/market.json)."""
    indicators: list[Indicator]
    notes: list[str] = []


class Banner(BaseModel):
    level: Literal["info", "warn", "error"]
    text_ko: str


class Sentiment(BaseModel):
    source: str
    vix: float
    score: int
    label: str
    label_en: str
    method: str
    note_ko: str


class ReleasedEvent(BaseModel):
    time_utc: AwareDatetime
    event: str
    event_ko: str
    importance: str
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None
    actual_source: str | None = None
    tag_ko: str | None = None


class UpcomingEvent(BaseModel):
    time_utc: AwareDatetime
    event: str
    event_ko: str
    importance: str
    forecast: str | None = None
    previous: str | None = None
    tag_ko: str | None = None


class Calendar(BaseModel):
    released: list[ReleasedEvent] = []
    upcoming: list[UpcomingEvent] = []
    status: Literal["ok", "missing"] = "missing"


class CalendarSnapshot(Calendar):
    """M3 output (data/raw/<date>/calendar.json). events carries Friday's releases into Monday."""
    events: list[UpcomingEvent] = []


class KrImpact(BaseModel):
    direction: Literal["positive", "negative", "neutral"]
    text: str


class NewsItem(BaseModel):
    id: str
    category: Literal["macro_rates", "theme_earnings", "geopolitics_commodities", "other"]
    importance: int
    importance_label_ko: str
    title_ko: str
    preview_ko: str
    published_at: AwareDatetime
    source: str
    url: str
    summary_ko: list[str] = []
    kr_impact: KrImpact | None = None
    us_tickers: list[str] = []


class RawArticle(BaseModel):
    """Title, link and RSS summary (<=300 chars) only: data/raw is public."""
    id: str
    title: str
    link: str
    summary: str = ""
    source: str
    feed: str
    published_at: AwareDatetime


class NewsCollection(BaseModel):
    """M2 output (data/raw/<date>/news.json)."""
    articles: list[RawArticle]
    feeds_ok: list[str] = []
    notes: list[str] = []


class CalendarActual(BaseModel):
    event: str
    actual: str
    evidence_id: str


class NewsAnalysis(BaseModel):
    """M6 output (data/raw/<date>/news_analysis.json)."""
    news: list[NewsItem]
    calendar_actuals: list[CalendarActual] = []
    total_batches: int = 0
    failed_batches: int = 0
    picked: list[str] = []  # display order chosen by the picker; empty means M9 sorts by importance
    notes: list[str] = []
    llm_usage: dict[str, int] = {}


class KrStock(BaseModel):
    code: str
    name: str
    link: str
    relation_ko: str


class Theme(BaseModel):
    us_ticker: str
    name: str
    change_pct: float
    theme: str
    momentum_ko: str = ""
    related_news_ids: list[str] = []
    kr_stocks: list[KrStock] = []
    mapping_source: str = "theme_map.yaml"


class BriefingPoint(BaseModel):
    title_ko: str
    text_ko: str


class FocusChip(BaseModel):
    label: str
    anchor: str


class Briefing(BaseModel):
    direction: Literal["gap_up", "flat", "gap_down"]
    direction_ko: str
    confidence: Literal["low", "medium", "high"]
    headline_ko: list[str]
    headline_emphasis: str
    points: list[BriefingPoint]
    focus_chips: list[FocusChip] = []
    risks_ko: list[str] = []
    source: str
    label: str = "참고용"


class SourceInfo(BaseModel):
    name: str
    items: list[str]


class DailyReport(BaseModel):
    schema_version: str = "1.1"
    run_date: date
    weekday_ko: str
    us_session: date
    us_market_closed: bool
    generated_at: AwareDatetime
    run_status: RunStatus
    banner: Banner | None = None
    indicators: list[Indicator]
    sentiment: Sentiment | None = None
    calendar: Calendar = Calendar()
    news: list[NewsItem] = []
    themes: list[Theme] = []
    briefing: Briefing | None = None
    sources: list[SourceInfo] = []
    warnings: list[str] = []


class ModuleRun(BaseModel):
    status: ModuleStatus
    attempts: int = 1
    duration_s: float = 0.0
    error: str | None = None
    notes: list[str] = []


class RunLog(BaseModel):
    run_date: date
    us_session: date | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    run_status: RunStatus
    modules: dict[str, ModuleRun] = {}
    indicators_missing: list[str] = []
    llm_usage: dict[str, int] = {}
