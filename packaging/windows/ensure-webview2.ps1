# Microsoft Evergreen Runtime is shared with other applications. Never remove
# it on uninstall or package a private browser engine.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $MinimumVersion,
    [string] $ResultPath = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:webView2Acquisition = [ordered]@{
    phase = 'probe'; outcome = 'fail'; reason = 'probe_failed'; bootstrapper_exit_code = $null
}

function Set-WebView2Acquisition {
    param(
        [ValidateSet('probe','download','signature','launch','wait','readiness','complete')] [string] $Phase,
        [ValidateSet('pass','fail')] [string] $Outcome,
        [ValidateSet('compatible_present','runtime_ready','download_failed','signature_refused','launch_failed',
            'bootstrapper_timeout','bootstrapper_failed','runtime_not_ready','minimum_invalid','probe_failed','unexpected_failure')]
        [string] $Reason
    )
    $script:webView2Acquisition.phase = $Phase
    $script:webView2Acquisition.outcome = $Outcome
    $script:webView2Acquisition.reason = $Reason
}

function Get-WebView2AcquisitionLine {
    $code = if ($null -eq $script:webView2Acquisition.bootstrapper_exit_code) { 'none' }
        else { $script:webView2Acquisition.bootstrapper_exit_code.ToString([Globalization.CultureInfo]::InvariantCulture) }
    return 'phase={0};outcome={1};reason={2};bootstrapper_exit_code={3}' -f
        $script:webView2Acquisition.phase, $script:webView2Acquisition.outcome, $script:webView2Acquisition.reason, $code
}

function Write-WebView2AcquisitionObservation {
    $line = Get-WebView2AcquisitionLine
    Write-Output $line
    if (-not $ResultPath) { return }
    $destination = $ResultPath + '.diagnostics'
    $pending = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.pending'
    try {
        [IO.File]::WriteAllText($pending, $line, [Text.Encoding]::ASCII)
        if ([IO.File]::Exists($destination)) { [IO.File]::Replace($pending, $destination, [NullString]::Value) }
        else { [IO.File]::Move($pending, $destination) }
    } finally {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    }
}

function Get-WebView2InstallClock { return [Diagnostics.Stopwatch]::StartNew() }

function ConvertTo-WebView2Version([string] $Value) {
    $parsed = $null
    if (-not [Version]::TryParse($Value, [ref]$parsed) -or $parsed.Revision -lt 0 -or
        $parsed.Major -eq 0 -or $parsed.ToString(4) -cne $Value -or
        @($parsed.Major, $parsed.Minor, $parsed.Build, $parsed.Revision | Where-Object { $_ -gt 65535 }).Count) {
        return $null
    }
    return $parsed
}

function Test-WebView2Runtime {
    $required = ConvertTo-WebView2Version $MinimumVersion
    if ($null -eq $required) { throw 'WebView2 minimum version is invalid' }
    $client = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($path in @(
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$client",
        "Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\$client"
    )) {
        $entry = Get-ItemProperty -LiteralPath $path -Name pv -ErrorAction SilentlyContinue
        if ($entry) {
            $parsed = ConvertTo-WebView2Version ([string]$entry.pv)
            if ($null -ne $parsed -and $parsed -ge $required) { return $true }
        }
    }
    return $false
}

function Install-WebView2Runtime {
    if ($null -eq (ConvertTo-WebView2Version $MinimumVersion)) {
        Set-WebView2Acquisition probe fail minimum_invalid
        throw 'WebView2 minimum version is invalid'
    }
    if (Test-WebView2Runtime) {
        Set-WebView2Acquisition probe pass compatible_present
        return
    }
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('bridge-webview2-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $directory | Out-Null
    $bootstrapper = Join-Path $directory 'MicrosoftEdgeWebview2Setup.exe'
    try {
        Set-WebView2Acquisition download fail download_failed
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $bootstrapper -TimeoutSec 120
        Set-WebView2Acquisition signature fail signature_refused
        $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
        if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
            throw 'WebView2 download did not have a valid Microsoft signature'
        }
        Set-WebView2Acquisition launch fail launch_failed
        $clock = Get-WebView2InstallClock
        $process = Start-Process -FilePath $bootstrapper -ArgumentList '/silent', '/install' -WindowStyle Hidden -PassThru
        try {
            Set-WebView2Acquisition wait fail unexpected_failure
            $remaining = [int][Math]::Max(0, 180000 - [long]$clock.ElapsedMilliseconds)
            if (-not $process.WaitForExit($remaining)) {
                Set-WebView2Acquisition wait fail bootstrapper_timeout
                # Stop only the bootstrapper we started, never a shared updater.
                if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(5000) }
                throw 'WebView2 installation did not finish in time'
            }
            $exitCode = $process.ExitCode
            if ($exitCode -is [int]) { $script:webView2Acquisition.bootstrapper_exit_code = $exitCode }
            if ($exitCode -isnot [int] -or $exitCode -ne 0) {
                Set-WebView2Acquisition wait fail bootstrapper_failed
                throw 'WebView2 bootstrapper did not complete successfully'
            }
            # The bootstrapper can exit before Edge Update finishes registering
            # its runtime. Admission still requires a compatible version, within
            # the same overall installation budget, never an unbounded wait.
            Set-WebView2Acquisition readiness fail runtime_not_ready
            while ($true) {
                $remaining = [long]180000 - [long]$clock.ElapsedMilliseconds
                if ($remaining -le 0) { throw 'WebView2 compatible runtime was not ready in time' }
                if (Test-WebView2Runtime) {
                    Set-WebView2Acquisition complete pass runtime_ready
                    return
                }
                Start-Sleep -Milliseconds ([int][Math]::Min(250, $remaining))
            }
        } finally { $process.Dispose() }
    } finally {
        # Delete only files created in this private, fixed-purpose temp directory.
        if (Test-Path -LiteralPath $bootstrapper) { Remove-Item -LiteralPath $bootstrapper -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $directory -Force -ErrorAction SilentlyContinue
    }
}

$resultCode = 0
try { Install-WebView2Runtime } catch { $resultCode = 1 }
# Diagnostics are best effort and cannot replace the existing 0/1 result.
try { Write-WebView2AcquisitionObservation } catch { }
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
