from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from reqahe.runtime.reqelicit_session import ReqElicitSession
from reqahe.harness.workspace import load_harness_text, load_skill_catalog
from reqahe.infra.io import append_jsonl, ensure_dir, read_json, read_jsonl, write_json, write_text
from reqahe.utils.paths import to_posix_relpath
from reqahe.infra.llm_client import OpenAICompatibleClient
from reqahe.infra.network import CloseWaitCleaner
from reqahe.runtime.dataset import Scenario
from reqahe.runtime.interviewer import SeedInterviewer
from reqahe.runtime.metrics import aggregate_metrics, task_metrics
from reqahe.runtime.reflection import (
    ReflectionRuntime,
    format_reflection_retry_feedback,
    is_retryable_reflection_event,
)
from reqahe.runtime.route_stats import (
    build_router_reason,
    enrich_route_events_from_trace,
    make_route_event,
    write_rollout_route_stats,
)


def run_tasks(
    scenarios: list[Scenario],
    workspace_dir: str | Path,
    output_dir: str | Path,
    llm: OpenAICompatibleClient,
    llm_config: dict[str, Any],
    reqelicitgym_root: str | Path,
    interviewer_model: str,
    judge_model: str,
    user_model: str,
    user_answer_quality: str,
    max_turns: int,
    rollouts_per_task: int = 1,
    reflection_mode: str = "warn",
    agent_name: str = "seed_freeform",
    resume: bool = False,
    close_wait_cleaner: CloseWaitCleaner | None = None,
    skill_router_config: dict[str, Any] | None = None,
    skill_router_model: str = "",
    memory_router_config: dict[str, Any] | None = None,
    memory_router_model: str = "",
    self_reflection_config: dict[str, Any] | None = None,
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
            if resume:
                existing = _completed_task_result(task_dir, scenario)
                if existing:
                    task_results.append(existing)
                    metrics = existing["metrics"]
                    print(
                        f"[rollout] {completed}/{total_rollouts} scenario={scenario.scenario_id} "
                        f"rollout={rollout_idx} status=skipped_existing "
                        f"IRE={metrics['IRE']} TKQR={metrics['TKQR']} probe_effectiveness={metrics['probe_effectiveness']}",
                        flush=True,
                    )
                    if close_wait_cleaner:
                        close_wait_cleaner.maybe_cleanup(f"skip:{scenario.scenario_id}__r{rollout_idx}")
                    continue
                _archive_incomplete_task_dir(task_dir)
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
                llm_config,
                reqelicitgym_root,
                interviewer_model,
                judge_model,
                user_model,
                user_answer_quality,
                max_turns=max_turns,
                reflection_mode=reflection_mode,
                agent_name=agent_name,
                skill_router_config=skill_router_config,
                skill_router_model=skill_router_model,
                memory_router_config=memory_router_config,
                memory_router_model=memory_router_model,
                self_reflection_config=self_reflection_config,
            )
            task_results.append(result)
            metrics = result["metrics"]
            print(
                f"[rollout] {completed}/{total_rollouts} scenario={scenario.scenario_id} "
                f"status=done IRE={metrics['IRE']} TKQR={metrics['TKQR']} probe_effectiveness={metrics['probe_effectiveness']}",
                flush=True,
            )
            if close_wait_cleaner:
                close_wait_cleaner.maybe_cleanup(f"task:{scenario.scenario_id}__r{rollout_idx}")
    aggregate = aggregate_metrics(task_results, max_turns=max_turns)
    write_json(output / "metrics.json", aggregate)
    write_json(output / "task_results.json", task_results)
    router_skill_ids = [item["skill_id"] for item in load_skill_catalog(workspace_dir)]
    write_rollout_route_stats(output, task_results, router_skill_ids=router_skill_ids)
    print(
        f"[rollout] complete mean_IRE={aggregate['mean_IRE']} mean_TKQR={aggregate['mean_TKQR']} main_score={aggregate['main_score']}",
        flush=True,
    )
    return {"metrics": aggregate, "task_results": task_results}


def _completed_task_result(task_dir: Path, scenario: Scenario) -> dict[str, Any] | None:
    metrics_path = task_dir / "metrics.json"
    trace_path = task_dir / "clean_trace.json"
    if not metrics_path.exists() or not trace_path.exists():
        return None
    try:
        metrics = read_json(metrics_path)
        trace = read_json(trace_path)
    except Exception:
        return None
    if trace.get("scenario_id") != scenario.scenario_id:
        return None
    return {
        "scenario_id": scenario.scenario_id,
        "app_type": scenario.app_type,
        "metrics": metrics,
        "trace_dir": to_posix_relpath(task_dir, task_dir.parent),
        "evaluation_mode": trace.get("evaluation_mode", "reqelicitgym_judge_user"),
    }


def _archive_incomplete_task_dir(task_dir: Path) -> None:
    if not task_dir.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = task_dir.with_name(f"{task_dir.name}__incomplete_{stamp}")
    suffix = 1
    while archive.exists():
        suffix += 1
        archive = task_dir.with_name(f"{task_dir.name}__incomplete_{stamp}_{suffix}")
    shutil.move(str(task_dir), str(archive))
    print(f"[rollout] archived incomplete task dir {task_dir} -> {archive}", flush=True)


def run_single_task(
    scenario: Scenario,
    harness: dict[str, str],
    workspace_dir: str | Path,
    task_dir: str | Path,
    llm: OpenAICompatibleClient,
    llm_config: dict[str, Any],
    reqelicitgym_root: str | Path,
    interviewer_model: str,
    judge_model: str,
    user_model: str,
    user_answer_quality: str,
    max_turns: int,
    reflection_mode: str,
    agent_name: str,
    skill_router_config: dict[str, Any] | None = None,
    skill_router_model: str = "",
    memory_router_config: dict[str, Any] | None = None,
    memory_router_model: str = "",
    self_reflection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_path = ensure_dir(task_dir)
    reflection_cfg = self_reflection_config or {}
    session = ReqElicitSession(
        scenario,
        reqelicitgym_root=reqelicitgym_root,
        llm_config=llm_config,
        judge_model=judge_model,
        user_model=user_model,
        user_answer_quality=user_answer_quality,
    )
    reflection = ReflectionRuntime(workspace_dir, mode=reflection_mode, event_log_path=task_path / "self_reflection_events.jsonl")
    router_cfg = skill_router_config or {}
    memory_router_cfg = memory_router_config or {}
    agent = SeedInterviewer(
        harness,
        llm,
        model=interviewer_model,
        workspace_dir=workspace_dir,
        skill_router_config=router_cfg,
        skill_router_model=skill_router_model or interviewer_model,
        skill_routing_event_log_path=task_path / "skill_routing_events.jsonl",
        memory_router_config=memory_router_cfg,
        memory_router_model=memory_router_model or interviewer_model,
        memory_routing_event_log_path=task_path / "memory_routing_events.jsonl",
    )
    history: list[dict[str, str]] = []
    turns: list[dict] = []
    warnings: list[str] = []
    self_reflection_events: list[dict[str, Any]] = []
    route_events: list[dict[str, Any]] = []
    recent_hits = 0
    finished = False

    for turn_index in range(max_turns):
        print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} stage=interviewer", flush=True)
        action_result = _generate_action_with_reflection_retry(
            agent=agent,
            reflection=reflection,
            scenario=scenario,
            history=history,
            warnings=warnings,
            max_turns=max_turns,
            turn_index=turn_index,
            recent_hits=recent_hits,
            reflection_mode=reflection_mode,
            self_reflection_config=reflection_cfg,
            task_path=task_path,
            workspace_dir=workspace_dir,
            router_cfg=router_cfg,
            memory_router_cfg=memory_router_cfg,
        )
        action = action_result["action"]
        events = action_result["events"]
        all_events = action_result["all_events"]
        self_reflection_events.extend(all_events)

        append_jsonl(
            task_path / "agent_actions.jsonl",
            {
                "turn_index": turn_index,
                "action": action,
                "reflection_attempts": action_result["attempts"],
                "self_reflection_events": events,
                "all_self_reflection_events": all_events,
                "accepted_despite_reflection_warning": action_result["accepted_despite_reflection_warning"],
                "memory_routing": action_result["memory_routing"],
                "skill_routing": action_result["skill_routing"],
                **(
                    {"fallback_finish": True, "fallback_reason": action_result["fallback_reason"]}
                    if action_result.get("fallback_finish")
                    else {}
                ),
            },
        )
        skill_routing = action_result.get("skill_routing") or {}
        route_events.append(
            make_route_event(
                task_id=scenario.scenario_id,
                turn_index=turn_index,
                candidate_skill_ids=skill_routing.get("candidate_skill_ids") or [],
                selected_skill_ids=skill_routing.get("selected_skill_ids") or [],
                router_reason=str(skill_routing.get("router_reason") or build_router_reason(skill_routing)),
            )
        )

        if action["action"] == "finish_interview":
            utterance = action.get("finish_summary", "")
            print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=finish stage=judge", flush=True)
            step_result = session.step(utterance)
            judgement = step_result["judgement"]
            turn = {
                "turn_index": turn_index,
                "action": "finish_interview",
                "question": "",
                "user_response": step_result["user_response"],
                "finish_summary": utterance,
                "self_reflection_events": all_events,
                "reflection_attempts": action_result["attempts"],
                "accepted_despite_reflection_warning": action_result["accepted_despite_reflection_warning"],
                "judgement": judgement,
            }
            turns.append(turn)
            append_jsonl(task_path / "raw_trace.jsonl", turn)
            print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=finish status=done", flush=True)
            finished = True
            break

        question = action.get("question") or "Could you share one more specific requirement?"
        print(f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} action=ask_question stage=judge_user", flush=True)
        step_result = session.step(question)
        judgement = step_result["judgement"]
        answer = step_result["user_response"]
        recent_hits = 1 if judgement.get("is_relevant_to_implied_requirements") else 0
        history.append({"question": question, "answer": answer})
        turn = {
            "turn_index": turn_index,
            "action": "ask_question",
            "question": question,
            "user_response": answer,
            "self_reflection_events": all_events,
            "reflection_attempts": action_result["attempts"],
            "accepted_despite_reflection_warning": action_result["accepted_despite_reflection_warning"],
            "judgement": judgement,
        }
        turns.append(turn)
        append_jsonl(task_path / "raw_trace.jsonl", turn)
        elicited = judgement.get("elicited_requirement_ids", [])
        print(
            f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} "
            f"action=ask_question relevant={judgement.get('is_relevant_to_implied_requirements')} "
            f"elicited={','.join(elicited) or '-'}",
            flush=True,
        )

    if not finished:
        turn_index = len(turns)
        print(
            f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} "
            f"action=forced_finish reason=turn_budget_exhausted",
            flush=True,
        )
        action = _forced_finish_action(history, "turn budget exhausted")
        append_jsonl(
            task_path / "agent_actions.jsonl",
            {
                "turn_index": turn_index,
                "action": action,
                "memory_routing": {},
                "skill_routing": {},
            },
        )
        step_result = session.step(action.get("finish_summary", ""))
        turn = {
            "turn_index": turn_index,
            "action": "finish_interview",
            "question": "",
            "user_response": step_result["user_response"],
            "finish_summary": action.get("finish_summary", ""),
            "self_reflection_events": [],
            "judgement": step_result["judgement"],
        }
        turns.append(turn)
        append_jsonl(task_path / "raw_trace.jsonl", turn)

    metrics = task_metrics(turns, scenario.implicit_requirements, max_turns=max_turns)
    elicited_requirement_ids = _elicited_requirement_ids(turns)
    missed_requirement_ids = _missed_requirement_ids(scenario.implicit_requirements, elicited_requirement_ids)
    hit_sequence = _hit_sequence(turns)
    skill_routing_events_path = task_path / "skill_routing_events.jsonl"
    memory_routing_events_path = task_path / "memory_routing_events.jsonl"
    enriched_route_events = enrich_route_events_from_trace(
        route_events,
        scenario_id=scenario.scenario_id,
        turns=turns,
    )
    route_events_path = task_path / "route_events.jsonl"
    if route_events_path.exists():
        route_events_path.unlink()
    for event in enriched_route_events:
        append_jsonl(route_events_path, event)
    clean_trace = {
        "scenario_id": scenario.scenario_id,
        "app_type": scenario.app_type,
        "initial_req": scenario.initial_req,
        "turns": turns,
        "final_metrics": metrics,
        "elicited_requirement_ids": elicited_requirement_ids,
        "missed_requirement_ids": missed_requirement_ids,
        "missed_requirement_aspects": _missed_requirement_aspects(scenario.implicit_requirements, missed_requirement_ids),
        "hit_sequence": hit_sequence,
        "failure_tags": _failure_tags(metrics, turns, self_reflection_events),
        "self_reflection_events": self_reflection_events,
        "memory_routing_events": read_jsonl(memory_routing_events_path),
        "skill_routing_events": read_jsonl(skill_routing_events_path),
        "agent_name": agent_name,
        "evaluation_mode": session.evaluation_mode,
    }
    write_json(task_path / "clean_trace.json", clean_trace)
    write_json(task_path / "metrics.json", metrics)
    write_json(
        task_path / "judgement_turns.json",
        [{"turn_index": t["turn_index"], **t["judgement"]} for t in turns if t.get("judgement")],
    )
    write_text(task_path / "conversation.md", _conversation_md(scenario, turns, metrics))
    return {
        "scenario_id": scenario.scenario_id,
        "app_type": scenario.app_type,
        "metrics": metrics,
        "trace_dir": to_posix_relpath(task_path, task_path.parent),
        "evaluation_mode": session.evaluation_mode,
    }


def candidate_from_action(action: dict[str, Any], turn_index: int) -> dict[str, Any]:
    action_type = action.get("action")
    if action_type == "finish_interview":
        return {
            "kind": "finish",
            "text": str(action.get("finish_summary") or ""),
            "raw_action": action,
            "turn_index": turn_index,
        }
    if action_type == "ask_question":
        return {
            "kind": "question",
            "text": str(action.get("question") or ""),
            "raw_action": action,
            "turn_index": turn_index,
        }
    raise ValueError(f"Unsupported action type for candidate reflection: {action_type}")


def _candidate_hook(candidate: dict[str, Any]) -> str:
    if candidate["kind"] == "question":
        return "question_candidate"
    if candidate["kind"] == "finish":
        return "finish_candidate"
    raise ValueError(f"Unsupported candidate kind: {candidate.get('kind')}")


def _generate_action_with_reflection_retry(
    *,
    agent: SeedInterviewer,
    reflection: ReflectionRuntime,
    scenario: Scenario,
    history: list[dict[str, str]],
    warnings: list[str],
    max_turns: int,
    turn_index: int,
    recent_hits: int,
    reflection_mode: str,
    self_reflection_config: dict[str, Any],
    task_path: Path,
    workspace_dir: str | Path,
    router_cfg: dict[str, Any],
    memory_router_cfg: dict[str, Any],
) -> dict[str, Any]:
    max_retries = int(self_reflection_config.get("max_retries", 1))
    retry_on_modes = set(self_reflection_config.get("retry_on_modes", ["warn", "enforce"]))
    max_feedback_events = int(self_reflection_config.get("max_feedback_events", 3))

    attempt = 0
    retry_feedback: list[str] = []
    attempts: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    fallback_finish = False
    fallback_reason = ""

    while attempt <= max_retries:
        current_warnings = warnings + retry_feedback
        try:
            action = agent.next_action(
                scenario.initial_req,
                history,
                current_warnings,
                max_turns,
                turn_index=turn_index,
                app_type=scenario.app_type,
            )
        except RuntimeError as exc:
            print(
                f"[turn] scenario={scenario.scenario_id} turn={turn_index + 1}/{max_turns} "
                f"action=fallback_finish reason={exc}",
                flush=True,
            )
            action = _forced_finish_action(history, str(exc))
            fallback_finish = True
            fallback_reason = str(exc)

        routing_result = getattr(agent, "last_skill_routing_result", {}) or {}
        memory_routing_result = getattr(agent, "last_memory_routing", {}) or {}
        prompt_digest = getattr(agent, "last_prompt_digest", {}) or {}
        prompt_record = {
            "turn_index": turn_index,
            "reflection_attempt": attempt,
            "initial_req": scenario.initial_req,
            "history": history,
            "warnings": warnings[-5:],
            "reflection_retry_feedback": retry_feedback,
            "skill_router_enabled": bool(router_cfg.get("enabled", True)),
            "memory_router_enabled": bool(memory_router_cfg.get("enabled", True)),
            "memory_router": {
                "enabled": bool(memory_router_cfg.get("enabled", True)) if isinstance(memory_router_cfg, dict) else True,
                "selected_type": memory_routing_result.get("selected_type", ""),
                "confidence": memory_routing_result.get("confidence", 0.0),
                "decision": memory_routing_result.get("decision", "none"),
                "available_types": memory_routing_result.get("available_types", []),
                "router_error": memory_routing_result.get("router_error", ""),
            },
            "skill_router": {
                "enabled": bool(router_cfg.get("enabled", True)) if isinstance(router_cfg, dict) else True,
                "selected_skill_ids": routing_result.get("selected_skill_ids", []),
                "selected_skill_count": len(routing_result.get("selected_skill_ids", [])),
                "catalog_size": routing_result.get("catalog_size", 0),
                "router_error": routing_result.get("router_error", ""),
            },
            "prompt_digest": prompt_digest,
        }
        append_jsonl(task_path / "agent_prompts.jsonl", prompt_record)

        state = {
            "initial_req": scenario.initial_req,
            "app_type": scenario.app_type,
            "turn_index": turn_index,
            "max_turns": max_turns,
            "remaining_turns": max_turns - turn_index,
            "history": history,
            "previous_questions": [h["question"] for h in history],
            "recent_hits": recent_hits,
            "reflection_attempt": attempt,
        }
        candidate = candidate_from_action(action, turn_index)
        hook = _candidate_hook(candidate)
        events = reflection.check(candidate, state, hook=hook)

        retry_events = [
            event
            for event in events
            if is_retryable_reflection_event(
                event,
                runtime_mode=reflection_mode,
                retry_on_modes=retry_on_modes,
            )
        ]
        retry_triggered = bool(retry_events) and attempt < max_retries and not fallback_finish

        for event in events:
            event["turn_index"] = turn_index
            event["reflection_attempt"] = attempt
            event["discarded_action"] = retry_triggered
            event["retry_triggered"] = retry_triggered
            if reflection.event_log_path:
                append_jsonl(reflection.event_log_path, event)

        attempt_record = {
            "attempt": attempt,
            "action": action,
            "candidate_kind": candidate["kind"],
            "candidate_text": candidate["text"],
            "self_reflection_events": events,
            "retry_events": retry_events,
            "retry_triggered": retry_triggered,
            "discarded": retry_triggered,
            "reflection_retry_feedback": list(retry_feedback),
            "memory_routing": agent.last_memory_routing,
            "skill_routing": agent.last_skill_routing,
            "prompt_digest": prompt_digest,
        }
        attempts.append(attempt_record)
        all_events.extend(events)

        if retry_triggered:
            retry_feedback = format_reflection_retry_feedback(
                retry_events,
                workspace_dir,
                max_events=max_feedback_events,
            )
            attempt += 1
            continue

        accepted_despite = bool(retry_events) and attempt >= max_retries
        return {
            "action": action,
            "events": events,
            "all_events": all_events,
            "attempts": attempts,
            "accepted_despite_reflection_warning": accepted_despite,
            "memory_routing": agent.last_memory_routing,
            "skill_routing": agent.last_skill_routing,
            "prompt_digest": prompt_digest,
            "fallback_finish": fallback_finish,
            "fallback_reason": fallback_reason,
        }

    accepted_despite = bool(retry_events) if attempts else False
    final_attempt = attempts[-1] if attempts else {}
    return {
        "action": final_attempt.get("action", _forced_finish_action(history, "reflection retry exhausted")),
        "events": final_attempt.get("self_reflection_events", []),
        "all_events": all_events,
        "attempts": attempts,
        "accepted_despite_reflection_warning": accepted_despite,
        "memory_routing": final_attempt.get("memory_routing", {}),
        "skill_routing": final_attempt.get("skill_routing", {}),
        "prompt_digest": final_attempt.get("prompt_digest", {}),
        "fallback_finish": fallback_finish,
        "fallback_reason": fallback_reason,
    }


def _forced_finish_action(history: list[dict[str, str]], reason: str) -> dict[str, Any]:
    highlights = []
    for item in history[-3:]:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question:
            highlights.append(f"- Q: {question[:240]}")
        if answer:
            highlights.append(f"  A: {answer[:240]}")
    summary = "Interview ended automatically because the interviewer could not produce a valid next action."
    if reason:
        summary += f" Reason: {reason}."
    if highlights:
        summary += " Recent exchanges:\n" + "\n".join(highlights)
    return {
        "thought_summary": "forced finish fallback",
        "action": "finish_interview",
        "question": "",
        "finish_summary": summary,
    }


def _elicited_requirement_ids(turns: list[dict]) -> list[str]:
    elicited: set[str] = set()
    for turn in turns:
        for req_id in turn.get("judgement", {}).get("elicited_requirement_ids", []):
            elicited.add(str(req_id))
    return sorted(elicited)


def _all_requirement_ids(implicit_requirements: list[dict]) -> list[str]:
    ids = []
    for idx, req in enumerate(implicit_requirements, start=1):
        ids.append(str(req.get("id") or req.get("ID") or f"IR{idx}"))
    return ids


def _missed_requirement_ids(implicit_requirements: list[dict], elicited_ids: list[str]) -> list[str]:
    elicited = set(elicited_ids)
    return [req_id for req_id in _all_requirement_ids(implicit_requirements) if req_id not in elicited]


def _missed_requirement_aspects(implicit_requirements: list[dict], missed_ids: list[str]) -> dict[str, int]:
    missed = set(missed_ids)
    counts = {"interaction": 0, "content": 0, "style": 0}
    for idx, req in enumerate(implicit_requirements, start=1):
        req_id = str(req.get("id") or req.get("ID") or f"IR{idx}")
        if req_id not in missed:
            continue
        aspect = str(req.get("Aspect") or req.get("aspect") or "").lower()
        if aspect in counts:
            counts[aspect] += 1
    return counts


def _hit_sequence(turns: list[dict]) -> list[int]:
    return [1 if t.get("judgement", {}).get("is_relevant_to_implied_requirements") else 0 for t in turns]


def _failure_tags(metrics: dict, turns: list[dict], self_reflection_events: list[dict] | None = None) -> list[str]:
    tags: list[str] = []
    coverage = metrics.get("type_coverage", {})
    if coverage.get("interaction", 0) == 0:
        tags.append("interaction_gap")
    if coverage.get("content", 0) == 0:
        tags.append("content_gap")
    if coverage.get("style", 0) == 0:
        tags.append("style_gap")
    if metrics.get("IRE", 0) < 0.4:
        tags.append("low_ire")
    if metrics.get("TKQR", 0) < 0.35:
        tags.append("low_tkqr")
    if not any(t.get("judgement", {}).get("is_relevant_to_implied_requirements") for t in turns):
        tags.append("no_progress_turns")
    for event in self_reflection_events or []:
        event_type = event.get("type")
        if event_type:
            tags.append(str(event_type))
    return sorted(set(tags))


def _conversation_md(scenario: Scenario, turns: list[dict], metrics: dict) -> str:
    lines = [f"# {scenario.scenario_id}", "", f"Initial requirement: {scenario.initial_req}", "", "## Conversation"]
    for turn in turns:
        judgement = turn.get("judgement", {})
        if turn["action"] == "ask_question":
            lines.append(f"\n**Interviewer:** {turn['question']}")
            lines.append(f"\n**User:** {turn['user_response']}")
            lines.append(
                f"\nRelevant: {judgement.get('is_relevant_to_implied_requirements')} "
                f"({', '.join(judgement.get('elicited_requirement_ids', []))})"
            )
        else:
            lines.append(f"\n**Finish:** {turn.get('finish_summary', '')}")
    lines.append("\n## Metrics")
    lines.append(
        f"\nIRE={metrics['IRE']} TKQR={metrics['TKQR']} probe_effectiveness={metrics['probe_effectiveness']}"
    )
    return "\n".join(lines)
