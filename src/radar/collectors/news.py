"""M2: collect RSS items, filter by time window, remove duplicates. No summaries, no LLM."""
import calendar as stdlib_calendar
import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from difflib import SequenceMatcher

import feedparser
import httpx

from radar.models import NewsCollection, RawArticle
from radar.retry import with_retry

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MorningRadar/1.0)"}
MAX_SUMMARY_CHARS = 300  # public repo: never store article bodies
SIMILAR_TITLE_RATIO = 0.9


def fetch_feed(url: str) -> bytes:
    response = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_feed(content: bytes, feed: dict, start: datetime, end: datetime) -> list[RawArticle]:
    """Entries inside [start, end]. Entries without a date or an http(s) link are dropped."""
    articles = []
    for entry in feedparser.parse(content).entries:
        link = entry.get("link", "")
        published = entry.get("published_parsed")
        if not published or not link.startswith(("https://", "http://")):
            continue
        published_at = datetime.fromtimestamp(stdlib_calendar.timegm(published), UTC)
        if not start <= published_at <= end:
            continue
        publisher = (entry.get("source") or {}).get("title")
        title = _plain(entry.get("title", ""))
        if publisher:
            title = title.removesuffix(f" - {publisher}").strip()
        summary = _plain(entry.get("summary", ""))
        if summary.startswith(title):  # Google News repeats the title as its summary
            summary = ""
        if not title:
            continue
        articles.append(RawArticle(
            id="n_" + hashlib.sha1(link.encode()).hexdigest()[:8],
            title=title,
            link=link,
            summary=summary[:MAX_SUMMARY_CHARS],
            source=publisher or feed["name"],
            feed=feed["name"],
            published_at=published_at,
        ))
    return articles


def deduplicate(articles: list[RawArticle]) -> list[RawArticle]:
    """Keeps the first occurrence, so callers sort by preference first."""
    kept: list[RawArticle] = []
    keys: list[str] = []
    links: set[str] = set()
    for article in articles:
        key = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).strip()
        if article.link in links or any(
            key == other or SequenceMatcher(None, key, other).ratio() >= SIMILAR_TITLE_RATIO for other in keys
        ):
            continue
        kept.append(article)
        keys.append(key)
        links.add(article.link)
    return kept


def collect(feeds_cfg: dict, start: datetime, end: datetime) -> NewsCollection:
    feeds = sorted((f for f in feeds_cfg["feeds"] if f["verified"]), key=lambda f: f["priority"])
    backoff = feeds_cfg["fetch_retry_backoff_s"]

    def one(feed: dict):
        try:
            content = with_retry(lambda: fetch_feed(feed["url"]), backoff)
            return parse_feed(content, feed, start, end), None
        except Exception as exc:
            return [], f"{feed['name']}: {type(exc).__name__}"

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(one, feeds))

    if not any(error is None for _, error in results):
        raise RuntimeError("all news feeds failed")

    articles: list[RawArticle] = []
    for found, _ in results:  # feeds are already in priority order
        articles += sorted(found, key=lambda a: a.published_at, reverse=True)
    unique = deduplicate(articles)[: feeds_cfg["max_articles_to_llm"]]

    notes = [error for _, error in results if error]
    if len(unique) < feeds_cfg["min_articles_warning"]:
        notes.append(f"only {len(unique)} articles collected")
    return NewsCollection(
        articles=unique,
        feeds_ok=[feed["name"] for feed, (_, error) in zip(feeds, results) if not error],
        notes=notes,
    )
