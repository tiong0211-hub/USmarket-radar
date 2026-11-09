"""M12: "오늘 국장은?" — KOSPI opening-gap direction from overnight US moves. Rule, no LLM.

Scoring and calibration: docs/outlook_scoring.md.
Daily: compute() applies the coefficients in data/outlook_model.json (no network).
Monthly: `python -m radar.analytics.outlook` refits them on recent history and rewrites that file.
"""
import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from radar import config
from radar.models import MarketSnapshot, MoversSnapshot, Outlook

log = logging.getLogger("radar.analytics.outlook")
MODEL_PATH = config.ROOT / "data" / "outlook_model.json"
MINUS = "−"


def changes(snapshot: MarketSnapshot, movers: MoversSnapshot | None, features: list[str]) -> dict[str, float]:
    """Overnight % change per feature: M1 indicator by key, else M4 mover by symbol."""
    indicators = {i.key: i.change_pct for i in snapshot.indicators if not i.missing and i.change_pct is not None}
    moved = {m.symbol: m.change_pct for m in movers.movers} if movers else {}
    out = {}
    for key in features:
        value = indicators.get(key, moved.get(key))
        if value is None:
            raise ValueError(f"{key} missing")
        out[key] = value
    return out


def predict(model: dict, x: dict[str, float]) -> float:
    return model["intercept"] + sum(model["coef"][k] * x[k] for k in model["coef"])


def classify(score: float, cfg: dict) -> tuple[str, bool]:
    if abs(score) < cfg["flat_band_pct"]:
        return "flat", False
    return ("gap_up" if score > 0 else "gap_down"), abs(score) >= cfg["strong_pct"]


def _pct(value: float) -> str:
    return f"{value:+.2f}%".replace("-", MINUS)


def compute(snapshot: MarketSnapshot, movers: MoversSnapshot | None, model: dict, cfg: dict) -> Outlook:
    x = changes(snapshot, movers, cfg["features"])
    direction, strong = classify(predict(model, x), cfg)
    test = model["test"]
    hit, days = ((None, 0) if direction == "flat" else
                 (test["strong_hit_rate"], test["strong_days"]) if strong else (test["hit_rate"], test["days"]))
    return Outlook(
        direction=direction, strong=strong, hit_rate=hit, test_days=days,
        basis_ko=" · ".join(f"{cfg['labels'][k]} {_pct(x[k])}" for k in cfg["features"]),
        calibrated=date.fromisoformat(model["calibrated"]), method=model["method"], note_ko=cfg["note_ko"],
    )


# --- monthly calibration -------------------------------------------------------------------------

def fit(x: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    """Ordinary least squares with intercept."""
    beta, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(x)), x]), y, rcond=None)
    return float(beta[0]), beta[1:]


def evaluate(scores: np.ndarray, gaps: np.ndarray, cfg: dict) -> dict:
    """Hit = predicted sign equals the actual gap's sign, on days the call is not flat."""
    called = np.abs(scores) >= cfg["flat_band_pct"]
    strong = np.abs(scores) >= cfg["strong_pct"]
    hit = np.sign(scores) == np.sign(gaps)
    rate = lambda mask: round(float(hit[mask].mean()), 3) if mask.any() else None  # noqa: E731
    return {"days": int(called.sum()), "hit_rate": rate(called),
            "strong_days": int(strong.sum()), "strong_hit_rate": rate(strong), "flat_days": int((~called).sum())}


def calibrate(dataset, cfg: dict, today: date) -> dict:
    """dataset: DataFrame indexed by KOSPI date with column gap (%) and one column per feature (%).
    Score the last test_months out of sample, then refit on the whole window for live use."""
    features = cfg["features"]
    df = dataset.dropna(subset=["gap", *features])
    df = df[df.index >= np.datetime64(today - timedelta(days=365 * cfg["window_years"]))]
    split = np.datetime64(today - timedelta(days=30 * cfg["test_months"]))
    train, test = df[df.index < split], df[df.index >= split]
    if len(train) < cfg["min_train_days"] or len(test) < cfg["min_test_days"]:
        raise ValueError(f"not enough history: train {len(train)}, test {len(test)}")
    a, b = fit(train[features].to_numpy(), train["gap"].to_numpy())
    result = evaluate(a + test[features].to_numpy() @ b, test["gap"].to_numpy(), cfg)
    a, b = fit(df[features].to_numpy(), df["gap"].to_numpy())
    return {
        "method": cfg["method"], "calibrated": today.isoformat(),
        "window": [str(df.index[0].date()), str(df.index[-1].date())], "n": len(df),
        "intercept": round(a, 4), "coef": {k: round(float(v), 4) for k, v in zip(features, b)},
        "test": {"from": str(test.index[0].date()), "to": str(test.index[-1].date()), **result},
    }


def download(cfg: dict, today: date):
    """KOSPI open/close (FinanceDataReader) joined to the last US session before each KOSPI day (yfinance)."""
    import FinanceDataReader as fdr
    import pandas as pd
    import yfinance as yf

    start = (today - timedelta(days=365 * cfg["window_years"] + 10)).isoformat()
    kospi = fdr.DataReader(cfg["kospi_symbol"], start, (today + timedelta(days=1)).isoformat())[["Open", "Close"]]
    kospi = kospi[kospi["Open"] > 0].dropna()
    gap = (kospi["Open"] / kospi["Close"].shift(1) - 1) * 100
    symbols = cfg["history_symbols"]
    closes = yf.download(list(symbols.values()), start=start, end=(today + timedelta(days=1)).isoformat(),
                         progress=False, auto_adjust=True)["Close"]
    moves = closes.rename(columns={v: k for k, v in symbols.items()}).pct_change() * 100
    moves = moves.dropna(how="all")
    rows = []
    for day, g in gap.dropna().items():
        prior = moves.index[moves.index < day]
        if len(prior) and (day - prior[-1]).days <= 4:  # skip days after long US gaps
            rows.append({"date": day, "gap": g, **moves.loc[prior[-1], cfg["features"]].to_dict()})
    return pd.DataFrame(rows).set_index("date")


def main(argv: list[str] | None = None) -> int:
    cfg = config.load("settings")["outlook"]
    parser = argparse.ArgumentParser(prog="radar.analytics.outlook", description="M12 monthly recalibration")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out", type=Path, default=MODEL_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        model = calibrate(download(cfg, args.date), cfg, args.date)
    except Exception as exc:  # keep last month's file; the workflow fails visibly
        log.error("calibration failed: %s %s", type(exc).__name__, exc)
        return 1
    args.out.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("calibrated: %s", json.dumps(model["test"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
