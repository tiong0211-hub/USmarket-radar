"""M5: VIX to a 0-100 score by rule. No news and no LLM.

vix_percentile_252d_v0 (temporary, settled with M12): 100 - percentile of today's VIX
within the last 252 closes. A high VIX means fear, so the score is low.
"""
from radar.models import MarketSnapshot, Sentiment


def score(vix: float, history: list[float]) -> int:
    percentile = sum(v <= vix for v in history) / len(history) * 100
    return round(100 - percentile)


def compute(snapshot: MarketSnapshot, cfg: dict) -> Sentiment:
    vix = next((i for i in snapshot.indicators if i.key == "VIX"), None)
    history = snapshot.history.get("VIX", [])
    if vix is None or vix.missing or vix.value is None:
        raise ValueError("VIX missing")
    if len(history) < cfg["min_history"]:
        raise ValueError(f"VIX history has {len(history)} closes, needs {cfg['min_history']}")
    value = score(vix.value, history)
    band = next(b for b in cfg["bands"] if value <= b["max"])
    return Sentiment(source="VIX", vix=vix.value, score=value, label=band["label"], label_en=band["label_en"],
                     method=cfg["method"], note_ko=cfg["note_ko"])
