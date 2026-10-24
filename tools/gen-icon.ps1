# Generates res/app.ico -- an original vector drawing of a Master Ball style icon.
# Every size is drawn at its own resolution (not just downscaled) so 16/32px stay crisp.
# ASCII only: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI.
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = 'Stop'

$root  = Split-Path -Parent $PSScriptRoot
$out   = Join-Path $root 'res\app.ico'
$sizes = @(16, 24, 32, 48, 64, 128, 256)

function Col([int]$a, [int]$r, [int]$g, [int]$b) {
    return [System.Drawing.Color]::FromArgb($a, $r, $g, $b)
}

function New-Frame([int]$n) {
    $bmp = New-Object System.Drawing.Bitmap($n, $n, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)

    $pad = [double]$n * 0.035
    $d   = [double]$n - 2.0 * $pad
    $cx  = [double]$n / 2.0
    $cy  = [double]$n / 2.0
    $rect = New-Object System.Drawing.RectangleF([float]$pad, [float]$pad, [float]$d, [float]$d)

    $dark    = Col 255 26 27 32
    $magenta = Col 255 226 62 155
    $white1  = Col 255 253 253 253
    $white2  = Col 255 230 230 235
    $purple1 = Col 255 128 100 186
    $purple2 = Col 255 82 54 122

    # --- bottom half (white, slightly shaded) ---
    $wGrad = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $white1, $white2, 90.0)
    $g.FillEllipse($wGrad, $rect)

    # --- top half (purple) ---
    $topPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $topPath.AddArc($rect, 180.0, 180.0)
    $topPath.CloseFigure()
    $pGrad = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $purple1, $purple2, 90.0)
    $g.FillPath($pGrad, $topPath)

    # --- black band, clipped to the ball ---
    $circle = New-Object System.Drawing.Drawing2D.GraphicsPath
    $circle.AddEllipse($rect)
    $g.SetClip($circle)
    $bh = [float]($d * 0.120)
    $bandBrush = New-Object System.Drawing.SolidBrush($dark)
    $g.FillRectangle($bandBrush, [float]$pad, [float]($cy - $bh / 2.0), [float]$d, $bh)

    # --- centre button ---
    $rb = [double]$d * 0.125
    $bw = New-Object System.Drawing.SolidBrush($white1)
    $g.FillEllipse($bw, [float]($cx - $rb), [float]($cy - $rb), [float]($rb * 2), [float]($rb * 2))
    $g.ResetClip()

    # --- detail level by size ---
    if ($n -ge 24) {
        $penW = [float]([Math]::Max(1.0, $d * 0.018))
        $outline = New-Object System.Drawing.Pen($dark, $penW)
        $g.DrawEllipse($outline, $rect)
        $outline.Dispose()
    }

    if ($n -ge 24) {
        $rd = [double]$d * 0.062
        $dx = [double]$d * 0.275
        $dy = [double]$d * 0.220
        $dotBrush = New-Object System.Drawing.SolidBrush($magenta)
        $g.FillEllipse($dotBrush, [float]($cx - $dx - $rd), [float]($cy - $dy - $rd), [float]($rd * 2), [float]($rd * 2))
        $g.FillEllipse($dotBrush, [float]($cx + $dx - $rd), [float]($cy - $dy - $rd), [float]($rd * 2), [float]($rd * 2))
        $dotBrush.Dispose()
    }

    if ($n -ge 48) {
        $mw = [double]$d * 0.300
        $mh = [double]$d * 0.250
        $ytop = [double]$cy - [double]($d * 0.345)
        $ybot = $ytop + $mh
        $pts = @(
            (New-Object System.Drawing.PointF([float]($cx - $mw / 2.0), [float]$ybot)),
            (New-Object System.Drawing.PointF([float]($cx - $mw / 2.0), [float]$ytop)),
            (New-Object System.Drawing.PointF([float]$cx,                  [float]($ytop + $mh * 0.52))),
            (New-Object System.Drawing.PointF([float]($cx + $mw / 2.0), [float]$ytop)),
            (New-Object System.Drawing.PointF([float]($cx + $mw / 2.0), [float]$ybot))
        )
        $mPen = New-Object System.Drawing.Pen($magenta, [float]($d * 0.055))
        $mPen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $mPen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
        $mPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $g.DrawLines($mPen, [System.Drawing.PointF[]]$pts)
        $mPen.Dispose()
    }

    $g.Dispose()
    return $bmp
}

function Get-PngBytes($bmp) {
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    return $ms.ToArray()
}

function Get-BmpBytes($bmp) {
    $n = $bmp.Width
    $rect = New-Object System.Drawing.Rectangle(0, 0, $n, $n)
    $data = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $stride = $data.Stride
    $buf = New-Object byte[] ($stride * $n)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $buf, 0, $buf.Length)
    $bmp.UnlockBits($data)

    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    $bw.Write([int]40); $bw.Write([int]$n); $bw.Write([int]($n * 2))
    $bw.Write([int16]1); $bw.Write([int16]32)
    $bw.Write([int]0); $bw.Write([int]($n * $n * 4))
    $bw.Write([int]0); $bw.Write([int]0); $bw.Write([int]0); $bw.Write([int]0)
    for ($y = $n - 1; $y -ge 0; $y--) { $bw.Write($buf, $y * $stride, $n * 4) }
    $maskRow = [int](([Math]::Ceiling($n / 32.0)) * 4)
    $zero = New-Object byte[] ($maskRow * $n)
    $bw.Write($zero, 0, $zero.Length)
    $bw.Flush()
    return $ms.ToArray()
}

$images = @()
foreach ($s in $sizes) {
    $bmp = New-Frame $s
    if ($s -ge 256) { $bytes = Get-PngBytes $bmp } else { $bytes = Get-BmpBytes $bmp }
    $images += [pscustomobject]@{ Size = $s; Bytes = $bytes }
    if ($s -eq 256) {
        $prev = Join-Path $root 'preview\icon-256.png'
        if (-not (Test-Path (Split-Path $prev))) { New-Item -ItemType Directory -Force -Path (Split-Path $prev) | Out-Null }
        $bmp.Save($prev, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    $bmp.Dispose()
}

$fs = [System.IO.File]::Create($out)
$bw = New-Object System.IO.BinaryWriter($fs)
$bw.Write([int16]0); $bw.Write([int16]1); $bw.Write([int16]$images.Count)
$offset = 6 + 16 * $images.Count
foreach ($img in $images) {
    $s = $img.Size
    $dim = 0
    if ($s -lt 256) { $dim = $s }
    $bw.Write([byte]$dim)
    $bw.Write([byte]$dim)
    $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([int16]1); $bw.Write([int16]32)
    $bw.Write([int]$img.Bytes.Length)
    $bw.Write([int]$offset)
    $offset += $img.Bytes.Length
}
foreach ($img in $images) { $bw.Write($img.Bytes, 0, $img.Bytes.Length) }
$bw.Flush(); $fs.Close()

Write-Host ("app.ico written: {0} bytes, {1} frames" -f (Get-Item $out).Length, $images.Count)
