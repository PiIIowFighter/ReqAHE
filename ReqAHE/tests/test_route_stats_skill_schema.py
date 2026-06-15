from __future__ import annotations

import json
from pathlib import Path

from reqahe.diagnoser.pipeline import build_component_localization_payload, build_full_trace_problem_payload
from reqahe.evolution.attribution import judge_batch_decision
from reqahe.harness.component_schema import validate_skill_minimal_frontmatter
from reqahe.harness.workspace import load_skill_catalog, load_skill_catalog_summary, render_skill_catalog, scan_skill_artifacts
from reqahe.infra.io import read_json
from reqahe.refiner.validation import validate_fix_plan
from reqahe.runtime.route_stats import collect_rollout_route_events, render_route_stats_digest, write_rollout_route_stats


def _write_workspace(workspace: Path) -> None:
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "self_reflection").mkdir(exist_ok=True)
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n"
        "memory:\n  - memory/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "system_prompt.md").write_text("# Role\nInterviewer\n", encoding="utf-8")
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")


def _minimal_skill_content(skill_id: str, *, enabled: bool = True) -> str:
    return (
        "---\n"
        f'id: "{skill_id}"\n'
        f'name: "{skill_id.replace("-", " ").title()}"\n'
        "version: 1\n"
        f"enabled: {'true' if enabled else 'false'}\n"
        'intent: "Improve elicitation in tests."\n'
        "scope:\n"
        '  - "Test scenarios"\n'
        "use_when:\n"
        '  - "A focused follow-up may help."\n'
        "avoid_when:\n"
        '  - "The user already answered the concern."\n'
        "risk_notes:\n"
        '  - "Overuse may narrow questioning."\n'
        "---\n"
        "# Skill\n\nAsk one focused question.\n"
    )


def _write_skill(workspace: Path, skill_id: str, *, enabled: bool = True) -> None:
    skill_dir = workspace / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_minimal_skill_content(skill_id, enabled=enabled), encoding="utf-8")


def test_valid_skill_loads_into_router_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace)
    _write_skill(workspace, "skill-a")

    catalog = load_skill_catalog(workspace)

    assert [item["skill_id"] for item in catalog] == ["skill-a"]
    assert catalog[0]["intent"]


def test_invalid_skill_records_schema_error_and_skips_router(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace)
    skill_dir = workspace / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\nNo front matter\n", encoding="utf-8")

    scan = scan_skill_artifacts(workspace)

    assert scan["router_catalog"] == []
    assert scan["schema_errors"]
    errors = read_json(workspace / "skill_schema_errors.json")
    assert errors["errors"][0]["path"] == "skills/bad-skill/SKILL.md"


def test_disabled_skill_not_in_router_but_in_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace)
    _write_skill(workspace, "enabled-skill", enabled=True)
    _write_skill(workspace, "disabled-skill", enabled=False)

    catalog = load_skill_catalog(workspace)
    summary = load_skill_catalog_summary(workspace)

    assert [item["skill_id"] for item in catalog] == ["enabled-skill"]
    assert "disabled-skill" in summary
    assert "## Disabled Skills" in summary


def test_route_events_and_stats_written_for_rollout(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    trace_dir = rollout / "train_001__r0"
    trace_dir.mkdir(parents=True)
    (trace_dir / "route_events.jsonl").write_text(
        "\n".join(
            [
                '{"task_id":"train_001","turn_index":0,"candidate_skill_ids":["skill-a","skill-b"],"selected_skill_ids":["skill-a"],"router_reason":"skill-a: directly applies","question":"What data should it show?","answer":"Revenue.","turn_hit":true,"hit_targets":["IR1"]}',
                '{"task_id":"train_001","turn_index":1,"candidate_skill_ids":["skill-a","skill-b"],"selected_skill_ids":["skill-b"],"router_reason":"skill-b: follow-up","question":"Any filters?","answer":"No.","turn_hit":false,"hit_targets":[]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task_results = [{"scenario_id": "train_001", "trace_dir": "train_001__r0", "metrics": {"IRE": 0.5}}]
    (rollout / "task_results.json").write_text(
        __import__("json").dumps(task_results),
        encoding="utf-8",
    )

    stats = write_rollout_route_stats(rollout, task_results, router_skill_ids=["skill-a", "skill-b"])

    assert stats["total_turns"] == 2
    assert stats["skills"]["skill-a"]["selected_count"] == 1
    assert stats["skills"]["skill-a"]["hit_count"] == 1
    assert stats["skills"]["skill-a"]["hit_rate"] == 1.0
    assert stats["skills"]["skill-b"]["selection_share"] == 0.5
    assert (rollout / "route_stats.json").exists()
    digest = (rollout / "route_stats_digest.md").read_text(encoding="utf-8")
    assert "Route Stats Digest" in digest
    assert "skill-a" in digest
    assert "## Skill Hit Summary" in digest
    assert "hit_rate=0.0" in digest
    assert "low observed hit rates" not in digest


def test_diagnoser_payload_includes_route_stats(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    rollout.mkdir()
    (rollout / "metrics.json").write_text('{"mean_IRE": 0.2}', encoding="utf-8")
    (rollout / "task_results.json").write_text("[]", encoding="utf-8")
    (rollout / "route_stats_digest.md").write_text("# Route Stats Digest\n", encoding="utf-8")
    (rollout / "route_stats.json").write_text('{"total_turns": 1, "skills": {}, "unselected_skills": []}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    _write_workspace(workspace)
    _write_skill(workspace, "skill-a")

    payload = build_full_trace_problem_payload(rollout, {"mean_IRE": 0.2}, {}, workspace)

    assert "route_stats_digest_md" in payload
    assert "route_stats_summary" in payload
    assert "skill_catalog_summary" in payload


def test_refiner_fix_plan_payload_includes_route_stats_and_skill_summary(tmp_path: Path) -> None:
    from reqahe.refiner.pipeline import build_fix_plan_payload, build_write_policy, load_declared_components

    iteration = tmp_path / "iteration"
    rollout = iteration / "rollout_before"
    rollout.mkdir(parents=True)
    (rollout / "route_stats_digest.md").write_text("# Route Stats Digest\n", encoding="utf-8")
    (rollout / "route_stats.json").write_text('{"total_turns": 1, "skills": {}, "unselected_skills": []}', encoding="utf-8")
    workspace = iteration / "workspace"
    _write_workspace(workspace)
    _write_skill(workspace, "skill-a")
    write_policy = build_write_policy(workspace, load_declared_components(workspace))
    payload = build_fix_plan_payload(
        iteration,
        workspace,
        {"localization_summary": "x", "component_findings": [], "refiner_guidance": {}},
        write_policy,
    )
    assert "route_stats_digest_md" in payload
    assert "skill_catalog_summary" in payload


def test_fix_plan_schema_supports_extended_operations() -> None:
    fix_plan = {
        "fix_plan": [
            {
                "fix_id": "F1",
                "component": "skills",
                "artifact_type": "skill_markdown_v1",
                "operation_intent": "demote",
                "target_file_hint": "skills/skill-a/SKILL.md",
                "evidence": ["route stats"],
                "fix_summary": "narrow boundaries",
                "expected_effect": "better routing",
                "risk": "low",
            }
        ],
        "rationale": "test",
    }
    validate_fix_plan(fix_plan, {"system_prompt", "skills"})


def test_batch_decision_does_not_use_route_stats() -> None:
    before = {"main_score": 0.4, "mean_IRE": 0.4, "mean_TKQR": 0.4}
    after = {"main_score": 0.5, "mean_IRE": 0.5, "mean_TKQR": 0.5}
    verdict = judge_batch_decision(before, after)
    assert verdict["decision"] == "keep"
    assert "route_stats" not in str(verdict)


def test_route_stats_digest_outputs_facts_without_fixed_hit_rate_judgement() -> None:
    digest = render_route_stats_digest(
        {
            "total_turns": 12,
            "skills": {
                "skill-a": {
                    "selected_count": 12,
                    "hit_count": 2,
                    "selection_share": 1.0,
                    "hit_rate": 0.1667,
                    "sample_questions": [],
                    "sample_router_reasons": [],
                }
            },
            "unselected_skills": ["skill-b"],
        }
    )

    assert "`skill-a`: selected 12 times, hit 2 times, hit_rate=0.1667." in digest
    assert "low observed hit rates" not in digest
    assert "low_hit_rate" not in digest
    assert "overused" not in digest
    assert "fallback_overuse" not in digest
    assert "starved" not in digest
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "reqahe"
        / "runtime"
        / "route_stats.py"
    ).read_text(encoding="utf-8")
    assert "hit_rate" in source
    assert "< 0.5" not in source


def test_collect_rollout_route_events_fallback_enriches_per_scenario(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    first = rollout / "task_001__r0"
    second = rollout / "task_002__r0"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "clean_trace.json").write_text(
        json.dumps(
            {
                "scenario_id": "task_001",
                "skill_routing_events": [
                    {
                        "turn_index": 0,
                        "candidate_skill_ids": ["skill-a"],
                        "selected_skill_ids": ["skill-a"],
                        "router_reason": "first route",
                    }
                ],
                "turns": [
                    {
                        "turn_index": 0,
                        "action": "ask_question",
                        "question": "First question?",
                        "user_response": "First answer.",
                        "judgement": {
                            "is_relevant_to_implied_requirements": True,
                            "elicited_requirement_ids": ["IR1"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (second / "clean_trace.json").write_text(
        json.dumps(
            {
                "scenario_id": "task_002",
                "skill_routing_events": [
                    {
                        "turn_index": 0,
                        "candidate_skill_ids": ["skill-b"],
                        "selected_skill_ids": ["skill-b"],
                        "router_reason": "second route",
                    }
                ],
                "turns": [
                    {
                        "turn_index": 0,
                        "action": "ask_question",
                        "question": "Second question?",
                        "user_response": "Second answer.",
                        "judgement": {
                            "is_relevant_to_implied_requirements": False,
                            "elicited_requirement_ids": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    events = collect_rollout_route_events(
        rollout,
        [
            {"scenario_id": "task_001", "trace_dir": "task_001__r0"},
            {"scenario_id": "task_002", "trace_dir": "task_002__r0"},
        ],
    )

    assert len(events) == 2
    assert events[0]["task_id"] == "task_001"
    assert events[0]["question"] == "First question?"
    assert events[0]["answer"] == "First answer."
    assert events[0]["turn_hit"] is True
    assert events[1]["task_id"] == "task_002"
    assert events[1]["question"] == "Second question?"
    assert events[1]["answer"] == "Second answer."
    assert events[1]["turn_hit"] is False


def test_validate_skill_minimal_frontmatter_accepts_disable() -> None:
    content = _minimal_skill_content("skill-a", enabled=False)
    errors = validate_skill_minimal_frontmatter("skills/skill-a/SKILL.md", content)
    assert errors == []


def test_render_skill_catalog_uses_intent_not_procedure() -> None:
    rendered = render_skill_catalog(
        [
            {
                "skill_id": "x",
                "name": "X",
                "intent": "Probe missing details",
                "scope": ["tests"],
                "use_when": ["gap"],
                "avoid_when": ["done"],
                "risk_notes": ["overuse"],
                "relative_path": "skills/x/SKILL.md",
            }
        ]
    )
    assert "Probe missing details" in rendered
    assert "Procedure" not in rendered


def test_component_localization_payload_includes_route_stats(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout"
    rollout.mkdir()
    (rollout / "route_stats_digest.md").write_text("# Route Stats Digest\n", encoding="utf-8")
    payload = build_component_localization_payload(
        {"diagnosis_summary": "bad routing"},
        [{"name": "skills", "purpose": "skills"}],
        rollout=rollout,
    )
    assert "route_stats_digest_md" in payload
