# Build ClassRollCall.exe (Win32 x86, static, no runtime deps).
# ASCII comments only: Windows PowerShell 5.1 parses BOM-less .ps1 as ANSI.
# NOTE: always call the toolchain with RELATIVE paths while cwd = project root.
#       windres/gcc are ANSI programs and cannot open absolute paths that contain
#       non-ASCII characters (this project lives under D:\<Chinese>\...).
param(
    [switch]$Sign,
    [switch]$KeepSymbols
)

$ErrorActionPreference = 'Continue'   # native tools write warnings to stderr
$root = $PSScriptRoot

# The toolchain must live on a pure-ASCII path: gcc derives its internal
# library paths from argv[0], and the 32-bit ANSI CRT mangles non-ASCII.
$candidates = @(
    $env:RCD_MINGW,
    'D:\rcd-tools\mingw32',
    (Join-Path $root '_tools\mingw32')
) | Where-Object { $_ }
$tools = $null
foreach ($c in $candidates) {
    if (Test-Path (Join-Path $c 'bin\i686-w64-mingw32-gcc.exe')) { $tools = Join-Path $c 'bin'; break }
}
if (-not $tools) { throw "MinGW not found. Set RCD_MINGW to an ASCII path like D:\rcd-tools\mingw32" }
$gcc = Join-Path $tools 'i686-w64-mingw32-gcc.exe'
$windres = Join-Path $tools 'windres.exe'

New-Item -ItemType Directory -Force -Path (Join-Path $root 'dist') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'build') | Out-Null

Write-Host '[1/4] roster'
& node (Join-Path $root 'tools\gen-roster.mjs') | Out-Host

if (-not (Test-Path (Join-Path $root 'res\app.ico'))) {
    Write-Host '[2/4] icon'
    & (Join-Path $root 'tools\gen-icon.ps1')
} else {
    Write-Host '[2/4] icon (cached)'
}

Write-Host '[3/4] windres'
Push-Location $root
try {
    # 版本资源跟着 main.c 的 APP_VERSION 走，免得属性里显示的还是 1.0.0.0
    $mainSrc = [System.IO.File]::ReadAllText((Join-Path $root 'src\main.c'), [System.Text.Encoding]::UTF8)
    $vm = [regex]::Match($mainSrc, '#define\s+APP_VERSION\s+L"(\d+)\.(\d+)\.(\d+)"')
    if (-not $vm.Success) { throw 'APP_VERSION not found in main.c' }
    $verLines = @(
        ('#define VER_MAJOR ' + $vm.Groups[1].Value),
        ('#define VER_MINOR ' + $vm.Groups[2].Value),
        ('#define VER_PATCH ' + $vm.Groups[3].Value),
        ('#define VER_STR "' + $vm.Groups[1].Value + '.' + $vm.Groups[2].Value + '.' + $vm.Groups[3].Value + '\0"')
    )
    [System.IO.File]::WriteAllLines((Join-Path $root 'res\version.h'), $verLines, (New-Object System.Text.UTF8Encoding($false)))

    & $windres -c 65001 -O coff -i res\app.rc -o build\app.res 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'windres failed' }

    Write-Host '[4/4] gcc'
    $flags = @(
        '-O2', '-municode', '-mwindows', '-DUNICODE', '-D_UNICODE',
        '-Wall', '-Wextra',
        '-Wl,--major-subsystem-version,5',
        '-Wl,--minor-subsystem-version,1',
        '-Wl,--nxcompat',
        '-Wl,--dynamicbase',
        '-o', 'dist\ClassRollCall.exe',
        'src\main.c', 'src\net.c', 'src\state.c', 'src\update.c',
        'build\app.res',
        '-lwinhttp', '-liphlpapi', '-limm32', '-lshlwapi',
        '-lgdi32', '-luser32', '-lshell32', '-lole32', '-ladvapi32', '-lws2_32'
    )
    if (-not $KeepSymbols) { $flags = @('-s') + $flags }

    & $gcc @flags 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'gcc failed' }
} finally {
    Pop-Location
}

$out = Get-Item (Join-Path $root 'dist\ClassRollCall.exe')
Write-Host ("OK  {0}  {1:N0} bytes" -f $out.FullName, $out.Length)

if ($Sign) {
    & (Join-Path $root 'tools\sign.ps1')
}
