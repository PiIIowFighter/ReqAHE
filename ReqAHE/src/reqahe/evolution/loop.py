from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import copy_harness_seed, load_skill_catalog_summary, merge_memory_workspace
from reqahe.infra.io import ensure_dir, read_json, write_json, write_text
from reqahe.runtime.metrics import aggregate_metrics
from reqahe.utils.paths import to_posix_relpath


def load_rollout_task_results(rollout_dir: Path) -> list[dict[str, Any]]:
    path = rollout_dir / "task_results.json"
    if not path.exists():
        return []
    data = read_json(path)
    return data if isinstance(data, list) else []


def aggregate_rollout_dirs(rollout_dirs: list[Path], max_turns: int) -> dict[str, Any]:
    task_results: list[dict[str, Any]] = []
    for rollout_dir in rollout_dirs:
        task_results.extend(load_rollout_task_results(rollout_dir))
    if not task_results:
        return {
            "task_count": 0,
            "max_turns": max_turns,
            "mean_IRE": 0.0,
            "mean_TKQR": 0.0,
            "main_score": 0.0,
            "probe_effectiveness": 0.0,
            "mean_turns": 0.0,
            "early_finish_rate": 0.0,
            "type_coverage": {"interaction": 0.0, "content": 0.0, "style": 0.0},
        }
    return aggregate_metrics(task_results, max_turns=max_turns)


def aggregate_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    return {
        "mean_IRE": round(after.get("mean_IRE", 0.0) - before.get("mean_IRE", 0.0), 6),
        "mean_TKQR": round(after.get("mean_TKQR", 0.0) - before.get("mean_TKQR", 0.0), 6),
        "main_score": round(after.get("main_score", 0.0) - before.get("main_score", 0.0), 6),
    }


def write_rollout_after_status(
    rollout_after_dir: Path,
    status: str,
    reason: str | None,
    message: str,
    iteration: str | int | None = None,
    batch: str | int | None = None,
) -> bool:
    try:
        ensure_dir(rollout_after_dir)
        payload: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "message": message,
        }
        if iteration is not None:
            payload["iteration"] = (
                f"iteration_{int(iteration):03d}" if isinstance(iteration, int) else str(iteration)
            )
        if batch is not None:
            payload["batch"] = f"batch_{int(batch):03d}" if isinstance(batch, int) else str(batch)
        write_json(rollout_after_dir / "STATUS.json", payload)
        return True
    except Exception:
        return False


def harness_source_for_decision(decision: str) -> str:
    """Return the non-memory harness workspace used when finalizing a batch."""
    if decision == "keep":
        return "workspace_candidate"
    return "workspace_before"


def finalize_batch_workspace(
    project_root: Path,
    batch_dir: Path,
    decision: str,
    workspace_before: Path,
    workspace_candidate: Path,
    workspace_memory: Path | None = None,
) -> dict[str, Any]:
    workspace_after = batch_dir / "workspace_after"
    harness_source_name = harness_source_for_decision(decision)
    if decision == "keep":
        harness_source = workspace_candidate
    else:
        harness_source = workspace_before

    # Copy non-memory harness components from the keep/rollback decision source.
    copy_harness_seed(project_root, workspace_after, source_workspace=harness_source)

    memory_merged = False
    # Memory is an append-only experience record.
    # It is not part of the current batch retest and is never rolled back by the keep/rollback decision.
    # Current-batch memory becomes visible only from the next batch / next iteration.
    if workspace_memory and workspace_memory.exists():
        target_memory = workspace_after / "memory"
        if target_memory.exists():
            shutil.rmtree(target_memory)
        merge_memory_workspace(workspace_memory, workspace_after)
        memory_merged = True

    return {
        "workspace_after": workspace_after,
        "harness_decision": decision,
        "harness_source": harness_source_name,
        "harness_source_path": harness_source,
        "memory_policy": "no_rollback",
        "memory_apply_timing": "next_batch",
        "memory_source": "workspace_memory",
        "memory_source_path": workspace_memory if workspace_memory else batch_dir / "workspace_memory",
        "memory_merged": memory_merged,
        "rollout_after_uses_new_memory": False,
    }


def write_batch_decision(
    batch_dir: Path,
    *,
    before_metrics: dict[str, Any],
    after_metrics: dict[str, Any],
    decision: dict[str, Any],
    accepted_workspace: str,
    finalize_info: dict[str, Any],
) -> None:
    harness_source_path = finalize_info.get("harness_source_path")
    memory_source_path = finalize_info.get("memory_source_path")
    harness_decision = finalize_info.get("harness_decision", decision.get("decision"))
    metrics_compared = bool(decision.get("metrics_compared", True))
    payload = {
        "before_metrics": before_metrics,
        "after_metrics": after_metrics,
        "delta_mean_IRE": decision.get("delta_mean_IRE"),
        "delta_mean_TKQR": decision.get("delta_mean_TKQR"),
        "delta_main_score": decision.get("delta_main_score"),
        "effective_delta_main_score": float(decision.get("effective_delta_main_score", 0.0) or 0.0),
        "is_small_delta": bool(decision.get("is_small_delta", False)),
        "metrics_compared": metrics_compared,
        "decision_thresholds": decision.get("decision_thresholds") or {},
        "decision": decision.get("decision"),
        "reason": decision.get("reason"),
        "accepted_workspace": to_posix_relpath(accepted_workspace, batch_dir),
        "final_workspace": "workspace_after",
        "harness_decision": harness_decision,
        "harness_source": finalize_info.get("harness_source", ""),
        "harness_source_path": to_posix_relpath(harness_source_path, batch_dir)
        if harness_source_path
        else "",
        "memory_policy": finalize_info.get("memory_policy", "no_rollback"),
        "memory_apply_timing": finalize_info.get("memory_apply_timing", "next_batch"),
        "memory_source": finalize_info.get("memory_source", "workspace_memory"),
        "memory_source_path": to_posix_relpath(memory_source_path, batch_dir)
        if memory_source_path
        else "",
        "memory_merged": bool(finalize_info.get("memory_merged")),
        "rollout_after_uses_new_memory": bool(finalize_info.get("rollout_after_uses_new_memory", False)),
    }
    write_json(batch_dir / "batch_decision.json", payload)
    write_text(
        batch_dir / "rollback_decisions.md",
        _rollback_decisions_markdown(payload),
    )


def _rollback_decisions_markdown(payload: dict[str, Any]) -> str:
    before = payload.get("before_metrics") or {}
    after = payload.get("after_metrics") or {}
    lines = [
        "# Batch Rollback Decision",
        "",
        f"Decision: `{payload.get('decision', '')}`",
        f"Reason: {payload.get('reason', '')}",
        "",
        "## Before Metrics",
        "",
        f"- mean_IRE: {before.get('mean_IRE', '')}",
        f"- mean_TKQR: {before.get('mean_TKQR', '')}",
        f"- main_score: {before.get('main_score', '')}",
        "",
        "## After Metrics",
        "",
        f"- mean_IRE: {after.get('mean_IRE', '')}",
        f"- mean_TKQR: {after.get('mean_TKQR', '')}",
        f"- main_score: {after.get('main_score', '')}",
        "",
        "## Delta",
        "",
        f"- delta_mean_IRE: {payload.get('delta_mean_IRE', '')}",
        f"- delta_mean_TKQR: {payload.get('delta_mean_TKQR', '')}",
        f"- delta_main_score: {payload.get('delta_main_score', '')}",
        "",
        f"Accepted workspace: `{payload.get('accepted_workspace', '')}`",
        f"Harness source: `{payload.get('harness_source', '')}`",
        f"Harness source path: `{payload.get('harness_source_path', '')}`",
        f"Memory policy: `{payload.get('memory_policy', '')}`",
        f"Memory apply timing: `{payload.get('memory_apply_timing', '')}`",
        f"Memory source: `{payload.get('memory_source', '')}`",
        f"Memory merged: `{payload.get('memory_merged', '')}`",
        f"Rollout after uses new memory: `{payload.get('rollout_after_uses_new_memory', '')}`",
    ]
    return "\n".join(lines) + "\n"


def write_iteration_artifacts(
    iteration_dir: Path,
    *,
    iteration: int,
    selected_scenario_count: int,
    batch_size: int | None,
    batch_summaries: list[dict[str, Any]],
    pre_update_aggregate: dict[str, Any],
    post_judged_aggregate: dict[str, Any],
    max_turns: int,
    final_workspace: Path | None = None,
) -> None:
    batch_count = len(batch_summaries)
    accepted_batches = sum(1 for item in batch_summaries if item.get("decision") == "keep")
    rolled_back_batches = sum(1 for item in batch_summaries if str(item.get("decision") or "").startswith("rollback"))
    failed_batches = sum(
        1
        for item in batch_summaries
        if item.get("decision") in {"rollback_refiner_failed", "rollback_retest_failed"}
    )
    aggregate_delta_value = aggregate_delta(pre_update_aggregate, post_judged_aggregate)
    skill_evolution = _aggregate_refiner_skill_stats(iteration_dir, batch_summaries, final_workspace=final_workspace)
    iteration_metrics = {
        "iteration": iteration,
        "selected_scenario_count": selected_scenario_count,
        "batch_size": batch_size or 0,
        "batch_count": batch_count,
        "accepted_batches": accepted_batches,
        "rolled_back_batches": rolled_back_batches,
        "failed_batches": failed_batches,
        "pre_update_aggregate": pre_update_aggregate,
        "post_judged_aggregate": post_judged_aggregate,
        "aggregate_delta": aggregate_delta_value,
        "batch_summaries": batch_summaries,
        "accepted_skill_count": skill_evolution["accepted_skill_count"],
        "rollback_skill_count": skill_evolution["rollback_skill_count"],
        "workspace_skill_catalog_size": skill_evolution["workspace_skill_catalog_size"],
        "proposed_skill_count": skill_evolution["proposed_skill_count"],
        "written_skill_count": skill_evolution["written_skill_count"],
    }
    write_json(iteration_dir / "iteration_metrics.json", iteration_metrics)
    write_json(iteration_dir / "batch_metrics.json", {"batches": batch_summaries})
    write_json(iteration_dir / "summary.json", post_judged_aggregate)


def _aggregate_refiner_skill_stats(
    iteration_dir: Path,
    batch_summaries: list[dict[str, Any]],
    *,
    final_workspace: Path | None = None,
) -> dict[str, int]:
    accepted_skill_count = 0
    rollback_skill_count = 0
    proposed_skill_count = 0
    written_skill_count = 0
    for item in batch_summaries:
        batch_index = item.get("batch_index")
        if not isinstance(batch_index, int):
            continue
        stats_path = iteration_dir / f"batch_{batch_index:03d}" / "refiner" / "refiner_stats.json"
        stats = read_json(stats_path) if stats_path.exists() else {}
        proposed = int(stats.get("proposed_skill_count") or 0)
        written = int(stats.get("written_skill_count") or 0)
        proposed_skill_count += proposed
        written_skill_count += written
        decision = str(item.get("decision") or "")
        if decision == "keep":
            accepted_skill_count += written
        elif decision.startswith("rollback"):
            rollback_skill_count += proposed or written
    workspace = final_workspace if final_workspace is not None else iteration_dir / "workspace"
    catalog_size = 0
    skills_dir = workspace / "skills"
    if skills_dir.exists():
        catalog_size = sum(1 for path in skills_dir.glob("*/SKILL.md") if path.is_file())
    return {
        "accepted_skill_count": accepted_skill_count,
        "rollback_skill_count": rollback_skill_count,
        "proposed_skill_count": proposed_skill_count,
        "written_skill_count": written_skill_count,
        "workspace_skill_catalog_size": catalog_size,
    }


def write_skill_evolution_digest(
    iteration_dir: Path,
    batch_dir: Path,
    workspace: Path,
    *,
    recent_window: int = 8,
    include_current: bool = False,
) -> Path:
    analysis = ensure_dir(batch_dir / "analysis")
    payload = build_skill_evolution_digest(
        iteration_dir,
        workspace,
        current_batch_dir=batch_dir,
        recent_window=recent_window,
        include_current=include_current,
    )
    out = analysis / "skill_evolution_digest.json"
    write_json(out, payload)
    return out


def collect_recent_completed_batch_dirs(
    run_dir: Path,
    current_iteration_dir: Path,
    current_batch_dir: Path | None = None,
    recent_window: int = 8,
    include_current: bool = False,
) -> list[Path]:
    if recent_window <= 0 or not run_dir.is_dir():
        return []
    current_key = _batch_sort_key(current_iteration_dir, current_batch_dir) if current_batch_dir else None
    batch_dirs: list[tuple[tuple[int, int], Path]] = []
    for iteration in run_dir.glob("iteration_*"):
        if not iteration.is_dir():
            continue
        iteration_number = _parse_numbered_dir_name(iteration.name, "iteration_")
        if iteration_number is None:
            continue
        for batch in iteration.glob("batch_*"):
            if not batch.is_dir():
                continue
            batch_number = _parse_numbered_dir_name(batch.name, "batch_")
            if batch_number is None:
                continue
            key = (iteration_number, batch_number)
            if current_key is not None:
                if key > current_key:
                    continue
                if key == current_key and not include_current:
                    continue
            if _is_completed_batch_dir(batch):
                batch_dirs.append((key, batch))
    return [path for _, path in sorted(batch_dirs)[-recent_window:]]


def build_skill_evolution_digest(
    iteration_dir: Path,
    workspace: Path,
    *,
    current_batch_dir: Path | None = None,
    recent_window: int = 8,
    include_current: bool = False,
) -> dict[str, Any]:
    skill_ids = _workspace_skill_ids(workspace)
    skill_data: dict[str, dict[str, Any]] = {
        skill_id: {
            "recent_touched_count": 0,
            "recent_keep_count": 0,
            "recent_rollback_count": 0,
            "selection_shares": [],
            "hit_rates": [],
            "operation_intents": [],
        }
        for skill_id in skill_ids
    }
    batch_dirs = collect_recent_completed_batch_dirs(
        iteration_dir.parent,
        iteration_dir,
        current_batch_dir=current_batch_dir,
        recent_window=recent_window,
        include_current=include_current,
    )
    for recent_batch in batch_dirs:
        decision = _read_optional_json(recent_batch / "batch_decision.json", {})
        decision_name = str(decision.get("harness_decision") or decision.get("decision") or "")
        stats = _read_optional_json(recent_batch / "refiner" / "refiner_stats.json", {})
        touched = [str(item) for item in stats.get("touched_skill_ids") or [] if str(item).strip()]
        intents = [str(item) for item in stats.get("operation_intents") or [] if str(item).strip()]
        for skill_id in touched:
            item = skill_data.setdefault(
                skill_id,
                {
                    "recent_touched_count": 0,
                    "recent_keep_count": 0,
                    "recent_rollback_count": 0,
                    "selection_shares": [],
                    "hit_rates": [],
                    "operation_intents": [],
                },
            )
            item["recent_touched_count"] += 1
            item["operation_intents"].extend(intents)
            if decision_name == "keep":
                item["recent_keep_count"] += 1
            elif decision_name.startswith("rollback"):
                item["recent_rollback_count"] += 1
        route_stats = _read_optional_json(recent_batch / "rollout_before" / "route_stats.json", {})
        for skill_id, route_item in (route_stats.get("skills") or {}).items():
            item = skill_data.setdefault(
                str(skill_id),
                {
                    "recent_touched_count": 0,
                    "recent_keep_count": 0,
                    "recent_rollback_count": 0,
                    "selection_shares": [],
                    "hit_rates": [],
                    "operation_intents": [],
                },
            )
            item["selection_shares"].append(float(route_item.get("selection_share", 0.0) or 0.0))
            item["hit_rates"].append(float(route_item.get("hit_rate", 0.0) or 0.0))

    digest_skills: dict[str, Any] = {}
    for skill_id, item in sorted(skill_data.items()):
        touched = int(item.get("recent_touched_count") or 0)
        selection_shares = item.get("selection_shares") or []
        hit_rates = item.get("hit_rates") or []
        avg_selection_share = _mean(selection_shares)
        avg_hit_rate = _mean(hit_rates)
        digest_skills[skill_id] = {
            "recent_touched_count": touched,
            "recent_keep_count": int(item.get("recent_keep_count") or 0),
            "recent_rollback_count": int(item.get("recent_rollback_count") or 0),
            "avg_selection_share": avg_selection_share,
            "avg_hit_rate": avg_hit_rate,
            "operation_intents": sorted(set(item.get("operation_intents") or [])),
            "observed_pattern": _describe_skill_pattern(touched, avg_selection_share, avg_hit_rate),
        }
    return {
        "skills": digest_skills,
        "source": {
            "recent_window": recent_window,
            "batch_count_seen": len(batch_dirs),
            "workspace_skill_catalog_summary": load_skill_catalog_summary(workspace),
        },
    }


def _batch_sort_key(iteration_dir: Path, batch_dir: Path | None) -> tuple[int, int] | None:
    iteration_number = _parse_numbered_dir_name(iteration_dir.name, "iteration_")
    if iteration_number is None or batch_dir is None:
        return None
    batch_number = _parse_numbered_dir_name(batch_dir.name, "batch_")
    if batch_number is None:
        return None
    return (iteration_number, batch_number)


def _parse_numbered_dir_name(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix) :])
    except ValueError:
        return None


def _is_completed_batch_dir(batch_dir: Path) -> bool:
    completion_markers = (
        batch_dir / "batch_decision.json",
        batch_dir / "refiner" / "refiner_stats.json",
        batch_dir / "analysis" / "route_stats_digest.json",
    )
    return any(path.is_file() for path in completion_markers)


def _workspace_skill_ids(workspace: Path) -> list[str]:
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(path.parent.name for path in skills_dir.glob("*/SKILL.md") if path.is_file())


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _describe_skill_pattern(touched: int, avg_selection_share: float, avg_hit_rate: float) -> str:
    parts: list[str] = []
    if touched:
        parts.append(f"updated in {touched} recent batch(es)")
    if avg_selection_share:
        parts.append(f"average selection share {avg_selection_share}")
    if avg_hit_rate:
        parts.append(f"average hit rate {avg_hit_rate}")
    return "; ".join(parts) if parts else "no recent route or edit evidence"


def _read_optional_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json(path)
    except Exception:
        return default
