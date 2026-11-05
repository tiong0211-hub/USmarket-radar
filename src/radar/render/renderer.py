"""M10: data/daily/*.json to a static site. No data shaping and no external calls."""
import logging
import math
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from radar.models import Banner, DailyReport

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
MINUS = "−"
FAILED_BANNER = Banner(level="error", text_ko="오늘 브리핑 생성에 실패해 어제 내용을 표시합니다.")


def number(value: float | None, decimals: int = 2) -> str:
    return "데이터 없음" if value is None else f"{value:,.{decimals}f}".replace("-", MINUS)


def signed(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "데이터 없음"
    return f"{value:+,.{decimals}f}".replace("-", MINUS)


def direction(value: float | None) -> str:
    return "" if not value else ("up" if value > 0 else "down")


def arrow(value: float | None) -> str:
    return "" if not value else ("▲" if value > 0 else "▼")


def kst(value: datetime | None, fmt: str = "%m/%d %H:%M") -> str:
    return "" if value is None else value.astimezone(KST).strftime(fmt)


def et(value: datetime | None, fmt: str = "%H:%M") -> str:
    return "" if value is None else value.astimezone(ET).strftime(fmt)


def spark_path(values: list[float], width: int = 86, height: int = 24) -> str:
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = (width - 2) / (len(values) - 1)
    points = (
        f"{1 + i * step:.1f} {height - 2 - (v - low) / span * (height - 4):.1f}"
        for i, v in enumerate(values)
    )
    return "M" + " L".join(points)


def gauge_point(score: int) -> tuple[float, float]:
    angle = math.pi * (1 - score / 100)
    return round(120 + 92 * math.cos(angle), 1), round(118 - 92 * math.sin(angle), 1)


def safe_url(url: str) -> str:
    """Feed-supplied links must not become javascript: hrefs."""
    return url if url.startswith(("https://", "http://")) else "#"


def safe_anchor(anchor: str) -> str:
    return anchor if anchor.startswith("#") else "#overview"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        number=number, signed=signed, direction=direction, arrow=arrow, kst=kst, et=et,
        spark_path=spark_path, safe_url=safe_url, safe_anchor=safe_anchor,
    )
    env.globals["gauge_point"] = gauge_point
    env.globals["css"] = (HERE / "static" / "style.css").read_text(encoding="utf-8")
    return env


def load_reports(data_dir: Path, exclude: date | None = None) -> dict[date, DailyReport]:
    reports = {}
    for path in sorted((data_dir / "daily").glob("*.json")):
        try:
            report = DailyReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            log.warning("skipping unreadable %s: %s", path.name, exc)
            continue
        if report.run_date != exclude:
            reports[report.run_date] = report
    return reports


def render_site(data_dir: Path, site_dir: Path, settings: dict, keep_previous_for: date | None = None) -> None:
    """Rebuild the whole site: a Pages artifact replaces the previous deployment.

    keep_previous_for: today's run failed, so show the newest earlier page with the error banner.
    """
    reports = load_reports(data_dir, exclude=keep_previous_for)
    if not reports:
        raise FileNotFoundError(f"no daily reports in {data_dir / 'daily'}")

    env = _environment()
    page = env.get_template("index.html.j2")
    dates = sorted(reports)
    shutil.rmtree(site_dir, ignore_errors=True)
    (site_dir / "archive").mkdir(parents=True)

    og_rel = settings["ui"]["og_image"].removeprefix("site/")
    common = {"settings": settings, "og_image": settings["site_url"] + og_rel}

    def write(path: Path, report: DailyReport, prefix: str, banner: Banner | None) -> None:
        earlier = [d for d in dates if d < report.run_date]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            page.render(
                r=report, banner=banner, prefix=prefix,
                yesterday=earlier[-1].isoformat() if earlier else None, **common,
            ),
            encoding="utf-8",
        )

    latest = reports[dates[-1]]
    write(site_dir / "index.html", latest, "", FAILED_BANNER if keep_previous_for else latest.banner)
    for day, report in reports.items():
        write(site_dir / day.isoformat() / "index.html", report, "../", report.banner)
    (site_dir / "archive" / "index.html").write_text(
        env.get_template("archive.html.j2").render(
            reports=[reports[d] for d in reversed(dates)], **common
        ),
        encoding="utf-8",
    )
    target = site_dir / og_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "static" / "og.png", target)
