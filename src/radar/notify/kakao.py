"""M13: send today's page link to the owner's own KakaoTalk ("나에게 보내기").

python -m radar.notify.kakao --date YYYY-MM-DD
Env: KAKAO_REST_API_KEY, KAKAO_REFRESH_TOKEN, KAKAO_CLIENT_SECRET (optional),
KAKAO_ROTATED_TOKEN_FILE (optional): when Kakao issues a new refresh token (it does so once the
current one has a month or less left), it is written there and the workflow saves it as the
KAKAO_REFRESH_TOKEN secret. Daily runs therefore keep the token alive indefinitely.
Runs after deploy as its own job: a failure here never blocks the page.
Errors log the type and Kakao error_code only (logs are public).
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from radar import config
from radar.models import DailyReport

log = logging.getLogger("radar.notify.kakao")
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
STATUS_KO = {"OK": "", "DEGRADED": " · 일부 데이터 누락", "FAILED_INDICATORS": " · 필수 지표 누락",
             "FAILED": " · 생성 실패(어제 페이지 유지)"}


class KakaoError(Exception):
    pass


def _check(response: httpx.Response) -> dict:
    body = response.json() if response.content else {}
    if response.status_code != 200:
        raise KakaoError(f"HTTP {response.status_code} {body.get('error_code') or body.get('code') or ''}".strip())
    return body


def access_token(client: httpx.Client, rest_key: str, refresh_token: str, secret: str) -> tuple[str, str | None, int | None]:
    """Returns (access token, new refresh token if Kakao rotated it, its lifetime in days)."""
    data = {"grant_type": "refresh_token", "client_id": rest_key, "refresh_token": refresh_token}
    if secret:
        data["client_secret"] = secret
    body = _check(client.post(TOKEN_URL, data=data))
    left = body.get("refresh_token_expires_in")
    return body["access_token"], body.get("refresh_token"), (left // 86400 if left is not None else None)


def message(report: DailyReport | None, run_date: date, url: str, renew_days: int | None) -> dict:
    weekday = "월화수목금토일"[run_date.weekday()]
    lines = [f"{run_date.month}월 {run_date.day}일 ({weekday}) 모닝 레이더"]
    if report is None:
        lines.append("오늘 페이지 데이터가 없습니다.")
    else:
        lines[0] += STATUS_KO.get(report.run_status, "")
        if report.briefing:
            lines.append(" ".join(report.briefing.headline_ko))
            lines.append(f"오늘 국장은? {report.briefing.direction_ko} (참고용)")
    if renew_days is not None:
        lines.append(f"⚠ 카카오 토큰 자동 갱신 실패: 기존 토큰은 곧 만료됩니다. refresh token을 새로 받아 Secret을 갱신하세요.")
    text = "\n".join(lines)[:200]  # Kakao text template limit
    return {"object_type": "text", "text": text, "link": {"web_url": url, "mobile_web_url": url},
            "button_title": "브리핑 보기"}


def send(report: DailyReport | None, run_date: date, url: str, env: dict, client: httpx.Client) -> None:
    token, rotated, days = access_token(client, env["KAKAO_REST_API_KEY"].strip(), env["KAKAO_REFRESH_TOKEN"].strip(),
                                        env.get("KAKAO_CLIENT_SECRET", "").strip())
    renew_days = None
    if rotated:
        target = env.get("KAKAO_ROTATED_TOKEN_FILE", "")
        if target:
            with open(target, "w", encoding="utf-8") as f:  # never logged; the next step stores it as a secret
                f.write(rotated)
        else:
            renew_days = days  # nowhere to save it: tell the owner to renew by hand
    template = message(report, run_date, url, renew_days)
    _check(client.post(MEMO_URL, headers={"Authorization": f"Bearer {token}"},
                       data={"template_object": json.dumps(template, ensure_ascii=False)}))


def main(argv: list[str] | None = None) -> int:
    settings = config.load("settings")
    parser = argparse.ArgumentParser(prog="radar.notify.kakao")
    parser.add_argument("--date", type=date.fromisoformat)
    args = parser.parse_args(argv)
    run_date = args.date or datetime.now(ZoneInfo(settings["timezone_display"])).date()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (os.environ.get("KAKAO_REST_API_KEY") and os.environ.get("KAKAO_REFRESH_TOKEN")):
        log.info("Kakao secrets not set: skipped")
        return 0
    path = config.ROOT / "data" / "daily" / f"{run_date}.json"
    report = DailyReport.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
    try:
        with httpx.Client(timeout=20) as client:
            send(report, run_date, settings["site_url"], os.environ, client)
    except (KakaoError, httpx.HTTPError, KeyError, ValueError) as exc:
        log.error("Kakao send failed: %s %s", type(exc).__name__, exc if isinstance(exc, KakaoError) else "")
        return 1
    log.info("Kakao message sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
