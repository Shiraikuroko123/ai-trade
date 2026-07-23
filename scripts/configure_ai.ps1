param(
    [switch]$Disable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$names = @(
    "AI_TRADE_AI_BASE_URL",
    "AI_TRADE_AI_MODEL",
    "AI_TRADE_AI_API_KEY",
    "AI_TRADE_AI_TIMEOUT_SECONDS",
    "AI_TRADE_AI_MAX_RETRIES",
    "AI_TRADE_AI_MAX_CONCURRENT_CALLS",
    "AI_TRADE_AI_MAX_TOKENS_PER_CALL",
    "AI_TRADE_AI_DAILY_TOKEN_BUDGET",
    "AI_TRADE_AI_INPUT_COST_PER_MILLION_USD",
    "AI_TRADE_AI_OUTPUT_COST_PER_MILLION_USD",
    "AI_TRADE_AI_DAILY_COST_BUDGET_USD"
)

function Test-LoopbackHost([Uri]$Uri) {
    $hostName = $Uri.DnsSafeHost.Trim().ToLowerInvariant()
    if ($hostName -eq "localhost") {
        return $true
    }

    $address = $null
    if ([Net.IPAddress]::TryParse($hostName, [ref]$address)) {
        return [Net.IPAddress]::IsLoopback($address)
    }
    return $false
}

if ($Disable) {
    foreach ($name in $names) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    Write-Host "AI Trade model mode is disabled for the current Windows user."
    Write-Host "Local assistant mode remains available without an API key."
    Write-Host "Restart the AI Trade workstation so the running process sees the change."
    exit 0
}

$defaultBaseUrl = "https://api.openai.com/v1"
$defaultTimeout = "30"
$secretPointer = [IntPtr]::Zero
$apiKey = $null
$settings = $null

try {
    Write-Host "AI Trade model assistant setup"
    Write-Host "Settings are stored in the current Windows user's environment."
    Write-Host "The API key is not echoed or written to repository files."

    $baseInput = (Read-Host "Base URL [$defaultBaseUrl]").Trim()
    $baseUrl = if ($baseInput) { $baseInput } else { $defaultBaseUrl }
    if ($baseUrl.Contains("`r") -or $baseUrl.Contains("`n") -or $baseUrl.Contains([char]0)) {
        throw "Base URL cannot contain control characters."
    }

    $parsedUri = $null
    if (-not [Uri]::TryCreate($baseUrl, [UriKind]::Absolute, [ref]$parsedUri)) {
        throw "Base URL must be an absolute URL."
    }
    if ($parsedUri.UserInfo -or $parsedUri.Query -or $parsedUri.Fragment) {
        throw "Base URL cannot contain credentials, a query string, or a fragment."
    }
    $scheme = $parsedUri.Scheme.ToLowerInvariant()
    $allowedEndpoint = $scheme -eq "https" -or (
        $scheme -eq "http" -and (Test-LoopbackHost -Uri $parsedUri)
    )
    if (-not $allowedEndpoint) {
        throw "Base URL must use HTTPS; HTTP is allowed only for a loopback host."
    }
    $baseUrl = $baseUrl.TrimEnd("/")

    $currentModel = [Environment]::GetEnvironmentVariable("AI_TRADE_AI_MODEL", "User")
    $modelPrompt = if ($currentModel) { "Model [$currentModel]" } else { "Model" }
    $modelInput = (Read-Host $modelPrompt).Trim()
    $model = if ($modelInput) { $modelInput } else { $currentModel }
    if (-not $model) {
        throw "Model cannot be empty. Enter a model ID supported by the endpoint."
    }
    if ($model.Length -gt 200 -or $model.Contains("`r") -or $model.Contains("`n") -or $model.Contains([char]0)) {
        throw "Model is too long or contains control characters."
    }

    $timeoutInput = (Read-Host "Timeout seconds [$defaultTimeout]").Trim()
    $timeoutText = if ($timeoutInput) { $timeoutInput } else { $defaultTimeout }
    $timeoutSeconds = 0
    if (-not [int]::TryParse($timeoutText, [ref]$timeoutSeconds) -or $timeoutSeconds -lt 1 -or $timeoutSeconds -gt 120) {
        throw "Timeout seconds must be an integer from 1 through 120."
    }

    $maxRetriesInput = (Read-Host "Maximum retries [1]").Trim()
    $maxRetries = 0
    if (-not [int]::TryParse($(if ($maxRetriesInput) { $maxRetriesInput } else { "1" }), [ref]$maxRetries) -or $maxRetries -lt 0 -or $maxRetries -gt 3) {
        throw "Maximum retries must be an integer from 0 through 3."
    }

    $maxConcurrentInput = (Read-Host "Maximum concurrent calls [1]").Trim()
    $maxConcurrent = 0
    if (-not [int]::TryParse($(if ($maxConcurrentInput) { $maxConcurrentInput } else { "1" }), [ref]$maxConcurrent) -or $maxConcurrent -lt 1 -or $maxConcurrent -gt 8) {
        throw "Maximum concurrent calls must be an integer from 1 through 8."
    }

    $maxCallTokensInput = (Read-Host "Maximum accounted tokens per call [50000]").Trim()
    $maxCallTokens = 0
    if (-not [int]::TryParse($(if ($maxCallTokensInput) { $maxCallTokensInput } else { "50000" }), [ref]$maxCallTokens) -or $maxCallTokens -lt 2000 -or $maxCallTokens -gt 10000000) {
        throw "Maximum accounted tokens per call must be from 2000 through 10000000."
    }

    $dailyTokensInput = (Read-Host "Daily accounted token budget in UTC [100000]").Trim()
    $dailyTokens = 0
    if (-not [int]::TryParse($(if ($dailyTokensInput) { $dailyTokensInput } else { "100000" }), [ref]$dailyTokens) -or $dailyTokens -lt 1000 -or $dailyTokens -gt 100000000) {
        throw "Daily accounted token budget must be from 1000 through 100000000."
    }

    $secureKey = Read-Host "API key" -AsSecureString
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    if (-not $apiKey) {
        throw "API key cannot be empty."
    }
    if ($apiKey.Contains("`r") -or $apiKey.Contains("`n") -or $apiKey.Contains([char]0)) {
        throw "API key cannot contain control characters."
    }

    $settings = [ordered]@{
        "AI_TRADE_AI_BASE_URL" = $baseUrl
        "AI_TRADE_AI_MODEL" = $model
        "AI_TRADE_AI_API_KEY" = $apiKey
        "AI_TRADE_AI_TIMEOUT_SECONDS" = [string]$timeoutSeconds
        "AI_TRADE_AI_MAX_RETRIES" = [string]$maxRetries
        "AI_TRADE_AI_MAX_CONCURRENT_CALLS" = [string]$maxConcurrent
        "AI_TRADE_AI_MAX_TOKENS_PER_CALL" = [string]$maxCallTokens
        "AI_TRADE_AI_DAILY_TOKEN_BUDGET" = [string]$dailyTokens
    }
    $previousUser = @{}
    $previousProcess = @{}
    foreach ($name in $names) {
        $previousUser[$name] = [Environment]::GetEnvironmentVariable($name, "User")
        $previousProcess[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }

    try {
        foreach ($entry in $settings.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "User")
            [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
        }
    }
    catch {
        foreach ($name in $names) {
            [Environment]::SetEnvironmentVariable($name, $previousUser[$name], "User")
            [Environment]::SetEnvironmentVariable($name, $previousProcess[$name], "Process")
        }
        throw
    }

    Write-Host "AI Trade model mode is configured for the current Windows user."
    Write-Host "Endpoint: $baseUrl"
    Write-Host "Model: $model"
    Write-Host "Timeout: $timeoutSeconds seconds"
    Write-Host "Governance: $maxRetries retries, $maxConcurrent concurrent call(s), $maxCallTokens tokens/call, $dailyTokens tokens/UTC day"
    Write-Host "Restart the AI Trade workstation before using model mode."
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    if ($null -ne $settings -and $settings.Contains("AI_TRADE_AI_API_KEY")) {
        $settings["AI_TRADE_AI_API_KEY"] = $null
    }
    $apiKey = $null
}
