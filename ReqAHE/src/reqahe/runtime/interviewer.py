from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import load_selected_skill_text, load_skill_catalog
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.runtime.memory_router import (
    format_relevant_memory_block,
    list_memory_types,
    load_memory_for_type,
    memory_router_config_from_dict,
    route_memory_type,
)
from reqahe.runtime.skill_router import (
    format_skill_routing_summary,
    route_relevant_skills,
    skill_router_config_from_dict,
)


class SeedInterviewer:
    def __init__(
        self,
        harness: dict[str, str],
        llm: OpenAICompatibleClient,
        model: str = "",
        workspace_dir: str | Path | None = None,
        skill_router_config: dict[str, Any] | None = None,
        skill_router_model: str = "",
        skill_routing_event_log_path: str | Path | None = None,
        memory_router_config: dict[str, Any] | None = None,
        memory_router_model: str = "",
        memory_routing_event_log_path: str | Path | None = None,
    ):
        self.harness = harness
        self.llm = llm
        self.model = model
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.skill_router_config = skill_router_config_from_dict(skill_router_config, skill_router_model)
        self.skill_router_model = skill_router_model or model
        self.skill_routing_event_log_path = skill_routing_event_log_path
        self.memory_router_config = memory_router_config_from_dict(memory_router_config, memory_router_model)
        self.memory_router_model = memory_router_model or model
        self.memory_routing_event_log_path = memory_routing_event_log_path
        self._skill_catalog = load_skill_catalog(workspace_dir) if workspace_dir else []
        self._memory_types = list_memory_types(workspace_dir) if workspace_dir else []
        self._selected_memory_type: str = ""
        self._loaded_memory_excerpt: str = ""
        self._memory_routed: bool = False
        self.last_memory_routing: dict[str, Any] = {}
        self.last_skill_routing: dict[str, Any] = {}
        self.last_skill_routing_result: dict[str, Any] = {}
        self.last_prompt_digest: dict[str, Any] = {}
        self.last_routing: dict[str, Any] = {}

    def _ensure_memory_routing(self, initial_req: str) -> None:
        if self._memory_routed or not self.workspace_dir:
            return
        routing: dict[str, Any] = {
            "selected_type": "",
            "confidence": 0.0,
            "decision": "none",
            "reason": "",
            "router_error": "",
            "available_types": self._memory_types,
        }
        excerpt = ""
        if self.memory_router_config.enabled and self._memory_types:
            routing = route_memory_type(
                llm=self.llm,
                model=self.memory_router_model or self.model,
                initial_req=initial_req,
                available_types=self._memory_types,
                config=self.memory_router_config,
                event_log_path=self.memory_routing_event_log_path,
            )
            selected = routing.get("selected_type") or ""
            if selected:
                excerpt = load_memory_for_type(
                    self.workspace_dir,
                    selected,
                    self.memory_router_config.max_chars_per_type_memory,
                )
        self._selected_memory_type = routing.get("selected_type") or ""
        self._loaded_memory_excerpt = excerpt
        self.last_memory_routing = routing
        self._memory_routed = True

    def next_action(
        self,
        initial_req: str,
        history: list[dict[str, str]],
        warnings: list[str],
        max_turns: int,
        turn_index: int = 0,
        app_type: str = "",
    ) -> dict[str, Any]:
        self._ensure_memory_routing(initial_req)
        relevant_memory = format_relevant_memory_block(self._loaded_memory_excerpt)

        selected_skill_details = "(none selected by skill router)"
        skill_routing_summary = "(skill router disabled or no workspace)"
        skill_routing: dict[str, Any] = {
            "selected_skill_ids": [],
            "candidate_skill_ids": [item["skill_id"] for item in self._skill_catalog],
            "decisions": [],
            "router_reason": "",
            "router_error": "",
            "catalog_size": len(self._skill_catalog),
        }

        if self.workspace_dir and self.skill_router_config.enabled and self._skill_catalog:
            skill_routing = route_relevant_skills(
                llm=self.llm,
                model=self.skill_router_model or self.model,
                catalog=self._skill_catalog,
                initial_req=initial_req,
                history=history,
                warnings=warnings,
                turn_index=turn_index,
                max_turns=max_turns,
                config=self.skill_router_config,
                event_log_path=self.skill_routing_event_log_path,
            )
            skill_routing_summary = format_skill_routing_summary(skill_routing)
            loaded_skills = load_selected_skill_text(
                self.workspace_dir,
                skill_routing.get("selected_skill_ids", []),
            )
            if loaded_skills:
                selected_skill_details = loaded_skills
        elif not self._skill_catalog:
            skill_routing_summary = "(no active skills in catalog)"

        self.last_skill_routing = skill_routing
        self.last_skill_routing_result = skill_routing
        self.last_routing = skill_routing

        prompt = self._build_prompt(
            initial_req,
            history,
            warnings,
            max_turns,
            relevant_memory=relevant_memory,
            selected_skill_details=selected_skill_details,
            skill_routing_summary=skill_routing_summary,
            turn_index=turn_index,
        )
        self.last_prompt_digest = {
            "available_skill_catalog_present": "# Skill Catalog" in prompt,
            "relevant_memory_present": "# Relevant Memory" in prompt,
            "relevant_memory_empty": not bool(self._loaded_memory_excerpt.strip()),
            "selected_memory_type": self._selected_memory_type,
            "selected_skill_details_present": "# Selected Skill Details" in prompt,
            "selected_skill_details_is_none": "(none selected by skill router)" in prompt,
            "selected_skill_ids": skill_routing.get("selected_skill_ids", []),
            "selected_skill_count": len(skill_routing.get("selected_skill_ids", [])),
            "catalog_size": skill_routing.get("catalog_size", 0),
            "memory_router_error": self.last_memory_routing.get("router_error", ""),
            "router_error": skill_routing.get("router_error", ""),
            "memory_catalog_absent": "# Memory Catalog" not in prompt,
            "self_reflection_runtime_only": True,
        }
        parsed = self.llm.json_chat(
            [
                {"role": "system", "content": self.harness.get("system_prompt", "")},
                {"role": "user", "content": prompt},
            ],
            model=self.model,
            purpose="interviewer action generation",
        )
        return _validate_action(parsed)

    def _build_prompt(
        self,
        initial_req: str,
        history: list[dict[str, str]],
        warnings: list[str],
        max_turns: int,
        relevant_memory: str = "",
        selected_skill_details: str = "(none selected by skill router)",
        skill_routing_summary: dict[str, Any] | str = "",
        turn_index: int = 0,
    ) -> str:
        rendered_history = "\n".join(
            f"Interviewer: {h.get('question','')}\nUser: {h.get('answer','')}" for h in history
        )
        latest_user_message = history[-1].get("answer", "") if history else initial_req
        recent_feedback = "\n".join(f"- {item}" for item in warnings[-5:]) or "(none)"
        if isinstance(skill_routing_summary, dict):
            skill_routing_text = format_skill_routing_summary(skill_routing_summary)
        else:
            skill_routing_text = str(skill_routing_summary or "(none)")
        memory_block = relevant_memory.strip() or (
            "# Relevant Memory\n\n(none selected for this scenario type)"
        )
        return (
            "# Component Boundaries\n"
            "- system_prompt: global invariant rules.\n"
            "- memory: previously elicited requirement content hints for similar scenario types.\n"
            "- skills: selected operational procedures; use these to decide what to ask.\n"
            "- self_reflection: Python runtime checks only; not injected here.\n\n"
            "# Initial Requirement\n"
            f"{initial_req}\n\n"
            "# Turn Budget\n"
            f"{max_turns}\n\n"
            "# Dialogue History\n"
            f"{rendered_history or '(none)'}\n\n"
            "# Recent Reflection Feedback\n"
            "The following items are produced by Python runtime self_reflection checks.\n"
            "If an item starts with RETRY_THIS_TURN, revise the previously proposed action in this same turn.\n"
            "Do not repeat the same violation.\n"
            f"{recent_feedback}\n\n"
            f"{memory_block}\n\n"
            "# Skill Catalog\n"
            f"{self.harness.get('skills','') or '(none)'}\n\n"
            "# Skill Routing Summary\n"
            f"{skill_routing_text}\n\n"
            "# Selected Skill Details\n"
            f"{selected_skill_details or '(none selected by skill router)'}\n\n"
            "# Return strict JSON only\n"
            "Use the fixed framework actions ask_question or finish_interview. "
            f"Latest user message: {latest_user_message}. "
            f"Current turn index: {turn_index}. "
            "Return action, question, finish_summary, and optional thought_summary."
        )


def _validate_action(data: dict[str, Any]) -> dict[str, Any]:
    action = data.get("action")
    if action not in {"ask_question", "finish_interview"}:
        raise RuntimeError("interviewer action generation failed: action must be ask_question or finish_interview")
    if action == "ask_question" and not str(data.get("question") or "").strip():
        raise RuntimeError("interviewer action generation failed: ask_question requires a non-empty question")
    if action == "finish_interview" and not str(data.get("finish_summary") or "").strip():
        raise RuntimeError("interviewer action generation failed: finish_interview requires finish_summary")
    return {
        "action": action,
        "thought_summary": str(data.get("thought_summary") or ""),
        "question": str(data.get("question") or ""),
        "finish_summary": str(data.get("finish_summary") or ""),
    }
