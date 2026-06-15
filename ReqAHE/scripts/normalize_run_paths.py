#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from reqahe.utils.paths import (
    looks_like_absolute_path,
    relativize_all_absolute_strings,
    relativize_json_paths,
    to_posix_relpath,
)


JSON_SUFFIXES = {".json", ".jsonl"}
SKIP_DIR_NAMES = {".git", ".pytest_cache", "__pycache__"}


def _project_root_for_runs(runs_dir: Path) -> Path:
    if runs_dir.name == "runs":
        return runs_dir.parent
    return runs_dir.parent


def _iter_json_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in JSON_SUFFIXES:
            files.append(path)
    return sorted(files)


def _normalize_file(path: Path, project_root: Path, run_dir: Path) -> tuple[dict | list | None, bool]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None, False

    bases = [path.parent, run_dir, project_root]
    normalized = data
    changed = False
    for base in bases:
        updated = relativize_json_paths(normalized, base)
        updated = relativize_all_absolute_strings(updated, base)
        if updated != normalized:
            changed = True
            normalized = updated
    return normalized, changed


def normalize_runs(runs_dir: Path, *, write: bool) -> list[str]:
    runs_dir = runs_dir.resolve()
    project_root = _project_root_for_runs(runs_dir)
    reports: list[str] = []

    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        for json_path in _iter_json_files(run_dir):
            normalized, changed = _normalize_file(json_path, project_root, run_dir)
            if not changed or normalized is None:
                continue
            rel = to_posix_relpath(json_path, project_root)
            reports.append(rel)
            if write:
                json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize absolute paths in run artifacts to relative paths.")
    parser.add_argument("--runs", default="runs", help="Path to runs directory")
    parser.add_argument("--write", action="store_true", help="Apply changes (default is dry-run)")
    args = parser.parse_args()

    runs_dir = Path(args.runs)
    if not runs_dir.exists():
        raise SystemExit(f"runs directory not found: {runs_dir}")

    changed = normalize_runs(runs_dir, write=args.write)
    mode = "write" if args.write else "dry-run"
    if not changed:
        print(f"[{mode}] no relative-path updates needed under {runs_dir}")
        return 0
    print(f"[{mode}] would update {len(changed)} file(s):" if not args.write else f"[write] updated {len(changed)} file(s):")
    for item in changed:
        print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
