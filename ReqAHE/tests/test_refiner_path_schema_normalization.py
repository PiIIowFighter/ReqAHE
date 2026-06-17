from pathlib import Path

from reqahe.refiner.pipeline import (
    _normalize_edits_from_llm,
    _normalize_fix_plan_target_hints,
    _sanitize_fix_plan_or_drop_invalid,
)


DECLARED_COMPONENTS = {"system_prompt", "skills", "self_reflection"}


def _fix(
    *,
    component: str,
    target_file_hint: str,
    operation_intent: str = "update",
    fix_id: str = "F1",
) -> dict:
    return {
        "fix_id": fix_id,
        "component": component,
        "artifact_type": "skill_markdown_v1",
        "operation_intent": operation_intent,
        "target_file_hint": target_file_hint,
        "evidence": ["route_stats"],
        "fix_summary": "repair target",
        "expected_effect": "improve next rollout",
        "risk": "low",
    }


def test_skill_router_system_prompt_target_normalizes_to_system_prompt() -> None:
    fix_plan = {
        "fix_plan": [
            _fix(component="skill_router", target_file_hint="system_prompt.md"),
        ]
    }

    normalized = _normalize_fix_plan_target_hints(fix_plan)
    item = normalized["fix_plan"][0]

    assert item["component"] == "system_prompt"
    assert item["artifact_type"] == "system_prompt_section_v1"
    assert item["target_file_hint"] == "system_prompt.md"


def test_skills_component_system_prompt_target_uses_path_component() -> None:
    fix_plan = {
        "fix_plan": [
            _fix(component="skills", target_file_hint="system_prompt.md"),
        ]
    }

    normalized = _normalize_fix_plan_target_hints(fix_plan)
    item = normalized["fix_plan"][0]

    assert item["component"] == "system_prompt"
    assert item["artifact_type"] == "system_prompt_section_v1"


def test_invalid_self_reflection_readme_does_not_drop_valid_skill_fix() -> None:
    fix_plan = {
        "fix_plan": [
            _fix(
                component="self_reflection",
                target_file_hint="self_reflection/README.md",
                operation_intent="create",
                fix_id="F_bad",
            ),
            _fix(
                component="skills",
                target_file_hint="skills/focus-question/SKILL.md",
                operation_intent="update",
                fix_id="F_good",
            ),
        ]
    }

    sanitized = _sanitize_fix_plan_or_drop_invalid(fix_plan, DECLARED_COMPONENTS)

    assert [item["fix_id"] for item in sanitized["fix_plan"]] == ["F_good"]
    assert sanitized["fix_plan"][0]["component"] == "skills"
    assert sanitized["dropped_invalid_fixes"]
    assert "self_reflection/<reflection-id>/check.py" in sanitized["dropped_invalid_fixes"][0]["reason"]


def test_mixed_edits_rebuild_schema_compliance_by_path(tmp_path: Path) -> None:
    refinement = {
        "changes": [],
        "file_edits": [
            {"relative_path": "skills/a/SKILL.md", "operation": "replace", "old": "", "new_content": "a"},
            {"relative_path": "skills/b/SKILL.md", "operation": "replace", "old": "", "new_content": "b"},
            {"relative_path": "system_prompt.md", "operation": "replace", "old": "", "new_content": "prompt"},
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": [
                    "skills/a/SKILL.md",
                    "skills/b/SKILL.md",
                    "system_prompt.md",
                ],
            }
        ],
    }

    normalized = _normalize_edits_from_llm(tmp_path, refinement, {"fix_plan": []})
    compliance = normalized["schema_compliance"]

    assert compliance == [
        {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/a/SKILL.md", "skills/b/SKILL.md"],
        },
        {
            "component": "system_prompt",
            "schema_name": "system_prompt_section_v1",
            "new_or_updated_files": ["system_prompt.md"],
        },
    ]
    assert "system_prompt.md" not in compliance[0]["new_or_updated_files"]


def test_self_reflection_bundle_schema_compliance_and_primary_target(tmp_path: Path) -> None:
    refinement = {
        "changes": [],
        "file_edits": [
            {
                "relative_path": "self_reflection/example_reflection_bundle/check.py",
                "operation": "create",
                "new_content": "def check(candidate, state):\n    return []\n",
            },
            {
                "relative_path": "self_reflection/example_reflection_bundle/PROMPT.md",
                "operation": "create",
                "new_content": "Revise the candidate question.",
            },
        ],
        "schema_compliance": [],
    }
    fix_plan = {
        "fix_plan": [
            _fix(
                component="self_reflection",
                target_file_hint=(
                    "self_reflection/example_reflection_bundle/check.py + "
                    "self_reflection/example_reflection_bundle/PROMPT.md"
                ),
                operation_intent="create",
            )
        ]
    }

    normalized_plan = _normalize_fix_plan_target_hints(fix_plan)
    normalized_edits = _normalize_edits_from_llm(tmp_path, refinement, normalized_plan)

    assert normalized_plan["fix_plan"][0]["target_file_hint"] == "self_reflection/example_reflection_bundle/check.py"
    assert normalized_edits["schema_compliance"] == [
        {
            "component": "self_reflection",
            "schema_name": "reflection_check_bundle_v1",
            "new_or_updated_files": [
                "self_reflection/example_reflection_bundle/PROMPT.md",
                "self_reflection/example_reflection_bundle/check.py",
            ],
        }
    ]
