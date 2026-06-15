from __future__ import annotations

from pathlib import Path

from reqahe.config import parse_bool, role_model
from reqahe.infra.io import read_json
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.infra.paths import safe_name
from reqahe.utils.paths import resolve_maybe_relative, resolve_project_path, to_posix_relpath


def reflection_mode(config: dict, default: str = "warn") -> str:
    evolution = config.get("evolution", {})
    return str(evolution.get("reflection_mode") or default)


def rollout_kwargs(config: dict, agent_name: str, reflection_mode: str) -> dict:
    eval_cfg = config.get("evaluation", {})
    project_root = Path(config["paths"]["project_root"])
    reqelicit_root = resolve_project_path(config["paths"]["reqelicitgym_root"], project_root)
    return {
        "llm_config": config.get("llm", {}),
        "reqelicitgym_root": str(reqelicit_root),
        "interviewer_model": role_model(config, "interviewer"),
        "judge_model": role_model(config, "judge"),
        "user_model": role_model(config, "user"),
        "user_answer_quality": str(eval_cfg.get("user_answer_quality", "high")),
        "max_turns": int(eval_cfg["max_turns"]),
        "rollouts_per_task": int(eval_cfg["rollouts_per_task"]),
        "reflection_mode": reflection_mode,
        "agent_name": agent_name,
        "skill_router_config": config.get("runtime", {}).get("skill_router", {}),
        "skill_router_model": role_model(config, "skill_router") or role_model(config, "interviewer"),
        "memory_router_config": config.get("runtime", {}).get("memory_router", {}),
        "memory_router_model": role_model(config, "memory_router") or role_model(config, "interviewer"),
        "self_reflection_config": config.get("runtime", {}).get("self_reflection", {}),
    }


def llm_client(config: dict) -> OpenAICompatibleClient:
    llm = config.get("llm", {})
    return OpenAICompatibleClient(
        api_key=llm.get("api_key", ""),
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        temperature=float(llm.get("temperature", 0.2)),
        max_tokens=int(llm.get("max_tokens", 1200)),
        max_retries=int(llm.get("max_retries", 8)),
        trust_env=parse_bool(llm.get("trust_env"), default=False),
        timeout=float(llm.get("timeout", 120)),
    )


def output_label(config: dict, task_mode: str, container_name: str) -> str:
    model = safe_name(config.get("llm", {}).get("model") or "model")
    mode = safe_name(task_mode)
    container = safe_name(container_name)
    return f"reqahe_{model}_{mode}_{container}"


def conversation_turns(trace: dict) -> list[dict]:
    total = max(1, trace["final_metrics"].get("total_implicit_requirements", 0))
    elicited_ids: set[str] = set()
    turns = []
    for turn in trace["turns"]:
        judgement = turn.get("judgement", {})
        for req_id in judgement.get("elicited_requirement_ids", []):
            elicited_ids.add(str(req_id))
        if turn["action"] == "ask_question":
            turns.append(
                {
                    "turn": turn["turn_index"] + 1,
                    "interviewer": turn.get("question", ""),
                    "user": turn.get("user_response", ""),
                    "action_type": judgement.get("action_type", "probe"),
                    "is_relevant_to_url": bool(judgement.get("is_relevant_to_implied_requirements")),
                    "elicitation_ratio": round(len(elicited_ids) / total, 6),
                    "elicited_requirements": list(judgement.get("elicited_requirement_ids", [])),
                }
            )
        else:
            turns.append(
                {
                    "turn": turn["turn_index"] + 1,
                    "interviewer": turn.get("finish_summary", ""),
                    "user": turn.get("user_response", ""),
                    "action_type": judgement.get("action_type", "finish"),
                    "is_relevant_to_url": bool(judgement.get("is_relevant_to_implied_requirements")),
                    "elicitation_ratio": round(len(elicited_ids) / total, 6),
                    "elicited_requirements": list(judgement.get("elicited_requirement_ids", [])),
                }
            )
    return turns


def conversation_output(
    scenarios: list,
    result: dict,
    config: dict,
    rollout_dir: Path,
    *,
    blank_trace_dir: bool = False,
) -> list[dict]:
    scenarios_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    rows = []
    for task_result in result["task_results"]:
        trace_path = resolve_maybe_relative(task_result["trace_dir"], rollout_dir) / "clean_trace.json"
        trace = read_json(trace_path)
        scenario = scenarios_by_id[trace["scenario_id"]]
        trace_dir = ""
        if not blank_trace_dir:
            trace_dir = to_posix_relpath(task_result["trace_dir"], rollout_dir)
        rows.append(
            {
                "task_id": scenario.scenario_id,
                "task_name": scenario.name,
                "application_type": scenario.app_type,
                "initial_requirements": scenario.initial_req,
                "user_stories": scenario.final_requirements,
                "user_answer_quality": config["evaluation"].get("user_answer_quality", "high"),
                "interviewer_model": role_model(config, "interviewer"),
                "judge_model": role_model(config, "judge"),
                "user_model": role_model(config, "user"),
                "evaluation_mode": trace.get("evaluation_mode", "reqelicitgym_judge_user"),
                "conversation": conversation_turns(trace),
                "final_metrics": trace["final_metrics"],
                "trace_dir": trace_dir,
            }
        )
    return rows


def metrics_output(
    rollout_dir: Path,
    scenarios: list,
    result: dict,
    config: dict,
    split: str,
    task_mode: str,
    agent_name: str,
    *,
    base_dir: Path | None = None,
    batch_size: int | None = None,
) -> dict:
    aggregate = result["metrics"]
    task_results = result["task_results"]
    total_hidden = sum(item["metrics"].get("total_implicit_requirements", 0) for item in task_results)
    total_elicited = sum(item["metrics"].get("hit_count", 0) for item in task_results)
    path_base = base_dir or rollout_dir.parent
    project_root = Path(config["paths"]["project_root"])
    data_path = config["evaluation"].get("resolved_data_path", "")
    payload: dict = {
        "data_path": to_posix_relpath(data_path, project_root) if data_path else "",
        "rollout_dir": to_posix_relpath(rollout_dir, path_base),
        "config": {
            "agent": agent_name,
            "interviewer_model": role_model(config, "interviewer"),
            "judge_model": role_model(config, "judge"),
            "user_model": role_model(config, "user"),
            "diagnoser_model": role_model(config, "diagnoser"),
            "refiner_model": role_model(config, "refiner"),
            "user_answer_quality": config["evaluation"].get("user_answer_quality", "high"),
            "evaluation_mode": aggregate.get("evaluation_mode", "reqelicitgym_judge_user"),
            "split": split,
            "task_mode": task_mode,
            "dataset_file": config["evaluation"].get("dataset_file", ""),
            "dataset_number": config["evaluation"].get("dataset_number", ""),
            "max_turns": config["evaluation"]["max_turns"],
            "rollouts_per_task": config["evaluation"]["rollouts_per_task"],
            "reflection_mode": reflection_mode(config, default="warn"),
            "batch_size": batch_size or 0,
        },
        "overall_evaluation": {
            "total_test_samples": len(scenarios),
            "total_hidden_requirements": total_hidden,
            "total_elicited": total_elicited,
            "average_elicitation_ratio": aggregate["mean_IRE"],
            "average_tkqr": aggregate["mean_TKQR"],
            "probe_effectiveness": aggregate.get("probe_effectiveness", 0.0),
            "main_score": aggregate["main_score"],
            "type_coverage": aggregate.get("type_coverage", {}),
        },
        "task_results": task_results,
    }
    return payload


def independent_test_metrics_output(
    scenarios: list,
    result: dict,
    config: dict,
    *,
    iteration_name: str,
    workspace_dir: str,
    dataset_relpath: str,
    agent_name: str = "evolved_reahe",
    blank_trace_dir: bool = False,
) -> dict:
    aggregate = result["metrics"]
    task_results = [dict(item) for item in result["task_results"]]
    if blank_trace_dir:
        for item in task_results:
            item["trace_dir"] = ""
    total_hidden = sum(item["metrics"].get("total_implicit_requirements", 0) for item in task_results)
    total_elicited = sum(item["metrics"].get("hit_count", 0) for item in task_results)
    return {
        "data_path": dataset_relpath,
        "result_type": "independent_test_result",
        "evaluated_iteration": iteration_name,
        "workspace_dir": workspace_dir,
        "config": {
            "agent": agent_name,
            "interviewer_model": role_model(config, "interviewer"),
            "judge_model": role_model(config, "judge"),
            "user_model": role_model(config, "user"),
            "user_answer_quality": config["evaluation"].get("user_answer_quality", "high"),
            "evaluation_mode": aggregate.get("evaluation_mode", "reqelicitgym_judge_user"),
            "split": "independent_test",
            "task_mode": "full",
            "dataset_file": dataset_relpath,
            "max_turns": config["evaluation"]["max_turns"],
            "rollouts_per_task": config["evaluation"]["rollouts_per_task"],
            "reflection_mode": reflection_mode(config, default="warn"),
        },
        "overall_evaluation": {
            "total_test_samples": len(scenarios),
            "total_hidden_requirements": total_hidden,
            "total_elicited": total_elicited,
            "average_elicitation_ratio": aggregate["mean_IRE"],
            "average_tkqr": aggregate["mean_TKQR"],
            "probe_effectiveness": aggregate.get("probe_effectiveness", 0.0),
            "main_score": aggregate["main_score"],
            "type_coverage": aggregate.get("type_coverage", {}),
        },
        "task_results": task_results,
    }
