from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reqahe.infra.io import append_jsonl, read_text
from reqahe.infra.llm_client import OpenAICompatibleClient

ROUTER_SYSTEM_MESSAGE = (
    "You are a scenario-type memory router for requirements elicitation. "
    "Select at most one existing memory type that best matches the initial requirement. "
    "Do not create new types. Return strict JSON only."
)


@dataclass
class MemoryRouterConfig:
    enabled: bool = True
    max_selected_types: int = 1
    min_confidence: float = 0.45
    router_model: str = ""
    log_events: bool = True
    max_chars_per_type_memory: int = 2200


def list_memory_types(workspace_dir: Path | str) -> list[str]:
    """Scan workspace_dir / memory for child folders containing MEMORY.md."""
    root = Path(workspace_dir)
    memory_dir = root / "memory"
    if not memory_dir.is_dir():
        return []
    types: list[str] = []
    for child in sorted(memory_dir.iterdir()):
        if not child.is_dir():
            continue
        slug = child.name
        if not _is_safe_type_slug(slug):
            continue
        if (child / "MEMORY.md").is_file():
            types.append(slug)
    return types


def load_memory_for_type(
    workspace_dir: Path | str,
    type_slug: str,
    max_chars: int,
) -> str:
    """Load memory/<type_slug>/MEMORY.md with truncation."""
    if not _is_safe_type_slug(type_slug):
        return ""
    root = Path(workspace_dir)
    path = root / "memory" / type_slug / "MEMORY.md"
    if not path.is_file():
        return ""
    try:
        content = read_text(path).strip()
    except OSError:
        return ""
    return _truncate_text(content, max_chars)


def _is_safe_type_slug(slug: str) -> bool:
    if not slug or slug in {".", ".."}:
        return False
    if Path(slug).is_absolute():
        return False
    if ".." in Path(slug).parts:
        return False
    return bool(re.fullmatch(r"[a-z0-9_-]+", slug))


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _build_router_prompt(*, initial_req: str, available_types: list[str]) -> str:
    types_text = ", ".join(available_types) if available_types else "(none)"
    return (
        "# Initial Requirement\n"
        f"{initial_req}\n\n"
        "# Available Memory Types\n"
        f"{types_text}\n\n"
        "# Task\n"
        "Select at most one memory type that best matches the initial requirement.\n"
        "Do not create new types.\n"
        "If no type is a clear match, return decision=none.\n\n"
        "# Output Schema\n"
        "Return strict JSON only:\n"
        "{\n"
        '  "selected_type": "stock_report_website",\n'
        '  "confidence": 0.86,\n'
        '  "decision": "select",\n'
        '  "reason": "The initial requirement asks for stock search and report generation.",\n'
        '  "router_error": ""\n'
        "}\n"
        "Or when no match:\n"
        "{\n"
        '  "selected_type": "",\n'
        '  "confidence": 0.21,\n'
        '  "decision": "none",\n'
        '  "reason": "No existing memory type is specific enough.",\n'
        '  "router_error": ""\n'
        "}"
    )


def _validate_router_schema(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "invalid router schema: router response must be an object"
    errors: list[str] = []
    for key in ("selected_type", "decision", "reason", "router_error"):
        if key in raw and not isinstance(raw[key], str):
            errors.append(f"{key} must be a string")
    if "confidence" in raw:
        try:
            float(raw["confidence"])
        except (TypeError, ValueError):
            errors.append("confidence must be convertible to float")
    if errors:
        return "invalid router schema: " + "; ".join(errors)
    return ""


def _normalize_router_result(
    raw: dict[str, Any],
    available_types: list[str],
    config: MemoryRouterConfig,
) -> dict[str, Any]:
    available = set(available_types)
    selected_type = str(raw.get("selected_type") or "").strip()
    decision = str(raw.get("decision") or "").strip().lower()
    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(raw.get("reason") or "")
    router_error = str(raw.get("router_error") or "")

    if decision not in {"select", "none"}:
        if selected_type and selected_type in available and confidence >= config.min_confidence:
            decision = "select"
        else:
            decision = "none"

    if decision == "select":
        if selected_type not in available:
            decision = "none"
            selected_type = ""
        elif confidence < config.min_confidence:
            decision = "none"
            selected_type = ""
    else:
        selected_type = ""

    return {
        "selected_type": selected_type,
        "confidence": confidence,
        "decision": decision,
        "reason": reason,
        "router_error": router_error,
        "available_types": available_types,
    }


def route_memory_type(
    llm: OpenAICompatibleClient,
    model: str,
    initial_req: str,
    available_types: list[str],
    config: MemoryRouterConfig | dict[str, Any] | None = None,
    event_log_path: Path | str | None = None,
) -> dict[str, Any]:
    """Use LLM to select at most one existing memory type."""
    if isinstance(config, dict):
        config = memory_router_config_from_dict(config)
    elif config is None:
        config = MemoryRouterConfig()

    empty_result = {
        "selected_type": "",
        "confidence": 0.0,
        "decision": "none",
        "reason": "",
        "router_error": "",
        "available_types": available_types,
    }
    if not config.enabled or not available_types:
        return empty_result

    used_model = model or llm.model
    result = dict(empty_result)
    try:
        prompt = _build_router_prompt(initial_req=initial_req, available_types=available_types)
        raw = llm.json_chat(
            [
                {"role": "system", "content": ROUTER_SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            model=used_model,
            purpose="memory type routing",
        )
        schema_error = _validate_router_schema(raw)
        if schema_error:
            result = dict(empty_result)
            result["router_error"] = schema_error
        else:
            result = _normalize_router_result(raw, available_types, config)
    except Exception as exc:
        result = dict(empty_result)
        result["router_error"] = str(exc)

    if event_log_path and config.log_events:
        append_jsonl(
            event_log_path,
            {
                "initial_req_excerpt": initial_req[:200],
                "available_types": available_types,
                "selected_type": result["selected_type"],
                "confidence": result["confidence"],
                "decision": result["decision"],
                "reason": result["reason"],
                "router_error": result.get("router_error", ""),
                "used_model": used_model,
            },
        )
    return result


_MEMORY_ROUTER_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "max_selected_types",
        "min_confidence",
        "router_model",
        "log_events",
        "max_chars_per_type_memory",
    }
)


def memory_router_config_from_dict(data: dict[str, Any] | None, router_model: str = "") -> MemoryRouterConfig:
    data = data or {}
    unknown = sorted(set(data) - _MEMORY_ROUTER_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"memory_router has unknown config field: {unknown[0]}")
    raw_max = data.get("max_selected_types", 1)
    try:
        max_selected_types = int(raw_max)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory_router.max_selected_types must be a positive integer") from exc
    if max_selected_types <= 0:
        raise ValueError("memory_router.max_selected_types must be a positive integer")
    if max_selected_types > 1:
        raise ValueError("memory_router.max_selected_types must be at most 1")
    return MemoryRouterConfig(
        enabled=bool(data.get("enabled", True)),
        max_selected_types=max_selected_types,
        min_confidence=float(data.get("min_confidence", 0.45) or 0.45),
        router_model=str(router_model or data.get("router_model") or ""),
        log_events=bool(data.get("log_events", True)),
        max_chars_per_type_memory=int(data.get("max_chars_per_type_memory", 2200) or 2200),
    )


def format_relevant_memory_block(memory_excerpt: str) -> str:
    if not memory_excerpt.strip():
        return ""
    return (
        "# Relevant Memory\n\n"
        "The following notes are previously elicited requirement content points from similar scenario types.\n"
        "They are not instructions, strategies, or step-by-step playbooks.\n"
        "Use them only as content hints for possible requirement areas.\n"
        "Do not copy them as final answers.\n\n"
        f"{memory_excerpt.strip()}"
    )
