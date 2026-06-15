from pathlib import Path

import pytest

from reqahe.harness.component_schema import (
    parse_markdown_frontmatter,
    validate_component_file,
    validate_reflection_python,
    validate_reflection_python_check,
    validate_system_prompt,
)
from reqahe.refiner.pipeline import (
    _normalize_component_content,
    _normalize_fix_plan_target_hints,
    _normalize_refinement_file_contents,
    _normalize_single_target_hint,
    _normalize_skill_frontmatter,
    _validate_fix_target_hint,
    build_write_policy,
    select_schemas_for_fix_plan,
    validate_and_plan_refinement,
)
from reqahe.refiner.validation import validate_fix_plan, validate_proposed_edits
from reqahe.diagnoser.pipeline import load_declared_components


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "skills").mkdir()
    (workspace / "self_reflection").mkdir()
    (workspace / "system_prompt.md").write_text(
        "# Role\nInterviewer.\n\n"
        "# Goal\nElicit requirements.\n\n"
        "# Interaction Rules\nUse harness components.\n\n"
        "# Output Format\nReturn JSON.\n\n"
        "# Safety Boundaries\nDo not reveal hidden data.\n",
        encoding="utf-8",
    )
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    return workspace


def _simplified_skill_body() -> str:
    return (
        "# Purpose\nAsk about style.\n\n"
        "# When to Use\nUse when style is missing.\n\n"
        "# Procedure\nAsk one style question.\n\n"
        "# Question Pattern\nWhat visual style do you prefer?\n\n"
        "# Stop Condition\nStop when style is answered.\n\n"
        "# Anti-patterns\nDo not ask generic questions.\n\n"
        "# Expected Effect\nImprove style coverage.\n"
    )


def _simplified_skill_frontmatter(skill_id: str = "style-elaboration") -> str:
    return "\n".join(
        [
            "---",
            "component: skills",
            f"skill_id: {skill_id}",
            "name: Style Elaboration",
            "description: Ask about missing style details.",
            'trigger: "style details are missing"',
            "evidence:",
            "  - RC2",
            "  - RC4",
            'expected_effect: "Improve style IRE"',
            "priority: high",
            "---",
        ]
    )


def _valid_fix_plan(skill_path: str = "skills/style-elaboration/SKILL.md") -> dict:
    return {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": skill_path,
                "evidence": ["RC2"],
                "fix_summary": "add style skill",
                "expected_effect": "improve style coverage",
                "risk": "low",
            }
        ],
        "rationale": "style gap",
    }


def test_normalize_skill_frontmatter_from_simplified_llm_output() -> None:
    metadata, _ = parse_markdown_frontmatter(
        _simplified_skill_frontmatter("style-elaboration") + "\n" + _simplified_skill_body(),
        "skills/style-elaboration/SKILL.md",
    )
    normalized = _normalize_skill_frontmatter(metadata, "style-elaboration", iteration=1)

    assert normalized["id"] == "style-elaboration"
    assert normalized["enabled"] is True
    assert isinstance(normalized["use_when"], list)
    assert isinstance(normalized["avoid_when"], list)
    assert isinstance(normalized["scope"], list)
    assert isinstance(normalized["risk_notes"], list)
    assert normalized["intent"]


def test_validate_proposed_edits_accepts_normalized_skill(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    fix_plan = _valid_fix_plan()
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    refinement = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "add skill"}],
        "file_edits": [
            {
                "relative_path": "skills/style-elaboration/SKILL.md",
                "operation": "create",
                "new_content": _simplified_skill_frontmatter() + "\n" + _simplified_skill_body(),
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/style-elaboration/SKILL.md"],
            }
        ],
        "refiner_rationale": "add style skill",
        "similarity_audit": [
            {
                "proposed_intent": "add style elaboration skill",
                "closest_existing_skill_id": None,
                "closest_existing_path": None,
                "similarity_score": 0.0,
                "matched_dimensions": [],
                "decision": "create_new",
                "justification": "no existing skill in workspace",
            }
        ],
        "self_validation": {
            "similarity_gate_applied": True,
            "no_duplicate_skill_created": True,
            "no_append_to_skill_markdown": True,
            "no_skill_readme_edit": True,
        },
    }
    normalized = _normalize_refinement_file_contents(refinement, iteration=1)
    report = validate_proposed_edits(
        workspace,
        normalized,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills", "self_reflection"},
        raw_refinement=refinement,
    )
    assert report["ok"], report["errors"]
    validate_and_plan_refinement(workspace, normalized, {"system_prompt", "skills", "self_reflection"})


def test_yaml_parse_error_becomes_runtime_error() -> None:
    content = '---\nname: bad\ndescription: "don\\\'t break yaml"\n---\n# Purpose\nx\n'
    with pytest.raises(RuntimeError, match="frontmatter YAML parse failed"):
        parse_markdown_frontmatter(content, "skills/bad/SKILL.md")


def test_reflection_docstring_auto_normalization() -> None:
    source = (
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return []\n"
    )
    normalized = _normalize_component_content("self_reflection/example_check/check.py", source)
    metadata = validate_reflection_python_check(normalized, "self_reflection/example_check/check.py")
    assert metadata["reflection_id"] == "example_check"
    assert metadata["component"] == "self_reflection"
    assert metadata["hook"] == "question_candidate"
    assert metadata["mode"] == "warn"


def test_reflection_normalization_overwrites_wrong_metadata() -> None:
    source = (
        '"""\n'
        "component: wrong\n"
        "reflection_id: wrong_id\n"
        "name: Bad\n"
        "version: 1.0.0\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return []\n"
    )
    normalized = _normalize_component_content("self_reflection/example_check/check.py", source)
    assert normalized.count('"""') == 2
    assert "component: self_reflection" in normalized
    assert "reflection_id: example_check" in normalized
    assert "def check(candidate" in normalized
    metadata = validate_reflection_python_check(normalized, "self_reflection/example_check/check.py")
    assert metadata["component"] == "self_reflection"
    assert metadata["reflection_id"] == "example_check"


def test_reflection_validator_rejects_wrong_component() -> None:
    content = (
        '"""\n'
        "component: wrong\n"
        "reflection_id: example_check\n"
        "name: Example Check\n"
        "version: 1.0.0\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "def check(candidate, state):\n"
        "    return []\n"
    )
    errors = validate_component_file("self_reflection/example_check/check.py", content)
    assert errors
    assert any("component must be self_reflection" in err for err in errors)


def test_reflection_validator_rejects_wrong_reflection_id() -> None:
    content = (
        '"""\n'
        "component: self_reflection\n"
        "reflection_id: other_id\n"
        "name: Example Check\n"
        "version: 1.0.0\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "def check(candidate, state):\n"
        "    return []\n"
    )
    errors = validate_reflection_python("self_reflection/example_check/check.py", content)
    assert errors
    assert any("reflection_id must equal bundle folder name example_check" in err for err in errors)


def test_generate_prompt_forbids_direct_registry_edit() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "reqahe"
        / "refiner"
        / "prompts"
        / "generate_edits_and_validate.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Do not edit files outside the allowed harness workspace" in prompt
    assert "Do not modify evaluator" in prompt


def test_system_prompt_rejects_new_top_level_heading() -> None:
    content = (
        "# Role\nRole text.\n\n"
        "# Goal\nGoal text.\n\n"
        "# Interaction Rules\nRules.\n\n"
        "# Output Format\nJSON.\n\n"
        "# Safety Boundaries\nSafe.\n\n"
        "# Scope and Boundaries\nNot allowed.\n"
    )
    errors = validate_system_prompt("system_prompt.md", content)
    assert errors
    assert any("unsupported system_prompt sections" in err for err in errors)


def test_system_prompt_valid_five_sections() -> None:
    content = (
        "# Role\nRole text.\n\n"
        "# Goal\nGoal text.\n\n"
        "# Interaction Rules\nRules.\n\n"
        "# Output Format\nJSON.\n\n"
        "# Safety Boundaries\nSafe.\n"
    )
    assert validate_system_prompt("system_prompt.md", content) == []


def _valid_reflection_check_py(reflection_id: str) -> str:
    name = reflection_id.replace("_", " ").title()
    return (
        '"""\n'
        "component: self_reflection\n"
        f"reflection_id: {reflection_id}\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return []\n"
    )


def _reflection_fix_plan(target: str = "self_reflection/example_check/check.py") -> dict:
    return {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "self_reflection",
                "artifact_type": "reflection_check_bundle_v1",
                "operation_intent": "create",
                "target_file_hint": target,
                "evidence": ["RC1"],
                "fix_summary": "add reflection check",
                "expected_effect": "warn on bad candidates",
                "risk": "low",
            }
        ],
        "rationale": "add runtime check",
    }


def test_validate_fix_plan_accepts_normalized_self_reflection_bundle_hint() -> None:
    fix_plan = _normalize_fix_plan_target_hints(
        _reflection_fix_plan("self_reflection/example_check/check.py + self_reflection/example_check/PROMPT.md")
    )
    validate_fix_plan(fix_plan, {"system_prompt", "skills", "self_reflection"})
    assert fix_plan["fix_plan"][0]["target_file_hint"] == "self_reflection/example_check/check.py"


def test_normalize_prompt_md_target_hint_to_check_py() -> None:
    assert (
        _normalize_single_target_hint("self_reflection/example_check/PROMPT.md")
        == "self_reflection/example_check/check.py"
    )
    fix_plan = _normalize_fix_plan_target_hints(_reflection_fix_plan("self_reflection/example_check/PROMPT.md"))
    assert fix_plan["fix_plan"][0]["target_file_hint"] == "self_reflection/example_check/check.py"
    validate_fix_plan(fix_plan, {"system_prompt", "skills", "self_reflection"})


def test_validate_fix_target_hint_accepts_reflection_check_py() -> None:
    _validate_fix_target_hint(
        "self_reflection",
        "reflection_check_bundle_v1",
        "create",
        "self_reflection/example_check/check.py",
    )


@pytest.mark.parametrize(
    "target_hint",
    [
        "self_reflection/example_check/other.py",
        "self_reflection/registry.yaml",
    ],
)
def test_validate_fix_target_hint_rejects_non_primary_reflection_paths(target_hint: str) -> None:
    with pytest.raises(RuntimeError, match="self_reflection/<reflection-id>/check.py"):
        _validate_fix_target_hint(
            "self_reflection",
            "reflection_check_bundle_v1",
            "create",
            target_hint,
        )


def test_validate_fix_target_hint_rejects_prompt_md_path() -> None:
    with pytest.raises(RuntimeError, match="primary check.py path"):
        _validate_fix_target_hint(
            "self_reflection",
            "reflection_check_bundle_v1",
            "create",
            "self_reflection/example_check/PROMPT.md",
        )


def test_validate_proposed_edits_accepts_reflection_with_auto_registry_sync(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    fix_plan = _reflection_fix_plan()
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    refinement = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "self_reflection", "summary": "add check"}],
        "file_edits": [
            {
                "relative_path": "self_reflection/example_check/check.py",
                "operation": "create",
                "new_content": _valid_reflection_check_py("example_check"),
            },
            {
                "relative_path": "self_reflection/example_check/PROMPT.md",
                "operation": "create",
                "new_content": "Revise the candidate in this same turn.",
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
    normalized = _normalize_refinement_file_contents(refinement, iteration=1)
    report = validate_proposed_edits(
        workspace,
        normalized,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills", "self_reflection"},
        raw_refinement=refinement,
    )
    assert report["ok"], report["errors"]
    assert "self_reflection/example_check/check.py" in report["checked_files"]
