from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from reqahe.config import apply_cli_overrides, load_config, parse_bool, role_model
from reqahe.evolution.attribution import judge_batch_decision, write_attribution
from reqahe.evolution.memorizer import memorize_rollout
from reqahe.evolution.batching import apply_scenario_count, split_scenarios_into_batches
from reqahe.diagnoser import run_elicitation_diagnosis
from reqahe.evolution.loop import (
    aggregate_rollout_dirs,
    finalize_batch_workspace,
    write_batch_decision,
    write_iteration_artifacts,
    write_rollout_after_status,
    write_skill_evolution_digest,
)
from reqahe.refiner import refine_harness
from reqahe.harness.workspace import copy_harness_seed
from reqahe.infra.io import ensure_dir, read_json, write_json, write_text
from reqahe.infra.network import CloseWaitCleaner
from reqahe.infra.paths import make_run_name, repo_root
from reqahe.utils.paths import resolve_project_path, to_posix_relpath
from reqahe.reporting.inspection import inspect_sources
from reqahe.reporting.report import generate_report, resolve_run_dir
from reqahe.runtime.dataset import load_or_create_splits, load_scenarios, resolve_dataset_path, select_scenarios
from reqahe.runtime.eval_outputs import (
    conversation_output as _conversation_output,
    llm_client as _llm,
    metrics_output as _metrics_output,
    output_label as _output_label,
    reflection_mode as _reflection_mode,
    rollout_kwargs as _rollout_kwargs,
)
from reqahe.runtime.runner import run_tasks


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = apply_cli_overrides(load_config(getattr(args, "config", None)), args)
    if args.command in {"run-baseline", "evolve", "resume-evolve", "final-eval"}:
        _require_llm_config(config)
    if args.command == "inspect":
        return cmd_inspect(args, config)
    if args.command == "run-baseline":
        return cmd_run_baseline(args, config)
    if args.command == "evolve":
        return cmd_evolve(args, config)
    if args.command == "resume-evolve":
        return cmd_resume_evolve(args, config)
    if args.command == "final-eval":
        return cmd_final_eval(args, config)
    if args.command == "report":
        return cmd_report(args, config)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reqahe")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--project-root", default=None)
    inspect.add_argument("--reqelicitgym-root", default=None)
    inspect.add_argument("--dataset-file", default=None)
    inspect.add_argument("--dataset-number", default=None)

    baseline = sub.add_parser("run-baseline")
    _add_runtime_args(baseline)
    baseline.add_argument("--agent", choices=["seed_freeform"], required=True)

    evolve = sub.add_parser("evolve")
    _add_runtime_args(evolve)
    evolve.add_argument("--iterations", type=int, default=None)
    evolve.add_argument("--batch-size", type=int, default=None)
    evolve.add_argument("--resume-run-dir", default=None)

    resume = sub.add_parser("resume-evolve")
    _add_runtime_args(resume)
    resume.add_argument("run_dir")
    resume.add_argument("--iterations", type=int, default=None)
    resume.add_argument("--batch-size", type=int, default=None)

    final_eval = sub.add_parser("final-eval")
    _add_runtime_args(final_eval)
    final_eval.add_argument("--best-run", default="latest")

    report = sub.add_parser("report")
    report.add_argument("--run-dir", default="runs/latest")
    return parser


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--split", choices=["train", "val", "test", "all"], default=None)
    parser.add_argument("--task-mode", choices=["test", "top3", "sample", "full"], default=None)
    parser.add_argument("--dataset-file", default=None)
    parser.add_argument("--dataset-number", default=None)
    parser.add_argument("--task-ids", default="")
    parser.add_argument("--scenario-count", type=int, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--rollouts-per-task", type=int, default=None)
    parser.add_argument("--reflection-mode", choices=["observe", "warn", "enforce"], default=None)
    parser.add_argument("--disable-close-wait-cleanup", action="store_true")
    parser.add_argument("--close-wait-cleanup-interval-tasks", type=int, default=None)
    parser.add_argument("--close-wait-cleanup-interval-seconds", type=float, default=None)
    parser.add_argument("--disable-skill-router", action="store_true")
    parser.add_argument("--max-selected-skills", type=int, default=None)
    parser.add_argument("--skill-router-min-relevance", type=float, default=None)
    parser.add_argument("--skill-router-model", default=None)
    parser.add_argument("--disable-memory-router", action="store_true")
    parser.add_argument("--max-selected-memory-types", type=int, default=None)
    parser.add_argument("--memory-router-min-relevance", type=float, default=None)
    parser.add_argument("--memory-router-model", default=None)


def cmd_inspect(args: argparse.Namespace, config: dict) -> int:
    paths = config["paths"]
    project_root = Path(args.project_root or paths["project_root"])
    out = inspect_sources(
        project_root=project_root,
        reqelicitgym_root=args.reqelicitgym_root or paths["reqelicitgym_root"],
        dataset_file=config["evaluation"].get("dataset_file"),
        dataset_number=config["evaluation"].get("dataset_number"),
    )
    print(f"Wrote inspection notes: {out}")
    return 0


def cmd_run_baseline(args: argparse.Namespace, config: dict) -> int:
    project_root = Path(config["paths"]["project_root"])
    run_dir = _create_run(project_root, config, prefix_agent=args.agent)
    _write_latest(project_root, run_dir)
    workspace = run_dir / "baseline_seed_freeform" / "workspace"
    copy_harness_seed(project_root, workspace)
    rollout = run_dir / "baseline_seed_freeform" / "rollout"
    scenarios, split, task_mode = _selected_scenarios(config, args)
    result = run_tasks(
        scenarios,
        workspace,
        rollout,
        _llm(config),
        **_rollout_kwargs(config, agent_name=args.agent, reflection_mode=_reflection_mode(config, default="observe")),
    )
    _write_metadata(run_dir / "baseline_seed_freeform", args.agent, split, task_mode, config, "internal_holdout_result")
    write_json(run_dir / "baseline_seed_freeform" / "summary.json", result["metrics"])
    _write_compat_outputs(run_dir / "baseline_seed_freeform", rollout, scenarios, result, config, split, task_mode, args.agent)
    print(f"Baseline run complete: {rollout}")
    return 0


def cmd_evolve(args: argparse.Namespace, config: dict) -> int:
    return _run_evolve(args, config, resume_run_dir=getattr(args, "resume_run_dir", None))


def cmd_resume_evolve(args: argparse.Namespace, config: dict) -> int:
    return _run_evolve(args, config, resume_run_dir=args.run_dir)


def _run_evolve(args: argparse.Namespace, config: dict, resume_run_dir: str | None = None) -> int:
    project_root = Path(config["paths"]["project_root"])
    resume = bool(resume_run_dir)
    if resume:
        run_dir = resolve_run_dir(project_root, resume_run_dir or "")
        _apply_resume_defaults(run_dir, config, args)
    else:
        run_dir = _create_run(project_root, config, prefix_agent="evolved_reahe")
    _write_latest(project_root, run_dir)
    scenarios, split, task_mode = _selected_scenarios(config, args)
    batch_size = _batch_size(config)
    batches = split_scenarios_into_batches(scenarios, batch_size)
    batch_count = len(batches)
    iterations = _target_iterations(run_dir, config, args, resume=resume)
    _write_run_state(run_dir, config, split, task_mode, iterations, resume=resume, scenario_count=len(scenarios))
    close_wait_cleaner = _close_wait_cleaner(config, run_dir)
    rollouts_per_task = int(config["evaluation"]["rollouts_per_task"])
    max_turns = int(config["evaluation"]["max_turns"])
    print(
        f"[run] evolve run_dir={run_dir} resume={resume} split={split} task_mode={task_mode} "
        f"tasks={len(scenarios)} batch_size={batch_size or 'all'} batch_count={batch_count} "
        f"iterations={iterations} max_turns={max_turns}",
        flush=True,
    )
    source_workspace: Path | None = None
    previous_iteration_metrics: dict | None = None
    for iteration in range(1, iterations + 1):
        iteration_dir = ensure_dir(run_dir / f"iteration_{iteration:03d}")
        if iteration > 1:
            prev_workspace = run_dir / f"iteration_{iteration - 1:03d}" / "workspace"
            if prev_workspace.exists():
                source_workspace = prev_workspace
        accepted_workspace: Path | None = source_workspace
        batch_summaries: list[dict[str, Any]] = []
        pre_rollout_dirs: list[Path] = []
        post_rollout_dirs: list[Path] = []
        print(
            f"[iteration {iteration}/{iterations}] batches={batch_count} dir={iteration_dir}",
            flush=True,
        )
        for batch_idx, batch_scenarios in enumerate(batches, start=1):
            batch_ids = [scenario.scenario_id for scenario in batch_scenarios]
            batch_dir = ensure_dir(iteration_dir / f"batch_{batch_idx:03d}")
            workspace_before = batch_dir / "workspace_before"
            if not workspace_before.exists():
                copy_harness_seed(project_root, workspace_before, source_workspace=accepted_workspace)
            rollout_before = batch_dir / "rollout_before"
            print(
                f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                f"rollout_before status=running batch_ids={batch_ids}",
                flush=True,
            )
            if resume and _rollout_complete(rollout_before, batch_scenarios, rollouts_per_task):
                before_result = {
                    "metrics": read_json(rollout_before / "metrics.json"),
                    "task_results": read_json(rollout_before / "task_results.json"),
                }
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"rollout_before status=skipped_existing",
                    flush=True,
                )
            else:
                llm = _llm(config)
                try:
                    before_result = run_tasks(
                        batch_scenarios,
                        workspace_before,
                        rollout_before,
                        llm,
                        **_rollout_kwargs(
                            config,
                            agent_name="evolved_reahe",
                            reflection_mode=_reflection_mode(config, default="warn"),
                        ),
                        resume=resume,
                        close_wait_cleaner=close_wait_cleaner,
                    )
                finally:
                    llm.close()
                    close_wait_cleaner.cleanup(
                        f"iteration:{iteration}:batch:{batch_idx}:rollout_before",
                        close_resources=[llm.close],
                    )
            print(
                f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] rollout_before status=done",
                flush=True,
            )
            write_json(batch_dir / "metrics.json", before_result["metrics"])
            write_json(batch_dir / "summary.json", before_result["metrics"])
            _write_compat_outputs(
                batch_dir,
                rollout_before,
                batch_scenarios,
                before_result,
                config,
                split,
                task_mode,
                "evolved_reahe",
            )
            pre_rollout_dirs.append(rollout_before)
            workspace_memory = batch_dir / "workspace_memory"
            if resume and _memorization_complete(batch_dir, workspace_memory):
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"memorize status=skipped_existing",
                    flush=True,
                )
            else:
                if not workspace_memory.exists():
                    copy_harness_seed(project_root, workspace_memory, source_workspace=workspace_before)
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"memorize status=running",
                    flush=True,
                )
                llm = _llm(config)
                try:
                    memorize_rollout(
                        batch_dir=batch_dir,
                        rollout_dir=rollout_before,
                        workspace_dir=workspace_memory,
                        llm=llm,
                        model=role_model(config, "memorizer") or role_model(config, "interviewer"),
                        config=config.get("runtime", {}).get("memory", {}),
                    )
                finally:
                    llm.close()
                    close_wait_cleaner.cleanup(
                        f"iteration:{iteration}:batch:{batch_idx}:memorize",
                        close_resources=[llm.close],
                    )
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] memorize status=done",
                    flush=True,
                )
            _write_memory_lifecycle(batch_dir)
            if resume and _diagnosis_complete(batch_dir):
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"diagnoser status=skipped_existing",
                    flush=True,
                )
            else:
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] diagnoser status=running",
                    flush=True,
                )
                write_skill_evolution_digest(iteration_dir, batch_dir, workspace_before)
                llm = _llm(config)
                try:
                    run_elicitation_diagnosis(
                        batch_dir,
                        rollout_before,
                        llm,
                        role_model(config, "diagnoser"),
                        previous_metrics=previous_iteration_metrics,
                        harness_dir=workspace_before,
                    )
                finally:
                    llm.close()
                    close_wait_cleaner.cleanup(
                        f"iteration:{iteration}:batch:{batch_idx}:diagnoser",
                        close_resources=[llm.close],
                    )
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] diagnoser status=done",
                    flush=True,
                )
            workspace_candidate = batch_dir / "workspace_candidate"
            refiner_ok = True
            refiner_error_message = ""
            if resume and _refinement_complete(batch_dir, workspace_candidate):
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"refiner status=skipped_existing",
                    flush=True,
                )
            else:
                copy_harness_seed(project_root, workspace_candidate, source_workspace=workspace_before)
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] refiner status=running",
                    flush=True,
                )
                llm = _llm(config)
                refiner_config = config.get("evolution", {}).get("refiner") or {}
                try:
                    refine_harness(
                        batch_dir,
                        workspace_candidate,
                        iteration,
                        llm,
                        role_model(config, "refiner"),
                        refiner_config=refiner_config,
                    )
                except KeyboardInterrupt:
                    refiner_stage = _read_refiner_stage(batch_dir / "refiner")
                    _write_batch_interrupted_state(
                        batch_dir,
                        rollout_after=batch_dir / "rollout_after",
                        workspace_before=workspace_before,
                        refiner_stage=refiner_stage,
                        iteration=iteration,
                        batch_idx=batch_idx,
                    )
                    raise
                except Exception as exc:
                    refiner_ok = False
                    refiner_error_message = str(exc)
                    if workspace_candidate.exists():
                        shutil.rmtree(workspace_candidate)
                    write_text(batch_dir / "refiner.log", f"Refiner failed: {exc}\n")
                    print(
                        f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                        f"refiner status=failed reason={exc}",
                        flush=True,
                    )
                else:
                    print(
                        f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] refiner status=done",
                        flush=True,
                    )
                finally:
                    llm.close()
                    close_wait_cleaner.cleanup(
                        f"iteration:{iteration}:batch:{batch_idx}:refiner",
                        close_resources=[llm.close],
                    )
            rollout_after = batch_dir / "rollout_after"
            retest_ok = refiner_ok
            retest_error_message = ""
            after_result: dict[str, Any] = {"metrics": {}, "task_results": []}
            status_write_ok = True
            if refiner_ok:
                _assert_current_batch_memory_not_in_candidate(
                    workspace_before,
                    workspace_candidate,
                    workspace_memory,
                    batch_dir,
                )
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"rollout_after status=running",
                    flush=True,
                )
                if resume and _rollout_complete(rollout_after, batch_scenarios, rollouts_per_task):
                    after_result = {
                        "metrics": read_json(rollout_after / "metrics.json"),
                        "task_results": read_json(rollout_after / "task_results.json"),
                    }
                    print(
                        f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                        f"rollout_after status=skipped_existing",
                        flush=True,
                    )
                else:
                    llm = _llm(config)
                    try:
                        after_result = run_tasks(
                            batch_scenarios,
                            workspace_candidate,
                            rollout_after,
                            llm,
                            **_rollout_kwargs(
                                config,
                                agent_name="evolved_reahe",
                                reflection_mode=_reflection_mode(config, default="warn"),
                            ),
                            resume=resume,
                            close_wait_cleaner=close_wait_cleaner,
                        )
                    except Exception as exc:
                        retest_ok = False
                        retest_error_message = str(exc)
                        write_text(batch_dir / "rollout_after_error.log", f"Retest failed: {exc}\n")
                        status_write_ok = write_rollout_after_status(
                            rollout_after,
                            status="failed",
                            reason="retest_failed",
                            message=retest_error_message or "Retest failed",
                            iteration=iteration,
                            batch=batch_idx,
                        )
                        print(
                            f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                            f"rollout_after status=failed reason={exc}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                            f"rollout_after status=done",
                            flush=True,
                        )
                        status_write_ok = write_rollout_after_status(
                            rollout_after,
                            status="completed",
                            reason=None,
                            message="rollout_after completed",
                            iteration=iteration,
                            batch=batch_idx,
                        )
                    finally:
                        llm.close()
                        close_wait_cleaner.cleanup(
                            f"iteration:{iteration}:batch:{batch_idx}:rollout_after",
                            close_resources=[llm.close],
                        )
            else:
                status_write_ok = write_rollout_after_status(
                    rollout_after,
                    status="skipped",
                    reason="refiner_failed",
                    message=refiner_error_message or "Refiner failed",
                    iteration=iteration,
                    batch=batch_idx,
                )
            if resume and _attribution_complete(batch_dir):
                print(
                    f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                    f"attribution status=skipped_existing",
                    flush=True,
                )
            elif refiner_ok and retest_ok:
                write_attribution(batch_dir, rollout_before, rollout_after)
            if resume and (batch_dir / "batch_decision.json").exists():
                stored = read_json(batch_dir / "batch_decision.json")
                batch_decision = {
                    "decision": stored.get("decision"),
                    "reason": stored.get("reason"),
                    "before_main_score": (stored.get("before_metrics") or {}).get("main_score", 0.0),
                    "after_main_score": (stored.get("after_metrics") or {}).get("main_score", 0.0),
                    "delta_main_score": stored.get("delta_main_score", 0.0),
                    "delta_mean_IRE": stored.get("delta_mean_IRE", 0.0),
                    "delta_mean_TKQR": stored.get("delta_mean_TKQR", 0.0),
                }
            else:
                batch_decision = judge_batch_decision(
                    before_result["metrics"],
                    after_result.get("metrics") or {},
                    refiner_ok=refiner_ok,
                    retest_ok=retest_ok,
                    decision_config=config.get("evolution", {}).get("decision") or {},
                )
                if not status_write_ok:
                    batch_decision["reason"] = (
                        f"{batch_decision.get('reason', '')}; STATUS.json write failed"
                    ).strip("; ")
                finalize_info = finalize_batch_workspace(
                    project_root,
                    batch_dir,
                    str(batch_decision["decision"]),
                    workspace_before,
                    workspace_candidate,
                    workspace_memory=workspace_memory,
                )
                workspace_after = finalize_info["workspace_after"]
                write_batch_decision(
                    batch_dir,
                    before_metrics=before_result["metrics"],
                    after_metrics=after_result.get("metrics") or {},
                    decision=batch_decision,
                    accepted_workspace=to_posix_relpath(workspace_after, batch_dir),
                    finalize_info=finalize_info,
                )
            workspace_after = batch_dir / "workspace_after"
            if not workspace_after.exists():
                finalize_batch_workspace(
                    project_root,
                    batch_dir,
                    str(batch_decision["decision"]),
                    workspace_before,
                    workspace_candidate,
                    workspace_memory=workspace_memory,
                )
            if batch_decision["decision"] == "keep":
                post_rollout_dirs.append(rollout_after)
            else:
                post_rollout_dirs.append(rollout_before)
            accepted_workspace = workspace_after
            write_json(
                batch_dir / "batch_state.json",
                {
                    "iteration": iteration,
                    "batch_index": batch_idx,
                    "batch_count": batch_count,
                    "scenario_ids": batch_ids,
                    "decision": batch_decision.get("decision"),
                },
            )
            batch_summaries.append(
                {
                    "batch_index": batch_idx,
                    "scenario_ids": batch_ids,
                    "decision": batch_decision.get("decision"),
                    "reason": batch_decision.get("reason"),
                    "before_metrics": before_result["metrics"],
                    "after_metrics": after_result.get("metrics") or {},
                    "delta_main_score": batch_decision.get("delta_main_score", 0.0),
                }
            )
            print(
                f"[iteration {iteration}/{iterations} batch {batch_idx}/{batch_count}] "
                f"decision={batch_decision.get('decision')}",
                flush=True,
            )
        pre_update_aggregate = aggregate_rollout_dirs(pre_rollout_dirs, max_turns)
        post_judged_aggregate = aggregate_rollout_dirs(post_rollout_dirs, max_turns)
        iteration_workspace = iteration_dir / "workspace"
        copy_harness_seed(project_root, iteration_workspace, source_workspace=accepted_workspace)
        write_iteration_artifacts(
            iteration_dir,
            iteration=iteration,
            selected_scenario_count=len(scenarios),
            batch_size=batch_size,
            batch_summaries=batch_summaries,
            pre_update_aggregate=pre_update_aggregate,
            post_judged_aggregate=post_judged_aggregate,
            max_turns=max_turns,
            final_workspace=iteration_workspace,
        )
        _write_metadata(iteration_dir, "evolved_reahe", split, task_mode, config, "internal_holdout_result")
        source_workspace = iteration_workspace
        previous_iteration_metrics = post_judged_aggregate
        print(f"[iteration {iteration}/{iterations}] complete workspace={iteration_workspace}", flush=True)
    print(f"Evolve run complete: {run_dir}")
    return 0


def cmd_final_eval(args: argparse.Namespace, config: dict) -> int:
    project_root = Path(config["paths"]["project_root"])
    base_run = resolve_run_dir(project_root, args.best_run)
    run_dir = _create_run(project_root, config, prefix_agent="final_eval")
    _write_latest(project_root, run_dir)
    candidates = sorted(base_run.glob("iteration_*/workspace"))
    source_workspace = candidates[-1] if candidates else project_root / "harness_seed"
    workspace = run_dir / "final_eval" / "workspace"
    copy_harness_seed(project_root, workspace, source_workspace=source_workspace)
    scenarios, split, task_mode = _selected_scenarios(config, args)
    rollout = run_dir / "final_eval" / "rollout"
    result = run_tasks(
        scenarios,
        workspace,
        rollout,
        _llm(config),
        **_rollout_kwargs(config, agent_name="evolved_reahe", reflection_mode=_reflection_mode(config, default="warn")),
    )
    result_type = "final evaluation result" if task_mode == "full" else "internal evolution result"
    _write_metadata(run_dir / "final_eval", "evolved_reahe", split, task_mode, config, result_type)
    write_json(run_dir / "final_eval" / "summary.json", result["metrics"])
    _write_compat_outputs(run_dir / "final_eval", rollout, scenarios, result, config, split, task_mode, "evolved_reahe")
    print(f"Final eval interface completed: {rollout}")
    return 0


def cmd_report(args: argparse.Namespace, config: dict) -> int:
    project_root = Path(config["paths"]["project_root"])
    out = generate_report(project_root, args.run_dir)
    print(f"Wrote report: {out}")
    return 0


def _apply_resume_defaults(run_dir: Path, config: dict, args: argparse.Namespace) -> None:
    previous = _read_resume_config(run_dir)
    eval_cfg = config.setdefault("evaluation", {})
    llm_cfg = config.setdefault("llm", {})

    for attr, key in [
        ("split", "split"),
        ("task_mode", "task_mode"),
        ("dataset_file", "dataset_file"),
        ("max_turns", "max_turns"),
        ("rollouts_per_task", "rollouts_per_task"),
    ]:
        if getattr(args, attr, None) is None and previous.get(key) not in {None, ""}:
            eval_cfg[key] = previous[key]
    if getattr(args, "dataset_number", None) is None and "dataset_number" in previous:
        eval_cfg["dataset_number"] = previous["dataset_number"]
    if getattr(args, "model", None) is None and previous.get("model"):
        llm_cfg["model"] = previous["model"]
    if getattr(args, "reflection_mode", None) is None and previous.get("reflection_mode"):
        config.setdefault("evolution", {})["reflection_mode"] = previous["reflection_mode"]
    if getattr(args, "batch_size", None) is None and previous.get("batch_size") is not None:
        config.setdefault("evolution", {})["batch_size"] = previous["batch_size"]
    if getattr(args, "scenario_count", None) is None and previous.get("scenario_count") is not None:
        config.setdefault("evaluation", {})["scenario_count"] = previous["scenario_count"]


def _read_resume_config(run_dir: Path) -> dict[str, Any]:
    state = run_dir / "run_state.json"
    if state.exists():
        try:
            data = read_json(state)
            if isinstance(data, dict):
                return {
                    **(data.get("evaluation") or {}),
                    **(data.get("llm") or {}),
                    "reflection_mode": (data.get("evolution") or {}).get("reflection_mode"),
                    "batch_size": (data.get("evolution") or {}).get("batch_size"),
                    "scenario_count": (data.get("evaluation") or {}).get("scenario_count"),
                    "target_iterations": data.get("target_iterations"),
                }
        except Exception:
            pass

    for metrics_path in reversed(sorted(run_dir.glob("iteration_*/outputs/metrics/*.json"))):
        try:
            metrics = read_json(metrics_path)
        except Exception:
            continue
        metrics_config = metrics.get("config") if isinstance(metrics, dict) else None
        if isinstance(metrics_config, dict):
            return dict(metrics_config)

    for metadata_path in reversed(sorted(run_dir.glob("iteration_*/run_metadata.json"))):
        try:
            metadata = read_json(metadata_path)
        except Exception:
            continue
        if isinstance(metadata, dict):
            return dict(metadata)
    return {}


def _target_iterations(run_dir: Path, config: dict, args: argparse.Namespace, resume: bool) -> int:
    configured = int(config.get("evolution", {}).get("iterations", 1))
    if not resume or getattr(args, "iterations", None) is not None:
        return configured
    previous = _read_resume_config(run_dir)
    target = previous.get("target_iterations")
    if target:
        return int(target)
    existing = _highest_iteration(run_dir)
    if existing > configured:
        return existing
    return configured


def _batch_size(config: dict) -> int | None:
    raw = config.get("evolution", {}).get("batch_size", 0)
    if raw in (None, ""):
        return None
    size = int(raw)
    return size if size > 0 else None


def _scenario_count(config: dict) -> int | None:
    raw = config.get("evaluation", {}).get("scenario_count", 0)
    if raw in (None, ""):
        return None
    count = int(raw)
    return count if count > 0 else None


def _highest_iteration(run_dir: Path) -> int:
    highest = 0
    for path in run_dir.glob("iteration_*"):
        if not path.is_dir():
            continue
        try:
            highest = max(highest, int(path.name.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return highest


def _write_run_state(
    run_dir: Path,
    config: dict,
    split: str,
    task_mode: str,
    iterations: int,
    resume: bool,
    scenario_count: int | None = None,
) -> None:
    eval_cfg = config.get("evaluation", {})
    batch_size = _batch_size(config)
    write_json(
        run_dir / "run_state.json",
        {
            "command": "evolve",
            "resume": resume,
            "target_iterations": iterations,
            "evaluation": {
                "split": split,
                "task_mode": task_mode,
                "dataset_file": eval_cfg.get("dataset_file"),
                "dataset_number": eval_cfg.get("dataset_number"),
                "max_turns": eval_cfg.get("max_turns"),
                "rollouts_per_task": eval_cfg.get("rollouts_per_task"),
                "selected_task_count": scenario_count,
                "scenario_count": _scenario_count(config) or 0,
            },
            "evolution": {
                "iterations": iterations,
                "batch_size": batch_size or 0,
                "reflection_mode": _reflection_mode(config, default="warn"),
            },
            "llm": {"model": config.get("llm", {}).get("model", "")},
        },
    )


def _close_wait_cleaner(config: dict, run_dir: Path) -> CloseWaitCleaner:
    cleanup = config.get("runtime", {}).get("close_wait_cleanup", {})
    return CloseWaitCleaner(
        enabled=parse_bool(cleanup.get("enabled"), default=True),
        interval_tasks=int(cleanup.get("interval_tasks", 1)),
        interval_seconds=float(cleanup.get("interval_seconds", 180)),
        log_path=run_dir / "close_wait_cleanup.jsonl",
    )


def _memorization_complete(batch_dir: Path, workspace_memory: Path) -> bool:
    result_path = batch_dir / "memorize_result.json"
    if not result_path.exists():
        return False

    try:
        result = read_json(result_path)
    except Exception:
        return False

    if result.get("skip"):
        return workspace_memory.exists()

    memory_path = result.get("memory_path")
    if not memory_path:
        return False

    return (workspace_memory / memory_path).exists()


def _current_batch_memory_paths(batch_dir: Path, workspace_memory: Path) -> list[str]:
    result_path = batch_dir / "memorize_result.json"
    if result_path.exists():
        try:
            result = read_json(result_path)
        except Exception:
            result = {}
        if not result.get("skip"):
            memory_path = result.get("memory_path")
            if isinstance(memory_path, str) and memory_path.strip():
                normalized = memory_path.strip().replace("\\", "/")
                if (workspace_memory / normalized).exists():
                    return [normalized]

    memory_root = workspace_memory / "memory"
    if not memory_root.is_dir():
        return []
    paths: list[str] = []
    for memory_file in sorted(memory_root.rglob("MEMORY.md")):
        if memory_file.is_file():
            paths.append(memory_file.relative_to(workspace_memory).as_posix())
    return paths


def _assert_current_batch_memory_not_in_candidate(
    workspace_before: Path,
    workspace_candidate: Path,
    workspace_memory: Path,
    batch_dir: Path,
) -> None:
    leaked_paths: list[str] = []
    for relative_path in _current_batch_memory_paths(batch_dir, workspace_memory):
        candidate_path = workspace_candidate / relative_path
        before_path = workspace_before / relative_path
        memory_path = workspace_memory / relative_path

        if not candidate_path.is_file():
            continue

        if not before_path.is_file():
            leaked_paths.append(relative_path)
            continue

        before_text = before_path.read_text(encoding="utf-8")
        candidate_text = candidate_path.read_text(encoding="utf-8")
        memory_text = memory_path.read_text(encoding="utf-8")

        if candidate_text != before_text:
            leaked_paths.append(relative_path)
            continue

        if memory_text != before_text and candidate_text == memory_text:
            leaked_paths.append(relative_path)
            continue

    if not leaked_paths:
        return
    payload = {
        "error": "current_batch_memory_leaked_into_workspace_candidate",
        "workspace_before": to_posix_relpath(workspace_before, batch_dir),
        "workspace_candidate": to_posix_relpath(workspace_candidate, batch_dir),
        "workspace_memory": to_posix_relpath(workspace_memory, batch_dir),
        "leaked_paths": leaked_paths,
    }
    write_json(batch_dir / "memory_visibility_error.json", payload)
    raise RuntimeError(
        "Current-batch memory leaked into workspace_candidate before rollout_after: "
        + ", ".join(leaked_paths)
    )


def _write_memory_lifecycle(batch_dir: Path) -> None:
    write_json(
        batch_dir / "memory_lifecycle.json",
        {
            "memorize_from": "rollout_before",
            "memory_staging_workspace": "workspace_memory",
            "rollout_after_workspace": "workspace_candidate",
            "rollout_after_uses_current_batch_memory": False,
            "apply_timing": "next_batch",
            "rollback_policy": "no_rollback",
        },
    )


def _rollout_complete(rollout: Path, scenarios: list, rollouts_per_task: int) -> bool:
    metrics_path = rollout / "metrics.json"
    task_results_path = rollout / "task_results.json"
    if not metrics_path.exists() or not task_results_path.exists():
        return False
    try:
        task_results = read_json(task_results_path)
    except Exception:
        return False
    return isinstance(task_results, list) and len(task_results) == len(scenarios) * rollouts_per_task


def _diagnosis_complete(iteration_dir: Path) -> bool:
    analysis = iteration_dir / "analysis"
    required = [
        analysis / "overview.md",
        analysis / "trace_problem_analysis.json",
        analysis / "component_localization.json",
    ]
    return all(path.exists() for path in required)


def _attribution_complete(iteration_dir: Path) -> bool:
    attribution = iteration_dir / "attribution"
    return (attribution / "task_deltas.csv").exists() and (attribution / "metric_deltas.json").exists()


def _refinement_complete(iteration_dir: Path, workspace: Path) -> bool:
    refiner = iteration_dir / "refiner"
    stage_path = refiner / "STAGE.json"
    if stage_path.exists():
        try:
            stage = read_json(stage_path)
            if stage.get("status") in {"failed", "interrupted"}:
                return False
        except Exception:
            pass
    validation_path = refiner / "validation_report.json"
    if validation_path.exists():
        try:
            report = read_json(validation_path)
            if report.get("ok") is False:
                return False
        except Exception:
            pass
    return (
        (refiner / "fix_plan.json").exists()
        and (refiner / "proposed_edits.json").exists()
        and validation_path.exists()
        and (iteration_dir / "refiner.log").exists()
        and workspace.exists()
    )


def _read_refiner_stage(refiner_dir: Path) -> str:
    stage_path = refiner_dir / "STAGE.json"
    if not stage_path.exists():
        return "unknown"
    try:
        payload = read_json(stage_path)
        return str(payload.get("stage") or "unknown")
    except Exception:
        return "unknown"


def _write_batch_interrupted_state(
    batch_dir: Path,
    *,
    rollout_after: Path,
    workspace_before: Path,
    refiner_stage: str,
    iteration: int,
    batch_idx: int,
) -> None:
    write_rollout_after_status(
        rollout_after,
        status="interrupted",
        reason="manual KeyboardInterrupt during refiner",
        message=f"manual KeyboardInterrupt during refiner (stage={refiner_stage})",
        iteration=iteration,
        batch=batch_idx,
    )
    write_json(
        batch_dir / "batch_decision.json",
        {
            "decision": "rollback_interrupted",
            "accepted": False,
            "reason": "manual interrupt during refiner",
            "accepted_workspace": to_posix_relpath(workspace_before, batch_dir),
            "refiner_stage": refiner_stage,
        },
    )
    write_json(
        batch_dir / "batch_state.json",
        {
            "status": "interrupted",
            "stage": "refiner",
            "recoverable": True,
            "refiner_stage": refiner_stage,
        },
    )
    write_text(batch_dir / "refiner.log", "Refiner interrupted: manual KeyboardInterrupt\n")


def _selected_scenarios(config: dict, args: argparse.Namespace):
    paths = config["paths"]
    project_root = Path(paths["project_root"])
    eval_cfg = config["evaluation"]
    reqelicit_root = resolve_project_path(paths["reqelicitgym_root"], project_root)
    data_path = resolve_dataset_path(
        reqelicit_root,
        dataset_file=eval_cfg.get("dataset_file"),
        dataset_number=eval_cfg.get("dataset_number"),
    )
    eval_cfg["resolved_data_path"] = str(data_path.resolve())
    scenarios = load_scenarios(reqelicit_root, data_path=data_path)
    splits = load_or_create_splits(project_root, scenarios, seed=int(eval_cfg.get("seed", 42)), dataset_path=data_path)
    split = eval_cfg.get("split", "train")
    task_mode = eval_cfg.get("task_mode", "test")
    task_ids = [x.strip() for x in getattr(args, "task_ids", "").split(",") if x.strip()]
    selected = select_scenarios(scenarios, splits, split=split, task_mode=task_mode, task_ids=task_ids)
    return apply_scenario_count(selected, _scenario_count(config)), split, task_mode


def _create_run(project_root: Path, config: dict, prefix_agent: str) -> Path:
    run_name = make_run_name(config.get("project", {}).get("name", "ReqAHE"), f"{prefix_agent}-{config.get('llm', {}).get('model') or 'unconfigured'}")
    return ensure_dir(project_root / "runs" / run_name)


def _write_compat_outputs(
    container_dir: Path,
    rollout_dir: Path,
    scenarios: list,
    result: dict,
    config: dict,
    split: str,
    task_mode: str,
    agent_name: str,
) -> None:
    output_root = ensure_dir(container_dir / "outputs")
    conversation_dir = ensure_dir(output_root / "conversation")
    metrics_dir = ensure_dir(output_root / "metrics")
    label = _output_label(config, task_mode, container_dir.name)
    conversations = _conversation_output(scenarios, result, config, rollout_dir)
    metrics = _metrics_output(
        rollout_dir,
        scenarios,
        result,
        config,
        split,
        task_mode,
        agent_name,
        base_dir=container_dir,
        batch_size=_batch_size(config),
    )
    conversation_path = conversation_dir / f"{label}.json"
    metrics_path = metrics_dir / f"{label}.json"
    write_json(conversation_path, conversations)
    write_json(metrics_path, metrics)
    print(f"[outputs] conversation={conversation_path}", flush=True)
    print(f"[outputs] metrics={metrics_path}", flush=True)


def _require_llm_config(config: dict) -> None:
    llm = config.get("llm", {})
    missing = [key for key in ["api_key", "model"] if not llm.get(key)]
    if missing:
        raise RuntimeError(f"Missing required LLM config values: {', '.join(missing)}")


def _write_latest(project_root: Path, run_dir: Path) -> None:
    write_text(project_root / "runs" / "latest.txt", to_posix_relpath(run_dir, project_root))


def _write_metadata(target_dir: Path, agent: str, split: str, task_mode: str, config: dict, result_type: str) -> None:
    project_root = Path(config["paths"]["project_root"])
    write_json(
        target_dir / "run_metadata.json",
        {
            "agent": agent,
            "split": split,
            "task_mode": task_mode,
            "result_type": result_type,
            "data_path": to_posix_relpath(config["evaluation"].get("resolved_data_path", ""), project_root)
            if config["evaluation"].get("resolved_data_path")
            else "",
            "max_turns": config["evaluation"]["max_turns"],
            "rollouts_per_task": config["evaluation"]["rollouts_per_task"],
            "batch_size": _batch_size(config) or 0,
            "model": config.get("llm", {}).get("model", ""),
            "fairness": "not strictly paper-fair if evolution used any ReqElicitGym scenario feedback",
        },
    )

if __name__ == "__main__":
    raise SystemExit(main())
