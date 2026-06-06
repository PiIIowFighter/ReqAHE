from __future__ import annotations

import shutil
from pathlib import Path

from reqahe.utils.io import ensure_dir, read_text


ALLOWED_WORKSPACE_DIRS = {"tools", "middleware", "skills", "memory", "subagents"}
ALLOWED_WORKSPACE_FILES = {"code_agent.yaml", "system_prompt.md", "change_manifest.json"}


def copy_harness_seed(project_root: str | Path, workspace_dir: str | Path, source_workspace: str | Path | None = None) -> Path:
    source = Path(source_workspace) if source_workspace else Path(project_root) / "harness_seed"
    target = Path(workspace_dir)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def load_harness_text(workspace_dir: str | Path) -> dict[str, str]:
    root = Path(workspace_dir)
    sections = {
        "system_prompt": read_text(root / "system_prompt.md"),
        "tools": "",
        "skills": "",
        "memory": "",
    }
    for folder, key in [("tools", "tools"), ("skills", "skills"), ("memory", "memory")]:
        parts = []
        for path in sorted((root / folder).glob("*.md")):
            parts.append(f"\n## {path.name}\n{read_text(path)}")
        sections[key] = "\n".join(parts)
    return sections


def assert_within_workspace(path: str | Path, workspace_dir: str | Path) -> None:
    candidate = Path(path).resolve()
    root = Path(workspace_dir).resolve()
    if root not in candidate.parents and candidate != root:
        raise PermissionError(f"Evolver attempted to write outside workspace: {candidate}")


def workspace_allowed_files(workspace_dir: str | Path) -> set[Path]:
    root = Path(workspace_dir)
    allowed: set[Path] = set()
    for file_name in ALLOWED_WORKSPACE_FILES:
        allowed.add((root / file_name).resolve())
    for folder in ALLOWED_WORKSPACE_DIRS:
        if (root / folder).exists():
            allowed.update(p.resolve() for p in (root / folder).rglob("*") if p.is_file())
    return allowed


def write_workspace_file(workspace_dir: str | Path, relative_path: str, content: str) -> Path:
    root = Path(workspace_dir)
    target = root / relative_path
    assert_within_workspace(target, root)
    first = Path(relative_path).parts[0]
    if first not in ALLOWED_WORKSPACE_DIRS and relative_path not in ALLOWED_WORKSPACE_FILES:
        raise PermissionError(f"File is not in evolver allowlist: {relative_path}")
    ensure_dir(target.parent)
    target.write_text(content, encoding="utf-8")
    return target
