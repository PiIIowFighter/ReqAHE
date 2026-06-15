from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reqahe.infra.io import append_jsonl, read_json, read_jsonl, write_json, write_text
from reqahe.utils.paths import to_posix_relpath


MAX_SAMPLE_QUESTIONS = 5
MAX_SAMPLE_REASONS = 5
MAX_DIGEST_SAMPLES = 2


def build_router_reason(routing: dict[str, Any]) -> str:
    if routing.get("router_error"):
        return str(routing.get("router_error") or "")
    decisions = routing.get("decisions") or []
    reasons: list[str] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if reason:
            skill_id = str(item.get("skill_id") or "").strip()
            if skill_id:
                reasons.append(f"{skill_id}: {reason}")
            else:
                reasons.append(reason)
    if reasons:
        return "; ".join(reasons)
    selected = routing.get("selected_skill_ids") or []
    if selected:
        return f"selected {', '.join(str(item) for item in selected)}"
    return ""


def make_route_event(
    *,
    task_id: str,
    turn_index: int,
    candidate_skill_ids: list[str],
    selected_skill_ids: list[str],
    router_reason: str,
    question: str = "",
    answer: str = "",
    turn_hit: bool | None = None,
    hit_targets: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "turn_index": turn_index,
        "candidate_skill_ids": list(candidate_skill_ids),
        "selected_skill_ids": list(selected_skill_ids),
        "router_reason": router_reason,
        "question": question,
        "answer": answer,
        "turn_hit": turn_hit,
        "hit_targets": list(hit_targets or []),
    }


def enrich_route_events_from_trace(
    events: list[dict[str, Any]],
    *,
    scenario_id: str,
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    turn_by_index = {
        int(turn.get("turn_index", -1)): turn
        for turn in turns
        if isinstance(turn, dict)
    }
    enriched: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        item = dict(event)
        item.setdefault("task_id", scenario_id)
        turn_index = int(item.get("turn_index", -1))
        turn = turn_by_index.get(turn_index)
        if turn and turn.get("action") == "ask_question":
            item["question"] = str(turn.get("question") or item.get("question") or "")
            item["answer"] = str(turn.get("user_response") or item.get("answer") or "")
            judgement = turn.get("judgement") if isinstance(turn.get("judgement"), dict) else {}
            if judgement:
                item["turn_hit"] = bool(judgement.get("is_relevant_to_implied_requirements"))
                item["hit_targets"] = [
                    str(req_id)
                    for req_id in judgement.get("elicited_requirement_ids") or []
                    if str(req_id).strip()
                ]
        enriched.append(item)
    return enriched


def aggregate_route_stats(events: list[dict[str, Any]], *, router_skill_ids: list[str] | None = None) -> dict[str, Any]:
    router_ids = list(router_skill_ids or [])
    selected_ids: set[str] = set()
    skill_stats: dict[str, dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        turn_index = int(event.get("turn_index", 0))
        for skill_id in event.get("selected_skill_ids") or []:
            skill_id = str(skill_id)
            if not skill_id:
                continue
            selected_ids.add(skill_id)
            stats = skill_stats.setdefault(
                skill_id,
                {
                    "selected_count": 0,
                    "hit_count": 0,
                    "first_selected_turn": turn_index,
                    "last_selected_turn": turn_index,
                    "sample_questions": [],
                    "sample_router_reasons": [],
                },
            )
            stats["selected_count"] += 1
            stats["first_selected_turn"] = min(stats["first_selected_turn"], turn_index)
            stats["last_selected_turn"] = max(stats["last_selected_turn"], turn_index)
            if event.get("turn_hit") is True:
                stats["hit_count"] += 1
            question = str(event.get("question") or "").strip()
            if question and question not in stats["sample_questions"]:
                if len(stats["sample_questions"]) < MAX_SAMPLE_QUESTIONS:
                    stats["sample_questions"].append(question)
            reason = str(event.get("router_reason") or "").strip()
            if reason and reason not in stats["sample_router_reasons"]:
                if len(stats["sample_router_reasons"]) < MAX_SAMPLE_REASONS:
                    stats["sample_router_reasons"].append(reason)

    total_turns = len(events)
    for skill_id, stats in skill_stats.items():
        selected_count = int(stats["selected_count"])
        hit_count = int(stats["hit_count"])
        stats["selection_share"] = round(selected_count / total_turns, 4) if total_turns else 0.0
        stats["hit_rate"] = round(hit_count / selected_count, 4) if selected_count else 0.0

    available_ids = router_ids or sorted(selected_ids)
    unselected = sorted(skill_id for skill_id in available_ids if skill_id not in selected_ids)
    return {
        "total_turns": total_turns,
        "skills": skill_stats,
        "unselected_skills": unselected,
    }


def render_route_stats_digest(stats: dict[str, Any]) -> str:
    total_turns = int(stats.get("total_turns") or 0)
    skills = stats.get("skills") if isinstance(stats.get("skills"), dict) else {}
    unselected = stats.get("unselected_skills") if isinstance(stats.get("unselected_skills"), list) else []
    ranked = sorted(
        skills.items(),
        key=lambda pair: (-int(pair[1].get("selected_count", 0)), pair[0]),
    )
    lines = [
        "# Route Stats Digest",
        "",
        "## Selection Summary",
        "",
        f"- Total turns: {total_turns}",
    ]
    if ranked:
        lines.append("- Selected skills:")
        for skill_id, item in ranked:
            lines.append(
                f"  - `{skill_id}`: selected {item.get('selected_count', 0)} times, "
                f"selection_share={item.get('selection_share', 0)}."
            )
    else:
        lines.append("- Selected skills: (none)")
    lines.extend(["", "## Skill Hit Summary", ""])
    if ranked:
        for skill_id, item in ranked:
            lines.append(
                f"- `{skill_id}`: selected {item.get('selected_count', 0)} times, "
                f"hit {item.get('hit_count', 0)} times, hit_rate={item.get('hit_rate', 0)}."
            )
    else:
        lines.append("- (none)")
    lines.extend(["", "## Skills Available But Never Selected", ""])
    if unselected:
        for skill_id in unselected:
            lines.append(f"- `{skill_id}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Evidence Samples", ""])
    for skill_id, item in sorted(skills.items()):
        lines.append(f"### {skill_id}")
        lines.append("")
        lines.append("Selected questions:")
        samples = item.get("sample_questions") or []
        if samples:
            for idx, question in enumerate(samples[:MAX_DIGEST_SAMPLES], start=1):
                lines.append(f"{idx}. {question}")
        else:
            lines.append("1. (none recorded)")
        lines.append("")
        lines.append("Router reasons:")
        reasons = item.get("sample_router_reasons") or []
        if reasons:
            for idx, reason in enumerate(reasons[:MAX_DIGEST_SAMPLES], start=1):
                lines.append(f"{idx}. {reason}")
        else:
            lines.append("1. (none recorded)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def compact_route_stats_summary(stats: dict[str, Any], *, max_skills: int = 8) -> dict[str, Any]:
    skills = stats.get("skills") if isinstance(stats.get("skills"), dict) else {}
    compact_skills: dict[str, Any] = {}
    ranked = sorted(
        skills.items(),
        key=lambda pair: (-int(pair[1].get("selected_count", 0)), pair[0]),
    )
    for skill_id, item in ranked[:max_skills]:
        compact_skills[skill_id] = {
            "selected_count": item.get("selected_count", 0),
            "selection_share": item.get("selection_share", 0),
            "hit_count": item.get("hit_count", 0),
            "hit_rate": item.get("hit_rate", 0),
        }
    return {
        "total_turns": stats.get("total_turns", 0),
        "skills": compact_skills,
        "unselected_skills": stats.get("unselected_skills", []),
    }


def collect_rollout_route_events(rollout_dir: Path, task_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rollout = Path(rollout_dir)
    events: list[dict[str, Any]] = []
    for result in task_results:
        if not isinstance(result, dict):
            continue
        trace_rel = str(result.get("trace_dir") or "")
        if not trace_rel:
            continue
        trace_dir = rollout / trace_rel
        route_events_path = trace_dir / "route_events.jsonl"
        if route_events_path.exists():
            events.extend(read_jsonl(route_events_path))
            continue
        trace_path = trace_dir / "clean_trace.json"
        if not trace_path.exists():
            continue
        trace = read_json(trace_path)
        scenario_id = str(result.get("scenario_id") or trace.get("scenario_id") or "")
        scenario_events: list[dict[str, Any]] = []
        for routing_event in trace.get("skill_routing_events") or []:
            if not isinstance(routing_event, dict):
                continue
            scenario_events.append(
                make_route_event(
                    task_id=scenario_id,
                    turn_index=int(routing_event.get("turn_index", 0)),
                    candidate_skill_ids=routing_event.get("candidate_skill_ids") or [],
                    selected_skill_ids=routing_event.get("selected_skill_ids") or [],
                    router_reason=str(routing_event.get("router_reason") or ""),
                )
            )
        enriched_scenario_events = enrich_route_events_from_trace(
            scenario_events,
            scenario_id=scenario_id,
            turns=trace.get("turns") or [],
        )
        events.extend(enriched_scenario_events)
    return events


def write_rollout_route_stats(
    rollout_dir: str | Path,
    task_results: list[dict[str, Any]],
    *,
    router_skill_ids: list[str] | None = None,
) -> dict[str, Any]:
    rollout = Path(rollout_dir)
    events = collect_rollout_route_events(rollout, task_results)
    stats = aggregate_route_stats(events, router_skill_ids=router_skill_ids)
    events_path = rollout / "route_events.jsonl"
    stats_path = rollout / "route_stats.json"
    digest_path = rollout / "route_stats_digest.md"
    if events_path.exists():
        events_path.unlink()
    for event in events:
        append_jsonl(events_path, event)
    write_json(stats_path, stats)
    write_text(digest_path, render_route_stats_digest(stats))
    return stats


def load_route_stats_artifacts(rollout_dir: str | Path) -> dict[str, Any]:
    rollout = Path(rollout_dir)
    artifacts: dict[str, Any] = {}
    digest_path = rollout / "route_stats_digest.md"
    stats_path = rollout / "route_stats.json"
    if digest_path.is_file():
        artifacts["route_stats_digest_md"] = digest_path.read_text(encoding="utf-8")
    if stats_path.is_file():
        artifacts["route_stats_summary"] = compact_route_stats_summary(read_json(stats_path))
    return artifacts
