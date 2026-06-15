#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reqahe.config import apply_cli_overrides, load_config
from reqahe.infra.io import ensure_dir, write_json
from reqahe.infra.network import CloseWaitCleaner
from reqahe.infra.paths import safe_name
from reqahe.runtime.dataset import load_scenarios
from reqahe.runtime.eval_outputs import (
    conversation_output,
    independent_test_metrics_output,
    llm_client,
    reflection_mode,
    rollout_kwargs,
)
from reqahe.runtime.runner import run_tasks
from reqahe.utils.paths import resolve_project_path, to_posix_relpath

DEFAULT_TEST_DATA = "ReqElicitGym/data/test.json"
ITERATION_DIR_RE = re.compile(r"^iteration_(\d+)$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate each iteration workspace of an evolved run on an independent test set.",
    )
    parser.add_argument("--run-dir", required=True, help="Existing evolved run directory (required).")
    parser.add_argument(
        "--test-data",
        default=DEFAULT_TEST_DATA,
        help=f"Independent test dataset JSON (default: {DEFAULT_TEST_DATA}).",
    )
    parser.add_argument("--config", default=None)
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
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete existing test_outputs for an iteration and rerun evaluation.",
    )
    parser.add_argument(
        "--keep-rollout",
        action="store_true",
        help="Keep rollout traces under test_outputs/rollout/ for debugging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned evaluations without calling the LLM.",
    )
    return parser.parse_args(argv)


def find_iteration_dirs(run_dir: Path) -> list[Path]:
    matched: list[tuple[int, Path]] = []
    for child in run_dir.iterdir():
        if not child.is_dir():
            continue
        match = ITERATION_DIR_RE.match(child.name)
        if match:
            matched.append((int(match.group(1)), child))
    return [path for _, path in sorted(matched)]


def _batch_number(path: Path) -> int:
    return int(path.name.split("_", 1)[1])


def resolve_iteration_workspace(iteration_dir: Path) -> Path | None:
    workspace = iteration_dir / "workspace"
    if workspace.exists():
        return workspace
    batch_dirs = sorted(
        (item for item in iteration_dir.iterdir() if item.is_dir() and item.name.startswith("batch_")),
        key=_batch_number,
    )
    if batch_dirs:
        workspace_after = batch_dirs[-1] / "workspace_after"
        if workspace_after.exists():
            return workspace_after
    return None


def load_independent_test_scenarios(
    project_root: Path,
    reqelicitgym_root: Path,
    test_data: Path,
) -> tuple[list, Path]:
    if not test_data.exists():
        raise FileNotFoundError(f"Independent test dataset not found: {test_data}")
    scenarios = load_scenarios(reqelicitgym_root, data_path=test_data)
    return scenarios, test_data


def build_output_label(model: str, iteration_name: str) -> str:
    return f"reqahe_{safe_name(model)}_test_full_{iteration_name}"


def iteration_outputs_complete(iteration_dir: Path, label: str) -> bool:
    conversation_path = iteration_dir / "test_outputs" / "conversation" / f"{label}.json"
    metrics_path = iteration_dir / "test_outputs" / "metrics" / f"{label}.json"
    return conversation_path.exists() and metrics_path.exists()


def _require_llm_config(config: dict) -> None:
    llm = config.get("llm", {})
    missing = [key for key in ["api_key", "model"] if not llm.get(key)]
    if missing:
        raise RuntimeError(f"Missing required LLM config values: {', '.join(missing)}")


def _close_wait_cleaner(config: dict, run_dir: Path) -> CloseWaitCleaner:
    from reqahe.config import parse_bool

    cleanup = config.get("runtime", {}).get("close_wait_cleanup", {})
    return CloseWaitCleaner(
        enabled=parse_bool(cleanup.get("enabled"), default=True),
        interval_tasks=int(cleanup.get("interval_tasks", 1)),
        interval_seconds=float(cleanup.get("interval_seconds", 180)),
        log_path=run_dir / "independent_test_close_wait_cleanup.jsonl",
    )


def _remove_iteration_test_outputs(iteration_dir: Path, label: str, *, keep_rollout: bool) -> None:
    test_outputs = iteration_dir / "test_outputs"
    for subpath in (
        test_outputs / "conversation" / f"{label}.json",
        test_outputs / "metrics" / f"{label}.json",
    ):
        if subpath.exists():
            subpath.unlink()
    if not keep_rollout:
        rollout_dir = test_outputs / "rollout"
        if rollout_dir.exists():
            shutil.rmtree(rollout_dir)
    tmp_rollout = test_outputs / "_rollout_tmp"
    if tmp_rollout.exists():
        shutil.rmtree(tmp_rollout)


def evaluate_one_iteration(
    *,
    iteration_dir: Path,
    workspace: Path,
    scenarios: list,
    config: dict,
    dataset_relpath: str,
    keep_rollout: bool,
    overwrite: bool,
    dry_run: bool,
    run_dir: Path,
) -> str:
    iteration_name = iteration_dir.name
    model = config.get("llm", {}).get("model") or "model"
    label = build_output_label(model, iteration_name)
    test_outputs = iteration_dir / "test_outputs"
    conversation_path = test_outputs / "conversation" / f"{label}.json"
    metrics_path = test_outputs / "metrics" / f"{label}.json"
    rollout_path = test_outputs / ("rollout" if keep_rollout else "_rollout_tmp")

    if iteration_outputs_complete(iteration_dir, label) and not overwrite:
        print(f"[skip] {iteration_name}: existing test outputs found at {test_outputs}", flush=True)
        return "skipped"

    if dry_run:
        print(
            f"[dry-run] iteration={iteration_name} "
            f"workspace={to_posix_relpath(workspace, iteration_dir)} "
            f"test_data={dataset_relpath} "
            f"conversation={to_posix_relpath(conversation_path, iteration_dir)} "
            f"metrics={to_posix_relpath(metrics_path, iteration_dir)}",
            flush=True,
        )
        return "planned"

    if overwrite:
        _remove_iteration_test_outputs(iteration_dir, label, keep_rollout=keep_rollout)

    ensure_dir(conversation_path.parent)
    ensure_dir(metrics_path.parent)
    if keep_rollout:
        ensure_dir(rollout_path)
    else:
        if rollout_path.exists():
            shutil.rmtree(rollout_path)
        ensure_dir(rollout_path)

    result = run_tasks(
        scenarios,
        workspace,
        rollout_path,
        llm_client(config),
        close_wait_cleaner=_close_wait_cleaner(config, run_dir),
        **rollout_kwargs(
            config,
            agent_name="evolved_reahe",
            reflection_mode=reflection_mode(config, default="warn"),
        ),
    )

    workspace_relpath = to_posix_relpath(workspace, iteration_dir)
    conversations = conversation_output(
        scenarios,
        result,
        config,
        rollout_path,
        blank_trace_dir=not keep_rollout,
    )
    metrics = independent_test_metrics_output(
        scenarios,
        result,
        config,
        iteration_name=iteration_name,
        workspace_dir=workspace_relpath,
        dataset_relpath=dataset_relpath,
        blank_trace_dir=not keep_rollout,
    )
    write_json(conversation_path, conversations)
    write_json(metrics_path, metrics)
    print(f"[outputs] conversation={conversation_path}", flush=True)
    print(f"[outputs] metrics={metrics_path}", flush=True)

    if not keep_rollout and rollout_path.exists():
        shutil.rmtree(rollout_path)

    return "evaluated"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = apply_cli_overrides(load_config(args.config), args)
    project_root = Path(config["paths"]["project_root"])
    run_dir = resolve_project_path(args.run_dir, project_root)
    if not run_dir.exists():
        raise SystemExit(f"Run directory not found: {run_dir}")
    if not run_dir.is_dir():
        raise SystemExit(f"Run path is not a directory: {run_dir}")

    reqelicitgym_root = resolve_project_path(config["paths"]["reqelicitgym_root"], project_root)
    test_data = resolve_project_path(args.test_data, project_root)
    scenarios, resolved_test_data = load_independent_test_scenarios(project_root, reqelicitgym_root, test_data)
    dataset_relpath = to_posix_relpath(resolved_test_data, project_root)
    config.setdefault("evaluation", {})["resolved_data_path"] = str(resolved_test_data.resolve())

    iteration_dirs = find_iteration_dirs(run_dir)
    if not iteration_dirs:
        raise SystemExit(f"No iteration directories found under: {run_dir}")

    if not args.dry_run:
        _require_llm_config(config)

    print(
        f"[independent-test] run_dir={to_posix_relpath(run_dir, project_root)} "
        f"test_data={dataset_relpath} iterations={len(iteration_dirs)} "
        f"tasks={len(scenarios)} dry_run={args.dry_run}",
        flush=True,
    )

    counts = {"planned": 0, "evaluated": 0, "skipped": 0, "missing_workspace": 0}
    for iteration_dir in iteration_dirs:
        workspace = resolve_iteration_workspace(iteration_dir)
        if workspace is None:
            counts["missing_workspace"] += 1
            print(f"[warn] {iteration_dir.name}: no workspace found, skipping", flush=True)
            continue
        status = evaluate_one_iteration(
            iteration_dir=iteration_dir,
            workspace=workspace,
            scenarios=scenarios,
            config=config,
            dataset_relpath=dataset_relpath,
            keep_rollout=args.keep_rollout,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            run_dir=run_dir,
        )
        counts[status] = counts.get(status, 0) + 1

    print(
        f"[independent-test] complete planned={counts.get('planned', 0)} "
        f"evaluated={counts.get('evaluated', 0)} skipped={counts.get('skipped', 0)} "
        f"missing_workspace={counts.get('missing_workspace', 0)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
