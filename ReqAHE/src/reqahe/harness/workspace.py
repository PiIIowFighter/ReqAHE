from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from reqahe.harness.component_schema import load_reflection_registry, parse_markdown_frontmatter, validate_skill_minimal_frontmatter
from reqahe.harness.component_spec import (
    allowed_suffixes_for_component,
    component_write_roots,
    is_declared_harness_path,
    load_harness_component_specs,
    path_component,
)
from reqahe.infra.io import ensure_dir, read_text, write_json


ALLOWED_WORKSPACE_FILES = {"code_agent.yaml", "system_prompt.md"}
SKILL_SCHEMA_ERRORS_FILE = "skill_schema_errors.json"


def copy_harness_seed(project_root: str | Path, workspace_dir: str | Path, source_workspace: str | Path | None = None) -> Path:
    source = Path(source_workspace) if source_workspace else Path(project_root) / "harness_seed"
    target = Path(workspace_dir)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def merge_memory_workspace(source_memory_workspace: str | Path, target_workspace: str | Path) -> None:
    """Merge memory/ from source into target without deleting unrelated memory types."""
    source = Path(source_memory_workspace)
    target = Path(target_workspace)
    source_memory = source / "memory"
    if not source_memory.is_dir():
        return
    target_memory = ensure_dir(target / "memory")
    for source_type_dir in sorted(source_memory.iterdir()):
        if not source_type_dir.is_dir():
            continue
        source_memory_file = source_type_dir / "MEMORY.md"
        if not source_memory_file.is_file():
            continue
        target_type_dir = ensure_dir(target_memory / source_type_dir.name)
        target_memory_file = target_type_dir / "MEMORY.md"
        if target_memory_file.exists():
            merged = _merge_memory_markdown(read_text(target_memory_file), read_text(source_memory_file))
            target_memory_file.write_text(merged, encoding="utf-8")
        else:
            shutil.copy2(source_memory_file, target_memory_file)
        for extra_file in source_type_dir.iterdir():
            if extra_file.name == "MEMORY.md" or not extra_file.is_file():
                continue
            shutil.copy2(extra_file, target_type_dir / extra_file.name)


def _merge_memory_markdown(existing: str, incoming: str) -> str:
    """Prefer incoming points while preserving unique historical points from existing."""
    from reqahe.evolution.memorizer import _parse_recorded_hit_points, _render_memory_md

    existing_points = _parse_recorded_hit_points(existing)
    incoming_points = _parse_recorded_hit_points(incoming)
    if not incoming_points:
        return existing
    display_name = incoming.splitlines()[0].lstrip("# ").strip() if incoming.strip() else "Memory"
    if not display_name:
        display_name = existing.splitlines()[0].lstrip("# ").strip() if existing.strip() else "Memory"
    merged_points: list[str] = []
    seen: set[str] = set()
    for point in incoming_points + existing_points:
        normalized = point.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged_points.append(point)
    return _render_memory_md(display_name, merged_points)


def load_harness_text(workspace_dir: str | Path) -> dict[str, str]:
    root = Path(workspace_dir)
    sections: dict[str, str] = {}
    specs = load_harness_component_specs(root)
    scan = scan_skill_artifacts(root)
    for name, spec in specs.items():
        if spec.kind == "file":
            sections[name] = "\n".join(read_text(root / path) for path in spec.paths).strip()
            continue
        if name == "skills":
            sections[name] = render_skill_catalog(scan["router_catalog"])
            continue
        if name == "memory":
            from reqahe.runtime.memory_router import list_memory_types

            types = list_memory_types(root)
            if types:
                sections[name] = "Available scenario memory types:\n" + "\n".join(f"- {item}" for item in types)
            else:
                sections[name] = "(no memory types recorded yet)"
            continue
        if name == "self_reflection":
            sections[name] = _render_reflection_runtime_summary(root)
            continue
        parts = []
        for folder in component_write_roots(root).get(name, ()):
            for path in sorted((root / folder).rglob("*")):
                if not path.is_file() or path.suffix.lower() not in allowed_suffixes_for_component(name):
                    continue
                if path.suffix.lower() == ".py":
                    continue
                parts.append(f"\n## {path.name}\n{read_text(path)}")
        sections[name] = "\n".join(parts)
    return sections


def scan_skill_artifacts(root: Path | str) -> dict[str, Any]:
    root_path = Path(root)
    skills_dir = root_path / "skills"
    router_catalog: list[dict[str, Any]] = []
    disabled_skills: list[dict[str, Any]] = []
    schema_errors: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        _write_skill_schema_errors(root_path, schema_errors)
        return {
            "router_catalog": router_catalog,
            "disabled_skills": disabled_skills,
            "schema_errors": schema_errors,
        }
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        rel_path = path.relative_to(root_path).as_posix()
        if ".." in rel_path.split("/"):
            continue
        try:
            content = read_text(path)
        except OSError as exc:
            schema_errors.append({"path": rel_path, "errors": [f"failed to read skill file: {exc}"]})
            continue
        errors = validate_skill_minimal_frontmatter(rel_path, content)
        if errors:
            schema_errors.append({"path": rel_path, "errors": errors})
            continue
        try:
            metadata, _body = parse_markdown_frontmatter(content, rel_path)
        except RuntimeError as exc:
            schema_errors.append({"path": rel_path, "errors": [str(exc)]})
            continue
        entry = _skill_catalog_entry(rel_path, metadata)
        if metadata.get("enabled") is True:
            router_catalog.append(entry)
        else:
            disabled_skills.append(entry)
    _write_skill_schema_errors(root_path, schema_errors)
    router_catalog.sort(key=lambda item: (item["skill_id"],))
    disabled_skills.sort(key=lambda item: (item["skill_id"],))
    return {
        "router_catalog": router_catalog,
        "disabled_skills": disabled_skills,
        "schema_errors": schema_errors,
    }


def load_skill_catalog(root: Path | str) -> list[dict[str, Any]]:
    return list(scan_skill_artifacts(root)["router_catalog"])


def load_skill_catalog_summary(root: Path | str) -> str:
    scan = scan_skill_artifacts(root)
    parts = ["# Skill Catalog Summary", ""]
    parts.append("## Enabled Skills")
    if scan["router_catalog"]:
        parts.append(render_skill_catalog(scan["router_catalog"]))
    else:
        parts.append("(none)")
    parts.extend(["", "## Disabled Skills"])
    if scan["disabled_skills"]:
        parts.append(render_skill_catalog(scan["disabled_skills"], include_enabled=False))
    else:
        parts.append("(none)")
    if scan["schema_errors"]:
        parts.extend(["", "## Schema Errors"])
        for item in scan["schema_errors"]:
            path = item.get("path", "")
            errors = item.get("errors") or []
            parts.append(f"- `{path}`: {'; '.join(str(err) for err in errors)}")
    return "\n\n".join(parts).strip()


def load_skill_schema_errors_summary(root: Path | str) -> str:
    scan = scan_skill_artifacts(root)
    if not scan["schema_errors"]:
        return ""
    lines = ["# Skill Schema Errors", ""]
    for item in scan["schema_errors"]:
        path = item.get("path", "")
        errors = item.get("errors") or []
        lines.append(f"- `{path}`:")
        for err in errors:
            lines.append(f"  - {err}")
    return "\n".join(lines).strip()


def render_skill_catalog(catalog: list[dict[str, Any]], *, include_enabled: bool = True) -> str:
    if not catalog:
        return ""
    parts: list[str] = []
    for item in catalog:
        scope_lines = "\n".join(f"  * {line}" for line in item.get("scope") or []) or "  * (not specified)"
        use_when_lines = "\n".join(f"  * {line}" for line in item.get("use_when") or []) or "  * (not specified)"
        avoid_when_lines = "\n".join(f"  * {line}" for line in item.get("avoid_when") or []) or "  * (not specified)"
        risk_lines = "\n".join(f"  * {line}" for line in item.get("risk_notes") or []) or "  * (not specified)"
        block = [
            f"## {item['skill_id']}",
            f"* Name: {item.get('name', item['skill_id'])}",
            f"* Intent: {item.get('intent') or '(not specified)'}",
            "* Scope:",
            scope_lines,
            "* Use when:",
            use_when_lines,
            "* Avoid when:",
            avoid_when_lines,
            "* Risk notes:",
            risk_lines,
            f"* Path: {item.get('relative_path', '')}",
        ]
        if include_enabled:
            block.insert(3, f"* Enabled: {item.get('enabled', True)}")
        parts.append("\n".join(block))
    return "\n\n".join(parts).strip()


def load_selected_skill_text(root: Path | str, selected_skill_ids: list[str]) -> str:
    if not selected_skill_ids:
        return ""
    root_path = Path(root)
    catalog = {item["skill_id"]: item for item in load_skill_catalog(root_path)}
    ordered_ids = sorted(skill_id for skill_id in selected_skill_ids if skill_id in catalog)
    parts: list[str] = []
    for skill_id in ordered_ids:
        rel_path = catalog[skill_id]["relative_path"]
        if ".." in rel_path.split("/"):
            continue
        path = root_path / rel_path
        if not path.is_file():
            continue
        try:
            content = read_text(path).strip()
        except OSError:
            continue
        if content:
            parts.append(f"## {skill_id}\n\n{content}")
    return "\n\n".join(parts).strip()


def _skill_catalog_entry(rel_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    skill_id = str(metadata.get("id") or Path(rel_path).parent.name)
    return {
        "skill_id": skill_id,
        "id": skill_id,
        "name": str(metadata.get("name") or skill_id),
        "intent": str(metadata.get("intent") or ""),
        "scope": [str(item) for item in metadata.get("scope") or [] if str(item).strip()],
        "use_when": [str(item) for item in metadata.get("use_when") or [] if str(item).strip()],
        "avoid_when": [str(item) for item in metadata.get("avoid_when") or [] if str(item).strip()],
        "risk_notes": [str(item) for item in metadata.get("risk_notes") or [] if str(item).strip()],
        "enabled": bool(metadata.get("enabled")),
        "version": metadata.get("version"),
        "relative_path": rel_path,
    }


def _write_skill_schema_errors(root: Path, schema_errors: list[dict[str, Any]]) -> None:
    try:
        write_json(root / SKILL_SCHEMA_ERRORS_FILE, {"errors": schema_errors})
    except OSError:
        return


def _render_reflection_runtime_summary(root: Path) -> str:
    try:
        registry = load_reflection_registry(root)
    except RuntimeError:
        return "(no runtime checks registered)"
    checks = registry.get("checks") or []
    if not checks:
        return "(no runtime checks registered)"
    lines = ["Registered Python runtime checks:"]
    for item in checks:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("id") or "")
        hook = str(item.get("hook") or "")
        mode = str(item.get("mode") or "")
        lines.append(f"- {check_id} (hook={hook}, mode={mode})")
    return "\n".join(lines)


def assert_within_workspace(path: str | Path, workspace_dir: str | Path) -> None:
    candidate = Path(path).resolve()
    root = Path(workspace_dir).resolve()
    if root not in candidate.parents and candidate != root:
        raise PermissionError(f"Harness refiner attempted to write outside workspace: {candidate}")


def workspace_allowed_files(workspace_dir: str | Path) -> set[Path]:
    root = Path(workspace_dir)
    allowed: set[Path] = set()
    for file_name in ALLOWED_WORKSPACE_FILES:
        allowed.add((root / file_name).resolve())
    for name, roots in component_write_roots(root).items():
        suffixes = allowed_suffixes_for_component(name)
        for folder in roots:
            folder_path = root / folder
            if folder_path.is_file():
                allowed.add(folder_path.resolve())
                continue
            if folder_path.exists():
                allowed.update(p.resolve() for p in folder_path.rglob("*") if p.is_file() and p.suffix.lower() in suffixes)
    return allowed


def write_workspace_file(workspace_dir: str | Path, relative_path: str, content: str) -> Path:
    root = Path(workspace_dir)
    target = root / relative_path
    assert_within_workspace(target, root)
    if not is_workspace_write_allowed(root, relative_path):
        raise PermissionError(f"File is not in harness refiner allowlist: {relative_path}")
    ensure_dir(target.parent)
    target.write_text(content, encoding="utf-8")
    return target


def is_workspace_write_allowed(workspace_dir: str | Path, relative_path: str | Path) -> bool:
    path = Path(relative_path)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return False
    as_posix = path.as_posix()
    if as_posix == "self_reflection/registry.yaml":
        specs = load_harness_component_specs(workspace_dir)
        return "self_reflection" in specs
    if as_posix == "code_agent.yaml":
        return True
    component = path_component(workspace_dir, path)
    if component is None:
        return False
    return is_declared_harness_path(workspace_dir, path, for_write=True)
