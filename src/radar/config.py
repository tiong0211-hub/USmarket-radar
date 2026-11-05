"""Read-only access to config/*.yaml."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / "config" / f"{name}.yaml").read_text(encoding="utf-8"))
