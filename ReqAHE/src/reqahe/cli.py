from __future__ import annotations

import argparse
from pathlib import Path

from reqahe.attribution.delta import write_attribution
from reqahe.config import apply_cli_overrides, load_config, role_model
from reqahe.debugger.rules import generate_analysis
from reqahe.envs.dataset import load_or_create_splits, load_scenarios, resolve_dataset_path, select_scenarios
from reqahe.evolver.rules import evolve_workspace
from reqahe.harness.workspace import copy_harness_seed
from reqahe.inspection import inspect_sources
from reqahe.llm.client import OpenAICompatibleClient
from reqahe.reporting.report import generate_report, resolve_run_dir
from reqahe.rollout.runner import run_tasks
from reqahe.utils.io import ensure_dir, read_json, write_json, write_text
from reqahe.utils.paths import make_run_name, repo_root, safe_name


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = apply_cli_overrides(load_config(getattr(args, "config", None)), args)
    if args.command in {"run-baseline", "evolve", "final-eval"} and getattr(args, "agent", "") != "local_ontoagent":
        _require_llm_config(config)
    if args.command == "inspect":
        return cmd_inspect(args, config)
    if args.command == "run-baseline":
        return cmd_run_baseline(args, config)
    if args.command == "evolve":
        return cmd_evolve(args, config)
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
    inspect.add_argument("--typoagent-root", default=None)
    inspect.add_argument("--reqelicitgym-root", default=None)
    inspect.add_argument("--ahe-root", default=None)
    inspect.add_argument("--dataset-file", default=None)
    inspect.add_argument("--dataset-number", default=None)

    baseline = sub.add_parser("run-baseline")
    _add_runtime_args(baseline)
    baseline.add_argument("--agent", choices=["seed_freeform", "local_ontoagent"], required=True)

    evolve = sub.add_parser("evolve")
    _add_runtime_args(evolve)
    evolve.add_argument("--iterations", type=int, default=None)
    evolve.add_argument("--middleware-mode", choices=["observe", "warn", "enforce"], default=None)

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
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--rollouts-per-task", type=int, default=None)


def cmd_inspect(args: argparse.Namespace, config: dict) -> int:
    paths = config["paths"]
    project_root = Path(args.project_root or paths["project_root"])
    out = inspect_sources(
        project_root=project_root,
        typoagent_root=args.typoagent_root or paths["typoagent_root"],
        reqelicitgym_root=args.reqelicitgym_root or paths["reqelicitgym_root"],
        ahe_root=args.ahe_root or paths["ahe_root"],
        dataset_file=config["evaluation"].get("dataset_file"),
        dataset_number=config["evaluation"].get("dataset_number"),
    )
    print(f"Wrote inspection notes: {out}")
    return 0


def cmd_run_baseline(args: argparse.Namespace, config: dict) -> int:
    project_root = Path(config["paths"]["project_root"])
    run_dir = _create_run(project_root, config, prefix_agent=args.agent)
    _write_latest(project_root, run_dir)
    if args.agent == "local_ontoagent":
        _record_local_ontoagent_unavailable(run_dir, config)
        print(f"local_ontoagent unavailable; reason written under {run_dir}")
        return 0
    workspace = run_dir / "baseline_seed_freeform" / "workspace"
    copy_harness_seed(project_root, workspace)
    rollout = run_dir / "baseline_seed_freeform" / "rollout"
    scenarios, split, task_mode = _selected_scenarios(config, args)
    result = run_tasks(
        scenarios,
        workspace,
        rollout,
        _llm(config),
        interviewer_model=role_model(config, "interviewer"),
        oracle_model=role_model(config, "oracle"),
        evaluator_model=role_model(config, "evaluator"),
        max_turns=int(config["evaluation"]["max_turns"]),
        rollouts_per_task=int(config["evaluation"]["rollouts_per_task"]),
        middleware_mode="observe",
        agent_name=args.agent,
    )
    _write_metadata(run_dir / "baseline_seed_freeform", args.agent, split, task_mode, config, "internal_holdout_result")
    write_json(run_dir / "baseline_seed_freeform" / "summary.json", result["metrics"])
    _write_compat_outputs(run_dir / "baseline_seed_freeform", rollout, scenarios, result, config, split, task_mode, args.agent)
    print(f"Baseline run complete: {rollout}")
    return 0


def cmd_evolve(args: argparse.Namespace, config: dict) -> int:
    project_root = Path(config["paths"]["project_root"])
    run_dir = _create_run(project_root, config, prefix_agent="evolved_reahe")
    _write_latest(project_root, run_dir)
    scenarios, split, task_mode = _selected_scenarios(config, args)
    previous_rollout = None
    previous_metrics = None
    source_workspace = None
    iterations = int(config.get("evolution", {}).get("iterations", 1))
    print(
        f"[run] evolve run_dir={run_dir} split={split} task_mode={task_mode} "
        f"tasks={len(scenarios)} iterations={iterations} max_turns={config['evaluation']['max_turns']}",
        flush=True,
    )
    for iteration in range(1, iterations + 1):
        iteration_dir = ensure_dir(run_dir / f"iteration_{iteration:03d}")
        workspace = iteration_dir / "workspace"
        copy_harness_seed(project_root, workspace, source_workspace=source_workspace)
        rollout = iteration_dir / "rollout"
        print(f"[iteration {iteration}/{iterations}] rollout status=running dir={iteration_dir}", flush=True)
        result = run_tasks(
            scenarios,
            workspace,
            rollout,
            _llm(config),
            interviewer_model=role_model(config, "interviewer"),
            oracle_model=role_model(config, "oracle"),
            evaluator_model=role_model(config, "evaluator"),
            max_turns=int(config["evaluation"]["max_turns"]),
            rollouts_per_task=int(config["evaluation"]["rollouts_per_task"]),
            middleware_mode=config.get("evolution", {}).get("middleware_mode", "warn"),
            agent_name="evolved_reahe",
        )
        print(f"[iteration {iteration}/{iterations}] rollout status=done", flush=True)
        _write_metadata(iteration_dir, "evolved_reahe", split, task_mode, config, "internal_holdout_result")
        write_json(iteration_dir / "summary.json", result["metrics"])
        _write_compat_outputs(iteration_dir, rollout, scenarios, result, config, split, task_mode, "evolved_reahe")
        print(f"[iteration {iteration}/{iterations}] analysis status=running", flush=True)
        generate_analysis(iteration_dir, rollout, _llm(config), role_model(config, "debugger"), previous_metrics=previous_metrics)
        print(f"[iteration {iteration}/{iterations}] analysis status=done", flush=True)
        write_attribution(iteration_dir, previous_rollout, rollout)
        print(f"[iteration {iteration}/{iterations}] evolution status=running", flush=True)
        evolve_workspace(iteration_dir, workspace, iteration, _llm(config), role_model(config, "evolver"))
        print(f"[iteration {iteration}/{iterations}] evolution status=done", flush=True)
        source_workspace = workspace
        previous_rollout = rollout
        previous_metrics = result["metrics"]
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
        interviewer_model=role_model(config, "interviewer"),
        oracle_model=role_model(config, "oracle"),
        evaluator_model=role_model(config, "evaluator"),
        max_turns=int(config["evaluation"]["max_turns"]),
        rollouts_per_task=int(config["evaluation"]["rollouts_per_task"]),
        middleware_mode="warn",
        agent_name="evolved_reahe",
    )
    result_type = "paper_style_result" if task_mode == "full" else "internal_holdout_result"
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


def _llm(config: dict) -> OpenAICompatibleClient:
    llm = config.get("llm", {})
    return OpenAICompatibleClient(
        api_key=llm.get("api_key", ""),
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        temperature=float(llm.get("temperature", 0.2)),
        max_tokens=int(llm.get("max_tokens", 1200)),
    )


def _selected_scenarios(config: dict, args: argparse.Namespace):
    paths = config["paths"]
    eval_cfg = config["evaluation"]
    data_path = resolve_dataset_path(
        paths["reqelicitgym_root"],
        dataset_file=eval_cfg.get("dataset_file"),
        dataset_number=eval_cfg.get("dataset_number"),
    )
    eval_cfg["resolved_data_path"] = str(data_path.resolve())
    scenarios = load_scenarios(paths["reqelicitgym_root"], data_path=data_path)
    splits = load_or_create_splits(paths["project_root"], scenarios, seed=int(eval_cfg.get("seed", 42)), dataset_path=data_path)
    split = eval_cfg.get("split", "train")
    task_mode = eval_cfg.get("task_mode", "test")
    task_ids = [x.strip() for x in getattr(args, "task_ids", "").split(",") if x.strip()]
    return select_scenarios(scenarios, splits, split=split, task_mode=task_mode, task_ids=task_ids), split, task_mode


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
    conversations = _conversation_output(scenarios, result, config)
    metrics = _metrics_output(rollout_dir, scenarios, result, config, split, task_mode, agent_name)
    conversation_path = conversation_dir / f"{label}.json"
    metrics_path = metrics_dir / f"{label}.json"
    write_json(conversation_path, conversations)
    write_json(metrics_path, metrics)
    print(f"[outputs] conversation={conversation_path}", flush=True)
    print(f"[outputs] metrics={metrics_path}", flush=True)


def _output_label(config: dict, task_mode: str, container_name: str) -> str:
    model = safe_name(config.get("llm", {}).get("model") or "model")
    mode = safe_name(task_mode)
    container = safe_name(container_name)
    return f"reqahe_{model}_{mode}_{container}"


def _conversation_output(scenarios: list, result: dict, config: dict) -> list[dict]:
    scenarios_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    rows = []
    for task_result in result["task_results"]:
        trace = read_json(Path(task_result["trace_dir"]) / "clean_trace.json")
        scenario = scenarios_by_id[trace["scenario_id"]]
        rows.append(
            {
                "task_id": scenario.scenario_id,
                "task_name": scenario.name,
                "application_type": scenario.app_type,
                "initial_requirements": scenario.initial_req,
                "user_stories": scenario.final_requirements,
                "user_answer_quality": "llm_oracle",
                "interviewer_model": role_model(config, "interviewer"),
                "oracle_model": role_model(config, "oracle"),
                "evaluator_model": role_model(config, "evaluator"),
                "conversation": _conversation_turns(trace),
                "final_metrics": trace["final_metrics"],
                "trace_dir": task_result["trace_dir"],
            }
        )
    return rows


def _conversation_turns(trace: dict) -> list[dict]:
    total = max(1, trace["final_metrics"].get("total_implicit_requirements", 0))
    elicited_ids: set[str] = set()
    turns = []
    for turn in trace["turns"]:
        evaluator = turn.get("evaluator", {})
        for req_id in evaluator.get("hit_requirement_ids", []):
            elicited_ids.add(str(req_id))
        if turn["action"] == "ask_question":
            turns.append(
                {
                    "turn": turn["turn_index"] + 1,
                    "interviewer": turn.get("question", ""),
                    "user": turn.get("oracle_answer", ""),
                    "action_type": evaluator.get("action_type", "probe"),
                    "is_relevant_to_url": bool(evaluator.get("hit")),
                    "elicitation_ratio": round(len(elicited_ids) / total, 6),
                    "hit_requirement_ids": evaluator.get("hit_requirement_ids", []),
                }
            )
        else:
            turns.append(
                {
                    "turn": turn["turn_index"] + 1,
                    "interviewer": turn.get("finish_summary", ""),
                    "user": "",
                    "action_type": "finish",
                    "is_relevant_to_url": False,
                    "elicitation_ratio": round(len(elicited_ids) / total, 6),
                    "hit_requirement_ids": [],
                }
            )
    return turns


def _metrics_output(
    rollout_dir: Path,
    scenarios: list,
    result: dict,
    config: dict,
    split: str,
    task_mode: str,
    agent_name: str,
) -> dict:
    aggregate = result["metrics"]
    task_results = result["task_results"]
    total_hidden = sum(item["metrics"].get("total_implicit_requirements", 0) for item in task_results)
    total_elicited = sum(item["metrics"].get("hit_count", 0) for item in task_results)
    return {
        "data_path": config["evaluation"].get("resolved_data_path", ""),
        "rollout_dir": str(Path(rollout_dir).resolve()),
        "config": {
            "agent": agent_name,
            "interviewer_model": role_model(config, "interviewer"),
            "oracle_model": role_model(config, "oracle"),
            "evaluator_model": role_model(config, "evaluator"),
            "debugger_model": role_model(config, "debugger"),
            "evolver_model": role_model(config, "evolver"),
            "split": split,
            "task_mode": task_mode,
            "dataset_file": config["evaluation"].get("dataset_file", ""),
            "dataset_number": config["evaluation"].get("dataset_number", ""),
            "max_turns": config["evaluation"]["max_turns"],
            "rollouts_per_task": config["evaluation"]["rollouts_per_task"],
        },
        "overall_evaluation": {
            "total_test_samples": len(scenarios),
            "total_hidden_requirements": total_hidden,
            "total_elicited": total_elicited,
            "average_elicitation_ratio": aggregate["mean_IRE"],
            "average_tkqr": aggregate["mean_TKQR"],
            "average_ora": aggregate.get("approx_ESR", 0.0),
            "main_score": aggregate["main_score"],
            "type_coverage": aggregate.get("type_coverage", {}),
        },
        "task_results": task_results,
    }


def _require_llm_config(config: dict) -> None:
    llm = config.get("llm", {})
    missing = [key for key in ["api_key", "model"] if not llm.get(key)]
    if missing:
        raise RuntimeError(f"Missing required LLM config values: {', '.join(missing)}")


def _write_latest(project_root: Path, run_dir: Path) -> None:
    write_text(project_root / "runs" / "latest.txt", str(run_dir.resolve()))


def _write_metadata(target_dir: Path, agent: str, split: str, task_mode: str, config: dict, result_type: str) -> None:
    write_json(
        target_dir / "run_metadata.json",
        {
            "agent": agent,
            "split": split,
            "task_mode": task_mode,
            "result_type": result_type,
            "data_path": config["evaluation"].get("resolved_data_path", ""),
            "max_turns": config["evaluation"]["max_turns"],
            "rollouts_per_task": config["evaluation"]["rollouts_per_task"],
            "model": config.get("llm", {}).get("model", ""),
            "fairness": "not strictly paper-fair if evolution used any ReqElicitGym scenario feedback",
        },
    )


def _record_local_ontoagent_unavailable(run_dir: Path, config: dict) -> None:
    typo_root = Path(config["paths"]["typoagent_root"])
    cache_candidates = [
        typo_root / "output" / "save_tree" / "Typo_Tree.json",
        typo_root / "output" / "save_tree" / "LLMTree.auto-p.json",
    ]
    cache_found = [str(p) for p in cache_candidates if p.exists()]
    reason = [
        "local_ontoagent was not executed by this interface.",
        f"ontology_cache_found={cache_found or 'none'}",
        "If cache is absent, ontology induction must follow TypoAgent build flow and must not use ReqElicitGym evaluation scenarios.",
        "paper_target is reported separately: IRE=0.69, TKQR=0.59.",
    ]
    write_text(run_dir / "local_ontoagent_unavailable_reason.txt", "\n".join(reason) + "\n")
    write_json(run_dir / "paper_target.json", {"method": "paper_target", "IRE": 0.69, "TKQR": 0.59})


if __name__ == "__main__":
    raise SystemExit(main())
