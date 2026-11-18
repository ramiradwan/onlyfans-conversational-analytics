# Usage: pwsh -File tools/ci_windows_host.ps1 [-StartCounters | -StopCounters -Destination <dir> | -SampleCounters -Output <csv>]
# Without a switch it prints the Windows runner's processor, memory and Defender state.
# -StartCounters launches a detached sampler that appends host counters every five seconds to
# RUNNER_TEMP\host-counters.csv. -StopCounters ends it and copies the CSV into the destination.
# Sampling is diagnostic, so these modes report problems and never fail the job.
param(
    [switch]$StartCounters,
    [switch]$StopCounters,
    [string]$Destination,
    [switch]$SampleCounters,
    [string]$Output
)
$ErrorActionPreference = 'Stop'

function Get-CounterPaths([string]$Drive) {
    [ordered]@{
        processor_time_percent = '\Processor(_Total)\% Processor Time'
        processor_queue_length = '\System\Processor Queue Length'
        disk_sec_per_transfer = "\LogicalDisk($Drive)\Avg. Disk sec/Transfer"
        disk_idle_percent = "\LogicalDisk($Drive)\% Idle Time"
        disk_queue_length = "\LogicalDisk($Drive)\Current Disk Queue Length"
        available_mbytes = '\Memory\Available MBytes'
    }
}

if ($SampleCounters) {
    $drive = Split-Path -Qualifier $Output
    $paths = Get-CounterPaths $drive
    Set-Content -LiteralPath $Output -Value ('time,disk,' + ($paths.Keys -join ',')) -Encoding utf8NoBOM
    Get-Counter -Counter @($paths.Values) -SampleInterval 5 -Continuous -ErrorAction SilentlyContinue | ForEach-Object {
        $values = foreach ($path in $paths.Values) {
            $suffix = ($path -replace '^\\[^\\]+', '').ToLowerInvariant()
            $sample = $_.CounterSamples | Where-Object { $_.Path.ToLowerInvariant().EndsWith($suffix) } | Select-Object -First 1
            if ($sample) { [string]::Format([cultureinfo]::InvariantCulture, '{0:0.######}', $sample.CookedValue) } else { '' }
        }
        Add-Content -LiteralPath $Output -Value (($_.Timestamp.ToUniversalTime().ToString('o'), $drive) + $values -join ',') -Encoding utf8NoBOM
    }
    return
}

if ($StartCounters) {
    try {
        $output = Join-Path $env:RUNNER_TEMP 'host-counters.csv'
        $process = Start-Process -FilePath (Get-Process -Id $PID).Path -WindowStyle Hidden -PassThru -ArgumentList @(
            '-NoProfile', '-File', "`"$PSCommandPath`"", '-SampleCounters', '-Output', "`"$output`"")
        Set-Content -LiteralPath (Join-Path $env:RUNNER_TEMP 'host-counters.pid') -Value $process.Id
        "Host counter sampling started every 5 s on $(Split-Path -Qualifier $output)"
    } catch {
        "::warning::Host counter sampling did not start: $($_.Exception.Message)"
    }
    return
}

if ($StopCounters) {
    try {
        $pidFile = Join-Path $env:RUNNER_TEMP 'host-counters.pid'
        if (Test-Path -LiteralPath $pidFile) {
            Stop-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
        }
        $source = Join-Path $env:RUNNER_TEMP 'host-counters.csv'
        if (Test-Path -LiteralPath $source) {
            New-Item -ItemType Directory -Force $Destination | Out-Null
            Copy-Item -LiteralPath $source -Destination (Join-Path $Destination 'host-counters.csv')
            "Host counter samples: $(@(Get-Content -LiteralPath $source).Count - 1)"
        } else {
            "::warning::No host counter samples were recorded"
        }
    } catch {
        "::warning::Host counter sampling did not stop cleanly: $($_.Exception.Message)"
    }
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
