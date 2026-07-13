$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Log = Join-Path $ProjectRoot 'logs\scheduled_paper.log'

if (-not (Test-Path $Python)) {
    throw 'Virtual environment is missing. Run scripts\bootstrap.ps1 first.'
}

Push-Location $ProjectRoot
try {
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Python -m ai_trade.cli --config config/default.json paper-run *>> $Log
    $PythonExitCode = $LASTEXITCODE
    if ($PythonExitCode -eq 0) {
        & $Python -m ai_trade.cli --config config/default.json paper-audit *>> $Log
        $PythonExitCode = $LASTEXITCODE
    }
    $CloudEnabled = [Environment]::GetEnvironmentVariable(
        'AI_TRADE_CLOUD_ENABLED', 'User'
    )
    if ($PythonExitCode -eq 0 -and $CloudEnabled -match '^(1|true|yes|on)$') {
        $CloudNames = @(
            'AI_TRADE_CLOUD_ENABLED',
            'AI_TRADE_CLOUD_PREFIX',
            'AI_TRADE_CLOUD_INSTALLATION_ID',
            'AI_TRADE_R2_ENDPOINT',
            'AI_TRADE_R2_REGION',
            'AI_TRADE_R2_BUCKET',
            'AI_TRADE_R2_ACCESS_KEY_ID',
            'AI_TRADE_R2_SECRET_ACCESS_KEY'
        )
        foreach ($Name in $CloudNames) {
            $Value = [Environment]::GetEnvironmentVariable($Name, 'User')
            if ($null -ne $Value) {
                [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
            }
            else {
                [Environment]::SetEnvironmentVariable($Name, $null, 'Process')
            }
        }
        & $Python -m ai_trade.cli --config config/default.json cloud-backup *>> $Log
        if ($LASTEXITCODE -ne 0) {
            Add-Content -LiteralPath $Log -Value 'Cloud backup failed; the local paper run remains valid.'
        }
    }
    $ErrorActionPreference = $PreviousErrorActionPreference
    if ($PythonExitCode -ne 0) {
        throw "AI Trade paper run failed with exit code $PythonExitCode. See $Log"
    }
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
