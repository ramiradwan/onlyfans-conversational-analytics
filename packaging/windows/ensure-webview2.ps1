# Microsoft Evergreen Runtime is shared with other applications. Never remove
# it on uninstall or package a private browser engine.
[CmdletBinding()]
param([string] $ResultPath = '')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-WebView2Runtime {
    $client = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($path in @(
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$client",
        "Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\$client"
    )) {
        $entry = Get-ItemProperty -LiteralPath $path -Name pv -ErrorAction SilentlyContinue
        if ($entry) {
            $parsed = $null
            if ([Version]::TryParse([string]$entry.pv, [ref]$parsed) -and $parsed -gt [Version]'0.0.0.0') { return $true }
        }
    }
    return $false
}

function Install-WebView2Runtime {
    if (Test-WebView2Runtime) { return }
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('bridge-webview2-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $directory | Out-Null
    $bootstrapper = Join-Path $directory 'MicrosoftEdgeWebview2Setup.exe'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $bootstrapper -TimeoutSec 120
        $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
        if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
            throw 'WebView2 download did not have a valid Microsoft signature'
        }
        $process = Start-Process -FilePath $bootstrapper -ArgumentList '/silent', '/install' -WindowStyle Hidden -PassThru
        try {
            if (-not $process.WaitForExit(180000)) {
                # Stop only the bootstrapper we started, never a shared updater.
                if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(5000) }
                throw 'WebView2 installation did not finish in time'
            }
            if ($process.ExitCode -ne 0 -or -not (Test-WebView2Runtime)) { throw 'WebView2 installation did not complete' }
        } finally { $process.Dispose() }
    } finally {
        # Delete only files created in this private, fixed-purpose temp directory.
        if (Test-Path -LiteralPath $bootstrapper) { Remove-Item -LiteralPath $bootstrapper -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $directory -Force -ErrorAction SilentlyContinue
    }
}

$resultCode = 0
try { Install-WebView2Runtime } catch { Write-Warning $_.Exception.Message; $resultCode = 1 }
if ($ResultPath) {
    $pending = $ResultPath + '.' + [Guid]::NewGuid().ToString('N') + '.pending'
    try {
        [IO.File]::WriteAllText($pending, [string]$resultCode, [Text.Encoding]::ASCII)
        [IO.File]::Move($pending, $ResultPath)
    } finally {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    }
}
exit $resultCode
