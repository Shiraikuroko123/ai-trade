$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Log = Join-Path $ProjectRoot 'logs\scheduled_archive.log'

if (-not (Test-Path $Python)) {
    throw 'Virtual environment is missing. Run scripts\bootstrap.ps1 first.'
}

Push-Location $ProjectRoot
try {
    & $Python -m ai_trade.cli --config config/default.json archive-generate --all-profiles --trigger scheduled *>> $Log
    if ($LASTEXITCODE -ne 0) {
        throw "AI Trade archive generation failed with exit code $LASTEXITCODE. See $Log"
    }
} finally {
    Pop-Location
}
