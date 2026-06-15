from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


METADATA_KEYS = {"name", "version", "description"}
TEXT_SUFFIXES = {".md", ".txt", ".json"}
PYTHON_ENABLED_COMPONENTS = {"self_reflection"}


@dataclass(frozen=True)
class HarnessComponentSpec:
    name: str
    paths: tuple[str, ...]
    kind: str  # "file" | "directory"


def load_harness_component_specs(harness_dir: str | Path) -> dict[str, HarnessComponentSpec]:
    root = Path(harness_dir)
    manifest = _read_manifest(root / "code_agent.yaml")
    specs: dict[str, HarnessComponentSpec] = {}

    system_prompt = _clean_relative_path(manifest.get("system_prompt"))
    if system_prompt:
        specs["system_prompt"] = HarnessComponentSpec("system_prompt", (system_prompt,), "file")

    for key, value in manifest.items():
        if key in METADATA_KEYS or key == "system_prompt":
            continue
        if not isinstance(value, list):
            continue
        paths = tuple(path for item in value if (path := _clean_relative_path(item)))
        if not paths:
            continue
        kind = "directory" if any(Path(path).parent != Path(".") for path in paths) else "file"
        specs[str(key)] = HarnessComponentSpec(str(key), paths, kind)
    return specs


def allowed_component_names(harness_dir: str | Path) -> set[str]:
    return set(load_harness_component_specs(harness_dir))


def component_allowed_paths(harness_dir: str | Path) -> dict[str, tuple[str, ...]]:
    return {name: spec.paths for name, spec in load_harness_component_specs(harness_dir).items()}


def component_write_roots(harness_dir: str | Path) -> dict[str, tuple[str, ...]]:
    roots: dict[str, tuple[str, ...]] = {}
    for name, spec in load_harness_component_specs(harness_dir).items():
        if spec.kind == "file":
            roots[name] = spec.paths
            continue
        dirs = sorted({Path(path).parent.as_posix() for path in spec.paths if Path(path).parent != Path(".")})
        roots[name] = tuple(dirs or spec.paths)
    return roots


def allowed_suffixes_for_component(component_name: str) -> set[str]:
    suffixes = set(TEXT_SUFFIXES)
    if component_name in PYTHON_ENABLED_COMPONENTS:
        suffixes.add(".py")
    return suffixes


def path_component(harness_dir: str | Path, relative_path: str | Path) -> str | None:
    path = _relative_path(relative_path)
    if path is None:
        return None
    as_posix = path.as_posix()
    for name, spec in load_harness_component_specs(harness_dir).items():
        if spec.kind == "file" and as_posix in spec.paths:
            return name
        for root in component_write_roots(harness_dir).get(name, ()):
            root_path = Path(root)
            if root_path == Path("."):
                continue
            if path == root_path or root_path in path.parents:
                return name
    return None


def is_declared_harness_path(harness_dir: str | Path, relative_path: str | Path, *, for_write: bool = False) -> bool:
    path = _relative_path(relative_path)
    if path is None:
        return False
    as_posix = path.as_posix()
    if as_posix == "self_reflection/registry.yaml":
        return "self_reflection" in load_harness_component_specs(harness_dir)
    if as_posix in {"code_agent.yaml", "system_prompt.md"}:
        return True
    for name, spec in load_harness_component_specs(harness_dir).items():
        if spec.kind == "file":
            if as_posix == spec.paths[0]:
                return True
            continue
        roots = component_write_roots(harness_dir).get(name, ())
        suffixes = allowed_suffixes_for_component(name)
        if for_write and path.suffix.lower() not in suffixes:
            continue
        if not for_write and path.suffix.lower() not in suffixes:
            continue
        for root in roots:
            root_path = Path(root)
            if root_path != Path(".") and (path == root_path or root_path in path.parents):
                return True
    return False


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _clean_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = _relative_path(value)
    return path.as_posix() if path else ""


def _relative_path(value: str | Path) -> Path | None:
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    return Path(path.as_posix())
