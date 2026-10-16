[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$iconPath = Join-Path $PSScriptRoot '../windows/conversation-analytics.ico'
$assetsRoot = Join-Path $PSScriptRoot 'Assets'
New-Item -ItemType Directory -Path $assetsRoot -Force | Out-Null
$sourceIcon = [Drawing.Icon]::new($iconPath, 256, 256)
$source = $sourceIcon.ToBitmap()
try {
    foreach ($asset in @(@('StoreLogo.png', 50), @('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150))) {
        $size = [int]$asset[1]
        $bitmap = [Drawing.Bitmap]::new($size, $size, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $drawing = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $drawing.Clear([Drawing.Color]::Transparent)
            $drawing.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $drawing.DrawImage($source, [Drawing.Rectangle]::new(0, 0, $size, $size))
            $bitmap.Save((Join-Path $assetsRoot $asset[0]), [Drawing.Imaging.ImageFormat]::Png)
        } finally { $drawing.Dispose(); $bitmap.Dispose() }
    }
} finally { $source.Dispose(); $sourceIcon.Dispose() }
