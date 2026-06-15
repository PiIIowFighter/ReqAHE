from pathlib import Path

import pytest

from reqahe.cli import _batch_size, build_parser
from reqahe.evolution.attribution import write_attribution
from reqahe.evolution.batching import split_scenarios_into_batches
from reqahe.infra.io import read_json, write_json
from reqahe.reporting.report import generate_report


def _write_rollout(rollout: Path, task_id: str, ire: float, tkqr: float, main_score: float) -> None:
    write_json(
        rollout / "task_results.json",
        [
            {
                "scenario_id": task_id,
                "metrics": {"IRE": ire, "TKQR": tkqr},
                "trace_dir": str(rollout / f"{task_id}__r0"),
            }
        ],
    )
    write_json(
        rollout / "metrics.json",
        {
            "mean_IRE": ire,
            "mean_TKQR": tkqr,
            "main_score": main_score,
            "type_coverage": {"interaction": 0.0, "content": 1.0, "style": 0.0},
            "early_finish_rate": 0.0,
            "max_turns": 4,
        },
    )


def test_attribution_records_metric_deltas(tmp_path: Path) -> None:
    previous_rollout = tmp_path / "iteration_001" / "rollout"
    current_rollout = tmp_path / "iteration_002" / "rollout"
    _write_rollout(previous_rollout, "train_001", 0.1, 0.1, 0.1)
    _write_rollout(current_rollout, "train_001", 0.3, 0.2, 0.265)

    write_attribution(tmp_path / "iteration_002", previous_rollout, current_rollout)

    deltas = read_json(tmp_path / "iteration_002" / "attribution" / "metric_deltas.json")
    assert deltas["aggregate_delta"]["main_score"] > 0
    assert deltas["improved_tasks"] == ["train_001"]
    assert deltas["regressed_tasks"] == []
    assert "manifest_verdict" not in deltas
    assert (tmp_path / "iteration_002" / "attribution" / "task_movement.md").exists()


def test_report_omits_external_target_language(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "run1"
    rollout = run / "iteration_001" / "rollout"
    _write_rollout(rollout, "train_001", 0.2, 0.3, 0.235)
    write_json(
        run / "iteration_001" / "run_metadata.json",
        {"result_type": "internal evolution result", "split": "train", "task_mode": "test", "max_turns": 4},
    )

    report = generate_report(tmp_path, run)
    text = report.read_text(encoding="utf-8")

    forbidden = ["Onto" + "Agent", "0" + ".69", "0" + ".59", "paper " + "target", "exceeds " + "Onto" + "Agent"]
    for term in forbidden:
        assert term not in text
    assert "internal evolution result" in text


def test_cli_help_uses_reflection_mode(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["evolve", "--help"])
    help_text = capsys.readouterr().out

    assert "--reflection-mode" in help_text
    assert "--batch-size" in help_text


def test_evolve_batch_size_splits_without_wraparound() -> None:
    class Scenario:
        def __init__(self, scenario_id: str) -> None:
            self.scenario_id = scenario_id

    scenarios = [Scenario(f"train_{idx}") for idx in range(5)]

    assert _batch_size({"evolution": {"batch_size": 0}}) is None
    assert [item.scenario_id for item in split_scenarios_into_batches(scenarios, 2)[0]] == ["train_0", "train_1"]
    assert [item.scenario_id for item in split_scenarios_into_batches(scenarios, 2)[1]] == ["train_2", "train_3"]
    assert [item.scenario_id for item in split_scenarios_into_batches(scenarios, 2)[2]] == ["train_4"]
    assert [item.scenario_id for item in split_scenarios_into_batches(scenarios, None)[0]] == [
        "train_0",
        "train_1",
        "train_2",
        "train_3",
        "train_4",
    ]
