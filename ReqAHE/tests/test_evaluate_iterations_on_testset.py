from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "evaluate_iterations_on_testset.py"


def _load_script_module():
    if str(PROJECT_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
    spec = importlib.util.spec_from_file_location("evaluate_iterations_on_testset", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_script = _load_script_module()


def test_run_dir_is_required() -> None:
    with pytest.raises(SystemExit):
        eval_script.parse_args([])


def test_default_test_data_is_relative_project_path() -> None:
    args = eval_script.parse_args(["--run-dir", "runs/example"])
    assert args.test_data == "ReqElicitGym/data/test.json"


def test_find_iteration_dirs_matches_numbered_iterations_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "iteration_002").mkdir(parents=True)
    (run_dir / "iteration_001").mkdir(parents=True)
    (run_dir / "iteration_extra").mkdir(parents=True)
    (run_dir / "batch_001").mkdir(parents=True)

    found = eval_script.find_iteration_dirs(run_dir)
    assert [path.name for path in found] == ["iteration_001", "iteration_002"]


def test_build_output_label_uses_test_full_and_safe_model_name() -> None:
    label = eval_script.build_output_label("glm-4.7", "iteration_001")
    assert label == "reqahe_glm-4.7_test_full_iteration_001"


def test_script_help_runs_without_pythonpath() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "ModuleNotFoundError" not in output
    assert "--run-dir" in output


def test_dry_run_does_not_call_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run_dir = tmp_path / "runs" / "ReqAHE-evolved_reahe-glm-4.7-test"
    iteration_dir = run_dir / "iteration_001"
    workspace = iteration_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "code_agent.yaml").write_text("agent: evolved_reahe\n", encoding="utf-8")

    test_data = tmp_path / "ReqElicitGym" / "data" / "test.json"
    test_data.parent.mkdir(parents=True)
    test_data.write_text("[]", encoding="utf-8")

    called = {"run_tasks": False}

    def _fail_run_tasks(*args, **kwargs):
        called["run_tasks"] = True
        raise AssertionError("run_tasks should not be called in dry-run mode")

    monkeypatch.setattr(eval_script, "run_tasks", _fail_run_tasks)
    monkeypatch.setattr(
        eval_script,
        "load_config",
        lambda *_args, **_kwargs: {
            "paths": {"project_root": str(tmp_path), "reqelicitgym_root": "ReqElicitGym"},
            "evaluation": {"max_turns": 4, "rollouts_per_task": 1},
            "llm": {},
        },
    )

    exit_code = eval_script.main(
        [
            "--run-dir",
            str(run_dir),
            "--test-data",
            "ReqElicitGym/data/test.json",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert called["run_tasks"] is False
    assert "[dry-run]" in output
    assert "iteration_001" in output


def _fake_run_tasks_factory(write_json):
    def _fake_run_tasks(_scenarios, _workspace, output_dir, *_args, **_kwargs):
        trace_dir = Path(output_dir) / "test_001__r0"
        trace_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            trace_dir / "clean_trace.json",
            {
                "scenario_id": "test_001",
                "evaluation_mode": "reqelicitgym_judge_user",
                "final_metrics": {"total_implicit_requirements": 1, "elicitation_ratio": 1.0},
                "turns": [
                    {
                        "turn_index": 0,
                        "action": "ask_question",
                        "question": "q",
                        "user_response": "a",
                        "judgement": {
                            "action_type": "probe",
                            "is_relevant_to_implied_requirements": True,
                            "elicited_requirement_ids": ["r1"],
                        },
                    }
                ],
            },
        )
        return {
            "metrics": {
                "mean_IRE": 1.0,
                "mean_TKQR": 0.5,
                "main_score": 0.75,
                "probe_effectiveness": 0.5,
                "type_coverage": {},
                "evaluation_mode": "reqelicitgym_judge_user",
            },
            "task_results": [
                {
                    "scenario_id": "test_001",
                    "metrics": {"total_implicit_requirements": 1, "hit_count": 1},
                    "trace_dir": "test_001__r0",
                }
            ],
        }

    return _fake_run_tasks


def _evaluate_iteration_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_rollout: bool):
    iteration_dir = tmp_path / "iteration_001"
    workspace = iteration_dir / "workspace"
    workspace.mkdir(parents=True)

    class Scenario:
        scenario_id = "test_001"
        name = "test_001"
        app_type = "demo"
        initial_req = "need app"
        final_requirements = ["story"]

    scenarios = [Scenario()]
    config = {
        "paths": {"project_root": str(tmp_path), "reqelicitgym_root": "ReqElicitGym"},
        "evaluation": {"max_turns": 4, "rollouts_per_task": 1, "user_answer_quality": "high"},
        "llm": {"model": "glm-4.7"},
    }
    from reqahe.infra.io import write_json

    monkeypatch.setattr(eval_script, "run_tasks", _fake_run_tasks_factory(write_json))
    monkeypatch.setattr(eval_script, "llm_client", lambda _config: object())
    monkeypatch.setattr(eval_script, "_close_wait_cleaner", lambda *_args, **_kwargs: None)

    status = eval_script.evaluate_one_iteration(
        iteration_dir=iteration_dir,
        workspace=workspace,
        scenarios=scenarios,
        config=config,
        dataset_relpath="ReqElicitGym/data/test.json",
        keep_rollout=keep_rollout,
        overwrite=False,
        dry_run=False,
        run_dir=tmp_path / "run",
    )
    return iteration_dir, status


def test_evaluate_one_iteration_writes_outputs_without_rollout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    iteration_dir, status = _evaluate_iteration_fixture(tmp_path, monkeypatch, keep_rollout=False)

    label = "reqahe_glm-4.7_test_full_iteration_001"
    conversation_path = iteration_dir / "test_outputs" / "conversation" / f"{label}.json"
    metrics_path = iteration_dir / "test_outputs" / "metrics" / f"{label}.json"

    assert status == "evaluated"
    assert conversation_path.exists()
    assert metrics_path.exists()
    assert not (iteration_dir / "test_outputs" / "_rollout_tmp").exists()

    from reqahe.infra.io import read_json

    metrics = read_json(metrics_path)
    assert metrics["result_type"] == "independent_test_result"
    assert metrics["config"]["split"] == "independent_test"
    assert metrics["config"]["task_mode"] == "full"
    assert metrics["task_results"][0]["trace_dir"] == ""

    conversations = read_json(conversation_path)
    assert conversations[0]["trace_dir"] == ""


def test_evaluate_one_iteration_keeps_rollout_trace_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    iteration_dir, status = _evaluate_iteration_fixture(tmp_path, monkeypatch, keep_rollout=True)

    label = "reqahe_glm-4.7_test_full_iteration_001"
    metrics_path = iteration_dir / "test_outputs" / "metrics" / f"{label}.json"
    rollout_dir = iteration_dir / "test_outputs" / "rollout"

    assert status == "evaluated"
    assert rollout_dir.exists()

    from reqahe.infra.io import read_json

    metrics = read_json(metrics_path)
    assert metrics["task_results"][0]["trace_dir"] == "test_001__r0"

    conversations = read_json(iteration_dir / "test_outputs" / "conversation" / f"{label}.json")
    assert conversations[0]["trace_dir"] == "test_001__r0"
