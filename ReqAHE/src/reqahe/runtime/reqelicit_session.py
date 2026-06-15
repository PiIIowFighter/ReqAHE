from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from reqahe.runtime.dataset import Scenario


def ensure_reqelicitgym_import(reqelicitgym_root: str | Path) -> None:
    package_root = Path(reqelicitgym_root).resolve()
    import_root = str(package_root.parent)
    if import_root not in sys.path:
        sys.path.insert(0, import_root)


def source_reqelicitgym_available(reqelicitgym_root: str | Path) -> tuple[bool, str]:
    try:
        ensure_reqelicitgym_import(reqelicitgym_root)
        from ReqElicitGym.env import ReqElicitGym  # noqa: F401
        from ReqElicitGym.config import ReqElicitGymConfig  # noqa: F401

        return True, ""
    except Exception as exc:
        return False, str(exc)


def build_model_config(llm_config: dict[str, Any], model_name: str) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "api_key": llm_config.get("api_key", ""),
        "model_name": model_name,
        "temperature": float(llm_config.get("temperature", 0.0)),
        "max_tokens": int(llm_config.get("max_tokens", 1024)),
        "timeout": float(llm_config.get("timeout", 60)),
    }
    base_url = llm_config.get("base_url")
    if base_url:
        cfg["base_url"] = base_url
    return cfg


def initialize_remaining_requirements(scenario: Scenario) -> list[dict[str, Any]]:
    """Mirror ReqElicitGym._initialize_requirements."""
    remaining: list[dict[str, Any]] = []
    counter = 1
    for req_data in scenario.implicit_requirements:
        aspect = str(req_data.get("Aspect") or req_data.get("aspect") or "")
        requirement_text = str(
            req_data.get("RequirementText") or req_data.get("requirement") or req_data.get("text") or ""
        )
        req_id = str(req_data.get("id") or req_data.get("ID") or f"IR{counter}")
        dimension = "NFR" if aspect == "Style" else "FR"
        remaining.append(
            {
                "id": req_id,
                "aspect": aspect,
                "requirement": requirement_text,
                "dimension": dimension,
                "elicited": False,
            }
        )
        counter += 1
    return remaining


def scenario_to_task(scenario: Scenario) -> dict[str, Any]:
    task = dict(scenario.raw) if scenario.raw else {}
    task.setdefault("id", scenario.scenario_id)
    task.setdefault("name", scenario.name)
    task.setdefault("application_type", scenario.app_type)
    task.setdefault("initial_requirements", scenario.initial_req)
    if "Implicit Requirements" not in task and scenario.implicit_requirements:
        task["Implicit Requirements"] = scenario.implicit_requirements
    return task


class ReqElicitSession:
    """Runtime session that delegates evaluation to ReqElicitGym judge->user flow."""

    evaluation_mode = "reqelicitgym_judge_user"

    def __init__(
        self,
        scenario: Scenario,
        reqelicitgym_root: str | Path,
        llm_config: dict[str, Any],
        judge_model: str,
        user_model: str,
        user_answer_quality: str = "high",
    ):
        ensure_reqelicitgym_import(reqelicitgym_root)
        from ReqElicitGym.env.prompts import evaluate_action

        self._evaluate_action = evaluate_action
        self.scenario = scenario
        self.task = scenario_to_task(scenario)
        self.user_answer_quality = user_answer_quality
        self.judge_model_config = build_model_config(llm_config, judge_model)
        self.user_model_config = build_model_config(llm_config, user_model)
        self.remaining_requirements = initialize_remaining_requirements(scenario)
        self.elicited_requirements: list[dict[str, Any]] = []
        self.conversation_history: list[dict[str, str]] = []
        if scenario.initial_req:
            self.conversation_history.append({"role": "user", "content": scenario.initial_req})

    def step(self, action: str) -> dict[str, Any]:
        user_response, elicited_ids, _reward, judgement = self._evaluate_action(
            action=action,
            task=self.task,
            judge_model_config=self.judge_model_config,
            user_simulator_config=self.user_model_config,
            conversation_history=list(self.conversation_history),
            remaining_requirements=self.remaining_requirements,
            user_quality_level=self.user_answer_quality,
        )
        judgement = dict(judgement or {})
        judgement["elicited_requirement_ids"] = list(elicited_ids or [])

        self.conversation_history.append({"role": "interviewer", "content": action})
        if elicited_ids:
            for req_id in elicited_ids:
                for req in self.remaining_requirements:
                    if req.get("id") == req_id and not req.get("elicited", False):
                        marked = req.copy()
                        marked["elicited"] = True
                        self.elicited_requirements.append(marked)
                        req["elicited"] = True
                        break
            self.remaining_requirements = [
                req for req in self.remaining_requirements if not req.get("elicited", False)
            ]

        if user_response:
            self.conversation_history.append({"role": "user", "content": user_response})

        return {
            "user_response": user_response,
            "judgement": judgement,
            "elicited_requirement_ids": list(elicited_ids or []),
            "is_finish": judgement.get("action_type") == "finish",
        }
