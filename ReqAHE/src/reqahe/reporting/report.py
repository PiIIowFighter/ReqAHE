from __future__ import annotations

from pathlib import Path

from reqahe.infra.io import read_json, write_text


def resolve_run_dir(project_root: str | Path, run_dir: str | Path) -> Path:
    raw = Path(run_dir)
    if raw.name == "latest" or str(run_dir).replace("\\", "/").endswith("runs/latest"):
        latest = Path(project_root) / "runs" / "latest.txt"
        if latest.exists():
            return Path(latest.read_text(encoding="utf-8").strip())
    if raw.is_absolute():
        return raw
    return Path(project_root) / raw


def generate_report(project_root: str | Path, run_dir: str | Path) -> Path:
    run = resolve_run_dir(project_root, run_dir)
    metrics_files = _collect_metrics_files(run)
    lines = [
        "# ReqAHE Report",
        "",
        f"Run: `{run}`",
        "",
        "## Internal Evolution Results",
        "",
        "- `internal evolution result`: metrics produced by the current ReqAHE evolution loop.",
        "- `iteration`: one full pass over all selected scenarios, split into local batches.",
        "- `batch`: local diagnose/refine/retest unit within an iteration.",
        "- `baseline vs current`: compare seed or earlier iterations with the latest available rollout.",
        "- `batch rollback`: keep/rollback decision based on same-batch before/after metrics.",
        "- `final evaluation result`: frozen harness evaluation when `final-eval` is run.",
        "",
        "## Rollout Metrics",
        "",
        "| run_step | result_type | split | task_mode | max_turns | mean_IRE | mean_TKQR | main_score | interaction_cov | content_cov | style_cov | early_finish_rate |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metrics_path, step_label, meta in metrics_files:
        metrics = read_json(metrics_path)
        cov = metrics.get("type_coverage", {})
        lines.append(
            "| {step} | {result_type} | {split} | {task_mode} | {max_turns} | {ire} | {tkqr} | {score} | {interaction} | {content} | {style} | {early} |".format(
                step=step_label,
                result_type=meta.get("result_type", "internal evolution result"),
                split=meta.get("split", ""),
                task_mode=meta.get("task_mode", ""),
                max_turns=metrics.get("max_turns", meta.get("max_turns", "")),
                ire=metrics.get("mean_IRE", ""),
                tkqr=metrics.get("mean_TKQR", ""),
                score=metrics.get("main_score", ""),
                interaction=cov.get("interaction", 0),
                content=cov.get("content", 0),
                style=cov.get("style", 0),
                early=metrics.get("early_finish_rate", ""),
            )
        )
    lines.extend(["", "## Iteration Metrics", ""])
    for metrics_path in sorted(run.glob("iteration_*/iteration_metrics.json")):
        metrics = read_json(metrics_path)
        lines.append(
            f"- `{metrics_path.parent.name}`: batches={metrics.get('batch_count', '')}, "
            f"accepted={metrics.get('accepted_batches', '')}, rolled_back={metrics.get('rolled_back_batches', '')}, "
            f"pre_main_score={(metrics.get('pre_update_aggregate') or {}).get('main_score', '')}, "
            f"post_main_score={(metrics.get('post_judged_aggregate') or {}).get('main_score', '')}"
        )
    lines.extend(["", "## Self-reflection Events", ""])
    for rollout_dir in sorted(_collect_rollout_dirs(run)):
        summary = _reflection_summary(rollout_dir)
        if summary:
            lines.append(f"- `{rollout_dir.parent.name}/{rollout_dir.name}`: {summary}")
    lines.extend(["", "## Diagnoser Localized Findings", ""])
    for localization_path in sorted(run.glob("**/analysis/component_localization.json")):
        localization = read_json(localization_path)
        findings = localization.get("component_findings") or localization.get("localized_findings") or []
        if not findings:
            continue
        lines.append(f"### {_analysis_label(localization_path)}")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if "issue" in finding:
                lines.append(
                    f"- `{finding.get('component', '')}`: {finding.get('issue', '')} "
                    f"(direction={finding.get('recommended_refinement_direction', '')})"
                )
            else:
                lines.append(
                    f"- `{finding.get('issue_id', '')}` {finding.get('target_component', '')}: "
                    f"{finding.get('reason', '')}"
                )
    lines.extend(["", "## Refiner Applied Edits", ""])
    for batch_dir in sorted(run.glob("iteration_*/batch_*")):
        refiner_dir = batch_dir / "refiner"
        if not refiner_dir.exists():
            continue
        fix_plan_path = refiner_dir / "fix_plan.json"
        proposed_edits_path = refiner_dir / "proposed_edits.json"
        validation_report_path = refiner_dir / "validation_report.json"
        if not fix_plan_path.exists() and not proposed_edits_path.exists():
            continue
        lines.append(f"### {batch_dir.parent.name}/{batch_dir.name}")
        if fix_plan_path.exists():
            fix_plan = read_json(fix_plan_path)
            for fix in fix_plan.get("fix_plan") or []:
                if not isinstance(fix, dict):
                    continue
                lines.append(
                    f"- `{fix.get('component', '')}`: {fix.get('fix_summary', '')} "
                    f"(expected: {fix.get('expected_effect', '')})"
                )
        if proposed_edits_path.exists():
            proposed = read_json(proposed_edits_path)
            for edit in proposed.get("file_edits") or []:
                if not isinstance(edit, dict):
                    continue
                lines.append(
                    f"- edit `{edit.get('relative_path', '')}` operation={edit.get('operation', '')}"
                )
        if validation_report_path.exists():
            validation = read_json(validation_report_path)
            lines.append(
                f"- validation ok={validation.get('ok', '')}, "
                f"errors={validation.get('errors', [])}, warnings={validation.get('warnings', [])}"
            )
    lines.extend(["", "## Attribution Metric Deltas", ""])
    for deltas_path in sorted(run.glob("**/attribution/metric_deltas.json")):
        deltas = read_json(deltas_path)
        aggregate = deltas.get("aggregate_delta", {})
        lines.append(
            f"- `{_attribution_label(deltas_path)}`: delta_main_score={aggregate.get('main_score', '')}, "
            f"improved={deltas.get('improved_tasks', [])}, regressed={deltas.get('regressed_tasks', [])}, "
            f"unchanged={deltas.get('unchanged_tasks', [])}"
        )
    lines.extend(["", "## Batch Rollback Decisions", ""])
    for decision_path in sorted(run.glob("iteration_*/batch_*/batch_decision.json")):
        decision = read_json(decision_path)
        lines.append(
            f"- `{decision_path.parent.parent.name}/{decision_path.parent.name}`: "
            f"decision={decision.get('decision', '')}, delta_main_score={decision.get('delta_main_score', '')}, "
            f"reason={decision.get('reason', '')}"
        )
    lines.extend(["", "## Rollback Decisions", ""])
    for rollback_path in sorted(run.glob("**/rollback_decisions.md")):
        if rollback_path.parent.name.startswith("batch_"):
            continue
        lines.append(f"### {rollback_path.parent.name}")
        lines.append(rollback_path.read_text(encoding="utf-8").strip())
    out = run / "report.md"
    write_text(out, "\n".join(lines) + "\n")
    return out


def _collect_metrics_files(run: Path) -> list[tuple[Path, str, dict]]:
    rows: list[tuple[Path, str, dict]] = []
    patterns = [
        ("**/rollout/metrics.json", lambda path: path.parent.parent.name),
        ("**/rollout_before/metrics.json", lambda path: f"{path.parent.parent.parent.name}/{path.parent.parent.name}/rollout_before"),
        ("**/rollout_after/metrics.json", lambda path: f"{path.parent.parent.parent.name}/{path.parent.parent.name}/rollout_after"),
        ("iteration_*/iteration_metrics.json", lambda path: f"{path.parent.name}/aggregate"),
        ("iteration_*/summary.json", lambda path: f"{path.parent.name}/summary"),
    ]
    seen: set[Path] = set()
    for pattern, label_fn in patterns:
        for metrics_path in sorted(run.glob(pattern)):
            if metrics_path in seen:
                continue
            seen.add(metrics_path)
            meta_path = _metadata_path_for_metrics(metrics_path)
            meta = read_json(meta_path) if meta_path.exists() else {}
            rows.append((metrics_path, label_fn(metrics_path), meta))
    return rows


def _metadata_path_for_metrics(metrics_path: Path) -> Path:
    parent = metrics_path.parent
    if parent.name in {"rollout_before", "rollout_after"}:
        return parent.parent / "run_metadata.json"
    if parent.name == "rollout":
        return parent.parent / "run_metadata.json"
    if parent.name.startswith("iteration_"):
        return parent / "run_metadata.json"
    return parent / "run_metadata.json"


def _collect_rollout_dirs(run: Path) -> list[Path]:
    dirs = sorted(run.glob("**/rollout"))
    dirs.extend(sorted(run.glob("**/rollout_before")))
    dirs.extend(sorted(run.glob("**/rollout_after")))
    return dirs


def _analysis_label(path: Path) -> str:
    if path.parent.parent.name.startswith("batch_"):
        return f"{path.parent.parent.parent.name}/{path.parent.parent.name}"
    return path.parent.parent.name


def _attribution_label(path: Path) -> str:
    if path.parent.parent.name.startswith("batch_"):
        return f"{path.parent.parent.parent.name}/{path.parent.parent.name}"
    return path.parent.parent.name


def _reflection_summary(rollout_dir: Path) -> str:
    counts: dict[str, int] = {}
    for event_path in sorted(rollout_dir.glob("*/self_reflection_events.jsonl")):
        for line in event_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = read_json_line(line)
            except Exception:
                continue
            event_type = str(event.get("type") or "reflection_event")
            counts[event_type] = counts.get(event_type, 0) + 1
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def read_json_line(line: str) -> dict:
    import json

    return json.loads(line)
