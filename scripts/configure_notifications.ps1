param(
    [switch]$Email,
    [switch]$Desktop,
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'
$emailNames = @(
    'AI_TRADE_EMAIL_SMTP_HOST',
    'AI_TRADE_EMAIL_SMTP_PORT',
    'AI_TRADE_EMAIL_SECURITY',
    'AI_TRADE_EMAIL_USERNAME',
    'AI_TRADE_EMAIL_PASSWORD',
    'AI_TRADE_EMAIL_FROM',
    'AI_TRADE_EMAIL_TO'
)
$desktopNames = @(
    'AI_TRADE_DESKTOP_NOTIFICATIONS',
    'AI_TRADE_DESKTOP_BATCH_SIZE'
)

if ($Disable) {
    foreach ($name in @($emailNames + $desktopNames)) {
        [Environment]::SetEnvironmentVariable($name, $null, 'User')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    Write-Host 'AI Trade email and desktop notification delivery are disabled.'
    exit 0
}

if (-not $Email -and -not $Desktop) {
    throw 'Choose -Email, -Desktop, both switches, or -Disable.'
}

function Set-UserSetting([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

$secretPointer = [IntPtr]::Zero
try {
    if ($Email) {
        $hostName = (Read-Host 'SMTP host').Trim()
        $port = (Read-Host 'SMTP port [587]').Trim()
        if (-not $port) { $port = '587' }
        $security = (Read-Host 'Security starttls or ssl [starttls]').Trim().ToLowerInvariant()
        if (-not $security) { $security = 'starttls' }
        $username = (Read-Host 'SMTP username (leave empty for no authentication)').Trim()
        $sender = (Read-Host 'From address').Trim()
        $recipient = (Read-Host 'Recipient address').Trim()
        if ($hostName.Length -lt 1 -or $hostName.Length -gt 253 -or $hostName -match '\s') {
            throw 'SMTP host is invalid.'
        }
        if ($port -notmatch '^\d{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) {
            throw 'SMTP port is invalid.'
        }
        if ($security -notin @('starttls', 'ssl')) {
            throw 'Security must be starttls or ssl.'
        }
        foreach ($address in @($sender, $recipient)) {
            if ($address -notmatch '^[^\s@]+@[^\s@]+$') {
                throw 'Email addresses must contain one @ and no whitespace.'
            }
        }
        $password = ''
        if ($username) {
            $securePassword = Read-Host 'SMTP password or app password' -AsSecureString
            $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
            $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
            if (-not $password) { throw 'SMTP password cannot be empty when a username is set.' }
        }
        $settings = [ordered]@{
            'AI_TRADE_EMAIL_SMTP_HOST' = $hostName
            'AI_TRADE_EMAIL_SMTP_PORT' = $port
            'AI_TRADE_EMAIL_SECURITY' = $security
            'AI_TRADE_EMAIL_USERNAME' = $username
            'AI_TRADE_EMAIL_PASSWORD' = $password
            'AI_TRADE_EMAIL_FROM' = $sender
            'AI_TRADE_EMAIL_TO' = $recipient
        }
        foreach ($entry in $settings.GetEnumerator()) {
            Set-UserSetting $entry.Key $entry.Value
        }
        Write-Host 'Email delivery is configured for the current Windows user.'
    }

    if ($Desktop) {
        Set-UserSetting 'AI_TRADE_DESKTOP_NOTIFICATIONS' '1'
        Set-UserSetting 'AI_TRADE_DESKTOP_BATCH_SIZE' '20'
        Write-Host 'Windows desktop Toast delivery is enabled for the current user session.'
    }

    Write-Host 'Restart AI Trade and run one monitoring scan to verify delivery.'
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
}
