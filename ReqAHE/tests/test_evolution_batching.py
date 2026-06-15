from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reqahe.evolution.attribution import judge_batch_decision
from reqahe.cli import (
    _assert_current_batch_memory_not_in_candidate,
    _memorization_complete,
    _run_evolve,
    _scenario_count,
    _selected_scenarios,
    build_parser,
    cmd_final_eval,
)
from reqahe.config import apply_cli_overrides, load_config
from reqahe.evolution.batching import apply_scenario_count, split_scenarios_into_batches
from reqahe.infra.io import read_json, write_json
from reqahe.reporting.report import generate_report, resolve_run_dir


class Scenario:
    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self.name = scenario_id
        self.app_type = "web"
        self.initial_req = "req"
        self.final_requirements = []


def _metrics(main_score: float, ire: float | None = None, tkqr: float | None = None) -> dict:
    ire = ire if ire is not None else main_score
    tkqr = tkqr if tkqr is not None else main_score
    return {
        "mean_IRE": ire,
        "mean_TKQR": tkqr,
        "main_score": main_score,
        "type_coverage": {"interaction": 0.0, "content": 1.0, "style": 0.0},
        "early_finish_rate": 0.0,
        "max_turns": 4,
        "task_count": 1,
    }


def _task_result(scenario_id: str, ire: float, tkqr: float) -> dict:
    return {
        "scenario_id": scenario_id,
        "metrics": {"IRE": ire, "TKQR": tkqr, "turns": 1, "probe_effectiveness": 0.0, "early_finish": False, "type_coverage": {}},
        "trace_dir": scenario_id,
    }


def _fake_memorize(batch_dir, rollout_dir, workspace_dir, llm, model, config=None):
    workspace = Path(workspace_dir)
    memory_dir = workspace / "memory" / "new_type"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "# New Type\n\n## Recorded Hit Points\n- new memory point\n",
        encoding="utf-8",
    )
    write_json(
        Path(batch_dir) / "memorize_result.json",
        {
            "skip": False,
            "memory_path": "memory/new_type/MEMORY.md",
            "apply_timing": "next_batch",
        },
    )
    return {"skip": False, "memory_path": "memory/new_type/MEMORY.md", "apply_timing": "next_batch"}


def test_split_scenarios_into_batches_counts() -> None:
    scenarios = [Scenario(f"s{idx}") for idx in range(21)]
    batches = split_scenarios_into_batches(scenarios, 3)
    assert len(batches) == 7
    assert len(batches[-1]) == 3
    assert [item.scenario_id for item in batches[0]] == ["s0", "s1", "s2"]
    assert [item.scenario_id for item in batches[-1]] == ["s18", "s19", "s20"]

    scenarios20 = [Scenario(f"s{idx}") for idx in range(20)]
    batches20 = split_scenarios_into_batches(scenarios20, 3)
    assert len(batches20) == 7
    assert len(batches20[-1]) == 2

    single = split_scenarios_into_batches(scenarios, None)
    assert len(single) == 1 and len(single[0]) == 21
    single_zero = split_scenarios_into_batches(scenarios, 0)
    assert len(single_zero) == 1 and len(single_zero[0]) == 21


def test_split_scenarios_no_wraparound() -> None:
    scenarios = [Scenario(f"s{idx}") for idx in range(5)]
    batches = split_scenarios_into_batches(scenarios, 2)
    flat = [item.scenario_id for batch in batches for item in batch]
    assert flat == ["s0", "s1", "s2", "s3", "s4"]


def test_apply_scenario_count() -> None:
    scenarios = [Scenario(f"s{idx}") for idx in range(30)]
    assert len(apply_scenario_count(scenarios, 21)) == 21
    assert len(apply_scenario_count(scenarios, 0)) == 30
    assert len(apply_scenario_count(scenarios, None)) == 30


def test_scenario_count_cli_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["evolve", "--scenario-count", "21"])
    config = apply_cli_overrides(load_config(), args)
    assert config["evaluation"]["scenario_count"] == 21
    assert _scenario_count(config) == 21


def test_judge_batch_decision_rules() -> None:
    before = _metrics(0.4)
    keep = judge_batch_decision(before, _metrics(0.5), refiner_ok=True, retest_ok=True)
    tie = judge_batch_decision(before, _metrics(0.4), refiner_ok=True, retest_ok=True)
    rollback = judge_batch_decision(before, _metrics(0.3), refiner_ok=True, retest_ok=True)
    refiner_failed = judge_batch_decision(before, _metrics(0.9), refiner_ok=False, retest_ok=True)
    retest_failed = judge_batch_decision(before, _metrics(0.9), refiner_ok=True, retest_ok=False)

    assert keep["decision"] == "keep"
    assert tie["decision"] == "keep"
    assert rollback["decision"] == "rollback"
    assert refiner_failed["decision"] == "rollback_refiner_failed"
    assert retest_failed["decision"] == "rollback_retest_failed"


def test_evolve_structure_with_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "code_agent.yaml").write_text("components: []\n", encoding="utf-8")
    (harness_seed / "system_prompt.md").write_text("prompt\n", encoding="utf-8")

    scenarios = [Scenario(f"s{idx}") for idx in range(7)]

    def fake_select(config, args):
        return scenarios, "train", "full"

    score = {"value": 0.2}

    def fake_run_tasks(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs):
        rollout_path = Path(rollout_dir)
        rollout_path.mkdir(parents=True, exist_ok=True)
        if "rollout_after" in str(rollout_path):
            score["value"] += 0.05
        task_results = []
        for scenario in batch_scenarios:
            task_dir = rollout_path / f"{scenario.scenario_id}__r0"
            task_dir.mkdir(parents=True, exist_ok=True)
            write_json(task_dir / "clean_trace.json", {"scenario_id": scenario.scenario_id, "turns": [], "final_metrics": {}})
            write_json(task_dir / "metrics.json", {"IRE": score["value"], "TKQR": score["value"]})
            task_results.append(_task_result(scenario.scenario_id, score["value"], score["value"]))
        metrics = _metrics(score["value"])
        write_json(rollout_path / "metrics.json", metrics)
        write_json(rollout_path / "task_results.json", task_results)
        return {"metrics": metrics, "task_results": task_results}

    def fake_diagnosis(batch_dir, rollout_dir, llm, model, **kwargs):
        analysis = Path(batch_dir) / "analysis"
        analysis.mkdir(parents=True, exist_ok=True)
        write_json(
            analysis / "trace_problem_analysis.json",
            {
                "diagnosis_summary": "ok",
                "evidence_limitations": [],
                "failure_findings": [],
                "route_observations": [],
                "candidate_root_causes": [],
            },
        )
        write_json(
            analysis / "component_localization.json",
            {
                "localization_summary": "overview",
                "component_findings": [],
                "refiner_guidance": {},
            },
        )
        (analysis / "overview.md").write_text("overview\n", encoding="utf-8")
        return analysis

    def fake_refine(batch_dir, workspace_dir, iteration, llm, model, **kwargs):
        refiner = Path(batch_dir) / "refiner"
        refiner.mkdir(parents=True, exist_ok=True)
        write_json(refiner / "fix_plan.json", {"fix_plan": []})
        write_json(refiner / "proposed_edits.json", {"changes": [], "file_edits": []})
        write_json(refiner / "validation_report.json", {"ok": True, "errors": [], "warnings": []})
        (Path(batch_dir) / "refiner.log").write_text("ok\n", encoding="utf-8")
        return Path(workspace_dir)

    monkeypatch.setattr("reqahe.cli._selected_scenarios", fake_select)
    monkeypatch.setattr("reqahe.cli.run_tasks", fake_run_tasks)
    monkeypatch.setattr("reqahe.cli.run_elicitation_diagnosis", fake_diagnosis)
    monkeypatch.setattr("reqahe.cli.refine_harness", fake_refine)
    monkeypatch.setattr("reqahe.cli.memorize_rollout", _fake_memorize)
    monkeypatch.setattr("reqahe.cli._llm", lambda config: MagicMock(close=lambda: None))
    monkeypatch.setattr("reqahe.cli._close_wait_cleaner", lambda config, run_dir: MagicMock(cleanup=lambda *args, **kwargs: None))
    monkeypatch.setattr("reqahe.cli._require_llm_config", lambda config: None)
    monkeypatch.setattr("reqahe.cli._write_compat_outputs", lambda *args, **kwargs: None)

    config = load_config()
    config["paths"]["project_root"] = str(project_root)
    config["llm"] = {"api_key": "k", "model": "m", "base_url": "http://x"}
    config["evaluation"]["scenario_count"] = 7
    config["evolution"]["iterations"] = 1
    config["evolution"]["batch_size"] = 3

    args = SimpleNamespace(
        config=None,
        split="train",
        task_mode="full",
        dataset_file=None,
        dataset_number=None,
        task_ids="",
        scenario_count=7,
        iterations=1,
        batch_size=3,
        reflection_mode="warn",
        base_url=None,
        api_key=None,
        model=None,
        temperature=None,
        max_turns=4,
        rollouts_per_task=1,
        disable_close_wait_cleanup=False,
        close_wait_cleanup_interval_tasks=None,
        close_wait_cleanup_interval_seconds=None,
        resume_run_dir=None,
    )

    assert _run_evolve(args, config) == 0
    run_dirs = list((project_root / "runs").glob("ReqAHE-evolved_reahe-*"))
    assert run_dirs
    run_dir = run_dirs[0]
    iteration_dir = run_dir / "iteration_001"
    assert (iteration_dir / "iteration_metrics.json").exists()
    assert (iteration_dir / "workspace").exists()
    for batch_name in ("batch_001", "batch_002", "batch_003"):
        batch_dir = iteration_dir / batch_name
        assert batch_dir.exists()
        assert (batch_dir / "rollout_before").exists()
        assert (batch_dir / "rollout_after").exists()
        assert (batch_dir / "batch_decision.json").exists()
        decision = read_json(batch_dir / "batch_decision.json")
        assert decision["accepted_workspace"].endswith("workspace_after")
        assert decision["memory_policy"] == "no_rollback"
        assert decision["rollout_after_uses_new_memory"] is False
        assert (batch_dir / "memory_lifecycle.json").exists()
        assert (batch_dir / "rollout_after" / "STATUS.json").exists()
        status = read_json(batch_dir / "rollout_after" / "STATUS.json")
        assert status["status"] == "completed"
    metrics = read_json(iteration_dir / "iteration_metrics.json")
    assert metrics["batch_count"] == 3
    assert metrics["selected_scenario_count"] == 7


def test_final_eval_finds_iteration_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    base_run = project_root / "runs" / "base_run"
    workspace = base_run / "iteration_001" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "system_prompt.md").write_text("evolved\n", encoding="utf-8")
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "system_prompt.md").write_text("seed\n", encoding="utf-8")
    (harness_seed / "code_agent.yaml").write_text("components: []\n", encoding="utf-8")

    copied = {}

    def fake_copy(project_root_arg, workspace_dir, source_workspace=None):
        copied["source"] = source_workspace
        target = Path(workspace_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr("reqahe.cli.copy_harness_seed", fake_copy)
    monkeypatch.setattr("reqahe.cli._create_run", lambda project_root_arg, config, prefix_agent: project_root / "runs" / "final")
    monkeypatch.setattr("reqahe.cli._write_latest", lambda *args, **kwargs: None)
    monkeypatch.setattr("reqahe.cli._selected_scenarios", lambda config, args: ([], "test", "full"))
    monkeypatch.setattr("reqahe.cli.run_tasks", lambda *args, **kwargs: {"metrics": {}, "task_results": []})
    monkeypatch.setattr("reqahe.cli._llm", lambda config: MagicMock(close=lambda: None))
    monkeypatch.setattr("reqahe.cli._write_compat_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("reqahe.cli._write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr("reqahe.cli.resolve_run_dir", lambda project_root_arg, best_run: base_run)

    config = load_config()
    config["paths"]["project_root"] = str(project_root)
    config["llm"] = {"api_key": "k", "model": "m", "base_url": "http://x"}
    args = SimpleNamespace(
        best_run="latest",
        split="test",
        task_mode="full",
        dataset_file=None,
        dataset_number=None,
        task_ids="",
        scenario_count=None,
        reflection_mode="warn",
        base_url=None,
        api_key=None,
        model=None,
        temperature=None,
        max_turns=4,
        rollouts_per_task=1,
        disable_close_wait_cleanup=False,
        close_wait_cleanup_interval_tasks=None,
        close_wait_cleanup_interval_seconds=None,
    )
    assert cmd_final_eval(args, config) == 0
    assert Path(copied["source"]) == workspace.resolve()


def test_report_reads_batch_metrics(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "run1"
    batch = run / "iteration_001" / "batch_001"
    write_json(
        batch / "rollout_before" / "metrics.json",
        _metrics(0.2),
    )
    write_json(
        batch / "rollout_after" / "metrics.json",
        _metrics(0.25),
    )
    write_json(run / "iteration_001" / "iteration_metrics.json", {"batch_count": 1, "accepted_batches": 1, "rolled_back_batches": 0})
    write_json(batch / "batch_decision.json", {"decision": "keep", "delta_main_score": 0.05, "reason": "ok"})

    report = generate_report(tmp_path, run)
    text = report.read_text(encoding="utf-8")
    assert "rollout_before" in text
    assert "rollout_after" in text
    assert "Batch Rollback Decisions" in text
    assert "iteration_001" in text


def _evolve_test_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenarios: list[Scenario]) -> tuple[dict, SimpleNamespace]:
    project_root = tmp_path / "project"
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "code_agent.yaml").write_text("components: []\n", encoding="utf-8")
    (harness_seed / "system_prompt.md").write_text("prompt\n", encoding="utf-8")

    monkeypatch.setattr("reqahe.cli._selected_scenarios", lambda config, args: (scenarios, "train", "full"))
    monkeypatch.setattr("reqahe.cli._llm", lambda config: MagicMock(close=lambda: None))
    monkeypatch.setattr("reqahe.cli._close_wait_cleaner", lambda config, run_dir: MagicMock(cleanup=lambda *args, **kwargs: None))
    monkeypatch.setattr("reqahe.cli._require_llm_config", lambda config: None)
    monkeypatch.setattr("reqahe.cli._write_compat_outputs", lambda *args, **kwargs: None)

    def fake_diagnosis(batch_dir, rollout_dir, llm, model, **kwargs):
        analysis = Path(batch_dir) / "analysis"
        analysis.mkdir(parents=True, exist_ok=True)
        write_json(
            analysis / "trace_problem_analysis.json",
            {
                "diagnosis_summary": "ok",
                "evidence_limitations": [],
                "failure_findings": [],
                "route_observations": [],
                "candidate_root_causes": [],
            },
        )
        write_json(
            analysis / "component_localization.json",
            {
                "localization_summary": "overview",
                "component_findings": [],
                "refiner_guidance": {},
            },
        )
        (analysis / "overview.md").write_text("overview\n", encoding="utf-8")
        return analysis

    monkeypatch.setattr("reqahe.cli.run_elicitation_diagnosis", fake_diagnosis)

    def fake_memorize(*args, **kwargs):
        return _fake_memorize(*args, **kwargs)

    monkeypatch.setattr("reqahe.cli.memorize_rollout", fake_memorize)

    config = load_config()
    config["paths"]["project_root"] = str(project_root)
    config["llm"] = {"api_key": "k", "model": "m", "base_url": "http://x"}
    config["evaluation"]["scenario_count"] = len(scenarios)
    config["evolution"]["iterations"] = 1
    config["evolution"]["batch_size"] = len(scenarios)

    args = SimpleNamespace(
        config=None,
        split="train",
        task_mode="full",
        dataset_file=None,
        dataset_number=None,
        task_ids="",
        scenario_count=len(scenarios),
        iterations=1,
        batch_size=len(scenarios),
        reflection_mode="warn",
        base_url=None,
        api_key=None,
        model=None,
        temperature=None,
        max_turns=4,
        rollouts_per_task=1,
        disable_close_wait_cleanup=False,
        close_wait_cleanup_interval_tasks=None,
        close_wait_cleanup_interval_seconds=None,
        resume_run_dir=None,
    )
    return config, args


def _fake_run_tasks_factory(before_score: float, after_score: float | None = None):
    after_score = after_score if after_score is not None else before_score + 0.05

    def fake_run_tasks(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs):
        rollout_path = Path(rollout_dir)
        rollout_path.mkdir(parents=True, exist_ok=True)
        score = after_score if "rollout_after" in str(rollout_path) else before_score
        task_results = []
        for scenario in batch_scenarios:
            task_dir = rollout_path / f"{scenario.scenario_id}__r0"
            task_dir.mkdir(parents=True, exist_ok=True)
            write_json(task_dir / "clean_trace.json", {"scenario_id": scenario.scenario_id, "turns": [], "final_metrics": {}})
            write_json(task_dir / "metrics.json", {"IRE": score, "TKQR": score})
            task_results.append(_task_result(scenario.scenario_id, score, score))
        metrics = _metrics(score)
        write_json(rollout_path / "metrics.json", metrics)
        write_json(rollout_path / "task_results.json", task_results)
        return {"metrics": metrics, "task_results": task_results}

    return fake_run_tasks


def _first_batch_dir(project_root: Path) -> Path:
    run_dirs = list((project_root / "runs").glob("ReqAHE-evolved_reahe-*"))
    assert run_dirs
    return run_dirs[0] / "iteration_001" / "batch_001"


def test_batch_refiner_failed_writes_status_and_workspace_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenarios = [Scenario("s0")]
    config, args = _evolve_test_setup(tmp_path, monkeypatch, scenarios)
    monkeypatch.setattr("reqahe.cli.run_tasks", _fake_run_tasks_factory(0.2))

    def failing_refine(*args, **kwargs):
        raise RuntimeError("refiner boom")

    monkeypatch.setattr("reqahe.cli.refine_harness", failing_refine)
    assert _run_evolve(args, config) == 0

    batch_dir = _first_batch_dir(Path(config["paths"]["project_root"]))
    decision = read_json(batch_dir / "batch_decision.json")
    status = read_json(batch_dir / "rollout_after" / "STATUS.json")

    assert (batch_dir / "workspace_after").exists()
    assert decision["decision"] == "rollback_refiner_failed"
    assert decision["accepted_workspace"].endswith("workspace_after")
    assert decision["harness_source"] == "workspace_before"
    assert status["status"] == "skipped"
    assert status["reason"] == "refiner_failed"


def test_batch_retest_failed_writes_status_and_workspace_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenarios = [Scenario("s0")]
    config, args = _evolve_test_setup(tmp_path, monkeypatch, scenarios)

    def fake_run_tasks(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs):
        rollout_path = Path(rollout_dir)
        if "rollout_after" in str(rollout_path):
            raise RuntimeError("retest boom")
        return _fake_run_tasks_factory(0.2)(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs)

    monkeypatch.setattr("reqahe.cli.run_tasks", fake_run_tasks)

    def fake_refine(batch_dir, workspace_dir, iteration, llm, model, **kwargs):
        refiner = Path(batch_dir) / "refiner"
        refiner.mkdir(parents=True, exist_ok=True)
        write_json(refiner / "fix_plan.json", {"fix_plan": []})
        write_json(refiner / "proposed_edits.json", {"changes": [], "file_edits": []})
        write_json(refiner / "validation_report.json", {"ok": True, "errors": [], "warnings": []})
        (Path(batch_dir) / "refiner.log").write_text("ok\n", encoding="utf-8")
        return Path(workspace_dir)

    monkeypatch.setattr("reqahe.cli.refine_harness", fake_refine)
    assert _run_evolve(args, config) == 0

    batch_dir = _first_batch_dir(Path(config["paths"]["project_root"]))
    decision = read_json(batch_dir / "batch_decision.json")
    status = read_json(batch_dir / "rollout_after" / "STATUS.json")

    assert decision["decision"] == "rollback_retest_failed"
    assert decision["accepted_workspace"].endswith("workspace_after")
    assert decision["harness_source"] == "workspace_before"
    assert status["status"] == "failed"
    assert status["reason"] == "retest_failed"


def test_batch_keep_records_workspace_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenarios = [Scenario("s0")]
    config, args = _evolve_test_setup(tmp_path, monkeypatch, scenarios)
    monkeypatch.setattr("reqahe.cli.run_tasks", _fake_run_tasks_factory(0.2, 0.3))

    def fake_refine(batch_dir, workspace_dir, iteration, llm, model, **kwargs):
        refiner = Path(batch_dir) / "refiner"
        refiner.mkdir(parents=True, exist_ok=True)
        write_json(refiner / "fix_plan.json", {"fix_plan": []})
        write_json(refiner / "proposed_edits.json", {"changes": [], "file_edits": []})
        write_json(refiner / "validation_report.json", {"ok": True, "errors": [], "warnings": []})
        (Path(batch_dir) / "refiner.log").write_text("ok\n", encoding="utf-8")
        return Path(workspace_dir)

    monkeypatch.setattr("reqahe.cli.refine_harness", fake_refine)
    assert _run_evolve(args, config) == 0

    batch_dir = _first_batch_dir(Path(config["paths"]["project_root"]))
    decision = read_json(batch_dir / "batch_decision.json")
    status = read_json(batch_dir / "rollout_after" / "STATUS.json")

    assert decision["decision"] == "keep"
    assert decision["accepted_workspace"].endswith("workspace_after")
    assert decision["harness_source"] == "workspace_candidate"
    assert status["status"] == "completed"


def test_batch_rollback_records_workspace_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenarios = [Scenario("s0")]
    config, args = _evolve_test_setup(tmp_path, monkeypatch, scenarios)
    monkeypatch.setattr("reqahe.cli.run_tasks", _fake_run_tasks_factory(0.4, 0.2))

    def fake_refine(batch_dir, workspace_dir, iteration, llm, model, **kwargs):
        refiner = Path(batch_dir) / "refiner"
        refiner.mkdir(parents=True, exist_ok=True)
        write_json(refiner / "fix_plan.json", {"fix_plan": []})
        write_json(refiner / "proposed_edits.json", {"changes": [], "file_edits": []})
        write_json(refiner / "validation_report.json", {"ok": True, "errors": [], "warnings": []})
        (Path(batch_dir) / "refiner.log").write_text("ok\n", encoding="utf-8")
        return Path(workspace_dir)

    monkeypatch.setattr("reqahe.cli.refine_harness", fake_refine)
    assert _run_evolve(args, config) == 0

    batch_dir = _first_batch_dir(Path(config["paths"]["project_root"]))
    decision = read_json(batch_dir / "batch_decision.json")

    assert decision["decision"] == "rollback"
    assert decision["accepted_workspace"].endswith("workspace_after")
    assert decision["harness_source"] == "workspace_before"
    assert decision["memory_policy"] == "no_rollback"
    assert decision["rollout_after_uses_new_memory"] is False


def test_rollout_after_uses_workspace_candidate_without_current_batch_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios = [Scenario("s0")]
    config, args = _evolve_test_setup(tmp_path, monkeypatch, scenarios)
    rollout_after_workspaces: list[Path] = []

    def fake_run_tasks(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs):
        rollout_path = Path(rollout_dir)
        if rollout_path.name == "rollout_after":
            rollout_after_workspaces.append(Path(workspace_dir).resolve())
            assert not (Path(workspace_dir) / "memory" / "new_type" / "MEMORY.md").exists()
        return _fake_run_tasks_factory(0.2, 0.3)(batch_scenarios, workspace_dir, rollout_dir, llm, **kwargs)

    def fake_refine(batch_dir, workspace_dir, iteration, llm, model, **kwargs):
        refiner = Path(batch_dir) / "refiner"
        refiner.mkdir(parents=True, exist_ok=True)
        write_json(refiner / "fix_plan.json", {"fix_plan": []})
        write_json(refiner / "proposed_edits.json", {"changes": [], "file_edits": []})
        write_json(refiner / "validation_report.json", {"ok": True, "errors": [], "warnings": []})
        (Path(batch_dir) / "refiner.log").write_text("ok\n", encoding="utf-8")
        return Path(workspace_dir)

    monkeypatch.setattr("reqahe.cli.run_tasks", fake_run_tasks)
    monkeypatch.setattr("reqahe.cli.refine_harness", fake_refine)
    monkeypatch.setattr("reqahe.cli.memorize_rollout", _fake_memorize)
    assert _run_evolve(args, config) == 0

    batch_dir = _first_batch_dir(Path(config["paths"]["project_root"]))
    assert rollout_after_workspaces
    assert rollout_after_workspaces[0] == (batch_dir / "workspace_candidate").resolve()
    assert (batch_dir / "workspace_memory" / "memory" / "new_type" / "MEMORY.md").exists()
    lifecycle = read_json(batch_dir / "memory_lifecycle.json")
    assert lifecycle["rollout_after_workspace"] == "workspace_candidate"
    assert lifecycle["rollout_after_uses_current_batch_memory"] is False
