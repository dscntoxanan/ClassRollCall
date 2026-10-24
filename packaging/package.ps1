# Assemble the release folder in dist\release\ (loose files, ready to copy to a USB stick).
# ASCII only (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
#
#   .\package.ps1           -> dist\release\ with exe + cer + install-cert.bat + README + SHA256
#   .\package.ps1 -Zip      -> also produce the zip
param(
    [string]$Version = '1.0.0',
    [switch]$Zip
)

$ErrorActionPreference = 'Continue'
$root  = $PSScriptRoot | Split-Path -Parent
$dist  = Join-Path $root 'dist'
$rel   = Join-Path $dist 'release'

$exe = Join-Path $dist 'ClassRollCall.exe'
$cer = Join-Path $dist 'ClassRollCall.cer'
if (-not (Test-Path $exe)) { throw "missing $exe (run build.ps1 -Sign first)" }
if (-not (Test-Path $cer)) { throw "missing $cer (run sign.ps1 first)" }

# CRLF is mandatory for .bat and for Windows 7 Notepad.
function ToCrlf([string]$s) { return ($s -replace "`r`n", "`n") -replace "`n", "`r`n" }

if (Test-Path $rel) { Remove-Item $rel -Recurse -Force }
New-Item -ItemType Directory -Force -Path $rel | Out-Null

Copy-Item $exe (Join-Path $rel 'ClassRollCall.exe') -Force
Copy-Item $cer (Join-Path $rel 'ClassRollCall.cer') -Force

# The launchers are Chinese; cmd.exe wants GBK (cp936) on a Chinese Windows.
$gbk = [System.Text.Encoding]::GetEncoding(936)
foreach ($b in @('install-cert.bat', 'uninstall-cert.bat')) {
    $src = [System.IO.File]::ReadAllText((Join-Path $root ('packaging\' + $b)), [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText((Join-Path $rel $b), (ToCrlf $src), $gbk)
}

# README must be UTF-8 with BOM + CRLF for Notepad on Win7.
$txt = [System.IO.File]::ReadAllText((Join-Path $root 'packaging\README.txt'), [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText((Join-Path $rel 'README.txt'), (ToCrlf $txt), (New-Object System.Text.UTF8Encoding($true)))

$hash  = (Get-FileHash (Join-Path $rel 'ClassRollCall.exe') -Algorithm SHA256).Hash
$thumb = (Get-PfxCertificate $cer).Thumbprint
$built = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$lines = @(
    "ClassRollCall v$Version  (Win32 x86, self-signed test build)",
    "SHA-256 (ClassRollCall.exe): $hash",
    ("Certificate thumbprint:      " + $thumb),
    ("Built: " + $built),
    "",
    "Copy this whole folder to the classroom PC, then:",
    "  1) double-click install-cert.bat  (UAC -> Yes; optional, only removes the 'Unknown publisher' label)",
    "  2) double-click ClassRollCall.exe"
)
[System.IO.File]::WriteAllText((Join-Path $rel 'SHA256.txt'), (ToCrlf ($lines -join "`r`n")), (New-Object System.Text.UTF8Encoding($true)))

if ($Zip) {
    $zip = Join-Path $dist ("ClassRollCall_v{0}_win7x86.zip" -f $Version)
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $rel '*') -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("zip   : {0} ({1:N0} bytes)" -f $zip, (Get-Item $zip).Length)
}

Write-Host ("folder: {0}" -f $rel)
Get-ChildItem $rel | ForEach-Object { Write-Host ("  {0,-22} {1,8:N0} bytes" -f $_.Name, $_.Length) }
Write-Host ("sha256: {0}" -f $hash)
