from pathlib import Path

import pytest

from reqahe.diagnoser.pipeline import (
    _validate_analysis,
    build_component_localization_payload,
    build_full_trace_problem_payload,
    load_declared_components,
    sanitize_trace_for_diagnoser,
)
from reqahe.refiner import refine_harness
from reqahe.refiner.pipeline import (
    _commit_workspace,
    _plan_edit,
    build_edit_payload,
    build_fix_plan_payload,
    select_schemas_for_fix_plan,
    validate_and_plan_refinement,
    validate_modified_workspace_preview,
)
from reqahe.refiner.validation import _validate_refinement, validate_proposed_edits
from reqahe.harness.component_schema import ALLOWED_ARTIFACT_TYPES, validate_workspace_component_schemas
from reqahe.infra.io import read_json, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _skill_create_similarity_audit(proposed_intent: str = "add skill") -> list[dict]:
    return [
        {
            "proposed_intent": proposed_intent,
            "closest_existing_skill_id": None,
            "closest_existing_path": None,
            "similarity_score": 0.0,
            "matched_dimensions": [],
            "decision": "create_new",
            "justification": "no existing skill in workspace",
        }
    ]


def _skill_self_validation() -> dict:
    return {
        "similarity_gate_applied": True,
        "no_duplicate_skill_created": True,
        "no_append_to_skill_markdown": True,
        "no_skill_readme_edit": True,
    }


def test_prompt_files_are_externalized_and_limited() -> None:
    diagnoser_prompts = sorted((PROJECT_ROOT / "src" / "reqahe" / "diagnoser" / "prompts").glob("*.md"))
    refiner_prompts = sorted((PROJECT_ROOT / "src" / "reqahe" / "refiner" / "prompts").glob("*.md"))

    assert [path.name for path in diagnoser_prompts] == [
        "analyze_trace.md",
        "localize_component.md",
    ]
    assert [path.name for path in refiner_prompts] == [
        "generate_edits_and_validate.md",
        "make_fix_plan.md",
    ]
    assert len(diagnoser_prompts) + len(refiner_prompts) == 4


def test_diagnoser_refiner_prompts_use_allowed_artifact_types_only() -> None:
    refiner_prompts = list((PROJECT_ROOT / "src" / "reqahe" / "refiner" / "prompts").glob("*.md"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in refiner_prompts)
    assert "skill_markdown_v1" not in combined or "SKILL.md" in combined
    assert "Required Minimal Skill Front Matter" in combined


def test_refiner_pipeline_has_expected_refinement_stages() -> None:
    refiner_source = (PROJECT_ROOT / "src" / "reqahe" / "refiner" / "pipeline.py").read_text(encoding="utf-8")
    assert "validate_and_plan_refinement" in refiner_source
    assert "build_fix_plan_payload" in refiner_source
    assert "build_edit_payload" in refiner_source


def test_prompt_files_have_expected_stage_contracts() -> None:
    analyze = (PROJECT_ROOT / "src" / "reqahe" / "diagnoser" / "prompts" / "analyze_trace.md").read_text(
        encoding="utf-8"
    )
    localize = (PROJECT_ROOT / "src" / "reqahe" / "diagnoser" / "prompts" / "localize_component.md").read_text(
        encoding="utf-8"
    )
    make_plan = (PROJECT_ROOT / "src" / "reqahe" / "refiner" / "prompts" / "make_fix_plan.md").read_text(encoding="utf-8")
    generate = (PROJECT_ROOT / "src" / "reqahe" / "refiner" / "prompts" / "generate_edits_and_validate.md").read_text(
        encoding="utf-8"
    )

    assert "complete_task_traces" in analyze or "rollout metrics" in analyze
    assert "Do not recommend concrete file edits" in analyze
    assert "diagnosis" in localize or "component_findings" in localize
    assert '"changes"' in make_plan
    assert '"file_edits":' not in make_plan
    assert '"edits"' in generate


def test_python_orchestration_does_not_embed_large_prompt_contracts() -> None:
    diagnoser_source = (PROJECT_ROOT / "src" / "reqahe" / "diagnoser" / "pipeline.py").read_text(encoding="utf-8")
    refiner_source = (PROJECT_ROOT / "src" / "reqahe" / "refiner" / "pipeline.py").read_text(encoding="utf-8")

    assert "You are an Elicitation Trace Diagnoser" not in diagnoser_source
    assert "You are a Harness Refiner Editor" not in refiner_source
    assert "Return strict JSON only" not in diagnoser_source
    assert "Return strict JSON only" not in refiner_source


def test_refiner_prompts_describe_skill_schema_requirements() -> None:
    prompt_paths = list((PROJECT_ROOT / "src" / "reqahe" / "refiner" / "prompts").glob("*.md"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in prompt_paths)
    assert "use_when" in combined
    assert "avoid_when" in combined
    assert "risk_notes" in combined


def test_diagnoser_stage1_payload_includes_complete_task_traces(tmp_path: Path) -> None:
    rollout = _sample_rollout(tmp_path)
    payload = build_full_trace_problem_payload(rollout, {"mean_IRE": 0.2}, {"mean_IRE": 0.1})
    assert "complete_task_traces" in payload
    assert payload["complete_task_traces"]
    assert payload["complete_task_traces"][0]["scenario_id"] == "train_001"
    assert "turns" in payload["complete_task_traces"][0]


def test_diagnoser_stage2_payload_excludes_complete_task_traces() -> None:
    trace_analysis = {"diagnosis_summary": "bad generic questions", "failure_findings": []}
    payload = build_component_localization_payload(trace_analysis, [{"name": "skills", "purpose": "skills"}])
    assert "diagnosis" in payload
    assert "declared_components" in payload
    assert "complete_task_traces" not in payload
    assert "turns" not in str(payload)


def test_refiner_fix_plan_payload_excludes_trace_and_workspace_contents(tmp_path: Path) -> None:
    iteration = tmp_path / "iteration"
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    localization = {"localization_summary": "overview", "component_findings": [], "refiner_guidance": {}}
    write_policy = {"max_fixes": 3, "allowed_components": ["skills"], "path_patterns": {"skills": "skills/<skill-id>/SKILL.md"}}
    payload = build_fix_plan_payload(iteration, workspace, localization, write_policy)
    serialized = str(payload)
    assert "complete_task_traces" not in serialized
    assert "turns" not in serialized
    assert "workspace_files" not in payload
    assert "artifact_index" in payload
    assert "existing_skill_catalog" in payload
    assert "write_policy" in payload


def test_refiner_edit_payload_only_includes_target_file_contents(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md", "memory": "memory/README.md"})
    (workspace / "system_prompt.md").write_text("# Role\nExisting prompt\n", encoding="utf-8")
    fix_plan = _valid_fix_plan(target_file_hint="system_prompt.md")
    fix_plan["fix_plan"][0]["operation_intent"] = "replace"
    fix_plan["fix_plan"][0]["component"] = "system_prompt"
    fix_plan["fix_plan"][0]["artifact_type"] = "system_prompt_section_v1"
    write_policy = {"max_fixes": 3, "allowed_components": ["system_prompt", "skills"], "path_patterns": {}}
    payload = build_edit_payload(workspace, fix_plan, write_policy, validator_errors=[])
    assert "target_file_context" in payload
    assert "system_prompt.md" in payload["target_file_context"]
    assert "memory/README.md" not in payload["target_file_context"]
    assert "existing_skill_catalog" in payload
    assert "existing_skill_contents" in payload
    assert "similar_skill_candidates" in payload
    assert "complete_task_traces" not in str(payload)


def test_selected_schemas_only_include_fix_plan_schemas() -> None:
    fix_plan = _valid_fix_plan()
    selected = select_schemas_for_fix_plan(fix_plan)
    assert set(selected.keys()) == {"skill_markdown_v1"}
    assert "memory_lesson_v1" not in selected


def _valid_analysis(component: str = "skills") -> dict:
    return {
        "localization_summary": "overview",
        "component_findings": [
            {
                "component": component,
                "issue": "missing focused content probing",
                "evidence": [{"source": "trace", "detail": "trace evidence"}],
                "recommended_refinement_direction": "create",
                "target_existing_items": [],
                "why_not_create_only": "no existing skill covers the gap",
                "confidence": "high",
            }
        ],
        "refiner_guidance": {
            "preferred_actions": ["create a focused skill"],
            "actions_to_avoid": ["duplicate existing skills"],
            "required_evidence_to_use": ["route_stats"],
        },
    }


def test_diagnoser_rejects_memory_component() -> None:
    data = _valid_analysis("memory")

    with pytest.raises(RuntimeError, match="not an evolvable component"):
        _validate_analysis(data, {"system_prompt", "skills", "memory", "self_reflection"})


def test_diagnoser_accepts_extra_component_declared_by_seed() -> None:
    data = _valid_analysis("extra_guidance")
    data["component_findings"][0]["component"] = "extra_guidance"

    _validate_analysis(data, {"system_prompt", "extra_guidance"})


def test_validator_rejects_readme_append_for_skills(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    write_policy = {"max_fixes": 3, "allowed_components": ["skills"], "path_patterns": {"skills": "skills/<skill-id>/SKILL.md"}}
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "skills/README.md",
                "operation": "append",
                "new_content": "skill content",
            }
        ],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/README.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    report = validate_proposed_edits(
        workspace,
        data,
        _valid_fix_plan(),
        write_policy,
        select_schemas_for_fix_plan(_valid_fix_plan()),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("README" in err for err in report["errors"])


def test_refiner_rejects_readme_append_for_skills(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "skills", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "skills/README.md",
                "operation": "append",
                "new_content": "skill content",
            }
        ],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/README.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }

    with pytest.raises(RuntimeError, match="README.md"):
        _validate_refinement(data, {"system_prompt", "skills"}, workspace)


def test_validator_rejects_dataset_path(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    write_policy = {"max_fixes": 3, "allowed_components": ["skills"], "path_patterns": {"skills": "skills/<skill-id>/SKILL.md"}}
    data = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "bad"}],
        "file_edits": [{"relative_path": "dataset/hidden.json", "operation": "create", "new_content": "{}"}],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["dataset/hidden.json"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    report = validate_proposed_edits(
        workspace,
        data,
        _valid_fix_plan(),
        write_policy,
        select_schemas_for_fix_plan(_valid_fix_plan()),
        {"system_prompt", "skills"},
    )
    assert not report["ok"]
    assert any("forbidden" in err.lower() for err in report["errors"])


def test_refiner_rejects_flat_skill_path(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "skills", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "skills/question_strategy.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("question_strategy")),
            }
        ],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/question_strategy.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }

    with pytest.raises(RuntimeError, match="skills/<skill-name>/SKILL.md"):
        _validate_refinement(data, {"system_prompt", "skills"}, workspace)


def test_refiner_rejects_components_not_declared_by_seed(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "not_declared_component", "summary": "bad"}],
        "file_edits": [{"relative_path": "skills/x.md", "operation": "create", "lines": ["ok"]}],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/x.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }

    with pytest.raises(RuntimeError, match="component is not declared by current harness seed"):
        _validate_refinement(data, {"system_prompt", "skills"}, workspace)


def test_refiner_rejects_paths_not_declared_by_seed(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "skills", "summary": "bad"}],
        "file_edits": [{"relative_path": "unknown_dir/file.md", "operation": "create", "lines": ["bad"]}],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["unknown_dir/file.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    with pytest.raises(RuntimeError, match="skills must be written as skills/<skill-name>/SKILL.md"):
        _validate_refinement(data, {"system_prompt", "skills"}, workspace)


def test_refiner_accepts_extra_component_declared_by_seed(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"extra_guidance": "extra_guidance/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "extra_guidance", "summary": "add guidance"}],
        "file_edits": [{"relative_path": "extra_guidance/new.md", "operation": "create", "lines": ["Check one item."]}],
        "schema_compliance": {
            "component": "extra_guidance",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["extra_guidance/new.md"],
        },
        "refiner_rationale": "declared component",
        "similarity_audit": [],
    }

    _validate_refinement(data, {"system_prompt", "extra_guidance"}, workspace)


def test_refiner_applies_edits_writes_expected_outputs(tmp_path: Path, monkeypatch) -> None:
    iteration = tmp_path / "iteration_001"
    workspace = iteration / "workspace"
    analysis = iteration / "analysis"
    rollout = iteration / "rollout"
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
        {
            "localization_summary": "overview",
            "component_findings": [],
            "refiner_guidance": {},
        },
    )
    write_json(rollout / "task_results.json", [])

    class FakeLLM:
        def __init__(self) -> None:
            self.purposes: list[str] = []

        def json_chat(self, *args, **kwargs) -> dict:
            self.purposes.append(kwargs["purpose"])
            if len(self.purposes) == 1:
                return _valid_fix_plan()
            return {
                "changes": [
                    {
                        "change_id": "C1",
                        "fix_id": "F1",
                        "component": "skills",
                        "summary": "add focused probing strategy",
                        "evidence": ["RC1"],
                        "expected_effect": "improve content coverage",
                        "risk": "low",
                    }
                ],
                "file_edits": [
                    {
                        "relative_path": "skills/question-strategy/SKILL.md",
                        "operation": "create",
                        "section_title": "Focused probing strategy",
                        "new_content": "\n".join(_valid_skill_lines("question-strategy")),
                    }
                ],
                "schema_compliance": [
                    {
                        "component": "skills",
                        "schema_name": "skill_markdown_v1",
                        "new_or_updated_files": ["skills/question-strategy/SKILL.md"],
                    }
                ],
                "refiner_rationale": "Use a reusable strategy, not a scenario answer.",
                "similarity_audit": _skill_create_similarity_audit("add focused probing strategy"),
                "self_validation": _skill_self_validation(),
            }

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)

    llm = FakeLLM()
    refine_harness(iteration, workspace, 1, llm, "m")  # type: ignore[arg-type]

    assert (workspace / "skills" / "question-strategy" / "SKILL.md").exists()
    assert (iteration / "refiner" / "fix_plan.json").exists()
    assert (iteration / "refiner" / "proposed_edits.json").exists()
    assert (iteration / "refiner" / "proposed_edits.normalized.json").exists()
    assert (iteration / "refiner" / "refiner_stats.json").exists()
    assert (iteration / "refiner" / "validation_report.json").exists()
    assert (iteration / "refiner" / "skill_similarity_audit.json").exists()
    assert (iteration / "refiner.log").exists()
    assert (iteration / "refiner_rationale.md").read_text(encoding="utf-8").strip() == (
        "Use a reusable strategy, not a scenario answer."
    )
    assert llm.purposes == ["harness fix plan selection", "harness file edit generation"]


def test_refiner_validation_failure_repairs_with_retry_and_does_not_write_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    iteration = tmp_path / "iteration_001"
    workspace = iteration / "workspace"
    analysis = iteration / "analysis"
    rollout = iteration / "rollout"
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
        {
            "localization_summary": "overview",
            "component_findings": [],
            "refiner_guidance": {},
        },
    )
    write_json(rollout / "task_results.json", [])

    class FakeLLM:
        def __init__(self) -> None:
            self.purposes: list[str] = []

        def json_chat(self, *args, **kwargs) -> dict:
            self.purposes.append(kwargs["purpose"])
            if len(self.purposes) == 1:
                return _valid_fix_plan(target_file_hint="skills/bad-skill/SKILL.md")
            return {
                "changes": [
                    {
                        "change_id": "C1",
                        "fix_id": "F1",
                        "component": "skills",
                        "summary": "invalid skill",
                        "evidence": ["RC1"],
                        "expected_effect": "improve content coverage",
                        "risk": "low",
                    }
                ],
                "file_edits": [
                    {
                        "relative_path": "skills/bad-skill/SKILL.md",
                        "operation": "create",
                        "section_title": "Bad",
                        "new_content": "not structured",
                    }
                ],
                "schema_compliance": [
                    {
                        "component": "skills",
                        "schema_name": "skill_markdown_v1",
                        "new_or_updated_files": ["skills/bad-skill/SKILL.md"],
                    }
                ],
                "refiner_rationale": "invalid",
                "similarity_audit": _skill_create_similarity_audit("invalid skill"),
            }

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)
    llm = FakeLLM()

    with pytest.raises(RuntimeError, match="validation failed"):
        refine_harness(iteration, workspace, 1, llm, "m")  # type: ignore[arg-type]

    assert llm.purposes == [
        "harness fix plan selection",
        "harness file edit generation",
        "harness file edit generation",
    ]
    assert not (workspace / "skills" / "bad-skill" / "SKILL.md").exists()
    assert (iteration / "refiner_error.json").exists()
    assert (iteration / "refiner_error.md").exists()


def test_refiner_stats_preserves_validator_errors_on_failure(tmp_path: Path, monkeypatch) -> None:
    iteration = tmp_path / "iteration_001"
    workspace = iteration / "workspace"
    analysis = iteration / "analysis"
    rollout = iteration / "rollout"
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
        {
            "localization_summary": "overview",
            "component_findings": [],
            "refiner_guidance": {},
        },
    )
    write_json(rollout / "task_results.json", [])

    class FakeLLM:
        def __init__(self) -> None:
            self.purposes: list[str] = []

        def json_chat(self, *args, **kwargs) -> dict:
            self.purposes.append(kwargs["purpose"])
            if len(self.purposes) == 1:
                return _valid_fix_plan(target_file_hint="skills/bad-skill/SKILL.md")
            return {
                "changes": [
                    {
                        "change_id": "C1",
                        "fix_id": "F1",
                        "component": "skills",
                        "summary": "invalid skill",
                        "evidence": ["RC1"],
                        "expected_effect": "improve content coverage",
                        "risk": "low",
                    }
                ],
                "file_edits": [
                    {
                        "relative_path": "skills/bad-skill/SKILL.md",
                        "operation": "create",
                        "section_title": "Bad",
                        "new_content": "not structured",
                    }
                ],
                "schema_compliance": [
                    {
                        "component": "skills",
                        "schema_name": "skill_markdown_v1",
                        "new_or_updated_files": ["skills/bad-skill/SKILL.md"],
                    }
                ],
                "refiner_rationale": "invalid",
                "similarity_audit": _skill_create_similarity_audit("invalid skill"),
            }

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)
    llm = FakeLLM()

    with pytest.raises(RuntimeError, match="validation failed"):
        refine_harness(iteration, workspace, 1, llm, "m")  # type: ignore[arg-type]

    validation_report = read_json(iteration / "refiner" / "validation_report.json")
    refiner_stats = read_json(iteration / "refiner" / "refiner_stats.json")
    assert validation_report["ok"] is False
    assert validation_report["errors"]
    assert refiner_stats["validator_error_count"] == len(validation_report["errors"])
    assert refiner_stats["validator_errors"] == validation_report["errors"]
    assert refiner_stats["repair_attempted"] is True


def test_refiner_rejects_invalid_skill_schema(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "skills", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "skills/bad-skill/SKILL.md",
                "operation": "create",
                "lines": ["not structured"],
            }
        ],
        "schema_compliance": {
            "component": "skills",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["skills/bad-skill/SKILL.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }
    planned = dict(_plan_edit(workspace, edit) for edit in data["file_edits"])

    with pytest.raises(RuntimeError, match="frontmatter"):
        validate_workspace_component_schemas(workspace, planned)


def test_refiner_rejects_memory_edits(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"memory": "memory/README.md"})
    data = {
        "changes": [{"change_id": "C1", "component": "memory", "summary": "bad"}],
        "file_edits": [
            {
                "relative_path": "memory/stock_report_website/MEMORY.md",
                "operation": "create",
                "new_content": "# Stock\n\n## Recorded Hit Points\n- point\n",
            }
        ],
        "schema_compliance": {
            "component": "memory",
            "schema_name": "skill_markdown_v1",
            "new_or_updated_files": ["memory/stock_report_website/MEMORY.md"],
        },
        "refiner_rationale": "bad",
        "similarity_audit": [],
    }

    with pytest.raises(RuntimeError, match="Refiner is not allowed to edit memory"):
        _validate_refinement(data, {"system_prompt", "skills", "memory"}, workspace)


def test_refiner_rejects_unregistered_self_reflection_check(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    planned = {
        "self_reflection/generated_check/check.py": _valid_reflection_check_py("generated_check"),
        "self_reflection/generated_check/PROMPT.md": "Revise the candidate.",
    }

    with pytest.raises(RuntimeError, match="must be registered"):
        validate_workspace_component_schemas(workspace, planned)


def test_refiner_rejects_registry_missing_check_file(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    planned = {
        "self_reflection/registry.yaml": (
            'version: "0.2"\n'
            "checks:\n"
            "  - id: missing_check\n"
            "    hook: question_candidate\n"
            "    file: missing_check/check.py\n"
            "    prompt: missing_check/PROMPT.md\n"
            "    applies_when: always\n"
            "    mode: warn\n"
            "    priority: 10\n"
        )
    }

    with pytest.raises(RuntimeError, match="does not exist"):
        validate_workspace_component_schemas(workspace, planned)


def test_refiner_auto_registers_new_reflection_bundle(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    refinement = {
        "changes": [{"change_id": "C1", "component": "self_reflection", "summary": "add check"}],
        "file_edits": [
            {
                "relative_path": "self_reflection/generated_check/check.py",
                "operation": "create",
                "new_content": _valid_reflection_check_py("generated_check"),
            },
            {
                "relative_path": "self_reflection/generated_check/PROMPT.md",
                "operation": "create",
                "new_content": "Revise the candidate in this same turn.",
            },
        ],
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": [
                    "self_reflection/generated_check/check.py",
                    "self_reflection/generated_check/PROMPT.md",
                ],
            }
        ],
        "refiner_rationale": "add check",
        "similarity_audit": [],
    }

    planned = validate_and_plan_refinement(
        workspace,
        refinement,
        {"system_prompt", "self_reflection"},
    )
    planned_map = dict(planned)

    assert "self_reflection/generated_check/check.py" in planned_map
    assert "self_reflection/registry.yaml" in planned_map
    assert "generated_check" in planned_map["self_reflection/registry.yaml"]
    assert "generated_check/PROMPT.md" in planned_map["self_reflection/registry.yaml"]
    validate_workspace_component_schemas(workspace, planned_map)


def test_refiner_normalizes_string_trigger_lists(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    bad_skill = "\n".join(_valid_skill_lines("style-probing"))
    bad_skill = bad_skill.replace(
        '  applies_when:\n    - "a requirement area is underspecified"',
        "  applies_when: a requirement area is underspecified",
    )
    refinement = {
        "changes": [{"change_id": "C1", "component": "skills", "summary": "add skill"}],
        "file_edits": [
            {
                "relative_path": "skills/style-probing/SKILL.md",
                "operation": "create",
                "new_content": bad_skill,
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/style-probing/SKILL.md"],
            }
        ],
        "refiner_rationale": "add skill",
        "similarity_audit": _skill_create_similarity_audit("add style probing skill"),
        "self_validation": _skill_self_validation(),
    }

    planned = validate_and_plan_refinement(
        workspace,
        refinement,
        {"system_prompt", "skills"},
    )

    validate_workspace_component_schemas(workspace, dict(planned))


def _install_old_check_bundle(workspace: Path) -> None:
    bundle = workspace / "self_reflection" / "old_check"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "check.py").write_text(_valid_reflection_check_py("old_check"), encoding="utf-8")
    (bundle / "PROMPT.md").write_text("Revise the old check candidate.", encoding="utf-8")
    (workspace / "self_reflection" / "registry.yaml").write_text(
        'version: "0.2"\n'
        "checks:\n"
        "  - id: old_check\n"
        "    hook: question_candidate\n"
        "    file: old_check/check.py\n"
        "    prompt: old_check/PROMPT.md\n"
        "    applies_when: always\n"
        "    mode: warn\n"
        "    priority: 10\n",
        encoding="utf-8",
    )


def _reflection_refinement_payload(*file_edits: dict) -> dict:
    edited_paths = [str(edit["relative_path"]) for edit in file_edits]
    return {
        "changes": [{"change_id": "C1", "component": "self_reflection", "summary": "update reflection"}],
        "file_edits": list(file_edits),
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": edited_paths,
            }
        ],
        "refiner_rationale": "reflection edit",
        "similarity_audit": [],
    }


def test_preview_validation_accepts_new_bundle_when_old_bundle_exists(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    _install_old_check_bundle(workspace)
    refinement = _reflection_refinement_payload(
        {
            "relative_path": "self_reflection/new_check/check.py",
            "operation": "create",
            "new_content": _valid_reflection_check_py("new_check"),
        },
        {
            "relative_path": "self_reflection/new_check/PROMPT.md",
            "operation": "create",
            "new_content": "Revise the new check candidate.",
        },
    )

    planned = validate_and_plan_refinement(workspace, refinement, {"system_prompt", "self_reflection"})
    validate_modified_workspace_preview(workspace, planned)

    write_policy = {
        "max_fixes": 3,
        "allowed_components": ["self_reflection"],
        "path_patterns": {"self_reflection": "self_reflection/<reflection-id>/check.py + self_reflection/<reflection-id>/PROMPT.md"},
    }
    report = validate_proposed_edits(
        workspace,
        refinement,
        _reflection_fix_plan("self_reflection/new_check/check.py"),
        write_policy,
        select_schemas_for_fix_plan(_reflection_fix_plan("self_reflection/new_check/check.py")),
        {"system_prompt", "self_reflection"},
    )
    assert report["ok"], report["errors"]
    assert not any("old_check/check.py" in err for err in report["errors"])


def test_preview_validation_accepts_check_only_update_when_prompt_exists(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    _install_old_check_bundle(workspace)
    updated_check = _valid_reflection_check_py("old_check").replace(
        "    return []\n",
        "    # updated logic\n    return []\n",
    )
    planned_edits = [("self_reflection/old_check/check.py", updated_check)]

    validate_modified_workspace_preview(workspace, planned_edits)


def test_preview_validation_rejects_new_bundle_missing_prompt(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    planned_edits = [("self_reflection/new_check/check.py", _valid_reflection_check_py("new_check"))]

    with pytest.raises(RuntimeError, match="PROMPT.md"):
        validate_modified_workspace_preview(workspace, planned_edits)


def test_preview_validation_rejects_new_bundle_missing_check_py(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    planned_edits = [("self_reflection/new_check/PROMPT.md", "Revise the candidate output.")]

    with pytest.raises(RuntimeError, match="new_check.*check.py|check.py.*new_check"):
        validate_modified_workspace_preview(workspace, planned_edits)


def test_preview_validation_accepts_prompt_only_update_when_check_exists(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    _install_old_check_bundle(workspace)
    planned_edits = [("self_reflection/old_check/PROMPT.md", "Revise the updated candidate output.")]

    validate_modified_workspace_preview(workspace, planned_edits)


def test_preview_validation_accepts_new_bundle_with_check_and_prompt(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    refinement = _reflection_refinement_payload(
        {
            "relative_path": "self_reflection/new_check/check.py",
            "operation": "create",
            "new_content": _valid_reflection_check_py("new_check"),
        },
        {
            "relative_path": "self_reflection/new_check/PROMPT.md",
            "operation": "create",
            "new_content": "Revise the candidate output.",
        },
    )

    planned = validate_and_plan_refinement(workspace, refinement, {"system_prompt", "self_reflection"})
    validate_modified_workspace_preview(workspace, planned)


def test_refiner_rejects_too_many_mixed_file_edits(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(
        tmp_path,
        {
            "skills": "skills/README.md",
            "self_reflection": "self_reflection/README.md",
        },
    )
    refinement = {
        "changes": [
            {"change_id": "C1", "component": "skills", "summary": "skill"},
            {"change_id": "C2", "component": "self_reflection", "summary": "check"},
        ],
        "file_edits": [
            {
                "relative_path": "skills/a-skill/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines("a-skill")),
            },
            {
                "relative_path": "self_reflection/generated_check/check.py",
                "operation": "create",
                "new_content": _valid_reflection_check_py("generated_check"),
            },
            {
                "relative_path": "self_reflection/generated_check/PROMPT.md",
                "operation": "create",
                "new_content": "Revise the candidate in this same turn.",
            },
            {
                "relative_path": "self_reflection/registry.yaml",
                "operation": "create",
                "new_content": "version: 0.1\nchecks: []\n",
            },
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": ["skills/a-skill/SKILL.md"],
            },
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": [
                    "self_reflection/generated_check/check.py",
                    "self_reflection/generated_check/PROMPT.md",
                ],
            },
        ],
        "refiner_rationale": "multi edit",
        "similarity_audit": [
            {
                "proposed_intent": "skills/a-skill/SKILL.md",
                "closest_existing_skill_id": None,
                "closest_existing_path": None,
                "similarity_score": 0.0,
                "matched_dimensions": [],
                "decision": "create_new",
                "justification": "first focused skill",
            },
        ],
        "self_validation": _skill_self_validation(),
    }

    with pytest.raises(RuntimeError, match="at most 3 edits"):
        validate_and_plan_refinement(
            workspace,
            refinement,
            {"system_prompt", "skills", "self_reflection"},
        )


def test_sanitize_trace_includes_reflection_retry_metadata() -> None:
    trace = sanitize_trace_for_diagnoser(
        {
            "scenario_id": "train_001",
            "initial_req": "build app",
            "turns": [
                {
                    "turn_index": 0,
                    "action": "ask_question",
                    "question": "What data?",
                    "user_response": "Revenue.",
                    "self_reflection_events": [
                        {
                            "check_id": "bad_action_check",
                            "message": "bad action detected",
                            "reflection_attempt": 0,
                            "discarded_action": True,
                        }
                    ],
                    "reflection_attempts": [{"attempt": 0, "discarded": True}],
                    "accepted_despite_reflection_warning": False,
                    "judgement": {"is_relevant_to_implied_requirements": True},
                }
            ],
            "elicited_requirement_ids": [],
            "missed_requirement_ids": ["IR1"],
        }
    )
    assert trace["turns"][0]["reflection_attempts"] == [{"attempt": 0, "discarded": True}]
    assert trace["turns"][0]["accepted_despite_reflection_warning"] is False
    assert trace["turns"][0]["self_reflection_events"][0]["discarded_action"] is True


def test_sanitize_trace_strips_sensitive_keys() -> None:
    trace = sanitize_trace_for_diagnoser(
        {
            "scenario_id": "train_001",
            "initial_req": "build app",
            "hidden_requirements": [{"id": "IR1", "text": "secret answer"}],
            "turns": [],
            "elicited_requirement_ids": [],
            "missed_requirement_ids": ["IR1"],
        }
    )
    assert "hidden_requirements" not in trace
    assert trace["initial_requirement"] == "build app"


def test_declared_components_loaded_from_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md", "memory": "memory/README.md"})
    components = load_declared_components(workspace)
    names = {item["name"] for item in components}
    assert names == {"system_prompt", "skills"}

    debug_components = load_declared_components(workspace, include_non_evolvable=True)
    debug_names = {item["name"] for item in debug_components}
    assert debug_names == {"system_prompt", "skills", "memory"}


def test_schema_validation_ignores_git_binary_files(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    git_object = workspace / ".git" / "objects" / "ab" / "binary"
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"\xed\x00\xff")

    validate_workspace_component_schemas(workspace)


def test_valid_skill_and_reflection_python_schemas_pass(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(
        tmp_path,
        {
            "skills": "skills/README.md",
            "memory": "memory/README.md",
            "self_reflection": "self_reflection/README.md",
        },
    )
    planned = {
        "skills/focused-slot-probing/SKILL.md": "\n".join(_valid_skill_lines("focused-slot-probing")) + "\n",
        "memory/stock_report_website/MEMORY.md": _valid_memory_hit_content(),
        "self_reflection/registry.yaml": (
            'version: "0.2"\n'
            "checks:\n"
            "  - id: generated_check\n"
            "    hook: question_candidate\n"
            "    file: generated_check/check.py\n"
            "    prompt: generated_check/PROMPT.md\n"
            "    applies_when: always\n"
            "    mode: warn\n"
            "    priority: 20\n"
        ),
        "self_reflection/generated_check/check.py": _valid_reflection_check_py("generated_check"),
        "self_reflection/generated_check/PROMPT.md": "Revise the candidate in this same turn.",
    }

    validate_workspace_component_schemas(workspace, planned)


def _sample_rollout(tmp_path: Path) -> Path:
    rollout = tmp_path / "rollout"
    trace_dir = rollout / "train_001__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "scenario_id": "train_001",
            "app_type": "Web App",
            "initial_req": "Build something",
            "turns": [
                {
                    "turn_index": 0,
                    "action": "ask_question",
                    "question": "What domain?",
                    "user_response": "Any",
                    "judgement": {"elicited_requirement_ids": []},
                }
            ],
            "final_metrics": {"IRE": 0.1},
            "elicited_requirement_ids": [],
            "missed_requirement_ids": ["IR1"],
            "missed_requirement_aspects": {"style": 1},
        },
    )
    write_json(
        rollout / "task_results.json",
        [{"scenario_id": "train_001", "trace_dir": str(trace_dir), "metrics": {"IRE": 0.1}}],
    )
    write_json(rollout / "metrics.json", {"mean_IRE": 0.1})
    return rollout


def _workspace_with_manifest(tmp_path: Path, components: dict[str, str]) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "system_prompt.md").write_text(_valid_system_prompt(), encoding="utf-8")
    lines = ["name: test", "system_prompt: system_prompt.md"]
    for name, path in components.items():
        folder = Path(path).parent
        (workspace / folder).mkdir(parents=True, exist_ok=True)
        (workspace / path).write_text("", encoding="utf-8")
        lines.extend([f"{name}:", f"  - {path}"])
    (workspace / "code_agent.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace


def _valid_fix_plan(target_file_hint: str = "skills/question-strategy/SKILL.md") -> dict:
    return {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "create",
                "target_file_hint": target_file_hint,
                "evidence": ["RC1"],
                "fix_summary": "add a reusable focused probing strategy",
                "expected_effect": "improve content coverage",
                "risk": "low",
            }
        ],
        "rationale": "Prefer a skill for reusable elicitation procedure.",
    }


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


def _valid_system_prompt() -> str:
    return (
        "# Role\nInterviewer.\n\n"
        "# Goal\nElicit requirements.\n\n"
        "# Interaction Rules\nUse harness components.\n\n"
        "# Output Format\nReturn JSON.\n\n"
        "# Safety Boundaries\nDo not reveal hidden data.\n"
    )


def _valid_skill_lines(skill_id: str) -> list[str]:
    name = skill_id.replace("-", " ").replace("_", " ").title()
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


def _valid_memory_hit_content() -> str:
    return (
        "# Stock Report Website\n\n"
        "## Scope\n"
        "This memory records concise requirement content points previously elicited in scenarios similar to: Stock Report Website.\n\n"
        "## Recorded Hit Points\n"
        "- Reports may include export format and chart type.\n"
    )


def _valid_reflection_check_py(reflection_id: str) -> str:
    name = reflection_id.replace("_", " ").title()
    return (
        '"""\n'
        "component: self_reflection\n"
        f"reflection_id: {reflection_id}\n"
        f"name: {name}\n"
        "version: 0.1\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return []\n"
    )
