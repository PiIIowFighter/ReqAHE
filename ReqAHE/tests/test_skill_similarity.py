from pathlib import Path

import pytest

from reqahe.refiner.pipeline import (
    SKILL_MARKDOWN_EXAMPLE,
    _map_fix_operation,
    _validate_fix_target_hint,
    build_edit_payload,
    build_write_policy,
    select_schemas_for_fix_plan,
)
from reqahe.refiner.skill_similarity import (
    build_existing_skill_catalog,
    find_similar_skills,
    load_relevant_skill_contents,
    skill_similarity_score,
)
from reqahe.refiner.validation import validate_fix_plan, validate_proposed_edits
from reqahe.diagnoser.pipeline import load_declared_components


def _workspace(tmp_path: Path, *, with_style_skill: bool = False) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "skills").mkdir()
    (workspace / "system_prompt.md").write_text(
        "# Role\nInterviewer.\n\n"
        "# Goal\nElicit requirements.\n\n"
        "# Interaction Rules\nUse harness components.\n\n"
        "# Output Format\nReturn JSON.\n\n"
        "# Safety Boundaries\nDo not reveal hidden data.\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n",
        encoding="utf-8",
    )
    if with_style_skill:
        skill_dir = workspace / "skills" / "style-elaboration"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_style_elaboration_skill_content(), encoding="utf-8")
    return workspace


def _style_elaboration_skill_content() -> str:
    return "\n".join(
        [
            "---",
            'id: "style-elaboration"',
            'name: "Style Elaboration"',
            "version: 1",
            "enabled: true",
            'intent: "Ask focused questions about visual style when style requirements are missing."',
            "scope:",
            '  - "Style-related requirement gaps"',
            "use_when:",
            '  - "Style preferences remain missing after content probing."',
            "avoid_when:",
            '  - "The user already provided concrete style preferences."',
            "risk_notes:",
            '  - "Overuse may narrow the interview to style."',
            "---",
            "# Skill",
            "Ask focused style questions.",
            "Ask one style question.",
        ]
    )


def _valid_skill_lines(skill_id: str) -> list[str]:
    name = skill_id.replace("-", " ").title()
    return [
        "---",
        f'id: "{skill_id}"',
        f'name: "{name}"',
        "version: 1",
        "enabled: true",
        f'intent: "{name} for requirements elicitation."',
        "scope:",
        '  - "Reusable interviewer questioning behavior"',
        "use_when:",
        '  - "a requirement area is underspecified"',
        "avoid_when:",
        '  - "the user has already answered this concern"',
        "risk_notes:",
        '  - "Overuse may narrow questioning."',
        "---",
        "# Skill",
        "Ask reusable follow-up questions.",
    ]


def _self_validation() -> dict:
    return {
        "writes_only_allowed_paths": True,
        "implements_only_approved_fix_plan": True,
        "no_dataset_or_evaluator_edit": True,
        "no_hidden_requirement_leakage": True,
        "no_scenario_specific_answer_memorization": True,
        "schemas_followed": True,
        "similarity_gate_applied": True,
        "no_duplicate_skill_created": True,
        "no_append_to_skill_markdown": True,
        "no_skill_readme_edit": True,
    }


def _fix_plan_create(skill_path: str) -> dict:
    return {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": skill_path,
                "evidence": ["RC1"],
                "fix_summary": "add skill",
                "expected_effect": "improve coverage",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }


def _fix_plan_replace(skill_path: str) -> dict:
    return {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "replace",
                "target_file_hint": skill_path,
                "evidence": ["RC1"],
                "fix_summary": "update skill",
                "expected_effect": "improve coverage",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }


def test_high_similarity_skill_create_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_create("skills/visual-preference-probing/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": "skills/visual-preference-probing/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("visual-preference-probing")) + "\n",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/visual-preference-probing/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "proposed_intent": "probe visual UI style preferences",
                "closest_existing_skill_id": "style-elaboration",
                "closest_existing_path": "skills/style-elaboration/SKILL.md",
                "similarity_score": 0.85,
                "matched_dimensions": ["problem_similarity", "canonical_family_similarity"],
                "decision": "create_new",
                "justification": "new skill for visual probing",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "bad",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("High-similarity skill creation is forbidden" in err for err in report["errors"])


def test_high_similarity_skill_replace_passes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    existing = (workspace / "skills" / "style-elaboration" / "SKILL.md").read_text(encoding="utf-8")
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_replace("skills/style-elaboration/SKILL.md")
    updated = existing.replace("Ask one style question.", "Ask one focused style question about layout or color.")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "replace"}],
        "file_edits": [
            {
                "relative_path": "skills/style-elaboration/SKILL.md",
                "operation": "replace",
                "old": existing,
                "new_content": updated,
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/style-elaboration/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "proposed_intent": "improve style elaboration probing",
                "closest_existing_skill_id": "style-elaboration",
                "closest_existing_path": "skills/style-elaboration/SKILL.md",
                "similarity_score": 0.95,
                "matched_dimensions": ["problem_similarity"],
                "decision": "replace_existing",
                "justification": "merge improved style probing into existing skill",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "replace",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert report["ok"]


def test_skill_markdown_append_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_replace("skills/style-elaboration/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "append"}],
        "file_edits": [
            {
                "relative_path": "skills/style-elaboration/SKILL.md",
                "operation": "append",
                "new_content": "# Extra\nMore text.",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/style-elaboration/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "proposed_intent": "append style guidance",
                "closest_existing_skill_id": "style-elaboration",
                "closest_existing_path": "skills/style-elaboration/SKILL.md",
                "similarity_score": 0.9,
                "matched_dimensions": ["procedure_similarity"],
                "decision": "update_existing",
                "justification": "append more style guidance",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "bad",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any(
        "SKILL.md must be updated with replace, not append" in err for err in report["errors"]
    )


def test_skills_readme_edit_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_create("skills/new-skill/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "skills/README.md",
                "operation": "replace",
                "old": "",
                "new_content": "bad",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/README.md"],
            }
        ],
        "similarity_audit": [],
        "refiner_rationale": "bad",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("skills/README.md must not be edited by the skill refiner" in err for err in report["errors"])


def test_create_new_missing_similarity_audit_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_create("skills/first-skill/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": "skills/first-skill/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("first-skill")) + "\n",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/first-skill/SKILL.md"],
            }
        ],
        "similarity_audit": [],
        "self_validation": _self_validation(),
        "refiner_rationale": "bad",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("similarity_audit must be non-empty" in err for err in report["errors"])


def test_medium_similarity_create_missing_justification_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_create("skills/ui-style-followup/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": "skills/ui-style-followup/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("ui-style-followup")) + "\n",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/ui-style-followup/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "proposed_intent": "follow up on UI style",
                "closest_existing_skill_id": "style-elaboration",
                "closest_existing_path": "skills/style-elaboration/SKILL.md",
                "similarity_score": 0.55,
                "matched_dimensions": ["trigger_similarity"],
                "decision": "create_new",
                "justification": "needs a separate skill",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "bad",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("Medium-similarity create_new requires justification" in err for err in report["errors"])


def test_build_existing_skill_catalog_parses_skills(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    second = workspace / "skills" / "content-detail-probing"
    second.mkdir(parents=True)
    (second / "SKILL.md").write_text(
        "\n".join(_valid_skill_lines("content-detail-probing")) + "\n",
        encoding="utf-8",
    )
    catalog = build_existing_skill_catalog(workspace)
    assert len(catalog) == 2
    by_id = {item["skill_id"]: item for item in catalog}
    assert set(by_id) == {"style-elaboration", "content-detail-probing"}
    for key in ("skill_id", "name", "description", "trigger", "expected_effect", "priority", "version", "path"):
        assert key in by_id["style-elaboration"]


def test_empty_workspace_first_skill_create_allowed_with_audit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = _fix_plan_create("skills/aspect-coverage/SKILL.md")
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": "skills/aspect-coverage/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("aspect-coverage")) + "\n",
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/aspect-coverage/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "proposed_intent": "systematic aspect coverage probing",
                "closest_existing_skill_id": None,
                "closest_existing_path": None,
                "similarity_score": 0.0,
                "matched_dimensions": [],
                "decision": "create_new",
                "justification": "no existing skill covers aspect coverage",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "first skill",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert report["ok"]


def test_edit_payload_includes_skill_catalog_and_candidates(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": "skills/visual-preference-probing/SKILL.md",
                "evidence": ["RC1"],
                "fix_summary": "probe visual UI style preferences",
                "expected_effect": "style coverage",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }
    payload = build_edit_payload(workspace, fix_plan, write_policy, validator_errors=[])
    assert "existing_skill_catalog" in payload
    assert len(payload["existing_skill_catalog"]) == 1
    assert payload["existing_skill_catalog"][0]["skill_id"] == "style-elaboration"
    assert "existing_skill_contents" in payload
    assert "similar_skill_candidates" in payload
    assert payload["similar_skill_candidates"]
    assert "artifact_index" in payload


def test_skill_similarity_uses_dynamic_skill_text() -> None:
    catalog = [
        {
            "skill_id": "focused-follow-up",
            "path": "skills/focused-follow-up/SKILL.md",
            "name": "Focused Follow-up",
            "description": "Ask about unresolved observable requirement details.",
            "intent": "Clarify an underspecified detail from the current dialogue.",
            "scope": ["Concrete requirement details left unclear."],
            "use_when": ["A single focused question can clarify the detail."],
            "avoid_when": ["The same detail has already been clarified."],
            "risk_notes": ["Overuse may make the interview repetitive."],
            "trigger": {"applies_when": ["detail unclear"], "avoid_when": []},
            "expected_effect": {"metrics": ["mean_IRE"], "description": "clearer details"},
            "body_excerpt": "Ask one concise follow-up question grounded in the conversation.",
            "priority": 70,
            "version": "1.0.0",
        }
    ]
    scored = find_similar_skills("clarify unresolved observable requirement detail", catalog)
    assert scored
    assert scored[0]["skill_id"] == "focused-follow-up"
    assert skill_similarity_score("focused question for unclear requirement detail", catalog[0]) >= 0.45


def test_skill_similarity_has_no_canonical_families_or_special_skill_ids() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "reqahe"
    similarity_source = (source_root / "refiner" / "skill_similarity.py").read_text(encoding="utf-8")
    production_source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))

    assert "CANONICAL_FAMILIES" not in similarity_source
    for token in (
        "noncommittal-pivot",
        "style-elaboration",
        "content-detail-probing",
        "interaction-flow-probing",
    ):
        assert token not in production_source


def test_skill_markdown_example_is_neutral() -> None:
    assert "focused-follow-up" in SKILL_MARKDOWN_EXAMPLE
    for token in (
        "style-elaboration",
        "noncommittal-pivot",
        "content-detail-probing",
        "interaction-flow-probing",
        "style",
        "content",
        "interaction",
        "benchmark",
        "hidden",
    ):
        assert token not in SKILL_MARKDOWN_EXAMPLE.lower()


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("create", "create"),
        ("update", "update"),
        ("replace", "replace"),
        ("demote", "demote"),
        ("disable", "disable"),
        ("remove", "remove"),
        ("validate", "validate"),
        ("unknown", "update"),
    ],
)
def test_map_fix_operation_preserves_supported_operations(operation: str, expected: str) -> None:
    assert _map_fix_operation(operation) == expected


def test_load_relevant_skill_contents_respects_max(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, with_style_skill=True)
    catalog = build_existing_skill_catalog(workspace)
    contents = load_relevant_skill_contents(workspace, catalog, max_skills=12)
    assert len(contents) == 1
    assert contents[0]["skill_id"] == "style-elaboration"
    assert "content" in contents[0]


def test_pipeline_allows_skill_replace() -> None:
    _validate_fix_target_hint(
        "skills",
        "skill_markdown_v1",
        "replace",
        "skills/style-elaboration/SKILL.md",
    )
    fix_plan = _fix_plan_replace("skills/style-elaboration/SKILL.md")
    validate_fix_plan(fix_plan, {"system_prompt", "skills"})


def test_pipeline_rejects_skill_append() -> None:
    with pytest.raises(RuntimeError, match="skills must use create or replace, not append"):
        _validate_fix_target_hint(
            "skills",
            "skill_markdown_v1",
            "append",
            "skills/style-elaboration/SKILL.md",
        )


@pytest.mark.parametrize(
    "target_hint",
    [
        "skills/README.md",
        "skills/style-elaboration.md",
        "skills/style-elaboration/OTHER.md",
        "../skills/style-elaboration/SKILL.md",
    ],
)
def test_pipeline_rejects_invalid_skill_paths(target_hint: str) -> None:
    with pytest.raises(RuntimeError, match="skills must target skills/<skill-id>/SKILL.md"):
        _validate_fix_target_hint(
            "skills",
            "skill_markdown_v1",
            "create",
            target_hint,
        )


def test_max_two_skill_creates_per_batch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": "skills/a/SKILL.md",
                "evidence": ["RC1"],
                "fix_summary": "add skills",
                "expected_effect": "coverage",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": f"skills/{skill_id}/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines(skill_id)) + "\n",
            }
            for skill_id in ("a", "b", "c")
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": [f"skills/{skill_id}/SKILL.md" for skill_id in ("a", "b", "c")],
            }
        ],
        "similarity_audit": [
            {
                "target_path": f"skills/{skill_id}/SKILL.md",
                "proposed_intent": f"add {skill_id} skill",
                "closest_existing_skill_id": None,
                "closest_existing_path": None,
                "similarity_score": 0.0,
                "matched_dimensions": [],
                "decision": "create_new",
                "justification": "distinct operational problem",
            }
            for skill_id in ("a", "b", "c")
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "too many creates",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("At most 2 new skills may be created in one batch" in err for err in report["errors"])


def test_skill_edits_cannot_share_single_audit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    declared = load_declared_components(workspace)
    write_policy = build_write_policy(workspace, declared)
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": "skills/a/SKILL.md",
                "evidence": ["RC1"],
                "fix_summary": "add skills",
                "expected_effect": "coverage",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "create"}],
        "file_edits": [
            {
                "relative_path": "skills/a/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("a")) + "\n",
            },
            {
                "relative_path": "skills/b/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("b")) + "\n",
            },
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/a/SKILL.md", "skills/b/SKILL.md"],
            }
        ],
        "similarity_audit": [
            {
                "target_path": "skills/a/SKILL.md",
                "proposed_intent": "add a skill",
                "closest_existing_skill_id": None,
                "closest_existing_path": None,
                "similarity_score": 0.0,
                "matched_dimensions": [],
                "decision": "create_new",
                "justification": "distinct operational problem for a",
            }
        ],
        "self_validation": _self_validation(),
        "refiner_rationale": "shared audit",
    }
    report = validate_proposed_edits(
        workspace,
        data,
        fix_plan,
        write_policy,
        select_schemas_for_fix_plan(fix_plan),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any(
        "no similarity_audit entry explains skill file_edit for skills/b/SKILL.md" in err
        for err in report["errors"]
    )
