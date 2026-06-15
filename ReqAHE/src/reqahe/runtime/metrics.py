from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Iterable


def main_score(mean_ire: float, mean_tkqr: float) -> float:
    return 0.65 * mean_ire + 0.35 * mean_tkqr


def task_metrics(turns: list[dict], implicit_requirements: list[dict], max_turns: int) -> dict:
    total = max(1, len(implicit_requirements))
    elicited_ids: set[str] = set()
    aspect_hits: dict[str, set[str]] = defaultdict(set)
    hit_sequence: list[int] = []
    probe_hits = 0
    probe_total = 0

    for turn in turns:
        judgement = turn.get("judgement", {})
        is_relevant = bool(judgement.get("is_relevant_to_implied_requirements"))
        hit_sequence.append(1 if is_relevant else 0)
        action_type = str(judgement.get("action_type") or "unknown")
        if action_type in {"probe", "clarify"}:
            probe_total += 1
            if is_relevant:
                probe_hits += 1
        for req_id in judgement.get("elicited_requirement_ids", []):
            elicited_ids.add(str(req_id))

    aspect_by_id = {}
    for idx, req in enumerate(implicit_requirements, start=1):
        req_id = str(req.get("id") or req.get("ID") or f"IR{idx}")
        aspect_by_id[req_id] = str(req.get("Aspect") or req.get("aspect") or "unknown").lower()
    for req_id in elicited_ids:
        aspect_hits[aspect_by_id.get(req_id, "unknown")].add(req_id)

    ire = len(elicited_ids) / total
    tkqr = calculate_tkqr(hit_sequence, total)
    probe_effectiveness = probe_hits / probe_total if probe_total else 0.0
    turns_count = len(turns)
    return {
        "IRE": round(ire, 6),
        "TKQR": round(tkqr, 6),
        "probe_effectiveness": round(probe_effectiveness, 6),
        "turns": turns_count,
        "hit_count": len(elicited_ids),
        "total_implicit_requirements": len(implicit_requirements),
        "type_coverage": {
            "interaction": round(len(aspect_hits.get("interaction", set())) / max(1, _aspect_total(implicit_requirements, "Interaction")), 6),
            "content": round(len(aspect_hits.get("content", set())) / max(1, _aspect_total(implicit_requirements, "Content")), 6),
            "style": round(len(aspect_hits.get("style", set())) / max(1, _aspect_total(implicit_requirements, "Style")), 6),
        },
        "early_finish": turns_count < min(3, max_turns),
        "metric_note": "IRE/TKQR follow ReqElicitGym elicitation_ratio and _calculate_tkqr semantics via judge->user evaluate_action.",
    }


def _aspect_total(requirements: list[dict], aspect: str) -> int:
    return sum(1 for req in requirements if str(req.get("Aspect") or req.get("aspect") or "").lower() == aspect.lower())


def calculate_tkqr(hit_sequence: list[int], total_reqs: int) -> float:
    """Match ReqElicitGym._calculate_tkqr discounting."""
    n = len(hit_sequence)
    k = total_reqs
    if n == 0 or k == 0:
        return 0.0
    dcg = 0.0
    for i, hit in enumerate(hit_sequence, start=1):
        if hit:
            dcg += 1.0 / math.log2(i + 1)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, min(n, k) + 1))
    return dcg / idcg if idcg else 0.0


def aggregate_metrics(task_results: Iterable[dict], max_turns: int, paper_target: dict | None = None) -> dict:
    results = list(task_results)
    n = max(1, len(results))
    mean_ire = sum(r["metrics"]["IRE"] for r in results) / n
    mean_tkqr = sum(r["metrics"]["TKQR"] for r in results) / n
    mean_turns = sum(r["metrics"]["turns"] for r in results) / n
    mean_probe = sum(r["metrics"].get("probe_effectiveness", 0.0) for r in results) / n
    cov = Counter()
    rates = Counter()
    for result in results:
        metrics = result["metrics"]
        for key, value in metrics.get("type_coverage", {}).items():
            cov[key] += value
        rates["early_finish_rate"] += 1.0 if metrics.get("early_finish") else 0.0
    score = main_score(mean_ire, mean_tkqr)
    aggregate = {
        "task_count": len(results),
        "max_turns": max_turns,
        "mean_IRE": round(mean_ire, 6),
        "mean_TKQR": round(mean_tkqr, 6),
        "probe_effectiveness": round(mean_probe, 6),
        "mean_turns": round(mean_turns, 6),
        "main_score": round(score, 6),
        "type_coverage": {k: round(v / n, 6) for k, v in cov.items()},
        "early_finish_rate": round(rates["early_finish_rate"] / n, 6),
        "evaluation_mode": "reqelicitgym_judge_user",
    }
    if paper_target:
        aggregate["exceed_target_IRE"] = mean_ire > paper_target["IRE"]
        aggregate["exceed_target_TKQR"] = mean_tkqr > paper_target["TKQR"]
        aggregate["exceed_target_both"] = mean_ire > paper_target["IRE"] and mean_tkqr > paper_target["TKQR"]
    return aggregate
