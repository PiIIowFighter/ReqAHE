from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml

from reqahe.harness.component_schema import (
    ALLOWED_ARTIFACT_TYPES,
    validate_component_file,
    validate_schema_compliance_block,
    validate_skill_minimal_frontmatter,
    validate_workspace_component_schemas,
)
from reqahe.harness.component_spec import is_declared_harness_path, path_component
from reqahe.refiner.skill_similarity import (
    find_similar_skills,
    is_skill_markdown_path,
    skill_id_from_path,
)


FORBIDDEN_PATH_PREFIXES = (
    "dataset",
    "datasets",
    "evaluator",
    "oracle",
    "judge",
    "metrics",
    "runs",
    "envs",
    "configs",
    "src/reqahe/runtime/reflection.py",
    "src/reqahe/evaluator",
    "src/reqahe/runner",
)

FORBIDDEN_PATH_FRAGMENTS = (
    "hidden_requirement",
    "answer_key",
    "oracle_prompt",
    "evaluator_prompt",
    "api_key",
    ".env",
)

APPEND_ALLOWED_SUFFIXES = {".md"}
APPEND_ALLOWED_EXACT = {"system_prompt.md"}


def validate_proposed_edits(
    workspace_dir: Path,
    proposed_edits: dict[str, Any],
    approved_fix_plan: dict[str, Any],
    write_policy: dict[str, Any],
    selected_schemas: dict[str, Any],
    declared_components: set[str],
    *,
    raw_refinement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from reqahe.refiner.pipeline import (
        _edit_new_content,
        _plan_edit,
        _schema_compliance_items,
        _validate_component,
        _validate_file_edit_target,
        _validate_schema_artifact_paths,
    )

    errors: list[str] = []
    warnings: list[str] = []
    structured_errors: list[dict[str, Any]] = []
    checked_files: list[str] = []
    workspace = Path(workspace_dir)
    max_fixes = int(write_policy.get("max_fixes") or 3)
    allowed_components = set(write_policy.get("allowed_components") or declared_components)
    allow_registry_edit = bool(write_policy.get("allow_registry_edit"))

    normalized = proposed_edits
    file_edits = normalized.get("file_edits") or []
    changes = normalized.get("changes") or []
    fix_ids = {str(item.get("fix_id")) for item in approved_fix_plan.get("fix_plan") or [] if item.get("fix_id")}

    registry_source = raw_refinement if raw_refinement is not None else proposed_edits
    for edit in registry_source.get("file_edits") or []:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        if relative_path == "self_reflection/registry.yaml" and not allow_registry_edit:
            errors.append(
                "LLM file_edits must not directly edit self_reflection/registry.yaml; "
                "registry is synchronized by the runtime."
            )

    if not isinstance(file_edits, list) or not file_edits:
        errors.append("file_edits must be a non-empty list")
    if len(file_edits) > max_fixes:
        errors.append(f"file_edits count {len(file_edits)} exceeds max_fixes {max_fixes}")

    for change in changes:
        if not isinstance(change, dict):
            errors.append("changes entries must be objects")
            continue
        fix_id = str(change.get("fix_id") or "")
        if fix_ids and fix_id and fix_id not in fix_ids:
            errors.append(f"change references unknown fix_id: {fix_id}")
        component = str(change.get("component") or "")
        try:
            _validate_component(component, allowed_components)
        except RuntimeError as exc:
            errors.append(str(exc))

    for edit in file_edits:
        if not isinstance(edit, dict):
            errors.append("file_edits entries must be objects")
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        checked_files.append(relative_path)
        errors.extend(_validate_forbidden_path(relative_path))
        if relative_path == "self_reflection/registry.yaml" and not allow_registry_edit:
            errors.append(
                "LLM file_edits must not directly edit self_reflection/registry.yaml; "
                "registry is synchronized by the runtime."
            )
        if not _path_allowed_by_write_policy(relative_path, write_policy, workspace):
            errors.append(f"path not allowed by write_policy: {relative_path}")
        if not is_declared_harness_path(workspace, relative_path, for_write=True):
            errors.append(f"path is not declared by current harness seed: {relative_path}")
        component = path_component(workspace, relative_path)
        if component:
            try:
                _validate_component(component, allowed_components)
            except RuntimeError as exc:
                errors.append(str(exc))
            try:
                _validate_file_edit_target(relative_path, operation, component)
            except RuntimeError as exc:
                errors.append(str(exc))
        if operation == "create" and (workspace / relative_path).exists():
            errors.append(f"create target already exists: {relative_path}")
        if operation == "delete" and not (workspace / relative_path).exists():
            errors.append(f"delete target does not exist: {relative_path}")
        if operation == "append":
            errors.extend(_validate_append_target(relative_path, component))
        if operation == "replace":
            target = workspace / relative_path
            if target.exists():
                old = str(edit.get("old") or "")
                current = target.read_text(encoding="utf-8")
                if old and old not in current:
                    errors.append(f"replace old text not found in {relative_path}")

    try:
        validate_refinement_schema(normalized, allowed_components)
    except RuntimeError as exc:
        errors.append(str(exc))

    try:
        validate_schema_compliance_block(
            normalized,
            [str(e.get("relative_path")) for e in file_edits if e.get("relative_path")],
        )
    except RuntimeError as exc:
        errors.append(str(exc))

    errors.extend(_validate_selected_schemas(normalized, selected_schemas, workspace))
    errors.extend(_validate_max_skill_creates(file_edits))
    errors.extend(_validate_skill_frontmatter_edits(file_edits))
    errors.extend(_validate_skill_similarity_rules(normalized, workspace, structured_errors))
    errors.extend(_validate_reflection_registry_edit_bundle(file_edits))

    if not errors:
        try:
            from reqahe.refiner.pipeline import _sync_reflection_registry_entries

            planned = [_plan_edit(workspace, edit) for edit in file_edits]
            planned = _sync_reflection_registry_entries(workspace, planned)
            staged = {path: content for path, content in planned}
            try:
                validate_workspace_component_schemas(workspace, staged_files=staged)
            except RuntimeError as exc:
                errors.append(str(exc))
        except (RuntimeError, ValueError, TypeError) as exc:
            errors.append(str(exc))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "structured_errors": structured_errors,
        "checked_files": sorted(set(checked_files)),
    }


def validate_refinement_schema(data: dict[str, Any], allowed_components: set[str]) -> None:
    from reqahe.refiner.pipeline import (
        _edit_new_content,
        _schema_compliance_items,
        _validate_component,
        _validate_schema_artifact_paths,
    )

    if not isinstance(data.get("changes"), list) or not data["changes"]:
        raise RuntimeError("harness refinement generation failed: changes must be a non-empty list")
    if not isinstance(data.get("file_edits"), list) or not data["file_edits"]:
        raise RuntimeError("harness refinement generation failed: file_edits must be a non-empty list")
    if len(data["file_edits"]) > 3:
        raise RuntimeError("harness refinement generation failed: file_edits must contain at most 3 edits")
    compliance = _schema_compliance_items(data)
    if not compliance:
        raise RuntimeError("harness refinement generation failed: schema_compliance must be a non-empty list")
    if not isinstance(data.get("similarity_audit"), list):
        raise RuntimeError("harness refinement generation failed: similarity_audit must be a list")
    for change in data["changes"]:
        if not isinstance(change, dict):
            raise RuntimeError("harness refinement generation failed: changes entries must be objects")
        _validate_component(str(change.get("component") or ""), allowed_components)
    for edit in data["file_edits"]:
        if not isinstance(edit, dict) or not edit.get("relative_path"):
            raise RuntimeError("harness refinement generation failed: invalid file edit")
        operation = edit.get("operation")
        if operation not in {"create", "append", "replace", "delete"}:
            raise RuntimeError("harness refinement generation failed: invalid file edit operation")
        if operation in {"create", "append"} and _edit_new_content(edit) is None:
            raise RuntimeError("harness refinement generation failed: create/append edit requires new_content")
        if operation == "replace" and (not isinstance(edit.get("old"), str) or _edit_new_content(edit) is None):
            raise RuntimeError("harness refinement generation failed: replace edit requires old and new_content")
    for item in compliance:
        if not isinstance(item, dict):
            raise RuntimeError("harness refinement generation failed: schema_compliance entries must be objects")
        component = str(item.get("component") or "")
        schema_name = str(item.get("schema_name") or "")
        _validate_component(component, allowed_components)
        if schema_name not in ALLOWED_ARTIFACT_TYPES:
            raise RuntimeError("harness refinement generation failed: invalid schema_compliance schema_name")
        _validate_schema_artifact_paths(component, schema_name, item.get("new_or_updated_files"))


def validate_paths(data: dict[str, Any], workspace_dir: str | Path, allowed_components: set[str]) -> None:
    from reqahe.refiner.pipeline import _validate_component, _validate_file_edit_target

    for edit in data["file_edits"]:
        relative_path = str(edit["relative_path"])
        if not is_declared_harness_path(workspace_dir, relative_path, for_write=True):
            raise RuntimeError(f"path is not declared by current harness seed: {relative_path}")
        component = path_component(workspace_dir, relative_path)
        if component:
            _validate_component(component, allowed_components)
        _validate_file_edit_target(relative_path, str(edit.get("operation") or ""), component)


def validate_fix_plan(data: dict[str, Any], allowed_components: set[str]) -> None:
    from reqahe.refiner.pipeline import (
        _artifact_type_matches_component,
        _validate_component,
        _validate_fix_target_hint,
    )

    if "file_edits" in data:
        raise RuntimeError("harness fix plan selection failed: fix_plan step must not output file_edits")
    fixes = data.get("fix_plan")
    if not isinstance(fixes, list) or not fixes:
        raise RuntimeError("harness fix plan selection failed: fix_plan must be a non-empty list")
    if len(fixes) > 3:
        raise RuntimeError("harness fix plan selection failed: fix_plan must contain at most 3 fixes")
    for fix in fixes:
        if not isinstance(fix, dict):
            raise RuntimeError("harness fix plan selection failed: fix items must be objects")
        component = str(fix.get("component") or "")
        _validate_component(component, allowed_components)
        artifact_type = str(fix.get("artifact_type") or "")
        if artifact_type not in ALLOWED_ARTIFACT_TYPES:
            raise RuntimeError("harness fix plan selection failed: invalid artifact_type")
        if not _artifact_type_matches_component(component, artifact_type):
            raise RuntimeError("harness fix plan selection failed: artifact_type does not match component")
        operation_intent = str(fix.get("operation_intent") or "")
        if operation_intent not in {
            "create",
            "append",
            "replace",
            "update",
            "demote",
            "disable",
            "validate",
            "remove",
        }:
            raise RuntimeError("harness fix plan selection failed: invalid operation_intent")
        target_hint = str(fix.get("target_file_hint") or "").strip()
        _validate_fix_target_hint(component, artifact_type, operation_intent, target_hint)
        if fix.get("risk") not in {"low", "medium", "high"}:
            raise RuntimeError("harness fix plan selection failed: invalid risk")


def _validate_refinement(data: dict[str, Any], allowed_components: set[str], workspace_dir: str | Path) -> None:
    validate_refinement_schema(data, allowed_components)
    validate_paths(data, workspace_dir, allowed_components)


def _validate_forbidden_path(relative_path: str) -> list[str]:
    errors: list[str] = []
    normalized = Path(relative_path).as_posix().lower()
    for prefix in FORBIDDEN_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            errors.append(f"forbidden path prefix: {relative_path}")
    for fragment in FORBIDDEN_PATH_FRAGMENTS:
        if fragment in normalized:
            errors.append(f"forbidden path fragment in {relative_path}")
    return errors


def _path_allowed_by_write_policy(relative_path: str, write_policy: dict[str, Any], workspace: Path) -> bool:
    path_patterns = write_policy.get("path_patterns") or {}
    allowed_components = write_policy.get("allowed_components") or []
    rel = Path(relative_path)
    for component in allowed_components:
        pattern = str(path_patterns.get(component) or "")
        if not pattern:
            continue
        if component == "system_prompt" and relative_path == pattern:
            return True
        if component == "skills" and rel.name == "SKILL.md" and len(rel.parts) == 3 and rel.parts[0] == "skills":
            return True
        if component == "memory" and rel.parent.as_posix() == "memory" and rel.name != "README.md" and rel.suffix == ".md":
            return True
        if (
            component == "self_reflection"
            and relative_path == "self_reflection/registry.yaml"
        ):
            return True
        if (
            component == "self_reflection"
            and len(rel.parts) == 3
            and rel.parts[0] == "self_reflection"
            and rel.name in {"check.py", "PROMPT.md"}
        ):
            return True
        if component not in {"system_prompt", "skills", "memory", "self_reflection"}:
            root = pattern.split("<", 1)[0].rstrip("/")
            if root and (relative_path == root or relative_path.startswith(root + "/")):
                return True
    return is_declared_harness_path(workspace, relative_path, for_write=True)


def _validate_append_target(relative_path: str, component: str | None) -> list[str]:
    errors: list[str] = []
    path = Path(relative_path)
    if is_skill_markdown_path(relative_path):
        errors.append(
            "SKILL.md must be updated with replace, not append, to avoid duplicated sections and schema drift."
        )
        return errors
    if path.name == "README.md":
        errors.append(f"do not append schema artifacts to {relative_path}")
    if component in {"skills", "memory", "self_reflection"}:
        errors.append(f"append not allowed for {component} schema artifact {relative_path}")
    if relative_path not in APPEND_ALLOWED_EXACT and path.suffix.lower() not in APPEND_ALLOWED_SUFFIXES:
        errors.append(f"append not allowed for file type: {relative_path}")
    return errors


def _validate_skill_similarity_rules(
    proposed_edits: dict[str, Any],
    workspace: Path,
    structured_errors: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    structured = structured_errors if structured_errors is not None else []
    file_edits = proposed_edits.get("file_edits") or []
    similarity_audit = proposed_edits.get("similarity_audit")
    if not isinstance(similarity_audit, list):
        errors.append("similarity_audit must be a list")
        return errors

    skill_edits: list[dict[str, Any]] = []
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        if relative_path == "skills/README.md":
            errors.append("skills/README.md must not be edited by the skill refiner.")
        if is_skill_markdown_path(relative_path) and operation in {"create", "replace", "append"}:
            skill_edits.append(edit)
            if operation == "replace":
                target = workspace / relative_path
                if target.exists():
                    old = str(edit.get("old") or "")
                    current = target.read_text(encoding="utf-8")
                    if not old.strip():
                        errors.append(f"replace on {relative_path} requires non-empty old matching existing content")
                    elif old != current:
                        errors.append(
                            f"replace old text must exactly match existing file content for {relative_path}"
                        )

    if not skill_edits:
        return errors

    if not similarity_audit:
        errors.append(
            "similarity_audit must be non-empty when file_edits contain skill SKILL.md create or replace operations"
        )
        return errors

    audit_by_path: dict[str, dict[str, Any]] = {}
    for item in similarity_audit:
        if not isinstance(item, dict):
            errors.append("similarity_audit entries must be objects")
            continue
        closest_path = str(item.get("closest_existing_path") or "")
        decision = str(item.get("decision") or "")
        proposed_intent = str(item.get("proposed_intent") or "").strip()
        justification = str(item.get("justification") or "").strip()
        try:
            score = float(item.get("similarity_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
            errors.append("similarity_audit similarity_score must be a number")

        if decision == "create_new":
            if not proposed_intent:
                errors.append("similarity_audit create_new requires non-empty proposed_intent")
            if score >= 0.75:
                errors.append(
                    "High-similarity skill creation is forbidden. Use replace/update on the closest existing skill."
                )
                structured.append(
                    {
                        "error_type": "high_similarity_skill_create",
                        "new_skill_path": str(item.get("target_path") or closest_path or ""),
                        "closest_existing_skill": str(item.get("closest_existing_path") or closest_path or ""),
                        "required_action": "replace_existing_skill_instead_of_create",
                    }
                )
            if not justification:
                errors.append("similarity_audit create_new requires non-empty justification")
            elif 0.45 <= score < 0.75 and not _has_strong_create_justification(justification):
                errors.append(
                    "Medium-similarity create_new requires justification explaining trigger difference, "
                    "procedure difference, and why merge into closest existing skill is insufficient"
                )
        if closest_path:
            audit_by_path[closest_path] = item

    used_audit_indices: set[int] = set()
    for edit in skill_edits:
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        skill_id = skill_id_from_path(relative_path)
        matched, _audit_index = _find_audit_for_skill_edit(
            relative_path,
            skill_id,
            similarity_audit,
            used_indices=used_audit_indices,
        )
        if not matched:
            errors.append(f"no similarity_audit entry explains skill file_edit for {relative_path}")
            continue
        if operation == "create":
            if str(matched.get("decision") or "") != "create_new":
                errors.append(
                    f"skill create at {relative_path} requires similarity_audit decision create_new"
                )
            _validate_deterministic_duplicate_create(
                errors,
                structured,
                workspace,
                relative_path,
                edit,
                matched,
            )

    self_validation = proposed_edits.get("self_validation")
    if isinstance(self_validation, dict) and skill_edits:
        for field in (
            "similarity_gate_applied",
            "no_duplicate_skill_created",
            "no_append_to_skill_markdown",
            "no_skill_readme_edit",
        ):
            if self_validation.get(field) is not True:
                errors.append(f"self_validation.{field} must be true when skill edits are present")

    return errors


def _has_strong_create_justification(justification: str) -> bool:
    lowered = justification.lower()
    has_trigger = "trigger" in lowered
    has_procedure = "procedure" in lowered or "procedur" in lowered
    has_difference = any(
        token in lowered
        for token in (
            "different",
            "distinct",
            "separate",
            "not merge",
            "should not merge",
            "cannot merge",
            "unlike",
            "versus",
            " vs ",
        )
    )
    return has_trigger and has_procedure and has_difference


def _validate_skill_frontmatter_edits(file_edits: list[Any]) -> list[str]:
    from reqahe.refiner.pipeline import _normalize_component_content

    errors: list[str] = []
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        if not is_skill_markdown_path(relative_path):
            continue
        if operation not in {"create", "replace", "append"}:
            continue
        content = edit.get("new_content")
        if content is None and isinstance(edit.get("lines"), list):
            content = "\n".join(str(line) for line in edit["lines"])
        if not isinstance(content, str):
            errors.append(f"{relative_path} requires complete SKILL.md content for validation")
            continue
        normalized_content = _normalize_component_content(relative_path, content)
        errors.extend(validate_skill_minimal_frontmatter(relative_path, normalized_content))
    return errors


def _validate_max_skill_creates(file_edits: list[Any]) -> list[str]:
    create_count = 0
    for edit in file_edits:
        if not isinstance(edit, dict):
            continue
        relative_path = str(edit.get("relative_path") or "")
        operation = str(edit.get("operation") or "")
        if operation == "create" and is_skill_markdown_path(relative_path):
            create_count += 1
    if create_count > 2:
        return [
            "At most 2 new skills may be created in one batch. "
            "Similar or overlapping skill ideas must be consolidated by replacing existing skills."
        ]
    return []


def _validate_reflection_registry_edit_bundle(file_edits: list[Any]) -> list[str]:
    registry_edits = [
        edit
        for edit in file_edits
        if isinstance(edit, dict) and str(edit.get("relative_path") or "") == "self_reflection/registry.yaml"
    ]
    if not registry_edits:
        return []
    check_paths = {
        str(edit.get("relative_path") or "")
        for edit in file_edits
        if isinstance(edit, dict)
        and str(edit.get("relative_path") or "").startswith("self_reflection/")
        and str(edit.get("relative_path") or "").endswith("/check.py")
    }
    prompt_paths = {
        str(edit.get("relative_path") or "")
        for edit in file_edits
        if isinstance(edit, dict)
        and str(edit.get("relative_path") or "").startswith("self_reflection/")
        and str(edit.get("relative_path") or "").endswith("/PROMPT.md")
    }
    if not check_paths or not prompt_paths:
        return ["self_reflection/registry.yaml edit must accompany self_reflection/<id>/check.py and PROMPT.md edits"]
    content = _edit_content(registry_edits[-1])
    if content is None:
        return ["self_reflection/registry.yaml edit requires complete registry content"]
    try:
        registry = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        return [f"self_reflection/registry.yaml is not valid YAML: {exc}"]
    checks = registry.get("checks") if isinstance(registry, dict) else None
    if not isinstance(checks, list):
        return ["self_reflection/registry.yaml checks must be a list"]
    registered_check_paths = {
        (Path("self_reflection") / str(item.get("file") or "")).as_posix()
        for item in checks
        if isinstance(item, dict) and item.get("file")
    }
    missing = sorted(check_paths - registered_check_paths)
    if missing:
        return [f"self_reflection/registry.yaml must register edited check files: {missing}"]
    return []


def _edit_content(edit: dict[str, Any]) -> str | None:
    if isinstance(edit.get("new_content"), str):
        return str(edit["new_content"])
    if isinstance(edit.get("lines"), list):
        return "\n".join(str(line) for line in edit["lines"])
    if isinstance(edit.get("new_lines"), list):
        return "\n".join(str(line) for line in edit["new_lines"])
    return None


def _find_audit_for_skill_edit(
    relative_path: str,
    skill_id: str,
    similarity_audit: list[Any],
    *,
    used_indices: set[int] | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    used = used_indices if used_indices is not None else set()

    for idx, item in enumerate(similarity_audit):
        if idx in used or not isinstance(item, dict):
            continue
        for key in ("target_path", "proposed_path", "closest_existing_path"):
            path_value = str(item.get(key) or "")
            if path_value and path_value == relative_path:
                if used_indices is not None:
                    used_indices.add(idx)
                return item, idx

    for idx, item in enumerate(similarity_audit):
        if idx in used or not isinstance(item, dict):
            continue
        closest_path = str(item.get("closest_existing_path") or "")
        proposed_intent = str(item.get("proposed_intent") or "")
        if closest_path == relative_path:
            if used_indices is not None:
                used_indices.add(idx)
            return item, idx
        if skill_id and skill_id in proposed_intent:
            if used_indices is not None:
                used_indices.add(idx)
            return item, idx
        if relative_path in proposed_intent:
            if used_indices is not None:
                used_indices.add(idx)
            return item, idx

    if len(similarity_audit) == 1 and isinstance(similarity_audit[0], dict) and 0 not in used:
        if used_indices is not None:
            used_indices.add(0)
        return similarity_audit[0], 0

    for idx, item in enumerate(similarity_audit):
        if idx in used or not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "")
        if decision in {"create_new", "replace_existing", "update_existing"}:
            if used_indices is not None:
                used_indices.add(idx)
            return item, idx
    return None, None


def _validate_deterministic_duplicate_create(
    errors: list[str],
    structured_errors: list[dict[str, Any]],
    workspace: Path,
    relative_path: str,
    edit: dict[str, Any],
    audit_item: dict[str, Any],
) -> None:
    from reqahe.refiner.skill_similarity import build_existing_skill_catalog

    catalog = build_existing_skill_catalog(workspace)
    if not catalog:
        return
    proposed_text = " ".join(
        part
        for part in (
            str(audit_item.get("proposed_intent") or ""),
            str(edit.get("new_content") or "")[:500],
            skill_id_from_path(relative_path),
        )
        if part
    )
    scored = find_similar_skills(proposed_text, catalog, top_k=1)
    if not scored:
        return
    top = scored[0]
    deterministic_score = float(top.get("similarity_score") or 0.0)
    if deterministic_score >= 0.75:
        errors.append(
            "High-similarity skill creation is forbidden. Use replace/update on the closest existing skill."
        )
        closest_path = str(top.get("path") or top.get("relative_path") or "")
        structured_errors.append(
            {
                "error_type": "high_similarity_skill_create",
                "new_skill_path": relative_path,
                "closest_existing_skill": closest_path,
                "required_action": "replace_existing_skill_instead_of_create",
            }
        )


def _validate_selected_schemas(
    proposed_edits: dict[str, Any],
    selected_schemas: dict[str, Any],
    workspace: Path,
) -> list[str]:
    from reqahe.refiner.pipeline import _edit_new_content, _normalize_component_content, _schema_compliance_items

    errors: list[str] = []
    allowed_schema_names = set(selected_schemas.keys())
    for item in _schema_compliance_items(proposed_edits):
        schema_name = str(item.get("schema_name") or "")
        if schema_name and schema_name not in allowed_schema_names:
            errors.append(f"schema {schema_name} was not provided in selected_schemas")
    preview: dict[str, str] = {}
    for edit in proposed_edits.get("file_edits") or []:
        rel_path = str(edit.get("relative_path") or "")
        content = _edit_new_content(edit)
        if rel_path and content is not None:
            normalized_content = _normalize_component_content(rel_path, content)
            preview[rel_path] = normalized_content
            if rel_path.endswith(".py"):
                errors.extend(_validate_python_check_ast(normalized_content, rel_path))
            else:
                try:
                    errors.extend(validate_component_file(rel_path, normalized_content, preview))
                except (RuntimeError, ValueError, TypeError) as exc:
                    errors.append(str(exc))
    return errors


def _validate_python_check_ast(content: str, rel_path: str) -> list[str]:
    errors: list[str] = []
    try:
        module = ast.parse(content)
    except SyntaxError as exc:
        return [f"{rel_path} is not valid Python: {exc}"]
    found = False
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            found = True
            args = [arg.arg for arg in node.args.args]
            if args != ["candidate", "state"]:
                errors.append(f"{rel_path} check function must be check(candidate, state)")
    if not found:
        errors.append(f"{rel_path} must define check(candidate, state)")
    errors.extend(_validate_self_reflection_no_hardcoding(content, rel_path))
    return errors


def _validate_self_reflection_no_hardcoding(content: str, rel_path: str) -> list[str]:
    lowered = content.lower()
    forbidden_markers = (
        "hidden_requirement",
        "hidden requirement",
        "implicit_requirement",
        "ground_truth",
        "answer_key",
        "oracle",
        "test_set",
        "test set",
        "task_id",
        "scenario_id",
        "expected answer",
        "final requirement answer",
    )
    return [
        f"{rel_path} must not depend on hidden data, test data, task id, scenario id, or expected answers"
    ] if any(marker in lowered for marker in forbidden_markers) else []
