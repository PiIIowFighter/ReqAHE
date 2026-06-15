from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from reqahe.infra.io import read_json, write_json
from reqahe.refiner.pipeline import (
    _drop_offending_high_similarity_skill_creates,
    _normalize_fix_plan_target_hints,
    _write_refiner_stage,
    refine_harness,
)
from reqahe.refiner.validation import validate_proposed_edits
from reqahe.refiner.pipeline import build_write_policy, select_schemas_for_fix_plan
from reqahe.diagnoser.pipeline import load_declared_components
from tests.test_diagnoser_refiner_schema import (
    _reflection_fix_plan,
    _skill_create_similarity_audit,
    _skill_self_validation,
    _valid_reflection_check_py,
    _valid_skill_lines,
    _valid_system_prompt,
    _workspace_with_manifest,
)


def test_normalize_self_reflection_bundle_target_hint() -> None:
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "self_reflection",
                "artifact_type": "reflection_check_bundle_v1",
                "operation_intent": "create",
                "target_file_hint": "self_reflection/x/check.py + self_reflection/x/PROMPT.md",
                "evidence": ["RC1"],
                "fix_summary": "add check",
                "expected_effect": "warn",
                "risk": "low",
            }
        ],
        "rationale": "bundle",
    }
    normalized = _normalize_fix_plan_target_hints(fix_plan)
    assert normalized["fix_plan"][0]["target_file_hint"] == "self_reflection/x/check.py"


def test_normalize_self_reflection_prompt_md_only_target_hint() -> None:
    from reqahe.refiner.pipeline import _normalize_single_target_hint

    assert _normalize_single_target_hint("self_reflection/x/PROMPT.md") == "self_reflection/x/check.py"


def test_permission_error_writes_refiner_error_artifacts(tmp_path: Path, monkeypatch) -> None:
    iteration = tmp_path / "batch_001"
    workspace = iteration / "workspace_candidate"
    analysis = iteration / "analysis"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "memory").mkdir()
    (workspace / "self_reflection").mkdir()
    (workspace / "system_prompt.md").write_text(_valid_system_prompt(), encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: seed\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n"
        "memory:\n  - memory/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    write_json(
        analysis / "component_localization.json",
        {"localization_summary": "", "component_findings": [], "refiner_guidance": {}},
    )

    class FakeLLM:
        call_count = 0

        def json_chat(self, *args, **kwargs) -> dict:
            FakeLLM.call_count += 1
            if FakeLLM.call_count == 1:
                return {
                    "fix_plan": [
                        {
                            "fix_id": "F1",
                            "component": "skills",
                            "artifact_type": "skill_markdown_v1",
                            "operation_intent": "create",
                            "target_file_hint": "skills/new-skill/SKILL.md",
                            "evidence": ["RC1"],
                            "fix_summary": "add skill",
                            "expected_effect": "improve",
                            "risk": "low",
                        }
                    ],
                    "rationale": "skill",
                }
            return {
                "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "add"}],
                "file_edits": [
                    {
                        "relative_path": "skills/new-skill/SKILL.md",
                        "operation": "create",
                        "new_content": "\n".join(_valid_skill_lines("new-skill")),
                    }
                ],
                "schema_compliance": [
                    {
                        "component": "skills",
                        "schema_name": "skill_markdown_v1",
                        "new_or_updated_files": ["skills/new-skill/SKILL.md"],
                    }
                ],
                "refiner_rationale": "ok",
                "similarity_audit": _skill_create_similarity_audit(),
                "self_validation": _skill_self_validation(),
            }

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)

    def _raise_permission(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("reqahe.refiner.pipeline.write_workspace_file", _raise_permission)

    with pytest.raises(PermissionError):
        refine_harness(iteration, workspace, 1, FakeLLM(), "m")  # type: ignore[arg-type]

    refiner = iteration / "refiner"
    assert (refiner / "refiner_error.json").exists()
    assert (refiner / "refiner_error.md").exists()
    assert (refiner / "validation_report.json").exists()
    assert (refiner / "refiner_stats.json").exists()
    stage = read_json(refiner / "STAGE.json")
    assert stage["status"] == "failed"
    error = read_json(refiner / "refiner_error.json")
    assert error["ok"] is False
    assert error["error_type"] == "PermissionError"


def test_keyboard_interrupt_writes_interrupted_stage(tmp_path: Path, monkeypatch) -> None:
    iteration = tmp_path / "batch_001"
    workspace = iteration / "workspace_candidate"
    analysis = iteration / "analysis"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "self_reflection").mkdir()
    (workspace / "system_prompt.md").write_text(_valid_system_prompt(), encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: seed\nsystem_prompt: system_prompt.md\nskills:\n  - skills/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    write_json(
        analysis / "component_localization.json",
        {"localization_summary": "", "component_findings": [], "refiner_guidance": {}},
    )

    class FakeLLM:
        def json_chat(self, *args, **kwargs) -> dict:
            raise KeyboardInterrupt()

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)

    with pytest.raises(KeyboardInterrupt):
        refine_harness(iteration, workspace, 1, FakeLLM(), "m")  # type: ignore[arg-type]

    stage = read_json(iteration / "refiner" / "STAGE.json")
    assert stage["status"] == "interrupted"


def test_cli_keyboard_interrupt_writes_batch_state(tmp_path: Path) -> None:
    from reqahe.cli import _write_batch_interrupted_state

    batch_dir = tmp_path / "batch_001"
    batch_dir.mkdir()
    workspace_before = tmp_path / "workspace_before"
    workspace_before.mkdir()
    rollout_after = batch_dir / "rollout_after"
    _write_batch_interrupted_state(
        batch_dir,
        rollout_after=rollout_after,
        workspace_before=workspace_before,
        refiner_stage="generate_edits",
        iteration=1,
        batch_idx=1,
    )
    status = read_json(rollout_after / "STATUS.json")
    decision = read_json(batch_dir / "batch_decision.json")
    state = read_json(batch_dir / "batch_state.json")
    assert status["status"] == "interrupted"
    assert decision["decision"] == "rollback_interrupted"
    assert state["status"] == "interrupted"
    assert state["stage"] == "refiner"


def test_high_similarity_skill_create_drop_keeps_other_edits(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md", "self_reflection": "self_reflection/README.md"})
    existing_skill = workspace / "skills" / "style-elaboration" / "SKILL.md"
    existing_skill.parent.mkdir(parents=True)
    existing_skill.write_text("\n".join(_valid_skill_lines("style-elaboration")), encoding="utf-8")

    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": "skills/style-copy/SKILL.md",
                "evidence": ["RC1"],
                "fix_summary": "style",
                "expected_effect": "style",
                "risk": "low",
            },
            {
                "fix_id": "F2",
                "component": "self_reflection",
                "artifact_type": "reflection_check_bundle_v1",
                "operation_intent": "create",
                "target_file_hint": "self_reflection/new_check/check.py",
                "evidence": ["RC2"],
                "fix_summary": "check",
                "expected_effect": "warn",
                "risk": "low",
            },
        ],
        "rationale": "mixed",
    }
    refinement = {
        "changes": [
            {"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "bad skill"},
            {"change_id": "C2", "fix_id": "F2", "component": "self_reflection", "summary": "check"},
        ],
        "file_edits": [
            {
                "relative_path": "skills/style-copy/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("style-copy")),
            },
            {
                "relative_path": "self_reflection/new_check/check.py",
                "operation": "create",
                "new_content": _valid_reflection_check_py("new_check"),
            },
            {
                "relative_path": "self_reflection/new_check/PROMPT.md",
                "operation": "create",
                "new_content": "Revise candidate.",
            },
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/style-copy/SKILL.md"],
            },
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": [
                    "self_reflection/new_check/check.py",
                    "self_reflection/new_check/PROMPT.md",
                ],
            },
        ],
        "similarity_audit": [
            {
                "target_path": "skills/style-copy/SKILL.md",
                "proposed_intent": "style elaboration duplicate",
                "closest_existing_skill_id": "style-elaboration",
                "closest_existing_path": "skills/style-elaboration/SKILL.md",
                "similarity_score": 0.92,
                "matched_dimensions": ["problem_similarity"],
                "decision": "create_new",
                "justification": "duplicate",
            }
        ],
        "self_validation": _skill_self_validation(),
        "refiner_rationale": "mixed",
    }
    validation_report = {
        "ok": False,
        "errors": ["High-similarity skill creation is forbidden."],
        "warnings": [],
        "structured_errors": [
            {
                "error_type": "high_similarity_skill_create",
                "new_skill_path": "skills/style-copy/SKILL.md",
                "closest_existing_skill": "skills/style-elaboration/SKILL.md",
                "required_action": "replace_existing_skill_instead_of_create",
            }
        ],
        "checked_files": [],
    }
    dropped, warnings = _drop_offending_high_similarity_skill_creates(refinement, validation_report)
    assert warnings
    paths = {edit["relative_path"] for edit in dropped["file_edits"]}
    assert "skills/style-copy/SKILL.md" not in paths
    assert "self_reflection/new_check/check.py" in paths

    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    report = validate_proposed_edits(
        workspace,
        dropped,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills", "self_reflection"},
        raw_refinement=refinement,
    )
    assert report["ok"], report["errors"]


def test_write_refiner_stage_is_fault_tolerant(tmp_path: Path) -> None:
    refiner_dir = tmp_path / "refiner"
    refiner_dir.mkdir()
    with patch("reqahe.refiner.pipeline.write_json", side_effect=OSError("disk full")):
        _write_refiner_stage(refiner_dir, "validate", "running")
