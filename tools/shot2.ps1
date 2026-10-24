# Capture two frames: right after launch (sync state) and after pressing Space (draw).
# ASCII only.
param([string]$Exe, [string]$OutDir)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W2 {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

if (-not $Exe) { $Exe = Join-Path $PSScriptRoot '..\dist\ClassRollCall.exe' }
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\preview' }
$Exe = (Resolve-Path $Exe).Path
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

function Snap($h, $path) {
    $r = New-Object W2+RECT
    [W2]::GetWindowRect($h, [ref]$r) | Out-Null
    $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
    $bmp = New-Object System.Drawing.Bitmap($w, $ht)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    [W2]::PrintWindow($h, $hdc, 2) | Out-Null
    $g.ReleaseHdc($hdc); $g.Dispose()
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host ("saved {0}" -f $path)
}

$p = Start-Process -FilePath $Exe -PassThru

$h = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 200
    $p.Refresh()
    if ($p.HasExited) { break }
    $h = $p.MainWindowHandle
    if ($h -ne [IntPtr]::Zero) { break }
}
if ($h -eq [IntPtr]::Zero) { Write-Error 'window not found'; Stop-Process -Id $p.Id -Force; exit 1 }

Start-Sleep -Milliseconds 1200
[W2]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 300
Snap $h (Join-Path $OutDir '01-online.png')

[W2]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 120
[System.Windows.Forms.SendKeys]::SendWait(" ")
Start-Sleep -Milliseconds 1500
Snap $h (Join-Path $OutDir '02-drawn.png')

[W2]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 120
[System.Windows.Forms.SendKeys]::SendWait(" ")
Start-Sleep -Milliseconds 1500
Snap $h (Join-Path $OutDir '03-drawn2.png')

Stop-Process -Id $p.Id -Force
