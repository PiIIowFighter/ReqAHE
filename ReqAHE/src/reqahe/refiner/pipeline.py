from __future__ import annotations

import ast
import json
import re
import subprocess
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from reqahe.diagnoser.pipeline import NON_EVOLVABLE_COMPONENTS, load_declared_components
from reqahe.harness.component_schema import (
    ALLOWED_ARTIFACT_TYPES,
    REFLECTION_HOOKS,
    REFLECTION_MODES,
    load_reflection_registry,
    parse_markdown_frontmatter,
    validate_schema_compliance_block,
    validate_workspace_component_schemas,
)
from reqahe.harness.component_spec import is_declared_harness_path, path_component
from reqahe.harness.workspace import (
    load_skill_catalog_summary,
    load_skill_schema_errors_summary,
    workspace_allowed_files,
    write_workspace_file,
)
from reqahe.runtime.route_stats import load_route_stats_artifacts
from reqahe.infra.io import ensure_dir, read_json, read_text, write_json, write_text
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.refiner.skill_similarity import (
    build_existing_skill_catalog,
    collect_similar_skill_candidates,
    is_skill_markdown_path,
    load_relevant_skill_contents,
    skill_id_from_path,
)
from reqahe.refiner.validation import validate_fix_plan, validate_proposed_edits


"""Two-stage harness refiner: fix plan then generate edits with Python validation."""

PROMPT_DIR = Path(__file__).with_name("prompts")
DEFAULT_REFINER_CONFIG: dict[str, Any] = {
    "json_attempts": 3,
    "transport_attempts": 4,
    "max_repair_attempts": 1,
    "edit_generation_max_tokens": 10000,
    "compact_retry_on_empty": True,
    "compact_retry_max_chars": 18000,
    "save_llm_payloads": True,
}
MAX_REPAIR_ATTEMPTS = 1
TARGET_FILE_CONTEXT_MAX_CHARS = 6000
MAX_VALIDATOR_ERRORS_IN_PAYLOAD = 10
MAX_SIMILAR_SKILL_CANDIDATES = 3
_PRIORITY_ALIASES = {"high": 70, "medium": 50, "low": 30}
_DEFAULT_EXPECTED_EFFECT_METRICS = ["mean_IRE", "mean_TKQR", "main_score"]

SKILL_MARKDOWN_EXAMPLE = """---
id: focused-follow-up
name: Focused Follow-up
version: 1
enabled: true
intent: Ask a focused follow-up when the current dialogue leaves an observable requirement detail underspecified.
scope:
  - Use when the previous answer or the current context leaves a concrete requirement detail unclear.
use_when:
  - The latest dialogue context contains an unresolved detail that can be clarified with one focused question.
avoid_when:
  - The same detail has already been clarified.
  - The next question would repeat a previous question without adding a narrower focus.
risk_notes:
  - Overuse may make the interview repetitive or slow down exploration of other requirement details.
---

# Skill

Ask one concise follow-up question that narrows an observable uncertainty in the current dialogue. Keep the question grounded in what the user has already said, and avoid introducing assumptions not supported by the conversation.
"""

REFLECTION_PYTHON_EXAMPLE = '''"""
component: self_reflection
reflection_id: single_question_check
name: Single Question Check
version: 1.0.0
hook: question_candidate
mode: warn
"""

from typing import Any

def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    return warnings
'''

REFLECTION_PROMPT_EXAMPLE = """You generated a candidate output that triggered a runtime warning.

Revise the candidate in this same turn:
- Ask exactly one focused question.
- Do not mention hidden evaluation requirements.
"""

ARTIFACT_SCHEMAS: dict[str, Any] = {
    "system_prompt_section_v1": {
        "path_pattern": "system_prompt.md",
        "allowed_top_level_sections": [
            "Role",
            "Goal",
            "Interaction Rules",
            "Output Format",
            "Safety Boundaries",
        ],
        "rules": [
            "Only these five top-level # headings are allowed.",
            "Do not add # Scope and Boundaries, # Strategy, # Notes, or any other top-level heading.",
            "Merge scope, boundary, strategy, or closing-rule content into Interaction Rules, Goal, or Safety Boundaries.",
            "For replace, old must match the full file or an exact existing section; new_content must still use only the five allowed headings.",
            "Do not append new top-level headings to system_prompt.md.",
        ],
    },
    "skill_markdown_v1": {
        "path_pattern": "skills/<skill-id>/SKILL.md",
        "required_frontmatter": {
            "id": "must equal directory name under skills/",
            "name": "string",
            "version": "integer",
            "enabled": "boolean",
            "intent": "string",
            "scope": "list[str]",
            "use_when": "list[str]",
            "avoid_when": "list[str]",
            "risk_notes": "list[str]",
        },
        "required_body": "non-empty markdown after frontmatter",
    },
    "reflection_check_bundle_v1": {
        "path_pattern": "self_reflection/<reflection-id>/check.py + self_reflection/<reflection-id>/PROMPT.md",
        "required_module_docstring_metadata": {
            "component": "self_reflection",
            "reflection_id": "must equal bundle folder name",
            "name": "string",
            "version": "string, e.g. 1.0.0",
            "hook": "question_candidate | finish_candidate",
            "mode": "observe | warn | enforce",
        },
        "required_function": "def check(candidate: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]",
        "required_prompt_file": "self_reflection/<reflection-id>/PROMPT.md",
    },
}

DEFAULT_PATH_PATTERNS: dict[str, str] = {
    "system_prompt": "system_prompt.md",
    "skills": "skills/<skill-id>/SKILL.md",
    "self_reflection": "self_reflection/<reflection-id>/check.py + self_reflection/<reflection-id>/PROMPT.md",
}
INTERNAL_ROUTER_TARGETS = {"skill_router", "memory_router"}


def refine_harness(
    iteration_dir: str | Path,
    workspace_dir: str | Path,
    iteration: int,
    llm: OpenAICompatibleClient,
    refiner_model: str,
    refiner_config: dict[str, Any] | None = None,
) -> Path:
    iteration_path = Path(iteration_dir)
    workspace = Path(workspace_dir).resolve()
    refiner_dir = ensure_dir(iteration_path / "refiner")
    cfg = {**DEFAULT_REFINER_CONFIG, **(refiner_config or {})}
    max_repair_attempts = int(cfg.get("max_repair_attempts") or MAX_REPAIR_ATTEMPTS)
    declared_list = load_declared_components(workspace)
    declared_components = {item["name"] for item in declared_list}
    write_policy = build_write_policy(workspace, declared_list)
    component_localization_path = iteration_path / "analysis" / "component_localization.json"
    if not component_localization_path.exists():
        raise RuntimeError("missing analysis/component_localization.json; diagnoser must run before refiner")
    component_localization = read_json(component_localization_path)

    fix_plan_payload = build_fix_plan_payload(iteration_path, workspace, component_localization, write_policy)
    fix_plan: dict[str, Any] = {}
    refinement: dict[str, Any] = {}
    raw_refinement: dict[str, Any] = {}
    validation_report: dict[str, Any] = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "structured_errors": [],
        "checked_files": [],
    }
    repair_attempted = False
    written_files: list[str] = []
    refiner_stats: dict[str, Any] = {}
    last_stage = "init"
    refiner_call_stats = _initial_refiner_call_stats()

    try:
        last_stage = "fix_plan_running"
        _write_refiner_stage(refiner_dir, "fix_plan", "running", "calling LLM for fix plan")
        _save_llm_payload(refiner_dir, "fix_plan_payload.json", fix_plan_payload, cfg)
        refiner_call_stats["fix_plan_payload_chars"] = _json_chars(fix_plan_payload)
        refiner_call_stats["fix_plan_attempts"] += 1
        fix_plan = call_llm_for_fix_plan(
            llm,
            refiner_model,
            load_refiner_prompt("make_fix_plan.md"),
            fix_plan_payload,
            refiner_config=cfg,
        )
        fix_plan = _normalize_fix_plan_from_llm(fix_plan)
        fix_plan = _normalize_fix_plan_target_hints(fix_plan)
        fix_plan = _sanitize_fix_plan_or_drop_invalid(fix_plan, declared_components)
        validate_fix_plan(fix_plan, declared_components)
        write_json(refiner_dir / "fix_plan.json", fix_plan)
        _write_refiner_stage(
            refiner_dir,
            "fix_plan",
            "done",
            "fix plan validated",
            {"dropped_invalid_fixes": fix_plan.get("dropped_invalid_fixes") or []},
        )

        last_stage = "generate_edits_running"
        _write_refiner_stage(refiner_dir, "generate_edits", "running", "calling LLM for file edits")
        edit_payload = build_edit_payload(workspace, fix_plan, write_policy, validator_errors=[])
        _save_llm_payload(refiner_dir, "edit_payload.full.json", edit_payload, cfg)
        refiner_call_stats["edit_payload_full_chars"] = _json_chars(edit_payload)
        refinement = _call_llm_for_edits_with_optional_compact(
            llm,
            refiner_model,
            load_refiner_prompt("generate_edits_and_validate.md"),
            workspace,
            fix_plan,
            write_policy,
            full_payload=edit_payload,
            refiner_config=cfg,
            refiner_dir=refiner_dir,
            call_stats=refiner_call_stats,
        )
        refinement = _normalize_edits_from_llm(workspace, refinement, fix_plan)
        raw_refinement = deepcopy(refinement)
        write_json(refiner_dir / "proposed_edits.json", refinement)
        _write_refiner_stage(refiner_dir, "generate_edits", "done", "proposed edits received")

        last_stage = "validate_running"
        _write_refiner_stage(refiner_dir, "validate", "running", "validating proposed edits")
        planned_edits, refinement, validation_report, repair_attempted, refiner_stats = _validate_with_optional_repairs(
            llm,
            refiner_model,
            workspace,
            refiner_dir,
            fix_plan,
            refinement,
            raw_refinement,
            write_policy,
            declared_components,
            iteration,
            refiner_config=cfg,
            max_repair_attempts=max_repair_attempts,
            call_stats=refiner_call_stats,
        )
        _write_refiner_stage(refiner_dir, "validate", "done", "validation passed")

        last_stage = "apply_running"
        _write_refiner_stage(refiner_dir, "apply", "running", "applying file edits")
        write_json(refiner_dir / "validation_report.json", validation_report)
        written_files = apply_file_edits(workspace, planned_edits)
        refiner_stats = build_refiner_stats(
            raw_refinement or refinement,
            refinement,
            validation_report,
            repair_attempted,
            written_files,
            workspace=workspace,
            iteration=iteration,
        )
        refiner_stats["ok"] = True
        refiner_stats["stage"] = "apply"
        _add_repeat_update_warning(refiner_stats, refiner_dir)
        write_json(refiner_dir / "refiner_stats.json", refiner_stats)
        try:
            validate_workspace_after_write(workspace)
        except Exception as exc:
            _write_failure_artifacts(
                iteration_path,
                refiner_dir,
                exc,
                last_stage,
                fix_plan=fix_plan,
                refinement=refinement,
                validation_report=validation_report,
                repair_attempted=repair_attempted,
                refiner_stats=refiner_stats,
                declared_components=declared_components,
            )
            raise
        _write_refiner_stage(refiner_dir, "apply", "done", "file edits applied")
        _write_refiner_stage(refiner_dir, "done", "done", "refiner completed")
        refiner_call_stats["final_stage"] = "done"
        refiner_call_stats["final_status"] = "ok"
        refiner_call_stats["last_error"] = None
        _write_refiner_call_stats(refiner_dir, refiner_call_stats)
    except KeyboardInterrupt:
        _write_refiner_stage(refiner_dir, last_stage, "interrupted", "manual interrupt")
        refiner_call_stats["final_stage"] = _public_refiner_stage(last_stage)
        refiner_call_stats["final_status"] = "failed"
        refiner_call_stats["last_error"] = "manual interrupt"
        _write_refiner_call_stats(refiner_dir, refiner_call_stats)
        _write_failure_artifacts(
            iteration_path,
            refiner_dir,
            KeyboardInterrupt("manual interrupt"),
            last_stage,
            fix_plan=fix_plan,
            refinement=refinement,
            validation_report=validation_report,
            repair_attempted=repair_attempted,
            refiner_stats=refiner_stats,
            declared_components=declared_components,
        )
        raise
    except (RuntimeError, ValueError, TypeError, OSError, PermissionError) as exc:
        _write_refiner_stage(refiner_dir, last_stage, "failed", str(exc))
        refiner_call_stats["final_stage"] = _public_refiner_stage(last_stage)
        refiner_call_stats["final_status"] = "failed"
        refiner_call_stats["last_error"] = str(exc)
        _write_refiner_call_stats(refiner_dir, refiner_call_stats)
        _write_failure_artifacts(
            iteration_path,
            refiner_dir,
            exc,
            last_stage,
            fix_plan=fix_plan,
            refinement=refinement,
            validation_report=validation_report,
            repair_attempted=repair_attempted,
            refiner_stats=refiner_stats,
            declared_components=declared_components,
        )
        raise

    write_text(iteration_path / "refiner.log", "LLM harness refiner completed.\n")
    write_text(
        iteration_path / "refiner_rationale.md",
        str(refinement.get("refiner_rationale") or "").strip() + "\n",
    )
    _commit_workspace(workspace, f"iteration {iteration}: refine requirements elicitation harness")
    return workspace


def build_fix_plan_payload(
    iteration_path: Path,
    workspace: Path,
    component_localization: dict[str, Any],
    write_policy: dict[str, Any],
) -> dict[str, Any]:
    rollout = iteration_path / "rollout_before"
    existing_skill_catalog = build_existing_skill_catalog(workspace)
    payload = {
        "component_localization": component_localization,
        "artifact_index": build_artifact_index(workspace),
        "existing_skill_catalog": existing_skill_catalog,
        "skill_catalog_summary": load_skill_catalog_summary(workspace),
        "write_policy": write_policy,
        "prior_verdict_summary": _prior_verdict_summary(iteration_path, workspace),
    }
    schema_errors = load_skill_schema_errors_summary(workspace)
    if schema_errors:
        payload["skill_schema_errors_summary"] = schema_errors
    if rollout.is_dir():
        payload.update(load_route_stats_artifacts(rollout))
    return payload


def build_edit_payload(
    workspace: Path,
    approved_fix_plan: dict[str, Any],
    write_policy: dict[str, Any],
    *,
    validator_errors: list[str],
    validator_structured_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_files = collect_replace_or_append_targets(approved_fix_plan)
    existing_skill_catalog = _compact_skill_catalog(build_existing_skill_catalog(workspace))
    similar_skill_candidates = collect_similar_skill_candidates(
        approved_fix_plan,
        existing_skill_catalog,
        top_k=MAX_SIMILAR_SKILL_CANDIDATES,
    )
    priority_skill_ids = {
        skill_id_from_path(hint)
        for hint in target_files
        if is_skill_markdown_path(hint)
    }
    for fix in approved_fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        hint = str(fix.get("target_file_hint") or "")
        if is_skill_markdown_path(hint):
            priority_skill_ids.add(skill_id_from_path(hint))
    existing_skill_contents: list[dict[str, Any]] = []
    loaded_paths: set[str] = set()
    for relative in target_files:
        if is_skill_markdown_path(relative) and (workspace / relative).is_file():
            existing_skill_contents.append(
                {
                    "path": relative,
                    "skill_id": skill_id_from_path(relative),
                    "content": read_text(workspace / relative),
                }
            )
            loaded_paths.add(relative)
    if similar_skill_candidates:
        closest = similar_skill_candidates[0]
        closest_path = str(closest.get("path") or closest.get("relative_path") or "")
        if closest_path and closest_path not in loaded_paths and (workspace / closest_path).is_file():
            existing_skill_contents.append(
                {
                    "path": closest_path,
                    "skill_id": skill_id_from_path(closest_path),
                    "content": read_text(workspace / closest_path),
                }
            )
    limited_errors = list(validator_errors[:MAX_VALIDATOR_ERRORS_IN_PAYLOAD])
    payload = {
        "approved_fix_plan": approved_fix_plan,
        "target_file_context": read_only_target_files(workspace, target_files, max_chars=TARGET_FILE_CONTEXT_MAX_CHARS),
        "selected_schemas": select_schemas_for_fix_plan(approved_fix_plan),
        "write_policy": write_policy,
        "validator_errors": limited_errors,
        "validator_structured_errors": list(validator_structured_errors or []),
        "artifact_index": build_artifact_index(workspace),
        "existing_skill_catalog": existing_skill_catalog,
        "existing_skill_contents": existing_skill_contents,
        "similar_skill_candidates": similar_skill_candidates,
        "skill_catalog_summary": load_skill_catalog_summary(workspace),
    }
    schema_errors = load_skill_schema_errors_summary(workspace)
    if schema_errors:
        payload["skill_schema_errors_summary"] = schema_errors
    skill_digest = _load_skill_evolution_digest_for_workspace(workspace)
    if skill_digest:
        payload["skill_evolution_digest"] = skill_digest
    return payload


def build_compact_edit_payload(
    workspace: Path,
    fix_plan: dict[str, Any],
    write_policy: dict[str, Any],
    validator_errors: list[str] | None = None,
    validator_structured_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_files = collect_replace_or_append_targets(fix_plan)
    compact_target_context = read_only_target_files(
        workspace,
        target_files,
        max_chars=TARGET_FILE_CONTEXT_MAX_CHARS,
    )
    target_metadata: dict[str, Any] = {}
    existing_catalog = _compact_skill_catalog(build_existing_skill_catalog(workspace))
    target_skill_ids = {skill_id_from_path(path) for path in target_files if is_skill_markdown_path(path)}
    if target_skill_ids:
        target_metadata["skills"] = [
            item
            for item in existing_catalog
            if str(item.get("skill_id") or "") in target_skill_ids
            or str(item.get("path") or "") in target_files
        ]
    payload: dict[str, Any] = {
        "approved_fix_plan": fix_plan,
        "write_policy": write_policy,
        "target_file_context": compact_target_context,
        "target_component_metadata": target_metadata,
        "selected_schemas": select_schemas_for_fix_plan(fix_plan),
        "validator_errors": list((validator_errors or [])[:MAX_VALIDATOR_ERRORS_IN_PAYLOAD]),
        "validator_structured_errors": list(validator_structured_errors or []),
    }
    route_summary = _load_route_stats_summary_for_workspace(workspace)
    if route_summary:
        payload["route_stats_summary"] = route_summary
    skill_digest = _load_skill_evolution_digest_for_workspace(workspace)
    if skill_digest:
        payload["skill_evolution_digest"] = _compact_skill_evolution_digest(skill_digest, target_skill_ids)
    schema_errors = load_skill_schema_errors_summary(workspace)
    if schema_errors:
        payload["skill_schema_errors_summary"] = schema_errors
    return payload


def should_compact_retry(exc: Exception, cfg: Mapping[str, Any]) -> bool:
    if not bool(cfg.get("compact_retry_on_empty", True)):
        return False
    message = str(exc).lower()
    retry_markers = (
        "empty model response",
        "empty response",
        "content is empty",
        "finish_reason=stop",
        "request timed out",
        "timeout",
        "timed out",
        "json decode failed",
        "invalid json",
        "schema validation failed",
        "validation failed after normal repair",
    )
    return any(marker in message for marker in retry_markers)


def build_artifact_index(workspace_dir: Path) -> dict[str, list[str]]:
    workspace = Path(workspace_dir)
    index: dict[str, list[str]] = {}
    for path in sorted(workspace_allowed_files(workspace)):
        relative = path.relative_to(workspace).as_posix()
        if relative == "code_agent.yaml":
            continue
        component = path_component(workspace, relative)
        if not component:
            continue
        index.setdefault(component, []).append(relative)
    return index


def build_write_policy(workspace_dir: Path, declared_components: list[dict[str, str]]) -> dict[str, Any]:
    allowed = [item["name"] for item in declared_components if item["name"] not in NON_EVOLVABLE_COMPONENTS]
    path_patterns = {name: DEFAULT_PATH_PATTERNS.get(name, f"{name}/<artifact>") for name in allowed}
    return {
        "max_fixes": 3,
        "allowed_components": allowed,
        "path_patterns": path_patterns,
        "allow_registry_edit": True,
        "forbid_memory_edits": True,
    }


def collect_replace_or_append_targets(fix_plan: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    replace_ops = {"replace", "append", "update", "demote", "disable", "validate", "remove"}
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        operation = str(fix.get("operation_intent") or "")
        if operation not in replace_ops:
            continue
        hint = _normalize_single_target_hint(str(fix.get("target_file_hint") or "").strip())
        if hint:
            targets.append(hint)
    return sorted(set(targets))


def read_only_target_files(
    workspace_dir: Path,
    target_files: list[str],
    *,
    max_chars: int | None = None,
) -> dict[str, str]:
    workspace = Path(workspace_dir)
    context: dict[str, str] = {}
    for relative in target_files:
        path = workspace / relative
        if path.exists() and path.is_file():
            content = read_text(path)
            if max_chars is not None and len(content) > max_chars:
                content = content[:max_chars] + "\n...[truncated]..."
            context[relative] = content
    return context


def select_schemas_for_fix_plan(fix_plan: dict[str, Any]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        artifact_type = str(fix.get("artifact_type") or "")
        if artifact_type in ARTIFACT_SCHEMAS and artifact_type not in selected:
            schema = deepcopy(ARTIFACT_SCHEMAS[artifact_type])
            if artifact_type == "skill_markdown_v1":
                schema["example"] = SKILL_MARKDOWN_EXAMPLE
            elif artifact_type == "reflection_check_bundle_v1":
                schema["example_check_py"] = REFLECTION_PYTHON_EXAMPLE
                schema["example_prompt_md"] = REFLECTION_PROMPT_EXAMPLE
            selected[artifact_type] = schema
    return selected


def apply_file_edits(workspace_dir: Path, planned_edits: list[tuple[str, str | None]]) -> list[str]:
    workspace = Path(workspace_dir)
    written_files: list[str] = []
    for relative_path, content in planned_edits:
        if content is None:
            target = workspace / relative_path
            if target.exists():
                target.unlink()
                written_files.append(relative_path)
            continue
        write_workspace_file(workspace, relative_path, content)
        written_files.append(relative_path)
    return written_files


def load_refiner_prompt(prompt_name: str) -> str:
    path = PROMPT_DIR / prompt_name
    if path.parent != PROMPT_DIR or path.suffix.lower() != ".md":
        raise RuntimeError(f"invalid refiner prompt name: {prompt_name}")
    if not path.exists():
        raise RuntimeError(f"refiner prompt not found: {prompt_name}")
    return path.read_text(encoding="utf-8")


def call_llm_for_fix_plan(
    llm: OpenAICompatibleClient,
    model: str,
    system_prompt: str,
    payload: dict[str, Any],
    *,
    refiner_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**DEFAULT_REFINER_CONFIG, **(refiner_config or {})}
    return llm.json_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=model,
        purpose="harness fix plan selection",
        max_attempts=int(cfg.get("json_attempts") or 2),
        transport_max_attempts=int(cfg.get("transport_attempts") or 2),
    )


def call_llm_for_edits(
    llm: OpenAICompatibleClient,
    model: str,
    system_prompt: str,
    payload: dict[str, Any],
    *,
    refiner_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**DEFAULT_REFINER_CONFIG, **(refiner_config or {})}
    return llm.json_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=model,
        purpose="harness file edit generation",
        max_attempts=int(cfg.get("json_attempts") or 2),
        transport_max_attempts=int(cfg.get("transport_attempts") or 2),
        max_tokens=int(cfg.get("edit_generation_max_tokens") or DEFAULT_REFINER_CONFIG["edit_generation_max_tokens"]),
    )


def _call_llm_for_edits_with_optional_compact(
    llm: OpenAICompatibleClient,
    model: str,
    system_prompt: str,
    workspace: Path,
    fix_plan: dict[str, Any],
    write_policy: dict[str, Any],
    *,
    full_payload: dict[str, Any],
    refiner_config: dict[str, Any],
    refiner_dir: Path,
    call_stats: dict[str, Any],
    validator_errors: list[str] | None = None,
    validator_structured_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    call_stats["edit_generation_attempts"] += 1
    try:
        return call_llm_for_edits(
            llm,
            model,
            system_prompt,
            full_payload,
            refiner_config=refiner_config,
        )
    except Exception as exc:
        if not should_compact_retry(exc, refiner_config):
            raise
        compact_payload = build_compact_edit_payload(
            workspace,
            fix_plan,
            write_policy,
            validator_errors=validator_errors,
            validator_structured_errors=validator_structured_errors,
        )
        compact_payload = _limit_compact_payload(compact_payload, int(refiner_config.get("compact_retry_max_chars") or 18000))
        _save_llm_payload(refiner_dir, "edit_payload.compact.json", compact_payload, refiner_config)
        call_stats["edit_payload_compact_chars"] = _json_chars(compact_payload)
        call_stats["used_compact_retry"] = True
        call_stats["edit_generation_attempts"] += 1
        return call_llm_for_edits(
            llm,
            model,
            system_prompt,
            compact_payload,
            refiner_config=refiner_config,
        )


def validate_and_plan_refinement(
    workspace: Path,
    refinement: dict[str, Any],
    allowed_components: set[str],
) -> list[tuple[str, str]]:
    _reject_registry_file_edits(refinement)
    from reqahe.refiner.validation import validate_paths, validate_refinement_schema

    validate_refinement_schema(refinement, allowed_components)
    validate_paths(refinement, workspace, allowed_components)
    planned_edits = [_plan_edit(workspace, edit) for edit in refinement["file_edits"]]
    planned_edits = _normalize_planned_edits(planned_edits)
    planned_edits = _sync_reflection_registry_entries(workspace, planned_edits)
    validate_schema_compliance_block(refinement, [path for path, _ in planned_edits])
    validate_component_schema(refinement, planned_edits)
    validate_modified_workspace_preview(workspace, planned_edits)
    return planned_edits


def validate_component_schema(data: dict[str, Any], planned_edits: list[tuple[str, str]]) -> None:
    validate_schema_compliance_block(data, [path for path, _ in planned_edits])


def validate_modified_workspace_preview(workspace: Path, planned_edits: list[tuple[str, str]]) -> None:
    staged = {relative_path: content for relative_path, content in planned_edits}
    try:
        validate_workspace_component_schemas(workspace, staged_files=staged)
    except RuntimeError as exc:
        raise RuntimeError("harness workspace preview validation failed: " + str(exc)) from exc


def validate_workspace_after_write(workspace: Path) -> None:
    validate_workspace_component_schemas(workspace)


def _prior_verdict_summary(iteration_path: Path, workspace: Path) -> dict[str, Any]:
    attribution = _read_optional_json(iteration_path / "attribution" / "metric_deltas.json", {})
    summary: dict[str, Any] = {}
    if attribution:
        summary["attribution_metric_deltas"] = attribution
    return summary


def _validate_with_optional_repairs(
    llm: OpenAICompatibleClient,
    refiner_model: str,
    workspace: Path,
    refiner_dir: Path,
    fix_plan: dict[str, Any],
    refinement: dict[str, Any],
    raw_refinement: dict[str, Any],
    write_policy: dict[str, Any],
    allowed_components: set[str],
    iteration: int,
    *,
    refiner_config: dict[str, Any] | None = None,
    max_repair_attempts: int | None = None,
    call_stats: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, str]], dict[str, Any], dict[str, Any], bool, dict[str, Any]]:
    repair_attempted = False
    last_exc: RuntimeError | None = None
    selected_schemas = select_schemas_for_fix_plan(fix_plan)
    validation_report: dict[str, Any] = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "structured_errors": [],
        "checked_files": [],
    }
    latest_raw = raw_refinement or refinement
    repair_limit = max_repair_attempts if max_repair_attempts is not None else MAX_REPAIR_ATTEMPTS
    cfg = {**DEFAULT_REFINER_CONFIG, **(refiner_config or {})}

    for attempt in range(repair_limit + 1):
        normalized_refinement = _normalize_refinement_file_contents(refinement, iteration=iteration)
        write_json(refiner_dir / "proposed_edits.normalized.json", normalized_refinement)
        validation_report = validate_proposed_edits(
            workspace,
            normalized_refinement,
            fix_plan,
            write_policy,
            selected_schemas,
            allowed_components,
            raw_refinement=latest_raw,
        )
        if validation_report["ok"]:
            try:
                planned = validate_and_plan_refinement(workspace, normalized_refinement, allowed_components)
                stats = build_refiner_stats(
                    latest_raw,
                    normalized_refinement,
                    validation_report,
                    repair_attempted,
                    [],
                    workspace=workspace,
                    iteration=iteration,
                )
                _add_repeat_update_warning(stats, refiner_dir)
                _write_skill_similarity_audit(
                    refiner_dir,
                    normalized_refinement,
                    validation_report,
                    iteration=iteration,
                )
                return planned, normalized_refinement, validation_report, repair_attempted, stats
            except RuntimeError as exc:
                validation_report = {
                    "ok": False,
                    "errors": [str(exc)],
                    "warnings": [],
                    "structured_errors": validation_report.get("structured_errors") or [],
                    "checked_files": validation_report.get("checked_files", []),
                }
        if attempt >= repair_limit:
            dropped_refinement, drop_warnings = _drop_offending_high_similarity_skill_creates(
                normalized_refinement,
                validation_report,
            )
            if drop_warnings:
                validation_report = validate_proposed_edits(
                    workspace,
                    dropped_refinement,
                    fix_plan,
                    write_policy,
                    selected_schemas,
                    allowed_components,
                    raw_refinement=latest_raw,
                )
                validation_report.setdefault("warnings", [])
                validation_report["warnings"].extend(drop_warnings)
                if validation_report["ok"]:
                    try:
                        planned = validate_and_plan_refinement(workspace, dropped_refinement, allowed_components)
                        stats = build_refiner_stats(
                            latest_raw,
                            dropped_refinement,
                            validation_report,
                            repair_attempted,
                            [],
                            workspace=workspace,
                            iteration=iteration,
                        )
                        _add_repeat_update_warning(stats, refiner_dir)
                        _write_skill_similarity_audit(
                            refiner_dir,
                            dropped_refinement,
                            validation_report,
                            iteration=iteration,
                        )
                        return planned, dropped_refinement, validation_report, repair_attempted, stats
                    except RuntimeError as exc:
                        validation_report = {
                            "ok": False,
                            "errors": [str(exc)],
                            "warnings": validation_report.get("warnings") or [],
                            "structured_errors": validation_report.get("structured_errors") or [],
                            "checked_files": validation_report.get("checked_files", []),
                        }
                elif not (dropped_refinement.get("file_edits") or []):
                    validation_report.setdefault("errors", [])
                    validation_report["errors"].append(
                        "All proposed edits were dropped due to high-similarity skill creation."
                    )
            last_exc = RuntimeError(
                "harness refinement validation failed: " + "; ".join(validation_report.get("errors") or ["unknown"])
            )
            stats = build_refiner_stats(
                latest_raw,
                normalized_refinement,
                validation_report,
                repair_attempted,
                [],
                workspace=workspace,
                iteration=iteration,
            )
            stats["ok"] = False
            stats["stage"] = "validate"
            stats["error_type"] = "ValidationError"
            stats["message"] = str(last_exc)
            _add_repeat_update_warning(stats, refiner_dir)
            _write_skill_similarity_audit(
                refiner_dir,
                normalized_refinement,
                validation_report,
                iteration=iteration,
            )
            write_json(refiner_dir / "validation_report.json", validation_report)
            write_json(refiner_dir / "refiner_stats.json", stats)
            raise last_exc from None
        repair_attempted = True
        _write_refiner_stage(refiner_dir, "repair", "running", f"repair attempt {attempt + 1}")
        edit_payload = build_edit_payload(
            workspace,
            fix_plan,
            write_policy,
            validator_errors=list(validation_report.get("errors") or []),
            validator_structured_errors=list(validation_report.get("structured_errors") or []),
        )
        _save_llm_payload(refiner_dir, "edit_payload.full.json", edit_payload, cfg)
        if call_stats is not None:
            call_stats["edit_payload_full_chars"] = max(
                int(call_stats.get("edit_payload_full_chars") or 0),
                _json_chars(edit_payload),
            )
        refinement = _call_llm_for_edits_with_optional_compact(
            llm,
            refiner_model,
            load_refiner_prompt("generate_edits_and_validate.md"),
            workspace,
            fix_plan,
            write_policy,
            full_payload=edit_payload,
            refiner_config=cfg,
            refiner_dir=refiner_dir,
            call_stats=call_stats if call_stats is not None else _initial_refiner_call_stats(),
            validator_errors=list(validation_report.get("errors") or []),
            validator_structured_errors=list(validation_report.get("structured_errors") or []),
        )
        refinement = _normalize_edits_from_llm(workspace, refinement, fix_plan)
        write_json(refiner_dir / "proposed_edits.json", refinement)
        _write_refiner_stage(refiner_dir, "repair", "done", f"repair attempt {attempt + 1} complete")
    raise last_exc or RuntimeError("harness refinement validation failed")


def _validate_component(component: str, allowed_components: set[str]) -> None:
    normalized = component.strip()
    if normalized not in allowed_components:
        raise RuntimeError(f"component is not declared by current harness seed: {normalized}")


def _validate_fix_target_hint(component: str, artifact_type: str, operation_intent: str, target_hint: str) -> None:
    if not target_hint:
        raise RuntimeError("harness fix plan selection failed: target_file_hint is required")
    parts = _relative_path_parts(target_hint)
    name = parts[-1] if parts else ""
    if operation_intent == "append" and name == "README.md":
        raise RuntimeError("harness fix plan selection failed: do not append schema artifacts to README.md")
    if artifact_type == "skill_markdown_v1":
        if name != "SKILL.md" or len(parts) != 3 or parts[0] != "skills":
            raise RuntimeError(
                "harness fix plan selection failed: skills must target skills/<skill-id>/SKILL.md"
            )
        if operation_intent == "append":
            raise RuntimeError(
                "harness fix plan selection failed: skills must use create or replace, not append"
            )
        if operation_intent not in {"create", "replace", "update", "demote", "disable", "validate", "remove"}:
            raise RuntimeError(
                "harness fix plan selection failed: skills must use a supported operation_intent"
            )
    elif artifact_type == "reflection_check_bundle_v1":
        if len(parts) != 3 or parts[0] != "self_reflection":
            raise RuntimeError(
                "harness fix plan selection failed: self_reflection artifacts must target "
                "self_reflection/<reflection-id>/check.py"
            )
        if name == "PROMPT.md":
            raise RuntimeError(
                "reflection_check_bundle_v1 target_file_hint must use the primary check.py path: "
                f"self_reflection/{parts[1]}/check.py"
            )
        if name != "check.py":
            raise RuntimeError(
                "harness fix plan selection failed: self_reflection artifacts must target "
                "self_reflection/<reflection-id>/check.py"
            )


def _validate_file_edit_target(relative_path: str, operation: str, component: str | None) -> None:
    parts = _relative_path_parts(relative_path)
    name = parts[-1] if parts else ""
    if operation == "append" and name == "README.md":
        raise RuntimeError(f"harness refinement generation failed: do not append schema artifacts to {relative_path}")
    if relative_path.startswith("memory/"):
        raise RuntimeError(
            "Refiner is not allowed to edit memory. Memory is written only by memorizer and is never rolled back."
        )
    if component == "skills":
        if name == "README.md":
            raise RuntimeError(f"harness refinement generation failed: skills must not target {relative_path}")
        if operation == "create" and (name != "SKILL.md" or len(parts) != 3 or parts[0] != "skills"):
            raise RuntimeError(
                f"harness refinement generation failed: skills must be written as skills/<skill-name>/SKILL.md, not {relative_path}"
            )
    elif component == "self_reflection" and name == "README.md":
        raise RuntimeError(f"harness refinement generation failed: self_reflection artifacts must not target {relative_path}")
    elif component == "self_reflection" and relative_path == "self_reflection/registry.yaml":
        return
    elif component == "self_reflection" and not (
        (name == "check.py" and len(parts) == 3 and parts[0] == "self_reflection")
        or (name == "PROMPT.md" and len(parts) == 3 and parts[0] == "self_reflection")
    ):
        raise RuntimeError(
            f"harness refinement generation failed: self_reflection only supports bundle check.py and PROMPT.md files: {relative_path}"
        )


def _validate_schema_artifact_paths(component: str, schema_name: str, files: Any) -> None:
    if not isinstance(files, list):
        return
    for raw_path in files:
        parts = _relative_path_parts(str(raw_path))
        name = parts[-1] if parts else ""
        if component in {"skills", "memory", "self_reflection"} and name == "README.md":
            raise RuntimeError("harness refinement generation failed: schema artifacts must not target README.md")
        if schema_name == "skill_markdown_v1" and component == "skills":
            if name != "SKILL.md" or len(parts) != 3 or parts[0] != "skills":
                raise RuntimeError("harness refinement generation failed: skills must be written as skills/<skill-name>/SKILL.md")


def _artifact_type_matches_component(component: str, artifact_type: str) -> bool:
    if component == "memory":
        return False
    expected = {
        "system_prompt": {"system_prompt_section_v1"},
        "skills": {"skill_markdown_v1"},
        "self_reflection": {"reflection_check_bundle_v1"},
    }
    return artifact_type in expected.get(component, ALLOWED_ARTIFACT_TYPES)


def _plan_edit(workspace: Path, edit: dict[str, Any]) -> tuple[str, str | None]:
    relative_path = str(edit["relative_path"])
    target = workspace / relative_path
    operation = edit["operation"]
    if operation == "delete":
        return relative_path, None
    new_content = _edit_new_content(edit)
    if operation == "replace" and not target.exists() and not str(edit.get("old") or "").strip():
        operation = "create"
    if operation == "create":
        if target.exists():
            raise RuntimeError(f"harness refinement generation failed: create target already exists {relative_path}")
        content = (new_content or "").rstrip() + "\n"
    elif operation == "append":
        if not target.exists():
            raise RuntimeError(f"harness refinement generation failed: append target does not exist {relative_path}")
        current = read_text(target)
        addition = (new_content or "").rstrip()
        content = current.rstrip() + "\n\n" + addition + "\n" if current.strip() else addition + "\n"
    else:
        if not target.exists():
            raise RuntimeError(f"harness refinement generation failed: replace target does not exist {relative_path}")
        current = read_text(target)
        old = str(edit.get("old") or "")
        if old and old not in current:
            raise RuntimeError(f"harness refinement generation failed: replacement text not found in {relative_path}")
        content = current.replace(old, new_content or "", 1) if old else (new_content or "")
        if not content.endswith("\n"):
            content = content.rstrip() + "\n"
    return relative_path, content


def _edit_new_content(edit: dict[str, Any]) -> str | None:
    if isinstance(edit.get("new_content"), str):
        return str(edit["new_content"])
    if isinstance(edit.get("lines"), list):
        return "\n".join(str(line).rstrip() for line in edit["lines"])
    if isinstance(edit.get("new_lines"), list):
        return "\n".join(str(line).rstrip() for line in edit["new_lines"])
    return None


def _schema_compliance_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    compliance = data.get("schema_compliance")
    if isinstance(compliance, dict):
        return [compliance]
    if isinstance(compliance, list):
        return [item for item in compliance if isinstance(item, dict)]
    return []


def _reject_registry_file_edits(refinement: dict[str, Any]) -> None:
    return


def _strip_registry_file_edits(refinement: dict[str, Any]) -> dict[str, Any]:
    """Remove registry edits after explicit rejection; for internal cleanup only."""
    normalized = deepcopy(refinement)
    file_edits = normalized.get("file_edits")
    if not isinstance(file_edits, list):
        return normalized
    normalized["file_edits"] = [
        edit
        for edit in file_edits
        if isinstance(edit, dict) and str(edit.get("relative_path") or "") != "self_reflection/registry.yaml"
    ]
    compliance = normalized.get("schema_compliance")
    if isinstance(compliance, list):
        normalized["schema_compliance"] = [
            item
            for item in compliance
            if not (isinstance(item, dict) and str(item.get("schema_name") or "") == "reflection_registry_v1")
        ]
    return normalized


def _normalize_planned_edits(planned_edits: list[tuple[str, str]], iteration: int | None = None) -> list[tuple[str, str]]:
    return [
        (relative_path, _normalize_component_content(relative_path, content, iteration=iteration))
        for relative_path, content in planned_edits
    ]


def _normalize_refinement_file_contents(refinement: dict[str, Any], iteration: int | None = None) -> dict[str, Any]:
    normalized = deepcopy(refinement)
    file_edits = normalized.get("file_edits")
    if not isinstance(file_edits, list):
        return normalized
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        content = _edit_new_content(edit)
        if content is None:
            continue
        relative_path = str(edit.get("relative_path") or "")
        edit["new_content"] = _normalize_component_content(relative_path, content, iteration=iteration)
    return normalized


def _normalize_component_content(relative_path: str, content: str, iteration: int | None = None) -> str:
    path = Path(relative_path)
    if path.parent.as_posix().startswith("self_reflection/") and path.name == "check.py" and len(path.parts) == 3:
        return _normalize_reflection_python(relative_path, content)
    if path.suffix.lower() != ".md" or not content.startswith("---\n"):
        return content
    try:
        metadata, body = parse_markdown_frontmatter(content, relative_path)
    except RuntimeError:
        return content
    if relative_path.startswith("skills/") and path.name == "SKILL.md":
        metadata = _normalize_skill_frontmatter(metadata, path.parent.name, iteration=iteration)
    return _render_markdown_frontmatter(metadata, body)


def _normalize_skill_frontmatter(
    metadata: dict[str, Any],
    skill_id: str,
    *,
    iteration: int | None = None,
) -> dict[str, Any]:
    normalized = dict(metadata)
    normalized["id"] = str(normalized.get("id") or skill_id)
    if not str(normalized.get("name") or "").strip():
        normalized["name"] = skill_id.replace("-", " ").replace("_", " ").title()
    if not isinstance(normalized.get("version"), int):
        try:
            normalized["version"] = int(normalized.get("version") or 1)
        except (TypeError, ValueError):
            normalized["version"] = 1
    if not isinstance(normalized.get("enabled"), bool):
        normalized["enabled"] = True
    if not str(normalized.get("intent") or "").strip():
        normalized["intent"] = str(normalized.get("description") or "Improve requirements elicitation.")
    normalized["scope"] = _coerce_to_str_list(normalized.get("scope")) or ["Reusable interviewer questioning behavior."]
    trigger = normalized.get("trigger") if isinstance(normalized.get("trigger"), dict) else {}
    normalized["use_when"] = _coerce_to_str_list(normalized.get("use_when")) or _coerce_to_str_list(trigger.get("applies_when")) or [
        "A reusable questioning strategy may help the next interviewer turn."
    ]
    normalized["avoid_when"] = _coerce_to_str_list(normalized.get("avoid_when")) or _coerce_to_str_list(trigger.get("avoid_when")) or [
        "The dialogue context does not match this skill's intent."
    ]
    normalized["risk_notes"] = _coerce_to_str_list(normalized.get("risk_notes")) or [
        "Overuse may narrow the interview away from other requirement areas."
    ]
    for legacy_key in ("component", "skill_id", "status", "priority", "trigger", "evidence", "expected_effect", "description"):
        normalized.pop(legacy_key, None)
    return normalized


def _normalize_reflection_python(relative_path: str, content: str) -> str:
    path = Path(relative_path)
    try:
        module = ast.parse(content)
    except SyntaxError:
        return content
    docstring = ast.get_docstring(module) or ""
    reflection_id = path.parent.name
    metadata = _parse_reflection_docstring_metadata(docstring)
    hook = metadata.get("hook") or "question_candidate"
    if hook not in REFLECTION_HOOKS:
        hook = "question_candidate"
    mode = metadata.get("mode") or "warn"
    if mode not in REFLECTION_MODES:
        mode = "warn"
    merged = {
        "component": "self_reflection",
        "reflection_id": reflection_id,
        "name": metadata.get("name") or _humanize_identifier(reflection_id),
        "version": metadata.get("version") or "1.0.0",
        "hook": hook,
        "mode": mode,
    }
    doc_lines = ['"""']
    for key in ("component", "reflection_id", "name", "version", "hook", "mode"):
        doc_lines.append(f"{key}: {merged[key]}")
    doc_lines.append('"""')
    body = _strip_module_docstring_body(content, module) if docstring else content
    return "\n".join(doc_lines) + "\n\n" + body.lstrip()


def _strip_module_docstring_body(content: str, module: ast.Module) -> str:
    if not module.body:
        return content
    first = module.body[0]
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return content
    lines = content.splitlines(keepends=True)
    return "".join(lines[first.end_lineno :]).lstrip("\n")


def _parse_reflection_docstring_metadata(docstring: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in docstring.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            metadata[key] = value
    return metadata


def _humanize_identifier(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ")).strip().title()


def build_refiner_stats(
    raw_refinement: dict[str, Any],
    normalized_refinement: dict[str, Any],
    validation_report: dict[str, Any],
    repair_attempted: bool,
    written_files: list[str],
    *,
    workspace: Path | None = None,
    iteration: int | None = None,
) -> dict[str, Any]:
    raw_edits = raw_refinement.get("file_edits") or []
    normalized_edits = normalized_refinement.get("file_edits") or []
    touched_skill_ids = _touched_skill_ids_from_refinement(normalized_refinement)
    operation_intents = _operation_intents_from_refinement(normalized_refinement)
    preview_valid_counts = _count_valid_artifacts_from_preview(normalized_edits)
    existing_skill_catalog = build_existing_skill_catalog(workspace) if workspace is not None else []
    similarity_audit = normalized_refinement.get("similarity_audit")
    if not isinstance(similarity_audit, list):
        similarity_audit = []
    similar_candidates: list[dict[str, Any]] = []
    if workspace is not None:
        similar_candidates = collect_similar_skill_candidates(
            {"fix_plan": []},
            existing_skill_catalog,
            top_k=5,
        )
    return {
        "raw_file_edit_count": len(raw_edits) if isinstance(raw_edits, list) else 0,
        "normalized_file_edit_count": len(normalized_edits) if isinstance(normalized_edits, list) else 0,
        "proposed_skill_count": _count_artifact_edits(raw_edits, "skills"),
        "valid_skill_count": preview_valid_counts["skills"],
        "written_skill_count": _count_written_artifacts(written_files, "skills"),
        "proposed_reflection_count": _count_artifact_edits(raw_edits, "self_reflection"),
        "valid_reflection_count": preview_valid_counts["self_reflection"],
        "written_reflection_count": _count_written_artifacts(written_files, "self_reflection"),
        "proposed_system_prompt_count": _count_artifact_edits(raw_edits, "system_prompt"),
        "valid_system_prompt_count": preview_valid_counts["system_prompt"],
        "written_system_prompt_count": _count_written_artifacts(written_files, "system_prompt"),
        "repair_attempted": repair_attempted,
        "validator_error_count": len(validation_report.get("errors") or []),
        "validator_errors": list(validation_report.get("errors") or []),
        "touched_skill_ids": touched_skill_ids,
        "operation_intents": operation_intents,
        "repeat_update_warning": False,
        "repeat_update_reason": "",
        "existing_skill_count": len(existing_skill_catalog),
        "existing_skill_ids": [str(item.get("skill_id") or "") for item in existing_skill_catalog],
        "similar_skill_candidates": [
            {
                "skill_id": item.get("skill_id"),
                "path": item.get("path") or item.get("relative_path"),
                "similarity_score": item.get("similarity_score"),
            }
            for item in similar_candidates
        ],
        "similarity_audit_summary": [
            {
                "proposed_intent": item.get("proposed_intent"),
                "closest_existing_skill_id": item.get("closest_existing_skill_id"),
                "similarity_score": item.get("similarity_score"),
                "decision": item.get("decision"),
            }
            for item in similarity_audit
            if isinstance(item, dict)
        ],
        "iteration": f"iteration_{int(iteration):03d}" if iteration is not None else None,
    }


def _touched_skill_ids_from_refinement(refinement: dict[str, Any]) -> list[str]:
    touched: set[str] = set()
    for edit in refinement.get("file_edits") or []:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        if is_skill_markdown_path(relative_path):
            touched.add(skill_id_from_path(relative_path))
    return sorted(touched)


def _operation_intents_from_refinement(refinement: dict[str, Any]) -> list[str]:
    intents: set[str] = set()
    for change in refinement.get("changes") or []:
        if not isinstance(change, dict):
            continue
        for key in ("operation_intent", "operation", "intent"):
            value = str(change.get(key) or "").strip()
            if value:
                intents.add(value)
    for edit in refinement.get("file_edits") or []:
        if isinstance(edit, dict) and edit.get("operation"):
            intents.add(str(edit.get("operation")))
    return sorted(intents)


def _count_valid_artifacts_from_preview(file_edits: Any) -> dict[str, int]:
    from reqahe.harness.component_schema import validate_component_file

    counts = {"skills": 0, "self_reflection": 0, "system_prompt": 0}
    if not isinstance(file_edits, list):
        return counts
    preview: dict[str, str] = {}
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        content = _edit_new_content(edit)
        if relative_path and content is not None:
            preview[relative_path] = _normalize_component_content(relative_path, content)
    for relative_path, content in preview.items():
        component = _artifact_component_for_path(relative_path)
        if not component:
            continue
        try:
            errors = validate_component_file(relative_path, content, preview)
        except (RuntimeError, ValueError, TypeError):
            continue
        if not errors:
            counts[component] += 1
    return counts


def _artifact_component_for_path(relative_path: str) -> str | None:
    if relative_path.startswith("skills/") and relative_path.endswith("/SKILL.md"):
        return "skills"
    if relative_path.startswith("self_reflection/") and relative_path.endswith("/check.py"):
        return "self_reflection"
    if relative_path == "system_prompt.md":
        return "system_prompt"
    return None


def _count_artifact_edits(file_edits: Any, component: str) -> int:
    if not isinstance(file_edits, list):
        return 0
    count = 0
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        if component == "skills" and relative_path.startswith("skills/") and relative_path.endswith("/SKILL.md"):
            count += 1
        elif component == "self_reflection" and relative_path.startswith("self_reflection/") and relative_path.endswith("/check.py"):
            count += 1
        elif component == "system_prompt" and relative_path == "system_prompt.md":
            count += 1
    return count


def _count_written_artifacts(written_files: list[str], component: str) -> int:
    count = 0
    for relative_path in written_files:
        if component == "skills" and relative_path.startswith("skills/") and relative_path.endswith("/SKILL.md"):
            count += 1
        elif component == "self_reflection" and relative_path.startswith("self_reflection/") and relative_path.endswith("/check.py"):
            count += 1
        elif component == "system_prompt" and relative_path == "system_prompt.md":
            count += 1
    return count


def _default_source_iterations(iteration: int | None) -> list[str]:
    if iteration is not None:
        return [f"iteration_{int(iteration):03d}"]
    return ["unknown"]


def _coerce_to_str_list(value: Any) -> list[str]:
    raw_items: list[Any]
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
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_to_list(value: Any) -> list[Any]:
    return _coerce_to_str_list(value)


def _registry_entry_from_staged(registry: dict[str, Any], reflection_id: str) -> dict[str, Any] | None:
    for item in registry.get("checks") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == reflection_id:
            return item
    return None


def _sync_reflection_registry_entries(workspace: Path, planned_edits: list[tuple[str, str]]) -> list[tuple[str, str]]:
    staged = {relative_path: content for relative_path, content in planned_edits}
    reflection_files = sorted(
        path
        for path in staged
        if path.startswith("self_reflection/")
        and Path(path).name == "check.py"
        and len(Path(path).parts) == 3
    )
    if not reflection_files:
        return planned_edits

    registry = load_reflection_registry(workspace, staged)
    registry.setdefault("version", "0.2")
    registry.setdefault("checks", [])
    checks = list(registry.get("checks") or [])
    registered_ids = {str(item.get("id") or "") for item in checks if isinstance(item, dict) and item.get("id")}
    registered_files = {str(item.get("file") or "") for item in checks if isinstance(item, dict) and item.get("file")}
    changed = False

    for rel_path in reflection_files:
        bundle_id = Path(rel_path).parts[1]
        prompt_rel = f"{bundle_id}/PROMPT.md"
        prompt_path = f"self_reflection/{prompt_rel}"
        if prompt_path not in staged and not (workspace / prompt_path).exists():
            continue
        file_name = f"{bundle_id}/check.py"
        if file_name in registered_files:
            continue
        reflection_id = bundle_id
        mode = "warn"
        hook = "question_candidate"
        applies_when = "always"
        priority = 20
        docstring = staged[rel_path].split('"""', 2)
        if len(docstring) >= 2:
            for line in docstring[1].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key == "hook" and value:
                    hook = value
                elif key == "mode" and value:
                    mode = value
                elif key == "reflection_id" and value:
                    reflection_id = value
                elif key == "applies_when" and value:
                    applies_when = value
        staged_registry_entry = _registry_entry_from_staged(registry, reflection_id)
        if staged_registry_entry and staged_registry_entry.get("applies_when") not in (None, ""):
            applies_when = staged_registry_entry.get("applies_when")
        if reflection_id in registered_ids:
            continue
        checks.append(
            {
                "id": reflection_id,
                "hook": hook,
                "file": file_name,
                "prompt": prompt_rel,
                "applies_when": applies_when,
                "mode": mode,
                "priority": priority,
            }
        )
        registered_ids.add(reflection_id)
        registered_files.add(file_name)
        changed = True

    if not changed:
        return planned_edits

    registry["checks"] = checks
    registry_content = yaml.safe_dump(registry, sort_keys=False, allow_unicode=True)
    updated = [(path, content) for path, content in planned_edits if path != "self_reflection/registry.yaml"]
    updated.append(("self_reflection/registry.yaml", registry_content))
    return updated


def _coerce_priority(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _PRIORITY_ALIASES:
            return _PRIORITY_ALIASES[normalized]
        if normalized.isdigit():
            return int(normalized)
    return 50


def _render_markdown_frontmatter(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n{body.lstrip()}"


def _write_skill_similarity_audit(
    refiner_dir: Path,
    refinement: dict[str, Any],
    validation_report: dict[str, Any],
    *,
    iteration: int | None = None,
) -> None:
    skill_file_edits: list[dict[str, str]] = []
    for edit in refinement.get("file_edits") or []:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        if is_skill_markdown_path(relative_path) and operation in {"create", "replace"}:
            skill_file_edits.append({"relative_path": relative_path, "operation": operation})
    if not skill_file_edits:
        return
    similarity_audit = refinement.get("similarity_audit")
    if not isinstance(similarity_audit, list):
        similarity_audit = []
    write_json(
        refiner_dir / "skill_similarity_audit.json",
        {
            "iteration": f"iteration_{int(iteration):03d}" if iteration is not None else None,
            "batch": refiner_dir.parent.name if refiner_dir.parent.name.startswith("batch_") else None,
            "skill_file_edits": skill_file_edits,
            "similarity_audit": similarity_audit,
            "validator_warnings": list(validation_report.get("warnings") or []),
        },
    )


def _read_optional_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return read_json(path)
    except Exception:
        return default


def _load_skill_evolution_digest_for_workspace(workspace: Path) -> dict[str, Any]:
    path = workspace.parent / "analysis" / "skill_evolution_digest.json"
    data = _read_optional_json(path, {})
    return data if isinstance(data, dict) else {}


def _load_route_stats_summary_for_workspace(workspace: Path) -> dict[str, Any]:
    path = workspace.parent / "rollout_before" / "route_stats.json"
    if not path.exists():
        return {}
    try:
        from reqahe.runtime.route_stats import compact_route_stats_summary

        return compact_route_stats_summary(read_json(path), max_skills=6)
    except Exception:
        return {}


def _compact_skill_evolution_digest(digest: dict[str, Any], target_skill_ids: set[str]) -> dict[str, Any]:
    skills = digest.get("skills") if isinstance(digest.get("skills"), dict) else {}
    if not target_skill_ids:
        return {"skills": dict(list(skills.items())[:6])}
    return {
        "skills": {
            skill_id: item
            for skill_id, item in skills.items()
            if str(skill_id) in target_skill_ids
        }
    }


def _initial_refiner_call_stats() -> dict[str, Any]:
    return {
        "fix_plan_payload_chars": 0,
        "edit_payload_full_chars": 0,
        "edit_payload_compact_chars": 0,
        "fix_plan_attempts": 0,
        "edit_generation_attempts": 0,
        "used_compact_retry": False,
        "final_stage": "fix_plan",
        "final_status": "failed",
        "last_error": None,
    }


def _json_chars(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return 0


def _save_llm_payload(refiner_dir: Path, filename: str, payload: dict[str, Any], cfg: Mapping[str, Any]) -> None:
    if not bool(cfg.get("save_llm_payloads", True)):
        return
    try:
        write_json(refiner_dir / filename, payload)
    except Exception:
        return


def _write_refiner_call_stats(refiner_dir: Path, stats: dict[str, Any]) -> None:
    try:
        write_json(refiner_dir / "refiner_call_stats.json", stats)
    except Exception:
        return


def _public_refiner_stage(stage: str) -> str:
    if "fix_plan" in stage:
        return "fix_plan"
    if "generate" in stage or "repair" in stage:
        return "edit_generation"
    if "validate" in stage:
        return "validation"
    if "apply" in stage or stage == "done":
        return "done"
    return "edit_generation"


def _limit_compact_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    if max_chars <= 0 or _json_chars(payload) <= max_chars:
        return payload
    compact = deepcopy(payload)
    for key in ("skill_evolution_digest", "route_stats_summary", "target_component_metadata"):
        if _json_chars(compact) <= max_chars:
            break
        compact.pop(key, None)
    if _json_chars(compact) <= max_chars:
        return compact
    context = compact.get("target_file_context")
    if isinstance(context, dict):
        per_file_limit = max(1000, max_chars // max(len(context), 1) // 2)
        compact["target_file_context"] = {
            path: (str(content)[:per_file_limit] + "\n...[truncated]..." if len(str(content)) > per_file_limit else str(content))
            for path, content in context.items()
        }
    return compact


def _add_repeat_update_warning(stats: dict[str, Any], refiner_dir: Path) -> None:
    touched = [str(item) for item in stats.get("touched_skill_ids") or [] if str(item).strip()]
    stats.setdefault("repeat_update_warning", False)
    stats.setdefault("repeat_update_reason", "")
    if not touched:
        return
    digest = _read_optional_json(refiner_dir.parent / "analysis" / "skill_evolution_digest.json", {})
    skills = digest.get("skills") if isinstance(digest, dict) and isinstance(digest.get("skills"), dict) else {}
    repeated = [
        skill_id
        for skill_id in touched
        if int((skills.get(skill_id) or {}).get("recent_touched_count") or 0) >= 2
    ]
    if repeated:
        stats["repeat_update_warning"] = True
        stats["repeat_update_reason"] = "same skill was updated repeatedly in recent batches: " + ", ".join(sorted(repeated))


def _write_error_report(iteration_path: Path, exc: Exception, data: dict[str, Any], allowed_components: set[str]) -> None:
    refiner_dir = ensure_dir(iteration_path / "refiner")
    _write_failure_artifacts(
        iteration_path,
        refiner_dir,
        exc,
        str(data.get("stage") or "harness_refiner"),
        fix_plan=data.get("fix_plan") or {},
        refinement=data.get("refinement") or {},
        validation_report=data.get("validation_report") or {},
        repair_attempted=bool(data.get("repair_attempted")),
        refiner_stats=data.get("refiner_stats") or {},
        declared_components=allowed_components,
    )


def _write_failure_artifacts(
    iteration_path: Path,
    refiner_dir: Path,
    exc: Exception,
    stage: str,
    *,
    fix_plan: dict[str, Any],
    refinement: dict[str, Any],
    validation_report: dict[str, Any],
    repair_attempted: bool,
    refiner_stats: dict[str, Any],
    declared_components: set[str],
) -> None:
    error_type = type(exc).__name__
    message = str(exc)
    existing_report_path = refiner_dir / "validation_report.json"
    if existing_report_path.exists():
        try:
            validation_report = read_json(existing_report_path)
        except Exception:
            pass
    existing_stats_path = refiner_dir / "refiner_stats.json"
    if existing_stats_path.exists():
        try:
            refiner_stats = {**read_json(existing_stats_path), **(refiner_stats or {})}
        except Exception:
            pass
    if not validation_report:
        validation_report = {
            "ok": False,
            "errors": [message],
            "warnings": [],
            "structured_errors": [],
            "checked_files": [],
        }
    elif not validation_report.get("errors"):
        validation_report = {
            **validation_report,
            "ok": False,
            "errors": [message],
        }
    if not refiner_stats:
        refiner_stats = build_refiner_stats(
            refinement,
            refinement,
            validation_report,
            repair_attempted,
            [],
        )
    refiner_stats = {
        **refiner_stats,
        "ok": False,
        "stage": stage,
        "num_file_edits": len(refinement.get("file_edits") or []) if isinstance(refinement.get("file_edits"), list) else 0,
        "error_type": error_type,
        "message": message,
    }
    error_payload = {
        "ok": False,
        "error_type": error_type,
        "message": message,
        "stage": stage,
        "traceback": traceback.format_exc(),
        "declared_harness_components": sorted(declared_components),
        "raw_output": {
            "fix_plan": fix_plan,
            "refinement": refinement,
            "validation_report": validation_report,
            "repair_attempted": repair_attempted,
            "refiner_stats": refiner_stats,
        },
    }
    try:
        write_json(refiner_dir / "refiner_error.json", error_payload)
        write_text(refiner_dir / "refiner_error.md", f"# Harness Refiner Error\n\n{message}\n")
        write_json(refiner_dir / "validation_report.json", validation_report)
        write_json(refiner_dir / "refiner_stats.json", refiner_stats)
    except Exception:
        pass
    try:
        write_json(iteration_path / "refiner_error.json", error_payload)
        write_text(iteration_path / "refiner_error.md", f"# Harness Refiner Error\n\n{message}\n")
    except Exception:
        pass


def _write_refiner_stage(
    refiner_dir: Path,
    stage: str,
    status: str,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        payload: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "extra": extra or {},
        }
        write_json(refiner_dir / "STAGE.json", payload)
    except Exception:
        return


def _normalize_fix_plan_from_llm(fix_plan: dict[str, Any]) -> dict[str, Any]:
    if isinstance(fix_plan.get("fix_plan"), list) and fix_plan["fix_plan"]:
        normalized = deepcopy(fix_plan)
        for fix in normalized.get("fix_plan") or []:
            if isinstance(fix, dict):
                _normalize_fix_plan_item_fields(fix)
        return normalized
    changes = fix_plan.get("changes")
    if not isinstance(changes, list):
        return fix_plan
    normalized_fixes: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        component = _map_fix_plan_component(str(change.get("component") or ""))
        operation = str(change.get("operation") or "")
        target = _normalize_relative_path(change.get("target") or "")
        component = _infer_writable_component_from_target_hint(component, target)
        normalized_fix = {
            "fix_id": str(change.get("change_id") or f"CH{len(normalized_fixes) + 1}"),
            "component": component,
            "artifact_type": _artifact_type_for_component(component),
            "operation_intent": _map_fix_operation(operation),
            "target_file_hint": target,
            "evidence": change.get("evidence") or [],
            "fix_summary": str(change.get("reason") or ""),
            "expected_effect": str(change.get("expected_effect") or ""),
            "risk": _coerce_fix_risk(change.get("possible_risk")),
            "why_this_instead_of_create": str(change.get("why_this_instead_of_create") or ""),
        }
        _normalize_fix_plan_item_fields(normalized_fix)
        normalized_fixes.append(normalized_fix)
    return {
        "fix_plan": normalized_fixes,
        "rationale": str(fix_plan.get("plan_summary") or ""),
        "evidence_used": fix_plan.get("evidence_used") or [],
        "validation_requirements": fix_plan.get("validation_requirements") or [],
    }


def _normalize_edits_from_llm(
    workspace: Path,
    refinement: dict[str, Any],
    fix_plan: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(refinement.get("file_edits"), list) and refinement["file_edits"]:
        normalized = deepcopy(refinement)
        normalized_edits: list[Any] = []
        for edit in normalized.get("file_edits") or []:
            if not isinstance(edit, dict):
                normalized_edits.append(edit)
                continue
            relative_path = _normalize_relative_path(edit.get("relative_path") or edit.get("path") or "")
            edit["relative_path"] = relative_path
            if not edit.get("operation") and edit.get("action"):
                edit["operation"] = _map_file_edit_action(str(edit.get("action") or ""))
            normalized_edits.append(edit)
        normalized["file_edits"] = normalized_edits
        normalized["changes"] = _normalize_refinement_change_components(
            normalized.get("changes"),
            normalized_edits,
            fix_plan,
            workspace,
        )
        normalized["schema_compliance"] = _build_schema_compliance_from_file_edits(
            [edit for edit in normalized_edits if isinstance(edit, dict)]
        )
        return normalized
    edits = refinement.get("edits")
    if not isinstance(edits, list):
        return refinement
    file_edits: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    similarity_audit: list[dict[str, Any]] = []
    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            continue
        relative_path = _normalize_relative_path(edit.get("path") or "")
        action = str(edit.get("action") or "").strip().lower()
        content = str(edit.get("content") or "")
        reason = str(edit.get("reason") or "")
        operation = _map_file_edit_action(action)
        file_edit: dict[str, Any] = {
            "relative_path": relative_path,
            "operation": operation,
            "new_content": content,
        }
        if operation == "replace" and (workspace / relative_path).is_file():
            file_edit["old"] = read_text(workspace / relative_path)
        file_edits.append(file_edit)
        fix_id = _fix_id_for_target(fix_plan, relative_path) or f"CH{idx + 1}"
        component = _infer_component_and_schema_from_path(relative_path)[0] or path_component(workspace, relative_path) or "unknown"
        changes.append(
            {
                "change_id": f"C{idx + 1}",
                "fix_id": fix_id,
                "component": component,
                "summary": reason,
                "evidence": [reason] if reason else [],
                "expected_effect": "",
                "risk": "low",
            }
        )
        if is_skill_markdown_path(relative_path) and operation in {"create", "replace"}:
            similarity_audit.append(
                {
                    "proposed_intent": reason or relative_path,
                    "closest_existing_skill_id": None if operation == "create" else skill_id_from_path(relative_path),
                    "closest_existing_path": None if operation == "create" else relative_path,
                    "similarity_score": 0.0,
                    "matched_dimensions": [],
                    "decision": "create_new" if operation == "create" else "update_existing",
                    "justification": reason or "aligned with approved fix plan",
                    "target_path": relative_path,
                }
            )
    normalized = {
        "changes": changes,
        "file_edits": file_edits,
        "schema_compliance": _build_schema_compliance_from_file_edits(file_edits),
        "refiner_rationale": str(refinement.get("self_check", {}).get("notes", [""])[0] if isinstance(refinement.get("self_check"), dict) else ""),
        "similarity_audit": similarity_audit,
        "self_validation": _skill_self_validation_defaults() if similarity_audit else {},
        "self_check": refinement.get("self_check") or {},
    }
    if not normalized["refiner_rationale"]:
        normalized["refiner_rationale"] = fix_plan.get("rationale") or "apply approved fix plan"
    return normalized


def _skill_self_validation_defaults() -> dict[str, bool]:
    return {
        "similarity_gate_applied": True,
        "no_duplicate_skill_created": True,
        "no_append_to_skill_markdown": True,
        "no_skill_readme_edit": True,
    }


def _fix_id_for_target(fix_plan: dict[str, Any], relative_path: str) -> str:
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        if _normalize_relative_path(fix.get("target_file_hint") or "") == _normalize_relative_path(relative_path):
            return str(fix.get("fix_id") or "")
    return ""


def _fix_component_for_id(fix_plan: dict[str, Any], fix_id: str) -> str:
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        if str(fix.get("fix_id") or "") == fix_id:
            return str(fix.get("component") or "")
    return ""


def _fix_target_for_id(fix_plan: dict[str, Any], fix_id: str) -> str:
    for fix in fix_plan.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        if str(fix.get("fix_id") or "") == fix_id:
            return _normalize_relative_path(fix.get("target_file_hint") or "")
    return ""


def _normalize_refinement_change_components(
    changes: Any,
    file_edits: list[Any],
    fix_plan: dict[str, Any],
    workspace: Path,
) -> list[dict[str, Any]]:
    if not isinstance(changes, list):
        return []
    first_path = ""
    for edit in file_edits:
        if isinstance(edit, dict) and edit.get("relative_path"):
            first_path = _normalize_relative_path(edit.get("relative_path") or "")
            break
    normalized_changes: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        item = deepcopy(change)
        fix_id = str(item.get("fix_id") or "")
        target_component = _fix_component_for_id(fix_plan, fix_id)
        target_path = _fix_target_for_id(fix_plan, fix_id) or first_path
        if not target_component and target_path:
            target_component = _infer_component_and_schema_from_path(target_path)[0] or path_component(workspace, target_path) or ""
        item["component"] = _infer_writable_component_from_target_hint(
            str(item.get("component") or target_component),
            target_path,
        )
        if not item["component"] and target_component:
            item["component"] = target_component
        normalized_changes.append(item)
    return normalized_changes


def _map_fix_plan_component(component: str) -> str:
    return str(component or "").strip()


def _artifact_type_for_component(component: str) -> str:
    return {
        "system_prompt": "system_prompt_section_v1",
        "skills": "skill_markdown_v1",
        "self_reflection": "reflection_check_bundle_v1",
    }.get(str(component or "").strip(), str(component or "").strip())


def _map_fix_operation(operation: str) -> str:
    op = str(operation or "").strip().lower()
    if op in {"create", "update", "replace", "demote", "disable", "remove", "validate"}:
        return op
    return "update"


def _coerce_fix_risk(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high"}:
        return text
    if "high" in text:
        return "high"
    if "medium" in text:
        return "medium"
    return "low"


def _normalize_fix_plan_target_hints(fix_plan: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize bundle-style or legacy target hints produced by LLM.
    Example:
    'self_reflection/x/check.py + self_reflection/x/PROMPT.md'
    -> 'self_reflection/x/check.py'
    """
    normalized = deepcopy(fix_plan)
    for fix in normalized.get("fix_plan") or []:
        if not isinstance(fix, dict):
            continue
        hint = str(fix.get("target_file_hint") or "").strip()
        if hint:
            fix["target_file_hint"] = _normalize_single_target_hint(hint)
        _normalize_fix_plan_item_fields(fix)
    return normalized


def _prompt_path_to_check_path(prompt_path: str) -> str:
    parts = _relative_path_parts(prompt_path)
    if len(parts) == 3 and parts[0] == "self_reflection" and parts[-1] == "PROMPT.md":
        return f"self_reflection/{parts[1]}/check.py"
    return prompt_path


def _normalize_single_target_hint(target_hint: str) -> str:
    hint = _normalize_relative_path(target_hint)
    if "+" in hint:
        parts = [part.strip() for part in hint.split("+") if part.strip()]
        check_paths = [part for part in parts if part.startswith("self_reflection/") and part.endswith("/check.py")]
        if check_paths:
            return check_paths[0]
        prompt_paths = [part for part in parts if part.startswith("self_reflection/") and part.endswith("/PROMPT.md")]
        if prompt_paths:
            return _prompt_path_to_check_path(prompt_paths[0])
        return parts[0] if parts else hint
    if hint.startswith("self_reflection/") and hint.endswith("/PROMPT.md"):
        return _prompt_path_to_check_path(hint)
    return hint


def _normalize_relative_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip()


def _relative_path_parts(value: Any) -> tuple[str, ...]:
    normalized = _normalize_relative_path(value)
    return tuple(part for part in normalized.split("/") if part)


def _map_file_edit_action(action: str) -> str:
    normalized = str(action or "").strip().lower()
    if normalized == "delete":
        return "delete"
    if normalized == "create":
        return "create"
    return "replace"


def _infer_component_and_schema_from_path(relative_path: str) -> tuple[str | None, str | None]:
    path = _normalize_relative_path(relative_path)
    if path == "system_prompt.md":
        return "system_prompt", "system_prompt_section_v1"

    parts = _relative_path_parts(path)
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return "skills", "skill_markdown_v1"

    if len(parts) >= 3 and parts[0] == "self_reflection" and parts[-1] in {"check.py", "PROMPT.md"}:
        return "self_reflection", "reflection_check_bundle_v1"

    return None, None


def _build_schema_compliance_from_file_edits(file_edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for edit in file_edits:
        relative_path = _normalize_relative_path(edit.get("relative_path") or edit.get("path") or "")
        component, schema_name = _infer_component_and_schema_from_path(relative_path)
        if not component or not schema_name:
            continue
        grouped.setdefault((component, schema_name), set()).add(relative_path)

    return [
        {
            "component": component,
            "schema_name": schema_name,
            "new_or_updated_files": sorted(paths),
        }
        for (component, schema_name), paths in sorted(grouped.items())
    ]


def _infer_writable_component_from_target_hint(component: str, target_file_hint: str) -> str:
    normalized_component = str(component or "").strip()
    target = _normalize_relative_path(target_file_hint)
    parts = _relative_path_parts(target)

    if target in INTERNAL_ROUTER_TARGETS:
        return "system_prompt"
    if target == "system_prompt.md":
        return "system_prompt"
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return "skills"
    if len(parts) >= 3 and parts[0] == "self_reflection" and parts[-1] == "check.py":
        return "self_reflection"
    if normalized_component in INTERNAL_ROUTER_TARGETS:
        return "system_prompt"
    return normalized_component


def _normalize_fix_plan_item_fields(item: dict[str, Any]) -> dict[str, Any]:
    target = _normalize_single_target_hint(str(item.get("target_file_hint") or item.get("target") or ""))
    component = _map_fix_plan_component(str(item.get("component") or ""))
    if target in INTERNAL_ROUTER_TARGETS:
        target = "system_prompt.md"
        component = "system_prompt"
    component = _infer_writable_component_from_target_hint(component, target)
    item["component"] = component
    item["artifact_type"] = _artifact_type_for_component(component)
    item["target_file_hint"] = target
    if not item.get("operation_intent"):
        item["operation_intent"] = _map_fix_operation(str(item.get("operation") or ""))
    else:
        item["operation_intent"] = _map_fix_operation(str(item.get("operation_intent") or ""))
    if item.get("risk") not in {"low", "medium", "high"}:
        item["risk"] = _coerce_fix_risk(item.get("possible_risk") or item.get("risk"))
    return item


def _sanitize_fix_plan_or_drop_invalid(
    fix_plan: dict[str, Any],
    declared_components: set[str] | list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    items = list(fix_plan.get("fix_plan") or fix_plan.get("changes") or [])
    allowed_components = _coerce_declared_component_names(declared_components)
    valid_items: list[dict[str, Any]] = []
    dropped_items: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            dropped_items.append({"item": item, "reason": "fix_plan item must be an object"})
            continue
        original = deepcopy(item)
        try:
            normalized = deepcopy(item)
            normalized.setdefault("fix_id", str(normalized.get("change_id") or f"CH{idx + 1}"))
            normalized.setdefault("evidence", [])
            normalized.setdefault("fix_summary", str(normalized.get("reason") or ""))
            normalized.setdefault("expected_effect", "")
            _normalize_fix_plan_item_fields(normalized)
            _validate_one_fix_plan_item(normalized, allowed_components)
            valid_items.append(normalized)
        except Exception as exc:
            dropped_items.append({"item": original, "reason": str(exc)})

    if not valid_items:
        raise RuntimeError(f"all fix_plan items invalid: {dropped_items}")

    normalized_plan = deepcopy(fix_plan)
    normalized_plan["fix_plan"] = valid_items[:3]
    normalized_plan["dropped_invalid_fixes"] = dropped_items
    return normalized_plan


def _coerce_declared_component_names(value: set[str] | list[dict[str, str]] | None) -> set[str]:
    if value is None:
        return {"system_prompt", "skills", "self_reflection"}
    if isinstance(value, set):
        return set(value)
    return {str(item.get("name") or "") for item in value if isinstance(item, dict) and item.get("name")}


def _validate_one_fix_plan_item(item: dict[str, Any], declared_components: set[str]) -> None:
    validate_fix_plan({"fix_plan": [item]}, declared_components)


def _compact_skill_catalog(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "")
        compact.append(
            {
                "skill_id": item.get("skill_id"),
                "path": item.get("path") or item.get("relative_path"),
                "name": item.get("name"),
                "description": description,
                "digest": description[:160],
                "trigger": item.get("trigger"),
                "expected_effect": item.get("expected_effect"),
                "priority": item.get("priority"),
                "version": item.get("version"),
                "status": item.get("status"),
            }
        )
    return compact


def _drop_offending_high_similarity_skill_creates(
    proposed_edits: dict[str, Any],
    validation_report: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    structured = validation_report.get("structured_errors") or []
    paths_to_drop: set[str] = set()
    warnings: list[str] = []
    for item in structured:
        if not isinstance(item, dict):
            continue
        if item.get("error_type") != "high_similarity_skill_create":
            continue
        path = str(item.get("new_skill_path") or "").strip()
        if path:
            paths_to_drop.add(path)
    if not paths_to_drop:
        return proposed_edits, warnings
    normalized = deepcopy(proposed_edits)
    file_edits = normalized.get("file_edits")
    if not isinstance(file_edits, list):
        return proposed_edits, warnings
    kept: list[dict[str, Any]] = []
    for edit in file_edits:
        if not isinstance(edit, dict):
            kept.append(edit)
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        if relative_path in paths_to_drop and operation == "create" and is_skill_markdown_path(relative_path):
            warnings.append(f"Dropped high-similarity skill create edit: {relative_path}")
            continue
        kept.append(edit)
    normalized["file_edits"] = kept
    if isinstance(normalized.get("similarity_audit"), list):
        normalized["similarity_audit"] = [
            item
            for item in normalized["similarity_audit"]
            if not (
                isinstance(item, dict)
                and str(item.get("target_path") or "") in paths_to_drop
                and str(item.get("decision") or "") == "create_new"
            )
        ]
    if isinstance(normalized.get("schema_compliance"), list):
        updated_compliance: list[dict[str, Any]] = []
        for item in normalized["schema_compliance"]:
            if not isinstance(item, dict):
                continue
            files = item.get("new_or_updated_files")
            if isinstance(files, list):
                filtered_files = [path for path in files if str(path) not in paths_to_drop]
                if not filtered_files:
                    continue
                item = {**item, "new_or_updated_files": filtered_files}
            updated_compliance.append(item)
        normalized["schema_compliance"] = updated_compliance
    has_skill_edits = any(
        is_skill_markdown_path(str(edit.get("relative_path") or ""))
        for edit in kept
        if isinstance(edit, dict)
    )
    if not has_skill_edits and isinstance(normalized.get("changes"), list):
        normalized["changes"] = [
            change
            for change in normalized["changes"]
            if not (isinstance(change, dict) and str(change.get("component") or "") == "skills")
        ]
    return normalized, warnings


def _commit_workspace(workspace: Path, message: str) -> None:
    try:
        subprocess.run(["git", "init"], cwd=workspace, check=False, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=False, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=False, capture_output=True, text=True)
    except Exception:
        return
