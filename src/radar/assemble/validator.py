"""M9: required-indicator check and contract checks. Never edits values."""
from radar.models import DailyReport, Indicator, ModuleRun, RunStatus


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
