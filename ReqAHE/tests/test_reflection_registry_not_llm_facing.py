from pathlib import Path

from reqahe.harness.component_schema import ALLOWED_ARTIFACT_TYPES
from reqahe.refiner.pipeline import _sync_reflection_registry_entries, select_schemas_for_fix_plan
from reqahe.refiner.validation import validate_proposed_edits


def test_allowed_artifact_types_are_exactly_three_evolved_types() -> None:
    assert "reflection_registry_v1" not in ALLOWED_ARTIFACT_TYPES
    assert ALLOWED_ARTIFACT_TYPES == {
        "system_prompt_section_v1",
        "skill_markdown_v1",
        "reflection_check_bundle_v1",
    }


def test_selected_schemas_for_fix_plan_include_reflection_check_bundle_v1() -> None:
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "self_reflection",
                "artifact_type": "reflection_check_bundle_v1",
                "operation_intent": "create",
                "target_file_hint": "self_reflection/focused_check/check.py",
            }
        ]
    }
    selected = select_schemas_for_fix_plan(fix_plan)
    assert "reflection_check_bundle_v1" in selected
    assert set(selected).issubset(ALLOWED_ARTIFACT_TYPES)


def test_validator_rejects_llm_edit_to_registry_yaml(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "code_agent.yaml").write_text(
        "name: test\nsystem_prompt: system_prompt.md\nself_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "system_prompt.md").write_text("# Role\n", encoding="utf-8")
    (workspace / "self_reflection").mkdir()
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")

    proposed = {
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
                "new_or_updated_files": ["self_reflection/focused_check/check.py"],
            }
        ],
        "similarity_audit": [],
    }
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "self_reflection",
                "artifact_type": "reflection_check_bundle_v1",
                "operation_intent": "create",
                "target_file_hint": "self_reflection/focused_check/check.py",
            }
        ]
    }
    write_policy = {
        "max_fixes": 3,
        "allowed_components": ["self_reflection"],
        "path_patterns": {
            "self_reflection": "self_reflection/<reflection-id>/check.py + self_reflection/<reflection-id>/PROMPT.md"
        },
        "allow_registry_edit": False,
    }
    report = validate_proposed_edits(
        workspace,
        proposed,
        fix_plan,
        write_policy,
        selected_schemas={"reflection_check_bundle_v1": {}},
        declared_components={"self_reflection"},
    )
    assert not report["ok"]
    assert any("registry.yaml" in err for err in report["errors"])


def test_python_syncs_registry_after_new_reflection_bundle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rel_path = "self_reflection/focused_check/check.py"
    prompt_path = "self_reflection/focused_check/PROMPT.md"
    content = (
        '"""\n'
        "component: self_reflection\n"
        "reflection_id: focused_check\n"
        "name: Focused Check\n"
        "version: 0.1\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return []\n"
    )
    planned = _sync_reflection_registry_entries(
        workspace,
        [(rel_path, content), (prompt_path, "Revise the candidate.")],
    )
    registry_entry = next(item for item in planned if item[0] == "self_reflection/registry.yaml")
    assert "focused_check" in registry_entry[1]
    assert "focused_check/check.py" in registry_entry[1]
    assert "focused_check/PROMPT.md" in registry_entry[1]
    assert "0.2" in registry_entry[1]
