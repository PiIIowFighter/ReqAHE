from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from reqahe.infra.io import write_json
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.refiner.pipeline import refine_harness
from tests.test_diagnoser_refiner_schema import (
    _skill_create_similarity_audit,
    _skill_self_validation,
    _valid_skill_lines,
    _valid_system_prompt,
)


def test_refiner_generate_edits_uses_limited_transport_attempts(tmp_path: Path, monkeypatch) -> None:
    iteration = tmp_path / "batch_001"
    workspace = iteration / "workspace_candidate"
    analysis = iteration / "analysis"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "self_reflection").mkdir()
    (workspace / "system_prompt.md").write_text(_valid_system_prompt(), encoding="utf-8")
    (workspace / "code_agent.yaml").write_text(
        "name: seed\nsystem_prompt: system_prompt.md\nskills:\n  - skills/README.md\n"
        "self_reflection:\n  - self_reflection/README.md\n",
        encoding="utf-8",
    )
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    write_json(
        analysis / "component_localization.json",
        {"localization_summary": "", "component_findings": [], "refiner_guidance": {}},
    )

    transport_calls: list[int | None] = []

    class FakeLLM:
        def json_chat(self, *args, **kwargs) -> dict:
            if kwargs.get("purpose") == "harness file edit generation":
                transport_calls.append(kwargs.get("transport_max_attempts"))
            if kwargs.get("purpose") == "harness fix plan selection":
                return {
                    "fix_plan": [
                        {
                            "fix_id": "F1",
                            "component": "skills",
                            "artifact_type": "skill_markdown_v1",
                            "operation_intent": "create",
                            "target_file_hint": "skills/new-skill/SKILL.md",
                            "evidence": ["RC1"],
                            "fix_summary": "add skill",
                            "expected_effect": "improve",
                            "risk": "low",
                        }
                    ],
                    "rationale": "skill",
                }
            return {
                "changes": [{"change_id": "C1", "fix_id": "F1", "component": "skills", "summary": "add"}],
                "file_edits": [
                    {
                        "relative_path": "skills/new-skill/SKILL.md",
                        "operation": "create",
                        "new_content": "\n".join(_valid_skill_lines("new-skill")),
                    }
                ],
                "schema_compliance": [
                    {
                        "component": "skills",
                        "schema_name": "skill_markdown_v1",
                        "new_or_updated_files": ["skills/new-skill/SKILL.md"],
                    }
                ],
                "refiner_rationale": "ok",
                "similarity_audit": _skill_create_similarity_audit(),
                "self_validation": _skill_self_validation(),
            }

    monkeypatch.setattr("reqahe.refiner.pipeline._commit_workspace", lambda *args, **kwargs: None)
    refine_harness(
        iteration,
        workspace,
        1,
        FakeLLM(),  # type: ignore[arg-type]
        "m",
        refiner_config={"transport_attempts": 2, "json_attempts": 2, "max_repair_attempts": 1},
    )
    assert transport_calls
    assert all(value == 2 for value in transport_calls)


def test_require_chat_respects_max_attempts_override() -> None:
    client = OpenAICompatibleClient(api_key="k", model="m", max_retries=8)
    calls = {"count": 0}

    def fake_chat(*args, **kwargs):
        calls["count"] += 1
        from reqahe.infra.llm_client import LLMResult

        return LLMResult(False, "", "connection error")

    with patch.object(client, "chat", side_effect=fake_chat):
        with pytest.raises(RuntimeError):
            client.require_chat([{"role": "user", "content": "hi"}], max_attempts=2)
    assert calls["count"] == 2
