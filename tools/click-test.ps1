# Drive the app's stepper (mouse clicks, wheel) and print the persisted count. ASCII only.
param(
    [string]$Exe,
    [int]$Minus = 0,
    [int]$Plus = 0,
    [int]$WheelDown = 0,
    [int]$WheelUp = 0,
    [int]$Btn = 0
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W4 {
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, int d, IntPtr e);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
}
"@

if (-not $Exe) { $Exe = Join-Path $PSScriptRoot '..\dist\ClassRollCall.exe' }
$Exe = (Resolve-Path $Exe).Path

function Read-Count {
    $p = Join-Path $env:APPDATA 'XananRollCall\state.dat'
    if (-not (Test-Path $p)) { return 'no state' }
    $line = Select-String -Path $p -Pattern '^count=' | Select-Object -First 1
    if ($line) { return $line.Line.Trim() }
    return 'no count line'
}

$proc = Start-Process -FilePath $Exe -PassThru
$h = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 200
    $proc.Refresh(); if ($proc.HasExited) { break }
    $h = $proc.MainWindowHandle
    if ($h -ne [IntPtr]::Zero) { break }
}
if ($h -eq [IntPtr]::Zero) { Write-Error 'window not found'; exit 1 }
Start-Sleep -Milliseconds 900
[W4]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 300

function MoveTo([int]$cx, [int]$cy) {
    $pt = New-Object W4+POINT
    $pt.X = $cx; $pt.Y = $cy
    [W4]::ClientToScreen($h, [ref]$pt) | Out-Null
    [W4]::SetCursorPos($pt.X, $pt.Y) | Out-Null
    Start-Sleep -Milliseconds 60
}

function ClickAt([int]$cx, [int]$cy) {
    MoveTo $cx $cy
    [W4]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 50
    [W4]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 140
}

Write-Host ("before: " + (Read-Count))

# focus the window without touching any control
ClickAt 340 120

# layout at 96dpi: minus (209,356), plus (471,356), main button (340,356)
for ($i = 0; $i -lt $Minus; $i++) { ClickAt 209 356 }
Write-Host ("after minus x$Minus : " + (Read-Count))
for ($i = 0; $i -lt $Plus; $i++) { ClickAt 471 356 }
Write-Host ("after plus  x$Plus  : " + (Read-Count))

for ($i = 0; $i -lt $Btn; $i++) { ClickAt 340 356 }
Write-Host ("after button x$Btn : " + (Read-Count))

MoveTo 340 200
for ($i = 0; $i -lt $WheelDown; $i++) { [W4]::mouse_event(0x0800, 0, 0, -120, [IntPtr]::Zero); Start-Sleep -Milliseconds 150 }
Write-Host ("after wheel down x$WheelDown : " + (Read-Count))
for ($i = 0; $i -lt $WheelUp; $i++) { [W4]::mouse_event(0x0800, 0, 0, 120, [IntPtr]::Zero); Start-Sleep -Milliseconds 150 }
Write-Host ("after wheel up   x$WheelUp : " + (Read-Count))

Stop-Process -Id $proc.Id -Force
