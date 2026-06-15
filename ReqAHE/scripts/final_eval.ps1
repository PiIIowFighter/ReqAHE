$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli final-eval --best-run latest --split test --task-mode full --rollouts-per-task 1 --max-turns 20
