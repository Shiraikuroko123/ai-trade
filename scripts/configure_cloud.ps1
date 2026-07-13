param(
    [switch]$ImportPaperScout,
    [string]$PaperScoutEnv,
    [ValidateSet("default", "eu", "fedramp")]
    [string]$Jurisdiction = "default",
    [switch]$Disable
)

$ErrorActionPreference = "Stop"

$names = @(
    "AI_TRADE_CLOUD_ENABLED",
    "AI_TRADE_R2_ENDPOINT",
    "AI_TRADE_R2_REGION",
    "AI_TRADE_R2_BUCKET",
    "AI_TRADE_R2_ACCESS_KEY_ID",
    "AI_TRADE_R2_SECRET_ACCESS_KEY"
)

if ($Disable) {
    foreach ($name in $names) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    Write-Host "AI Trade cloud backup is disabled for the current Windows user."
    Write-Host "The non-secret installation ID and prefix were retained for later reconnection."
    exit 0
}

function Read-EnvFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Environment file was not found: $Path"
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches[1]] = $value
        }
    }
    return $values
}

$secretPointer = [IntPtr]::Zero
$secretKey = $null
try {
    if ($ImportPaperScout) {
        if (-not $PaperScoutEnv) {
            throw "-PaperScoutEnv is required when importing an existing Paper Scout profile."
        }
        $source = Read-EnvFile -Path $PaperScoutEnv
        $endpoint = [string]$source["PAPERFIELD_S3_ENDPOINT"]
        $region = [string]$source["PAPERFIELD_S3_REGION"]
        $bucket = [string]$source["PAPERFIELD_S3_BUCKET"]
        $accessKey = [string]$source["PAPERFIELD_S3_ACCESS_KEY_ID"]
        $secretKey = [string]$source["PAPERFIELD_S3_SECRET_ACCESS_KEY"]
    }
    else {
        Write-Host "AI Trade Cloudflare R2 setup"
        Write-Host "Settings are stored only in the current Windows user's environment."
        $accountId = (Read-Host "Cloudflare Account ID").Trim()
        if ($accountId -notmatch '^[a-fA-F0-9]{32}$') {
            throw "Account ID must be a 32-character hexadecimal string."
        }
        $jurisdictionSegment = if ($Jurisdiction -eq "default") { "" } else { ".$Jurisdiction" }
        $endpoint = "https://${accountId}${jurisdictionSegment}.r2.cloudflarestorage.com"
        $region = "auto"
        $bucket = (Read-Host "Private R2 bucket name").Trim()
        $accessKey = (Read-Host "R2 Access Key ID").Trim()
        $secureSecret = Read-Host "R2 Secret Access Key" -AsSecureString
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
        $secretKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    }

    if ($endpoint -notmatch '^https://[a-fA-F0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com/?$') {
        throw "Endpoint must be an account-scoped Cloudflare R2 HTTPS endpoint."
    }
    if ($bucket -notmatch '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$') {
        throw "R2 bucket name is invalid."
    }
    if (-not $accessKey -or -not $secretKey) {
        throw "R2 credentials cannot be empty."
    }
    foreach ($value in @($endpoint, $region, $bucket, $accessKey, $secretKey)) {
        if ($value.Contains("`r") -or $value.Contains("`n") -or $value.Contains([char]0)) {
            throw "Cloud settings cannot contain control characters."
        }
    }

    $installationId = [Environment]::GetEnvironmentVariable(
        "AI_TRADE_CLOUD_INSTALLATION_ID", "User"
    )
    if ($installationId -notmatch '^[a-f0-9]{32}$') {
        $installationId = [Guid]::NewGuid().ToString("N")
    }
    $settings = [ordered]@{
        "AI_TRADE_CLOUD_ENABLED" = "1"
        "AI_TRADE_CLOUD_PREFIX" = "ai-trade"
        "AI_TRADE_CLOUD_INSTALLATION_ID" = $installationId
        "AI_TRADE_R2_ENDPOINT" = $endpoint.TrimEnd("/")
        "AI_TRADE_R2_REGION" = $(if ($region) { $region } else { "auto" })
        "AI_TRADE_R2_BUCKET" = $bucket
        "AI_TRADE_R2_ACCESS_KEY_ID" = $accessKey
        "AI_TRADE_R2_SECRET_ACCESS_KEY" = $secretKey
    }
    foreach ($entry in $settings.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "User")
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }

    Write-Host "Cloud backup is configured for the current Windows user."
    Write-Host "Namespace: ai-trade/$installationId/v1"
    if ($ImportPaperScout) {
        Write-Warning "The imported token came from a plaintext environment file that may be broadly accessible. Review its permissions and rotate it after creating a least-privilege R2 token."
    }
    Write-Host "Restart AI Trade, then run: ai-trade cloud-status --check"
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    $secretKey = $null
}
