from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from reqahe.utils.paths import repo_root

ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    if isinstance(value, str):
        match = ENV_PATTERN.match(value)
        if match:
            return os.getenv(match.group(1), "")
    return value


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    root = repo_root()
    load_dotenv(root / ".env")
    path = Path(config_path) if config_path else root / "configs" / "default.yaml"
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data = _resolve_env(data)
    data.setdefault("paths", {})
    data["paths"].setdefault("project_root", str(root))
    return data


def apply_cli_overrides(config: dict[str, Any], args: Any) -> dict[str, Any]:
    llm = config.setdefault("llm", {})
    eval_cfg = config.setdefault("evaluation", {})
    evo_cfg = config.setdefault("evolution", {})

    for attr, key in [("base_url", "base_url"), ("api_key", "api_key"), ("model", "model")]:
        value = getattr(args, attr, None)
        if value:
            llm[key] = value
    if getattr(args, "temperature", None) is not None:
        llm["temperature"] = args.temperature
    if getattr(args, "max_turns", None) is not None:
        eval_cfg["max_turns"] = args.max_turns
    if getattr(args, "rollouts_per_task", None) is not None:
        eval_cfg["rollouts_per_task"] = args.rollouts_per_task
    if getattr(args, "task_mode", None):
        eval_cfg["task_mode"] = args.task_mode
    if getattr(args, "dataset_file", None):
        eval_cfg["dataset_file"] = args.dataset_file
    dataset_number = getattr(args, "dataset_number", None)
    if dataset_number is not None:
        if isinstance(dataset_number, str) and dataset_number.lower() in {"", "base", "none", "null"}:
            eval_cfg["dataset_number"] = None
        else:
            eval_cfg["dataset_number"] = int(dataset_number)
    if getattr(args, "split", None):
        eval_cfg["split"] = args.split
    if getattr(args, "iterations", None) is not None:
        evo_cfg["iterations"] = args.iterations
    if getattr(args, "middleware_mode", None):
        evo_cfg["middleware_mode"] = args.middleware_mode
    return config


def role_model(config: dict[str, Any], role: str) -> str:
    roles = config.get("roles", {})
    default_model = config.get("llm", {}).get("model") or os.getenv("OPENAI_MODEL") or "unknown-model"
    return roles.get(f"{role}_model") or default_model
