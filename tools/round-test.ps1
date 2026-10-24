# Verify the round auto-reset at the minute boundary. ASCII only.
param([string]$Exe, [string]$Out)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W5 {
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, int d, IntPtr e);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
}
"@

if (-not $Exe) { $Exe = Join-Path $PSScriptRoot '..\dist\release\ClassRollCall.exe' }
if (-not $Out) { $Out = Join-Path $PSScriptRoot '..\preview\round-reset.png' }
$Exe = (Resolve-Path $Exe).Path

$p = Start-Process -FilePath $Exe -PassThru
$h = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 200
    $p.Refresh(); if ($p.HasExited) { break }
    $h = $p.MainWindowHandle
    if ($h -ne [IntPtr]::Zero) { break }
}
if ($h -eq [IntPtr]::Zero) { Write-Error 'window not found'; exit 1 }
Start-Sleep -Milliseconds 900
[W5]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 200

function ClickAt([int]$cx, [int]$cy) {
    $pt = New-Object W5+POINT
    $pt.X = $cx; $pt.Y = $cy
    [W5]::ClientToScreen($h, [ref]$pt) | Out-Null
    [W5]::SetCursorPos($pt.X, $pt.Y) | Out-Null
    Start-Sleep -Milliseconds 60
    [W5]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 50
    [W5]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
}

ClickAt 340 356
Start-Sleep -Milliseconds 900
Write-Host ("drawn, waiting for minute boundary. now=" + (Get-Date).ToString('HH:mm:ss'))

# 等到下一分钟 + 3 秒
$sec = (Get-Date).Second
$wait = 60 - $sec + 3
Start-Sleep -Seconds $wait
Write-Host ("woke at " + (Get-Date).ToString('HH:mm:ss'))

$r = New-Object W5+RECT
[W5]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[W5]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc); $g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host ("saved " + $Out)

Stop-Process -Id $p.Id -Force
