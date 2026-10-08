[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $ArtifactPath,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string] $PublishedSha256,

    [string] $TranscriptPath = (Join-Path (Get-Location) 'packaging-smoke-transcript.json'),

    # Optional incremental diagnostics survive an interrupted harness invocation.
    [string] $ProgressPath,

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
$script:ownedProcessRecords = [ordered]@{}
$script:launcherProcessRecord = $null

function Write-SmokeProgress {
    param(
        [Parameter(Mandatory)] [string] $Stage,
        [Parameter(Mandatory)] [ValidateSet('begin', 'end', 'result')] [string] $Phase,
        [hashtable] $Details = @{}
    )

    if (-not $ProgressPath) {
        return
    }
    try {
        $directory = Split-Path -Parent $ProgressPath
        if ($directory) {
            [void] [IO.Directory]::CreateDirectory($directory)
        }
        $record = [ordered]@{
            timestamp_utc = [DateTime]::UtcNow.ToString('o')
            harness_process_id = $PID
            stage = $Stage
            phase = $Phase
            details = $Details
        }
        $line = $record | ConvertTo-Json -Depth 4 -Compress
        # Close the handle after every record instead of buffering until exit.
        [IO.File]::AppendAllText($ProgressPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch {
        # Observability must not change the acceptance decision or interrupt cleanup.
        Write-Warning -Message ('Unable to write smoke progress: {0}' -f $_.Exception.GetType().Name) -WarningAction Continue
    }
}

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
    Write-SmokeProgress -Stage $Step -Phase result -Details @{ outcome = $Outcome }
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
    Write-SmokeProgress -Stage 'harness' -Phase end -Details @{ status = $RunEvidence.status }
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

    Write-SmokeProgress -Stage 'listener-query' -Phase begin -Details @{ port = $Port }
    $listenerProcessIds = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    Write-SmokeProgress -Stage 'listener-query' -Phase end -Details @{ port = $Port; process_ids = $listenerProcessIds }
    return $listenerProcessIds
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
        # /VERYSILENT also suppresses the installation progress window, which
        # /SILENT still displays and which steals desktop focus.
        Write-SmokeProgress -Stage 'installer-wait' -Phase begin
        $installerProcess = Start-Process -FilePath $ArtifactPath -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
            ("/DIR=`"" + $Layout.InstallationPrefix + "`"")
        ) -Wait -PassThru
        Write-SmokeProgress -Stage 'installer-wait' -Phase end -Details @{ exit_code = $installerProcess.ExitCode }
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
    $process = $null
    try {
        Initialize-SmokeProcessInterop
        # -WindowStyle Hidden keeps the launcher console off the desktop; without
        # it Start-Process gives a console launcher its own terminal window.
        Write-SmokeProgress -Stage 'launcher-start' -Phase begin
        $process = Start-Process -FilePath $launcherPath -PassThru -WindowStyle Hidden
        $script:launcherProcessRecord = New-OwnedProcessRecord -Process $process
        $script:ownedProcessRecords[(Get-OwnedProcessIdentityKey -Record $script:launcherProcessRecord)] = $script:launcherProcessRecord
        Write-SmokeProgress -Stage 'launcher-start' -Phase end -Details @{ process_id = $process.Id }
        Add-Result -Step 'open-bridge' -Outcome pass -Evidence @{
            launcher_path = (Resolve-Path -LiteralPath $launcherPath).Path; process_id = $process.Id
        }
        return $process
    } catch {
        if ($null -ne $process -and $null -eq $script:launcherProcessRecord) {
            # Capture failure still leaves the original Start-Process handle owned
            # by this harness. Stop that instance directly, never reopen its PID.
            try {
                [PackagingSmokeNativeProcess]::Stop($process.SafeHandle)
                [void] [PackagingSmokeNativeProcess]::Wait($process.SafeHandle, 5000)
            } catch {
                # The open-bridge failure remains blocking if cleanup also fails.
            } finally {
                $process.Dispose()
            }
        }
        Add-Result -Step 'open-bridge' -Outcome fail -Evidence @{ finding = 'launcher_start_failed'; exception = $_.Exception.GetType().Name }
        return $null
    }
}

function Initialize-SmokeProcessInterop {
    if ('PackagingSmokeNativeProcess' -as [type]) {
        return
    }
    # All observations, termination and waits use the same retained native handle.
    # No cleanup operation reopens an authority-bearing handle from a numeric PID.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class PackagingSmokeNativeProcess {
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(SafeProcessHandle process, out long creation,
        out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetProcessId(SafeProcessHandle process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeProcessHandle process, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(SafeProcessHandle process, uint exitCode);

    public static long[] ReadLifetime(SafeProcessHandle process) {
        long creation, exit, kernel, user;
        uint id = GetProcessId(process);
        if (id == 0 || !GetProcessTimes(process, out creation, out exit, out kernel, out user))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        uint state = WaitForSingleObject(process, 0);
        if (state != 0 && state != 258)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        if (state == 0 && exit == 0 &&
            !GetProcessTimes(process, out creation, out exit, out kernel, out user))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        if (creation <= 0 || (state == 0 && exit <= 0))
            throw new InvalidOperationException("Process lifetime unavailable.");
        return new long[] { DateTime.FromFileTimeUtc(creation).Ticks,
            exit == 0 ? 0 : DateTime.FromFileTimeUtc(exit).Ticks, state == 0 || exit > 0 ? 1 : 0, id };
    }

    public static void Stop(SafeProcessHandle process) {
        if (WaitForSingleObject(process, 0) == 0)
            return;
        if (!TerminateProcess(process, 1)) {
            int error = Marshal.GetLastWin32Error();
            if (WaitForSingleObject(process, 0) != 0)
                throw new Win32Exception(error);
        }
    }

    public static bool Wait(SafeProcessHandle process, int milliseconds) {
        uint state = WaitForSingleObject(process, (uint)Math.Max(0, milliseconds));
        if (state == 0) return true;
        if (state == 258) return false;
        throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
}

function Get-OwnedProcessIdentityKey {
    param([Parameter(Mandatory)] [psobject] $Record)

    return ('{0}:{1}' -f [int] $Record.ProcessId, [long] $Record.CreationUtcTicks)
}

function New-OwnedProcessRecord {
    param([Parameter(Mandatory)] $Process)

    Initialize-SmokeProcessInterop
    $handle = $Process.SafeHandle
    $lifetime = [PackagingSmokeNativeProcess]::ReadLifetime($handle)
    if ([int] $lifetime[3] -ne [int] $Process.Id) {
        throw [InvalidOperationException]::new('Process identity unavailable.')
    }
    return [pscustomobject]@{
        ProcessId = [int] $lifetime[3]
        CreationUtcTicks = [long] $lifetime[0]
        Process = $Process
        Handle = $handle
    }
}

function Resolve-SmokeProcessRecord {
    param([Parameter(Mandatory)] [int] $ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    try {
        return New-OwnedProcessRecord -Process $process
    } catch {
        $process.Dispose()
        throw
    }
}

function Get-OwnedProcessLifetime {
    param([Parameter(Mandatory)] [psobject] $Record)

    $lifetime = [PackagingSmokeNativeProcess]::ReadLifetime($Record.Handle)
    if ([int] $lifetime[3] -ne [int] $Record.ProcessId -or [long] $lifetime[0] -ne [long] $Record.CreationUtcTicks) {
        throw [InvalidOperationException]::new('Process identity changed.')
    }
    return [pscustomobject]@{
        CreationUtcTicks = [long] $lifetime[0]
        ExitUtcTicks = [long] $lifetime[1]
        HasExited = [bool] $lifetime[2]
    }
}

function Close-OwnedProcessRecord {
    param([Parameter(Mandatory)] [psobject] $Record)

    $Record.Process.Dispose()
}

function Stop-OwnedProcessRecord {
    param([Parameter(Mandatory)] [psobject] $Record)

    [void] (Get-OwnedProcessLifetime -Record $Record)
    [PackagingSmokeNativeProcess]::Stop($Record.Handle)
}

function Wait-OwnedProcessRecord {
    param([Parameter(Mandatory)] [psobject] $Record, [int] $RemainingMilliseconds)

    [void] (Get-OwnedProcessLifetime -Record $Record)
    return [PackagingSmokeNativeProcess]::Wait($Record.Handle, $RemainingMilliseconds)
}

function Get-SmokeProcessChildren {
    param([Parameter(Mandatory)] [int] $ParentProcessId, [Parameter(Mandatory)] [datetime] $DeadlineUtc)

    $remainingSeconds = ($DeadlineUtc - [DateTime]::UtcNow).TotalSeconds
    if ($remainingSeconds -lt 1) {
        throw [TimeoutException]::new('Process family deadline reached.')
    }
    # Zero means the server default, so never pass zero or renew the probe deadline.
    $operationSeconds = [uint32] [Math]::Min(2, [Math]::Floor($remainingSeconds))
    return @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $ParentProcessId" `
        -Property ProcessId, ParentProcessId, CreationDate -OperationTimeoutSec $operationSeconds -ErrorAction Stop)
}

function Get-LauncherFamilyRecords {
    param(
        [Parameter(Mandatory)] [psobject] $LauncherRecord,
        [Parameter(Mandatory)] [datetime] $DeadlineUtc,
        [System.Collections.IDictionary] $OwnedRecords = $script:ownedProcessRecords,
        [scriptblock] $QueryChildren = { param($parentId, $deadline) Get-SmokeProcessChildren -ParentProcessId $parentId -DeadlineUtc $deadline },
        [scriptblock] $ResolveProcess = { param($processId) Resolve-SmokeProcessRecord -ProcessId $processId },
        [scriptblock] $ReadLifetime = { param($record) Get-OwnedProcessLifetime -Record $record },
        [scriptblock] $ReleaseRecord = { param($record) Close-OwnedProcessRecord -Record $record },
        [scriptblock] $UtcNow = { [DateTime]::UtcNow }
    )

    $rootKey = Get-OwnedProcessIdentityKey -Record $LauncherRecord
    $OwnedRecords[$rootKey] = $LauncherRecord
    $family = [ordered]@{ $rootKey = $LauncherRecord }
    $visited = [System.Collections.Generic.HashSet[string]]::new()
    $identityByProcessId = @{ ([int] $LauncherRecord.ProcessId) = $rootKey }
    $pending = [System.Collections.Generic.Queue[object]]::new()
    $pending.Enqueue($LauncherRecord)
    while ($pending.Count -gt 0) {
        if ((& $UtcNow) -ge $DeadlineUtc) {
            throw [TimeoutException]::new('Process family deadline reached.')
        }
        $parent = $pending.Dequeue()
        $parentKey = Get-OwnedProcessIdentityKey -Record $parent
        if (-not $visited.Add($parentKey)) {
            continue
        }
        $parentLifetime = & $ReadLifetime $parent
        if ([long] $parentLifetime.CreationUtcTicks -ne [long] $parent.CreationUtcTicks) {
            throw [InvalidOperationException]::new('Parent identity changed.')
        }
        Write-SmokeProgress -Stage 'process-family-query' -Phase begin -Details @{ parent_process_id = $parent.ProcessId }
        $children = @(& $QueryChildren ([int] $parent.ProcessId) $DeadlineUtc)
        Write-SmokeProgress -Stage 'process-family-query' -Phase end -Details @{
            parent_process_id = $parent.ProcessId; process_ids = @($children | ForEach-Object { [int] $_.ProcessId })
        }
        if ((& $UtcNow) -ge $DeadlineUtc) {
            throw [TimeoutException]::new('Process family deadline reached.')
        }
        foreach ($child in $children) {
            if ((& $UtcNow) -ge $DeadlineUtc) {
                throw [TimeoutException]::new('Process family deadline reached.')
            }
            $candidate = $null
            $retained = $false
            try {
                if ($null -eq $child -or $null -eq $child.PSObject.Properties['ProcessId'] -or
                    $null -eq $child.PSObject.Properties['ParentProcessId'] -or $null -eq $child.PSObject.Properties['CreationDate'] -or
                    [int] $child.ProcessId -le 0 -or [int] $child.ParentProcessId -ne [int] $parent.ProcessId -or
                    $null -eq $child.CreationDate -or $child.CreationDate -isnot [datetime] -or
                    $child.CreationDate.Kind -eq [DateTimeKind]::Unspecified) {
                    continue
                }
                $cimCreationTicks = [long] $child.CreationDate.ToUniversalTime().Ticks
                try {
                    $candidate = & $ResolveProcess ([int] $child.ProcessId)
                    if ($null -ne $candidate) {
                        $candidateLifetime = & $ReadLifetime $candidate
                    }
                } catch {
                    # A vanished or inaccessible candidate never grants ownership.
                    continue
                }
                if ($null -eq $candidate -or [int] $candidate.ProcessId -ne [int] $child.ProcessId) {
                    continue
                }
                $creationTicks = [long] $candidate.CreationUtcTicks
                # CIM_DATETIME resolves microseconds; native FILETIME resolves 100ns.
                if ($candidateLifetime.HasExited -or [long] $candidateLifetime.CreationUtcTicks -ne $creationTicks -or
                    ($cimCreationTicks - ($cimCreationTicks % 10)) -ne ($creationTicks - ($creationTicks % 10))) {
                    continue
                }
                # Re-read the original parent handle after resolving the child. A PID
                # reused after that parent exited cannot authorize its new children.
                $parentLifetime = & $ReadLifetime $parent
                if ([long] $parentLifetime.CreationUtcTicks -ne [long] $parent.CreationUtcTicks -or
                    $creationTicks -lt [long] $parentLifetime.CreationUtcTicks -or
                    ($parentLifetime.HasExited -and ($parentLifetime.ExitUtcTicks -le 0 -or $creationTicks -gt [long] $parentLifetime.ExitUtcTicks))) {
                    continue
                }
                $candidateKey = Get-OwnedProcessIdentityKey -Record $candidate
                if ($identityByProcessId.ContainsKey([int] $candidate.ProcessId) -and
                    $identityByProcessId[[int] $candidate.ProcessId] -ne $candidateKey) {
                    throw [InvalidOperationException]::new('Ambiguous process family identity.')
                }
                $identityByProcessId[[int] $candidate.ProcessId] = $candidateKey
                if ($OwnedRecords.Contains($candidateKey) -and
                    [Object]::ReferenceEquals($OwnedRecords[$candidateKey], $candidate)) {
                    $retained = $true
                }
                if ($family.Contains($candidateKey)) {
                    continue
                }
                if ($OwnedRecords.Contains($candidateKey)) {
                    $existing = $OwnedRecords[$candidateKey]
                    if (-not [Object]::ReferenceEquals($existing, $candidate)) {
                        [void] (& $ReleaseRecord $candidate)
                    }
                    $candidate = $existing
                } else {
                    $OwnedRecords[$candidateKey] = $candidate
                }
                $retained = $true
                $family[$candidateKey] = $candidate
                $pending.Enqueue($candidate)
            } finally {
                if ($null -ne $candidate -and -not $retained) {
                    [void] (& $ReleaseRecord $candidate)
                }
            }
        }
    }
    return @($family.Values)
}

function Test-ListenerOwnedProcess {
    param(
        [Parameter(Mandatory)] [int] $OwnerProcessId,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [object[]] $OwnedRecords,
        [scriptblock] $ResolveProcess = { param($processId) Resolve-SmokeProcessRecord -ProcessId $processId },
        [scriptblock] $ReadLifetime = { param($record) Get-OwnedProcessLifetime -Record $record },
        [scriptblock] $ReleaseRecord = { param($record) Close-OwnedProcessRecord -Record $record },
        [scriptblock] $GetListenerProcessIds = { Get-ListeningProcessIdsForPort -Port 17871 }
    )

    $current = $null
    try {
        $current = & $ResolveProcess $OwnerProcessId
        if ($null -eq $current -or [int] $current.ProcessId -ne $OwnerProcessId) {
            return $false
        }
        $lifetime = & $ReadLifetime $current
        if ($lifetime.HasExited -or [long] $lifetime.CreationUtcTicks -ne [long] $current.CreationUtcTicks) {
            return $false
        }
        $matching = @($OwnedRecords | Where-Object {
            [int] $_.ProcessId -eq $OwnerProcessId -and [long] $_.CreationUtcTicks -eq [long] $current.CreationUtcTicks
        })
        if ($matching.Count -ne 1) {
            return $false
        }
        $listenerIds = @(& $GetListenerProcessIds)
        # The port lookup can race process exit/PID reuse. Recheck the retained
        # identities after observing the current owner as well as before HTTP.
        $lifetime = & $ReadLifetime $current
        $ownedLifetime = & $ReadLifetime $matching[0]
        return (-not $ownedLifetime.HasExited -and
            -not $lifetime.HasExited -and [long] $lifetime.CreationUtcTicks -eq [long] $current.CreationUtcTicks -and
            [long] $ownedLifetime.CreationUtcTicks -eq [long] $matching[0].CreationUtcTicks -and
            $listenerIds.Count -eq 1 -and [int] $listenerIds[0] -eq $OwnerProcessId)
    } catch {
        return $false
    } finally {
        if ($null -ne $current) {
            [void] (& $ReleaseRecord $current)
        }
    }
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
    param(
        $LauncherProcess,
        $LauncherRecord = $script:launcherProcessRecord,
        [System.Collections.IDictionary] $OwnedRecords = $script:ownedProcessRecords,
        [scriptblock] $RefreshFamily = { param($parentRecord, $deadline, $owned) Get-LauncherFamilyRecords -LauncherRecord $parentRecord -DeadlineUtc $deadline -OwnedRecords $owned },
        [scriptblock] $ReadLifetime = { param($record) Get-OwnedProcessLifetime -Record $record },
        [scriptblock] $StopRecord = { param($record) Stop-OwnedProcessRecord -Record $record },
        [scriptblock] $WaitRecord = { param($record, $milliseconds) Wait-OwnedProcessRecord -Record $record -RemainingMilliseconds $milliseconds },
        [scriptblock] $ReleaseRecord = { param($record) Close-OwnedProcessRecord -Record $record },
        [scriptblock] $PortRelease = { Wait-ForProvisioningPortRelease },
        [scriptblock] $UtcNow = { [DateTime]::UtcNow }
    )

    if ($null -eq $LauncherProcess -and $OwnedRecords.Count -eq 0) {
        return
    }
    try {
        $processExitStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        $processExitDeadline = (& $UtcNow).AddSeconds(5)
        $stopFailure = $null
        try {
            if ($null -eq $LauncherRecord) {
                throw [InvalidOperationException]::new('Owned launcher identity unavailable.')
            }
            # A child need not bind the provisioning port to require cleanup.
            # Refresh only from the original pinned launcher, inside the existing
            # shutdown budget; partial discoveries remain owned if refresh fails.
            [void] (& $RefreshFamily $LauncherRecord $processExitDeadline $OwnedRecords)
            if ($processExitStopwatch.ElapsedMilliseconds -ge 5000 -or (& $UtcNow) -ge $processExitDeadline) {
                throw [TimeoutException]::new('Process cleanup deadline reached.')
            }
        } catch {
            $stopFailure = $_.Exception.GetType().Name
        }
        # Records are inserted root-first as ownership is proved. Reversing them
        # stops children before their parent without reopening any process by PID.
        $records = @($OwnedRecords.Values)
        if ($records.Count -eq 0) {
            throw [InvalidOperationException]::new('Owned launcher identity unavailable.')
        }
        [array]::Reverse($records)
        $processesRequestedToStop = [System.Collections.Generic.List[object]]::new()
        $stoppedProcessIds = [System.Collections.Generic.List[int]]::new()
        foreach ($record in $records) {
            try {
                $lifetime = & $ReadLifetime $record
                if ([long] $lifetime.CreationUtcTicks -ne [long] $record.CreationUtcTicks) {
                    throw [InvalidOperationException]::new('Cleanup identity changed.')
                }
                if (-not $lifetime.HasExited) {
                    Write-SmokeProgress -Stage 'process-stop' -Phase begin -Details @{ process_id = $record.ProcessId }
                    [void] (& $StopRecord $record)
                    Write-SmokeProgress -Stage 'process-stop' -Phase end -Details @{ process_id = $record.ProcessId }
                    $processesRequestedToStop.Add($record)
                }
            } catch {
                # Continue releasing the other owned instances, never a replacement.
                $stopFailure = $_.Exception.GetType().Name
            }
        }
        foreach ($record in $processesRequestedToStop) {
            $remainingMilliseconds = [int] [Math]::Floor([Math]::Max(0, [Math]::Min(
                5000 - $processExitStopwatch.ElapsedMilliseconds, ($processExitDeadline - (& $UtcNow)).TotalMilliseconds)))
            Write-SmokeProgress -Stage 'process-exit-wait' -Phase begin -Details @{ process_id = $record.ProcessId }
            if (-not (& $WaitRecord $record $remainingMilliseconds)) {
                Write-SmokeProgress -Stage 'process-exit-wait' -Phase end -Details @{ process_id = $record.ProcessId; exited = $false }
                Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{
                    finding = 'launcher_process_not_exited'; process_id = $record.ProcessId
                    stopped_process_ids = @($stoppedProcessIds); timeout_seconds = 5
                }
                return
            }
            Write-SmokeProgress -Stage 'process-exit-wait' -Phase end -Details @{ process_id = $record.ProcessId; exited = $true }
            $stoppedProcessIds.Add([int] $record.ProcessId)
        }
        if ($null -ne $stopFailure) {
            Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{ finding = 'launcher_stop_failed'; exception = $stopFailure }
            return
        }
        $portReleaseResult = & $PortRelease
        if (-not $portReleaseResult.Released) {
            Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{
                finding = 'provisioning_listener_port_not_released'; stopped_process_ids = @($stoppedProcessIds)
                listener_process_ids = @($portReleaseResult.ListenerProcessIds); port_released = $false
            }
            return
        }
        Add-Result -Step 'close-bridge' -Outcome pass -Evidence @{
            stopped_process_ids = @($stoppedProcessIds); port_released = $true; listener_process_ids = @()
        }
    } catch {
        Add-Result -Step 'close-bridge' -Outcome fail -Evidence @{ finding = 'launcher_stop_failed'; exception = $_.Exception.GetType().Name }
    } finally {
        foreach ($record in @($OwnedRecords.Values)) {
            try {
                [void] (& $ReleaseRecord $record)
            } catch {
                # Handle release must continue for the remaining owned instances.
            }
        }
        $OwnedRecords.Clear()
    }
}

function Get-SurvivingInstallationEntry {
    param([Parameter(Mandatory)] [string] $InstallationPrefix)

    if (-not (Test-Path -LiteralPath $InstallationPrefix -PathType Container)) {
        return @()
    }
    $prefixLength = (Get-Item -LiteralPath $InstallationPrefix).FullName.Length
    return @(
        Get-ChildItem -LiteralPath $InstallationPrefix -Recurse -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                $_.FullName.Substring($prefixLength).TrimStart([IO.Path]::DirectorySeparatorChar)
            } |
            Sort-Object
    )
}

function Wait-InstallationPrefixRemoved {
    param(
        [Parameter(Mandatory)] [string] $InstallationPrefix,
        [Parameter(Mandatory)] [int] $TimeoutSeconds
    )

    # The uninstaller relaunches itself from a temporary copy, so the process
    # this harness waited on can exit before removal has finished.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Test-Path -LiteralPath $InstallationPrefix) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 250
    }
    return -not (Test-Path -LiteralPath $InstallationPrefix)
}

function Remove-TemporaryRunRoot {
    param([psobject] $Layout)

    if ($null -eq $Layout -or -not (Test-Path -LiteralPath $Layout.RunRoot -PathType Container)) {
        return $true
    }
    Write-SmokeProgress -Stage 'temporary-root-removal' -Phase begin
    Remove-Item -LiteralPath $Layout.RunRoot -Recurse -Force -ErrorAction SilentlyContinue
    Write-SmokeProgress -Stage 'temporary-root-removal' -Phase end
    return -not (Test-Path -LiteralPath $Layout.RunRoot)
}

function Invoke-UninstallArtifact {
    param([psobject] $Installation, [psobject] $Layout)

    $evidence = @{}
    try {
        if ($null -ne $Installation) {
            if (-not (Test-Path -LiteralPath $Installation.UninstallerPath -PathType Leaf)) {
                throw "installer cleanup cannot find uninstaller: $($Installation.UninstallerPath)"
            }
            $runtimeDataExisted = Test-Path -LiteralPath $Installation.RuntimeDataDirectory -PathType Container
            Write-SmokeProgress -Stage 'uninstaller-wait' -Phase begin
            $uninstallerProcess = Start-Process -FilePath $Installation.UninstallerPath -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
            ) -Wait -PassThru
            Write-SmokeProgress -Stage 'uninstaller-wait' -Phase end -Details @{ exit_code = $uninstallerProcess.ExitCode }
            # A zero exit code reports that the uninstaller started, not that it
            # removed anything, so the end state is measured rather than assumed.
            if ($uninstallerProcess.ExitCode -ne 0) {
                throw "uninstaller exited with code $($uninstallerProcess.ExitCode)"
            }
            Write-SmokeProgress -Stage 'installation-removal-wait' -Phase begin
            $prefixRemoved = Wait-InstallationPrefixRemoved `
                -InstallationPrefix $Installation.InstallationPrefix -TimeoutSeconds 60
            Write-SmokeProgress -Stage 'installation-removal-wait' -Phase end -Details @{ removed = $prefixRemoved }
            $surviving = @(Get-SurvivingInstallationEntry -InstallationPrefix $Installation.InstallationPrefix)
            $runtimeDataRetained = Test-Path -LiteralPath $Installation.RuntimeDataDirectory -PathType Container

            $evidence['installation_prefix_removed'] = $prefixRemoved
            $evidence['surviving_installation_entry_count'] = $surviving.Count
            # Bound the transcript; the count above carries the full extent.
            $evidence['surviving_installation_entries'] = @($surviving | Select-Object -First 25)
            $evidence['runtime_data_directory_existed'] = $runtimeDataExisted
            $evidence['runtime_data_directory_retained'] = $runtimeDataRetained

            if (-not $prefixRemoved) {
                $evidence['finding'] = 'installation_prefix_survived_uninstall'
            } elseif ($runtimeDataExisted -and -not $runtimeDataRetained) {
                # The product data directory outlives the program on purpose, so
                # an uninstall that removes it is a regression this step catches.
                $evidence['finding'] = 'runtime_data_directory_removed_by_uninstall'
            }
        }
    } catch {
        Add-Result -Step 'uninstall-artifact' -Outcome fail -Evidence @{
            finding = 'uninstall_failed'
            exception = $_.Exception.GetType().Name
            message = $_.Exception.Message
        }
        [void] (Remove-TemporaryRunRoot -Layout $Layout)
        return
    }

    $evidence['removed_temporary_installation'] = (Remove-TemporaryRunRoot -Layout $Layout)
    if ($evidence.ContainsKey('finding')) {
        Add-Result -Step 'uninstall-artifact' -Outcome fail -Evidence $evidence
    } else {
        Add-Result -Step 'uninstall-artifact' -Outcome pass -Evidence $evidence
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
    # One seam covers both observations, so ownership-bypass mutation tests still
    # exercise the complete negative control, including the post-health check.
    $listenerOwnershipProbe = { param($processId, $records) Test-ListenerOwnedProcess -OwnerProcessId $processId -OwnedRecords $records }
    do {
        try {
            $listenerProcessIds = @(Get-ListeningProcessIdsForPort -Port 17871)
            if ($listenerProcessIds.Count -ne 1) {
                $lastFailure = 'listener_owner_not_unique'
                Start-Sleep -Milliseconds 250
                continue
            }
            $ownerProcessId = [int] $listenerProcessIds[0]
            $ownedRecords = @(Get-LauncherFamilyRecords -LauncherRecord $script:launcherProcessRecord -DeadlineUtc $deadline)
            $ownedProcessIds = @($ownedRecords | ForEach-Object { [int] $_.ProcessId })
            $listenerOwnedByLauncher = & $listenerOwnershipProbe $ownerProcessId $ownedRecords
            if (-not $listenerOwnedByLauncher) {
                Add-Result -Step 'provisioning-listener' -Outcome fail -Evidence @{
                    finding = 'listener_owned_by_unrelated_process'; endpoint = 'http://127.0.0.1:17871/health'
                    listener_process_id = $ownerProcessId; launcher_process_id = $LauncherProcess.Id
                    launcher_family_process_ids = $ownedProcessIds
                }
                return $false
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                throw [TimeoutException]::new('Provisioning listener deadline reached.')
            }
            Write-SmokeProgress -Stage 'listener-health-request' -Phase begin -Details @{ process_id = $ownerProcessId }
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:17871/health' -TimeoutSec 2
            Write-SmokeProgress -Stage 'listener-health-request' -Phase end -Details @{ status_code = $response.StatusCode }
            $health = $response.Content | ConvertFrom-Json
            if ($response.StatusCode -eq 200 -and $health.status -eq 'ok') {
                if ([DateTime]::UtcNow -ge $deadline) {
                    throw [TimeoutException]::new('Provisioning listener deadline reached.')
                }
                if (-not (& $listenerOwnershipProbe $ownerProcessId $ownedRecords)) {
                    $lastFailure = 'listener_owner_not_unique'
                    Start-Sleep -Milliseconds 250
                    continue
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

function Invoke-VerifyPlatformCapability {
    param([Parameter(Mandatory)] [psobject] $Installation)

    $markerPath = Join-Path -Path $Installation.InstallationPrefix -ChildPath 'platform-capability.txt'
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        # The installer records the probe outcome for every install it completes,
        # so a missing marker means the probe did not run at all.
        Add-Result -Step 'platform-capability' -Outcome fail -Evidence @{
            finding = 'platform_capability_marker_missing'
            path = $markerPath
        }
        return
    }
    $probeOutcome = (Get-Content -LiteralPath $markerPath -Raw).Trim()
    # A machine without a hardware-backed provider still installs, so the probe
    # outcome is recorded rather than enforced. Only /REQUIREPLATFORMKEY enforces.
    Add-Result -Step 'platform-capability' -Outcome pass -Evidence @{
        platform_key_probe = $probeOutcome
        platform_key_available = ($probeOutcome -eq 'available')
    }
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

Write-SmokeProgress -Stage 'harness' -Phase begin
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
        Invoke-VerifyPlatformCapability -Installation $installation
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
