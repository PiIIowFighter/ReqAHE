from pathlib import Path

from reqahe.refiner.pipeline import _sanitize_fix_plan_or_drop_invalid


DECLARED_COMPONENTS = {"system_prompt", "skills", "self_reflection"}


def _sanitize_item(item: dict) -> dict:
    sanitized = _sanitize_fix_plan_or_drop_invalid({"fix_plan": [item]}, DECLARED_COMPONENTS)
    return sanitized["fix_plan"][0]


def test_skill_router_target_hint_normalizes_to_system_prompt() -> None:
    item = _sanitize_item(
        {
            "id": "CH1",
            "component": "skills",
            "artifact_type": "skill_markdown_v1",
            "operation_intent": "update",
            "target_file_hint": "skill_router",
            "diagnosis_ref": "D1",
            "rationale": "Router selection rule is unclear.",
        }
    )

    assert item["component"] == "system_prompt"
    assert item["artifact_type"] == "system_prompt_section_v1"
    assert item["target_file_hint"] == "system_prompt.md"


def test_skill_router_component_and_target_normalize_to_system_prompt() -> None:
    item = _sanitize_item(
        {
            "id": "CH1",
            "component": "skill_router",
            "artifact_type": "skill_markdown_v1",
            "operation_intent": "update",
            "target_file_hint": "skill_router",
            "diagnosis_ref": "D1",
            "rationale": "Router selection rule is unclear.",
        }
    )

    assert item["component"] == "system_prompt"
    assert item["artifact_type"] == "system_prompt_section_v1"
    assert item["target_file_hint"] == "system_prompt.md"


def test_memory_router_target_hint_normalizes_to_system_prompt() -> None:
    item = _sanitize_item(
        {
            "id": "CH1",
            "component": "memory_router",
            "operation_intent": "update",
            "target_file_hint": "memory_router",
            "diagnosis_ref": "D1",
            "rationale": "Memory routing guidance is unclear.",
        }
    )

    assert item["component"] == "system_prompt"
    assert item["artifact_type"] == "system_prompt_section_v1"
    assert item["target_file_hint"] == "system_prompt.md"


def test_prompts_do_not_list_memory_as_final_component() -> None:
    root = Path(__file__).resolve().parents[1]
    prompt_paths = [
        root / "src" / "reqahe" / "diagnoser" / "prompts" / "localize_component.md",
        root / "src" / "reqahe" / "refiner" / "prompts" / "make_fix_plan.md",
        root / "src" / "reqahe" / "refiner" / "prompts" / "generate_edits_and_validate.md",
    ]
    prompt_text = "\n".join(path.read_text(encoding="utf-8") for path in prompt_paths)

    forbidden_patterns = [
        "system_prompt | skills | memory | self_reflection",
        "skills | system_prompt | memory | self_reflection",
        "Allowed final components are:\n- system_prompt\n- skills\n- self_reflection\n- memory",
        "Allowed final components are:\n\n- system_prompt\n- skills\n- self_reflection\n- memory",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in prompt_text

    assert "Memory is evidence produced by the memorizer" in prompt_text
    assert "Do not localize failures to memory" in prompt_text
