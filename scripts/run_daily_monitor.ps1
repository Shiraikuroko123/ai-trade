[CmdletBinding()]
param(
    [string]$Config = 'config/default.json',
    [ValidateRange(65536, 1073741824)]
    [int64]$MaxLogBytes = (5 * 1024 * 1024),
    [ValidateRange(1, 20)]
    [int]$KeepLogs = 5
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$Log = Join-Path $LogDirectory 'scheduled_monitor.log'
$ConfigPath = if ([IO.Path]::IsPathRooted($Config)) {
    [IO.Path]::GetFullPath($Config)
} else {
    [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Config))
}
$Utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
$ExitCode = 1
$TemporaryFiles = @()
$ManagedEnvironmentNames = @(
    'PYTHONUTF8',
    'PYTHONIOENCODING',
    'AI_TRADE_CLOUD_ENABLED',
    'AI_TRADE_CLOUD_PREFIX',
    'AI_TRADE_CLOUD_INSTALLATION_ID',
    'AI_TRADE_R2_ENDPOINT',
    'AI_TRADE_R2_REGION',
    'AI_TRADE_R2_BUCKET',
    'AI_TRADE_R2_ACCESS_KEY_ID',
    'AI_TRADE_R2_SECRET_ACCESS_KEY'
)
$OriginalEnvironment = @{}

function Add-MonitorLogText {
    param([AllowEmptyString()][string]$Text)

    if ($null -eq $Text -or $Text.Length -eq 0) {
        return
    }
    [IO.File]::AppendAllText($Log, $Text, $Utf8NoBom)
    if (-not $Text.EndsWith("`n")) {
        [IO.File]::AppendAllText($Log, [Environment]::NewLine, $Utf8NoBom)
    }
}

function Add-MonitorLogLine {
    param([AllowEmptyString()][string]$Text)

    Add-MonitorLogText ($Text + [Environment]::NewLine)
}

function Rotate-MonitorLog {
    if (Test-Path -LiteralPath $Log) {
        $item = Get-Item -LiteralPath $Log
        $rotate = $item.Length -ge $MaxLogBytes
        if (-not $rotate) {
            $stream = [IO.File]::Open($Log, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            try {
                $header = New-Object byte[] 2
                $read = $stream.Read($header, 0, 2)
                # Older PowerShell redirection created UTF-16 logs. Start a new
                # UTF-8 file instead of appending mixed encodings.
                $rotate = $read -ge 2 -and (($header[0] -eq 0xFF -and $header[1] -eq 0xFE) -or ($header[0] -eq 0xFE -and $header[1] -eq 0xFF))
            } finally {
                $stream.Dispose()
            }
        }
        if ($rotate) {
            $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
            $archive = "$Log.$stamp"
            while (Test-Path -LiteralPath $archive) {
                $archive = "$Log.$stamp.$([guid]::NewGuid().ToString('N').Substring(0, 8))"
            }
            Move-Item -LiteralPath $Log -Destination $archive
        }
    }

    $archives = @(
        Get-ChildItem -LiteralPath $LogDirectory -Filter 'scheduled_monitor.log.*' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
    )
    if ($archives.Count -gt $KeepLogs) {
        $archives | Select-Object -Skip $KeepLogs | Remove-Item -Force
    }
}

function Append-ProcessOutput {
    param(
        [string]$Path,
        [string]$StreamName
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -eq 0) {
        return
    }
    $text = $Utf8NoBom.GetString($bytes)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
        $text = $text.Substring(1)
    }
    Add-MonitorLogLine ("[{0}]" -f $StreamName)
    Add-MonitorLogText $text
}

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
Rotate-MonitorLog
Add-MonitorLogLine ("[{0}] monitor runner started; config={1}" -f (Get-Date).ToUniversalTime().ToString('o'), $ConfigPath)

try {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw 'Virtual environment is missing. Run scripts/bootstrap.ps1 first.'
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Configuration file is missing: $ConfigPath"
    }

    foreach ($Name in $ManagedEnvironmentNames) {
        $OriginalEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    foreach ($Entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'AI_TRADE_AI_*' })) {
        if (-not $OriginalEnvironment.ContainsKey($Entry.Name)) {
            $OriginalEnvironment[$Entry.Name] = $Entry.Value
        }
        [Environment]::SetEnvironmentVariable($Entry.Name, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8', 'Process')
    foreach ($Name in $ManagedEnvironmentNames | Where-Object { $_ -notin @('PYTHONUTF8', 'PYTHONIOENCODING') }) {
        $userValue = [Environment]::GetEnvironmentVariable($Name, 'User')
        [Environment]::SetEnvironmentVariable($Name, $userValue, 'Process')
    }

    $suffix = [guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) "ai-trade-monitor-$suffix.stdout"
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) "ai-trade-monitor-$suffix.stderr"
    $TemporaryFiles = @($stdoutPath, $stderrPath)
    $quotedConfig = '"' + $ConfigPath.Replace('"', '\"') + '"'
    $arguments = @('-m', 'ai_trade.cli', '--config', $quotedConfig, 'monitor-scan', '--all-profiles')

    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -Wait `
        -PassThru
    $ExitCode = [int]$process.ExitCode
    Append-ProcessOutput -Path $stdoutPath -StreamName 'stdout'
    Append-ProcessOutput -Path $stderrPath -StreamName 'stderr'
} catch {
    Add-MonitorLogLine ("[{0}] monitor runner failed: {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $_.Exception.Message)
    $ExitCode = 1
} finally {
    foreach ($Name in $OriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $OriginalEnvironment[$Name], 'Process')
    }
    foreach ($Path in $TemporaryFiles) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}

Add-MonitorLogLine ("[{0}] monitor runner finished; exit_code={1}" -f (Get-Date).ToUniversalTime().ToString('o'), $ExitCode)
exit $ExitCode
