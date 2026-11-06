"""M9: required-indicator check and contract checks. Never edits values."""
import json
import re

from radar.models import Briefing, DailyReport, Indicator, ModuleRun, RunStatus


def missing_required(indicators: list[Indicator]) -> list[str]:
    return [i.key for i in indicators if i.required and i.missing]


def run_status(indicators: list[Indicator], modules: dict[str, ModuleRun]) -> RunStatus:
    if missing_required(indicators):
        return "FAILED_INDICATORS"
    if any(m.status != "SUCCEEDED" for m in modules.values()):
        return "DEGRADED"
    return "OK"


def check_report(report: DailyReport, expected_keys: list[str]) -> None:
    keys = [i.key for i in report.indicators]
    if keys != expected_keys:
        raise ValueError(f"indicator keys {keys} != {expected_keys}")
    briefing = report.briefing
    if briefing and not any(briefing.headline_emphasis in line for line in briefing.headline_ko):
        raise ValueError("headline_emphasis is not in headline_ko")


def _numbers(text: str) -> set[str]:
    """Numbers as written, normalized so "06" == "6" and "4.30" == "4.3"; thousands commas dropped."""
    found = set()
    for token in re.findall(r"\d+(?:[.,]\d+)*", text):
        token = token.replace(",", "")
        found.add(token.rstrip("0").rstrip(".") if "." in token else str(int(token)))
    return found


def briefing_problems(briefing: Briefing, digest: dict) -> list[str]:
    """M8 output must quote only digest numbers and keep the hero layout. Empty list = accept."""
    problems = []
    if len(briefing.headline_ko) != 2:
        problems.append("headline must have 2 lines")
    if not any(briefing.headline_emphasis and briefing.headline_emphasis in line for line in briefing.headline_ko):
        problems.append("headline_emphasis is not in headline_ko")
    if not 1 <= len(briefing.points) <= 3:
        problems.append("points must be 1-3")
    written = " ".join([*briefing.headline_ko, *(f"{p.title_ko} {p.text_ko}" for p in briefing.points),
                        *briefing.risks_ko, *(c.label for c in briefing.focus_chips)])
    unsupported = _numbers(written) - _numbers(json.dumps(digest, ensure_ascii=False))
    if unsupported:
        problems.append(f"numbers not in digest: {', '.join(sorted(unsupported))}")
    return problems
