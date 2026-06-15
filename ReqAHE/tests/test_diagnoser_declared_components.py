from pathlib import Path

from reqahe.diagnoser.pipeline import build_component_localization_payload, load_declared_components


def test_load_declared_components_excludes_memory_by_default(tmp_path: Path) -> None:
    harness_dir = _harness_with_components(tmp_path)

    components = load_declared_components(harness_dir)
    names = {item["name"] for item in components}

    assert "system_prompt" in names
    assert "skills" in names
    assert "self_reflection" in names
    assert "memory" not in names


def test_load_declared_components_can_include_memory_for_debug(tmp_path: Path) -> None:
    harness_dir = _harness_with_components(tmp_path)

    components = load_declared_components(harness_dir, include_non_evolvable=True)
    names = {item["name"] for item in components}

    assert "memory" in names


def test_component_localization_payload_excludes_memory(tmp_path: Path) -> None:
    harness_dir = _harness_with_components(tmp_path)
    declared_components = load_declared_components(harness_dir)

    payload = build_component_localization_payload({"diagnosis_summary": "needs sharper probing"}, declared_components)
    names = {item["name"] for item in payload["declared_components"]}

    assert "memory" not in names


def _harness_with_components(tmp_path: Path) -> Path:
    harness_dir = tmp_path / "workspace"
    harness_dir.mkdir()
    (harness_dir / "system_prompt.md").write_text("system prompt\n", encoding="utf-8")
    for relative_path in ("skills/README.md", "memory/README.md", "self_reflection/README.md"):
        path = harness_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (harness_dir / "code_agent.yaml").write_text(
        "\n".join(
            [
                "name: test",
                "system_prompt: system_prompt.md",
                "skills:",
                "  - skills/README.md",
                "memory:",
                "  - memory/README.md",
                "self_reflection:",
                "  - self_reflection/README.md",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return harness_dir
