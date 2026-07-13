$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    python -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }
}

& $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "AI Trade requires Python 3.10 or newer" }
& $Python -m pip install --disable-pip-version-check wheel==0.45.1
if ($LASTEXITCODE -ne 0) { throw "Failed to install the pinned build dependency" }
& $Python -m pip install --disable-pip-version-check --no-build-isolation -e $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to install AI Trade" }
& $Python -m unittest discover -s (Join-Path $ProjectRoot 'tests') -v
if ($LASTEXITCODE -ne 0) { throw "AI Trade tests failed" }
