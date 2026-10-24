# Start the app, bump the pick count, draw, and capture. ASCII only.
param([string]$Exe, [string]$Out, [int]$Ups = 2)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W3 {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

if (-not $Exe) { $Exe = Join-Path $PSScriptRoot '..\dist\release\ClassRollCall.exe' }
if (-not $Out) { $Out = Join-Path $PSScriptRoot '..\preview\03-multi.png' }
$Exe = (Resolve-Path $Exe).Path

$p = Start-Process -FilePath $Exe -PassThru
$h = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 200
    $p.Refresh()
    if ($p.HasExited) { break }
    $h = $p.MainWindowHandle
    if ($h -ne [IntPtr]::Zero) { break }
}
if ($h -eq [IntPtr]::Zero) { Write-Error 'window not found'; exit 1 }

Start-Sleep -Milliseconds 900
[W3]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 250
for ($i = 0; $i -lt $Ups; $i++) {
    [System.Windows.Forms.SendKeys]::SendWait("{UP}")
    Start-Sleep -Milliseconds 150
}

[W3]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 150
[System.Windows.Forms.SendKeys]::SendWait(" ")
Start-Sleep -Milliseconds 1600

$r = New-Object W3+RECT
[W3]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[W3]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc); $g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host ("saved {0}" -f $Out)

Stop-Process -Id $p.Id -Force
