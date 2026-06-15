from pathlib import Path

import pytest
import yaml

from reqahe.harness.component_spec import allowed_component_names, component_allowed_paths, load_harness_component_specs
from reqahe.harness.workspace import load_harness_text, write_workspace_file
from reqahe.runtime.interviewer import SeedInterviewer, _validate_action


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_seed_harness_has_four_core_components() -> None:
    seed = PROJECT_ROOT / "harness_seed"

    assert (seed / "skills" / "README.md").exists()
    assert (seed / "memory" / "README.md").exists()
    assert (seed / "self_reflection" / "README.md").exists()
    assert (seed / "self_reflection" / "registry.yaml").exists()


def test_component_specs_are_loaded_from_code_agent_yaml() -> None:
    manifest = yaml.safe_load((PROJECT_ROOT / "harness_seed" / "code_agent.yaml").read_text(encoding="utf-8"))

    assert set(manifest) == {"name", "version", "system_prompt", "skills", "memory", "self_reflection"}
    seed = PROJECT_ROOT / "harness_seed"

    specs = load_harness_component_specs(seed)

    assert set(specs) == {"system_prompt", "skills", "memory", "self_reflection"}
    assert specs["system_prompt"].paths == ("system_prompt.md",)
    assert component_allowed_paths(seed)["skills"] == ("skills/README.md",)


def test_load_harness_text_returns_only_core_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "memory").mkdir()
    (workspace / "self_reflection").mkdir()
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n"
        "memory:\n  - memory/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "skills" / "README.md").write_text("skills", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("memory", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("reflection", encoding="utf-8")

    harness = load_harness_text(workspace)

    assert set(harness) == {"system_prompt", "skills", "memory", "self_reflection"}


def test_seed_interviewer_prompt_includes_core_sections() -> None:
    agent = SeedInterviewer(
        {
            "system_prompt": "system",
            "skills": "skills",
            "memory": "memory",
            "self_reflection": "reflection",
        },
        llm=object(),  # type: ignore[arg-type]
    )

    prompt = agent._build_prompt("Build a dashboard", [], ["avoid ambiguity"], max_turns=8)

    assert "# Initial Requirement" in prompt
    assert "# Turn Budget" in prompt
    assert "# Dialogue History" in prompt
    assert "# Skill Catalog" in prompt
    assert "# Selected Skill Details" in prompt
    assert "# Relevant Memory" in prompt
    assert "# Skill Routing Summary" in prompt
    assert "# Memory Catalog" not in prompt


def test_load_harness_text_reports_runtime_checks_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "self_reflection").mkdir(parents=True)
    (workspace / "skills").mkdir()
    (workspace / "memory").mkdir()
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "skills:\n  - skills/README.md\n"
        "memory:\n  - memory/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n  - self_reflection/registry.yaml\n",
        encoding="utf-8",
    )
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "registry.yaml").write_text(
        'version: "0.2"\n'
        "checks:\n"
        "  - id: generated_check\n"
        "    hook: question_candidate\n"
        "    file: generated_check/check.py\n"
        "    prompt: generated_check/PROMPT.md\n"
        "    applies_when: always\n"
        "    mode: warn\n"
        "    priority: 10\n",
        encoding="utf-8",
    )
    bundle = workspace / "self_reflection" / "generated_check"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "check.py").write_text(
        '"""\n'
        "component: self_reflection\n"
        "reflection_id: generated_check\n"
        "name: Generated Check\n"
        "version: 0.1\n"
        "hook: question_candidate\n"
        "mode: warn\n"
        '"""\n\n'
        "def check(candidate, state):\n"
        "    return []\n",
        encoding="utf-8",
    )
    (bundle / "PROMPT.md").write_text("Revise the candidate.", encoding="utf-8")
    (workspace / "self_reflection" / "unregistered_extra.md").write_text("Unregistered content.\n", encoding="utf-8")

    harness = load_harness_text(workspace)

    assert "generated_check" in harness["self_reflection"]
    assert "Unregistered content" not in harness["self_reflection"]


def test_validate_action_drops_unknown_extra_fields() -> None:
    action = _validate_action(
        {
            "thought_summary": "brief",
            "unknown_extra_field": "should not be preserved",
            "action": "ask_question",
            "question": "What data should the dashboard show?",
            "finish_summary": "",
        }
    )

    assert "unknown_extra_field" not in action
    assert action == {
        "action": "ask_question",
        "thought_summary": "brief",
        "question": "What data should the dashboard show?",
        "finish_summary": "",
    }


def test_validate_action_requires_finish_summary_for_finish_interview() -> None:
    with pytest.raises(RuntimeError, match="finish_interview requires finish_summary"):
        _validate_action(
            {
                "action": "finish_interview",
                "question": "",
                "finish_summary": "",
            }
        )


def test_write_workspace_file_rejects_undeclared_component_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: test\nsystem_prompt: system_prompt.md\nskills:\n  - skills/README.md\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")

    write_workspace_file(workspace, "skills/new.md", "ok")
    try:
        write_workspace_file(workspace, "unknown_dir/file.md", "bad")
    except PermissionError:
        pass
    else:
        raise AssertionError("expected undeclared path to be rejected")


def test_declared_extra_component_is_automatically_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "extra_guidance").mkdir(parents=True)
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "extra_guidance" / "README.md").write_text("extra_guidance", encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: test\n"
        "system_prompt: system_prompt.md\n"
        "extra_guidance:\n  - extra_guidance/README.md\n",
        encoding="utf-8",
    )

    assert "extra_guidance" in allowed_component_names(workspace)
    write_workspace_file(workspace, "extra_guidance/new.md", "new guidance item")
    assert (workspace / "extra_guidance" / "new.md").exists()
