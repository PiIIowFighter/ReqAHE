import importlib

import pytest

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_reqahe_top_level_packages_match_current_design() -> None:
    src = PROJECT_ROOT / "src" / "reqahe"
    expected_dirs = {
        "diagnoser",
        "evolution",
        "harness",
        "infra",
        "refiner",
        "reporting",
        "runtime",
        "utils",
    }
    actual_dirs = {path.name for path in src.iterdir() if path.is_dir() and not path.name.startswith("_")}
    assert actual_dirs == expected_dirs
    assert (src / "cli.py").is_file()
    assert (src / "config.py").is_file()


def test_removed_module_import_is_unavailable() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("reqahe.removed_module_a")


def test_removed_nested_pipeline_import_is_unavailable() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("reqahe.evolution.removed_pipeline_module_a")


def test_utils_paths_module_exists() -> None:
    assert (PROJECT_ROOT / "src" / "reqahe" / "utils" / "paths.py").is_file()


def test_diagnoser_and_refiner_packages_exist() -> None:
    src = PROJECT_ROOT / "src" / "reqahe"
    assert (src / "diagnoser").is_dir()
    assert (src / "refiner").is_dir()
