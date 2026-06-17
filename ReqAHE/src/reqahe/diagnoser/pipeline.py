from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from reqahe.harness.workspace import load_skill_catalog_summary, load_skill_schema_errors_summary
from reqahe.runtime.route_stats import load_route_stats_artifacts
from reqahe.harness.component_spec import load_harness_component_specs
from reqahe.infra.io import ensure_dir, read_json, write_json, write_text
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.utils.paths import resolve_maybe_relative


"""Two-stage elicitation diagnosis: trace problem analysis then component localization."""

PROMPT_DIR = Path(__file__).with_name("prompts")

DEFAULT_COMPONENT_PURPOSES: dict[str, str] = {
    "system_prompt": "global role, task contract, output format, safety boundary",
    "skills": "reusable elicitation procedures and questioning strategies",
    "memory": "scenario-type hit-content record produced only by memorize; do not localize failures to memory",
    "self_reflection": "candidate-level Python runtime checks that inspect generated question or finish candidates before they are sent to ReqElicitGym. It supports only question_candidate and finish_candidate. Warn/enforce checks can trigger same-turn regeneration.",
}

NON_EVOLVABLE_COMPONENTS = frozenset({"memory"})

_SENSITIVE_TRACE_KEYS = frozenset(
    {
        "hidden_requirements",
        "implicit_requirements",
        "oracle_prompt",
        "oracle_system_prompt",
        "evaluator_prompt",
        "judge_prompt",
        "dataset_answer",
        "answer_text",
        "api_key",
        "base_url",
        "model_config",
        "llm_config",
        "workspace_files",
        "raw_trace",
    }
)

_SENSITIVE_JUDGEMENT_KEYS = frozenset(
    {
        "evaluator_prompt",
        "oracle_prompt",
        "hidden_requirement_text",
        "requirement_answer",
    }
)


def run_elicitation_diagnosis(
    iteration_dir: str | Path,
    rollout_dir: str | Path,
    llm: OpenAICompatibleClient,
    diagnoser_model: str,
    previous_metrics: dict | None = None,
    attribution_dir: str | Path | None = None,
    harness_dir: str | Path | None = None,
) -> Path:
    iteration = Path(iteration_dir)
    rollout = Path(rollout_dir)
    harness = Path(harness_dir) if harness_dir else iteration / "workspace"
    analysis = ensure_dir(iteration / "analysis")
    per_task_dir = ensure_dir(analysis / "per_task")
    declared_components = load_declared_components(harness)
    declared_component_names = {item["name"] for item in declared_components}

    metrics = read_json(rollout / "metrics.json") if (rollout / "metrics.json").exists() else {}
    stage1_payload = build_full_trace_problem_payload(rollout, metrics, previous_metrics or {}, harness)
    trace_analysis = llm.json_chat(
        [
            {"role": "system", "content": load_diagnoser_prompt("analyze_trace.md")},
            {"role": "user", "content": json.dumps(stage1_payload, ensure_ascii=False)},
        ],
        model=diagnoser_model,
        purpose="trace problem analysis",
    )
    validate_trace_problem_analysis(trace_analysis)
    write_json(analysis / "trace_problem_analysis.json", trace_analysis)

    stage2_payload = build_component_localization_payload(trace_analysis, declared_components, rollout, harness)
    localization = llm.json_chat(
        [
            {"role": "system", "content": load_diagnoser_prompt("localize_component.md")},
            {"role": "user", "content": json.dumps(stage2_payload, ensure_ascii=False)},
        ],
        model=diagnoser_model,
        purpose="component localization",
    )
    try:
        validate_component_localization(localization, declared_component_names)
    except RuntimeError as exc:
        _write_error_report(analysis, exc, localization, declared_component_names)
        raise

    write_json(analysis / "component_localization.json", localization)
    write_diagnosis_outputs(analysis, per_task_dir, trace_analysis, localization)
    return analysis


def load_declared_components(
    harness_dir: str | Path,
    *,
    include_non_evolvable: bool = False,
) -> list[dict[str, str]]:
    specs = load_harness_component_specs(harness_dir)
    names = sorted(specs)
    if not include_non_evolvable:
        names = [name for name in names if name not in NON_EVOLVABLE_COMPONENTS]
    return [
        {
            "name": name,
            "purpose": DEFAULT_COMPONENT_PURPOSES.get(name, f"harness component: {name}"),
        }
        for name in names
    ]


def build_full_trace_problem_payload(
    rollout: Path,
    rollout_metrics: dict[str, Any],
    previous_metrics: dict[str, Any],
    harness_dir: Path | None = None,
) -> dict[str, Any]:
    complete_task_traces, trace_warnings = load_complete_clean_traces(rollout)
    payload: dict[str, Any] = {
        "rollout_metrics": rollout_metrics,
        "complete_task_traces": complete_task_traces,
        "previous_metric_delta": compute_previous_metric_delta(previous_metrics, rollout_metrics),
    }
    payload.update(load_route_stats_artifacts(rollout))
    if harness_dir is not None:
        payload["skill_catalog_summary"] = load_skill_catalog_summary(harness_dir)
        schema_errors = load_skill_schema_errors_summary(harness_dir)
        if schema_errors:
            payload["skill_schema_errors_summary"] = schema_errors
    digest = _load_skill_evolution_digest(rollout.parent)
    if digest:
        payload["skill_evolution_digest"] = digest
    if trace_warnings:
        payload["trace_resolution_warnings"] = trace_warnings
    return payload


def build_component_localization_payload(
    trace_problem_analysis: dict[str, Any],
    declared_components: list[dict[str, str]],
    rollout: Path | None = None,
    harness_dir: Path | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "diagnosis": trace_problem_analysis,
        "declared_components": declared_components,
    }
    if rollout is not None:
        payload.update(load_route_stats_artifacts(rollout))
    if harness_dir is not None:
        payload["skill_catalog_summary"] = load_skill_catalog_summary(harness_dir)
        schema_errors = load_skill_schema_errors_summary(harness_dir)
        if schema_errors:
            payload["skill_schema_errors_summary"] = schema_errors
    digest = _load_skill_evolution_digest(rollout.parent) if rollout is not None else {}
    if digest:
        payload["skill_evolution_digest"] = digest
    return payload


def load_complete_clean_traces(rollout: Path) -> tuple[list[dict[str, Any]], list[str]]:
    task_results_path = rollout / "task_results.json"
    if not task_results_path.exists():
        return [], []
    task_results = read_json(task_results_path)
    if not isinstance(task_results, list):
        return [], []
    traces: list[dict[str, Any]] = []
    warnings: list[str] = []
    for result in task_results:
        if not isinstance(result, dict):
            continue
        trace_dir = _resolve_trace_dir(rollout, result)
        if trace_dir is None:
            scenario_id = str(result.get("scenario_id") or result.get("task_id") or "").strip() or "unknown"
            warnings.append(
                f"skipped trace for scenario_id={scenario_id}: could not resolve trace_dir "
                f"(stored={result.get('trace_dir')!r})"
            )
            continue
        trace_path = trace_dir / "clean_trace.json"
        if not trace_path.exists():
            scenario_id = str(result.get("scenario_id") or result.get("task_id") or "").strip() or "unknown"
            warnings.append(f"skipped trace for scenario_id={scenario_id}: missing {trace_path.as_posix()}")
            continue
        trace = read_json(trace_path)
        traces.append(sanitize_trace_for_diagnoser(trace, result.get("metrics")))
    return traces, warnings


def _load_skill_evolution_digest(batch_dir: Path) -> dict[str, Any]:
    path = batch_dir / "analysis" / "skill_evolution_digest.json"
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_trace_dir(rollout: Path, result: dict[str, Any]) -> Path | None:
    raw = result.get("trace_dir")
    if raw:
        direct = Path(str(raw))
        if direct.exists():
            return direct

        resolved = resolve_maybe_relative(raw, rollout)
        if resolved.exists():
            return resolved

        resolved = resolve_maybe_relative(raw, rollout.parent)
        if resolved.exists():
            return resolved

    scenario_id = str(result.get("scenario_id") or result.get("task_id") or "").strip()
    repeat_id = result.get("repeat_id")
    rollout_id = result.get("rollout_id")
    run_id = result.get("run_id")

    if scenario_id:
        patterns: list[str] = []
        if repeat_id is not None:
            patterns.append(f"{scenario_id}__r{repeat_id}/clean_trace.json")
        if rollout_id is not None:
            patterns.append(f"{scenario_id}__r{rollout_id}/clean_trace.json")
        if run_id is not None:
            patterns.append(f"{scenario_id}__r{run_id}/clean_trace.json")
        patterns.append(f"{scenario_id}__r*/clean_trace.json")

        for pattern in patterns:
            matches = sorted(rollout.glob(pattern))
            if matches:
                return matches[0].parent
            matches = sorted(rollout.rglob(pattern))
            if matches:
                return matches[0].parent

    return None


def sanitize_trace_for_diagnoser(trace: dict[str, Any], task_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned = _strip_sensitive_keys(deepcopy(trace))
    implicit_ids = _all_implicit_requirement_ids(trace)
    cumulative_hits: list[str] = []
    sanitized_turns: list[dict[str, Any]] = []

    for turn in cleaned.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        judgement = turn.get("judgement") or {}
        if isinstance(judgement, dict):
            judgement = {k: v for k, v in judgement.items() if k not in _SENSITIVE_JUDGEMENT_KEYS}
        newly_hit = _newly_hit_ids(judgement)
        cumulative_hits = sorted(set(cumulative_hits) | set(newly_hit))
        missed_ids = sorted(set(implicit_ids) - set(cumulative_hits))
        sanitized_turns.append(
            {
                "turn_index": turn.get("turn_index"),
                "action": turn.get("action"),
                "question": turn.get("question", ""),
                "user_response": turn.get("user_response", ""),
                "judgement": judgement,
                "newly_hit_requirement_ids": newly_hit,
                "cumulative_hit_requirement_ids": list(cumulative_hits),
                "missed_requirement_ids": missed_ids,
                "missed_requirement_aspects": _missed_aspects_for_ids(trace, missed_ids),
                "self_reflection_events": turn.get("self_reflection_events") or [],
                "reflection_attempts": turn.get("reflection_attempts") or [],
                "accepted_despite_reflection_warning": turn.get("accepted_despite_reflection_warning", False),
                "stop_reason": turn.get("stop_reason"),
            }
        )

    final_metrics = cleaned.get("final_metrics") or task_metrics or {}
    elicited = cleaned.get("elicited_requirement_ids") or cumulative_hits
    missed = cleaned.get("missed_requirement_ids") or sorted(set(implicit_ids) - set(elicited))

    return {
        "scenario_id": cleaned.get("scenario_id"),
        "app_type": cleaned.get("app_type"),
        "initial_requirement": cleaned.get("initial_requirement") or cleaned.get("initial_req", ""),
        "final_metrics": final_metrics,
        "turns": sanitized_turns,
        "final_hit_requirement_ids": list(elicited),
        "final_missed_requirement_ids": list(missed),
        "final_missed_requirement_aspects": cleaned.get("missed_requirement_aspects")
        or _missed_aspects_for_ids(trace, missed),
        "stop_reason": _stop_reason(cleaned),
        "self_reflection_events": cleaned.get("self_reflection_events") or [],
        "failure_tags": cleaned.get("failure_tags") or [],
        "hit_sequence": cleaned.get("hit_sequence") or [],
    }


def compute_previous_metric_delta(
    previous_metrics: dict[str, Any],
    current_metrics: dict[str, Any],
) -> dict[str, float]:
    if not previous_metrics:
        return {}
    keys = ("mean_IRE", "mean_TKQR", "main_score", "IRE", "TKQR")
    delta: dict[str, float] = {}
    for key in keys:
        if key in previous_metrics or key in current_metrics:
            before = float(previous_metrics.get(key, 0.0) or 0.0)
            after = float(current_metrics.get(key, 0.0) or 0.0)
            delta[key] = round(after - before, 6)
    return delta


def load_diagnoser_prompt(prompt_name: str) -> str:
    path = PROMPT_DIR / prompt_name
    if path.parent != PROMPT_DIR or path.suffix.lower() != ".md":
        raise RuntimeError(f"invalid diagnoser prompt name: {prompt_name}")
    if not path.exists():
        raise RuntimeError(f"diagnoser prompt not found: {prompt_name}")
    return path.read_text(encoding="utf-8")


def validate_trace_problem_analysis(data: dict[str, Any]) -> None:
    required = [
        "diagnosis_summary",
        "evidence_limitations",
        "failure_findings",
        "route_observations",
        "candidate_root_causes",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"trace problem analysis failed: missing keys {missing}")
    if not isinstance(data.get("failure_findings"), list):
        raise RuntimeError("trace problem analysis failed: failure_findings must be a list")
    if not isinstance(data.get("route_observations"), list):
        raise RuntimeError("trace problem analysis failed: route_observations must be a list")


def validate_component_localization(data: dict[str, Any], allowed_components: set[str]) -> None:
    _validate_analysis(data, allowed_components)


def validate_diagnosis_schema(data: dict[str, Any], allowed_components: set[str]) -> None:
    validate_component_localization(data, allowed_components)


def write_diagnosis_outputs(
    analysis: Path,
    per_task_dir: Path,
    trace_analysis: dict[str, Any],
    localization: dict[str, Any],
) -> None:
    for item in trace_analysis.get("failure_findings") or []:
        if isinstance(item, dict) and item.get("finding_id"):
            write_text(per_task_dir / f"{item['finding_id']}.md", _finding_markdown(item, localization))
    overview = [localization.get("localization_summary") or trace_analysis.get("diagnosis_summary") or ""]
    write_text(analysis / "overview.md", _overview_markdown(overview))


LOCALIZATION_COMPONENTS = {
    "skills",
    "system_prompt",
    "memory",
    "self_reflection",
}
NON_WRITABLE_LOCALIZATION_COMPONENTS = {
    "skill_router",
    "memory_router",
    "schema",
    "registry",
    "pipeline",
    "runtime",
    "evaluator",
    "judge",
    "other",
}
LOCALIZATION_DIRECTIONS = {
    "create",
    "update",
    "replace",
    "demote",
    "disable",
    "remove",
    "validate",
    "inspect",
    "none",
}


def _validate_analysis(data: dict[str, Any], allowed_components: set[str]) -> None:
    required = ["localization_summary", "component_findings", "refiner_guidance"]
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"component localization failed: missing keys {missing}")
    if not isinstance(data.get("component_findings"), list):
        raise RuntimeError("component localization failed: component_findings must be a list")
    if not isinstance(data.get("refiner_guidance"), dict):
        raise RuntimeError("component localization failed: refiner_guidance must be an object")
    for finding in data["component_findings"]:
        if not isinstance(finding, dict):
            raise RuntimeError("component localization failed: invalid component finding")
        component = str(finding.get("component") or "")
        _validate_localization_component(component, allowed_components)
        direction = str(finding.get("recommended_refinement_direction") or "")
        if direction not in LOCALIZATION_DIRECTIONS:
            raise RuntimeError("component localization failed: invalid recommended_refinement_direction")
        if not finding.get("issue"):
            raise RuntimeError("component localization failed: component finding missing issue")


def _validate_localization_component(component: str, allowed_components: set[str]) -> None:
    normalized = component.strip()
    if normalized in NON_EVOLVABLE_COMPONENTS:
        raise RuntimeError(f"component localization failed: {normalized} is not an evolvable component")
    if normalized in NON_WRITABLE_LOCALIZATION_COMPONENTS:
        raise RuntimeError(f"component localization failed: {normalized} is not an editable component")
    if normalized not in allowed_components:
        raise RuntimeError(f"component is not declared by current harness seed: {normalized}")


def _validate_component(component: str, allowed_components: set[str]) -> None:
    _validate_localization_component(component, allowed_components)


def _strip_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_sensitive_keys(v)
            for k, v in value.items()
            if k not in _SENSITIVE_TRACE_KEYS and not str(k).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_sensitive_keys(item) for item in value]
    return value


def _all_implicit_requirement_ids(trace: dict[str, Any]) -> set[str]:
    elicited = set(trace.get("elicited_requirement_ids") or [])
    missed = set(trace.get("missed_requirement_ids") or [])
    for turn in trace.get("turns") or []:
        judgement = turn.get("judgement") or {}
        if isinstance(judgement, dict):
            rel = judgement.get("relevant_implied_requirements_id")
            if isinstance(rel, str) and rel:
                for part in rel.replace(",", " ").split():
                    if part.strip():
                        elicited.add(part.strip())
            for req_id in judgement.get("elicited_requirement_ids") or []:
                elicited.add(str(req_id))
    return elicited | missed


def _newly_hit_ids(judgement: dict[str, Any]) -> list[str]:
    if not isinstance(judgement, dict):
        return []
    return [str(item) for item in judgement.get("elicited_requirement_ids") or [] if str(item).strip()]


def _missed_aspects_for_ids(trace: dict[str, Any], missed_ids: list[str]) -> dict[str, int]:
    aspects = trace.get("missed_requirement_aspects")
    if isinstance(aspects, dict) and not missed_ids:
        return aspects
    if isinstance(aspects, dict) and missed_ids:
        return aspects
    return {}


def _stop_reason(trace: dict[str, Any]) -> str:
    turns = trace.get("turns") or []
    if not turns:
        return "unknown"
    last = turns[-1]
    if last.get("action") == "finish_interview":
        final_metrics = trace.get("final_metrics") or {}
        if final_metrics.get("early_finish"):
            return "premature_finish"
        return "finished"
    return "turn_budget_exhausted"


def _write_error_report(analysis: Path, exc: Exception, data: dict[str, Any], allowed_components: set[str]) -> None:
    write_json(
        analysis / "error_report.json",
        {
            "stage": "elicitation_diagnoser",
            "error": str(exc),
            "declared_harness_components": sorted(allowed_components),
            "raw_output": data,
        },
    )


def _overview_markdown(bullets: list[Any]) -> str:
    lines = ["# Elicitation Diagnoser Overview", ""]
    lines.extend(f"- {str(item)}" for item in bullets)
    return "\n".join(lines).rstrip() + "\n"


def _finding_markdown(item: dict[str, Any], localization: dict[str, Any]) -> str:
    lines = [f"# {item.get('finding_id', 'finding')}", "", str(item.get("description") or "")]
    lines.extend(["", "## Observed Effect", str(item.get("observed_effect") or "")])
    lines.extend(["", "## Related Localization"])
    for finding in localization.get("component_findings") or []:
        if not isinstance(finding, dict):
            continue
        lines.append(f"- {finding.get('component', '')}: {finding.get('issue', '')}")
    return "\n".join(lines).rstrip() + "\n"
