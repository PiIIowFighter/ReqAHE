from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import copy_harness_seed, merge_memory_workspace
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
    payload = {
        "before_metrics": before_metrics,
        "after_metrics": after_metrics,
        "delta_mean_IRE": decision.get("delta_mean_IRE", 0.0),
        "delta_mean_TKQR": decision.get("delta_mean_TKQR", 0.0),
        "delta_main_score": decision.get("delta_main_score", 0.0),
        "decision": decision.get("decision"),
        "reason": decision.get("reason"),
        "accepted_workspace": to_posix_relpath(accepted_workspace, batch_dir),
        "harness_decision": finalize_info.get("harness_decision", decision.get("decision")),
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
) -> None:
    batch_count = len(batch_summaries)
    accepted_batches = sum(1 for item in batch_summaries if item.get("decision") == "keep")
    rolled_back_batches = sum(1 for item in batch_summaries if item.get("decision") == "rollback")
    failed_batches = sum(
        1
        for item in batch_summaries
        if item.get("decision") in {"rollback_refiner_failed", "rollback_retest_failed"}
    )
    aggregate_delta_value = aggregate_delta(pre_update_aggregate, post_judged_aggregate)
    skill_evolution = _aggregate_refiner_skill_stats(iteration_dir, batch_summaries)
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


def _aggregate_refiner_skill_stats(iteration_dir: Path, batch_summaries: list[dict[str, Any]]) -> dict[str, int]:
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
    workspace = iteration_dir / "workspace"
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
