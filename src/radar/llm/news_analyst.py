"""M6: classify and summarize articles, backfill calendar actuals.

Links, sources and timestamps always come from M2, never from the model.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Literal

from pydantic import BaseModel

from radar.llm.client import LLMClient, LLMFatal, LLMRetryExhausted, load_prompt
from radar.models import (
    CalendarActual, CalendarSnapshot, KrImpact, NewsAnalysis, NewsCollection, NewsItem, RawArticle,
)

log = logging.getLogger(__name__)
NO_SUMMARY_LABEL = "요약 없음"


class _Item(BaseModel):
    id: str
    relevant: bool
    category: Literal["macro_rates", "theme_earnings", "geopolitics_commodities", "other"]
    importance: int
    title_ko: str
    preview_ko: str
    summary_ko: list[str]
    kr_impact: KrImpact
    us_tickers: list[str]


class _Output(BaseModel):
    items: list[_Item]
    calendar_actuals: list[CalendarActual]


class _Picks(BaseModel):
    ids: list[str]


def pick(news: list[NewsItem], cfg: dict, settings: dict, client: LLMClient) -> tuple[list[str], str | None]:
    """One event per card: batches rate the same story independently, so dedupe across them.
    Returns ([], note) when picking is unnecessary or fails; M9 then sorts by importance.
    """
    top_n = settings["news"]["top_n_display"]
    candidates = [n for n in news if n.summary_ko and n.importance >= settings["news"]["min_importance_display"]]
    if len(candidates) <= top_n:
        return [], None
    user = json.dumps({"top_n": top_n, "articles": [
        {"id": n.id, "title_ko": n.title_ko, "preview_ko": n.preview_ko, "category": n.category,
         "importance": n.importance, "us_tickers": n.us_tickers}
        for n in candidates
    ]}, ensure_ascii=False)
    try:
        ids = client.structured(cfg, "news_picker", load_prompt("news_picker"), user, _Picks.model_validate).ids
    except (LLMFatal, LLMRetryExhausted) as exc:
        return [], f"picker failed: {exc}"
    known = {n.id for n in candidates}
    picked = list(dict.fromkeys(i for i in ids if i in known))[:top_n]
    return picked, None if picked else "picker returned no known ids"


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", text)}


def unsupported_numbers(item: _Item, article: RawArticle) -> set[str]:
    """Numbers the model wrote that the article does not contain."""
    written = " ".join([item.title_ko, item.preview_ko, *item.summary_ko, item.kr_impact.text])
    return numbers_in(written) - numbers_in(f"{article.title} {article.summary}")


def title_only(article: RawArticle) -> NewsItem:
    """Fallback card. Importance 3 is only the minimum that the contract displays;
    the label says the article was not analyzed and M9 sorts it after analyzed items.
    """
    return NewsItem(
        id=article.id, category="other", importance=3, importance_label_ko=NO_SUMMARY_LABEL,
        title_ko=article.title, preview_ko="", published_at=article.published_at,
        source=article.source, url=article.link,
    )


def _merge(item: _Item, article: RawArticle, labels: dict) -> NewsItem:
    return NewsItem(
        id=article.id, category=item.category, importance=item.importance,
        importance_label_ko=labels.get(item.importance, ""),
        title_ko=item.title_ko, preview_ko=item.preview_ko, published_at=article.published_at,
        source=article.source, url=article.link, summary_ko=item.summary_ko,
        kr_impact=item.kr_impact, us_tickers=item.us_tickers,
    )


def skipped(collection: NewsCollection, note: str) -> NewsAnalysis:
    return NewsAnalysis(news=[title_only(a) for a in collection.articles], notes=[note])


def analyze(
    collection: NewsCollection, calendar: CalendarSnapshot | None, us_session: date,
    cfg: dict, settings: dict, client: LLMClient, picker_cfg: dict | None = None,
) -> NewsAnalysis:
    articles = collection.articles
    size = cfg["batch_size"]
    batches = [articles[i:i + size] for i in range(0, len(articles), size)]
    released = calendar.released if calendar else []
    calendar_input = [
        {"event": e.event, "time_utc": e.time_utc.isoformat(), "forecast": e.forecast,
         "previous": e.previous, "actual": e.actual}
        for e in released
    ]
    system = load_prompt("news_analyst")
    labels = settings["news"]["importance_labels"]
    fatal: list[str] = []

    def one(batch: list[RawArticle]) -> tuple[_Output | None, str | None]:
        if fatal:
            return None, "skipped after fatal error"
        user = json.dumps({
            "us_session": us_session.isoformat(),
            "calendar": calendar_input,
            "articles": [
                {"id": a.id, "title": a.title, "summary": a.summary, "source": a.source,
                 "published_at": a.published_at.isoformat()}
                for a in batch
            ],
        }, ensure_ascii=False)
        try:
            return client.structured(cfg, "news_analyst", system, user, _Output.model_validate), None
        except LLMFatal as exc:
            fatal.append(str(exc))
            return None, str(exc)
        except LLMRetryExhausted as exc:
            return None, str(exc)

    with ThreadPoolExecutor(cfg["max_concurrency"]) as pool:
        results = list(pool.map(one, batches))

    by_id = {a.id: a for a in articles}
    known_events = {e.event for e in released}
    news: list[NewsItem] = []
    actuals: dict[str, CalendarActual] = {}
    notes = sorted({f"batch failed: {error}" for _, error in results if error})
    dropped = 0
    for batch, (output, _) in zip(batches, results):
        items = {item.id: item for item in output.items} if output else {}
        for article in batch:
            item = items.get(article.id)
            if item is None:
                news.append(title_only(article))
            elif not item.relevant:
                continue
            elif unsupported_numbers(item, article):
                dropped += 1
                news.append(title_only(article))
            else:
                news.append(_merge(item, article, labels))
        for actual in output.calendar_actuals if output else []:
            evidence = by_id.get(actual.evidence_id)
            if (
                evidence and actual.event in known_events and numbers_in(actual.actual)
                and numbers_in(actual.actual) <= numbers_in(f"{evidence.title} {evidence.summary}")
            ):
                actuals.setdefault(actual.event, actual)
    if dropped:
        notes.append(f"{dropped} summaries replaced by titles: numbers not in article")
    picked: list[str] = []
    if picker_cfg and not fatal:
        picked, note = pick(news, picker_cfg, settings, client)
        notes += [note] if note else []
    return NewsAnalysis(
        news=news, calendar_actuals=list(actuals.values()), total_batches=len(batches), picked=picked,
        failed_batches=sum(1 for output, _ in results if output is None), notes=notes,
        llm_usage=dict(client.budget.used),
    )
