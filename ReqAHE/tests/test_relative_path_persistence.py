import json
import re
from pathlib import Path

from reqahe.evolution.loop import write_batch_decision
from reqahe.infra.io import read_json
from reqahe.utils.paths import resolve_maybe_relative

WINDOWS_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")
POSIX_LOCAL_ABS_RE = re.compile(r'(?<!["\w])/(home|Users|mnt|tmp)/')


def _assert_no_machine_absolute_paths(payload: str) -> None:
    assert not WINDOWS_ABS_RE.search(payload), payload
    assert not POSIX_LOCAL_ABS_RE.search(payload), payload


def test_batch_decision_uses_relative_workspace_paths(tmp_path: Path) -> None:
    batch_dir = tmp_path / "iteration_001" / "batch_001"
    batch_dir.mkdir(parents=True)
    workspace_after = batch_dir / "workspace_after"
    workspace_source = batch_dir / "workspace_candidate"
    workspace_after.mkdir()
    workspace_source.mkdir()

    write_batch_decision(
        batch_dir,
        before_metrics={"mean_IRE": 0.1},
        after_metrics={"mean_IRE": 0.2},
        decision={"decision": "keep", "delta_main_score": 0.01, "reason": "improved"},
        accepted_workspace=str(workspace_after),
        finalize_info={
            "harness_decision": "keep",
            "harness_source": "workspace_candidate",
            "harness_source_path": workspace_source,
            "memory_policy": "no_rollback",
            "memory_apply_timing": "next_batch",
            "memory_source": "workspace_memory",
            "memory_source_path": batch_dir / "workspace_memory",
            "memory_merged": True,
            "rollout_after_uses_new_memory": False,
        },
    )

    payload = read_json(batch_dir / "batch_decision.json")
    serialized = json.dumps(payload, ensure_ascii=False)
    _assert_no_machine_absolute_paths(serialized)
    assert payload["accepted_workspace"] == "workspace_after"
    assert payload["harness_source_path"] == "workspace_candidate"
    assert payload["memory_policy"] == "no_rollback"


def test_resolve_maybe_relative_reads_absolute_trace_dir(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    trace_dir = rollout / "train_001__r0"
    trace_dir.mkdir(parents=True)
    absolute_path = str(trace_dir.resolve())
    resolved = resolve_maybe_relative(absolute_path, rollout)
    assert resolved == trace_dir
