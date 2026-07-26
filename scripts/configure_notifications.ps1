param(
    [switch]$Email,
    [switch]$Desktop,
    [switch]$PushPlus,
    [switch]$DingTalk,
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
$pushplusNames = @(
    'AI_TRADE_PUSHPLUS_TOKEN',
    'AI_TRADE_PUSHPLUS_TOPIC'
)
$dingtalkNames = @(
    'AI_TRADE_DINGTALK_WEBHOOK',
    'AI_TRADE_DINGTALK_SECRET'
)

if ($Disable) {
    foreach ($name in @($emailNames + $desktopNames + $pushplusNames + $dingtalkNames)) {
        [Environment]::SetEnvironmentVariable($name, $null, 'User')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    Write-Host 'AI Trade email, desktop, PushPlus, and DingTalk delivery are disabled.'
    exit 0
}

if (-not $Email -and -not $Desktop -and -not $PushPlus -and -not $DingTalk) {
    throw 'Choose -Email, -Desktop, -PushPlus, -DingTalk, any combination, or -Disable.'
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

    if ($PushPlus) {
        $secureToken = Read-Host 'PushPlus token (from pushplus.plus)' -AsSecureString
        $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
        }
        if ($token -notmatch '^[A-Za-z0-9]{8,64}$') {
            throw 'PushPlus token must be 8 to 64 letters or digits.'
        }
        $topic = (Read-Host 'PushPlus group topic (leave empty for one-to-one push)').Trim()
        if ($topic -and ($topic.Length -gt 64 -or $topic -match '\s')) {
            throw 'PushPlus topic is invalid.'
        }
        Set-UserSetting 'AI_TRADE_PUSHPLUS_TOKEN' $token
        if ($topic) { Set-UserSetting 'AI_TRADE_PUSHPLUS_TOPIC' $topic }
        Write-Host 'PushPlus WeChat delivery is configured for the current Windows user.'
    }

    if ($DingTalk) {
        $webhook = (Read-Host 'DingTalk robot webhook URL').Trim()
        if (-not $webhook.StartsWith('https://oapi.dingtalk.com/robot/send?access_token=')) {
            throw 'DingTalk webhook must start with https://oapi.dingtalk.com/robot/send?access_token='
        }
        if ($webhook.Length -gt 512 -or $webhook -match '\s') {
            throw 'DingTalk webhook URL is invalid.'
        }
        $secureSecret = Read-Host 'DingTalk signing secret SEC... (leave empty to rely on the robot keyword "AI Trade")' -AsSecureString
        $secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
        try {
            $robotSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr)
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr)
        }
        if ($robotSecret -and ($robotSecret.Length -gt 128 -or $robotSecret -match '\s')) {
            throw 'DingTalk secret is invalid.'
        }
        Set-UserSetting 'AI_TRADE_DINGTALK_WEBHOOK' $webhook
        if ($robotSecret) { Set-UserSetting 'AI_TRADE_DINGTALK_SECRET' $robotSecret }
        Write-Host 'DingTalk robot delivery is configured for the current Windows user.'
    }

    Write-Host 'Restart AI Trade and run one monitoring scan to verify delivery.'
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
}
