from __future__ import annotations

from pathlib import Path

from reqahe.utils.io import read_json, write_text


PAPER_TARGET = {"IRE": 0.69, "TKQR": 0.59}


def resolve_run_dir(project_root: str | Path, run_dir: str | Path) -> Path:
    raw = Path(run_dir)
    if raw.name == "latest" or str(run_dir).replace("\\", "/").endswith("runs/latest"):
        latest = Path(project_root) / "runs" / "latest.txt"
        if latest.exists():
            return Path(latest.read_text(encoding="utf-8").strip())
    if raw.is_absolute():
        return raw
    return Path(project_root) / raw


def generate_report(project_root: str | Path, run_dir: str | Path) -> Path:
    run = resolve_run_dir(project_root, run_dir)
    metrics_files = sorted(run.glob("**/rollout/metrics.json"))
    lines = [
        "# ReqAHE Report",
        "",
        f"Run: `{run}`",
        "",
        "## Result Types",
        "",
        "- `internal_holdout_result`: ReqAHE train/val/test split result, used to show evolution did not consume final test feedback.",
        "- `paper_style_result`: frozen harness on the full configured ReqElicitGym scenario dataset. If evolution used any of those scenarios, it is marked `not strictly paper-fair`.",
        "",
        "## Methods",
        "",
        "| method | split | task_mode | max_turns | mean_IRE | mean_TKQR | main_score | mean_turns | interaction_cov | content_cov | style_cov |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metrics_path in metrics_files:
        metrics = read_json(metrics_path)
        meta_path = metrics_path.parent.parent / "run_metadata.json"
        meta = read_json(meta_path) if meta_path.exists() else {}
        cov = metrics.get("type_coverage", {})
        lines.append(
            "| {method} | {split} | {task_mode} | {max_turns} | {ire} | {tkqr} | {score} | {turns} | {interaction} | {content} | {style} |".format(
                method=meta.get("agent", meta.get("method", "unknown")),
                split=meta.get("split", ""),
                task_mode=meta.get("task_mode", ""),
                max_turns=metrics.get("max_turns", meta.get("max_turns", "")),
                ire=metrics.get("mean_IRE", ""),
                tkqr=metrics.get("mean_TKQR", ""),
                score=metrics.get("main_score", ""),
                turns=metrics.get("mean_turns", ""),
                interaction=cov.get("interaction", 0),
                content=cov.get("content", 0),
                style=cov.get("style", 0),
            )
        )
    lines.extend(
        [
            "",
            "## OntoAgent Comparison",
            "",
            f"- Paper target: IRE>{PAPER_TARGET['IRE']}, TKQR>{PAPER_TARGET['TKQR']}.",
            "- `local_ontoagent` is non-blocking. If unavailable, the report records `local_ontoagent_unavailable_reason` and only reports paper target.",
            "- Current LLM oracle/evaluator results are internal benchmark evidence unless produced through the source ReqElicitGym judge/oracle configuration.",
            "",
            "## Fairness Statement",
            "",
            "This implementation distinguishes internal holdout evidence from paper-style full evaluation. A paper-style result is not strictly paper-fair if the evolution loop used any scenario from the configured evaluation dataset as feedback.",
        ]
    )
    unavailable = run / "local_ontoagent_unavailable_reason.txt"
    if unavailable.exists():
        lines.extend(["", "## local_ontoagent_unavailable_reason", "", unavailable.read_text(encoding="utf-8")])
    out = run / "report.md"
    write_text(out, "\n".join(lines) + "\n")
    return out
