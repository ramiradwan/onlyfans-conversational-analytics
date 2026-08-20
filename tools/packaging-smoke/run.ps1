[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $ArtifactPath,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string] $PublishedSha256,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $LauncherPath,

    [string] $TranscriptPath = (Join-Path (Get-Location) 'packaging-smoke-transcript.json'),

    [string[]] $InspectionRoot = @($env:SystemDrive),

    [string] $ExecutableSearchPath = $env:Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExitCode = @{
    Success = 0
    InvalidInput = 2
    PythonDetected = 21
    NodeDetected = 22
    RepositoryDetected = 23
    DigestMismatch = 31
    AcceptanceBlocked = 40
    AcceptanceFailed = 41
}
$script:results = [System.Collections.Generic.List[object]]::new()

function Add-Result {
    param(
        [Parameter(Mandatory)] [string] $Step,
        [Parameter(Mandatory)] [ValidateSet('pass', 'blocked', 'fail', 'abort')] [string] $Outcome,
        [Parameter(Mandatory)] [hashtable] $Evidence
    )

    $record = [pscustomobject]([ordered]@{
            step = $Step
            outcome = $Outcome
            evidence = $Evidence
        })
    $script:results.Add($record)
    Write-Host ('[{0}] {1}' -f $Outcome.ToUpperInvariant(), $Step)
}

function Write-Transcript {
    param([hashtable] $RunEvidence)

    $transcript = [pscustomobject]([ordered]@{
            schema_version = 1
            generated_at_utc = [DateTime]::UtcNow.ToString('o')
            artifact = $RunEvidence
            steps = @($script:results)
        })
    $directory = Split-Path -Parent $TranscriptPath
    if ($directory) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    $transcript | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $TranscriptPath -Encoding utf8
    Write-Host ('Transcript: {0}' -f $TranscriptPath)
}

function Complete-Abort {
    param(
        [Parameter(Mandatory)] [string] $Reason,
        [Parameter(Mandatory)] [int] $Code,
        [Parameter(Mandatory)] [hashtable] $Evidence
    )

    Add-Result -Step 'clean-environment' -Outcome abort -Evidence $Evidence
    Write-Transcript -RunEvidence @{ status = 'aborted'; reason = $Reason }
    exit $Code
}

function Find-ExecutableOnSearchPath {
    param([Parameter(Mandatory)] [string[]] $Names)

    $separator = [regex]::Escape([string][IO.Path]::PathSeparator)
    $directories = $ExecutableSearchPath -split $separator | Where-Object { $_ }
    foreach ($directory in $directories) {
        foreach ($name in $Names) {
            $candidate = Join-Path -Path $directory -ChildPath $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }
    return $null
}

function Find-RepositoryCheckout {
    foreach ($root in $InspectionRoot) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            continue
        }
        $directoryMarker = Get-ChildItem -LiteralPath $root -Force -Recurse -Directory -Filter '.git' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $directoryMarker) {
            return $directoryMarker.FullName
        }
        $fileMarker = Get-ChildItem -LiteralPath $root -Force -Recurse -File -Filter '.git' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $fileMarker) {
            return $fileMarker.FullName
        }
    }
    return $null
}

function Assert-CleanEnvironment {
    $python = Find-ExecutableOnSearchPath -Names @('python.exe', 'python', 'py.exe', 'py')
    if ($null -ne $python) {
        Complete-Abort -Reason 'python_detected' -Code $ExitCode.PythonDetected -Evidence @{
            finding = 'python_executable_present'; path = $python
        }
    }
    $node = Find-ExecutableOnSearchPath -Names @('node.exe', 'node')
    if ($null -ne $node) {
        Complete-Abort -Reason 'node_detected' -Code $ExitCode.NodeDetected -Evidence @{
            finding = 'node_executable_present'; path = $node
        }
    }
    $repository = Find-RepositoryCheckout
    if ($null -ne $repository) {
        Complete-Abort -Reason 'repository_detected' -Code $ExitCode.RepositoryDetected -Evidence @{
            finding = 'repository_checkout_present'; path = $repository
        }
    }
    Add-Result -Step 'clean-environment' -Outcome pass -Evidence @{
        executable_search_path = $ExecutableSearchPath
        inspection_roots = @($InspectionRoot)
    }
}

function Assert-ArtifactDigest {
    if (-not (Test-Path -LiteralPath $ArtifactPath -PathType Leaf)) {
        Add-Result -Step 'artifact-digest' -Outcome fail -Evidence @{ finding = 'artifact_missing'; path = $ArtifactPath }
        Write-Transcript -RunEvidence @{ status = 'failed'; reason = 'artifact_missing' }
        exit $ExitCode.InvalidInput
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArtifactPath).Hash.ToLowerInvariant()
    $expected = $PublishedSha256.ToLowerInvariant()
    if ($actual -ne $expected) {
        Add-Result -Step 'artifact-digest' -Outcome fail -Evidence @{
            finding = 'published_digest_mismatch'; path = (Resolve-Path -LiteralPath $ArtifactPath).Path
            expected_sha256 = $expected; actual_sha256 = $actual
        }
        Write-Transcript -RunEvidence @{ status = 'failed'; artifact_path = $ArtifactPath; expected_sha256 = $expected; actual_sha256 = $actual }
        exit $ExitCode.DigestMismatch
    }
    Add-Result -Step 'artifact-digest' -Outcome pass -Evidence @{
        artifact_path = (Resolve-Path -LiteralPath $ArtifactPath).Path; sha256 = $actual
    }
}

function Invoke-OpenBridge {
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        Add-Result -Step 'open-bridge' -Outcome fail -Evidence @{ finding = 'launcher_missing'; path = $LauncherPath }
        return $null
    }
    try {
        $process = Start-Process -FilePath $LauncherPath -PassThru
        Add-Result -Step 'open-bridge' -Outcome pass -Evidence @{
            launcher_path = (Resolve-Path -LiteralPath $LauncherPath).Path; process_id = $process.Id
        }
        return $process
    } catch {
        Add-Result -Step 'open-bridge' -Outcome fail -Evidence @{ finding = 'launcher_start_failed'; exception = $_.Exception.GetType().Name }
        return $null
    }
}

function Invoke-VerifyProvisioningListener {
    param([System.Diagnostics.Process] $LauncherProcess)

    if ($null -eq $LauncherProcess) {
        Add-Result -Step 'provisioning-listener' -Outcome blocked -Evidence @{ reason = 'launcher_did_not_start' }
        return $false
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $lastFailure = 'listener_not_ready'
    do {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:17871/health' -TimeoutSec 2
            $health = $response.Content | ConvertFrom-Json
            if ($response.StatusCode -eq 200 -and $health.status -eq 'ok') {
                Add-Result -Step 'provisioning-listener' -Outcome pass -Evidence @{
                    endpoint = 'http://127.0.0.1:17871/health'; status_code = $response.StatusCode; status = $health.status
                }
                return $true
            }
            $lastFailure = 'unexpected_health_response'
        } catch {
            $lastFailure = $_.Exception.GetType().Name
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    Add-Result -Step 'provisioning-listener' -Outcome fail -Evidence @{ finding = 'provisioning_listener_unavailable'; reason = $lastFailure }
    return $false
}

function Invoke-VerifyInstallationKey {
    param([bool] $ListenerReady)

    $reason = if ($ListenerReady) {
        'no_public_nonsecret_installation-key status is exposed before claim consumption'
    } else {
        'provisioning_listener_is_not_available'
    }
    Add-Result -Step 'installation-key' -Outcome blocked -Evidence @{ reason = $reason }
}

function Invoke-VerifyProvisioningHandoff {
    param([bool] $ListenerReady)

    $reason = if ($ListenerReady) {
        'handoff is browser-session-bound; the harness does not extract or manufacture the launcher secret'
    } else {
        'provisioning_listener_is_not_available'
    }
    Add-Result -Step 'provisioning-handoff' -Outcome blocked -Evidence @{ reason = $reason }
}

function Invoke-SubmitInstallationClaim {
    Add-Result -Step 'submit-installation-claim' -Outcome blocked -Evidence @{
        reason = 'a real claim must be pasted into the browser; the harness never accepts claim material on its command line or transcript'
    }
}

function Invoke-ConsumeInstallationClaim {
    Add-Result -Step 'consume-installation-claim' -Outcome blocked -Evidence @{
        reason = 'requires the browser-submitted claim and a reachable hosted provisioning plane'
    }
}

Assert-CleanEnvironment
Assert-ArtifactDigest
$launcher = Invoke-OpenBridge
$listenerReady = Invoke-VerifyProvisioningListener -LauncherProcess $launcher
Invoke-VerifyInstallationKey -ListenerReady $listenerReady
Invoke-VerifyProvisioningHandoff -ListenerReady $listenerReady
Invoke-SubmitInstallationClaim
Invoke-ConsumeInstallationClaim

$outcomes = @($script:results | Select-Object -ExpandProperty outcome)
if ($outcomes -contains 'fail') {
    Write-Transcript -RunEvidence @{ status = 'failed' }
    exit $ExitCode.AcceptanceFailed
}
if ($outcomes -contains 'blocked') {
    Write-Transcript -RunEvidence @{ status = 'blocked' }
    exit $ExitCode.AcceptanceBlocked
}
Write-Transcript -RunEvidence @{ status = 'passed' }
exit $ExitCode.Success
