$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "scripts\use_direct_network.ps1")
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
python -m pip install -e .
$BatchSize = 3
python -m reqahe.cli evolve --split train --task-mode sample --iterations 3 --batch-size $BatchSize --rollouts-per-task 1 --max-turns 15 --reflection-mode warn
