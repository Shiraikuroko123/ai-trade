param(
    [string]$TaskName = 'AI-Trade Research Archive Daily',
    [string]$RunAt = '18:30'
)

$ErrorActionPreference = 'Stop'
$Runner = Join-Path $PSScriptRoot 'run_daily_archive.ps1'
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Archive runner is missing: $Runner"
}

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Persist owner-isolated AI Trade daily and weekly research digests.' -Force
