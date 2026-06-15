$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "scripts\use_direct_network.ps1")
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
python -m pip install -e .
python -m reqahe.cli inspect
