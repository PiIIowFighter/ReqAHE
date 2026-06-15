from pathlib import Path

from reqahe.diagnoser.pipeline import (
    _resolve_trace_dir,
    build_full_trace_problem_payload,
    load_complete_clean_traces,
)
from reqahe.infra.io import write_json


def test_resolve_trace_dir_falls_back_when_stored_windows_absolute_path_missing(tmp_path: Path) -> None:
    """Simulate task_results with a stale Windows absolute trace_dir."""
    rollout = tmp_path / "rollout"
    actual_trace_dir = rollout / "train_000195__r0"
    actual_trace_dir.mkdir(parents=True)
    write_json(
        actual_trace_dir / "clean_trace.json",
        {
            "scenario_id": "train_000195",
            "turns": [],
            "final_metrics": {"IRE": 0.2},
            "elicited_requirement_ids": [],
            "missed_requirement_ids": [],
        },
    )
    stale_trace_dir = r"X:\old\moved\ReqAHE\runs\stale_relocated\train_000195__r0"
    result = {
        "scenario_id": "train_000195",
        "trace_dir": stale_trace_dir,
        "metrics": {"IRE": 0.2},
    }

    resolved = _resolve_trace_dir(rollout, result)
    assert resolved == actual_trace_dir


def test_load_complete_clean_traces_uses_scenario_id_fallback(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    trace_dir = rollout / "train_000195__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "scenario_id": "train_000195",
            "turns": [{"turn_index": 0, "action": "ask_question", "question": "Q?", "user_response": "A", "judgement": {}}],
            "final_metrics": {"IRE": 0.2},
            "elicited_requirement_ids": [],
            "missed_requirement_ids": ["IR1"],
        },
    )
    write_json(
        rollout / "task_results.json",
        [
            {
                "scenario_id": "train_000195",
                "trace_dir": r"X:\old\moved\ReqAHE\runs\stale_relocated\train_000195__r0",
                "metrics": {"IRE": 0.2},
            }
        ],
    )

    traces, warnings = load_complete_clean_traces(rollout)
    assert len(traces) == 1
    assert traces[0]["scenario_id"] == "train_000195"
    assert not warnings


def test_build_full_trace_problem_payload_includes_complete_task_traces(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    trace_dir = rollout / "train_000195__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "scenario_id": "train_000195",
            "turns": [],
            "final_metrics": {"IRE": 0.1},
            "elicited_requirement_ids": [],
            "missed_requirement_ids": [],
        },
    )
    write_json(
        rollout / "task_results.json",
        [
            {
                "scenario_id": "train_000195",
                "trace_dir": r"X:\old\moved\ReqAHE\runs\stale_relocated\train_000195__r0",
                "metrics": {"IRE": 0.1},
            }
        ],
    )
    write_json(rollout / "metrics.json", {"mean_IRE": 0.1})

    payload = build_full_trace_problem_payload(rollout, {"mean_IRE": 0.1}, {})
    assert payload["complete_task_traces"]
    assert payload["complete_task_traces"][0]["scenario_id"] == "train_000195"
