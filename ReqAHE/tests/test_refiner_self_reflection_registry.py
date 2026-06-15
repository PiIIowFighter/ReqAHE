from __future__ import annotations

from pathlib import Path

import pytest

from reqahe.diagnoser.pipeline import load_declared_components
from reqahe.harness.workspace import is_workspace_write_allowed
from reqahe.refiner.pipeline import apply_file_edits, build_write_policy, select_schemas_for_fix_plan, validate_and_plan_refinement
from reqahe.refiner.validation import validate_proposed_edits
from tests.test_diagnoser_refiner_schema import (
    _reflection_fix_plan,
    _valid_reflection_check_py,
    _workspace_with_manifest,
)


def test_system_write_registry_yaml_allowed(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    assert is_workspace_write_allowed(workspace, "self_reflection/registry.yaml")

    refinement = {
        "changes": [{"change_id": "C1", "component": "self_reflection", "summary": "add check"}],
        "file_edits": [
            {
                "relative_path": "self_reflection/example_check/check.py",
                "operation": "create",
                "new_content": _valid_reflection_check_py("example_check"),
            },
            {
                "relative_path": "self_reflection/example_check/PROMPT.md",
                "operation": "create",
                "new_content": "Revise the candidate.",
            },
        ],
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": [
                    "self_reflection/example_check/check.py",
                    "self_reflection/example_check/PROMPT.md",
                ],
            }
        ],
        "refiner_rationale": "add check",
        "similarity_audit": [],
    }
    planned = validate_and_plan_refinement(workspace, refinement, {"system_prompt", "self_reflection"})
    written = apply_file_edits(workspace, planned)
    assert "self_reflection/example_check/check.py" in written
    assert "self_reflection/example_check/PROMPT.md" in written
    assert (workspace / "self_reflection" / "registry.yaml").is_file()
    assert "example_check" in (workspace / "self_reflection" / "registry.yaml").read_text(encoding="utf-8")


def test_llm_direct_registry_edit_rejected_by_validation(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    fix_plan = _reflection_fix_plan()
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    refinement = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "self_reflection", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "self_reflection/registry.yaml",
                "operation": "create",
                "new_content": "version: 0.2\nchecks: []\n",
            }
        ],
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": ["self_reflection/registry.yaml"],
            }
        ],
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    report = validate_proposed_edits(
        workspace,
        refinement,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "self_reflection"},
        raw_refinement=refinement,
    )
    assert report["ok"] is False
    assert any("registry is synchronized by the runtime" in err for err in report["errors"])


def test_validate_and_plan_refinement_rejects_registry_yaml(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    refinement = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "self_reflection", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "self_reflection/registry.yaml",
                "operation": "replace",
                "old": 'version: "0.2"',
                "new_content": 'version: "0.2"\nchecks: []\n',
            }
        ],
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": ["self_reflection/registry.yaml"],
            }
        ],
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    with pytest.raises(RuntimeError, match="registry is synchronized by the runtime"):
        validate_and_plan_refinement(workspace, refinement, {"system_prompt", "self_reflection"})
