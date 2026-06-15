from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from reqahe.infra.paths import repo_root

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


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_direct_network_env() -> None:
    """Prefer local direct network for domestic LLM endpoints instead of VPN/system proxy."""
    if parse_bool(os.getenv("OPENAI_TRUST_ENV"), default=False):
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    no_proxy = os.getenv("OPENAI_NO_PROXY", "open.bigmodel.cn,localhost,127.0.0.1,<local>")
    existing = os.getenv("NO_PROXY", "")
    merged = ",".join(part for part in [existing, no_proxy] if part)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    root = repo_root()
    load_dotenv(root / ".env")
    apply_direct_network_env()
    path = Path(config_path) if config_path else root / "configs" / "default.yaml"
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data = _resolve_env(data)
    data.setdefault("paths", {})
    data["paths"].setdefault("project_root", str(root))
    data["paths"].setdefault("reqelicitgym_root", "ReqElicitGym")
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
    if getattr(args, "scenario_count", None) is not None:
        eval_cfg["scenario_count"] = args.scenario_count
    if getattr(args, "iterations", None) is not None:
        evo_cfg["iterations"] = args.iterations
    if getattr(args, "batch_size", None) is not None:
        evo_cfg["batch_size"] = args.batch_size
    if getattr(args, "reflection_mode", None):
        evo_cfg["reflection_mode"] = args.reflection_mode
    runtime_cfg = config.setdefault("runtime", {})
    cleanup_cfg = runtime_cfg.setdefault("close_wait_cleanup", {})
    if getattr(args, "disable_close_wait_cleanup", False):
        cleanup_cfg["enabled"] = False
    if getattr(args, "close_wait_cleanup_interval_tasks", None) is not None:
        cleanup_cfg["interval_tasks"] = args.close_wait_cleanup_interval_tasks
    if getattr(args, "close_wait_cleanup_interval_seconds", None) is not None:
        cleanup_cfg["interval_seconds"] = args.close_wait_cleanup_interval_seconds
    router_cfg = runtime_cfg.setdefault("skill_router", {})
    if getattr(args, "disable_skill_router", False):
        router_cfg["enabled"] = False
    if getattr(args, "max_selected_skills", None) is not None:
        router_cfg["max_selected_skills"] = args.max_selected_skills
    if getattr(args, "skill_router_min_relevance", None) is not None:
        router_cfg["min_relevance"] = args.skill_router_min_relevance
    if getattr(args, "skill_router_model", None):
        config.setdefault("roles", {})["skill_router_model"] = args.skill_router_model
    memory_router_cfg = runtime_cfg.setdefault("memory_router", {})
    if getattr(args, "disable_memory_router", False):
        memory_router_cfg["enabled"] = False
    if getattr(args, "max_selected_memory_types", None) is not None:
        value = int(args.max_selected_memory_types)
        if value > 1:
            raise ValueError("memory router supports at most one memory type; --max-selected-memory-types must be 1")
        memory_router_cfg["max_selected_types"] = value
    if getattr(args, "memory_router_min_relevance", None) is not None:
        memory_router_cfg["min_confidence"] = args.memory_router_min_relevance
    if getattr(args, "memory_router_model", None):
        config.setdefault("roles", {})["memory_router_model"] = args.memory_router_model
    return config


def role_model(config: dict[str, Any], role: str) -> str:
    roles = config.get("roles", {})
    default_model = config.get("llm", {}).get("model") or os.getenv("OPENAI_MODEL") or "unknown-model"
    return roles.get(f"{role}_model") or default_model
