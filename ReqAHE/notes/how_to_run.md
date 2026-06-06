# How to Run

```powershell
python -m pip install -e .
python -m reqahe.cli inspect
python -m reqahe.cli run-baseline --agent seed_freeform --split train --task-mode test --dataset-number 1 --rollouts-per-task 1 --max-turns 8
python -m reqahe.cli evolve --split train --task-mode test --dataset-number 1 --iterations 1 --rollouts-per-task 1 --max-turns 8 --middleware-mode warn
python -m reqahe.cli report --run-dir runs/latest
```
