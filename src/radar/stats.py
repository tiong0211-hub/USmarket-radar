"""P5 report: python -m radar.stats [--since YYYY-MM-DD]

Reads data/logs/*.run.json (one per date, last run wins) and prints the daily table and
success rates. Read-only.
"""
import argparse
from collections import Counter
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from radar import config
from radar.models import RunLog

KST = ZoneInfo("Asia/Seoul")


def load(data_dir: Path, since: date | None = None) -> list[RunLog]:
    logs = [RunLog.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted((data_dir / "logs").glob("*.run.json"))]
    return [log for log in logs if since is None or log.run_date >= since]


def report(logs: list[RunLog]) -> str:
    days = [log for log in logs if log.run_status != "SKIPPED_HOLIDAY"]
    lines = ["date        status             start(KST)  not succeeded"]
    for log in days:
        bad = [f"{k}:{m.status}" for k, m in log.modules.items() if m.status != "SUCCEEDED"]
        start = log.started_at.astimezone(KST).strftime("%m-%d %H:%M")
        lines.append(f"{log.run_date}  {log.run_status:<18} {start}  {', '.join(bad) or '-'}")
    if not days:
        return "no runs"
    published = sum(log.run_status in ("OK", "DEGRADED") for log in days)
    late = sum(log.started_at.astimezone(KST).date() != log.run_date
               or log.started_at.astimezone(KST).hour >= 8 for log in days)
    lines += ["", f"published (OK/DEGRADED): {published}/{len(days)} = {published / len(days):.0%}",
              f"started late (>=08:00 KST or another day): {late}/{len(days)}"]
    total, succeeded = Counter(), Counter()
    for log in days:
        for key, module in log.modules.items():
            total[key] += 1
            succeeded[key] += module.status == "SUCCEEDED"
    lines.append("module success: " + "  ".join(
        f"{k} {succeeded[k]}/{total[k]}" for k in sorted(total, key=lambda k: (len(k), k))))
    missing = Counter(key for log in days for key in log.indicators_missing)
    lines.append("missing indicators: " + (", ".join(f"{k} x{n}" for k, n in missing.most_common()) or "none"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar.stats")
    parser.add_argument("--since", type=date.fromisoformat)
    args = parser.parse_args(argv)
    print(report(load(config.ROOT / "data", args.since)))


if __name__ == "__main__":
    main()
