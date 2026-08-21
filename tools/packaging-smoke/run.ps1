[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $ArtifactPath,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string] $PublishedSha256,

    [string] $TranscriptPath = (Join-Path (Get-Location) 'packaging-smoke-transcript.json'),

    # The default preserves system-drive scope while bounding the recursive repository scan.
    [string[]] $InspectionRoot = @([IO.Path]::GetFullPath((Join-Path -Path $env:SystemDrive -ChildPath '\'))),

    [ValidateRange(0, 64)]
    [int] $InspectionDepth = 4,

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
    PortOccupied = 24
    DigestMismatch = 31
    InstallationFailed = 32
    AcceptanceBlocked = 40
    AcceptanceFailed = 41
}
$script:results = [System.Collections.Generic.List[object]]::new()
$script:ownedListenerProcessIds = [System.Collections.Generic.List[int]]::new()

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

function Test-UsableFileIdentity {
    param([Parameter(Mandatory)] [IO.FileSystemInfo] $File)

    if ($File.PSIsContainer -or $File.Length -le 0) {
        return $false
    }
    return (($File.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
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
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                continue
            }
            try {
                $file = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
            } catch {
                continue
            }
            if (Test-UsableFileIdentity -File $file) {
                return $file.FullName
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
        $directoryMarker = Get-ChildItem -LiteralPath $root -Force -Recurse -Depth $InspectionDepth -Directory -Filter '.git' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $directoryMarker) {
            return $directoryMarker.FullName
        }
        $fileMarker = Get-ChildItem -LiteralPath $root -Force -Recurse -Depth $InspectionDepth -File -Filter '.git' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $fileMarker) {
            try {
                $file = Get-Item -LiteralPath $fileMarker.FullName -Force -ErrorAction Stop
            } catch {
                $file = $null
            }
            if ($null -ne $file -and (Test-UsableFileIdentity -File $file)) {
                return $file.FullName
            }
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
        inspection_depth = $InspectionDepth
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

function Get-ListeningProcessIdsForPort {
    param([Parameter(Mandatory)] [int] $Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Assert-ProvisioningPortAvailable {
    $listenerProcessIds = @(Get-ListeningProcessIdsForPort -Port 17871)
    if ($listenerProcessIds.Count -ne 0) {
        Add-Result -Step 'provisioning-listener-port-preflight' -Outcome abort -Evidence @{
            finding = 'provisioning_listener_port_occupied'; port = 17871; listener_process_ids = $listenerProcessIds
        }
        Write-Transcript -RunEvidence @{ status = 'aborted'; reason = 'provisioning_listener_port_occupied' }
        exit $ExitCode.PortOccupied
    }
    Add-Result -Step 'provisioning-listener-port-preflight' -Outcome pass -Evidence @{ port = 17871; listener_process_ids = @() }
}

function New-InstallationLayout {
    $runRoot = Join-Path ([IO.Path]::GetTempPath()) ("ofca-packaging-smoke-" + [guid]::NewGuid().ToString('N'))
    $installationPrefix = Join-Path $runRoot 'installation'
    $runtimeDataDirectory = Join-Path $runRoot 'runtime-data'
    New-Item -ItemType Directory -Path $runRoot | Out-Null
    return [pscustomobject]@{
        RunRoot = $runRoot
        InstallationPrefix = $installationPrefix
        RuntimeDataDirectory = $runtimeDataDirectory
    }
}

function Invoke-InstallArtifact {
    param([Parameter(Mandatory)] [psobject] $Layout)

    try {
        $installerProcess = Start-Process -FilePath $ArtifactPath -ArgumentList @(
            '/SILENT', '/SUPPRESSMSGBOXES', ("/DIR=`"" + $Layout.InstallationPrefix + "`"")
        ) -Wait -PassThru
        if ($installerProcess.ExitCode -ne 0) {
            throw "installer exited with code $($installerProcess.ExitCode)"
        }
        $launcherPath = Join-Path -Path $Layout.InstallationPrefix -ChildPath 'Brain.exe'
        $uninstallerPath = Join-Path -Path $Layout.InstallationPrefix -ChildPath 'unins000.exe'
        if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
            throw "installer did not create the required launcher: $launcherPath"
        }
        if (-not (Test-Path -LiteralPath $uninstallerPath -PathType Leaf)) {
            throw "installer did not create the required uninstaller: $uninstallerPath"
        }
        Add-Result -Step 'install-artifact' -Outcome pass -Evidence @{
            artifact_path = (Resolve-Path -LiteralPath $ArtifactPath).Path
            installation_prefix = (Resolve-Path -LiteralPath $Layout.InstallationPrefix).Path
            runtime_data_directory = $Layout.RuntimeDataDirectory
            launcher_path = (Resolve-Path -LiteralPath $launcherPath).Path
            launcher_exists = $true
        }
        return [pscustomobject]@{
            InstallationPrefix = $Layout.InstallationPrefix
            RuntimeDataDirectory = $Layout.RuntimeDataDirectory
            LauncherPath = $launcherPath
            UninstallerPath = $uninstallerPath
        }
    } catch {
        Add-Result -Step 'install-artifact' -Outcome fail -Evidence @{
            finding = 'installation_failed'; exception = $_.Exception.GetType().Name; message = $_.Exception.Message
        }
        return $null
    }
}

function Invoke-OpenBridge {
    param([Parameter(Mandatory)] [psobject] $Installation)

    $launcherPath = $Installation.LauncherPath
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        Add-Result -Step 'open-bridge' -Outcome fail -Evidence @{ finding = 'launcher_missing'; path = $launcherPath }
        return $null
    }
    try {
        $process = Start-Process -FilePath $launcherPath -PassThru
        Add-Result -Step 'open-bridge' -Outcome pass -Evidence @{
            launcher_path = (Resolve-Path -LiteralPath $launcherPath).Path; process_id = $process.Id
        }
        return $process
    } catch {
        Add-Result -Step 'open-bridge' -Outcome fail -Evidence @{ finding = 'launcher_start_failed'; exception = $_.Exception.GetType().Name }
        return $null
    }
}

function Get-DescendantProcessIds {
    param([Parameter(Mandatory)] [int] $ParentProcessId)

    $descendants = [System.Collections.Generic.List[int]]::new()
    foreach ($child in @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $ParentProcessId" -ErrorAction Stop)) {
        $childId = [int] $child.ProcessId
        $descendants.Add($childId)
        foreach ($descendantId in @(Get-DescendantProcessIds -ParentProcessId $childId)) {
            $descendants.Add($descendantId)
        }
    }
    return @($descendants)
}

function Get-LauncherFamilyProcessIds {
    param([Parameter(Mandatory)] [System.Diagnostics.Process] $LauncherProcess)

    return @(@($LauncherProcess.Id) + @(Get-DescendantProcessIds -ParentProcessId $LauncherProcess.Id) | Select-Object -Unique)
}

function Wait-ForProvisioningPortRelease {
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $listenerProcessIds = @(Get-ListeningProcessIdsForPort -Port 17871)
        if ($listenerProcessIds.Count -eq 0) {
            return [pscustomobject]@{ Released = $true; ListenerProcessIds = @() }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return [pscustomobject]@{ Released = $false; ListenerProcessIds = @(Get-ListeningProcessIdsForPort -Port 17871) }
}

function Stop-LauncherProcess {
    param([System.Diagnostics.Process] $LauncherProcess)

    if ($null -eq $LauncherProcess) {
        return
    }
    try {
        $descendantProcessIds = @(Get-DescendantProcessIds -ParentProcessId $LauncherProcess.Id)
        [array]::Reverse($descendantProcessIds)
        $processIds = @($descendantProcessIds) + @($script:ownedListenerProcessIds) + @($LauncherProcess.Id) | Select-Object -Unique
        $stoppedProcessIds = [System.Collections.Generic.List[int]]::new()
        foreach ($processId in $processIds) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -ne $process -and -not $process.HasExited) {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                $stoppedProcessIds.Add($processId)
            }
        }
        $portRelease = Wait-ForProvisioningPortRelease
        if (-not $portRelease.Released) {
            Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{
                finding = 'provisioning_listener_port_not_released'; stopped_process_ids = @($stoppedProcessIds)
                listener_process_ids = @($portRelease.ListenerProcessIds); port_released = $false
            }
            return
        }
        Add-Result -Step 'close-bridge' -Outcome pass -Evidence @{
            stopped_process_ids = @($stoppedProcessIds); port_released = $true; listener_process_ids = @()
        }
    } catch {
        Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{ finding = 'launcher_stop_failed'; exception = $_.Exception.GetType().Name }
    }
}

function Invoke-UninstallArtifact {
    param([psobject] $Installation, [psobject] $Layout)

    try {
        if ($null -ne $Installation) {
            if (-not (Test-Path -LiteralPath $Installation.UninstallerPath -PathType Leaf)) {
                throw "installer cleanup cannot find uninstaller: $($Installation.UninstallerPath)"
            }
            $uninstallerProcess = Start-Process -FilePath $Installation.UninstallerPath -ArgumentList @(
                '/SILENT', '/SUPPRESSMSGBOXES'
            ) -Wait -PassThru
            if ($uninstallerProcess.ExitCode -ne 0) {
                throw "uninstaller exited with code $($uninstallerProcess.ExitCode)"
            }
        }
        if ($null -ne $Layout -and (Test-Path -LiteralPath $Layout.RunRoot -PathType Container)) {
            Remove-Item -LiteralPath $Layout.RunRoot -Recurse -Force
        }
        Add-Result -Step 'uninstall-artifact' -Outcome pass -Evidence @{ removed_temporary_installation = $true }
    } catch {
        Add-Result -Step 'uninstall-artifact' -Outcome fail -Evidence @{ finding = 'uninstall_failed'; exception = $_.Exception.GetType().Name; message = $_.Exception.Message }
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
            $listenerProcessIds = @(Get-ListeningProcessIdsForPort -Port 17871)
            if ($listenerProcessIds.Count -ne 1) {
                $lastFailure = 'listener_owner_not_unique'
                Start-Sleep -Milliseconds 250
                continue
            }
            $ownerProcessId = [int] $listenerProcessIds[0]
            $ownedProcessIds = @(Get-LauncherFamilyProcessIds -LauncherProcess $LauncherProcess)
            $listenerOwnedByLauncher = $ownerProcessId -in $ownedProcessIds
            if (-not $listenerOwnedByLauncher) {
                Add-Result -Step 'provisioning-listener' -Outcome fail -Evidence @{
                    finding = 'listener_owned_by_unrelated_process'; endpoint = 'http://127.0.0.1:17871/health'
                    listener_process_id = $ownerProcessId; launcher_process_id = $LauncherProcess.Id
                    launcher_family_process_ids = $ownedProcessIds
                }
                return $false
            }
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:17871/health' -TimeoutSec 2
            $health = $response.Content | ConvertFrom-Json
            if ($response.StatusCode -eq 200 -and $health.status -eq 'ok') {
                if (-not $script:ownedListenerProcessIds.Contains($ownerProcessId)) {
                    $script:ownedListenerProcessIds.Add($ownerProcessId)
                }
                Add-Result -Step 'provisioning-listener' -Outcome pass -Evidence @{
                    endpoint = 'http://127.0.0.1:17871/health'; status_code = $response.StatusCode; status = $health.status
                    listener_process_id = $ownerProcessId; launcher_process_id = $LauncherProcess.Id
                    launcher_family_process_ids = $ownedProcessIds
                    listener_ownership = $(if ($ownerProcessId -eq $LauncherProcess.Id) { 'launcher' } else { 'launcher_descendant' })
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
Assert-ProvisioningPortAvailable
$layout = New-InstallationLayout
$previousRuntimeDataDirectory = $env:LOCAL_ANALYTICS_DATA_DIR
$env:LOCAL_ANALYTICS_DATA_DIR = $layout.RuntimeDataDirectory
$installation = $null
$launcher = $null
$installationFailed = $false
try {
    $installation = Invoke-InstallArtifact -Layout $layout
    if ($null -eq $installation) {
        $installationFailed = $true
    } else {
        $launcher = Invoke-OpenBridge -Installation $installation
        $listenerReady = Invoke-VerifyProvisioningListener -LauncherProcess $launcher
        Invoke-VerifyInstallationKey -ListenerReady $listenerReady
        Invoke-VerifyProvisioningHandoff -ListenerReady $listenerReady
        Invoke-SubmitInstallationClaim
        Invoke-ConsumeInstallationClaim
    }
} finally {
    Stop-LauncherProcess -LauncherProcess $launcher
    Invoke-UninstallArtifact -Installation $installation -Layout $layout
    $env:LOCAL_ANALYTICS_DATA_DIR = $previousRuntimeDataDirectory
}

if ($installationFailed) {
    Write-Transcript -RunEvidence @{ status = 'failed'; reason = 'installation_failed' }
    exit $ExitCode.InstallationFailed
}

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
