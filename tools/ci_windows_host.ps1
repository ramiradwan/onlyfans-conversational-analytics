# Usage: pwsh -File tools/ci_windows_host.ps1 [-ExcludeTestStorage]
# Prints the Windows runner's processor, memory and Defender state. With -ExcludeTestStorage it
# first excludes the workspace and runner temporary directory from Defender scanning, and fails
# if that cannot be applied.
param([switch]$ExcludeTestStorage)
$ErrorActionPreference = 'Stop'

if ($ExcludeTestStorage) {
    $paths = @($env:GITHUB_WORKSPACE, $env:RUNNER_TEMP) | Where-Object { $_ }
    if ($paths.Count -ne 2) { throw 'GITHUB_WORKSPACE and RUNNER_TEMP must both be set' }
    Add-MpPreference -ExclusionPath $paths
    $applied = @((Get-MpPreference).ExclusionPath)
    foreach ($path in $paths) {
        if ($applied -notcontains $path) { throw "Defender exclusion was not applied: $path" }
    }
    "Excluded from Defender scanning: $($paths -join ', ')"
    return
}

try {
    $processors = @(Get-CimInstance Win32_Processor)
    $names = ($processors.Name | Sort-Object -Unique) -join '; '
    "Processor: $names, $([Environment]::ProcessorCount) logical processors"
    $system = Get-CimInstance Win32_OperatingSystem
    'Memory: {0:N1} GiB total, {1:N1} GiB free' -f ($system.TotalVisibleMemorySize / 1MB), ($system.FreePhysicalMemory / 1MB)
} catch {
    "Host hardware unavailable: $($_.Exception.Message)"
}
try {
    $status = Get-MpComputerStatus
    "Defender: antivirus $($status.AntivirusEnabled), real-time $($status.RealTimeProtectionEnabled), on-access $($status.OnAccessProtectionEnabled), behaviour monitor $($status.BehaviorMonitorEnabled)"
    $preference = Get-MpPreference
    $excluded = @($preference.ExclusionPath | Where-Object { $_ })
    "Defender exclusions: $(if ($excluded) { $excluded -join ', ' } else { 'none' }); scan CPU load factor $($preference.ScanAvgCPULoadFactor)"
} catch {
    "Defender state unavailable: $($_.Exception.Message)"
}
