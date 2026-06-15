$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "scripts\use_direct_network.ps1")
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$BatchSize = 3
python -m reqahe.cli evolve --split all --task-mode full --dataset-number 1 --iterations 10 --batch-size $BatchSize --rollouts-per-task 1 --max-turns 20 --reflection-mode warn
