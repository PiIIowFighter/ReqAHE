$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli report --run-dir runs/latest
