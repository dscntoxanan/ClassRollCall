# Deploy the roll-call API to example.com (nginx reverse proxy -> 127.0.0.1:8120).
# ASCII only (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
#
# Usage:
#   .\tools\deploy.ps1 -Server <server-ip> -Password <ssh-pw> -Token <admin-token>
#
# The admin token is NOT stored in the repo: it is hashed on the way in
# (sha256) and only the hash lives on the server, in secret.json (chmod 600).
param(
    [switch]$ForceRoster,
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$Token,
    [string]$HostKey = 'SHA256:PUT-YOUR-SERVER-HOSTKEY-HERE',
    [string]$RemoteDir = '/opt/rollcall-api'
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent
$plink = Join-Path $root '_tools\plink.exe'
$pscp  = Join-Path $root '_tools\pscp.exe'
if (-not (Test-Path $plink)) { throw "plink.exe missing at $plink" }

if (-not $Token) {
    $bytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $Token = ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLower()
}

$target = "root@$Server"

function Invoke-Remote([string]$cmd) {
    & $plink -ssh -batch -hostkey $HostKey -pw $Password $target $cmd 2>&1 | ForEach-Object { "$_" }
}

function Send-File([string]$local, [string]$remote) {
    & $pscp -batch -hostkey $HostKey -pw $Password $local ($target + ':' + $remote) 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "upload failed: $local" }
}

Write-Host '[1/6] local artifacts'
$names = [System.IO.File]::ReadAllLines((Join-Path $root 'src\roster.txt'), [System.Text.Encoding]::UTF8) |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }

# class name lives in main.c -- keep a single source of truth
$mainSrc = [System.IO.File]::ReadAllText((Join-Path $root 'src\main.c'), [System.Text.Encoding]::UTF8)
$mm = [regex]::Match($mainSrc, '#define\s+CLASS_NAME\s+L"([^"]+)"')
$className = '班级'
if ($mm.Success) { $className = $mm.Groups[1].Value }

$payload = [ordered]@{ class = $className; names = @($names); force = [bool]$ForceRoster }
$namesJson = ConvertTo-Json -InputObject $payload -Compress
$tmp = Join-Path $env:TEMP 'rollcall-names.json'
[System.IO.File]::WriteAllText($tmp, $namesJson, ([System.Text.UTF8Encoding]::new($false)))
Write-Host ("  roster: {0} names, class: {1}" -f $names.Count, $className)

$sha = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($Token))).Replace('-', '').ToLower()

# Reuse the existing cookie secret so redeploying does NOT log everyone out.
# (Every deploy used to regenerate it, which invalidated open admin sessions -> 401.)
$cookieSecret = ''
try {
    $rawSecret = (& $plink -ssh -batch -hostkey $HostKey -pw $Password $target "cat $RemoteDir/secret.json" 2>$null) -join "`n"
    $old = $rawSecret | ConvertFrom-Json
    if ($old.cookie_secret) { $cookieSecret = [string]$old.cookie_secret }
} catch { }
if (-not $cookieSecret) {
    $cb = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($cb)
    $cookieSecret = [System.BitConverter]::ToString($cb).Replace('-', '').ToLower()
    Write-Host '  cookie secret: generated (first deploy)'
} else {
    Write-Host '  cookie secret: reused (sessions kept)'
}
$secret = @{
    token_sha256 = $sha
    sync_key = 'CHANGE-ME-sync-key-32-hex'
    cookie_secret = $cookieSecret
} | ConvertTo-Json -Compress
$secretB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($secret))

Write-Host '[2/6] remote dirs'
Invoke-Remote ("mkdir -p $RemoteDir/data $RemoteDir/logs && chmod 700 $RemoteDir/data") | Out-Host

Write-Host '[3/6] upload files'
# pscp is an ANSI program and cannot open local paths with non-ASCII chars
# (this checkout lives under D:\<Chinese>\...), so stage them on an ASCII path first.
$stage = 'C:\rcd-stage'
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $root 'server\app.py') (Join-Path $stage 'app.py') -Force
Copy-Item (Join-Path $root 'server\rollcall-api.service') (Join-Path $stage 'rollcall-api.service') -Force
Copy-Item (Join-Path $root 'server\nginx-rollcall.conf') (Join-Path $stage 'nginx-rollcall.conf') -Force
Copy-Item $tmp (Join-Path $stage 'names.json') -Force

Send-File (Join-Path $stage 'app.py') "$RemoteDir/app.py"
Send-File (Join-Path $stage 'names.json') "$RemoteDir/data/names.json"
Send-File (Join-Path $stage 'rollcall-api.service') '/etc/systemd/system/rollcall-api.service'
Send-File (Join-Path $stage 'nginx-rollcall.conf') '/etc/nginx/conf.d/rollcall.conf'

Write-Host '[4/6] secret.json + permissions'
Invoke-Remote ("echo '$secretB64' | base64 -d > $RemoteDir/secret.json && chmod 600 $RemoteDir/secret.json && chown -R www:www $RemoteDir && chmod 700 $RemoteDir/data") | Out-Host

Write-Host '[5/6] services'
Invoke-Remote 'systemctl daemon-reload; systemctl enable rollcall-api >/dev/null 2>&1; systemctl restart rollcall-api; sleep 1; systemctl is-active rollcall-api; nginx -t && systemctl reload nginx' | Out-Host

Write-Host '[6/6] verify'
$ErrorActionPreference = 'Stop'
try {
    $r = Invoke-WebRequest 'https://example.com/api/admin' -UseBasicParsing -TimeoutSec 10
    Write-Host ("  GET /api/admin -> {0} ({1} bytes)" -f $r.StatusCode, $r.RawContentLength)
} catch { Write-Host ("  GET /api/admin -> ERR " + $_.Exception.Message) }
try {
    $r = Invoke-WebRequest 'http://example.com/api/health' -UseBasicParsing -TimeoutSec 10
    Write-Host ("  GET /api/health -> {0} {1}" -f $r.StatusCode, $r.Content)
} catch { Write-Host ("  GET /api/health -> ERR " + $_.Exception.Message) }

Write-Host ''
Write-Host '================================================'
Write-Host (' ADMIN URL : https://example.com/api/admin')
Write-Host (' TOKEN     : ' + $Token)
Write-Host '================================================'
