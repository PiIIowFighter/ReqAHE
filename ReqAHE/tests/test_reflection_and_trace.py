from __future__ import annotations

import json
from pathlib import Path

from reqahe.harness.component_schema import (
    REFLECTION_HOOKS,
    validate_component_file,
    validate_reflection_registry,
    validate_workspace_preview,
)
from reqahe.infra.io import read_json, read_jsonl
from reqahe.diagnoser.pipeline import sanitize_trace_for_diagnoser
from reqahe.runtime import runner
from reqahe.runtime.dataset import Scenario
from reqahe.runtime.reflection import (
    ALLOWED_HOOKS,
    ReflectionRuntime,
    SUPPORTED_HOOKS,
    format_reflection_retry_feedback,
    is_retryable_reflection_event,
)


NEAR_DUPLICATE_CHECK = '''"""
component: self_reflection
reflection_id: near_duplicate_question
name: Near Duplicate Question
version: 0.1
hook: question_candidate
mode: warn
"""

from __future__ import annotations

import difflib
import re
from typing import Any


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\\s]", " ", text)
    stopwords = {
        "what", "which", "how", "do", "does", "you", "the",
        "a", "an", "to", "for", "of", "in", "on", "is", "are"
    }
    return " ".join(token for token in text.split() if token not in stopwords)


def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    if candidate.get("kind") != "question":
        return []

    question = str(candidate.get("text") or "").strip()
    if not question:
        return []

    for idx, prev in enumerate(state.get("previous_questions") or []):
        score = difflib.SequenceMatcher(
            None,
            _normalize(question),
            _normalize(str(prev)),
        ).ratio()
        if score >= 0.86:
            return [
                {
                    "type": "near_duplicate_question",
                    "message": (
                        f"The candidate question is too similar to a previous question "
                        f"at turn {idx + 1}. similarity={score:.2f}"
                    ),
                    "suggestion": (
                        "Ask about a different unresolved requirement aspect instead "
                        "of repeating the same concern."
                    ),
                    "details": {
                        "matched_turn": idx + 1,
                        "matched_question": str(prev),
                        "candidate_question": question,
                        "similarity": score,
                    },
                }
            ]

    return []
'''

NEAR_DUPLICATE_PROMPT = """You generated a question that appears too similar to a previous question.

Revise the candidate question in this same turn:
- Do not ask the same requirement concern again.
- Ask about a different unresolved requirement aspect.
- Ask exactly one focused question.
- Do not mention hidden evaluation requirements.
"""


def _bundle_registry_yaml(
    reflection_id: str,
    *,
    hook: str = "question_candidate",
    mode: str = "warn",
    applies_when: str = "always",
) -> str:
    return (
        'version: "0.2"\n'
        "checks:\n"
        f"  - id: {reflection_id}\n"
        f"    hook: {hook}\n"
        f"    file: {reflection_id}/check.py\n"
        f"    prompt: {reflection_id}/PROMPT.md\n"
        f"    applies_when: {applies_when}\n"
        f"    mode: {mode}\n"
        "    priority: 10\n"
    )


def _write_bundle_workspace(
    workspace: Path,
    reflection_id: str,
    *,
    hook: str = "question_candidate",
    mode: str = "warn",
    check_body: str | None = None,
    prompt_body: str | None = None,
) -> None:
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "self_reflection").mkdir(exist_ok=True)
    (workspace / "system_prompt.md").write_text("system", encoding="utf-8")
    (workspace / "skills" / "README.md").write_text("", encoding="utf-8")
    (workspace / "memory" / "README.md").write_text("", encoding="utf-8")
    (workspace / "self_reflection" / "README.md").write_text("", encoding="utf-8")
    bundle = workspace / "self_reflection" / reflection_id
    bundle.mkdir(parents=True, exist_ok=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        _bundle_registry_yaml(reflection_id, hook=hook, mode=mode),
        encoding="utf-8",
    )
    if check_body is None:
        check_body = _generated_check_py(reflection_id, hook=hook, mode=mode)
    (bundle / "check.py").write_text(check_body, encoding="utf-8")
    (bundle / "PROMPT.md").write_text(prompt_body or "Revise the candidate in this same turn.", encoding="utf-8")


def _generated_check_py(
    reflection_id: str,
    *,
    hook: str = "question_candidate",
    mode: str = "warn",
    trigger: str = "BAD",
) -> str:
    return (
        '"""\n'
        "component: self_reflection\n"
        f"reflection_id: {reflection_id}\n"
        f"name: {reflection_id.replace('_', ' ').title()}\n"
        "version: 0.1\n"
        f"hook: {hook}\n"
        f"mode: {mode}\n"
        '"""\n\n'
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
        f"    text = str(candidate.get('text') or '')\n"
        f"    if '{trigger}' in text:\n"
        "        return [{\n"
        f"            'type': '{reflection_id}_event',\n"
        "            'message': 'bad candidate detected',\n"
        "            'suggestion': 'ask one focused question.',\n"
        "        }]\n"
        "    return []\n"
    )


def test_supported_hooks_are_candidate_only() -> None:
    assert ALLOWED_HOOKS == {"question_candidate", "finish_candidate"}
    assert SUPPORTED_HOOKS == ALLOWED_HOOKS
    assert REFLECTION_HOOKS == ALLOWED_HOOKS


def test_reflection_runtime_initial_seed_has_no_builtin_events(tmp_path: Path) -> None:
    (tmp_path / "self_reflection").mkdir()
    (tmp_path / "self_reflection" / "registry.yaml").write_text(
        'version: "0.2"\nchecks: []\n',
        encoding="utf-8",
    )
    runtime = ReflectionRuntime(tmp_path, mode="warn")
    candidate = {
        "kind": "question",
        "text": "What data should it show? And what else should it do?",
        "raw_action": {"action": "ask_question", "question": "What data should it show? And what else should it do?"},
        "turn_index": 1,
    }
    state = {
        "turn_index": 1,
        "max_turns": 8,
        "previous_questions": ["What data should it show? And what else should it do?"],
    }

    events = runtime.check(candidate, state, hook="question_candidate")

    assert events == []


def test_question_candidate_hook_executes_registered_check(tmp_path: Path) -> None:
    _write_bundle_workspace(tmp_path, "generated_check", hook="question_candidate")
    candidate = {
        "kind": "question",
        "text": "What data?",
        "raw_action": {"action": "ask_question", "question": "What data?"},
        "turn_index": 0,
    }
    events = ReflectionRuntime(tmp_path, mode="warn").check(candidate, {"turn_index": 0}, hook="question_candidate")

    assert events == []


def test_finish_candidate_hook_executes_registered_check(tmp_path: Path) -> None:
    _write_bundle_workspace(
        tmp_path,
        "finish_check",
        hook="finish_candidate",
        check_body=_generated_check_py("finish_check", hook="finish_candidate", trigger="TOO_EARLY"),
    )
    candidate = {
        "kind": "finish",
        "text": "TOO_EARLY finish",
        "raw_action": {"action": "finish_interview", "finish_summary": "TOO_EARLY finish"},
        "turn_index": 1,
    }
    events = ReflectionRuntime(tmp_path, mode="warn").check(candidate, {"turn_index": 1}, hook="finish_candidate")

    assert [event["type"] for event in events] == ["finish_check_event"]
    assert events[0]["candidate_kind"] == "finish"
    assert events[0]["candidate_text"] == "TOO_EARLY finish"


def test_reflection_runtime_executes_only_registered_check(tmp_path: Path) -> None:
    _write_bundle_workspace(
        tmp_path,
        "generated_check",
        check_body=_generated_check_py("generated_check", trigger="__ALWAYS_WARN__"),
    )
    (tmp_path / "self_reflection" / "generated_check" / "check.py").write_text(
        _generated_check_py("generated_check").replace("BAD", "__ALWAYS_WARN__"),
        encoding="utf-8",
    )
    candidate = {
        "kind": "question",
        "text": "__ALWAYS_WARN__",
        "raw_action": {"action": "ask_question", "question": "__ALWAYS_WARN__"},
        "turn_index": 0,
    }
    events = ReflectionRuntime(tmp_path, mode="warn").check(candidate, {"turn_index": 0}, hook="question_candidate")

    assert [event["type"] for event in events] == ["generated_check_event"]
    assert events[0]["check_id"] == "generated_check"
    assert events[0]["source_file"] == "self_reflection/generated_check/check.py"
    assert events[0]["prompt_file"] == "self_reflection/generated_check/PROMPT.md"


def test_reflection_runtime_source_has_no_hard_coded_quality_checks() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "reqahe" / "runtime" / "reflection.py").read_text(
        encoding="utf-8"
    )

    for needle in [
        "empty_question",
        "multi_question_in_one_turn",
        "ambiguous_reference_risk",
        "duplicate_question",
        "premature_finish",
        "generic_broad_question",
    ]:
        assert needle not in source


def test_run_single_task_writes_enhanced_clean_trace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(
        workspace,
        "generated_check",
        mode="observe",
        check_body=(
            '"""\n'
            "component: self_reflection\n"
            "reflection_id: generated_check\n"
            "name: Generated Check\n"
            "version: 0.1\n"
            "hook: question_candidate\n"
            "mode: observe\n"
            '"""\n\n'
            "from __future__ import annotations\n"
            "from typing import Any\n\n"
            "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:\n"
            "    return [{'check_id': 'generated_check', 'type': 'generated_event', 'severity': 'warn', 'message': 'generated'}]\n"
        ),
    )
    harness = {
        "system_prompt": "system",
        "skills": "",
        "memory": "",
        "self_reflection": "",
    }

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
            if self.calls == 1:
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
        implicit_requirements=[
            {"id": "IR1", "Aspect": "Content"},
            {"id": "IR2", "Aspect": "Style"},
        ],
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
        reflection_mode="warn",
        agent_name="seed_freeform",
    )
    trace = read_json((tmp_path / result["trace_dir"]) / "clean_trace.json")

    assert trace["missed_requirement_ids"] == ["IR2"]
    assert trace["hit_sequence"] == [1, 0]
    assert "self_reflection_events" in trace
    assert trace["self_reflection_events"][0]["type"] == "generated_event"
    assert trace["turns"][0]["reflection_attempts"][0]["candidate_kind"] == "question"
    assert trace["turns"][0]["reflection_attempts"][0]["candidate_text"] == "What data should it show?"


def test_runner_failure_tags_do_not_use_hard_coded_language_rules() -> None:
    metrics = {
        "IRE": 0.8,
        "TKQR": 0.8,
        "duplicate_question_rate": 1.0,
        "broad_question_rate": 1.0,
        "early_finish": True,
        "type_coverage": {"interaction": 1, "content": 1, "style": 1},
    }
    turns = [{"judgement": {"is_relevant_to_implied_requirements": True}}]

    tags = runner._failure_tags(metrics, turns, [{"type": "generated_language_event"}])

    assert "generated_language_event" in tags
    assert "generic_broad_question" not in tags
    assert "premature_finish" not in tags


def _sample_scenario() -> Scenario:
    return Scenario(
        scenario_id="train_001",
        name="train_001",
        app_type="dashboard",
        initial_req="Build a dashboard",
        implicit_requirements=[
            {"id": "IR1", "Aspect": "Content"},
            {"id": "IR2", "Aspect": "Style"},
        ],
        final_requirements=[],
        raw={},
    )


class FakeFinishSession:
    evaluation_mode = "reqelicitgym_judge_user"

    def __init__(self) -> None:
        self.utterances: list[str] = []

    def step(self, utterance: str) -> dict:
        self.utterances.append(utterance)
        if len(self.utterances) == 1:
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


class SequenceLLM:
    def __init__(self, actions: list[dict]) -> None:
        self.actions = actions
        self.calls = 0

    def json_chat(self, *args, **kwargs) -> dict:
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        return dict(self.actions[idx])


def test_is_retryable_reflection_event_respects_runtime_and_modes() -> None:
    event = {"mode": "warn", "message": "bad"}
    assert is_retryable_reflection_event(event, runtime_mode="warn", retry_on_modes={"warn", "enforce"})
    assert not is_retryable_reflection_event(event, runtime_mode="observe", retry_on_modes={"warn", "enforce"})
    assert not is_retryable_reflection_event({"mode": "observe", "message": "x"}, runtime_mode="warn", retry_on_modes={"warn"})


def test_format_reflection_retry_feedback_includes_prompt_md(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", prompt_body="Repair the candidate now.")
    feedback = format_reflection_retry_feedback(
        [
            {
                "check_id": "bad_action_check",
                "mode": "warn",
                "message": "bad candidate detected",
                "suggestion": "ask one focused question.",
                "candidate_kind": "question",
                "candidate_text": "BAD question?",
                "prompt_file": "self_reflection/bad_action_check/PROMPT.md",
            }
        ],
        workspace,
    )
    assert feedback[0].startswith("RETRY_THIS_TURN self_reflection bad_action_check/warn:")
    assert "Candidate kind: question" in feedback[0]
    assert "BAD question?" in feedback[0]
    assert "Repair the candidate now." in feedback[0]
    assert "Regenerate the current action only." in feedback[0]


def test_warn_triggers_same_turn_retry(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="warn")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "good",
                "action": "ask_question",
                "question": "What data should it show?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Revenue dashboard.",
            },
        ]
    )
    session = FakeFinishSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    result = runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="warn",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 1, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )
    trace_dir = tmp_path / result["trace_dir"]
    prompts = read_jsonl(trace_dir / "agent_prompts.jsonl")
    trace = read_json(trace_dir / "clean_trace.json")

    assert llm.calls == 3
    assert session.utterances == ["What data should it show?", "Revenue dashboard."]
    assert len([record for record in prompts if record["turn_index"] == 0]) == 2
    assert prompts[0]["reflection_attempt"] == 0
    assert prompts[1]["reflection_attempt"] == 1
    assert prompts[1]["reflection_retry_feedback"]
    actions = read_jsonl(trace_dir / "agent_actions.jsonl")
    assert actions[0]["reflection_attempts"][0]["discarded"] is True
    assert actions[0]["reflection_attempts"][0]["candidate_kind"] == "question"
    assert trace["turns"][0]["reflection_attempts"][0]["discarded"] is True
    turn_events = trace["turns"][0]["self_reflection_events"]
    assert any(event.get("discarded_action") is True for event in turn_events)
    assert actions[0]["all_self_reflection_events"] == turn_events
    assert actions[0]["self_reflection_events"] == actions[0]["reflection_attempts"][-1]["self_reflection_events"]
    logged_events = read_jsonl(trace_dir / "self_reflection_events.jsonl")
    assert any(
        event.get("reflection_attempt") == 0 and event.get("discarded_action") is True for event in logged_events
    )
    sanitized = sanitize_trace_for_diagnoser(trace)
    assert sanitized["turns"][0]["reflection_attempts"][0]["discarded"] is True
    assert sanitized["turns"][0]["accepted_despite_reflection_warning"] is False


def test_observe_mode_does_not_trigger_retry(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="observe")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Done.",
            },
        ]
    )
    session = FakeFinishSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="warn",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 1, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )

    assert llm.calls == 2
    assert session.utterances[0] == "BAD question?"


def test_global_observe_mode_does_not_trigger_retry(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="warn")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Done.",
            },
        ]
    )
    session = FakeFinishSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="observe",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 1, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )

    assert llm.calls == 2
    assert session.utterances[0] == "BAD question?"


def test_max_retries_exceeded_executes_last_action(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="warn")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad1",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "bad2",
                "action": "ask_question",
                "question": "BAD again?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Done.",
            },
        ]
    )
    session = FakeFinishSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    result = runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="warn",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 1, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )
    trace_dir = tmp_path / result["trace_dir"]
    actions = read_jsonl(trace_dir / "agent_actions.jsonl")

    assert llm.calls == 3
    assert session.utterances[0] == "BAD again?"
    assert actions[0]["accepted_despite_reflection_warning"] is True


def test_accepted_despite_does_not_pollute_next_turn_prompt(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="warn")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Done.",
            },
        ]
    )
    session = FakeFinishSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    result = runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=4,
        reflection_mode="warn",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 0, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )
    trace_dir = tmp_path / result["trace_dir"]
    prompts = read_jsonl(trace_dir / "agent_prompts.jsonl")
    actions = read_jsonl(trace_dir / "agent_actions.jsonl")
    trace = read_json(trace_dir / "clean_trace.json")
    logged_events = read_jsonl(trace_dir / "self_reflection_events.jsonl")

    assert actions[0]["accepted_despite_reflection_warning"] is True
    assert trace["turns"][0]["accepted_despite_reflection_warning"] is True
    assert trace["turns"][0]["self_reflection_events"]
    assert logged_events
    assert any(event.get("type") == "bad_action_check_event" for event in logged_events)

    next_turn_prompts = [record for record in prompts if record["turn_index"] == 1]
    assert next_turn_prompts
    for record in next_turn_prompts:
        joined = json.dumps(record, ensure_ascii=False)
        assert "RETRY_THIS_TURN" not in joined
        for warning in record.get("warnings") or []:
            assert "RETRY_THIS_TURN" not in warning
        for feedback in record.get("reflection_retry_feedback") or []:
            assert "RETRY_THIS_TURN" not in feedback


def test_retry_does_not_consume_turn_budget(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "bad_action_check", mode="warn")
    harness = {"system_prompt": "system", "skills": "", "memory": "", "self_reflection": ""}
    llm = SequenceLLM(
        [
            {
                "thought_summary": "bad",
                "action": "ask_question",
                "question": "BAD question?",
                "finish_summary": "",
            },
            {
                "thought_summary": "finish",
                "action": "finish_interview",
                "question": "",
                "finish_summary": "Revenue dashboard summary.",
            },
        ]
    )

    class SingleStepSession:
        evaluation_mode = "reqelicitgym_judge_user"

        def __init__(self) -> None:
            self.calls = 0

        def step(self, utterance: str) -> dict:
            self.calls += 1
            return {
                "user_response": "Done.",
                "judgement": {
                    "is_relevant_to_implied_requirements": False,
                    "elicited_requirement_ids": [],
                    "action_type": "finish",
                },
            }

    session = SingleStepSession()
    monkeypatch.setattr(runner, "ReqElicitSession", lambda *args, **kwargs: session)

    result = runner.run_single_task(
        _sample_scenario(),
        harness,
        workspace,
        tmp_path / "task",
        llm,  # type: ignore[arg-type]
        llm_config={},
        reqelicitgym_root=tmp_path,
        interviewer_model="m",
        judge_model="m",
        user_model="m",
        user_answer_quality="high",
        max_turns=1,
        reflection_mode="warn",
        agent_name="seed_freeform",
        self_reflection_config={"max_retries": 1, "retry_on_modes": ["warn", "enforce"], "max_feedback_events": 3},
    )
    trace = read_json((tmp_path / result["trace_dir"]) / "clean_trace.json")

    assert llm.calls == 2
    assert session.calls == 1
    assert len(trace["turns"]) == 1
    assert trace["turns"][0]["action"] == "finish_interview"


def test_candidate_from_action_maps_supported_actions() -> None:
    question = runner.candidate_from_action({"action": "ask_question", "question": "Q?"}, 2)
    finish = runner.candidate_from_action({"action": "finish_interview", "finish_summary": "Done."}, 3)
    assert question["kind"] == "question"
    assert question["text"] == "Q?"
    assert finish["kind"] == "finish"
    assert finish["text"] == "Done."


def test_near_duplicate_question_bundle_example(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundle = workspace / "self_reflection" / "near_duplicate_question"
    bundle.mkdir(parents=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        _bundle_registry_yaml("near_duplicate_question"),
        encoding="utf-8",
    )
    (bundle / "check.py").write_text(NEAR_DUPLICATE_CHECK, encoding="utf-8")
    (bundle / "PROMPT.md").write_text(NEAR_DUPLICATE_PROMPT, encoding="utf-8")

    candidate = {
        "kind": "question",
        "text": "What data should the dashboard show?",
        "raw_action": {"action": "ask_question", "question": "What data should the dashboard show?"},
        "turn_index": 1,
    }
    state = {"previous_questions": ["What data should the dashboard show"]}
    events = ReflectionRuntime(workspace, mode="warn").check(candidate, state, hook="question_candidate")
    assert events
    assert events[0]["type"] == "near_duplicate_question"


def test_invalid_hooks_are_rejected_in_registry() -> None:
    for hook in ("invalid_hook_name", "unexpected_hook", "removed_hook_a"):
        errors = validate_reflection_registry(
            "self_reflection/registry.yaml",
            (
                'version: "0.2"\n'
                "checks:\n"
                f"  - id: bad\n    hook: {hook}\n    file: bad/check.py\n    prompt: bad/PROMPT.md\n"
                "    applies_when: always\n    mode: warn\n"
            ),
            {
                "self_reflection/bad/check.py": _generated_check_py("bad", hook=hook),
                "self_reflection/bad/PROMPT.md": "ok",
            },
        )
        assert errors


def test_invalid_root_py_path_is_invalid() -> None:
    errors = validate_component_file(
        "self_reflection/foo.py",
        _generated_check_py("foo").replace("question_candidate", "invalid_hook"),
    )
    assert errors


def test_bundle_without_prompt_is_invalid() -> None:
    preview = {
        "self_reflection/near_duplicate_question/check.py": NEAR_DUPLICATE_CHECK,
        "self_reflection/registry.yaml": _bundle_registry_yaml("near_duplicate_question"),
    }
    errors = validate_workspace_preview(preview)
    assert any("PROMPT.md" in err for err in errors)


def test_registry_prompt_missing_file_is_invalid() -> None:
    errors = validate_reflection_registry(
        "self_reflection/registry.yaml",
        _bundle_registry_yaml("generated_check"),
        {"self_reflection/generated_check/check.py": _generated_check_py("generated_check")},
    )
    assert any("PROMPT.md" in err for err in errors)


def test_registry_id_must_match_bundle_folder() -> None:
    errors = validate_reflection_registry(
        "self_reflection/registry.yaml",
        (
            'version: "0.2"\n'
            "checks:\n"
            "  - id: wrong_id\n"
            "    hook: question_candidate\n"
            "    file: generated_check/check.py\n"
            "    prompt: generated_check/PROMPT.md\n"
            "    applies_when: always\n"
            "    mode: warn\n"
        ),
        {
            "self_reflection/generated_check/check.py": _generated_check_py("generated_check"),
            "self_reflection/generated_check/PROMPT.md": "ok",
        },
    )
    assert any("bundle folder name" in err for err in errors)


def test_hook_mismatch_skips_check(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_bundle_workspace(workspace, "generated_check", hook="question_candidate")
    candidate = {"kind": "finish", "text": "BAD finish", "raw_action": {}, "turn_index": 0}
    events = ReflectionRuntime(workspace, mode="warn").check(candidate, {"turn_index": 0, "max_turns": 4}, hook="finish_candidate")
    assert events == []


def test_applies_when_no_history_skips_check(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundle = workspace / "self_reflection" / "generated_check"
    bundle.mkdir(parents=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        _bundle_registry_yaml("generated_check", applies_when="has_history"),
        encoding="utf-8",
    )
    (bundle / "check.py").write_text(_generated_check_py("generated_check"), encoding="utf-8")
    (bundle / "PROMPT.md").write_text("ok", encoding="utf-8")
    candidate = {"kind": "question", "text": "BAD question?", "raw_action": {}, "turn_index": 0}
    runtime = ReflectionRuntime(workspace, mode="warn")
    skipped = runtime.check(candidate, {"turn_index": 0, "max_turns": 4, "history": []}, hook="question_candidate")
    assert skipped == []
    executed = runtime.check(
        candidate,
        {"turn_index": 1, "max_turns": 4, "history": [{"question": "Q1", "answer": "A1"}]},
        hook="question_candidate",
    )
    assert executed


def test_applies_when_early_turn_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundle = workspace / "self_reflection" / "generated_check"
    bundle.mkdir(parents=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        _bundle_registry_yaml("generated_check", applies_when="early_turn"),
        encoding="utf-8",
    )
    (bundle / "check.py").write_text(_generated_check_py("generated_check"), encoding="utf-8")
    (bundle / "PROMPT.md").write_text("ok", encoding="utf-8")
    candidate = {"kind": "question", "text": "BAD question?", "raw_action": {}, "turn_index": 0}
    runtime = ReflectionRuntime(workspace, mode="warn")
    early = runtime.check(candidate, {"turn_index": 0, "max_turns": 8}, hook="question_candidate")
    late = runtime.check(candidate, {"turn_index": 6, "max_turns": 8}, hook="question_candidate")
    assert early
    assert late == []


def test_invalid_applies_when_rejected_by_registry_validation() -> None:
    for condition in ("invalid_condition_name", "totally_unknown"):
        errors = validate_reflection_registry(
            "self_reflection/registry.yaml",
            _bundle_registry_yaml("generated_check", applies_when=condition),
            {
                "self_reflection/generated_check/check.py": _generated_check_py("generated_check"),
                "self_reflection/generated_check/PROMPT.md": "ok",
            },
        )
        assert any("unsupported applies_when" in err for err in errors)


def test_non_string_applies_when_rejected_by_registry_validation() -> None:
    for condition in (True, {"candidate": {"kind": "question"}}):
        errors = validate_reflection_registry(
            "self_reflection/registry.yaml",
            (
                'version: "0.2"\n'
                "checks:\n"
                "  - id: generated_check\n"
                "    hook: question_candidate\n"
                "    file: generated_check/check.py\n"
                "    prompt: generated_check/PROMPT.md\n"
                f"    applies_when: {condition!r}\n"
                "    mode: warn\n"
            ),
            {
                "self_reflection/generated_check/check.py": _generated_check_py("generated_check"),
                "self_reflection/generated_check/PROMPT.md": "ok",
            },
        )
        assert any("unsupported applies_when type" in err for err in errors)


def test_applies_when_candidate_is_finish_runs_for_finish_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundle = workspace / "self_reflection" / "generated_check"
    bundle.mkdir(parents=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        _bundle_registry_yaml("generated_check", hook="finish_candidate", applies_when="candidate_is_finish"),
        encoding="utf-8",
    )
    (bundle / "check.py").write_text(
        _generated_check_py("generated_check", hook="finish_candidate", trigger="BAD"),
        encoding="utf-8",
    )
    (bundle / "PROMPT.md").write_text("ok", encoding="utf-8")
    finish_candidate = {"kind": "finish", "text": "BAD finish summary", "raw_action": {}, "turn_index": 0}
    question_candidate = {"kind": "question", "text": "What data?", "raw_action": {}, "turn_index": 0}
    runtime = ReflectionRuntime(workspace, mode="warn")
    finish_events = runtime.check(
        finish_candidate,
        {"turn_index": 0, "max_turns": 4},
        hook="finish_candidate",
    )
    question_events = runtime.check(
        question_candidate,
        {"turn_index": 0, "max_turns": 4},
        hook="finish_candidate",
    )
    assert finish_events
    assert question_events == []


def test_unknown_applies_when_emits_registry_error_event(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    bundle = workspace / "self_reflection" / "generated_check"
    bundle.mkdir(parents=True)
    (workspace / "self_reflection" / "registry.yaml").write_text(
        'version: "0.2"\nchecks:\n'
        "  - id: generated_check\n"
        "    hook: question_candidate\n"
        "    file: generated_check/check.py\n"
        "    prompt: generated_check/PROMPT.md\n"
        "    applies_when: invalid_condition_name\n"
        "    mode: warn\n",
        encoding="utf-8",
    )
    (bundle / "check.py").write_text(_generated_check_py("generated_check"), encoding="utf-8")
    (bundle / "PROMPT.md").write_text("ok", encoding="utf-8")
    candidate = {"kind": "question", "text": "BAD question?", "raw_action": {}, "turn_index": 0}
    events = ReflectionRuntime(workspace, mode="warn").check(candidate, {"turn_index": 0, "max_turns": 4}, hook="question_candidate")
    assert any(event.get("type") == "reflection_registry_error" for event in events)
