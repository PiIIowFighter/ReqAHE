from __future__ import annotations

import json
from pathlib import Path

import pytest

from reqahe.evolution.attribution import judge_batch_decision
from reqahe.evolution.loop import build_skill_evolution_digest, write_iteration_artifacts
from reqahe.harness.component_schema import validate_reflection_python
from reqahe.infra.io import read_json, write_json
from reqahe.refiner.pipeline import refine_harness
from reqahe.refiner.validation import validate_proposed_edits
from tests.test_diagnoser_refiner_schema import (
    _skill_create_similarity_audit,
    _skill_self_validation,
    _valid_fix_plan,
    _valid_skill_lines,
    _valid_system_prompt,
    _workspace_with_manifest,
)


def _refiner_workspace(tmp_path: Path) -> tuple[Path, Path]:
    iteration = tmp_path / "batch_001"
    workspace = iteration / "workspace_candidate"
    analysis = iteration / "analysis"
    rollout = iteration / "rollout_before"
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
    write_json(rollout / "route_stats.json", {"total_turns": 0, "skills": {}, "unselected_skills": []})
    return iteration, workspace


def _skill_refinement(skill_id: str) -> dict:
    return {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "add"}],
        "file_edits": [
            {
                "relative_path": f"skills/{skill_id}/SKILL.md",
                "operation": "create",
                "new_content": "\n".join(_valid_skill_lines(skill_id)),
            }
        ],
        "schema_compliance": [
            {
                "component": "skills",
                "schema_name": "skill_markdown_v1",
                "new_or_updated_files": [f"skills/{skill_id}/SKILL.md"],
            }
        ],
        "refiner_rationale": "compact retry result",
        "similarity_audit": _skill_create_similarity_audit("compact retry skill"),
        "self_validation": _skill_self_validation(),
    }


def test_refiner_compact_retry_after_empty_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    iteration, workspace = _refiner_workspace(tmp_path)

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def json_chat(self, *args, **kwargs) -> dict:
            self.calls += 1
            if self.calls == 1:
                return _valid_fix_plan("skills/compact-skill/SKILL.md")
            if self.calls == 2:
                raise RuntimeError("empty model response")
            return _skill_refinement("compact-skill")

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)
    llm = FakeLLM()
    refine_harness(iteration, workspace, 1, llm, "m")  # type: ignore[arg-type]

    refiner_dir = iteration / "refiner"
    stats = read_json(refiner_dir / "refiner_call_stats.json")
    assert (refiner_dir / "edit_payload.full.json").exists()
    assert (refiner_dir / "edit_payload.compact.json").exists()
    assert stats["used_compact_retry"] is True
    assert stats["edit_generation_attempts"] == 2
    assert stats["final_status"] == "ok"


def test_refiner_compact_retry_failure_has_no_fallback_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    iteration, workspace = _refiner_workspace(tmp_path)

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def json_chat(self, *args, **kwargs) -> dict:
            self.calls += 1
            if self.calls == 1:
                return _valid_fix_plan("skills/compact-fail/SKILL.md")
            if self.calls == 2:
                raise RuntimeError("empty model response")
            raise RuntimeError("invalid JSON")

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        refine_harness(iteration, workspace, 1, FakeLLM(), "m")  # type: ignore[arg-type]

    refiner_dir = iteration / "refiner"
    stats = read_json(refiner_dir / "refiner_call_stats.json")
    assert (refiner_dir / "edit_payload.compact.json").exists()
    assert stats["used_compact_retry"] is True
    assert stats["final_status"] == "failed"
    assert not (refiner_dir / "proposed_edits.fallback.json").exists()
    assert not (workspace / "skills" / "compact-fail" / "SKILL.md").exists()


def test_no_minimal_fallback_builder_exists() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "reqahe" / "refiner" / "pipeline.py").read_text(
        encoding="utf-8"
    )
    assert "try_build_minimal_skill_edits_from_fix_plan" not in source
    assert "proposed_edits.fallback" not in source


def test_decision_thresholds_and_metric_tradeoff() -> None:
    cfg = {"min_keep_delta_main_score": 0.02, "max_allowed_IRE_drop": 0.01, "rollback_small_delta": True}
    before = {"main_score": 0.5, "mean_IRE": 0.5, "mean_TKQR": 0.5}
    assert judge_batch_decision(before, {"main_score": 0.505, "mean_IRE": 0.5}, decision_config=cfg)["decision"] == "rollback_small_delta"
    assert judge_batch_decision(before, {"main_score": 0.53, "mean_IRE": 0.5}, decision_config=cfg)["decision"] == "keep"
    assert judge_batch_decision(before, {"main_score": 0.53, "mean_IRE": 0.48}, decision_config=cfg)["decision"] == "rollback_metric_tradeoff"
    failed = judge_batch_decision(before, {}, refiner_ok=False, decision_config=cfg)
    assert failed["metrics_compared"] is False
    assert failed["delta_main_score"] is None


def test_skill_evolution_digest_is_descriptive_and_tolerates_missing_inputs(tmp_path: Path) -> None:
    iteration = tmp_path / "iteration_001"
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    skill_path = workspace / "skills" / "vagueness_handler" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("\n".join(_valid_skill_lines("vagueness_handler")), encoding="utf-8")
    batch = iteration / "batch_001"
    write_json(batch / "refiner" / "refiner_stats.json", {"touched_skill_ids": ["vagueness_handler"], "operation_intents": ["update"]})
    write_json(batch / "batch_decision.json", {"decision": "rollback_small_delta"})
    write_json(
        batch / "rollout_before" / "route_stats.json",
        {"total_turns": 10, "skills": {"vagueness_handler": {"selection_share": 0.4, "hit_rate": 0.1}}},
    )

    digest = build_skill_evolution_digest(iteration, workspace)
    assert digest["skills"]["vagueness_handler"]["recent_touched_count"] == 1
    serialized = json.dumps(digest, ensure_ascii=False)
    assert "must demote" not in serialized
    assert "< 0.5" not in serialized
    assert build_skill_evolution_digest(tmp_path / "missing", workspace)["skills"]


def test_skill_evolution_digest_collects_recent_batches_across_iterations(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    for skill_id in ("history_skill", "old_skill", "current_skill"):
        skill_path = workspace / "skills" / skill_id / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("\n".join(_valid_skill_lines(skill_id)), encoding="utf-8")

    iteration_001 = tmp_path / "iteration_001"
    iteration_002 = tmp_path / "iteration_002"
    batch_006 = iteration_001 / "batch_006"
    batch_007 = iteration_001 / "batch_007"
    batch_001 = iteration_002 / "batch_001"
    current_batch = iteration_002 / "batch_002"

    write_json(batch_006 / "refiner" / "refiner_stats.json", {"touched_skill_ids": ["old_skill"], "operation_intents": ["update"]})
    write_json(batch_006 / "batch_decision.json", {"decision": "keep"})
    write_json(
        batch_006 / "rollout_before" / "route_stats.json",
        {"total_turns": 10, "skills": {"old_skill": {"selection_share": 0.8, "hit_rate": 0.2}}},
    )
    write_json(
        batch_007 / "refiner" / "refiner_stats.json",
        {"touched_skill_ids": ["history_skill"], "operation_intents": ["update"]},
    )
    write_json(batch_007 / "batch_decision.json", {"decision": "rollback_small_delta"})
    write_json(
        batch_001 / "refiner" / "refiner_stats.json",
        {"touched_skill_ids": ["history_skill"], "operation_intents": ["update"]},
    )
    write_json(batch_001 / "batch_decision.json", {"decision": "keep"})
    write_json(
        batch_001 / "rollout_before" / "route_stats.json",
        {"total_turns": 10, "skills": {"history_skill": {"selection_share": 0.25, "hit_rate": 0.1}}},
    )
    write_json(
        current_batch / "refiner" / "refiner_stats.json",
        {"touched_skill_ids": ["current_skill"], "operation_intents": ["update"]},
    )

    digest = build_skill_evolution_digest(
        iteration_002,
        workspace,
        current_batch_dir=current_batch,
        recent_window=2,
    )

    assert digest["source"]["batch_count_seen"] == 2
    assert digest["skills"]["history_skill"]["recent_touched_count"] == 2
    assert digest["skills"]["history_skill"]["recent_keep_count"] == 1
    assert digest["skills"]["history_skill"]["recent_rollback_count"] == 1
    assert digest["skills"]["history_skill"]["avg_selection_share"] == 0.25
    assert digest["skills"]["old_skill"]["recent_touched_count"] == 0
    assert digest["skills"]["current_skill"]["recent_touched_count"] == 0


def test_seed_registry_empty_and_self_reflection_validation_guards() -> None:
    seed_registry = Path(__file__).resolve().parents[1] / "harness_seed" / "self_reflection" / "registry.yaml"
    assert "checks: []" in seed_registry.read_text(encoding="utf-8")

    bad = (
        '"""\ncomponent: self_reflection\nreflection_id: bad_check\nname: Bad\nversion: 0.1\n'
        'hook: question_candidate\nmode: warn\n"""\n\n'
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        "    return [{'message': state.get('task_id', '')}]\n"
    )
    errors = validate_reflection_python("self_reflection/bad_check/check.py", bad)
    assert any("task id" in err for err in errors)


def test_validator_rejects_runtime_reflection_edit(tmp_path: Path) -> None:
    workspace = _workspace_with_manifest(tmp_path, {"self_reflection": "self_reflection/README.md"})
    refinement = {
        "changes": [{"change_id": "C1", "fix_id": "F1", "component": "self_reflection", "summary": "bad"}],
        "file_edits": [{"relative_path": "src/reqahe/runtime/reflection.py", "operation": "create", "new_content": ""}],
        "schema_compliance": [
            {
                "component": "self_reflection",
                "schema_name": "reflection_check_bundle_v1",
                "new_or_updated_files": ["src/reqahe/runtime/reflection.py"],
            }
        ],
        "similarity_audit": [],
    }
    report = validate_proposed_edits(
        workspace,
        refinement,
        {"fix_plan": []},
        {"max_fixes": 3, "allowed_components": ["self_reflection"], "path_patterns": {"self_reflection": "self_reflection/<id>/check.py"}},
        {"reflection_check_bundle_v1": {}},
        {"self_reflection"},
    )
    assert report["ok"] is False
    assert any("forbidden path prefix" in err or "not allowed" in err for err in report["errors"])


def test_iteration_metrics_counts_final_workspace_skills(tmp_path: Path) -> None:
    iteration = tmp_path / "iteration_001"
    final_workspace = _workspace_with_manifest(tmp_path, {"skills": "skills/README.md"})
    skill_path = final_workspace / "skills" / "counted_skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("\n".join(_valid_skill_lines("counted_skill")), encoding="utf-8")

    write_iteration_artifacts(
        iteration,
        iteration=1,
        selected_scenario_count=1,
        batch_size=1,
        batch_summaries=[],
        pre_update_aggregate={},
        post_judged_aggregate={},
        max_turns=4,
        final_workspace=final_workspace,
    )
    metrics = read_json(iteration / "iteration_metrics.json")
    assert metrics["workspace_skill_catalog_size"] > 0
