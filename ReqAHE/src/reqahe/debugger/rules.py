from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from reqahe.llm.client import OpenAICompatibleClient
from reqahe.utils.io import ensure_dir, read_json, write_json, write_text


def generate_analysis(
    iteration_dir: str | Path,
    rollout_dir: str | Path,
    llm: OpenAICompatibleClient,
    debugger_model: str,
    previous_metrics: dict | None = None,
) -> Path:
    iteration = Path(iteration_dir)
    rollout = Path(rollout_dir)
    analysis = ensure_dir(iteration / "analysis")
    per_task_dir = ensure_dir(analysis / "per_task")
    task_results = read_json(rollout / "task_results.json")
    metrics = read_json(rollout / "metrics.json")
    traces = []
    observed_failures: Counter[str] = Counter()
    for result in task_results:
        trace = read_json(Path(result["trace_dir"]) / "clean_trace.json")
        observed_failures.update(trace.get("failure_tags", []))
        traces.append(
            {
                "scenario_id": result["scenario_id"],
                "metrics": result["metrics"],
                "failure_tags": trace.get("failure_tags", []),
                "turns": trace.get("turns", []),
            }
        )
    payload = {
        "rollout_metrics": metrics,
        "previous_metrics": previous_metrics,
        "observed_failure_tags": dict(observed_failures),
        "task_traces": traces,
    }
    data = llm.json_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are the debugger in an agentic harness optimization loop for requirements elicitation. "
                    "Analyze the rollout traces and produce actionable diagnostics for the harness evolver. "
                    "Return strict compact JSON with keys: overview_bullets, recommendations, "
                    "failure_patterns, successful_patterns, per_task. "
                    "overview_bullets and recommendations must be arrays of short strings. "
                    "failure_patterns and successful_patterns must be objects mapping labels to integer counts. "
                    "per_task must be a list of objects with scenario_id, findings, and recommended_edit. "
                    "findings must be an array of short strings. Do not include Markdown code fences."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=debugger_model,
        purpose="debugger analysis generation",
    )
    _validate_analysis(data)
    for item in data["per_task"]:
        write_text(per_task_dir / f"{item['scenario_id']}.md", _task_markdown(item))
    write_text(analysis / "overview.md", _bullets_markdown("RE Agent Debugger Overview", data["overview_bullets"]))
    write_text(analysis / "recommendations.md", _bullets_markdown("Recommended Next Harness Edits", data["recommendations"]))
    write_json(analysis / "failure_patterns.json", data["failure_patterns"])
    write_json(analysis / "successful_patterns.json", data["successful_patterns"])
    return analysis


def _validate_analysis(data: dict[str, Any]) -> None:
    required = ["overview_bullets", "recommendations", "failure_patterns", "successful_patterns", "per_task"]
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"debugger analysis generation failed: missing keys {missing}")
    if not isinstance(data["overview_bullets"], list) or not isinstance(data["recommendations"], list):
        raise RuntimeError("debugger analysis generation failed: overview_bullets and recommendations must be lists")
    if not isinstance(data["failure_patterns"], dict) or not isinstance(data["successful_patterns"], dict):
        raise RuntimeError("debugger analysis generation failed: pattern fields must be objects")
    if not isinstance(data["per_task"], list):
        raise RuntimeError("debugger analysis generation failed: per_task must be a list")
    for item in data["per_task"]:
        if not isinstance(item, dict) or not item.get("scenario_id") or not isinstance(item.get("findings"), list):
            raise RuntimeError("debugger analysis generation failed: invalid per_task item")


def _bullets_markdown(title: str, bullets: list[Any]) -> str:
    lines = [f"# {title}", ""]
    lines.extend(f"- {str(item)}" for item in bullets)
    return "\n".join(lines).rstrip() + "\n"


def _task_markdown(item: dict[str, Any]) -> str:
    lines = [f"# {item['scenario_id']}", "", "## Findings"]
    lines.extend(f"- {str(finding)}" for finding in item["findings"])
    lines.extend(["", "## Recommended Edit", str(item.get("recommended_edit") or "")])
    return "\n".join(lines).rstrip() + "\n"
