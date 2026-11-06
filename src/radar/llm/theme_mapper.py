"""M7: map US movers to Korean stocks from config/theme_map.yaml only.

Unmapped movers that the news mentions get an LLM proposal, filed as a GitHub Issue
for a human to review. The pipeline never edits theme_map.yaml and never shows proposals.
"""
import json
import os
from datetime import date

import httpx
from pydantic import BaseModel

from radar.llm.client import LLMClient, LLMFatal, LLMRetryExhausted, load_prompt
from radar.models import (
    Indicator, KrStock, MoversSnapshot, NewsAnalysis, NewsItem, Theme, ThemeMapping, ThemeProposal,
)

GITHUB_API = "https://api.github.com"


def krx_listing() -> dict[str, str]:
    import FinanceDataReader as fdr  # slow import, only needed here

    frame = fdr.StockListing("KRX")
    return dict(zip(frame["Code"].astype(str), frame["Name"].astype(str)))


def _same_name(a: str, b: str) -> bool:
    return a.replace(" ", "").lower() == b.replace(" ", "").lower()


def _news_by_ticker(analysis: NewsAnalysis | None) -> dict[str, list[NewsItem]]:
    """Analyzed news per ticker, most important first."""
    found: dict[str, list[NewsItem]] = {}
    for item in sorted(analysis.news if analysis else [], key=lambda n: n.importance, reverse=True):
        if item.summary_ko:
            for ticker in item.us_tickers:
                found.setdefault(ticker.upper(), []).append(item)
    return found


def leads(change_pct: float, benchmark_pct: float | None, min_excess_pct: float) -> bool:
    """A leader rose and beat the index. Falling stocks never qualify, even on a crash day."""
    return benchmark_pct is not None and change_pct > 0 and change_pct - benchmark_pct >= min_excess_pct


def map_themes(
    theme_map: dict, movers: MoversSnapshot, analysis: NewsAnalysis | None,
    listing: dict[str, str] | None, max_display: int, benchmark_pct: float | None, min_excess_pct: float,
) -> tuple[list[Theme], list[str], list[str]]:
    """Returns (themes, excluded, notes). Only verified entries that rose and beat the
    benchmark by min_excess_pct are shown; without a benchmark nothing is shown.
    """
    by_symbol = {m.symbol: m for m in movers.movers}
    news = _news_by_ticker(analysis)
    themes: list[Theme] = []
    excluded: list[str] = []
    unverified = 0
    for entry in theme_map["themes"]:
        if not entry.get("verified"):
            unverified += 1
            continue
        mover = by_symbol.get(entry["us_ticker"])
        if mover is None:
            continue  # M4 notes already name the ticker
        if not leads(mover.change_pct, benchmark_pct, min_excess_pct):
            continue
        stocks = []
        for stock in entry["kr_stocks"]:
            if listing is not None and not _same_name(listing.get(stock["code"], ""), stock["name"]):
                excluded.append(f"{entry['us_ticker']} → {stock['code']} {stock['name']}")
                continue
            stocks.append(KrStock(code=stock["code"], name=stock["name"], link=stock["link"],
                                  relation_ko=stock["relation_ko"]))
        # Articles naming the company beat market-wide stories that only list the ticker.
        names = (entry["us_ticker"].lower(), entry["name"].split()[0].lower())
        related = sorted(news.get(entry["us_ticker"], []),
                         key=lambda n: not any(k in n.title_ko.lower() for k in names))
        themes.append(Theme(
            us_ticker=entry["us_ticker"], name=entry["name"], change_pct=mover.change_pct, theme=entry["theme"],
            momentum_ko=(related[0].preview_ko or related[0].title_ko) if related else "",
            related_news_ids=[n.id for n in related[:3]], kr_stocks=stocks,
        ))
    themes.sort(key=lambda t: t.change_pct, reverse=True)
    notes = [f"{unverified} theme_map entries not verified: hidden"] if unverified else []
    return themes[:max_display], excluded, notes


class _Proposals(BaseModel):
    proposals: list[ThemeProposal]


def propose(
    theme_map: dict, movers: MoversSnapshot, analysis: NewsAnalysis | None, listing: dict[str, str],
    cfg: dict, settings: dict, client: LLMClient, benchmark_pct: float | None,
) -> list[ThemeProposal]:
    """Unmapped stocks that moved and appear in the news. Candidates not in the KRX listing are dropped."""
    rules = settings["themes"]
    mapped = {t["us_ticker"] for t in theme_map["themes"]}
    news = _news_by_ticker(analysis)
    picked = sorted(
        (m for m in movers.movers if m.kind == "stock" and m.symbol not in mapped and m.symbol in news
         and m.change_pct >= rules["proposal_min_change_pct"]
         and leads(m.change_pct, benchmark_pct, rules["min_excess_pct"])),
        key=lambda m: m.change_pct, reverse=True,
    )[: rules["proposal_max_tickers"]]
    if not picked:
        return []
    user = json.dumps({
        "unmapped": [
            {"us_ticker": m.symbol, "name": m.name, "change_pct": m.change_pct,
             "news_context": [line for n in news[m.symbol][:2] for line in n.summary_ko]}
            for m in picked
        ],
        "existing_themes": sorted({t["theme"] for t in theme_map["themes"]}),
    }, ensure_ascii=False)
    output = client.structured(cfg, "theme_proposer", load_prompt("theme_proposer"), user, _Proposals.model_validate)
    by_symbol = {m.symbol: m for m in picked}
    code_of = {name.replace(" ", "").lower(): code for code, name in listing.items()}
    proposals = []
    for proposal in output.proposals:
        mover = by_symbol.get(proposal.us_ticker)
        if mover is None:
            continue
        candidates = []
        for candidate in proposal.kr_candidates[:3]:
            if candidate.confidence == "low":
                continue
            code = code_of.get(candidate.name.replace(" ", "").lower())  # the name fixes a wrong code
            if code:
                candidates.append(candidate.model_copy(update={"code": code, "name": listing[code]}))
        if candidates:
            proposals.append(proposal.model_copy(update={
                "name": mover.name, "change_pct": mover.change_pct, "kr_candidates": candidates,
                "news_ids": [n.id for n in news[mover.symbol][:3]],
            }))
    return proposals


def issue_body(proposal: ThemeProposal, run_date: date, news_urls: dict[str, str]) -> str:
    lines = [
        f"자동 생성된 테마 매핑 **제안**입니다 ({run_date}). 화면에는 표시되지 않습니다.",
        "검토 후 `config/theme_map.yaml`에 직접 반영하고(`verified: true`) 이 Issue를 닫아 주세요.",
        "",
        f"- 종목: **{proposal.us_ticker}** {proposal.name} ({proposal.change_pct:+.2f}%)",
        f"- 테마: {proposal.theme}{' (신규)' if proposal.is_new_theme else ''}",
        "",
        "### 국내 후보 (KRX 상장 목록으로 확인된 종목만)",
    ]
    lines += [
        f"- {c.name} `{c.code}` · {c.link_type} · 확신도 {c.confidence}: {c.rationale_ko}"
        for c in proposal.kr_candidates
    ]
    lines += ["", "### 근거 뉴스"] + [f"- {news_urls[i]}" for i in proposal.news_ids if i in news_urls]
    lines += ["", "_LLM 제안이며 사업 관계를 확인하지 않았습니다. 투자 권유가 아닙니다._"]
    return "\n".join(lines)


def file_issues(proposals: list[ThemeProposal], run_date: date, news_urls: dict[str, str], label: str) -> list[str]:
    """One Issue per ticker; an open Issue for the same ticker gets a comment instead. Returns notes."""
    token, repo = os.environ.get("GITHUB_TOKEN", "").strip(), os.environ.get("GITHUB_REPOSITORY", "")
    if not (token and repo):
        return ["issues skipped: GITHUB_TOKEN or GITHUB_REPOSITORY not set"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    notes = []
    with httpx.Client(base_url=f"{GITHUB_API}/repos/{repo}", headers=headers, timeout=20) as gh:
        response = gh.get("/issues", params={"state": "open", "labels": label, "per_page": 100})
        response.raise_for_status()
        open_issues = {i["title"]: i["number"] for i in response.json()}
        for proposal in proposals:
            title = f"[{label}] {proposal.us_ticker}"
            body = issue_body(proposal, run_date, news_urls)
            if title in open_issues:
                gh.post(f"/issues/{open_issues[title]}/comments", json={"body": body}).raise_for_status()
                notes.append(f"{proposal.us_ticker}: commented on #{open_issues[title]}")
            else:
                created = gh.post("/issues", json={"title": title, "body": body, "labels": [label]})
                created.raise_for_status()
                notes.append(f"{proposal.us_ticker}: opened #{created.json()['number']}")
    return notes


def run(
    theme_map: dict, movers: MoversSnapshot, analysis: NewsAnalysis | None, run_date: date,
    settings: dict, client: LLMClient | None, cfg: dict, file: bool, benchmark: Indicator | None,
) -> ThemeMapping:
    """client=None skips proposals (--no-llm). file=False keeps proposals local (--dry-run).
    benchmark is the index from M1; when it is missing no theme is shown.
    """
    rules = settings["themes"]
    benchmark_pct = None if benchmark is None or benchmark.missing else benchmark.change_pct
    notes: list[str] = [] if benchmark_pct is not None else ["benchmark missing: no themes shown"]
    try:
        listing = krx_listing()
    except Exception as exc:
        listing = None
        notes.append(f"KRX listing unavailable ({type(exc).__name__}): codes not checked, proposals skipped")
    themes, excluded, map_notes = map_themes(
        theme_map, movers, analysis, listing, settings["ui"]["themes_display_max"],
        benchmark_pct, rules["min_excess_pct"],
    )
    notes += map_notes
    proposals: list[ThemeProposal] = []
    if client is not None and listing is not None:
        try:
            proposals = propose(theme_map, movers, analysis, listing, cfg, settings, client, benchmark_pct)
        except (LLMFatal, LLMRetryExhausted) as exc:
            notes.append(f"proposals skipped: {exc}")
    if proposals and file:
        news_urls = {n.id: n.url for n in analysis.news} if analysis else {}
        try:
            notes += file_issues(proposals, run_date, news_urls, settings["themes"]["issue_label"])
        except Exception as exc:  # proposals stay in data/proposals; type only, logs are public
            notes.append(f"issues failed: {type(exc).__name__}")
    rule_ko = (f"{benchmark.label}({benchmark_pct:+.2f}%)보다 {rules['min_excess_pct']:g}%p 이상 오른 종목만"
               .replace("-", "−") if benchmark_pct is not None else "")
    return ThemeMapping(themes=themes, proposals=proposals, listing_checked=listing is not None, rule_ko=rule_ko,
                        excluded=excluded, notes=notes)
