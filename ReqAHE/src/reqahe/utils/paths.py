from __future__ import annotations

import re
from pathlib import Path
from typing import Any

WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_ABS_RE = re.compile(r"^\\\\")
POSIX_LOCAL_ABS_RE = re.compile(r"^/(home|Users|mnt|tmp)(/|$)")

PATH_KEYS = {
    "path",
    "dir",
    "file",
    "trace_dir",
    "workspace_dir",
    "workspace_path",
    "run_dir",
    "rollout_dir",
    "analysis_dir",
    "refiner_dir",
    "accepted_workspace",
    "workspace_before",
    "workspace_after",
    "workspace_candidate",
    "harness_source_path",
    "memory_source_path",
    "allowed_write_root",
    "source_path",
    "target_path",
    "harness_seed",
    "dataset_path",
    "reqelicit_path",
    "project_root",
    "data_path",
}


def looks_like_absolute_path(value: str | Path) -> bool:
    text = str(value).strip()
    if not text:
        return False
    if WINDOWS_ABS_RE.match(text) or UNC_ABS_RE.match(text):
        return True
    if POSIX_LOCAL_ABS_RE.match(text):
        return True
    if text.startswith("/"):
        return True
    return Path(text).expanduser().is_absolute()


def normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def resolve_project_path(path: str | Path, project_root: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if looks_like_absolute_path(candidate):
        return candidate.resolve()
    return (Path(project_root) / candidate).resolve()


def to_posix_relpath(path: str | Path, base: str | Path) -> str:
    path_obj = Path(str(path)).expanduser()
    base_obj = Path(base).expanduser()
    if not looks_like_absolute_path(path_obj) and not path_obj.is_absolute():
        return path_obj.as_posix()
    try:
        return path_obj.resolve().relative_to(base_obj.resolve()).as_posix()
    except ValueError:
        return path_obj.as_posix()


def resolve_maybe_relative(path: str | Path, base: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if looks_like_absolute_path(candidate) or candidate.is_absolute():
        return candidate.resolve()
    return (Path(base) / candidate).resolve()


def _should_relativize_key(key: str, path_keys: set[str]) -> bool:
    normalized = str(key)
    if normalized in path_keys:
        return True
    return any(normalized.endswith(suffix) for suffix in ("_path", "_dir", "_file"))


def relativize_json_paths(
    obj: Any,
    base: str | Path,
    path_keys: set[str] | None = None,
) -> Any:
    keys = path_keys or PATH_KEYS
    base_path = Path(base)

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(value, str) and _should_relativize_key(str(key), keys):
                if looks_like_absolute_path(value):
                    out[key] = to_posix_relpath(value, base_path)
                else:
                    out[key] = value
            else:
                out[key] = relativize_json_paths(value, base_path, keys)
        return out
    if isinstance(obj, list):
        return [relativize_json_paths(item, base_path, keys) for item in obj]
    return obj


def relativize_all_absolute_strings(obj: Any, base: str | Path) -> Any:
    base_path = Path(base)
    if isinstance(obj, dict):
        return {key: relativize_all_absolute_strings(value, base_path) for key, value in obj.items()}
    if isinstance(obj, list):
        return [relativize_all_absolute_strings(item, base_path) for item in obj]
    if isinstance(obj, str) and looks_like_absolute_path(obj):
        return to_posix_relpath(obj, base_path)
    return obj


def relativize_for_bases(obj: Any, bases: list[str | Path], path_keys: set[str] | None = None) -> Any:
    keys = path_keys or PATH_KEYS
    current = obj
    for base in bases:
        current = relativize_json_paths(current, base, keys)
        current = relativize_all_absolute_strings(current, base)
    return current
