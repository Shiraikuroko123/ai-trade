param(
    [string]$TaskName = 'AI-Trade Paper Daily',
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$RunAt = '18:10'
)

$ErrorActionPreference = 'Stop'
$Runner = Join-Path $PSScriptRoot 'run_daily_paper.ps1'
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Paper runner is missing: $Runner"
}

$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 35) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 15)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description 'Reuse or refresh completed ETF bars, then advance and audit the local paper account; never place broker orders.' `
    -Force
