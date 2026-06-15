$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli run-baseline --agent seed_freeform --split train --task-mode test --rollouts-per-task 1 --max-turns 8
