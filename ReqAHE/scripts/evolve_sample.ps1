$ErrorActionPreference = "Stop"
$ProjectRoot = "D:\Desktop\科研\.ICSE\projects\ReqAHE"
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli evolve --split train --task-mode sample --iterations 3 --rollouts-per-task 1 --max-turns 15 --middleware-mode warn
