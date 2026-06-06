$ErrorActionPreference = "Stop"
$ProjectRoot = "D:\Desktop\科研\.ICSE\projects\ReqAHE"
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli run-baseline --agent seed_freeform --split train --task-mode test --rollouts-per-task 1 --max-turns 8
