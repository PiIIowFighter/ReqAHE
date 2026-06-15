from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from reqahe.infra.io import append_jsonl, ensure_dir, read_json, read_text, write_json
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.runtime.memory_router import list_memory_types, load_memory_for_type

PROMPT_DIR = Path(__file__).with_name("prompts")

_STRATEGY_PATTERNS = [
    re.compile(r"\bask\b", re.IGNORECASE),
    re.compile(r"\binterviewer should\b", re.IGNORECASE),
    re.compile(r"\bnext time\b", re.IGNORECASE),
    re.compile(r"\bprobe earlier\b", re.IGNORECASE),
    re.compile(r"\bfollow up\b", re.IGNORECASE),
    re.compile(r"\bfollow-up\b", re.IGNORECASE),
]
_LEAKAGE_PATTERNS = [
    re.compile(r"\bscenario_id\b", re.IGNORECASE),
    re.compile(r"\bimplicit requirement id\b", re.IGNORECASE),
    re.compile(r"\bhidden\b", re.IGNORECASE),
    re.compile(r"\boracle answer\b", re.IGNORECASE),
    re.compile(r"\bIR\d+\b"),
]


def memorize_config_from_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    data = data or {}
    apply_timing = str(data.get("apply_timing") or "next_batch").strip() or "next_batch"
    if apply_timing != "next_batch":
        apply_timing = "next_batch"
    return {
        "enabled": bool(data.get("enabled", True)),
        "apply_timing": apply_timing,
        "max_points_per_trace": int(data.get("max_points_per_trace", 6) or 6),
        "max_chars_per_point": int(data.get("max_chars_per_point", 180) or 180),
        "max_points_per_type": int(data.get("max_points_per_type", 40) or 40),
        "max_chars_per_type_memory": int(data.get("max_chars_per_type_memory", 3000) or 3000),
        "deduplicate": bool(data.get("deduplicate", True)),
        "log_events": bool(data.get("log_events", True)),
    }


def load_memorizer_prompt() -> str:
    path = PROMPT_DIR / "memorize_hit_points.md"
    if not path.exists():
        raise RuntimeError("memorizer prompt not found: memorize_hit_points.md")
    return path.read_text(encoding="utf-8")


def memorize_rollout(
    *,
    batch_dir: Path,
    rollout_dir: Path,
    workspace_dir: Path,
    llm: OpenAICompatibleClient,
    model: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = memorize_config_from_dict(config)
    result_path = batch_dir / "memorize_result.json"
    events_path = batch_dir / "memorize_events.jsonl"

    apply_timing = cfg["apply_timing"]

    if not cfg["enabled"]:
        result = {
            "skip": True,
            "skip_reason": "memorizer disabled",
            "written_points": [],
            "type_slug": "",
            "apply_timing": apply_timing,
        }
        write_json(result_path, result)
        return result

    traces = _load_rollout_traces(rollout_dir)
    if not traces:
        result = {
            "skip": True,
            "skip_reason": "no rollout traces found",
            "written_points": [],
            "type_slug": "",
            "apply_timing": apply_timing,
        }
        write_json(result_path, result)
        return result

    available_types = list_memory_types(workspace_dir)
    aggregated_successful_turns: list[dict[str, Any]] = []
    initial_req = ""
    for trace in traces:
        initial_req = initial_req or str(trace.get("initial_req") or trace.get("initial_requirement") or "")
        aggregated_successful_turns.extend(_extract_successful_turns(trace))

    if not aggregated_successful_turns:
        result = {
            "skip": True,
            "skip_reason": "no successful turns in rollout",
            "written_points": [],
            "type_slug": "",
            "trace_count": len(traces),
            "apply_timing": apply_timing,
        }
        write_json(result_path, result)
        return result

    candidate_type = available_types[0] if len(available_types) == 1 else ""
    current_excerpt = load_memory_for_type(workspace_dir, candidate_type, cfg["max_chars_per_type_memory"]) if candidate_type else ""

    payload = {
        "initial_req": initial_req,
        "available_memory_types": available_types,
        "successful_turns": aggregated_successful_turns[:30],
        "current_type_memory_excerpt": current_excerpt,
        "trace_count": len(traces),
    }

    raw = llm.json_chat(
        [
            {"role": "system", "content": load_memorizer_prompt()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=model or llm.model,
        purpose="memorize hit points",
    )

    validated = _validate_memorizer_output(raw, cfg, available_types)
    if validated.get("skip"):
        validated["apply_timing"] = apply_timing
        write_json(result_path, validated)
        if cfg["log_events"]:
            append_jsonl(events_path, {"event": "skip", "result": validated})
        return validated

    type_slug = validated["type_slug"]
    display_name = validated["display_name"]
    hit_points = validated["hit_points"]
    memory_path = workspace_dir / "memory" / type_slug / "MEMORY.md"
    ensure_dir(memory_path.parent)
    existing_content = read_text(memory_path) if memory_path.exists() else ""
    existing_points = _parse_recorded_hit_points(existing_content)
    new_points = [point for point in hit_points if point not in existing_points]
    if cfg["deduplicate"]:
        new_points = [point for point in new_points if not _is_duplicate(point, existing_points)]

    if not new_points:
        result = {
            "skip": True,
            "skip_reason": "all hit points already recorded or rejected",
            "written_points": [],
            "type_slug": type_slug,
            "trace_count": len(traces),
            "apply_timing": apply_timing,
        }
        write_json(result_path, result)
        if cfg["log_events"]:
            append_jsonl(events_path, {"event": "no_new_points", "type_slug": type_slug})
        return result

    merged_points = (existing_points + new_points)[-cfg["max_points_per_type"] :]
    content = _render_memory_md(display_name, merged_points)
    content = _truncate_text(content, cfg["max_chars_per_type_memory"])
    memory_path.write_text(content, encoding="utf-8")

    result = {
        "skip": False,
        "skip_reason": "",
        "type_slug": type_slug,
        "display_name": display_name,
        "written_points": new_points,
        "total_points": len(merged_points),
        "memory_path": f"memory/{type_slug}/MEMORY.md",
        "trace_count": len(traces),
        "apply_timing": apply_timing,
    }
    write_json(result_path, result)
    if cfg["log_events"]:
        append_jsonl(
            events_path,
            {
                "event": "write",
                "type_slug": type_slug,
                "written_points": new_points,
                "trace_count": len(traces),
            },
        )
    return result


def _load_rollout_traces(rollout_dir: Path) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    seen_trace_dirs: set[str] = set()
    task_results_path = rollout_dir / "task_results.json"
    if task_results_path.exists():
        task_results = read_json(task_results_path)
        if isinstance(task_results, list):
            for item in task_results:
                if not isinstance(item, dict):
                    continue
                trace_dir = item.get("trace_dir")
                if not trace_dir:
                    continue
                candidate = Path(trace_dir)
                if not candidate.is_absolute():
                    candidate = rollout_dir / candidate
                trace = _read_trace_file(candidate)
                if trace:
                    traces.append(trace)
                    seen_trace_dirs.add(str(candidate.resolve()))
    for clean_trace in sorted(rollout_dir.glob("*/clean_trace.json")):
        trace_dir = clean_trace.parent.resolve()
        if str(trace_dir) in seen_trace_dirs:
            continue
        trace = read_json(clean_trace)
        if isinstance(trace, dict):
            traces.append(trace)
            seen_trace_dirs.add(str(trace_dir))
    return traces


def _read_trace_file(trace_dir: Path) -> dict[str, Any] | None:
    clean = trace_dir / "clean_trace.json"
    if clean.exists():
        data = read_json(clean)
        return data if isinstance(data, dict) else None
    return None


def _extract_successful_turns(trace: dict[str, Any]) -> list[dict[str, Any]]:
    successful: list[dict[str, Any]] = []
    initial_req = str(trace.get("initial_req") or trace.get("initial_requirement") or "")
    for turn in trace.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        judgement = turn.get("judgement") or {}
        if not isinstance(judgement, dict):
            continue
        is_relevant = bool(judgement.get("is_relevant_to_implied_requirements"))
        elicited = judgement.get("elicited_requirement_ids") or []
        if not is_relevant and not elicited:
            continue
        successful.append(
            {
                "turn_index": turn.get("turn_index"),
                "question": str(turn.get("question") or ""),
                "user_response": str(turn.get("user_response") or turn.get("answer") or ""),
                "is_relevant": is_relevant,
                "has_elicited": bool(elicited),
                "initial_req": initial_req,
            }
        )
    return successful


def _validate_memorizer_output(
    raw: Any,
    cfg: dict[str, Any],
    available_types: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"skip": True, "skip_reason": "invalid memorizer output", "written_points": [], "type_slug": ""}
    if raw.get("skip"):
        return {
            "skip": True,
            "skip_reason": str(raw.get("skip_reason") or "memorizer skipped"),
            "written_points": [],
            "type_slug": "",
        }

    scenario_type = raw.get("scenario_type") if isinstance(raw.get("scenario_type"), dict) else {}
    type_slug = _safe_type_slug(str(scenario_type.get("type_slug") or ""))
    display_name = str(scenario_type.get("display_name") or type_slug.replace("_", " ").title()).strip()
    match_status = str(scenario_type.get("match_status") or "").lower()
    matched_existing = _safe_type_slug(str(scenario_type.get("matched_existing_type") or ""))

    if match_status == "existing" and matched_existing in available_types:
        type_slug = matched_existing
    if not type_slug:
        return {"skip": True, "skip_reason": "invalid type_slug", "written_points": [], "type_slug": ""}

    hit_points_raw = raw.get("hit_points") if isinstance(raw.get("hit_points"), list) else []
    validated_points: list[str] = []
    for item in hit_points_raw[: cfg["max_points_per_trace"]]:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        if not point:
            continue
        if not _validate_hit_point(point, cfg["max_chars_per_point"]):
            continue
        validated_points.append(point)

    if not validated_points:
        return {"skip": True, "skip_reason": "no valid hit points", "written_points": [], "type_slug": type_slug}

    return {
        "skip": False,
        "skip_reason": "",
        "type_slug": type_slug,
        "display_name": display_name,
        "hit_points": validated_points,
        "written_points": validated_points,
    }


def _safe_type_slug(raw: str) -> str:
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "_", slug)
    slug = slug.strip("_-")
    if not slug or slug in {".", ".."} or ".." in slug.split("/"):
        return ""
    if not re.fullmatch(r"[a-z0-9_-]+", slug):
        return ""
    return slug


def _validate_hit_point(point: str, max_chars: int) -> bool:
    if len(point) > max_chars:
        return False
    if point.count(".") > 2 or point.count("?") > 0:
        return False
    for pattern in _LEAKAGE_PATTERNS + _STRATEGY_PATTERNS:
        if pattern.search(point):
            return False
    return True


def _is_duplicate(point: str, existing: list[str]) -> bool:
    normalized = _normalize_point(point)
    for item in existing:
        if _normalize_point(item) == normalized:
            return True
        if _similarity(normalized, _normalize_point(item)) >= 0.88:
            return True
    return False


def _normalize_point(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _parse_recorded_hit_points(content: str) -> list[str]:
    points: list[str] = []
    in_section = False
    for line in content.splitlines():
        if line.strip().lower() == "## recorded hit points":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.strip().startswith("- "):
            points.append(line.strip()[2:].strip())
    return points


def _render_memory_md(display_name: str, points: list[str]) -> str:
    lines = [
        f"# {display_name}",
        "",
        "## Scope",
        "",
        f"This memory records concise requirement content points previously elicited in scenarios similar to: {display_name}.",
        "",
        "## Recorded Hit Points",
        "",
    ]
    lines.extend(f"- {point}" for point in points)
    return "\n".join(lines).rstrip() + "\n"


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    truncated = text[: max_chars - 3].rstrip()
    section_idx = truncated.rfind("## Recorded Hit Points")
    if section_idx == -1:
        return truncated + "..."
    body = truncated[section_idx:]
    lines = body.splitlines()
    kept = [lines[0], lines[1] if len(lines) > 1 else ""]
    for line in lines[2:]:
        if line.strip().startswith("- "):
            candidate = "\n".join(kept + [line])
            if len(truncated[:section_idx] + candidate) > max_chars - 3:
                break
            kept.append(line)
    prefix = truncated[:section_idx]
    return (prefix + "\n".join(kept)).rstrip() + "\n"
