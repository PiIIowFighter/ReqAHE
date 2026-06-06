from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import workspace_allowed_files, write_workspace_file
from reqahe.llm.client import OpenAICompatibleClient
from reqahe.utils.io import read_json, read_text, write_json, write_text


def evolve_workspace(
    iteration_dir: str | Path,
    workspace_dir: str | Path,
    iteration: int,
    llm: OpenAICompatibleClient,
    evolver_model: str,
) -> Path:
    iteration_path = Path(iteration_dir)
    workspace = Path(workspace_dir).resolve()
    analysis_dir = iteration_path / "analysis"
    payload = {
        "iteration": iteration,
        "rollout_metrics": read_json(iteration_path / "summary.json") if (iteration_path / "summary.json").exists() else {},
        "analysis_overview": read_text(analysis_dir / "overview.md"),
        "analysis_recommendations": read_text(analysis_dir / "recommendations.md"),
        "failure_patterns": read_json(analysis_dir / "failure_patterns.json"),
        "successful_patterns": read_json(analysis_dir / "successful_patterns.json"),
        "workspace_files": _workspace_payload(workspace),
        "allowed_write_roots": ["tools", "middleware", "skills", "memory", "subagents", "system_prompt.md", "code_agent.yaml"],
    }
    data = llm.json_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are the harness evolver in a requirements elicitation optimization loop. "
                    "Make a small, evidence-backed harness improvement using only the provided workspace files. "
                    "Return strict compact JSON with keys: changes, file_edits, evolver_rationale. "
                    "changes must be an array of short strings. "
                    "file_edits must be a non-empty array with at most 3 small edits. "
                    "Each edit must be one of: "
                    "{\"relative_path\":\"...\",\"operation\":\"append\",\"section_title\":\"...\",\"lines\":[\"...\"]} or "
                    "{\"relative_path\":\"...\",\"operation\":\"replace\",\"old\":\"exact existing text\",\"new_lines\":[\"...\"]}. "
                    "Prefer append edits to Markdown prompt/skill/memory files. Keep each lines array under 12 short strings. "
                    "Only edit files that already appear in workspace_files or are under tools, middleware, skills, memory, or subagents. "
                    "Do not include Markdown fences."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=evolver_model,
        purpose="harness evolution generation",
    )
    _validate_evolution(data)
    written_files = []
    for edit in data["file_edits"]:
        relative_path = _apply_edit(workspace, edit)
        written_files.append(relative_path)
    manifest = {
        "iteration": iteration,
        "changes": data["changes"],
        "file_edits": [{"relative_path": path} for path in written_files],
        "evolver_mode": "llm",
        "evolver_rationale": str(data.get("evolver_rationale") or ""),
        "safety": {"allowed_write_root": str(workspace.resolve()), "status": "passed"},
    }
    out = workspace / "change_manifest.json"
    write_json(out, manifest)
    write_json(iteration_path / "change_manifest.json", manifest)
    write_text(iteration_path / "evolver.log", "LLM evolver completed.\n")
    _commit_workspace(workspace, f"iteration {iteration}: llm harness update")
    return out


def _workspace_payload(workspace: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    editable_suffixes = {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
    for path in sorted(workspace_allowed_files(workspace)):
        if path.name == "change_manifest.json":
            continue
        if path.suffix.lower() not in editable_suffixes:
            continue
        relative = path.relative_to(workspace).as_posix()
        payload[relative] = read_text(path)
    return payload


def _validate_evolution(data: dict[str, Any]) -> None:
    if not isinstance(data.get("changes"), list) or not data["changes"]:
        raise RuntimeError("harness evolution generation failed: changes must be a non-empty list")
    if not isinstance(data.get("file_edits"), list) or not data["file_edits"]:
        raise RuntimeError("harness evolution generation failed: file_edits must be a non-empty list")
    if len(data["file_edits"]) > 3:
        raise RuntimeError("harness evolution generation failed: file_edits must contain at most 3 edits")
    for edit in data["file_edits"]:
        if not isinstance(edit, dict) or not edit.get("relative_path") or edit.get("operation") not in {"append", "replace"}:
            raise RuntimeError("harness evolution generation failed: invalid file edit")
        if edit["operation"] == "append" and not isinstance(edit.get("lines"), list):
            raise RuntimeError("harness evolution generation failed: append edit requires lines")
        if edit["operation"] == "replace" and (not isinstance(edit.get("old"), str) or not isinstance(edit.get("new_lines"), list)):
            raise RuntimeError("harness evolution generation failed: replace edit requires old and new_lines")


def _apply_edit(workspace: Path, edit: dict[str, Any]) -> str:
    relative_path = str(edit["relative_path"])
    current = read_text(workspace / relative_path)
    if edit["operation"] == "append":
        section_title = str(edit.get("section_title") or "LLM Evolution Update").strip()
        lines = [str(line).rstrip() for line in edit["lines"]]
        addition = "\n".join([f"## {section_title}", *lines]).rstrip()
        content = current.rstrip() + "\n\n" + addition + "\n"
    else:
        old = str(edit["old"])
        if old not in current:
            raise RuntimeError(f"harness evolution generation failed: replacement text not found in {relative_path}")
        replacement = "\n".join(str(line).rstrip() for line in edit["new_lines"])
        content = current.replace(old, replacement, 1)
    write_workspace_file(workspace, relative_path, content)
    return relative_path


def _commit_workspace(workspace: Path, message: str) -> None:
    try:
        subprocess.run(["git", "init"], cwd=workspace, check=False, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=False, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=False, capture_output=True, text=True)
    except Exception:
        # Git is useful provenance, not a blocker for the optimization loop.
        return
