# How to Run

```powershell
python -m pip install -e .
python -m reqahe.cli inspect
python -m reqahe.cli run-baseline --agent seed_freeform --split train --task-mode test --dataset-number 1 --rollouts-per-task 1 --max-turns 8 --reflection-mode warn
python -m reqahe.cli evolve --split train --task-mode full --scenario-count 21 --dataset-number 1 --iterations 1 --batch-size 3 --rollouts-per-task 1 --max-turns 8 --reflection-mode warn
python -m reqahe.cli report --run-dir runs/latest
```

Evolution semantics:

- `iterations`: how many full passes to run over the selected scenario set.
- `batch_size`: how many scenarios each local diagnose/refine/retest batch uses inside one iteration.
- Example: `scenario_count=21`, `batch_size=3`, `iterations=1` creates `iteration_001/batch_001` through `batch_007`, then writes `iteration_001/iteration_metrics.json` and `iteration_001/workspace/`.

Memory lifecycle (`runtime.memory.apply_timing = next_batch`):

- The current batch's `rollout_before` produces memory and writes it to `workspace_memory`.
- That memory is not injected into the current batch's `rollout_after`.
- That memory does not participate in the current batch's keep/rollback decision.
- That memory is not deleted when a batch rolls back.
- At batch finalize, memory is merged into `workspace_after`.
- It becomes available to `memory_router` only from the next batch or the next iteration.

Rollback only decides whether non-memory harness components use `workspace_candidate` or `workspace_before`. Memory is an append-only experience record and is always retained.

## Independent Test Evaluation (Post-Evolution)

After an evolved run completes, evaluate each iteration workspace on the held-out test set. This produces **independent test results** and must not be mixed with evolution batch metrics under `batch_*/outputs/`.

```powershell
python scripts/evaluate_iterations_on_testset.py ^
  --run-dir "runs/ReqAHE-evolved_reahe-glm-4.7-20260614-150511" ^
  --test-data "ReqElicitGym/data/test.json" ^
  --rollouts-per-task 1 ^
  --max-turns 20
```

Outputs are written per iteration to:

```text
iteration_XXX/test_outputs/conversation/
iteration_XXX/test_outputs/metrics/
```

Use `--dry-run` to preview planned evaluations without calling the LLM. Use `--overwrite` to rerun iterations that already have complete `test_outputs/`. Use `--keep-rollout` to retain rollout traces under `test_outputs/rollout/` for debugging.
