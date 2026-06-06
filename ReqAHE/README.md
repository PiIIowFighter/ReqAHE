# ReqAHE

RE-AHE is a runnable research prototype for evolving a requirements elicitation interviewer harness, inspired by AHE/NexAU.

The project keeps ReqElicitGym under this repository and treats external TypoAgent / agentic-harness-engineering sources as read-only references.

## Why Harness Evolution

ReqAHE does not directly patch OntoAgent. It starts with a minimal interviewer and evolves the surrounding harness: prompt, tool descriptions, middleware, skills, memory, and manifests. This keeps changes observable, attributable, and reversible.

## AHE to RE Mapping

- Raw trace becomes dialogue trace.
- pass@1 becomes IRE, TKQR, coverage, turn quality, and main_score.
- coding-agent harness components become interviewer prompt, tools, middleware, skills, memory, and optional subagent config.

## Install

From the project root:

```powershell
cd "D:\Desktop\科研\.ICSE\projects\ReqAHE"
python -m pip install -e .
```

If editable install is not available, set:

```powershell
$env:PYTHONPATH="D:\Desktop\科研\.ICSE\projects\ReqAHE\src"
```

## API Configuration

Create `.env` from `.env.example`:

```env
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your_base_url/v1
OPENAI_MODEL=your_model_name
```

Optional role overrides are supported: `INTERVIEWER_MODEL`, `ORACLE_MODEL`, `EVALUATOR_MODEL`, `DEBUGGER_MODEL`, `EVOLVER_MODEL`.

API configuration is required. The interviewer, oracle, evaluator, debugger, and evolver all use the configured OpenAI-compatible API; missing keys, model names, API failures, or invalid JSON responses stop the run.

## Priority Commands

```powershell
python -m reqahe.cli inspect
python -m reqahe.cli run-baseline --agent seed_freeform --split train --task-mode test --dataset-number 1 --rollouts-per-task 1 --max-turns 8
python -m reqahe.cli evolve --split train --task-mode test --dataset-number 1 --iterations 1 --rollouts-per-task 1 --max-turns 8 --middleware-mode warn
python -m reqahe.cli report --run-dir runs/latest
```

Final evaluation interface:

```powershell
python -m reqahe.cli final-eval --best-run latest --split test --task-mode full --dataset-number 1 --rollouts-per-task 1 --max-turns 20
```

`evaluation.dataset_file` defaults to `converted_scenarios.json`, and the current default `evaluation.dataset_number: 1` resolves to `ReqElicitGym\data\converted_scenarios_1.json`. Set `evaluation.dataset_number` or pass `--dataset-number 2` to use files such as `converted_scenarios_2.json`; set `evaluation.dataset_number: null` or pass `--dataset-number base` to use the base `converted_scenarios.json` file directly.

## Results

Runs are written under `runs/<run_name>/`. `runs/latest.txt` points to the latest run.

Each rollout task writes `raw_trace.jsonl`, `clean_trace.json`, `metrics.json`, `conversation.md`, `middleware_events.jsonl`, `evaluator_turn_hits.json`, `agent_prompts.jsonl`, and `agent_actions.jsonl`.

Reports are written to `runs/<run_name>/report.md`.

## Result Types and Fairness

- `internal_holdout_result`: uses ReqAHE's own train/val/test split over the configured converted scenario dataset and demonstrates that evolution did not consume final test feedback.
- `paper_style_result`: freezes the best harness and evaluates on the full configured ReqElicitGym scenario dataset.

If the evolution loop used any scenario from the configured evaluation dataset as feedback, the paper-style result is marked `not strictly paper-fair`. In that case, ReqAHE must not claim a strict paper-fair improvement over OntoAgent. It can only claim runnable automatic evolution or improvement under the current protocol.

## OntoAgent Comparison

The paper target is IRE=0.69 and TKQR=0.59. `local_ontoagent` first checks ontology cache. If cache/import/dependencies fail, ReqAHE records `local_ontoagent_unavailable_reason` and reports only the paper target, not fake local scores.

## Current Status

This implementation prioritizes a runnable closed loop. It does not claim to have truly exceeded OntoAgent. It provides inspect, seed baseline, LLM-driven evolution, attribution artifacts, and reporting.
