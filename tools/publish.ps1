# Publish a new version to the hot-update channel: upload exe + write the manifest.
# ASCII only: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI, so Chinese
# comments here would corrupt the param block.
#
#   .\tools\publish.ps1 -Version 1.0.3 -Server 203.0.113.10 -Password <ssh-pw>
#   .\tools\publish.ps1 -Version 1.0.16 -Class c1 -Server ... -Password ...
#
# Without -Class the manifest is the shared data/version.txt; with -Class the
# per-class manifest data/rollcall/<cid>.txt is written, so only that class'
# exes see the update.
#
# Prereq: APP_VERSION in main.c already equals -Version, and build.ps1 -Sign has run.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Class = '',
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$HostKey = 'SHA256:W3QMNjEh5BvfrMD23EinE8YyY6ZMHFD/Q2D+jr5wJo8',
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
if ($Class -ne '') { $name = "ClassRollCall_v${Version}_$Class.exe" }
Write-Host ("class     : " + $(if ($Class -ne '') { $Class } else { '(shared manifest)' }))
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

$manifestRemote = "$RemoteDataDir/version.txt"
if ($Class -ne '') { $manifestRemote = "$RemoteDataDir/rollcall/$Class.txt" }
& $plink -ssh -batch -hostkey $HostKey -pw $Password $target "mkdir -p $RemoteDataDir/rollcall" 2>&1 | Out-Null
& $pscp -batch -hostkey $HostKey -pw $Password (Join-Path $stage 'version.txt') ($target + ":$manifestRemote") 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'manifest upload failed' }

& $plink -ssh -batch -hostkey $HostKey -pw $Password $target "chown www:www $manifestRemote; chmod 644 $manifestRemote; chmod 644 $RemoteExeDir/$name" 2>&1 | Out-Host

Write-Host ''
Write-Host '--- server check ---'
try {
    $probe = 'http://example.com/api/rollcall/version'
    if ($Class -ne '') { $probe = "$probe?cid=$Class" }
    (Invoke-WebRequest $probe -UseBasicParsing -TimeoutSec 10).Content | Out-Host
} catch { Write-Host ("manifest ERR: " + $_.Exception.Message) }
