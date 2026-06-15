from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from reqahe.infra.io import read_json, write_json
from reqahe.infra.paths import safe_name


DEFAULT_DATASET_FILE = "converted_scenarios.json"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    app_type: str
    initial_req: str
    implicit_requirements: list[dict]
    final_requirements: list[str]
    raw: dict


def dataset_filename(dataset_file: str | None = None, dataset_number: int | str | None = None) -> str:
    name = dataset_file or DEFAULT_DATASET_FILE
    if dataset_number in (None, ""):
        return name

    number = str(dataset_number).strip()
    path = Path(name)
    suffix = path.suffix or ".json"
    if "{number}" in name:
        return name.format(number=number)
    if path.stem.endswith(f"_{number}"):
        return path.name
    return f"{path.stem}_{number}{suffix}"


def resolve_dataset_path(
    reqelicitgym_root: str | Path,
    dataset_file: str | None = None,
    dataset_number: int | str | None = None,
) -> Path:
    source = Path(dataset_filename(dataset_file, dataset_number))
    if not source.is_absolute():
        source = Path(reqelicitgym_root) / "data" / source
    return source


def default_dataset_path(reqelicitgym_root: str | Path) -> Path:
    return resolve_dataset_path(reqelicitgym_root)


def load_scenarios(reqelicitgym_root: str | Path, data_path: str | Path | None = None) -> list[Scenario]:
    source = Path(data_path) if data_path else default_dataset_path(reqelicitgym_root)
    data = read_json(source)
    scenarios: list[Scenario] = []
    for idx, item in enumerate(data):
        scenario_id = str(item.get("id") or item.get("task_id") or f"task_{idx}")
        implicit = item.get("Implicit Requirements") or item.get("implicit_requirements") or []
        final_reqs = item.get("URL") or item.get("user_stories") or []
        scenarios.append(
            Scenario(
                scenario_id=scenario_id,
                name=str(item.get("name") or scenario_id),
                app_type=str(item.get("application_type") or "unknown"),
                initial_req=str(item.get("initial_requirements") or item.get("initial_requirement") or ""),
                implicit_requirements=list(implicit),
                final_requirements=list(final_reqs) if isinstance(final_reqs, list) else [str(final_reqs)],
                raw=item,
            )
        )
    return scenarios


def _split_bucket(ids: list[str], rng: random.Random) -> tuple[list[str], list[str], list[str]]:
    shuffled = list(ids)
    rng.shuffle(shuffled)
    n = len(shuffled)
    train_n = max(1, int(round(n * 0.6))) if n else 0
    val_n = max(0, int(round(n * 0.2)))
    if train_n + val_n > n:
        val_n = max(0, n - train_n)
    return shuffled[:train_n], shuffled[train_n : train_n + val_n], shuffled[train_n + val_n :]


def build_splits(scenarios: Iterable[Scenario], seed: int = 42) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = defaultdict(list)
    for scenario in scenarios:
        by_type[scenario.app_type].append(scenario.scenario_id)
    rng = random.Random(seed)
    splits = {"train": [], "val": [], "test": []}
    for ids in by_type.values():
        train_ids, val_ids, test_ids = _split_bucket(ids, rng)
        splits["train"].extend(train_ids)
        splits["val"].extend(val_ids)
        splits["test"].extend(test_ids)
    for key in splits:
        splits[key].sort()
    splits["all"] = sorted(s.scenario_id for s in scenarios)
    return splits


def load_or_create_splits(
    project_root: str | Path,
    scenarios: list[Scenario],
    seed: int = 42,
    dataset_path: str | Path | None = None,
) -> dict[str, list[str]]:
    path = _splits_path(project_root, dataset_path)
    if path.exists():
        splits = read_json(path)
        if _splits_match_dataset(splits, scenarios):
            return splits
    splits = build_splits(scenarios, seed=seed)
    write_json(path, splits)
    return splits


def _splits_path(project_root: str | Path, dataset_path: str | Path | None) -> Path:
    if dataset_path is None:
        return Path(project_root) / "splits.json"
    stem = safe_name(Path(dataset_path).stem, default="dataset")
    return Path(project_root) / f"splits_{stem}.json"


def _splits_match_dataset(splits: dict[str, list[str]], scenarios: list[Scenario]) -> bool:
    scenario_ids = {scenario.scenario_id for scenario in scenarios}
    split_ids = {sid for ids in splits.values() if isinstance(ids, list) for sid in ids}
    return bool(split_ids) and split_ids.issubset(scenario_ids)


def select_scenarios(
    scenarios: list[Scenario],
    splits: dict[str, list[str]],
    split: str = "train",
    task_mode: str = "sample",
    task_ids: list[str] | None = None,
) -> list[Scenario]:
    by_id = {s.scenario_id: s for s in scenarios}
    if task_ids:
        selected_ids = [tid for tid in task_ids if tid in by_id]
    else:
        selected_ids = list(splits.get(split, splits.get("train", [])))
    selected = [by_id[sid] for sid in selected_ids if sid in by_id]
    if task_mode == "test":
        return selected[:1]
    if task_mode == "top3":
        return selected[:3]
    if task_mode == "sample":
        return selected[:10]
    if task_mode == "full":
        return selected
    raise ValueError(f"Unknown task_mode: {task_mode}")
