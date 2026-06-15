from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml


REFLECTION_APPLIES_WHEN = {
    "always",
    "early_turn",
    "late_turn",
    "has_history",
    "no_history",
    "candidate_is_question",
    "candidate_is_finish",
}


def validate_registry_applies_when(condition: Any) -> str | None:
    """Return an error message when applies_when is invalid for registry validation."""
    if condition in (None, "", "always"):
        return None
    if not isinstance(condition, str):
        return f"unsupported applies_when type {type(condition).__name__}"
    cond = condition.strip()
    if cond not in REFLECTION_APPLIES_WHEN:
        return f"unsupported applies_when {cond!r}"
    return None


ALLOWED_ARTIFACT_TYPES = {
    "system_prompt_section_v1",
    "skill_markdown_v1",
    "reflection_check_bundle_v1",
}

REFLECTION_REGISTRY_KEYS = {"version", "checks"}
REFLECTION_REGISTRY_KEYS_ERROR = "self_reflection/registry.yaml supports only version and checks"

SYSTEM_PROMPT_SECTIONS = (
    "Role",
    "Goal",
    "Interaction Rules",
    "Output Format",
    "Safety Boundaries",
)

SKILL_MINIMAL_REQUIRED_KEYS = frozenset(
    {"id", "name", "version", "enabled", "intent", "scope", "use_when", "avoid_when", "risk_notes"}
)

SKILL_SECTIONS = (
    "Purpose",
    "When to Use",
    "Procedure",
    "Question Pattern",
    "Stop Condition",
    "Anti-patterns",
    "Expected Effect",
)

MEMORY_MAX_FILE_CHARS = 8000
REFLECTION_HOOKS = {"question_candidate", "finish_candidate"}
REFLECTION_MODES = {"observe", "warn", "enforce"}
REFLECTION_PROMPT_MAX_CHARS = 2000
REFLECTION_PROMPT_LEAKAGE_PATTERNS = [
    re.compile(r"\bhidden\s+requirement\b", flags=re.IGNORECASE),
    re.compile(r"\bimplicit\s+requirement\b", flags=re.IGNORECASE),
    re.compile(r"\bground\s+truth\b", flags=re.IGNORECASE),
    re.compile(r"\boracle\s+prompt\b", flags=re.IGNORECASE),
    re.compile(r"\bjudge\s+prompt\b", flags=re.IGNORECASE),
    re.compile(r"\banswer\s+key\b", flags=re.IGNORECASE),
    re.compile(r"\bfinal\s+requirement\b", flags=re.IGNORECASE),
    re.compile(r"\bgold\b", flags=re.IGNORECASE),
    re.compile(r"隐藏需求", flags=re.IGNORECASE),
    re.compile(r"真实答案", flags=re.IGNORECASE),
    re.compile(r"标准答案", flags=re.IGNORECASE),
    re.compile(r"最终需求", flags=re.IGNORECASE),
    re.compile(r"评测器提示词", flags=re.IGNORECASE),
    re.compile(r"\boracle\b", flags=re.IGNORECASE),
    re.compile(r"\bjudge\b", flags=re.IGNORECASE),
]


def _validate_system_prompt_strict(content: str, rel_path: str = "system_prompt.md") -> None:
    """Validate system_prompt.md section structure and reject unsupported headings."""
    headings = _section_headings(content)
    if not headings:
        raise RuntimeError(f"{rel_path} must use the required system_prompt sections")
    invalid = [heading for heading in headings if heading not in SYSTEM_PROMPT_SECTIONS]
    if invalid:
        raise RuntimeError(f"{rel_path} contains unsupported system_prompt sections: {invalid}")
    duplicates = sorted({heading for heading in headings if headings.count(heading) > 1})
    if duplicates:
        raise RuntimeError(f"{rel_path} contains duplicate system_prompt sections: {duplicates}")


def _validate_non_empty_str_list(value: Any, field_name: str, rel_path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{rel_path} frontmatter {field_name} must be a non-empty list[str]"]
    errors: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{rel_path} frontmatter {field_name}[{idx}] must be a non-empty string")
    return errors


def validate_skill_minimal_frontmatter(rel_path: str, content: str) -> list[str]:
    """Validate minimal skill front matter structure only; return error messages."""
    path = Path(rel_path)
    if path.name != "SKILL.md" or len(path.parts) != 3 or path.parts[0] != "skills":
        return [f"{rel_path} must be written as skills/<skill-id>/SKILL.md"]
    folder_id = path.parent.name
    errors: list[str] = []
    if not content.startswith("---\n"):
        return [f"{rel_path} must start with YAML frontmatter"]
    marker = "\n---\n"
    end = content.find(marker, 4)
    if end == -1:
        return [f"{rel_path} frontmatter is not closed"]
    frontmatter_text = content[4:end]
    body = content[end + len(marker) :]
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        return [f"{rel_path} frontmatter YAML parse failed: {exc}"]
    if not isinstance(metadata, dict):
        return [f"{rel_path} frontmatter must be a YAML object"]
    missing = sorted(key for key in SKILL_MINIMAL_REQUIRED_KEYS if key not in metadata)
    if missing:
        errors.append(f"{rel_path} frontmatter missing keys: {missing}")
    skill_id = metadata.get("id")
    if not isinstance(skill_id, str) or not skill_id.strip():
        errors.append(f"{rel_path} frontmatter id must be a non-empty string")
    elif skill_id != folder_id:
        errors.append(f"{rel_path} frontmatter id must equal folder name {folder_id!r}")
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{rel_path} frontmatter name must be a non-empty string")
    version = metadata.get("version")
    if not isinstance(version, int):
        errors.append(f"{rel_path} frontmatter version must be an integer")
    enabled = metadata.get("enabled")
    if not isinstance(enabled, bool):
        errors.append(f"{rel_path} frontmatter enabled must be a boolean")
    intent = metadata.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        errors.append(f"{rel_path} frontmatter intent must be a non-empty string")
    for field in ("scope", "use_when", "avoid_when", "risk_notes"):
        errors.extend(_validate_non_empty_str_list(metadata.get(field), field, rel_path))
    if not body.strip():
        errors.append(f"{rel_path} body must be non-empty after frontmatter")
    if "```python" in body.lower():
        errors.append(f"{rel_path} must not contain Python code")
    return errors


def _validate_skill_markdown_strict(content: str, rel_path: str) -> None:
    """Validate a skills/<skill-id>/SKILL.md artifact against minimal schema rules."""
    errors = validate_skill_minimal_frontmatter(rel_path, content)
    if errors:
        raise RuntimeError(errors[0])


def _validate_memory_markdown_strict(content: str, rel_path: str) -> None:
    """Light validation for memory/<type_slug>/MEMORY.md hit-content records."""
    path = Path(rel_path)
    if path.name != "MEMORY.md" or len(path.parts) != 3 or path.parts[0] != "memory":
        raise RuntimeError(f"{rel_path} must be written as memory/<scenario-type>/MEMORY.md")
    type_slug = path.parent.name
    if not re.fullmatch(r"[a-z0-9_-]+", type_slug):
        raise RuntimeError(f"{rel_path} uses an unsafe scenario type slug")
    if len(content) > MEMORY_MAX_FILE_CHARS:
        raise RuntimeError(f"{rel_path} exceeds max memory file length")
    if content.startswith("---\n"):
        raise RuntimeError(f"{rel_path} must not use YAML frontmatter")
    if re.search(r"\bscenario_id\s*->", content, flags=re.IGNORECASE):
        raise RuntimeError(f"{rel_path} must not store scenario_id -> answer mappings")
    _validate_memory_not_skill_like(content, rel_path)
    leakage = _leakage_errors(rel_path, content)
    if leakage:
        raise RuntimeError(leakage[0])


def _validate_memory_not_skill_like(body: str, rel_path: str) -> None:
    """Reject memory artifacts that read like operational skill procedures."""
    skill_like_headings = re.findall(
        r"(?im)^#\s+(Step\s+\d+|Procedure|Question Pattern|Generalized Lesson|When to Recall|How to Use)\s*$",
        body,
    )
    if skill_like_headings:
        raise RuntimeError(
            f"{rel_path} must not contain lesson or skill-like headings: {sorted(set(skill_like_headings))}"
        )
    strategy_markers = ("interviewer should", "next time", "follow up", "probe earlier", "ask earlier")
    lower = body.lower()
    for marker in strategy_markers:
        if marker in lower:
            raise RuntimeError(f"{rel_path} must not contain strategy language: {marker}")


def _reflection_bundle_id_from_check_path(rel_path: str) -> str:
    path = Path(rel_path)
    if path.parts[0] != "self_reflection" or len(path.parts) != 3 or path.parts[2] != "check.py":
        raise RuntimeError(f"{rel_path} must be written as self_reflection/<reflection-id>/check.py")
    return path.parts[1]


def _is_valid_reflection_check_path(rel_path: str) -> bool:
    path = Path(rel_path)
    return (
        path.parts[0] == "self_reflection"
        and len(path.parts) == 3
        and path.parts[2] == "check.py"
        and bool(path.parts[1])
    )


def _is_valid_reflection_prompt_path(rel_path: str) -> bool:
    path = Path(rel_path)
    return (
        path.parts[0] == "self_reflection"
        and len(path.parts) == 3
        and path.parts[2] == "PROMPT.md"
        and bool(path.parts[1])
    )


def _is_invalid_reflection_bundle_path(rel_path: str) -> bool:
    path = Path(rel_path)
    if path.parts[0] != "self_reflection":
        return False
    if path.name == "README.md" or path.name == "registry.yaml":
        return False
    if len(path.parts) == 2 and path.suffix.lower() == ".py":
        return True
    if len(path.parts) == 3 and path.parts[2] != "check.py" and path.parts[2] != "PROMPT.md":
        return True
    return False


def validate_reflection_python_check(content: str, rel_path: str) -> dict[str, str]:
    if not _is_valid_reflection_check_path(rel_path):
        raise RuntimeError(f"{rel_path} must be written as self_reflection/<reflection-id>/check.py")
    try:
        module = ast.parse(content)
    except SyntaxError as exc:
        raise RuntimeError(f"{rel_path} is not valid Python: {exc}") from exc
    metadata = _parse_docstring_metadata(ast.get_docstring(module) or "", rel_path)
    bundle_id = _reflection_bundle_id_from_check_path(rel_path)
    if metadata.get("component") != "self_reflection":
        raise RuntimeError(f"{rel_path} metadata component must be self_reflection")
    if metadata.get("reflection_id") != bundle_id:
        raise RuntimeError(f"{rel_path} metadata reflection_id must equal bundle folder name {bundle_id}")
    for key in ("name", "version", "hook", "mode"):
        if not metadata.get(key):
            raise RuntimeError(f"{rel_path} metadata missing {key}")
    if metadata["hook"] not in REFLECTION_HOOKS:
        raise RuntimeError(f"{rel_path} metadata hook must be one of {sorted(REFLECTION_HOOKS)}")
    if metadata["mode"] not in REFLECTION_MODES:
        raise RuntimeError(f"{rel_path} metadata mode must be one of {sorted(REFLECTION_MODES)}")
    _validate_check_function(module, rel_path)
    _validate_python_check_sandbox(module, rel_path)
    return metadata


def validate_reflection_prompt_md(content: str, rel_path: str) -> None:
    if not _is_valid_reflection_prompt_path(rel_path):
        raise RuntimeError(f"{rel_path} must be written as self_reflection/<reflection-id>/PROMPT.md")
    if content.startswith("---\n"):
        raise RuntimeError(f"{rel_path} must not use YAML frontmatter")
    if len(content) > REFLECTION_PROMPT_MAX_CHARS:
        raise RuntimeError(f"{rel_path} exceeds max prompt length {REFLECTION_PROMPT_MAX_CHARS}")
    for pattern in REFLECTION_PROMPT_LEAKAGE_PATTERNS:
        if pattern.search(content):
            raise RuntimeError(f"{rel_path} appears to contain leakage-related wording")


def _validate_reflection_registry_workspace_strict(workspace_dir: str | Path, staged_files: dict[str, str] | None = None) -> None:
    """Validate self_reflection/registry.yaml against files present in the workspace."""
    workspace = Path(workspace_dir)
    staged = {Path(path).as_posix(): content for path, content in (staged_files or {}).items()}
    registry = load_reflection_registry(workspace, staged)
    _reject_unknown_reflection_registry_keys(registry)
    checks = registry.get("checks", [])
    if not isinstance(checks, list):
        raise RuntimeError("self_reflection/registry.yaml checks must be a list")
    seen_ids: set[str] = set()
    for item in checks:
        if not isinstance(item, dict):
            raise RuntimeError("self_reflection/registry.yaml checks entries must be objects")
        check_id = _registry_id(item)
        rel_path = _registry_file_path(item, expected_suffix="/check.py")
        prompt_path = _registry_prompt_path(item)
        _require_unique_registry_id(check_id, seen_ids)
        if check_id != Path(rel_path).parts[1]:
            raise RuntimeError(f"registry check {check_id} id must equal bundle folder name")
        if item.get("hook") not in REFLECTION_HOOKS:
            raise RuntimeError(f"registry check {check_id} has invalid hook")
        if item.get("mode") not in REFLECTION_MODES:
            raise RuntimeError(f"registry check {check_id} has invalid mode")
        applies_error = validate_registry_applies_when(item.get("applies_when", "always"))
        if applies_error:
            raise RuntimeError(f"registry check {check_id}: {applies_error}")
        metadata = validate_reflection_python_check(_content_for(workspace, rel_path, staged), rel_path)
        validate_reflection_prompt_md(_content_for(workspace, prompt_path, staged), prompt_path)
        if metadata.get("reflection_id") != check_id:
            raise RuntimeError(f"registry check {check_id} does not match {rel_path} metadata")
        if metadata.get("hook") != item.get("hook"):
            raise RuntimeError(f"registry check {check_id} hook does not match {rel_path} metadata")
        if metadata.get("mode") != item.get("mode"):
            raise RuntimeError(f"registry check {check_id} mode does not match {rel_path} metadata")


def load_reflection_registry(workspace_dir: str | Path, staged_files: dict[str, str] | None = None) -> dict[str, Any]:
    workspace = Path(workspace_dir)
    staged = {Path(path).as_posix(): content for path, content in (staged_files or {}).items()}
    rel_path = "self_reflection/registry.yaml"
    if rel_path in staged:
        raw = staged[rel_path]
    else:
        path = workspace / rel_path
        if not path.exists():
            return {"version": "0.2", "checks": []}
        raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise RuntimeError("self_reflection/registry.yaml must be a YAML object")
    _reject_unknown_reflection_registry_keys(data)
    data.setdefault("checks", [])
    return data


def _reject_unknown_reflection_registry_keys(registry: dict[str, Any]) -> None:
    unknown = set(registry.keys()) - REFLECTION_REGISTRY_KEYS
    if unknown:
        raise RuntimeError(REFLECTION_REGISTRY_KEYS_ERROR)


def parse_markdown_frontmatter(content: str, rel_path: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise RuntimeError(f"{rel_path} must start with YAML frontmatter")
    marker = "\n---\n"
    end = content.find(marker, 4)
    if end == -1:
        raise RuntimeError(f"{rel_path} frontmatter is not closed")
    frontmatter_text = content[4:end]
    body = content[end + len(marker) :]
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{rel_path} frontmatter YAML parse failed: {exc}") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{rel_path} frontmatter must be a YAML object")
    return metadata, body


def markdown_sections(content: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.splitlines():
        if line.startswith("# "):
            current = line[2:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _known_paths(workspace: Path, staged: dict[str, str]) -> set[str]:
    paths = (
        {
            rel_path
            for path in workspace.rglob("*")
            if path.is_file()
            if (rel_path := path.relative_to(workspace).as_posix())
            if _is_workspace_text_candidate(rel_path)
        }
        if workspace.exists()
        else set()
    )
    paths.update(staged)
    return paths


def _is_workspace_text_candidate(rel_path: str) -> bool:
    path = Path(rel_path)
    if ".git" in path.parts:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    if rel_path in {"code_agent.yaml", "system_prompt.md"}:
        return True
    if rel_path == "self_reflection/registry.yaml":
        return True
    return path.suffix.lower() in {".md", ".py", ".yaml", ".yml", ".json", ".txt"}


def _content_for(workspace: Path, rel_path: str, staged: dict[str, str]) -> str:
    rel_path = Path(rel_path).as_posix()
    if rel_path in staged:
        return staged[rel_path]
    path = workspace / rel_path
    if not path.exists():
        raise RuntimeError(f"referenced harness file does not exist: {rel_path}")
    return path.read_text(encoding="utf-8")


def _section_headings(content: str) -> list[str]:
    return [line[2:].strip() for line in content.splitlines() if line.startswith("# ")]


def _require_sections(body: str, rel_path: str, required: tuple[str, ...]) -> None:
    headings = _section_headings(body)
    missing = [section for section in required if section not in headings]
    if missing:
        raise RuntimeError(f"{rel_path} missing required sections: {missing}")


def _require_metadata(
    metadata: dict[str, Any],
    rel_path: str,
    expected: dict[str, Any],
    *,
    required_keys: set[str],
) -> None:
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise RuntimeError(f"{rel_path} frontmatter {key} must be {value!r}")
    missing = sorted(key for key in required_keys if key not in metadata)
    if missing:
        raise RuntimeError(f"{rel_path} frontmatter missing keys: {missing}")


def _require_nested_list(metadata: dict[str, Any], rel_path: str, parent: str, child: str) -> None:
    parent_value = metadata.get(parent)
    if not isinstance(parent_value, dict) or not isinstance(parent_value.get(child), list):
        raise RuntimeError(f"{rel_path} frontmatter {parent}.{child} must be a list")


def _parse_docstring_metadata(docstring: str, rel_path: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in docstring.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            metadata[key] = value
    if not metadata:
        raise RuntimeError(f"{rel_path} must contain component metadata in the module docstring")
    return metadata


def _validate_check_function(module: ast.Module, rel_path: str) -> None:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            args = [arg.arg for arg in node.args.args]
            if args[:2] != ["candidate", "state"] or len(args) != 2:
                raise RuntimeError(f"{rel_path} check function must be check(candidate, state)")
            return
    raise RuntimeError(f"{rel_path} must define check(candidate, state)")


def _validate_python_check_sandbox(module: ast.Module, rel_path: str) -> None:
    forbidden_import_roots = {"os", "pathlib", "requests", "httpx", "urllib", "socket", "subprocess", "reqahe"}
    forbidden_calls = {"open", "exec", "eval", "__import__"}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in forbidden_import_roots:
                    raise RuntimeError(f"{rel_path} imports forbidden module {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in forbidden_import_roots:
                raise RuntimeError(f"{rel_path} imports forbidden module {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
            raise RuntimeError(f"{rel_path} calls forbidden function {node.func.id}")


def _registry_id(item: dict[str, Any]) -> str:
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        raise RuntimeError("self_reflection/registry.yaml entries require id")
    return item_id


def _registry_file_path(item: dict[str, Any], *, expected_suffix: str) -> str:
    raw_file = str(item.get("file") or "").strip()
    if not raw_file:
        raise RuntimeError("self_reflection/registry.yaml entries require file")
    rel_path = (Path("self_reflection") / raw_file).as_posix()
    if Path(raw_file).is_absolute() or any(part == ".." for part in Path(raw_file).parts):
        raise RuntimeError(f"registry file path is unsafe: {raw_file}")
    if not rel_path.endswith(expected_suffix):
        raise RuntimeError(f"registry file {raw_file} must end with {expected_suffix}")
    if not _is_valid_reflection_check_path(rel_path):
        raise RuntimeError(f"registry file {raw_file} must be <reflection-id>/check.py")
    return rel_path


def _registry_prompt_path(item: dict[str, Any]) -> str:
    check_id = _registry_id(item)
    raw_prompt = str(item.get("prompt") or "").strip()
    if not raw_prompt:
        raise RuntimeError(f"registry check {check_id} requires prompt")
    rel_path = (Path("self_reflection") / raw_prompt).as_posix()
    if Path(raw_prompt).is_absolute() or any(part == ".." for part in Path(raw_prompt).parts):
        raise RuntimeError(f"registry prompt path is unsafe: {raw_prompt}")
    if not _is_valid_reflection_prompt_path(rel_path):
        raise RuntimeError(f"registry prompt {raw_prompt} must be <reflection-id>/PROMPT.md")
    expected = f"{check_id}/PROMPT.md"
    if raw_prompt != expected:
        raise RuntimeError(f"registry check {check_id} prompt must be {expected}")
    return rel_path


def _require_unique_registry_id(item_id: str, seen_ids: set[str]) -> None:
    if item_id in seen_ids:
        raise RuntimeError(f"duplicate self_reflection registry id: {item_id}")
    seen_ids.add(item_id)


def validate_workspace_component_schemas(workspace_dir: str | Path, staged_files: dict[str, str] | None = None) -> None:
    """Validate workspace components on disk plus optional staged in-memory edits."""
    workspace = Path(workspace_dir)
    staged = {Path(path).as_posix(): content for path, content in (staged_files or {}).items()}
    preview: dict[str, str] = {}
    for rel_path in _known_paths(workspace, staged):
        preview[rel_path] = _content_for(workspace, rel_path, staged)
    errors = validate_workspace_preview(preview)
    if errors:
        raise RuntimeError("; ".join(errors))


def _is_schema_managed_path(rel_path: str) -> bool:
    path = Path(rel_path)
    if rel_path == "system_prompt.md":
        return True
    if path.name == "README.md":
        return False
    return rel_path.startswith(("skills/", "memory/", "self_reflection/")) and path.suffix in {".md", ".py"}


_raise_validate_system_prompt = _validate_system_prompt_strict
_raise_validate_skill_markdown = _validate_skill_markdown_strict
_raise_validate_memory_markdown = _validate_memory_markdown_strict


def validate_component_file(
    relative_path: str,
    content: str,
    workspace_files: dict[str, str] | None = None,
) -> list[str]:
    rel_path = Path(relative_path).as_posix()
    workspace = {Path(path).as_posix(): value for path, value in (workspace_files or {}).items()}
    errors: list[str] = []
    if rel_path == "system_prompt.md":
        errors.extend(validate_system_prompt(rel_path, content))
    elif rel_path.startswith("skills/") and rel_path.endswith(".md") and Path(rel_path).name != "README.md":
        errors.extend(validate_skill_markdown(rel_path, content))
    elif rel_path.startswith("memory/") and rel_path.endswith("MEMORY.md"):
        errors.extend(validate_memory_markdown(rel_path, content))
    elif rel_path.startswith("self_reflection/"):
        name = Path(rel_path).name
        if name == "README.md":
            return errors
        if name == "registry.yaml":
            errors.extend(validate_reflection_registry(rel_path, content, workspace))
        elif _is_invalid_reflection_bundle_path(rel_path):
            errors.append(f"{rel_path} is not a valid self_reflection bundle path")
        elif rel_path.endswith("/check.py"):
            errors.extend(validate_reflection_python(rel_path, content))
        elif rel_path.endswith("/PROMPT.md"):
            errors.extend(_errors_from(validate_reflection_prompt_md, content, rel_path))
        elif rel_path.endswith(".py"):
            errors.append(f"{rel_path} self_reflection root .py files are not allowed")
        elif rel_path.endswith(".md"):
            errors.append(f"{rel_path} self_reflection only supports bundle check.py and PROMPT.md files")
    if _is_schema_managed_path(rel_path):
        errors.extend(_leakage_errors(rel_path, content))
    return errors


def validate_workspace_preview(preview_files: dict[str, str]) -> list[str]:
    preview = {Path(path).as_posix(): content for path, content in preview_files.items()}
    errors: list[str] = []
    for rel_path, content in sorted(preview.items()):
        errors.extend(validate_component_file(rel_path, content, preview))
    errors.extend(_validate_reflection_registration_in_preview(preview))
    return errors


def validate_schema_compliance_block(data: dict[str, Any], edited_paths: list[str]) -> None:
    """Ensure schema_compliance references only edited files and allowed artifact types."""
    compliance = data.get("schema_compliance")
    if compliance is None:
        return
    items = [compliance] if isinstance(compliance, dict) else compliance
    if not isinstance(items, list):
        raise RuntimeError("harness refinement generation failed: schema_compliance must be a list")
    normalized_edits = {Path(path).as_posix() for path in edited_paths}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("harness refinement generation failed: invalid schema_compliance block")
        component = str(item.get("component") or "").strip()
        schema_name = str(item.get("schema_name") or "").strip()
        files = item.get("new_or_updated_files")
        if not component or schema_name not in ALLOWED_ARTIFACT_TYPES or not isinstance(files, list):
            raise RuntimeError("harness refinement generation failed: invalid schema_compliance block")
        normalized_files = {Path(str(path)).as_posix() for path in files}
        if not normalized_files.issubset(normalized_edits):
            raise RuntimeError("harness refinement generation failed: schema_compliance references files not edited")


def validate_system_prompt(path: str, content: str | None = None) -> list[str]:
    rel_path, text = _coerce_path_content(path, content, "system_prompt.md")
    return _errors_from(_raise_validate_system_prompt, text, rel_path)


def validate_skill_markdown(path: str, content: str | None = None) -> list[str]:
    rel_path, text = _coerce_path_content(path, content, "skills/generated.md")
    return _errors_from(_raise_validate_skill_markdown, text, rel_path)


def validate_memory_markdown(path: str, content: str | None = None) -> list[str]:
    rel_path, text = _coerce_path_content(path, content, "memory/example_type/MEMORY.md")
    return _errors_from(_raise_validate_memory_markdown, text, rel_path)


def validate_reflection_python(path: str, content: str | None = None) -> list[str]:
    rel_path, text = _coerce_path_content(path, content, "self_reflection/generated_check/check.py")
    errors = _errors_from(validate_reflection_python_check, text, rel_path)
    if errors:
        return errors
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel_path} is not valid Python: {exc}"]
    errors.extend(_validate_check_annotations(module, rel_path))
    return errors


def validate_reflection_registry(
    path: str,
    content: str | None = None,
    workspace_files: dict[str, str] | None = None,
) -> list[str]:
    rel_path, text = _coerce_path_content(path, content, "self_reflection/registry.yaml")
    workspace = {Path(item_path).as_posix(): value for item_path, value in (workspace_files or {}).items()}
    errors: list[str] = []
    try:
        registry = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return [f"{rel_path} is not valid YAML: {exc}"]
    if not isinstance(registry, dict):
        return [f"{rel_path} must be a YAML object"]
    try:
        _reject_unknown_reflection_registry_keys(registry)
    except RuntimeError as exc:
        return [str(exc)]
    checks = registry.get("checks", [])
    if not isinstance(checks, list):
        errors.append("self_reflection/registry.yaml checks must be a list")
        return errors
    seen_ids: set[str] = set()
    for item in checks:
        errors.extend(_validate_registry_item(item, seen_ids, workspace, is_check=True))
    return errors


def _coerce_path_content(path: str, content: str | None, default_path: str) -> tuple[str, str]:
    if content is None:
        return default_path, str(path)
    if "\n" in str(path) and "\n" not in str(content) and Path(str(content)).suffix:
        return Path(str(content)).as_posix(), str(path)
    return Path(str(path)).as_posix(), str(content)


def _errors_from(func: Any, *args: Any) -> list[str]:
    try:
        func(*args)
    except RuntimeError as exc:
        return [str(exc)]
    return []


def _validate_check_annotations(module: ast.Module, rel_path: str) -> list[str]:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            args = node.args.args
            if len(args) != 2:
                return []
            missing = [arg.arg for arg in args if arg.annotation is None]
            if node.returns is None:
                missing.append("return")
            if missing:
                return [f"{rel_path} check function must include type annotations for {missing}"]
            return []
    return []


def _validate_registry_item(
    item: Any,
    seen_ids: set[str],
    workspace_files: dict[str, str],
    *,
    is_check: bool,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return ["self_reflection/registry.yaml entries must be objects"]
    try:
        item_id = _registry_id(item)
        _require_unique_registry_id(item_id, seen_ids)
        raw_file = str(item.get("file") or "").strip()
        file_bundle_id = Path(raw_file).parts[0] if raw_file else ""
        if file_bundle_id and item_id != file_bundle_id:
            errors.append(f"registry check {item_id} id must equal bundle folder name")
            return errors
        rel_path = _registry_file_path(item, expected_suffix="/check.py")
        prompt_path = _registry_prompt_path(item)
    except RuntimeError as exc:
        return [str(exc)]
    hook_key = "hook" if is_check else "inject_at"
    if item.get(hook_key) not in REFLECTION_HOOKS:
        errors.append(f"registry item {item_id} has invalid {hook_key}")
    if is_check and item.get("mode") not in REFLECTION_MODES:
        errors.append(f"registry check {item_id} has invalid mode")
    applies_error = validate_registry_applies_when(item.get("applies_when", "always"))
    if applies_error:
        errors.append(f"registry check {item_id}: {applies_error}")
    if rel_path not in workspace_files:
        errors.append(f"referenced harness file does not exist: {rel_path}")
        return errors
    if prompt_path not in workspace_files:
        errors.append(f"referenced harness file does not exist: {prompt_path}")
        return errors
    if is_check:
        errors.extend(validate_reflection_python(rel_path, workspace_files[rel_path]))
        errors.extend(_errors_from(validate_reflection_prompt_md, workspace_files[prompt_path], prompt_path))
    return errors


def _validate_reflection_registration_in_preview(preview_files: dict[str, str]) -> list[str]:
    reflection_checks = {
        path
        for path in preview_files
        if _is_valid_reflection_check_path(path)
    }
    reflection_prompts = {
        path
        for path in preview_files
        if _is_valid_reflection_prompt_path(path)
    }
    if not reflection_checks and not reflection_prompts:
        invalid_paths = {
            path
            for path in preview_files
            if path.startswith("self_reflection/")
            and Path(path).name not in {"README.md", "registry.yaml"}
            and _is_invalid_reflection_bundle_path(path)
        }
        if invalid_paths:
            return [f"invalid self_reflection paths are not allowed: {sorted(invalid_paths)}"]
        return []
    for check_path in sorted(reflection_checks):
        bundle_id = Path(check_path).parts[1]
        prompt_path = f"self_reflection/{bundle_id}/PROMPT.md"
        if prompt_path not in preview_files:
            return [f"self_reflection bundle {bundle_id} must include PROMPT.md"]
    for prompt_path in sorted(reflection_prompts):
        bundle_id = Path(prompt_path).parts[1]
        check_path = f"self_reflection/{bundle_id}/check.py"
        if check_path not in preview_files:
            return [f"self_reflection bundle {bundle_id} must include check.py"]
    registry_content = preview_files.get("self_reflection/registry.yaml")
    if registry_content is None:
        return ["self_reflection files must be registered in self_reflection/registry.yaml"]
    errors = validate_reflection_registry("self_reflection/registry.yaml", registry_content, preview_files)
    if errors:
        return errors
    registry = yaml.safe_load(registry_content) or {}
    registered: set[str] = set()
    for item in registry.get("checks", []) or []:
        if isinstance(item, dict) and item.get("file"):
            registered.add((Path("self_reflection") / str(item["file"])).as_posix())
    missing = sorted(reflection_checks - registered)
    if missing:
        return [f"self_reflection files must be registered in self_reflection/registry.yaml: {missing}"]
    return []


def _leakage_errors(rel_path: str, content: str) -> list[str]:
    patterns = [
        re.compile(r"\bscenario_id\s*->", flags=re.IGNORECASE),
        re.compile(r"\bhidden\s+requirement\s+(id|answer|content)\b", flags=re.IGNORECASE),
        re.compile(r"\bfinal\s+requirement\s+answer\b", flags=re.IGNORECASE),
    ]
    for pattern in patterns:
        if pattern.search(content):
            return [f"{rel_path} appears to contain hidden requirement or scenario-specific leakage"]
    return []
