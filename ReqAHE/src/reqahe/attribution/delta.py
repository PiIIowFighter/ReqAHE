from __future__ import annotations

import csv
from pathlib import Path

from reqahe.utils.io import ensure_dir, read_json, write_json, write_text


def write_attribution(iteration_dir: str | Path, previous_rollout: str | Path | None, current_rollout: str | Path) -> Path:
    iteration = Path(iteration_dir)
    out = ensure_dir(iteration / "attribution")
    current = _by_task(Path(current_rollout) / "task_results.json")
    previous = _by_task(Path(previous_rollout) / "task_results.json") if previous_rollout and Path(previous_rollout).exists() else {}
    rows = []
    improved = []
    regressed = []
    for task_id, result in current.items():
        cur = result["metrics"]
        prev = previous.get(task_id, {}).get("metrics", {})
        delta_ire = cur.get("IRE", 0) - prev.get("IRE", 0)
        delta_tkqr = cur.get("TKQR", 0) - prev.get("TKQR", 0)
        delta_main = 0.65 * delta_ire + 0.35 * delta_tkqr
        status = "unchanged"
        if delta_main >= 0.03 or delta_ire >= 0.05:
            status = "improved"
            improved.append(task_id)
        if delta_main <= -0.03 or delta_ire <= -0.05:
            status = "regressed"
            regressed.append(task_id)
        rows.append([task_id, f"{delta_ire:.6f}", f"{delta_tkqr:.6f}", f"{delta_main:.6f}", status])
    with (out / "task_deltas.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario_id", "delta_IRE", "delta_TKQR", "delta_main_score", "status"])
        writer.writerows(rows)
    verdicts = {
        "improved_tasks": improved,
        "regressed_tasks": regressed,
        "change_verdicts": [],
        "note": "First iteration uses task deltas; manifest-level attribution is populated when previous manifests exist.",
    }
    write_json(out / "change_verdicts.json", verdicts)
    write_text(out / "rollback_decisions.md", "# Rollback Decisions\n\nNo automatic rollback was triggered in this iteration.\n")
    return out


def _by_task(path: Path) -> dict:
    if not path.exists():
        return {}
    return {item["scenario_id"]: item for item in read_json(path)}
