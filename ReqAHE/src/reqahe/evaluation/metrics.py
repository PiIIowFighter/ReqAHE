from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable


def main_score(mean_ire: float, mean_tkqr: float) -> float:
    return 0.65 * mean_ire + 0.35 * mean_tkqr


def task_metrics(turns: list[dict], implicit_requirements: list[dict], max_turns: int) -> dict:
    total = max(1, len(implicit_requirements))
    hit_ids: set[str] = set()
    aspect_hits: dict[str, set[str]] = defaultdict(set)
    hit_sequence: list[int] = []
    probe_hits = 0
    probe_total = 0

    for turn in turns:
        action_type = str(turn.get("evaluator", {}).get("action_type") or turn.get("action") or "ask_question")
        hit = bool(turn.get("evaluator", {}).get("hit"))
        hit_sequence.append(1 if hit else 0)
        if action_type in {"probe", "ask_question", "clarify"}:
            probe_total += 1
            if hit:
                probe_hits += 1
        for req_id in turn.get("evaluator", {}).get("hit_requirement_ids", []):
            hit_ids.add(str(req_id))

    aspect_by_id = {}
    for idx, req in enumerate(implicit_requirements, start=1):
        req_id = str(req.get("id") or f"IR{idx}")
        aspect_by_id[req_id] = str(req.get("Aspect") or req.get("aspect") or "unknown").lower()
    for req_id in hit_ids:
        aspect_hits[aspect_by_id.get(req_id, "unknown")].add(req_id)

    ire = len(hit_ids) / total
    tkqr = calculate_tkqr(hit_sequence, total)
    approx_esr = probe_hits / probe_total if probe_total else 0.0
    turns_count = len(turns)
    return {
        "IRE": round(ire, 6),
        "TKQR": round(tkqr, 6),
        "approx_ESR": round(approx_esr, 6),
        "turns": turns_count,
        "hit_count": len(hit_ids),
        "total_implicit_requirements": len(implicit_requirements),
        "type_coverage": {
            "interaction": round(len(aspect_hits.get("interaction", set())) / max(1, _aspect_total(implicit_requirements, "Interaction")), 6),
            "content": round(len(aspect_hits.get("content", set())) / max(1, _aspect_total(implicit_requirements, "Content")), 6),
            "style": round(len(aspect_hits.get("style", set())) / max(1, _aspect_total(implicit_requirements, "Style")), 6),
        },
        "early_finish": turns_count < min(3, max_turns),
        "evaluator_note": "approx_ESR is computed from turn-level action labels and hit results; ReqElicitGym source did not expose a standalone ESR metric.",
    }


def _aspect_total(requirements: list[dict], aspect: str) -> int:
    return sum(1 for req in requirements if str(req.get("Aspect") or req.get("aspect") or "").lower() == aspect.lower())


def calculate_tkqr(hit_sequence: list[int], total_reqs: int) -> float:
    if not hit_sequence or total_reqs <= 0:
        return 0.0
    dcg = 0.0
    for idx, hit in enumerate(hit_sequence, start=1):
        if hit:
            dcg += 1.0 / idx
    ideal_hits = min(total_reqs, len(hit_sequence))
    idcg = sum(1.0 / idx for idx in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def aggregate_metrics(task_results: Iterable[dict], max_turns: int, paper_target: dict | None = None) -> dict:
    results = list(task_results)
    n = max(1, len(results))
    mean_ire = sum(r["metrics"]["IRE"] for r in results) / n
    mean_tkqr = sum(r["metrics"]["TKQR"] for r in results) / n
    mean_turns = sum(r["metrics"]["turns"] for r in results) / n
    mean_esr = sum(r["metrics"].get("approx_ESR", 0.0) for r in results) / n
    cov = Counter()
    rates = Counter()
    for result in results:
        metrics = result["metrics"]
        for key, value in metrics.get("type_coverage", {}).items():
            cov[key] += value
        for key in [
            "duplicate_question_rate",
            "broad_question_rate",
            "unanswered_or_invalid_question_rate",
        ]:
            rates[key] += metrics.get(key, 0.0)
        rates["early_finish_rate"] += 1.0 if metrics.get("early_finish") else 0.0
    score = main_score(mean_ire, mean_tkqr)
    target = paper_target or {"IRE": 0.69, "TKQR": 0.59}
    return {
        "task_count": len(results),
        "max_turns": max_turns,
        "mean_IRE": round(mean_ire, 6),
        "mean_TKQR": round(mean_tkqr, 6),
        "approx_ESR": round(mean_esr, 6),
        "mean_turns": round(mean_turns, 6),
        "main_score": round(score, 6),
        "type_coverage": {k: round(v / n, 6) for k, v in cov.items()},
        "duplicate_question_rate": round(rates["duplicate_question_rate"] / n, 6),
        "broad_question_rate": round(rates["broad_question_rate"] / n, 6),
        "early_finish_rate": round(rates["early_finish_rate"] / n, 6),
        "unanswered_or_invalid_question_rate": round(rates["unanswered_or_invalid_question_rate"] / n, 6),
        "exceed_ontoagent_IRE": mean_ire > target["IRE"],
        "exceed_ontoagent_TKQR": mean_tkqr > target["TKQR"],
        "exceed_ontoagent_both": mean_ire > target["IRE"] and mean_tkqr > target["TKQR"],
        "metric_caveats": [
            "approx_ESR is not treated as the paper ESR unless a source ESR implementation is discovered.",
        ],
    }
