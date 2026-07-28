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
$Log = Join-Path $LogDirectory 'scheduled_forward_evidence.log'
$ConfigPath = if ([IO.Path]::IsPathRooted($Config)) {
    [IO.Path]::GetFullPath($Config)
} else {
    [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Config))
}
$Utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
$ExitCode = 1
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
    'AI_TRADE_R2_SECRET_ACCESS_KEY',
    'AI_TRADE_TUSHARE_TOKEN'
)
$OriginalEnvironment = @{}

function Add-ForwardEvidenceLogText {
    param([AllowEmptyString()][string]$Text)

    if ($null -eq $Text -or $Text.Length -eq 0) {
        return
    }
    [IO.File]::AppendAllText($Log, $Text, $Utf8NoBom)
    if (-not $Text.EndsWith("`n")) {
        [IO.File]::AppendAllText($Log, [Environment]::NewLine, $Utf8NoBom)
    }
}

function Add-ForwardEvidenceLogLine {
    param([AllowEmptyString()][string]$Text)

    Add-ForwardEvidenceLogText ($Text + [Environment]::NewLine)
}

function Rotate-ForwardEvidenceLog {
    if (Test-Path -LiteralPath $Log) {
        $Item = Get-Item -LiteralPath $Log
        if ($Item.Length -ge $MaxLogBytes) {
            $Stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
            $Archive = "$Log.$Stamp"
            while (Test-Path -LiteralPath $Archive) {
                $Archive = "$Log.$Stamp.$([guid]::NewGuid().ToString('N').Substring(0, 8))"
            }
            Move-Item -LiteralPath $Log -Destination $Archive
        }
    }

    $Archives = @(
        Get-ChildItem -LiteralPath $LogDirectory -Filter 'scheduled_forward_evidence.log.*' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
    )
    if ($Archives.Count -gt $KeepLogs) {
        $Archives | Select-Object -Skip $KeepLogs | Remove-Item -Force
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
    $Bytes = [IO.File]::ReadAllBytes($Path)
    if ($Bytes.Length -eq 0) {
        return
    }
    $Text = $Utf8NoBom.GetString($Bytes)
    if ($Text.Length -gt 0 -and $Text[0] -eq [char]0xFEFF) {
        $Text = $Text.Substring(1)
    }
    Add-ForwardEvidenceLogLine ("[{0}]" -f $StreamName)
    Add-ForwardEvidenceLogText $Text
}

function Invoke-AiTradeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArguments
    )

    $Suffix = [guid]::NewGuid().ToString('N')
    $StdoutPath = Join-Path ([IO.Path]::GetTempPath()) "ai-trade-forward-$Suffix.stdout"
    $StderrPath = Join-Path ([IO.Path]::GetTempPath()) "ai-trade-forward-$Suffix.stderr"
    $QuotedConfig = '"' + $ConfigPath.Replace('"', '\"') + '"'
    $ProcessArguments = @('-m', 'ai_trade.cli', '--config', $QuotedConfig) + $CommandArguments

    Add-ForwardEvidenceLogLine ("[{0}] starting {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $Label)
    try {
        $Process = Start-Process `
            -FilePath $Python `
            -ArgumentList $ProcessArguments `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -Wait `
            -PassThru
        Append-ProcessOutput -Path $StdoutPath -StreamName "$Label stdout"
        Append-ProcessOutput -Path $StderrPath -StreamName "$Label stderr"
        if ([int]$Process.ExitCode -ne 0) {
            throw "AI Trade command '$Label' failed with exit code $([int]$Process.ExitCode)."
        }
        Add-ForwardEvidenceLogLine ("[{0}] completed {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $Label)
    } finally {
        Remove-Item -LiteralPath $StdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $StderrPath -Force -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
Rotate-ForwardEvidenceLog
Add-ForwardEvidenceLogLine ("[{0}] forward evidence runner started; config={1}" -f (Get-Date).ToUniversalTime().ToString('o'), $ConfigPath)

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
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONIOENCODING', 'utf-8', 'Process')
    foreach ($Name in $ManagedEnvironmentNames | Where-Object { $_ -notin @('PYTHONUTF8', 'PYTHONIOENCODING') }) {
        $UserValue = [Environment]::GetEnvironmentVariable($Name, 'User')
        if ($null -ne $UserValue) {
            [Environment]::SetEnvironmentVariable($Name, $UserValue, 'Process')
        }
    }

    Invoke-AiTradeCommand -Label 'download --force' -CommandArguments @('download', '--force')
    Invoke-AiTradeCommand -Label 'feature-forward-run' -CommandArguments @('feature-forward-run')
    $ExitCode = 0
} catch {
    Add-ForwardEvidenceLogLine ("[{0}] forward evidence runner failed: {1}" -f (Get-Date).ToUniversalTime().ToString('o'), $_.Exception.Message)
    $ExitCode = 1
} finally {
    foreach ($Name in $OriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $OriginalEnvironment[$Name], 'Process')
    }
}

Add-ForwardEvidenceLogLine ("[{0}] forward evidence runner finished; exit_code={1}" -f (Get-Date).ToUniversalTime().ToString('o'), $ExitCode)
exit $ExitCode
