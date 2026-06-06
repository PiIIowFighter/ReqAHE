from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reqahe.envs.dataset import Scenario
from reqahe.llm.client import OpenAICompatibleClient

_VALID_ACTION_TYPES = frozenset({"probe", "clarify", "finish"})
_ACTION_TYPE_ALIASES = {
    "ask_question": "probe",
    "finish_interview": "finish",
    "question": "probe",
    "probe_question": "probe",
    "clarify_question": "clarify",
    "clarification": "clarify",
    "end": "finish",
    "complete": "finish",
    "unknown": "probe",
}


def _normalize_action_type(raw: str, fallback_action: str) -> str:
    action_type = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if action_type in _VALID_ACTION_TYPES:
        return action_type
    if action_type in _ACTION_TYPE_ALIASES:
        return _ACTION_TYPE_ALIASES[action_type]
    if fallback_action == "finish_interview":
        return "finish"
    if fallback_action == "ask_question":
        return "probe"
    return "probe"


class ReqElicitSession:
    """LLM-backed runtime adapter for ReqElicitGym tasks."""

    def __init__(
        self,
        scenario: Scenario,
        llm: OpenAICompatibleClient,
        oracle_model: str,
        evaluator_model: str,
    ):
        self.scenario = scenario
        self.remaining_ids = {self._req_id(i, req) for i, req in enumerate(scenario.implicit_requirements, start=1)}
        self.llm = llm
        self.oracle_model = oracle_model
        self.evaluator_model = evaluator_model
        self.evaluator_mode = "llm"
        self.history: list[dict[str, Any]] = []

    def ask(self, question: str) -> tuple[str, dict]:
        answer = self._oracle_answer(question)
        evaluator = self._evaluate_turn("ask_question", question=question, answer=answer)
        for req_id in evaluator["hit_requirement_ids"]:
            self.remaining_ids.discard(req_id)
        self.history.append({"question": question, "answer": answer, "evaluator": evaluator})
        return answer, evaluator

    def finish(self, summary: str) -> dict:
        evaluator = self._evaluate_turn("finish_interview", question="", answer="", finish_summary=summary)
        self.history.append({"finish_summary": summary, "evaluator": evaluator})
        return evaluator

    def _oracle_answer(self, question: str) -> str:
        payload = {
            "scenario_id": self.scenario.scenario_id,
            "application_type": self.scenario.app_type,
            "initial_requirement": self.scenario.initial_req,
            "hidden_implicit_requirements": self._requirements_payload(),
            "conversation_history": self._compact_history(),
            "current_question": question,
        }
        data = self.llm.json_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a requirements elicitation oracle simulating the stakeholder. "
                        "Answer only from the hidden implicit requirements and the conversation. "
                        "Reveal only requirements that are relevant to the current question. "
                        "If the question is off-target, say that you are not sure and invite a more specific question. "
                        "Return strict JSON with exactly this shape: {\"answer\": \"...\"}."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model=self.oracle_model,
            purpose="oracle answer generation",
        )
        answer = str(data.get("answer") or "").strip()
        if not answer:
            raise RuntimeError("oracle answer generation failed: missing answer")
        return answer

    def _evaluate_turn(self, action: str, question: str, answer: str, finish_summary: str = "") -> dict:
        payload = {
            "scenario_id": self.scenario.scenario_id,
            "application_type": self.scenario.app_type,
            "initial_requirement": self.scenario.initial_req,
            "hidden_implicit_requirements": self._requirements_payload(),
            "remaining_requirement_ids": sorted(self.remaining_ids),
            "conversation_history": self._compact_history(),
            "action": action,
            "question": question,
            "answer": answer,
            "finish_summary": finish_summary,
        }
        base_messages = [
            {
                "role": "system",
                "content": (
                    "You are the evaluator for a requirements elicitation benchmark. "
                    "Judge whether the latest interviewer action elicited any still-undiscovered hidden implicit requirements. "
                    "Use only requirement IDs listed in remaining_requirement_ids. "
                    "Return strict JSON with keys: action_type, hit, hit_requirement_ids, reasoning. "
                    "action_type must be exactly one of: probe, clarify, finish. "
                    "Do not use ask_question, finish_interview, or any other label."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        messages = list(base_messages)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            data = self.llm.json_chat(
                messages,
                model=self.evaluator_model,
                purpose="evaluator turn judgment",
            )
            try:
                return self._validate_evaluator(data, fallback_action=action)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= 3:
                    break
                messages = [
                    *base_messages,
                    {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                    {
                        "role": "user",
                        "content": (
                            f"Your previous evaluator JSON failed validation: {exc}. "
                            "Regenerate one compact valid JSON object. "
                            "action_type must be exactly probe, clarify, or finish. "
                            "hit must be a boolean. hit_requirement_ids must be a list of IDs from remaining_requirement_ids only."
                        ),
                    },
                ]
        raise RuntimeError(f"evaluator turn judgment failed after 3 attempts: {last_error}")

    def _validate_evaluator(self, data: dict[str, Any], fallback_action: str = "ask_question") -> dict:
        required = ["action_type", "hit", "hit_requirement_ids", "reasoning"]
        missing = [key for key in required if key not in data]
        if missing:
            raise RuntimeError(f"missing keys {missing}")
        action_type = _normalize_action_type(str(data["action_type"]), fallback_action)
        raw_ids = data["hit_requirement_ids"]
        if not isinstance(raw_ids, list):
            raise RuntimeError("hit_requirement_ids must be a list")
        valid_ids = {self._req_id(i, req) for i, req in enumerate(self.scenario.implicit_requirements, start=1)}
        hit_ids = []
        for req_id in raw_ids:
            req_id = str(req_id)
            if req_id not in valid_ids or req_id not in self.remaining_ids:
                continue
            hit_ids.append(req_id)
        return {
            "action_type": action_type,
            "hit": bool(hit_ids),
            "hit_requirement_ids": hit_ids,
            "reasoning": str(data.get("reasoning") or ""),
            "evaluator_mode": self.evaluator_mode,
        }

    def _requirements_payload(self) -> list[dict[str, str]]:
        payload = []
        for idx, req in enumerate(self.scenario.implicit_requirements, start=1):
            payload.append(
                {
                    "id": self._req_id(idx, req),
                    "aspect": str(req.get("Aspect") or req.get("aspect") or "unknown"),
                    "text": self._req_text(req),
                }
            )
        return payload

    def _compact_history(self) -> list[dict[str, str]]:
        items = []
        for turn in self.history:
            if "question" in turn:
                items.append({"question": str(turn["question"]), "answer": str(turn["answer"])})
            elif "finish_summary" in turn:
                items.append({"finish_summary": str(turn["finish_summary"])})
        return items

    def _req_id(self, idx: int, req: dict) -> str:
        return str(req.get("id") or req.get("ID") or f"IR{idx}")

    def _req_text(self, req: dict) -> str:
        return str(req.get("RequirementText") or req.get("requirement") or req.get("text") or "")


def source_reqelicitgym_available(reqelicitgym_root: str | Path) -> tuple[bool, str]:
    try:
        import sys

        package_root = Path(reqelicitgym_root).resolve()
        import_root = str(package_root.parent)
        if import_root not in sys.path:
            sys.path.insert(0, import_root)
        from ReqElicitGym.env import ReqElicitGym  # noqa: F401
        from ReqElicitGym.config import ReqElicitGymConfig  # noqa: F401

        return True, ""
    except Exception as exc:
        return False, str(exc)
