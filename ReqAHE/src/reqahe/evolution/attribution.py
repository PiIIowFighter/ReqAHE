from __future__ import annotations

import csv
import shutil
from pathlib import Path

from reqahe.infra.io import ensure_dir, read_json, write_json, write_text


def write_attribution(iteration_dir: str | Path, previous_rollout: str | Path | None, current_rollout: str | Path) -> Path:
    iteration = Path(iteration_dir)
    out = ensure_dir(iteration / "attribution")
    current_rollout_path = Path(current_rollout)
    previous_rollout_path = Path(previous_rollout) if previous_rollout and Path(previous_rollout).exists() else None
    current = _by_task(current_rollout_path / "task_results.json")
    previous = _by_task(previous_rollout_path / "task_results.json") if previous_rollout_path else {}
    rows = []
    improved = []
    regressed = []
    unchanged = []
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
        elif delta_main <= -0.03 or delta_ire <= -0.05:
            status = "regressed"
            regressed.append(task_id)
        else:
            unchanged.append(task_id)
        rows.append([task_id, f"{delta_ire:.6f}", f"{delta_tkqr:.6f}", f"{delta_main:.6f}", status])
    with (out / "task_deltas.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario_id", "delta_IRE", "delta_TKQR", "delta_main_score", "status"])
        writer.writerows(rows)
    previous_metrics = _read_metrics(previous_rollout_path / "metrics.json") if previous_rollout_path else {}
    current_metrics = _read_metrics(current_rollout_path / "metrics.json")
    aggregate_delta = _aggregate_delta(previous_metrics, current_metrics)
    metric_deltas = {
        "aggregate_delta": aggregate_delta,
        "improved_tasks": improved,
        "regressed_tasks": regressed,
        "unchanged_tasks": unchanged,
    }
    write_json(out / "metric_deltas.json", metric_deltas)
    write_text(out / "task_movement.md", _task_movement_markdown(aggregate_delta, improved, regressed, unchanged))
    return out


def _by_task(path: Path) -> dict:
    if not path.exists():
        return {}
    return {item["scenario_id"]: item for item in read_json(path)}


def _read_metrics(path: Path) -> dict:
    if not path or not path.exists():
        return {}
    return read_json(path)


def _aggregate_delta(previous_metrics: dict, current_metrics: dict) -> dict:
    return {
        "mean_IRE": round(current_metrics.get("mean_IRE", 0.0) - previous_metrics.get("mean_IRE", 0.0), 6),
        "mean_TKQR": round(current_metrics.get("mean_TKQR", 0.0) - previous_metrics.get("mean_TKQR", 0.0), 6),
        "main_score": round(current_metrics.get("main_score", 0.0) - previous_metrics.get("main_score", 0.0), 6),
    }


def _task_movement_markdown(
    aggregate_delta: dict,
    improved: list[str],
    regressed: list[str],
    unchanged: list[str],
) -> str:
    lines = [
        "# Task Movement",
        "",
        "## Aggregate Delta",
        "",
        f"- mean_IRE: {aggregate_delta['mean_IRE']}",
        f"- mean_TKQR: {aggregate_delta['mean_TKQR']}",
        f"- main_score: {aggregate_delta['main_score']}",
        "",
        "## Task Movement",
        "",
        f"- Improved tasks: {', '.join(improved) if improved else '(none)'}",
        f"- Regressed tasks: {', '.join(regressed) if regressed else '(none)'}",
        f"- Unchanged tasks: {', '.join(unchanged) if unchanged else '(none)'}",
    ]
    return "\n".join(lines) + "\n"


def judge_batch_decision(
    before_metrics: dict,
    after_metrics: dict,
    *,
    refiner_ok: bool = True,
    retest_ok: bool = True,
) -> dict:
    before_main = float(before_metrics.get("main_score", 0.0) or 0.0)
    after_main = float(after_metrics.get("main_score", 0.0) or 0.0)
    delta_ire = round(float(after_metrics.get("mean_IRE", 0.0) or 0.0) - float(before_metrics.get("mean_IRE", 0.0) or 0.0), 6)
    delta_tkqr = round(float(after_metrics.get("mean_TKQR", 0.0) or 0.0) - float(before_metrics.get("mean_TKQR", 0.0) or 0.0), 6)
    delta_main = round(after_main - before_main, 6)
    if not refiner_ok:
        return {
            "decision": "rollback_refiner_failed",
            "reason": "Refiner failed or validation failed; restoring workspace_before.",
            "before_main_score": before_main,
            "after_main_score": after_main,
            "delta_main_score": delta_main,
            "delta_mean_IRE": delta_ire,
            "delta_mean_TKQR": delta_tkqr,
        }
    if not retest_ok:
        return {
            "decision": "rollback_retest_failed",
            "reason": "Retest on workspace_candidate failed; restoring workspace_before.",
            "before_main_score": before_main,
            "after_main_score": after_main,
            "delta_main_score": delta_main,
            "delta_mean_IRE": delta_ire,
            "delta_mean_TKQR": delta_tkqr,
        }
    if after_main >= before_main:
        return {
            "decision": "keep",
            "reason": "after.main_score >= before.main_score",
            "before_main_score": before_main,
            "after_main_score": after_main,
            "delta_main_score": delta_main,
            "delta_mean_IRE": delta_ire,
            "delta_mean_TKQR": delta_tkqr,
        }
    return {
        "decision": "rollback",
        "reason": "after.main_score < before.main_score",
        "before_main_score": before_main,
        "after_main_score": after_main,
        "delta_main_score": delta_main,
        "delta_mean_IRE": delta_ire,
        "delta_mean_TKQR": delta_tkqr,
    }


def apply_rollback_if_needed(workspace_dir: str | Path, snapshot_dir: str | Path, verdict: dict) -> bool:
    if verdict.get("decision") != "rollback":
        return False
    workspace = Path(workspace_dir)
    snapshot = Path(snapshot_dir)
    if not snapshot.exists():
        return False
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(snapshot, workspace)
    return True
