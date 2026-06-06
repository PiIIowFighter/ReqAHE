$ErrorActionPreference = "Stop"
$ProjectRoot = "D:\Desktop\科研\.ICSE\projects\ReqAHE"
Set-Location $ProjectRoot
python -m pip install -e .
python -m reqahe.cli inspect --project-root $ProjectRoot --reqelicitgym-root "$ProjectRoot\ReqElicitGym"
