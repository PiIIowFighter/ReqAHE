from pathlib import Path
from types import SimpleNamespace

from reqahe.config import apply_cli_overrides
from reqahe.infra.io import read_json, write_json
from reqahe.runtime.dataset import Scenario, dataset_filename, load_or_create_splits, resolve_dataset_path


def test_dataset_filename_supports_numbered_variants() -> None:
    assert dataset_filename("converted_scenarios.json", 1) == "converted_scenarios_1.json"
    assert dataset_filename("converted_scenarios_{number}.json", 2) == "converted_scenarios_2.json"
    assert dataset_filename("custom.json", 12) == "custom_12.json"
    assert dataset_filename("converted_scenarios.json", None) == "converted_scenarios.json"


def test_resolve_dataset_path_uses_reqelicitgym_data_dir(tmp_path: Path) -> None:
    root = tmp_path / "ReqElicitGym"
    assert resolve_dataset_path(root, "converted_scenarios.json", 4) == root / "data" / "converted_scenarios_4.json"


def test_dataset_number_base_override_disables_suffix() -> None:
    config = {"evaluation": {"dataset_file": "converted_scenarios.json", "dataset_number": 1}}
    args = SimpleNamespace(
        base_url=None,
        api_key=None,
        model=None,
        temperature=None,
        max_turns=None,
        rollouts_per_task=None,
        task_mode=None,
        dataset_file=None,
        dataset_number="base",
        split=None,
        iterations=None,
        reflection_mode=None,
    )

    updated = apply_cli_overrides(config, args)

    assert updated["evaluation"]["dataset_number"] is None


def test_splits_are_dataset_specific_and_stale_safe(tmp_path: Path) -> None:
    scenarios = [
        Scenario(
            scenario_id="train_001",
            name="train_001",
            app_type="type_a",
            initial_req="",
            implicit_requirements=[],
            final_requirements=[],
            raw={},
        )
    ]
    dataset_path = tmp_path / "ReqElicitGym" / "data" / "converted_scenarios_1.json"
    split_path = tmp_path / "splits_converted_scenarios_1.json"
    write_json(split_path, {"train": ["task_1"], "val": [], "test": [], "all": ["task_1"]})

    splits = load_or_create_splits(tmp_path, scenarios, dataset_path=dataset_path)

    assert splits["all"] == ["train_001"]
    assert read_json(split_path)["all"] == ["train_001"]
