# Publish a new version to the hot-update channel: upload exe + write the manifest.
# ASCII only: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI, so Chinese
# comments here would corrupt the param block.
#
#   .\tools\publish.ps1 -Version 1.0.3 -Server <server-ip> -Password <ssh-pw>
#
# Prereq: APP_VERSION in main.c already equals -Version, and build.ps1 -Sign has run.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$HostKey = 'SHA256:PUT-YOUR-SERVER-HOSTKEY-HERE',
    [string]$RemoteExeDir = '/www/wwwroot/WhiteShadow/download/rollcall',
    [string]$RemoteDataDir = '/www/wwwroot/rollcall-api/data'
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent
$plink = Join-Path $root '_tools\plink.exe'
$pscp  = Join-Path $root '_tools\pscp.exe'
$exe   = Join-Path $root 'dist\release\ClassRollCall.exe'

if (-not (Test-Path $exe)) { throw "missing $exe" }

$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
$name = "ClassRollCall_v$Version.exe"
$stage = 'C:\rcd-stage'
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item $exe (Join-Path $stage $name) -Force

$lines = @(
    "ver=$Version",
    "url=http://example.com/download/rollcall/$name",
    "alt=https://pan.example.com/download/rollcall/$name",
    "sha256=$hash"
)
[System.IO.File]::WriteAllLines((Join-Path $stage 'version.txt'), $lines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ("publishing {0}  sha256={1}" -f $name, $hash)

$target = "root@$Server"

& $plink -ssh -batch -hostkey $HostKey -pw $Password $target "mkdir -p $RemoteExeDir" 2>&1 | Out-Host
& $pscp -batch -hostkey $HostKey -pw $Password (Join-Path $stage $name) ($target + ":$RemoteExeDir/$name") 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'exe upload failed' }

& $pscp -batch -hostkey $HostKey -pw $Password (Join-Path $stage 'version.txt') ($target + ":$RemoteDataDir/version.txt") 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'manifest upload failed' }

& $plink -ssh -batch -hostkey $HostKey -pw $Password $target "chown www:www $RemoteDataDir/version.txt; chmod 644 $RemoteDataDir/version.txt; chmod 644 $RemoteExeDir/$name" 2>&1 | Out-Host

Write-Host ''
Write-Host '--- server check ---'
try {
    (Invoke-WebRequest 'http://example.com/api/rollcall/version' -UseBasicParsing -TimeoutSec 10).Content | Out-Host
} catch { Write-Host ("manifest ERR: " + $_.Exception.Message) }
