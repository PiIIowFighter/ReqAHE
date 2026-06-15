from __future__ import annotations

import json
from pathlib import Path

import pytest
from types import SimpleNamespace

from reqahe.config import apply_cli_overrides
from reqahe.cli import _assert_current_batch_memory_not_in_candidate, _memorization_complete
from reqahe.evolution.memorizer import memorize_rollout
from reqahe.evolution.loop import finalize_batch_workspace
from reqahe.harness.component_schema import ALLOWED_ARTIFACT_TYPES, validate_memory_markdown
from reqahe.harness.workspace import copy_harness_seed, load_harness_text, merge_memory_workspace
from reqahe.infra.io import read_json, read_jsonl, write_json
from reqahe.runtime import runner
from reqahe.runtime.dataset import Scenario
from reqahe.runtime.interviewer import SeedInterviewer
from reqahe.runtime.memory_router import (
    MemoryRouterConfig,
    list_memory_types,
    load_memory_for_type,
    memory_router_config_from_dict,
    route_memory_type,
)


def _write_workspace_with_code_agent(workspace: Path) -> None:
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
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "registry.yaml").write_text("version: 0.1\nchecks: []\n", encoding="utf-8")


def _write_type_memory(workspace: Path, type_slug: str, content: str) -> None:
    folder = workspace / "memory" / type_slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "MEMORY.md").write_text(content, encoding="utf-8")


def test_memory_router_config_accepts_max_selected_types_one() -> None:
    config = memory_router_config_from_dict({"max_selected_types": 1})
    assert config.max_selected_types == 1


def test_memory_router_config_rejects_unknown_config_field() -> None:
    with pytest.raises(ValueError, match="unknown config field"):
        memory_router_config_from_dict({"unknown_config_field": 1})


def test_memory_router_config_rejects_max_selected_types_above_one() -> None:
    with pytest.raises(ValueError, match="must be at most 1"):
        memory_router_config_from_dict({"max_selected_types": 2})


def test_memory_router_config_rejects_non_positive_max_selected_types() -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        memory_router_config_from_dict({"max_selected_types": 0})


def test_list_memory_types_scans_type_folders(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(workspace, "stock_report_website", "# Stock\n\n## Recorded Hit Points\n- one\n")
    (workspace / "memory" / "ignored.md").write_text("old flat memory", encoding="utf-8")

    assert list_memory_types(workspace) == ["stock_report_website"]


def test_route_memory_type_selects_at_most_one(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(workspace, "stock_report_website", "# Stock\n")
    _write_type_memory(workspace, "travel_booking_website", "# Travel\n")
    types = list_memory_types(workspace)

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "selected_type": "stock_report_website",
                "confidence": 0.9,
                "decision": "select",
                "reason": "stock match",
                "router_error": "",
            }

    result = route_memory_type(
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        initial_req="Build a stock report website",
        available_types=types,
        config=MemoryRouterConfig(enabled=True, max_selected_types=1),
        event_log_path=tmp_path / "memory_events.jsonl",
    )

    assert result["selected_type"] == "stock_report_website"
    assert result["decision"] == "select"


def test_route_memory_type_returns_none_without_match(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(workspace, "stock_report_website", "# Stock\n")
    types = list_memory_types(workspace)

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "selected_type": "",
                "confidence": 0.21,
                "decision": "none",
                "reason": "No existing memory type is specific enough.",
                "router_error": "",
            }

    result = route_memory_type(
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        initial_req="Build a pet grooming app",
        available_types=types,
        config=MemoryRouterConfig(enabled=True, min_confidence=0.45),
    )

    assert result["selected_type"] == ""
    assert result["decision"] == "none"


def test_load_memory_for_type_truncates_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    long_body = "x" * 3000
    _write_type_memory(workspace, "stock_report_website", f"# Stock\n\n{long_body}")

    loaded = load_memory_for_type(workspace, "stock_report_website", max_chars=100)

    assert len(loaded) <= 100
    assert loaded.endswith("...")


def test_interviewer_routes_memory_only_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(
        workspace,
        "stock_report_website",
        "# Stock Report Website\n\n## Recorded Hit Points\n- Reports may include export format and chart type.\n",
    )

    calls = {"routing": 0, "action": 0}

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            purpose = kwargs.get("purpose", "")
            if purpose == "memory type routing":
                calls["routing"] += 1
                return {
                    "selected_type": "stock_report_website",
                    "confidence": 0.9,
                    "decision": "select",
                    "reason": "match",
                    "router_error": "",
                }
            if purpose == "skill relevance routing":
                return {"selected_skill_ids": [], "decisions": []}
            calls["action"] += 1
            return {
                "action": "ask_question",
                "question": "What data should it show?",
                "finish_summary": "",
            }

    harness = load_harness_text(workspace)
    agent = SeedInterviewer(
        harness,
        FakeLLM(),  # type: ignore[arg-type]
        model="m",
        workspace_dir=workspace,
        memory_router_config={"enabled": True},
        skill_router_config={"enabled": False},
    )
    agent.next_action("Build a stock report website", [], [], max_turns=8, turn_index=0)
    agent.next_action(
        "Build a stock report website",
        [{"question": "Q1", "answer": "A1"}],
        [],
        max_turns=8,
        turn_index=1,
    )

    assert calls["routing"] == 1
    assert calls["action"] == 2
    assert agent._selected_memory_type == "stock_report_website"


def test_interviewer_prompt_uses_relevant_memory_not_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(
        workspace,
        "stock_report_website",
        "# Stock Report Website\n\n## Recorded Hit Points\n- Reports may include export format and chart type.\n",
    )
    _write_type_memory(
        workspace,
        "travel_booking_website",
        "# Travel\n\n## Recorded Hit Points\n- UNSELECTED_TRAVEL_POINT.\n",
    )

    harness = load_harness_text(workspace)
    agent = SeedInterviewer(
        harness,
        object(),  # type: ignore[arg-type]
        workspace_dir=workspace,
    )
    agent._selected_memory_type = "stock_report_website"
    agent._loaded_memory_excerpt = load_memory_for_type(workspace, "stock_report_website", 2200)
    agent._memory_routed = True

    prompt = agent._build_prompt(
        "Build a stock report website",
        [],
        [],
        max_turns=8,
        relevant_memory=__import__(
            "reqahe.runtime.memory_router", fromlist=["format_relevant_memory_block"]
        ).format_relevant_memory_block(agent._loaded_memory_excerpt),
    )

    assert "# Relevant Memory" in prompt
    assert "export format and chart type" in prompt
    assert "UNSELECTED_TRAVEL_POINT" not in prompt
    assert "# Memory Catalog" not in prompt
    assert "RETRY_THIS_TURN" not in prompt or "# Recent Reflection Feedback" in prompt


def test_validate_memory_markdown_rejects_strategy_language() -> None:
    bad = (
        "# Example\n\n## Recorded Hit Points\n"
        "- The interviewer should ask about export format earlier.\n"
    )
    errors = validate_memory_markdown("memory/example/MEMORY.md", bad)
    assert errors


def test_memorizer_writes_hit_points(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    batch_dir = tmp_path / "batch"
    rollout_dir = tmp_path / "rollout"
    _write_workspace_with_code_agent(workspace)
    trace_dir = rollout_dir / "train_001__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "scenario_id": "train_001",
            "initial_req": "Build a stock report website",
            "turns": [
                {
                    "turn_index": 0,
                    "question": "What report format do you need?",
                    "user_response": "PDF and CSV exports.",
                    "judgement": {
                        "is_relevant_to_implied_requirements": True,
                        "elicited_requirement_ids": [],
                    },
                }
            ],
        },
    )
    write_json(
        rollout_dir / "task_results.json",
        [{"scenario_id": "train_001", "trace_dir": "train_001__r0", "metrics": {"IRE": 0.5}}],
    )

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "scenario_type": {
                    "type_slug": "stock_report_website",
                    "display_name": "Stock Report Website",
                    "match_status": "new",
                    "matched_existing_type": "",
                    "confidence": 0.9,
                    "reason": "stock website",
                },
                "hit_points": [
                    {
                        "aspect": "content",
                        "point": "Reports may include PDF and CSV export formats.",
                        "evidence_turn_indices": [0],
                    }
                ],
                "skip": False,
                "skip_reason": "",
            }

    result = memorize_rollout(
        batch_dir=batch_dir,
        rollout_dir=rollout_dir,
        workspace_dir=workspace,
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        config={"enabled": True, "max_chars_per_point": 220},
    )

    memory_path = workspace / "memory" / "stock_report_website" / "MEMORY.md"
    assert memory_path.exists()
    assert "PDF and CSV export formats" in memory_path.read_text(encoding="utf-8")
    assert result["skip"] is False
    assert (batch_dir / "memorize_result.json").exists()
    assert read_json(batch_dir / "memorize_result.json")["apply_timing"] == "next_batch"


def test_memorizer_fallback_glob_complements_partial_task_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    batch_dir = tmp_path / "batch"
    rollout_dir = tmp_path / "rollout"
    _write_workspace_with_code_agent(workspace)

    listed_trace = rollout_dir / "train_001__r0"
    listed_trace.mkdir(parents=True)
    write_json(
        listed_trace / "clean_trace.json",
        {
            "scenario_id": "train_001",
            "initial_req": "Build a stock report website",
            "turns": [
                {
                    "turn_index": 0,
                    "question": "What report format do you need?",
                    "user_response": "PDF exports.",
                    "judgement": {
                        "is_relevant_to_implied_requirements": True,
                        "elicited_requirement_ids": [],
                    },
                }
            ],
        },
    )
    extra_trace = rollout_dir / "train_002__r0"
    extra_trace.mkdir(parents=True)
    write_json(
        extra_trace / "clean_trace.json",
        {
            "scenario_id": "train_002",
            "initial_req": "Build a travel booking website",
            "turns": [
                {
                    "turn_index": 0,
                    "question": "What destinations?",
                    "user_response": "Europe.",
                    "judgement": {
                        "is_relevant_to_implied_requirements": True,
                        "elicited_requirement_ids": [],
                    },
                }
            ],
        },
    )
    write_json(
        rollout_dir / "task_results.json",
        [{"scenario_id": "train_001", "trace_dir": "train_001__r0", "metrics": {"IRE": 0.5}}],
    )

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "scenario_type": {
                    "type_slug": "stock_report_website",
                    "display_name": "Stock Report Website",
                    "match_status": "new",
                    "matched_existing_type": "",
                    "confidence": 0.9,
                    "reason": "stock website",
                },
                "hit_points": [
                    {
                        "aspect": "content",
                        "point": "Reports may include PDF export formats.",
                        "evidence_turn_indices": [0],
                    }
                ],
                "skip": False,
                "skip_reason": "",
            }

    result = memorize_rollout(
        batch_dir=batch_dir,
        rollout_dir=rollout_dir,
        workspace_dir=workspace,
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        config={"enabled": True},
    )

    assert result["trace_count"] == 2


def test_memorizer_rejects_strategy_points(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    batch_dir = tmp_path / "batch"
    rollout_dir = tmp_path / "rollout"
    _write_workspace_with_code_agent(workspace)
    trace_dir = rollout_dir / "train_001__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "initial_req": "Build a stock report website",
            "turns": [
                {
                    "turn_index": 0,
                    "question": "Q",
                    "user_response": "A",
                    "judgement": {"is_relevant_to_implied_requirements": True, "elicited_requirement_ids": []},
                }
            ],
        },
    )
    write_json(rollout_dir / "task_results.json", [{"scenario_id": "train_001", "trace_dir": "train_001__r0"}])

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "scenario_type": {
                    "type_slug": "stock_report_website",
                    "display_name": "Stock Report Website",
                    "match_status": "new",
                    "matched_existing_type": "",
                    "confidence": 0.9,
                    "reason": "stock",
                },
                "hit_points": [
                    {
                        "aspect": "unknown",
                        "point": "The interviewer should ask about export format earlier.",
                        "evidence_turn_indices": [0],
                    }
                ],
                "skip": False,
                "skip_reason": "",
            }

    result = memorize_rollout(
        batch_dir=batch_dir,
        rollout_dir=rollout_dir,
        workspace_dir=workspace,
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
    )

    assert result["skip"] is True


def test_memorizer_deduplicates_existing_points(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    batch_dir = tmp_path / "batch"
    rollout_dir = tmp_path / "rollout"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(
        workspace,
        "stock_report_website",
        "# Stock Report Website\n\n## Recorded Hit Points\n- Reports may include PDF and CSV export formats.\n",
    )
    trace_dir = rollout_dir / "train_001__r0"
    trace_dir.mkdir(parents=True)
    write_json(
        trace_dir / "clean_trace.json",
        {
            "initial_req": "Build a stock report website",
            "turns": [
                {
                    "turn_index": 0,
                    "question": "Q",
                    "user_response": "A",
                    "judgement": {"is_relevant_to_implied_requirements": True, "elicited_requirement_ids": []},
                }
            ],
        },
    )
    write_json(rollout_dir / "task_results.json", [{"scenario_id": "train_001", "trace_dir": "train_001__r0"}])

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "scenario_type": {
                    "type_slug": "stock_report_website",
                    "display_name": "Stock Report Website",
                    "match_status": "existing",
                    "matched_existing_type": "stock_report_website",
                    "confidence": 0.9,
                    "reason": "existing",
                },
                "hit_points": [
                    {
                        "aspect": "content",
                        "point": "Reports may include PDF and CSV export formats.",
                        "evidence_turn_indices": [0],
                    }
                ],
                "skip": False,
                "skip_reason": "",
            }

    result = memorize_rollout(
        batch_dir=batch_dir,
        rollout_dir=rollout_dir,
        workspace_dir=workspace,
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        config={"deduplicate": True},
    )

    assert result["skip"] is True


def test_last_prompt_digest_absent_fields_semantic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(
        workspace,
        "stock_report_website",
        "# Stock Report Website\n\n## Recorded Hit Points\n- Reports may include export format.\n",
    )

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            purpose = kwargs.get("purpose", "")
            if purpose == "memory type routing":
                return {
                    "selected_type": "stock_report_website",
                    "confidence": 0.9,
                    "decision": "select",
                    "reason": "match",
                    "router_error": "",
                }
            if purpose == "skill relevance routing":
                return {"selected_skill_ids": [], "decisions": []}
            return {
                "action": "ask_question",
                "question": "What data should it show?",
                "finish_summary": "",
            }

    harness = load_harness_text(workspace)
    agent = SeedInterviewer(
        harness,
        FakeLLM(),  # type: ignore[arg-type]
        model="m",
        workspace_dir=workspace,
        memory_router_config={"enabled": True},
        skill_router_config={"enabled": False},
    )
    agent.next_action("Build a stock report website", [], [], max_turns=8, turn_index=0)

    digest = agent.last_prompt_digest
    assert digest["memory_catalog_absent"] is True
    assert digest["self_reflection_runtime_only"] is True
    assert "memory_catalog_present" not in digest


def test_run_single_task_writes_memory_router_trace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_type_memory(
        workspace,
        "stock_report_website",
        "# Stock Report Website\n\n## Recorded Hit Points\n- Reports may include export format.\n",
    )
    harness = load_harness_text(workspace)

    class FakeSession:
        evaluation_mode = "reqelicitgym_judge_user"

        def __init__(self, *args, **kwargs) -> None:
            self.calls = 0

        def step(self, utterance: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "user_response": "It should show revenue.",
                    "judgement": {
                        "is_relevant_to_implied_requirements": True,
                        "elicited_requirement_ids": ["IR1"],
                        "action_type": "probe",
                    },
                }
            return {
                "user_response": "Done.",
                "judgement": {
                    "is_relevant_to_implied_requirements": False,
                    "elicited_requirement_ids": [],
                    "action_type": "finish",
                },
            }

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def json_chat(self, *args, **kwargs) -> dict:
            self.calls += 1
            purpose = kwargs.get("purpose", "")
            if purpose == "memory type routing":
                return {
                    "selected_type": "stock_report_website",
                    "confidence": 0.9,
                    "decision": "select",
                    "reason": "match",
                    "router_error": "",
                }
            if purpose == "skill relevance routing":
                return {"selected_skill_ids": [], "decisions": []}
            if purpose == "interviewer action generation" and self.calls <= 2:
                return {
                    "thought_summary": "ask",
                    "action": "ask_question",
                    "question": "What data should it show?",
                    "finish_summary": "",
                }
            return {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Revenue dashboard.",
            }

    monkeypatch.setattr(runner, "ReqElicitSession", FakeSession)
    scenario = Scenario(
        scenario_id="train_001",
        name="train_001",
        app_type="dashboard",
        initial_req="Build a stock report website",
        implicit_requirements=[{"id": "IR1", "Aspect": "Content"}],
        final_requirements=[],
        raw={},
    )

    result = runner.run_single_task(
        scenario,
        harness,
        workspace,
        tmp_path / "task",
        FakeLLM(),  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="observe",
        agent_name="seed_freeform",
        memory_router_config={"enabled": True, "log_events": True},
        memory_router_model="router-m",
        skill_router_config={"enabled": False},
    )
    trace_dir = tmp_path / result["trace_dir"]
    record = read_jsonl(trace_dir / "agent_prompts.jsonl")[0]

    assert "selected_type" in record["memory_router"]
    assert record["memory_router"]["selected_type"] == "stock_report_website"
    assert record["memory_router"]["decision"] == "select"
    assert "selected_memory_ids" not in record["memory_router"]
    assert record["prompt_digest"]["memory_catalog_absent"] is True
    assert record["prompt_digest"]["self_reflection_runtime_only"] is True


def test_finalize_batch_workspace_preserves_memory_on_rollback(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "system_prompt.md").write_text("before prompt", encoding="utf-8")
    (harness_seed / "code_agent.yaml").write_text(
        "name: seed\nsystem_prompt: system_prompt.md\nmemory:\n  - memory/README.md\n",
        encoding="utf-8",
    )
    (harness_seed / "memory").mkdir()
    (harness_seed / "memory" / "README.md").write_text("memory", encoding="utf-8")

    batch_dir = tmp_path / "batch"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    copy_harness_seed(project_root, workspace_before)
    copy_harness_seed(project_root, workspace_candidate)
    (workspace_candidate / "system_prompt.md").write_text("candidate prompt", encoding="utf-8")
    copy_harness_seed(project_root, workspace_memory)
    _write_type_memory(
        workspace_memory,
        "type_a",
        "# Type A\n\n## Recorded Hit Points\n- new memory\n",
    )

    finalize_info = finalize_batch_workspace(
        project_root,
        batch_dir,
        "rollback",
        workspace_before,
        workspace_candidate,
        workspace_memory=workspace_memory,
    )
    workspace_after = finalize_info["workspace_after"]

    assert (workspace_after / "system_prompt.md").read_text(encoding="utf-8").strip() == "before prompt"
    saved = workspace_after / "memory" / "type_a" / "MEMORY.md"
    assert saved.exists()
    assert "new memory" in saved.read_text(encoding="utf-8")
    assert finalize_info["harness_source"] == "workspace_before"
    assert finalize_info["memory_merged"] is True


def test_finalize_batch_workspace_preserves_memory_on_keep(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "system_prompt.md").write_text("before prompt", encoding="utf-8")
    (harness_seed / "code_agent.yaml").write_text(
        "name: seed\nsystem_prompt: system_prompt.md\nmemory:\n  - memory/README.md\n",
        encoding="utf-8",
    )
    (harness_seed / "memory").mkdir()
    (harness_seed / "memory" / "README.md").write_text("memory", encoding="utf-8")

    batch_dir = tmp_path / "batch"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    copy_harness_seed(project_root, workspace_before)
    copy_harness_seed(project_root, workspace_candidate)
    (workspace_candidate / "system_prompt.md").write_text("candidate prompt", encoding="utf-8")
    copy_harness_seed(project_root, workspace_memory)
    _write_type_memory(
        workspace_memory,
        "type_a",
        "# Type A\n\n## Recorded Hit Points\n- new memory\n",
    )

    finalize_info = finalize_batch_workspace(
        project_root,
        batch_dir,
        "keep",
        workspace_before,
        workspace_candidate,
        workspace_memory=workspace_memory,
    )
    workspace_after = finalize_info["workspace_after"]

    assert (workspace_after / "system_prompt.md").read_text(encoding="utf-8").strip() == "candidate prompt"
    saved = workspace_after / "memory" / "type_a" / "MEMORY.md"
    assert saved.exists()
    assert "new memory" in saved.read_text(encoding="utf-8")
    assert finalize_info["harness_source"] == "workspace_candidate"


def test_next_batch_inherits_previous_batch_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    harness_seed = project_root / "harness_seed"
    harness_seed.mkdir(parents=True)
    (harness_seed / "system_prompt.md").write_text("seed", encoding="utf-8")
    (harness_seed / "code_agent.yaml").write_text(
        "name: seed\nsystem_prompt: system_prompt.md\nmemory:\n  - memory/README.md\n",
        encoding="utf-8",
    )
    (harness_seed / "memory").mkdir()
    (harness_seed / "memory" / "README.md").write_text("memory", encoding="utf-8")

    iteration_dir = tmp_path / "iteration_001"
    batch_001 = iteration_dir / "batch_001"
    batch_002 = iteration_dir / "batch_002"
    workspace_before_1 = batch_001 / "workspace_before"
    workspace_candidate_1 = batch_001 / "workspace_candidate"
    workspace_memory_1 = batch_001 / "workspace_memory"
    copy_harness_seed(project_root, workspace_before_1)
    copy_harness_seed(project_root, workspace_candidate_1)
    copy_harness_seed(project_root, workspace_memory_1)
    _write_type_memory(
        workspace_memory_1,
        "type_a",
        "# Type A\n\n## Recorded Hit Points\n- inherited memory\n",
    )

    finalize_batch_workspace(
        project_root,
        batch_001,
        "rollback",
        workspace_before_1,
        workspace_candidate_1,
        workspace_memory=workspace_memory_1,
    )
    copy_harness_seed(project_root, batch_002 / "workspace_before", source_workspace=batch_001 / "workspace_after")

    inherited = batch_002 / "workspace_before" / "memory" / "type_a" / "MEMORY.md"
    assert inherited.exists()
    assert "inherited memory" in inherited.read_text(encoding="utf-8")


def test_memorization_complete_requires_workspace_memory_file(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_001"
    workspace_memory = batch_dir / "workspace_memory"
    write_json(
        batch_dir / "memorize_result.json",
        {
            "skip": False,
            "memory_path": "memory/type_a/MEMORY.md",
            "apply_timing": "next_batch",
        },
    )

    assert _memorization_complete(batch_dir, workspace_memory) is False

    _write_type_memory(workspace_memory, "type_a", "# Type A\n\n## Recorded Hit Points\n- one\n")
    assert _memorization_complete(batch_dir, workspace_memory) is True


def test_assert_current_batch_memory_not_in_candidate_raises(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_001"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    workspace_before.mkdir(parents=True)
    workspace_candidate.mkdir(parents=True)
    workspace_memory.mkdir(parents=True)
    _write_type_memory(workspace_memory, "new_type", "# New\n\n## Recorded Hit Points\n- one\n")
    write_json(
        batch_dir / "memorize_result.json",
        {"skip": False, "memory_path": "memory/new_type/MEMORY.md"},
    )
    leaked = workspace_candidate / "memory" / "new_type"
    leaked.mkdir(parents=True)
    (leaked / "MEMORY.md").write_text("leaked\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="leaked into workspace_candidate"):
        _assert_current_batch_memory_not_in_candidate(
            workspace_before,
            workspace_candidate,
            workspace_memory,
            batch_dir,
        )

    error = read_json(batch_dir / "memory_visibility_error.json")
    assert error["leaked_paths"] == ["memory/new_type/MEMORY.md"]


def test_assert_current_batch_memory_allows_inherited_paths(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_002"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    workspace_before.mkdir(parents=True)
    workspace_candidate.mkdir(parents=True)
    workspace_memory.mkdir(parents=True)
    _write_type_memory(workspace_before, "new_type", "# New\n\n## Recorded Hit Points\n- inherited\n")
    _write_type_memory(workspace_candidate, "new_type", "# New\n\n## Recorded Hit Points\n- inherited\n")
    _write_type_memory(workspace_memory, "new_type", "# New\n\n## Recorded Hit Points\n- inherited\n")
    write_json(
        batch_dir / "memorize_result.json",
        {"skip": False, "memory_path": "memory/new_type/MEMORY.md"},
    )

    _assert_current_batch_memory_not_in_candidate(
        workspace_before,
        workspace_candidate,
        workspace_memory,
        batch_dir,
    )


def test_assert_current_batch_memory_updated_existing_type_leaks(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_003"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    workspace_before.mkdir(parents=True)
    workspace_candidate.mkdir(parents=True)
    workspace_memory.mkdir(parents=True)

    old_memory = "old memory"
    new_memory = "old memory\nnew batch memory"
    _write_type_memory(workspace_before, "type_a", old_memory)
    _write_type_memory(workspace_memory, "type_a", new_memory)
    _write_type_memory(workspace_candidate, "type_a", new_memory)
    write_json(
        batch_dir / "memorize_result.json",
        {"skip": False, "memory_path": "memory/type_a/MEMORY.md"},
    )

    with pytest.raises(RuntimeError, match="leaked into workspace_candidate"):
        _assert_current_batch_memory_not_in_candidate(
            workspace_before,
            workspace_candidate,
            workspace_memory,
            batch_dir,
        )

    error = read_json(batch_dir / "memory_visibility_error.json")
    assert error["leaked_paths"] == ["memory/type_a/MEMORY.md"]
    assert error["workspace_before"] == "workspace_before"


def test_assert_current_batch_memory_updated_existing_type_not_leaked(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_004"
    workspace_before = batch_dir / "workspace_before"
    workspace_candidate = batch_dir / "workspace_candidate"
    workspace_memory = batch_dir / "workspace_memory"
    workspace_before.mkdir(parents=True)
    workspace_candidate.mkdir(parents=True)
    workspace_memory.mkdir(parents=True)

    old_memory = "old memory"
    new_memory = "old memory\nnew batch memory"
    _write_type_memory(workspace_before, "type_a", old_memory)
    _write_type_memory(workspace_memory, "type_a", new_memory)
    _write_type_memory(workspace_candidate, "type_a", old_memory)
    write_json(
        batch_dir / "memorize_result.json",
        {"skip": False, "memory_path": "memory/type_a/MEMORY.md"},
    )

    _assert_current_batch_memory_not_in_candidate(
        workspace_before,
        workspace_candidate,
        workspace_memory,
        batch_dir,
    )
    assert not (batch_dir / "memory_visibility_error.json").exists()


def test_apply_cli_overrides_disable_memory_router() -> None:
    config = {"runtime": {"memory_router": {"enabled": True}}}
    args = SimpleNamespace(
        base_url=None,
        api_key=None,
        model=None,
        temperature=None,
        max_turns=None,
        rollouts_per_task=None,
        task_mode=None,
        dataset_file=None,
        dataset_number=None,
        split=None,
        iterations=None,
        batch_size=None,
        reflection_mode=None,
        disable_close_wait_cleanup=False,
        close_wait_cleanup_interval_tasks=None,
        close_wait_cleanup_interval_seconds=None,
        disable_skill_router=False,
        max_selected_skills=None,
        skill_router_min_relevance=None,
        skill_router_model=None,
        disable_memory_router=True,
        max_selected_memory_types=None,
        memory_router_min_relevance=None,
        memory_router_model=None,
    )
    updated = apply_cli_overrides(config, args)
    assert updated["runtime"]["memory_router"]["enabled"] is False


def test_allowed_artifact_types_are_exactly_three_evolved_types() -> None:
    assert "memory_lesson_v1" not in ALLOWED_ARTIFACT_TYPES
    assert ALLOWED_ARTIFACT_TYPES == {
        "system_prompt_section_v1",
        "skill_markdown_v1",
        "reflection_check_bundle_v1",
    }
