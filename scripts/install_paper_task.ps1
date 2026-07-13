param(
    [string]$TaskName = 'AI-Trade Paper Daily',
    [string]$RunAt = '18:10'
)

$ErrorActionPreference = 'Stop'
$Runner = Join-Path $PSScriptRoot 'run_daily_paper.ps1'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Refresh ETF data and advance the AI-Trade paper account after market close.' -Force
