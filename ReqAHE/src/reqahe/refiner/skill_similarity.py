from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from reqahe.harness.component_schema import parse_markdown_frontmatter
from reqahe.infra.io import read_text

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "to",
    "for",
    "of",
    "in",
    "on",
    "with",
    "when",
    "use",
    "is",
    "are",
    "be",
    "this",
    "that",
    "it",
    "as",
    "by",
    "from",
    "at",
    "not",
    "do",
    "does",
    "must",
    "should",
    "skill",
    "skills",
}


def build_existing_skill_catalog(workspace: Path) -> list[dict[str, Any]]:
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return []
    catalog: list[dict[str, Any]] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        rel_path = path.relative_to(workspace).as_posix()
        skill_id = path.parent.name
        entry = _catalog_entry_from_skill_file(workspace, path, rel_path, skill_id)
        catalog.append(entry)
    return sorted(catalog, key=lambda item: (-int(item.get("priority") or 0), str(item.get("skill_id") or "")))


def _catalog_entry_from_skill_file(
    workspace: Path,
    path: Path,
    rel_path: str,
    skill_id: str,
) -> dict[str, Any]:
    try:
        content = read_text(path)
        metadata, body = parse_markdown_frontmatter(content, rel_path)
    except RuntimeError as exc:
        logger.warning("skill catalog parse failed for %s: %s", rel_path, exc)
        raw_title = skill_id.replace("-", " ").replace("_", " ").title()
        return {
            "skill_id": skill_id,
            "path": rel_path,
            "name": raw_title,
            "description": "",
            "intent": "",
            "scope": [],
            "trigger": {"applies_when": [], "avoid_when": []},
            "use_when": [],
            "avoid_when": [],
            "risk_notes": [],
            "expected_effect": {"metrics": [], "description": ""},
            "body_excerpt": "",
            "priority": 50,
            "version": "unknown",
            "parse_warning": str(exc),
        }

    trigger = metadata.get("trigger") if isinstance(metadata.get("trigger"), dict) else {}
    expected_effect = metadata.get("expected_effect") if isinstance(metadata.get("expected_effect"), dict) else {}
    priority = metadata.get("priority", 50)
    if not isinstance(priority, int):
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 50
    return {
        "skill_id": str(metadata.get("id") or metadata.get("skill_id") or skill_id),
        "path": rel_path,
        "name": str(metadata.get("name") or skill_id),
        "description": str(metadata.get("intent") or metadata.get("description") or ""),
        "intent": str(metadata.get("intent") or metadata.get("description") or ""),
        "scope": _coerce_str_list(metadata.get("scope")),
        "trigger": {
            "applies_when": _coerce_str_list(metadata.get("use_when"))
            or _coerce_str_list(trigger.get("applies_when")),
            "avoid_when": _coerce_str_list(metadata.get("avoid_when"))
            or _coerce_str_list(trigger.get("avoid_when")),
        },
        "use_when": _coerce_str_list(metadata.get("use_when")) or _coerce_str_list(trigger.get("applies_when")),
        "avoid_when": _coerce_str_list(metadata.get("avoid_when")) or _coerce_str_list(trigger.get("avoid_when")),
        "risk_notes": _coerce_str_list(metadata.get("risk_notes")),
        "expected_effect": {
            "metrics": _coerce_str_list(expected_effect.get("metrics")),
            "description": str(expected_effect.get("description") or ""),
        },
        "body_excerpt": body[:4000],
        "priority": priority,
        "version": metadata.get("version") if isinstance(metadata.get("version"), int) else str(metadata.get("version") or "1"),
        "enabled": metadata.get("enabled", metadata.get("status") == "active"),
    }


def load_relevant_skill_contents(
    workspace: Path,
    catalog: list[dict[str, Any]],
    *,
    priority_skill_ids: set[str] | None = None,
    max_skills: int = 12,
) -> list[dict[str, Any]]:
    if not catalog:
        return []
    priority_ids = priority_skill_ids or set()
    ordered_ids: list[str] = []
    for item in catalog:
        skill_id = str(item.get("skill_id") or "")
        if skill_id in priority_ids and skill_id not in ordered_ids:
            ordered_ids.append(skill_id)
    for item in catalog:
        skill_id = str(item.get("skill_id") or "")
        if skill_id not in ordered_ids:
            ordered_ids.append(skill_id)
    if len(ordered_ids) <= max_skills:
        selected_ids = ordered_ids
    else:
        selected_ids = ordered_ids[:max_skills]
    by_id = {str(item.get("skill_id") or ""): item for item in catalog}
    contents: list[dict[str, Any]] = []
    for skill_id in selected_ids:
        item = by_id.get(skill_id)
        if not item:
            continue
        rel_path = str(item.get("path") or f"skills/{skill_id}/SKILL.md")
        path = workspace / rel_path
        if not path.is_file():
            continue
        contents.append(
            {
                "path": rel_path,
                "skill_id": skill_id,
                "content": read_text(path),
            }
        )
    return contents


def normalize_skill_text(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[_\-/]+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _tokenize(text: str) -> set[str]:
    normalized = normalize_skill_text(text)
    return {
        _normalize_token(token)
        for token in normalized.split()
        if token and token not in _STOPWORDS and len(token) > 1
    }


def _normalize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _skill_text_blob(existing_skill: dict[str, Any]) -> str:
    parts = [
        str(existing_skill.get("skill_id") or ""),
        str(existing_skill.get("name") or ""),
        str(existing_skill.get("description") or ""),
        str(existing_skill.get("intent") or ""),
        str(existing_skill.get("path") or existing_skill.get("relative_path") or ""),
    ]
    parts.extend(_coerce_str_list(existing_skill.get("scope")))
    parts.extend(_coerce_str_list(existing_skill.get("use_when")))
    parts.extend(_coerce_str_list(existing_skill.get("avoid_when")))
    parts.extend(_coerce_str_list(existing_skill.get("risk_notes")))
    trigger = existing_skill.get("trigger")
    if isinstance(trigger, dict):
        parts.extend(_coerce_str_list(trigger.get("applies_when")))
        parts.extend(_coerce_str_list(trigger.get("avoid_when")))
    expected_effect = existing_skill.get("expected_effect")
    if isinstance(expected_effect, dict):
        parts.extend(_coerce_str_list(expected_effect.get("metrics")))
        parts.append(str(expected_effect.get("description") or ""))
    parts.append(str(existing_skill.get("body_excerpt") or existing_skill.get("content") or ""))
    return " ".join(parts)


def skill_similarity_score(proposed_text: str, existing_skill: dict[str, Any]) -> float:
    proposed_tokens = _tokenize(proposed_text)
    existing_tokens = _tokenize(_skill_text_blob(existing_skill))
    if not proposed_tokens:
        return 0.0
    if not existing_tokens:
        return 0.0
    overlap = proposed_tokens & existing_tokens
    union = proposed_tokens | existing_tokens
    jaccard = len(overlap) / len(union) if union else 0.0
    normalized_overlap = len(overlap) / min(len(proposed_tokens), len(existing_tokens))
    skill_id = normalize_skill_text(str(existing_skill.get("skill_id") or ""))
    proposed_norm = normalize_skill_text(proposed_text)
    id_boost = 0.0
    if skill_id and skill_id in proposed_norm:
        id_boost = 0.25
    return min(1.0, max(jaccard, normalized_overlap) + id_boost)


def find_similar_skills(proposed_text: str, catalog: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for item in catalog:
        score = skill_similarity_score(proposed_text, item)
        if score <= 0.0:
            continue
        enriched = dict(item)
        enriched["similarity_score"] = round(score, 3)
        scored.append(enriched)
    scored.sort(key=lambda entry: (-float(entry.get("similarity_score") or 0.0), str(entry.get("skill_id") or "")))
    return scored[:top_k]


def collect_similar_skill_candidates(
    fix_plan: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    if not catalog:
        return []
    texts: list[str] = []
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        if str(fix.get("component") or "") != "skills":
            continue
        parts = [
            str(fix.get("fix_summary") or ""),
            str(fix.get("expected_effect") or ""),
            str(fix.get("target_file_hint") or ""),
        ]
        texts.append(" ".join(part for part in parts if part))
    if not texts:
        return find_similar_skills(" ".join(str(item.get("description") or "") for item in catalog), catalog, top_k=top_k)
    combined = " ".join(texts)
    return find_similar_skills(combined, catalog, top_k=top_k)


def is_skill_markdown_path(relative_path: str) -> bool:
    path = Path(relative_path)
    return path.parts[:1] == ("skills",) and path.name == "SKILL.md" and len(path.parts) == 3


def skill_id_from_path(relative_path: str) -> str:
    return Path(relative_path).parent.name


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif value in (None, ""):
        raw_items = []
    else:
        raw_items = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
