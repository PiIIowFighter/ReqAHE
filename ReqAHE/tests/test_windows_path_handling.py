import json
from pathlib import Path

from reqahe.reporting.inspection import inspect_sources
from reqahe.utils.paths import (
    looks_like_absolute_path,
    relativize_all_absolute_strings,
    to_posix_relpath,
)


def test_looks_like_absolute_path_detects_windows_paths() -> None:
    assert looks_like_absolute_path(r"D:\runs\iteration_001\rollout")
    assert looks_like_absolute_path("D:/runs/iteration_001/rollout")
    assert looks_like_absolute_path(r"\\server\share\runs\latest")
    assert not looks_like_absolute_path("runs/latest")
    assert not looks_like_absolute_path("train_001__r0")


def test_to_posix_relpath_keeps_already_relative_values() -> None:
    assert to_posix_relpath("rollout/train_001__r0", Path("iteration_001")) == "rollout/train_001__r0"


def test_relativize_all_absolute_strings_handles_windows_trace_dir(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    rollout = project_root / "runs" / "demo" / "iteration_001" / "rollout"
    rollout.mkdir(parents=True)
    stale = str(rollout / "train_001__r0")
    payload = {"task_results": [{"scenario_id": "train_001", "trace_dir": stale}]}
    normalized = relativize_all_absolute_strings(payload, project_root)
    assert normalized["task_results"][0]["trace_dir"] == "runs/demo/iteration_001/rollout/train_001__r0"


def test_inspect_sources_writes_relative_paths_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    reqelicit = project / "ReqElicitGym"
    (reqelicit / "env").mkdir(parents=True)
    (reqelicit / "data").mkdir(parents=True)
    (reqelicit / "env" / "reqelicit_gym.py").write_text("# stub\n", encoding="utf-8")
    (reqelicit / "env" / "prompts.py").write_text("# stub\n", encoding="utf-8")
    (reqelicit / "data" / "converted_scenarios_1.json").write_text("[]", encoding="utf-8")

    out = inspect_sources(project, "ReqElicitGym", dataset_file="converted_scenarios.json", dataset_number=1)
    assert out.exists()
    payload = json.loads((project / "notes" / "source_inspection.json").read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "typoagent" not in serialized
    assert "ahe_root" not in serialized
    assert "D:" not in serialized
    assert payload["configured_roots"]["reqelicitgym_root"] == "ReqElicitGym"
    assert payload["reqelicit_env"] == "ReqElicitGym/env/reqelicit_gym.py"
