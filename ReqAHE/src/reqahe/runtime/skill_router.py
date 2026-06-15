from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import render_skill_catalog
from reqahe.runtime.route_stats import build_router_reason
from reqahe.infra.io import append_jsonl
from reqahe.infra.llm_client import OpenAICompatibleClient

ROUTER_SYSTEM_MESSAGE = (
    "You are a skill relevance router for a requirements elicitation interviewer. "
    "Select only skills whose trigger directly applies to the next interviewer decision. "
    "Do not select a skill merely because it is broadly related. "
    "Prefer no skill over an irrelevant skill. Return strict JSON only."
)


@dataclass
class SkillRouterConfig:
    enabled: bool = True
    max_selected_skills: int = 3
    min_relevance: float = 0.45
    router_model: str = ""
    log_events: bool = True


def _build_router_prompt(
    *,
    catalog: list[dict[str, Any]],
    initial_req: str,
    history: list[dict[str, str]],
    warnings: list[str],
    turn_index: int,
    max_turns: int,
) -> str:
    rendered_history = "\n".join(
        f"Interviewer: {h.get('question', '')}\nUser: {h.get('answer', '')}" for h in history
    )
    latest_user_message = history[-1].get("answer", "") if history else initial_req
    recent_feedback = "\n".join(f"- {item}" for item in warnings[-5:]) or "(none)"
    return (
        "# Initial Requirement\n"
        f"{initial_req}\n\n"
        "# Turn State\n"
        f"turn_index={turn_index}\n"
        f"max_turns={max_turns}\n\n"
        "# Dialogue History\n"
        f"{rendered_history or '(none)'}\n\n"
        "# Latest User Message\n"
        f"{latest_user_message}\n\n"
        "# Recent Reflection Feedback\n"
        f"{recent_feedback}\n\n"
        "# Skill Catalog (metadata only)\n"
        f"{render_skill_catalog(catalog) or '(none)'}\n\n"
        "# Output Schema\n"
        "Return strict JSON only:\n"
        "{\n"
        '  "selected_skill_ids": ["skill-a", "skill-b"],\n'
        '  "decisions": [\n'
        "    {\n"
        '      "skill_id": "skill-a",\n'
        '      "relevance": 0.82,\n'
        '      "decision": "select",\n'
        '      "reason": "This skill directly applies because ..."\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def _validate_router_schema(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "invalid router schema: router response must be an object"

    errors: list[str] = []
    if "selected_skill_ids" in raw and not isinstance(raw["selected_skill_ids"], list):
        errors.append("selected_skill_ids must be a list")

    decisions = raw.get("decisions")
    if "decisions" in raw and not isinstance(decisions, list):
        errors.append("decisions must be a list")
    elif isinstance(decisions, list):
        for idx, item in enumerate(decisions):
            if not isinstance(item, dict):
                errors.append(f"decision at index {idx} must be an object")
                continue
            if "skill_id" in item and not isinstance(item["skill_id"], str):
                errors.append(f"decision at index {idx} skill_id must be a string")
            if "relevance" in item:
                try:
                    float(item["relevance"])
                except (TypeError, ValueError):
                    errors.append(f"decision at index {idx} relevance must be convertible to float")

    if errors:
        return "invalid router schema: " + "; ".join(errors)
    return ""


def _normalize_router_result(
    raw: dict[str, Any],
    catalog: list[dict[str, Any]],
    config: SkillRouterConfig,
) -> dict[str, Any]:
    catalog_ids = {item["skill_id"] for item in catalog}
    relevance_by_id: dict[str, float] = {}
    decisions = raw.get("decisions")
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            if skill_id not in catalog_ids:
                continue
            try:
                relevance = float(item.get("relevance", 0))
            except (TypeError, ValueError):
                relevance = 0.0
            relevance_by_id[skill_id] = relevance

    selected: list[str] = []
    raw_selected = raw.get("selected_skill_ids")
    if isinstance(raw_selected, list):
        for skill_id in raw_selected:
            skill_id_str = str(skill_id)
            if skill_id_str not in catalog_ids:
                continue
            relevance = relevance_by_id.get(skill_id_str, 1.0)
            if relevance < config.min_relevance:
                continue
            if skill_id_str not in selected:
                selected.append(skill_id_str)

    if not selected and relevance_by_id:
        ranked = sorted(
            [
                (skill_id, score)
                for skill_id, score in relevance_by_id.items()
                if score >= config.min_relevance
            ],
            key=lambda pair: (-pair[1], pair[0]),
        )
        selected = [skill_id for skill_id, _score in ranked[: config.max_selected_skills]]

    selected = selected[: config.max_selected_skills]
    filtered_decisions = []
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            if skill_id not in selected:
                continue
            filtered_decisions.append(item)

    return {
        "selected_skill_ids": selected,
        "candidate_skill_ids": sorted(catalog_ids),
        "decisions": filtered_decisions,
        "router_reason": build_router_reason(
            {"selected_skill_ids": selected, "decisions": filtered_decisions, "router_error": ""}
        ),
        "router_error": "",
        "catalog_size": len(catalog),
    }


def route_relevant_skills(
    *,
    llm: OpenAICompatibleClient,
    model: str,
    catalog: list[dict[str, Any]],
    initial_req: str,
    history: list[dict[str, str]],
    warnings: list[str],
    turn_index: int,
    max_turns: int,
    config: SkillRouterConfig,
    event_log_path: str | Path | None = None,
) -> dict[str, Any]:
    empty_result = {
        "selected_skill_ids": [],
        "candidate_skill_ids": sorted(item["skill_id"] for item in catalog),
        "decisions": [],
        "router_reason": "",
        "router_error": "",
        "catalog_size": len(catalog),
    }
    if not config.enabled or not catalog:
        return empty_result

    used_model = model or llm.model
    router_error = ""
    result = dict(empty_result)
    try:
        prompt = _build_router_prompt(
            catalog=catalog,
            initial_req=initial_req,
            history=history,
            warnings=warnings,
            turn_index=turn_index,
            max_turns=max_turns,
        )
        raw = llm.json_chat(
            [
                {"role": "system", "content": ROUTER_SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            model=used_model,
            purpose="skill relevance routing",
        )
        schema_error = _validate_router_schema(raw)
        if schema_error:
            result = dict(empty_result)
            result["router_error"] = schema_error
            result["router_reason"] = schema_error
        else:
            result = _normalize_router_result(raw, catalog, config)
    except Exception as exc:
        router_error = str(exc)
        result = dict(empty_result)
        result["router_error"] = router_error
        result["router_reason"] = router_error

    if event_log_path and config.log_events:
        append_jsonl(
            event_log_path,
            {
                "turn_index": turn_index,
                "catalog_size": len(catalog),
                "candidate_skill_ids": result.get("candidate_skill_ids", []),
                "selected_skill_ids": result["selected_skill_ids"],
                "router_reason": result.get("router_reason", ""),
                "decisions": result.get("decisions", []),
                "router_error": result.get("router_error", router_error),
                "used_model": used_model,
            },
        )
    return result


def skill_router_config_from_dict(data: dict[str, Any] | None, router_model: str = "") -> SkillRouterConfig:
    data = data or {}
    return SkillRouterConfig(
        enabled=bool(data.get("enabled", True)),
        max_selected_skills=int(data.get("max_selected_skills", 3) or 3),
        min_relevance=float(data.get("min_relevance", 0.45) or 0.45),
        router_model=str(router_model or data.get("router_model") or ""),
        log_events=bool(data.get("log_events", True)),
    )


def format_skill_routing_summary(routing: dict[str, Any]) -> str:
    if routing.get("router_error"):
        return f"router_error: {routing['router_error']}; selected: (none)"
    selected = routing.get("selected_skill_ids") or []
    if not selected:
        return "selected: (none)"
    decisions = routing.get("decisions") or []
    parts = [f"selected: {', '.join(selected)}"]
    for item in decisions:
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        relevance = item.get("relevance")
        reason = item.get("reason")
        if skill_id:
            detail = f"- {skill_id}"
            if relevance is not None:
                detail += f" (relevance={relevance})"
            if reason:
                detail += f": {reason}"
            parts.append(detail)
    return "\n".join(parts)
