from __future__ import annotations

from pathlib import Path
from typing import Any

from reqahe.agents.seed import SeedInterviewer
from reqahe.envs.dataset import Scenario
from reqahe.envs.reqelicit import ReqElicitSession
from reqahe.evaluation.metrics import aggregate_metrics, task_metrics
from reqahe.harness.middleware import MiddlewareRuntime
from reqahe.harness.workspace import load_harness_text
from reqahe.llm.client import OpenAICompatibleClient
from reqahe.utils.io import append_jsonl, ensure_dir, write_json, write_text


def run_tasks(
    scenarios: list[Scenario],
    workspace_dir: str | Path,
    output_dir: str | Path,
    llm: OpenAICompatibleClient,
    interviewer_model: str,
    oracle_model: str,
    evaluator_model: str,
    max_turns: int,
    rollouts_per_task: int = 1,
    middleware_mode: str = "warn",
    agent_name: str = "seed_freeform",
) -> dict[str, Any]:
    output = ensure_dir(output_dir)
    harness = load_harness_text(workspace_dir)
    task_results: list[dict] = []
    total_rollouts = len(scenarios) * rollouts_per_task
    completed = 0
    print(f"[rollout] start tasks={len(scenarios)} rollouts_per_task={rollouts_per_task} total_rollouts={total_rollouts}", flush=True)
    for scenario in scenarios:
        for rollout_idx in range(rollouts_per_task):
            completed += 1
            task_dir = output / f"{scenario.scenario_id}__r{rollout_idx}"
            print(
                f"[rollout] {completed}/{total_rollouts} scenario={scenario.scenario_id} rollout={rollout_idx} status=running",
                flush=True,
            )
            result = run_single_task(
                scenario,
                harness,
                workspace_dir,
                task_dir,
                llm,
                interviewer_model,
                oracle_model,
                evaluator_model,
                max_turns=max_turns,
                middleware_mode=middleware_mode,
                agent_name=agent_name,
            )
            task_results.append(result)
            metrics = result["metrics"]
            print(
                f"[rollout] {completed}/{total_rollouts} scenario={scenario.scenario_id} "
                f"status=done IRE={metrics['IRE']} TKQR={metrics['TKQR']} approx_ESR={metrics['approx_ESR']}",
                flush=True,
            )
    aggregate = aggregate_metrics(task_results, max_turns=max_turns)
    write_json(output / "metrics.json", aggregate)
    write_json(output / "task_results.json", task_results)
    print(
        f"[rollout] complete mean_IRE={aggregate['mean_IRE']} mean_TKQR={aggregate['mean_TKQR']} main_score={aggregate['main_score']}",
        flush=True,
    )
    return {"metrics": aggregate, "task_results": task_results}


def run_single_task(
    scenario: Scenario,
    harness: dict[str, str],
    workspace_dir: str | Path,
    task_dir: str | Path,
    llm: OpenAICompatibleClient,
    interviewer_model: str,
    oracle_model: str,
    evaluator_model: str,
    max_turns: int,
    middleware_mode: str,
    agent_name: str,
) -> dict[str, Any]:
    task_path = ensure_dir(task_dir)
    session = ReqElicitSession(scenario, llm, oracle_model=oracle_model, evaluator_model=evaluator_model)
    middleware = MiddlewareRuntime(workspace_dir, mode=middleware_mode)
    agent = SeedInterviewer(harness, llm, model=interviewer_model)
    history: list[dict[str, str]] = []
    turns: list[dict] = []
    warnings: list[str] = []
    recent_hits = 0

    for turn_index in range(max_turns):
        print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} stage=interviewer", flush=True)
        prompt_record = {
            "turn_index": turn_index,
            "initial_req": scenario.initial_req,
            "history": history,
            "warnings": warnings[-5:],
        }
        append_jsonl(task_path / "agent_prompts.jsonl", prompt_record)
        action = agent.next_action(scenario.initial_req, history, warnings, max_turns)
        state = {
            "turn_index": turn_index,
            "max_turns": max_turns,
            "questions": [h["question"] for h in history],
            "recent_hits": recent_hits,
            "asked_style": any("style" in h["question"].lower() or "visual" in h["question"].lower() for h in history),
            "asked_content": any(_mentions_content(h["question"]) for h in history),
        }
        action, events = middleware.check(action, state)
        for event in events:
            event["turn_index"] = turn_index
            append_jsonl(task_path / "middleware_events.jsonl", event)
            if middleware_mode in {"warn", "enforce"}:
                warnings.append(event["message"])
        append_jsonl(task_path / "agent_actions.jsonl", {"turn_index": turn_index, "action": action})

        if action["action"] == "finish_interview":
            print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=finish stage=evaluator", flush=True)
            evaluator = session.finish(action.get("finish_summary", ""))
            turn = {
                "turn_index": turn_index,
                "action": "finish_interview",
                "question": "",
                "oracle_answer": "",
                "finish_summary": action.get("finish_summary", ""),
                "middleware_events": events,
                "evaluator": evaluator,
            }
            turns.append(turn)
            append_jsonl(task_path / "raw_trace.jsonl", turn)
            print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=finish status=done", flush=True)
            break

        question = action.get("question") or "Could you share one more specific requirement?"
        print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=ask_question stage=oracle", flush=True)
        answer, evaluator = session.ask(question)
        recent_hits = 1 if evaluator.get("hit") else 0
        history.append({"question": question, "answer": answer})
        turn = {
            "turn_index": turn_index,
            "action": "ask_question",
            "question": question,
            "oracle_answer": answer,
            "middleware_events": events,
            "evaluator": evaluator,
        }
        turns.append(turn)
        append_jsonl(task_path / "raw_trace.jsonl", turn)
        print(
            f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} "
            f"action=ask_question hit={evaluator.get('hit')} ids={','.join(evaluator.get('hit_requirement_ids', [])) or '-'}",
            flush=True,
        )

    metrics = task_metrics(turns, scenario.implicit_requirements, max_turns=max_turns)
    metrics.update(_quality_rates(turns))
    clean_trace = {
        "scenario_id": scenario.scenario_id,
        "app_type": scenario.app_type,
        "initial_req": scenario.initial_req,
        "turns": turns,
        "final_metrics": metrics,
        "failure_tags": _failure_tags(metrics, turns),
        "agent_name": agent_name,
        "evaluator_mode": session.evaluator_mode,
    }
    write_json(task_path / "clean_trace.json", clean_trace)
    write_json(task_path / "metrics.json", metrics)
    write_json(task_path / "evaluator_turn_hits.json", [{"turn_index": t["turn_index"], **t["evaluator"]} for t in turns])
    write_text(task_path / "conversation.md", _conversation_md(scenario, turns, metrics))
    return {
        "scenario_id": scenario.scenario_id,
        "app_type": scenario.app_type,
        "metrics": metrics,
        "trace_dir": str(task_path),
        "evaluator_mode": session.evaluator_mode,
    }


def _quality_rates(turns: list[dict]) -> dict:
    questions = [t.get("question", "") for t in turns if t.get("action") == "ask_question"]
    duplicate = 0
    broad = 0
    invalid = 0
    seen = set()
    for q in questions:
        norm = q.lower().strip()
        if norm in seen:
            duplicate += 1
        seen.add(norm)
        if any(p in norm for p in ["anything else", "any other", "what else", "还有别", "其他需求"]):
            broad += 1
        if not norm or len(norm.split()) < 3:
            invalid += 1
    denom = max(1, len(questions))
    return {
        "duplicate_question_rate": duplicate / denom,
        "broad_question_rate": broad / denom,
        "unanswered_or_invalid_question_rate": invalid / denom,
    }


def _mentions_content(question: str) -> bool:
    lowered = question.lower()
    content_terms = [
        "content",
        "data",
        "field",
        "record",
        "filter",
        "report",
        "dashboard",
        "chart",
        "integration",
        "search",
        "信息",
        "数据",
        "字段",
        "报表",
        "筛选",
    ]
    return any(term in lowered for term in content_terms)


def _failure_tags(metrics: dict, turns: list[dict]) -> list[str]:
    tags: list[str] = []
    coverage = metrics.get("type_coverage", {})
    if coverage.get("interaction", 0) == 0:
        tags.append("missed_interaction")
    if coverage.get("content", 0) == 0:
        tags.append("missed_content")
    if coverage.get("style", 0) == 0:
        tags.append("missed_style")
    if metrics.get("duplicate_question_rate", 0) > 0:
        tags.append("repeated_question")
    if metrics.get("broad_question_rate", 0) > 0:
        tags.append("broad_question_too_early")
    if metrics.get("early_finish"):
        tags.append("premature_finish")
    if not any(t.get("evaluator", {}).get("hit") for t in turns):
        tags.append("no_progress_turns")
    return tags


def _conversation_md(scenario: Scenario, turns: list[dict], metrics: dict) -> str:
    lines = [f"# {scenario.scenario_id}", "", f"Initial requirement: {scenario.initial_req}", "", "## Conversation"]
    for turn in turns:
        if turn["action"] == "ask_question":
            lines.append(f"\n**Interviewer:** {turn['question']}")
            lines.append(f"\n**Oracle:** {turn['oracle_answer']}")
            lines.append(f"\nHit: {turn['evaluator'].get('hit')} ({', '.join(turn['evaluator'].get('hit_requirement_ids', []))})")
        else:
            lines.append(f"\n**Finish:** {turn.get('finish_summary', '')}")
    lines.append("\n## Metrics")
    lines.append(f"\nIRE={metrics['IRE']} TKQR={metrics['TKQR']} approx_ESR={metrics['approx_ESR']}")
    return "\n".join(lines)
