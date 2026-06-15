from __future__ import annotations

import importlib.util
import traceback
from pathlib import Path
from typing import Any

from reqahe.harness.component_schema import REFLECTION_APPLIES_WHEN, load_reflection_registry

ALLOWED_HOOKS = {"question_candidate", "finish_candidate"}
ALLOWED_APPLIES_WHEN = REFLECTION_APPLIES_WHEN
SUPPORTED_HOOKS = ALLOWED_HOOKS
SUPPORTED_APPLIES_WHEN = ALLOWED_APPLIES_WHEN


def is_retryable_reflection_event(
    event: dict[str, Any],
    *,
    runtime_mode: str,
    retry_on_modes: set[str],
) -> bool:
    if runtime_mode not in {"warn", "enforce"}:
        return False
    event_mode = str(event.get("mode") or "")
    if event_mode not in {"warn", "enforce"}:
        return False
    return event_mode in retry_on_modes


def format_reflection_retry_feedback(
    events: list[dict[str, Any]],
    workspace_dir: str | Path,
    *,
    max_events: int = 3,
) -> list[str]:
    root = Path(workspace_dir)
    feedback: list[str] = []
    for event in events[:max_events]:
        check_id = str(event.get("check_id") or "unknown_check")
        mode = str(event.get("mode") or "warn")
        message = str(event.get("message") or "").strip()
        if not message:
            continue
        candidate_kind = str(event.get("candidate_kind") or "unknown")
        candidate_text = str(event.get("candidate_text") or "").strip()
        suggestion = str(event.get("suggestion") or "").strip()
        prompt_file = str(event.get("prompt_file") or "").strip()
        prompt_content = ""
        if prompt_file:
            prompt_path = root / prompt_file
            if prompt_path.exists():
                prompt_content = prompt_path.read_text(encoding="utf-8").strip()

        lines = [
            f"RETRY_THIS_TURN self_reflection {check_id}/{mode}:",
            "The candidate output triggered a runtime warning.",
            "",
            f"Candidate kind: {candidate_kind}",
            "Candidate text:",
            candidate_text,
            "",
            "Warning:",
            message,
        ]
        if suggestion:
            lines.extend(["", "Suggestion:", suggestion])
        if prompt_content:
            lines.extend(
                [
                    "",
                    f"Repair instruction from {prompt_file}:",
                    prompt_content,
                ]
            )
        lines.extend(
            [
                "",
                "Regenerate the current action only. Keep valid prior context. Return the same JSON action schema.",
            ]
        )
        feedback.append("\n".join(lines))
    return feedback


class ReflectionRuntime:
    def __init__(self, workspace_dir: str | Path, mode: str = "warn", event_log_path: str | Path | None = None):
        self.workspace_dir = Path(workspace_dir)
        self.mode = mode
        self.event_log_path = Path(event_log_path) if event_log_path else None
        self.registry = self._load_registry()
        self.registry_errors: list[str] = []

    def _load_registry(self) -> dict[str, Any]:
        try:
            return load_reflection_registry(self.workspace_dir)
        except RuntimeError:
            return {"version": "0.2", "checks": []}

    def check(
        self,
        candidate: dict[str, Any],
        state: dict[str, Any],
        hook: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if hook not in ALLOWED_HOOKS:
            return events
        for item in sorted(self.registry.get("checks", []) or [], key=_registry_sort_key):
            if not isinstance(item, dict) or item.get("hook") != hook:
                continue
            applies_when = str(item.get("applies_when") or "always").strip() or "always"
            try:
                if not applies_when_matches(applies_when, hook=hook, candidate=candidate, state=state):
                    continue
            except ValueError as exc:
                events.append(_registry_error_event(item, candidate, hook=hook, message=str(exc)))
                continue
            check_id = str(item.get("id") or "")
            rel_file = str(item.get("file") or "")
            rel_prompt = str(item.get("prompt") or "")
            path = self.workspace_dir / "self_reflection" / rel_file
            prompt_path = self.workspace_dir / "self_reflection" / rel_prompt if rel_prompt else None
            module = _load_module(path)
            if module is None or not hasattr(module, "check"):
                continue
            try:
                check_events = module.check(dict(candidate), dict(state)) or []
            except Exception as exc:
                events.append(
                    _error_event(
                        item,
                        candidate,
                        hook=hook,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        traceback_text=traceback.format_exc(),
                    )
                )
                continue
            for event in check_events:
                normalized = _event(event, item, candidate, hook=hook)
                for key in ("turn_index", "max_turns"):
                    if key in state:
                        normalized.setdefault(key, state[key])
                events.append(normalized)
        return events


def _load_module(path: Path) -> Any | None:
    if not path.exists() or path.name != "check.py":
        return None
    spec = importlib.util.spec_from_file_location(f"reqahe_reflection_{path.parent.name}", path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(
    event: dict[str, Any],
    registry_item: dict[str, Any],
    candidate: dict[str, Any],
    *,
    hook: str,
) -> dict[str, Any]:
    check_id = str(registry_item.get("id") or "")
    rel_file = str(registry_item.get("file") or f"{check_id}/check.py")
    rel_prompt = str(registry_item.get("prompt") or f"{check_id}/PROMPT.md")
    normalized = dict(event)
    normalized.setdefault("check_id", check_id)
    check_mode = str(registry_item.get("mode") or "observe")
    if check_mode in {"warn", "enforce"}:
        normalized.setdefault("type", "reflection_warning_event")
    else:
        normalized.setdefault("type", "reflection_check_executed")
    normalized.setdefault("severity", _severity_for_mode(check_mode))
    normalized.setdefault("message", "")
    normalized.setdefault("source_file", f"self_reflection/{rel_file}")
    normalized.setdefault("prompt_file", f"self_reflection/{rel_prompt}")
    normalized.setdefault("mode", str(registry_item.get("mode") or "observe"))
    normalized.setdefault("hook", hook)
    normalized.setdefault("candidate_kind", str(candidate.get("kind") or ""))
    normalized.setdefault("candidate_text", str(candidate.get("text") or ""))
    if "suggestion" in event:
        normalized["suggestion"] = str(event.get("suggestion") or "")
    return normalized


def _error_event(
    registry_item: dict[str, Any],
    candidate: dict[str, Any],
    *,
    hook: str,
    error_type: str,
    error_message: str,
    traceback_text: str,
) -> dict[str, Any]:
    check_id = str(registry_item.get("id") or "unknown_check")
    rel_file = str(registry_item.get("file") or f"{check_id}/check.py")
    rel_prompt = str(registry_item.get("prompt") or f"{check_id}/PROMPT.md")
    return {
        "check_id": check_id,
        "type": "reflection_check_error",
        "severity": "error",
        "message": f"Reflection check {check_id} failed: {error_type}: {error_message}",
        "source_file": f"self_reflection/{rel_file}",
        "prompt_file": f"self_reflection/{rel_prompt}",
        "mode": str(registry_item.get("mode") or "observe"),
        "hook": hook,
        "candidate_kind": str(candidate.get("kind") or ""),
        "candidate_text": str(candidate.get("text") or ""),
        "details": {
            "error_type": error_type,
            "error_message": error_message,
            "traceback": traceback_text,
        },
    }


def _registry_error_event(
    registry_item: dict[str, Any],
    candidate: dict[str, Any],
    *,
    hook: str,
    message: str,
) -> dict[str, Any]:
    check_id = str(registry_item.get("id") or "unknown_check")
    rel_file = str(registry_item.get("file") or f"{check_id}/check.py")
    rel_prompt = str(registry_item.get("prompt") or f"{check_id}/PROMPT.md")
    return {
        "check_id": check_id,
        "type": "reflection_registry_error",
        "severity": "error",
        "message": message,
        "source_file": f"self_reflection/{rel_file}",
        "prompt_file": f"self_reflection/{rel_prompt}",
        "mode": str(registry_item.get("mode") or "observe"),
        "hook": hook,
        "candidate_kind": str(candidate.get("kind") or ""),
        "candidate_text": str(candidate.get("text") or ""),
    }


def _severity_for_mode(mode: str) -> str:
    if mode == "enforce":
        return "error"
    if mode == "warn":
        return "warn"
    return "info"


def _registry_sort_key(item: Any) -> tuple[int, str]:
    if not isinstance(item, dict):
        return (0, "")
    return (int(item.get("priority", 0) or 0), str(item.get("id") or ""))


def _current_turn_index(state: dict[str, Any]) -> int:
    return int(state.get("turn_index", 0))


def _early_turn_threshold(state: dict[str, Any]) -> int:
    max_turns = max(1, int(state.get("max_turns", 1)))
    return max(1, max_turns // 2) - 1


def _late_turn_threshold(state: dict[str, Any]) -> int:
    max_turns = max(1, int(state.get("max_turns", 1)))
    return max(1, max_turns // 2)


def _candidate_action_type(candidate: dict[str, Any]) -> str:
    action_type = candidate.get("action_type")
    if action_type:
        return str(action_type)
    raw_action = candidate.get("raw_action")
    if isinstance(raw_action, dict) and raw_action.get("action"):
        return str(raw_action["action"])
    return ""


def applies_when_matches(
    applies_when: str,
    *,
    hook: str,
    candidate: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    if applies_when not in ALLOWED_APPLIES_WHEN:
        raise ValueError(f"Unknown applies_when: {applies_when}")

    if applies_when == "always":
        return True
    if applies_when == "early_turn":
        return _current_turn_index(state) <= _early_turn_threshold(state)
    if applies_when == "late_turn":
        return _current_turn_index(state) >= _late_turn_threshold(state)
    if applies_when == "has_history":
        return bool(state.get("history"))
    if applies_when == "no_history":
        return not bool(state.get("history"))
    if applies_when == "candidate_is_question":
        return hook == "question_candidate" or candidate.get("kind") == "question" or _candidate_action_type(
            candidate
        ) in {"ask_question", "question"}
    if applies_when == "candidate_is_finish":
        return hook == "finish_candidate" or candidate.get("kind") == "finish" or _candidate_action_type(
            candidate
        ) in {"finish_interview", "finish"}
    raise ValueError(f"Unknown applies_when: {applies_when}")
