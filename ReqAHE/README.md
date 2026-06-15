# ReqAHE

ReqAHE is a runnable research prototype for applying Agentic Harness Engineering ideas to conversational requirements elicitation.

The project uses `ReqElicitGym` as the evaluation backend. A requirements elicitation interviewer interacts with an oracle user, receives turn-level judgement, writes traces and metrics, and then uses the observed failures to evolve the interviewer harness.

The current implementation is designed for iterative RE interview harness evolution, not for code-agent harness evolution. It evolves four explicit harness components:

- `system_prompt`: global interviewer behavior rules.
- `skills`: reusable interview strategies. Runtime keeps skill metadata visible and uses the skill router to inject only selected skill bodies.
- `memory`: cross-task experience records grouped by scenario type. Runtime uses the memory router to select at most one relevant memory type.
- `self_reflection`: candidate-level Python runtime checks. It checks generated `question_candidate` and `finish_candidate` actions before they are committed. Warning or enforce events can trigger same-turn retry.

## 1. Project Layout

```text
ReqAHE/
├── README.md
├── pyproject.toml
├── configs/
│   └── default.yaml
├── harness_seed/
│   ├── system_prompt.md
│   ├── skills/
│   ├── memory/
│   └── self_reflection/
├── ReqElicitGym/
│   ├── data/
│   │   ├── converted_scenarios_1.json
│   │   ├── converted_scenarios_2.json
│   │   ├── converted_scenarios_4.json
│   │   ├── converted_scenarios_8.json
│   │   ├── converted_scenarios_12.json
│   │   └── test.json
│   └── env/
├── scripts/
│   ├── evaluate_iterations_on_testset.py
│   ├── evolve_full.ps1
│   ├── evolve_sample.ps1
│   ├── final_eval.ps1
│   ├── inspect.ps1
│   ├── report.ps1
│   └── smoke.ps1
├── src/reqahe/
│   ├── cli.py
│   ├── config.py
│   ├── runtime/
│   ├── evolution/
│   ├── harness/
│   ├── infra/
│   └── reporting/
├── tests/
└── runs/
```

Main source directories:

```text
src/reqahe/runtime/      # interviewer, dataset loading, rollout runner, metrics, reflection
src/reqahe/evolution/    # batching, loop, diagnoser, refiner, attribution, memory update
src/reqahe/harness/      # harness component schema and workspace I/O
src/reqahe/infra/        # LLM client, path handling, I/O, network cleanup
src/reqahe/reporting/    # source inspection and report generation
```

## 2. Installation

From the project root:

```powershell
cd D:\Desktop\科研\.ICSE\projects\ReqAHE
python -m pip install -e .
```

If editable install is unavailable, set `PYTHONPATH` manually:

```powershell
$env:PYTHONPATH="D:\Desktop\科研\.ICSE\projects\ReqAHE\src"
```

For a quick import check:

```powershell
python -m reqahe.cli --help
```

## 3. API Configuration

ReqAHE uses an OpenAI-compatible API client. Create `.env` from `.env.example`:

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

```env
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your_base_url/v1
OPENAI_MODEL=your_model_name
OPENAI_TRUST_ENV=false
OPENAI_NO_PROXY=open.bigmodel.cn,localhost,127.0.0.1,<local>

# Optional role-specific overrides. If unset, OPENAI_MODEL is used.
INTERVIEWER_MODEL=
JUDGE_MODEL=
USER_MODEL=
DIAGNOSER_MODEL=
REFINER_MODEL=
SKILL_ROUTER_MODEL=
MEMORY_ROUTER_MODEL=
MEMORIZER_MODEL=
```

Required values:

```env
OPENAI_API_KEY=...
OPENAI_MODEL=...
```

Usually required:

```env
OPENAI_BASE_URL=...
```

Role-specific model variables are optional. If they are empty, the project uses `OPENAI_MODEL`.

The main roles are:

```text
INTERVIEWER_MODEL       # interviewer action generation
JUDGE_MODEL             # ReqElicitGym judge role
USER_MODEL              # oracle user role
DIAGNOSER_MODEL         # diagnosis over rollout traces
REFINER_MODEL           # harness edit generation
SKILL_ROUTER_MODEL      # runtime skill selection
MEMORY_ROUTER_MODEL     # runtime memory type selection
MEMORIZER_MODEL         # memory writing after rollout_before
```

You can also override API settings from CLI:

```powershell
python -m reqahe.cli inspect --base-url "https://your_base_url/v1" --model "your_model"
```

For commands that actually call LLMs, missing `api_key` or `model` will raise an error.

## 4. Inspect the Project

Run inspection before long experiments:

```powershell
python -m reqahe.cli inspect
```

This checks project sources and dataset paths, and writes inspection notes under `notes/`.

You can specify dataset number:

```powershell
python -m reqahe.cli inspect --dataset-number 1
```

Dataset resolution rules:

```text
--dataset-number 1      -> ReqElicitGym/data/converted_scenarios_1.json
--dataset-number 2      -> ReqElicitGym/data/converted_scenarios_2.json
--dataset-number 4      -> ReqElicitGym/data/converted_scenarios_4.json
--dataset-number 8      -> ReqElicitGym/data/converted_scenarios_8.json
--dataset-number 12     -> ReqElicitGym/data/converted_scenarios_12.json
--dataset-number base   -> ReqElicitGym/data/converted_scenarios.json
```

## 5. Configuration File

Default configuration is in:

```text
configs/default.yaml
```

Important fields:

```yaml
llm:
  base_url: ${OPENAI_BASE_URL}
  api_key: ${OPENAI_API_KEY}
  model: ${OPENAI_MODEL}
  temperature: 0.2
  max_tokens: 6000
  trust_env: false
  timeout: 120
  max_retries: 8

evaluation:
  max_turns: 15
  rollouts_per_task: 1
  dataset_file: converted_scenarios.json
  dataset_number: 1
  task_mode: sample
  split: train
  scenario_count: 0
  seed: 42

evolution:
  iterations: 3
  batch_size: 0
  reflection_mode: warn
```

Runtime routers:

```yaml
runtime:
  skill_router:
    enabled: true
    max_selected_skills: 3
    min_relevance: 0.45

  memory:
    enabled: true
    apply_timing: next_batch
    rollback_policy: no_rollback

  memory_router:
    enabled: true
    max_selected_types: 1
    min_confidence: 0.45

  self_reflection:
    max_retries: 1
    retry_on_modes: ["warn", "enforce"]
```

You can use another config file:

```powershell
python -m reqahe.cli --config configs/default.yaml inspect
```

## 6. Baseline Run

Run the seed free-form interviewer baseline:

```powershell
python -m reqahe.cli run-baseline `
  --agent seed_freeform `
  --split train `
  --task-mode test `
  --dataset-number 1 `
  --rollouts-per-task 1 `
  --max-turns 8 `
  --reflection-mode warn
```

The baseline writes results to:

```text
runs/<run_name>/baseline_seed_freeform/
```

Key outputs:

```text
workspace/                       # copied harness seed
rollout/                         # rollout traces
summary.json                     # metric summary
run_metadata.json                # run metadata
outputs/conversation/*.json      # compatibility conversation output
outputs/metrics/*.json           # compatibility metrics output
```

## 7. Evolution Run

A normal small evolution run:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --scenario-count 21 `
  --dataset-number 1 `
  --iterations 1 `
  --batch-size 3 `
  --rollouts-per-task 1 `
  --max-turns 8 `
  --reflection-mode warn
```

Meaning of important parameters:

```text
--split train              use train split
--task-mode full           use the selected split fully before scenario_count truncation
--scenario-count 21        keep only the first 21 selected scenarios
--iterations 1             run one full pass over selected scenarios
--batch-size 3             split selected scenarios into local batches of 3
--rollouts-per-task 1      run one rollout per scenario
--max-turns 8              maximum interview turns
--reflection-mode warn     reflection warning can be injected for retry
```

With `--scenario-count 21 --batch-size 3 --iterations 1`, one iteration contains 7 batches:

```text
iteration_001/batch_001
iteration_001/batch_002
iteration_001/batch_003
iteration_001/batch_004
iteration_001/batch_005
iteration_001/batch_006
iteration_001/batch_007
```

A larger run can increase `--iterations`, `--scenario-count`, `--max-turns`, or `--rollouts-per-task`.

If `--batch-size` is omitted or set to `0`, all selected scenarios are placed in one batch.

## 8. Evolution Pipeline Semantics

Each batch executes:

```text
1. rollout_before
   Run current harness on this batch's scenarios.

2. memorize
   Extract compact memory points from rollout_before into workspace_memory.

3. diagnoser
   Read rollout_before traces and produce diagnosis artifacts.

4. refiner
   Produce fix_plan.json, proposed_edits.json, validation_report.json,
   and workspace_candidate.

5. rollout_after
   Retest workspace_candidate on the same batch scenarios.

6. attribution
   Compare rollout_before and rollout_after metrics.

7. keep / rollback
   Keep workspace_candidate if retest metrics improve, otherwise roll back
   non-memory harness components.

8. finalize
   Always merge workspace_memory into workspace_after.
```

Memory lifecycle:

```text
rollout_before
  -> memorize
  -> workspace_memory

workspace_memory is NOT injected into the same batch's rollout_after.
workspace_memory does NOT participate in keep/rollback.
workspace_memory is NOT deleted on rollback.
workspace_memory is merged into workspace_after at batch finalize.
The new memory becomes visible only from the next batch or next iteration.
```

Rollback only decides whether non-memory components come from `workspace_candidate` or `workspace_before`.

## 9. Run Directory Structure

Evolution runs are written under:

```text
runs/<run_name>/
```

`runs/latest.txt` points to the latest run.

Typical evolution structure:

```text
runs/<run_name>/
├── run_state.json
├── close_wait_cleanup.jsonl
├── iteration_001/
│   ├── batch_001/
│   │   ├── workspace_before/
│   │   ├── rollout_before/
│   │   ├── workspace_memory/
│   │   ├── memory_lifecycle.json
│   │   ├── memorize_result.json
│   │   ├── analysis/
│   │   │   ├── overview.md
│   │   │   ├── trace_problem_analysis.json
│   │   │   ├── component_localization.json
│   │   │   └── per_task/
│   │   ├── refiner/
│   │   │   ├── STAGE.json
│   │   │   ├── fix_plan.json
│   │   │   ├── proposed_edits.json
│   │   │   └── validation_report.json
│   │   ├── workspace_candidate/
│   │   ├── rollout_after/
│   │   ├── attribution/
│   │   │   ├── metric_deltas.json
│   │   │   ├── task_deltas.csv
│   │   │   └── task_movement.md
│   │   ├── batch_decision.json
│   │   ├── batch_state.json
│   │   └── workspace_after/
│   ├── iteration_metrics.json
│   ├── batch_summary.json
│   ├── workspace/
│   └── run_metadata.json
└── report.md
```

Each rollout task may contain:

```text
raw_trace.jsonl
clean_trace.json
metrics.json
conversation.md
self_reflection_events.jsonl
judgement_turns.json
agent_prompts.jsonl
agent_actions.jsonl
```

`agent_prompts.jsonl` is useful for checking selected skill IDs, prompt digest, memory router result, and router errors.

## 10. Resume an Interrupted Evolution Run

If a run is interrupted, do not delete the run directory. Use one of the following resume methods.

### Method A: `resume-evolve`

```powershell
python -m reqahe.cli resume-evolve `
  "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943"
```

You can also resume from latest:

```powershell
$run = Get-Content runs/latest.txt
python -m reqahe.cli resume-evolve $run
```

To extend or reset the target iteration count during resume:

```powershell
python -m reqahe.cli resume-evolve `
  "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --iterations 2
```

### Method B: `evolve --resume-run-dir`

```powershell
python -m reqahe.cli evolve `
  --resume-run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943"
```

With explicit target iterations:

```powershell
python -m reqahe.cli evolve `
  --resume-run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --iterations 2
```

### What Resume Reuses

Resume reads previous configuration mainly from:

```text
runs/<run_name>/run_state.json
```

If not explicitly overridden by CLI, resume reuses:

```text
split
task_mode
dataset_file
dataset_number
max_turns
rollouts_per_task
model
reflection_mode
batch_size
scenario_count
target_iterations
```

You may still override them from CLI, but for reproducibility it is recommended to keep the same parameters unless you intentionally start a changed continuation.

### What Resume Skips

During resume, completed artifacts are detected and skipped:

```text
rollout_before      skipped if rollout_before/metrics.json and task_results.json are complete
memorize            skipped if memorize_result.json and workspace_memory output are complete
diagnoser           skipped if analysis/overview.md, trace_problem_analysis.json, component_localization.json exist
refiner             skipped if fix_plan.json, proposed_edits.json, validation_report.json, refiner.log and workspace_candidate exist and validation is OK
rollout_after       skipped if rollout_after/metrics.json and task_results.json are complete
attribution         skipped if attribution/task_deltas.csv and metric_deltas.json exist
batch_decision      reused if batch_decision.json exists
```

If a stage is incomplete, failed, interrupted, or missing required artifacts, resume reruns that stage.

### Manual Interrupt During Refiner

If you press `Ctrl+C` during refiner, the code records recoverable state:

```text
batch_state.json
batch_decision.json
rollout_after/STATUS.json
refiner.log
```

Then run resume:

```powershell
python -m reqahe.cli resume-evolve "runs/<run_name>"
```

The interrupted batch can continue from the incomplete stage.

### Recommended Resume Checklist

Before resuming:

```powershell
Get-Content runs/latest.txt
```

Inspect the last batch:

```powershell
Get-ChildItem "runs/<run_name>/iteration_001" -Directory
```

Check batch state:

```powershell
Get-Content "runs/<run_name>/iteration_001/batch_007/batch_state.json"
```

Resume:

```powershell
python -m reqahe.cli resume-evolve "runs/<run_name>"
```

Generate report after completion:

```powershell
python -m reqahe.cli report --run-dir "runs/<run_name>"
```

## 11. Report Generation

Generate report for the latest run:

```powershell
python -m reqahe.cli report --run-dir runs/latest
```

Generate report for a specific run:

```powershell
python -m reqahe.cli report `
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943"
```

Report output:

```text
runs/<run_name>/report.md
```

## 12. Final Evaluation Interface

`final-eval` freezes the selected evolved harness and evaluates it through the ReqElicitGym interface.

Evaluate the latest evolved run on test split:

```powershell
python -m reqahe.cli final-eval `
  --best-run latest `
  --split test `
  --task-mode full `
  --dataset-number 1 `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --reflection-mode warn
```

Evaluate a specific evolved run:

```powershell
python -m reqahe.cli final-eval `
  --best-run "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --split test `
  --task-mode full `
  --dataset-number 1 `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --reflection-mode warn
```

`final-eval` creates a new run directory:

```text
runs/ReqAHE-final_eval-<model>-<timestamp>/final_eval/
```

## 13. Independent Test Evaluation for Every Iteration

After an evolved run completes, evaluate each iteration workspace on the held-out test set:

```powershell
python scripts/evaluate_iterations_on_testset.py `
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --test-data "ReqElicitGym/data/test.json" `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --reflection-mode warn
```

Preview only, without calling LLM:

```powershell
python scripts/evaluate_iterations_on_testset.py `
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --test-data "ReqElicitGym/data/test.json" `
  --dry-run
```

Overwrite previous test outputs:

```powershell
python scripts/evaluate_iterations_on_testset.py `
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --test-data "ReqElicitGym/data/test.json" `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --overwrite
```

Keep detailed rollout traces for debugging:

```powershell
python scripts/evaluate_iterations_on_testset.py `
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260615-180943" `
  --test-data "ReqElicitGym/data/test.json" `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --keep-rollout
```

Outputs are written under each iteration:

```text
iteration_XXX/test_outputs/conversation/
iteration_XXX/test_outputs/metrics/
iteration_XXX/test_outputs/rollout/       # only when --keep-rollout is used
```

These independent test results should not be mixed with internal evolution batch metrics.

## 14. Suggested Experiment Commands

### Smoke Baseline

```powershell
python -m reqahe.cli run-baseline `
  --agent seed_freeform `
  --split train `
  --task-mode test `
  --dataset-number 1 `
  --rollouts-per-task 1 `
  --max-turns 8 `
  --reflection-mode warn
```

### Small Evolution

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --scenario-count 6 `
  --dataset-number 1 `
  --iterations 1 `
  --batch-size 3 `
  --rollouts-per-task 1 `
  --max-turns 8 `
  --reflection-mode warn
```

### Main Evolution Setting

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --scenario-count 21 `
  --dataset-number 1 `
  --iterations 1 `
  --batch-size 3 `
  --rollouts-per-task 1 `
  --max-turns 8 `
  --reflection-mode warn
```

### Continue Main Evolution to Two Iterations

```powershell
python -m reqahe.cli resume-evolve `
  "runs/<run_name>" `
  --iterations 2
```

### Generate Report

```powershell
python -m reqahe.cli report --run-dir "runs/<run_name>"
```

### Independent Test Evaluation

```powershell
python scripts/evaluate_iterations_on_testset.py `
  --run-dir "runs/<run_name>" `
  --test-data "ReqElicitGym/data/test.json" `
  --rollouts-per-task 1 `
  --max-turns 20 `
  --reflection-mode warn
```

## 15. Router and Reflection Options

Disable skill router:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode test `
  --dataset-number 1 `
  --disable-skill-router
```

Disable memory router:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode test `
  --dataset-number 1 `
  --disable-memory-router
```

Adjust skill router:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --dataset-number 1 `
  --max-selected-skills 2 `
  --skill-router-min-relevance 0.5
```

Adjust memory router:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --dataset-number 1 `
  --max-selected-memory-types 1 `
  --memory-router-min-relevance 0.5
```

Reflection modes:

```text
observe   record reflection events only
warn      warnings can be injected into retry prompt
enforce   enforce-mode events can also trigger retry
```

Recommended default:

```powershell
--reflection-mode warn
```

## 16. Result Types

ReqAHE writes two different kinds of results:

```text
internal evolution result
```

This is produced during baseline/evolution on the configured split and task mode. It is useful for harness evolution and diagnosis.

```text
final evaluation result
```

This freezes a selected harness and evaluates it on the selected evaluation split.

For paper-style reporting, keep internal evolution metrics and independent test metrics separate.

## 17. Common Problems

### Missing LLM Config

Error:

```text
Missing required LLM config values: api_key, model
```

Fix `.env`:

```env
OPENAI_API_KEY=your_key
OPENAI_MODEL=your_model_name
OPENAI_BASE_URL=https://your_base_url/v1
```

Or pass CLI overrides:

```powershell
python -m reqahe.cli evolve `
  --api-key "your_key" `
  --base-url "https://your_base_url/v1" `
  --model "your_model_name"
```

### Dataset Not Found

Run:

```powershell
python -m reqahe.cli inspect --dataset-number 1
```

Check that the file exists:

```text
ReqElicitGym/data/converted_scenarios_1.json
```

For the independent test script, check:

```text
ReqElicitGym/data/test.json
```

### Resume Uses Unexpected Settings

Check:

```powershell
Get-Content "runs/<run_name>/run_state.json"
```

If needed, explicitly pass overrides when resuming:

```powershell
python -m reqahe.cli resume-evolve `
  "runs/<run_name>" `
  --iterations 2 `
  --batch-size 3 `
  --scenario-count 21 `
  --max-turns 8 `
  --rollouts-per-task 1 `
  --reflection-mode warn
```

### Refiner Fails Often

Check:

```text
runs/<run_name>/iteration_XXX/batch_YYY/refiner/STAGE.json
runs/<run_name>/iteration_XXX/batch_YYY/refiner/validation_report.json
runs/<run_name>/iteration_XXX/batch_YYY/refiner.log
```

Resume can rerun incomplete or failed refinement stages.

### Retest Failed

Check:

```text
runs/<run_name>/iteration_XXX/batch_YYY/rollout_after_error.log
runs/<run_name>/iteration_XXX/batch_YYY/rollout_after/STATUS.json
```

Then resume:

```powershell
python -m reqahe.cli resume-evolve "runs/<run_name>"
```

### CLOSE_WAIT or Network Resource Issues

The default config enables periodic close-wait cleanup:

```yaml
runtime:
  close_wait_cleanup:
    enabled: true
```

You can disable it for debugging:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode test `
  --dataset-number 1 `
  --disable-close-wait-cleanup
```

Or adjust intervals:

```powershell
python -m reqahe.cli evolve `
  --split train `
  --task-mode full `
  --dataset-number 1 `
  --close-wait-cleanup-interval-tasks 1 `
  --close-wait-cleanup-interval-seconds 180
```

## 18. Development Checks

Run all tests:

```powershell
python -m pytest
```

Run focused tests:

```powershell
python -m pytest `
  tests/test_evolution_batching.py `
  tests/test_reporting_cli_attribution.py `
  tests/test_evaluate_iterations_on_testset.py
```

Compile check:

```powershell
python -m compileall src scripts tests
```

## 19. Current Status

ReqAHE currently provides:

```text
source inspection
seed baseline rollout
LLM-driven diagnosis
LLM-driven harness refinement
batch-level retest and keep/rollback
append-only memory lifecycle
skill and memory routers
candidate-level self-reflection checks
resume for interrupted evolution runs
report generation
independent per-iteration test evaluation
```

The implementation prioritizes a runnable RE-AHE closed loop and reproducible experiment artifacts.
