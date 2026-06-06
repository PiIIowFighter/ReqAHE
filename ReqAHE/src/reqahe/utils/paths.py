from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def safe_name(value: str | None, default: str = "model") -> str:
    raw = value or default
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return clean or default


def make_run_name(project: str, model: str | None) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{safe_name(project, 'ReqAHE')}-{safe_name(model, 'model')}-{ts}"
