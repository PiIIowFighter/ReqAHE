from __future__ import annotations

from pathlib import Path

from reqahe.harness.workspace import load_harness_text, load_selected_skill_text, load_skill_catalog, render_skill_catalog
from reqahe.infra.io import read_json, read_jsonl
from reqahe.runtime import runner
from reqahe.runtime.dataset import Scenario
from reqahe.runtime.interviewer import SeedInterviewer
from reqahe.runtime.skill_router import route_relevant_skills, skill_router_config_from_dict


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


def _write_skill(
    workspace: Path,
    skill_id: str,
    *,
    description: str = "Test skill",
    applies_when: list[str] | None = None,
    procedure: str = "SECRET_PROCEDURE",
    enabled: bool = True,
) -> None:
    skill_dir = workspace / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    applies = applies_when or [f"Use when probing {skill_id}"]
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f'id: "{skill_id}"\n'
        f'name: "{skill_id}"\n'
        "version: 1\n"
        f"enabled: {'true' if enabled else 'false'}\n"
        f'intent: "{description}"\n'
        "scope:\n"
        '  - "Test scenarios"\n'
        "use_when:\n"
        + "".join(f'  - "{item}"\n' for item in applies)
        + "avoid_when:\n"
        '  - "Never when unrelated"\n'
        "risk_notes:\n"
        '  - "Overuse may narrow questioning."\n'
        "---\n"
        "# Skill\n"
        "Use in tests.\n"
        f"{procedure}\n",
        encoding="utf-8",
    )


def test_load_harness_text_skills_contains_catalog_not_full_body(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "probe-skill", procedure="SECRET_PROCEDURE")

    harness = load_harness_text(workspace)

    assert "probe-skill" in harness["skills"]
    assert "Description:" not in harness["skills"]
    assert "Intent:" in harness["skills"]
    assert "Use when:" in harness["skills"]
    assert "SECRET_PROCEDURE" not in harness["skills"]
    assert "Question Pattern" not in harness["skills"]


def test_load_selected_skill_text_loads_only_selected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a", procedure="PROCEDURE_A")
    _write_skill(workspace, "skill-b", procedure="PROCEDURE_B")

    selected = load_selected_skill_text(workspace, ["skill-a"])

    assert "PROCEDURE_A" in selected
    assert "PROCEDURE_B" not in selected


def test_load_skill_catalog_skips_disabled_and_invalid(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "active-skill", enabled=True)
    _write_skill(workspace, "inactive-skill", enabled=False)

    catalog = load_skill_catalog(workspace)

    assert [item["skill_id"] for item in catalog] == ["active-skill"]


def test_render_skill_catalog_excludes_procedure_sections() -> None:
    catalog = [
        {
            "skill_id": "x",
            "name": "X",
            "intent": "desc",
            "scope": ["when"],
            "use_when": ["when"],
            "avoid_when": ["avoid"],
            "risk_notes": ["risk"],
            "relative_path": "skills/x/SKILL.md",
        }
    ]

    rendered = render_skill_catalog(catalog)

    assert "Procedure" not in rendered
    assert "desc" in rendered


def test_seed_interviewer_routes_then_generates_action(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a", procedure="PROCEDURE_A")
    _write_skill(workspace, "skill-b", procedure="PROCEDURE_B")

    class FakeLLM:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def json_chat(self, messages, **kwargs) -> dict:
            purpose = kwargs.get("purpose", "")
            self.calls.append({"purpose": purpose, "messages": messages})
            if purpose == "skill relevance routing":
                return {
                    "selected_skill_ids": ["skill-a"],
                    "decisions": [
                        {
                            "skill_id": "skill-a",
                            "relevance": 0.9,
                            "decision": "select",
                            "reason": "directly applies",
                        }
                    ],
                }
            return {
                "thought_summary": "ask",
                "action": "ask_question",
                "question": "What data should it show?",
                "finish_summary": "",
            }

    llm = FakeLLM()
    harness = load_harness_text(workspace)
    agent = SeedInterviewer(
        harness,
        llm,  # type: ignore[arg-type]
        model="m",
        workspace_dir=workspace,
        skill_router_config={"enabled": True},
        skill_router_model="router-m",
        skill_routing_event_log_path=tmp_path / "skill_routing_events.jsonl",
    )

    action = agent.next_action("Build a dashboard", [], ["avoid ambiguity"], max_turns=8, turn_index=0)
    prompt = agent._build_prompt(
        "Build a dashboard",
        [],
        ["avoid ambiguity"],
        max_turns=8,
        selected_skill_details=load_selected_skill_text(workspace, ["skill-a"]),
        skill_routing_summary=agent.last_routing,
        turn_index=0,
    )

    assert action["action"] == "ask_question"
    assert len(llm.calls) == 2
    assert llm.calls[0]["purpose"] == "skill relevance routing"
    assert llm.calls[1]["purpose"] == "interviewer action generation"
    assert "# Skill Catalog" in prompt
    assert "# Selected Skill Details" in prompt
    assert "PROCEDURE_A" in prompt
    assert "PROCEDURE_B" not in prompt
    assert "PROCEDURE_B" not in harness["skills"]


def test_router_failure_does_not_load_all_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a", procedure="PROCEDURE_A")
    _write_skill(workspace, "skill-b", procedure="PROCEDURE_B")

    class FailingRouterLLM:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def json_chat(self, messages, **kwargs) -> dict:
            purpose = kwargs.get("purpose", "")
            if purpose == "skill relevance routing":
                if self.mode == "error":
                    raise RuntimeError("router failed")
                return {"selected_skill_ids": "not-a-list"}
            return {
                "action": "ask_question",
                "question": "What data should it show?",
                "finish_summary": "",
            }

    for mode in ("error", "invalid"):
        llm = FailingRouterLLM(mode)
        harness = load_harness_text(workspace)
        agent = SeedInterviewer(
            harness,
            llm,  # type: ignore[arg-type]
            model="m",
            workspace_dir=workspace,
            skill_router_config={"enabled": True},
        )
        agent.next_action("Build a dashboard", [], [], max_turns=8, turn_index=0)
        assert agent.last_routing["selected_skill_ids"] == []
        if mode == "invalid":
            assert agent.last_routing["router_error"]
            assert "invalid router schema" in agent.last_routing["router_error"]
        action_prompt = agent._build_prompt(
            "Build a dashboard",
            [],
            [],
            max_turns=8,
            selected_skill_details="(none selected by skill router)",
            skill_routing_summary=agent.last_routing,
        )
        assert "PROCEDURE_A" not in action_prompt
        assert "PROCEDURE_B" not in action_prompt


def test_invalid_router_schema_sets_router_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a")
    catalog = load_skill_catalog(workspace)
    config = skill_router_config_from_dict({"enabled": True})

    class FakeLLM:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json_chat(self, messages, **kwargs) -> dict:
            return self.payload

    result = route_relevant_skills(
        llm=FakeLLM({"selected_skill_ids": "not-a-list", "decisions": []}),  # type: ignore[arg-type]
        model="m",
        catalog=catalog,
        initial_req="Build a dashboard",
        history=[],
        warnings=[],
        turn_index=0,
        max_turns=8,
        config=config,
        event_log_path=tmp_path / "events_selected.jsonl",
    )
    assert result["selected_skill_ids"] == []
    assert result["router_error"]
    assert "invalid router schema" in result["router_error"]
    assert "selected_skill_ids must be a list" in result["router_error"]
    events = read_jsonl(tmp_path / "events_selected.jsonl")
    assert events[0]["router_error"]

    result = route_relevant_skills(
        llm=FakeLLM({"selected_skill_ids": [], "decisions": "not-a-list"}),  # type: ignore[arg-type]
        model="m",
        catalog=catalog,
        initial_req="Build a dashboard",
        history=[],
        warnings=[],
        turn_index=0,
        max_turns=8,
        config=config,
        event_log_path=tmp_path / "events_decisions.jsonl",
    )
    assert result["selected_skill_ids"] == []
    assert result["router_error"]
    assert "invalid router schema" in result["router_error"]
    assert "decisions must be a list" in result["router_error"]


def test_route_relevant_skills_filters_by_relevance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a")
    _write_skill(workspace, "skill-b")
    catalog = load_skill_catalog(workspace)

    class FakeLLM:
        def json_chat(self, messages, **kwargs) -> dict:
            return {
                "selected_skill_ids": ["skill-a", "skill-b"],
                "decisions": [
                    {"skill_id": "skill-a", "relevance": 0.9, "decision": "select", "reason": "ok"},
                    {"skill_id": "skill-b", "relevance": 0.2, "decision": "reject", "reason": "low"},
                ],
            }

    result = route_relevant_skills(
        llm=FakeLLM(),  # type: ignore[arg-type]
        model="m",
        catalog=catalog,
        initial_req="Build a dashboard",
        history=[],
        warnings=[],
        turn_index=0,
        max_turns=8,
        config=skill_router_config_from_dict({"enabled": True, "min_relevance": 0.45}),
        event_log_path=tmp_path / "events.jsonl",
    )

    assert result["selected_skill_ids"] == ["skill-a"]
    events = read_jsonl(tmp_path / "events.jsonl")
    assert events[0]["selected_skill_ids"] == ["skill-a"]


def test_run_single_task_writes_skill_routing_events(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a", procedure="PROCEDURE_A")
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
            if purpose == "skill relevance routing":
                return {
                    "selected_skill_ids": ["skill-a"],
                    "decisions": [
                        {"skill_id": "skill-a", "relevance": 0.9, "decision": "select", "reason": "ok"}
                    ],
                }
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
        initial_req="Build a dashboard",
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
        skill_router_config={"enabled": True, "log_events": True},
        skill_router_model="router-m",
    )
    trace_dir = tmp_path / result["trace_dir"]
    trace = read_json(trace_dir / "clean_trace.json")
    events = read_jsonl(trace_dir / "skill_routing_events.jsonl")

    assert events
    assert trace["skill_routing_events"]
    assert trace["skill_routing_events"][0]["selected_skill_ids"] == ["skill-a"]

    agent_prompts = read_jsonl(trace_dir / "agent_prompts.jsonl")
    assert agent_prompts
    record = agent_prompts[0]
    assert record["skill_router"]["selected_skill_ids"] == ["skill-a"]
    assert record["skill_router"]["selected_skill_count"] == 1
    assert record["skill_router"]["catalog_size"] == 1
    assert "prompt_digest" in record
    assert record["prompt_digest"]["selected_skill_details_present"] is True
    assert "PROCEDURE_A" not in str(record)
    assert "PROCEDURE_B" not in str(record)


def test_invalid_router_schema_recorded_in_agent_prompts(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace_with_code_agent(workspace)
    _write_skill(workspace, "skill-a", procedure="PROCEDURE_A")
    _write_skill(workspace, "skill-b", procedure="PROCEDURE_B")
    harness = load_harness_text(workspace)

    class FakeSession:
        evaluation_mode = "reqelicitgym_judge_user"

        def __init__(self, *args, **kwargs) -> None:
            pass

        def step(self, utterance: str) -> dict:
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
            if purpose == "skill relevance routing":
                return {"selected_skill_ids": "not-a-list"}
            return {
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Done.",
            }

    monkeypatch.setattr(runner, "ReqElicitSession", FakeSession)
    scenario = Scenario(
        scenario_id="train_002",
        name="train_002",
        app_type="dashboard",
        initial_req="Build a dashboard",
        implicit_requirements=[{"id": "IR1", "Aspect": "Content"}],
        final_requirements=[],
        raw={},
    )

    result = runner.run_single_task(
        scenario,
        harness,
        workspace,
        tmp_path / "task_invalid_router",
        FakeLLM(),  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=1,
        reflection_mode="observe",
        agent_name="seed_freeform",
        skill_router_config={"enabled": True, "log_events": True},
        skill_router_model="router-m",
    )
    trace_dir = tmp_path / result["trace_dir"]
    record = read_jsonl(trace_dir / "agent_prompts.jsonl")[0]

    assert record["skill_router"]["selected_skill_ids"] == []
    assert record["skill_router"]["router_error"]
    assert "invalid router schema" in record["skill_router"]["router_error"]
    assert record["prompt_digest"]["selected_skill_count"] == 0
    assert record["prompt_digest"]["selected_skill_details_is_none"] is True
    assert "PROCEDURE_A" not in str(record)
    assert "PROCEDURE_B" not in str(record)
