# Иконка Kivi: 512x512, тёмный скруглённый квадрат, оранжевый круг, белая K.
# Только System.Drawing из Windows, без сети и сторонних пакетов.
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
$size = 512
$out = Join-Path $PSScriptRoot 'icon.png'

$bitmap = New-Object System.Drawing.Bitmap($size, $size)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$graphics.Clear([System.Drawing.Color]::Transparent)

$radius = 104
$diameter = $radius * 2
$shape = New-Object System.Drawing.Drawing2D.GraphicsPath
$shape.AddArc(0, 0, $diameter, $diameter, 180, 90)
$shape.AddArc($size - $diameter, 0, $diameter, $diameter, 270, 90)
$shape.AddArc($size - $diameter, $size - $diameter, $diameter, $diameter, 0, 90)
$shape.AddArc(0, $size - $diameter, $diameter, $diameter, 90, 90)
$shape.CloseFigure()

$dark = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 23, 27, 34))
$orange = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 240, 112, 60))
$white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
$graphics.FillPath($dark, $shape)

$circle = 276
$circleOffset = [int](($size - $circle) / 2)
$graphics.FillEllipse($orange, $circleOffset, $circleOffset, $circle, $circle)

$font = New-Object System.Drawing.Font(
    'Segoe UI',
    220,
    [System.Drawing.FontStyle]::Bold,
    [System.Drawing.GraphicsUnit]::Pixel
)
$format = New-Object System.Drawing.StringFormat
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$box = New-Object System.Drawing.RectangleF(0, -6, $size, $size)
$graphics.DrawString('K', $font, $white, $box, $format)

$bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$format.Dispose()
$font.Dispose()
$white.Dispose()
$orange.Dispose()
$dark.Dispose()
$shape.Dispose()
$graphics.Dispose()
$bitmap.Dispose()

Write-Host "icon: $out ($size x $size)"
