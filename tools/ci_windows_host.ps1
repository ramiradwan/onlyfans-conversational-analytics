# Usage: pwsh -File tools/ci_windows_host.ps1 [-StartCounters | -StopCounters -Destination <dir> | -SampleCounters -Output <csv>]
# Without a switch it prints the Windows runner's processor, memory and Defender state.
# On GitHub Actions it also starts separate setup sampling, ending at -StartCounters.
# -StartCounters launches a detached sampler that appends host counters every five seconds to
# RUNNER_TEMP\host-counters.csv. -StopCounters ends it and copies both CSVs into the destination.
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
    Set-Content -LiteralPath $Output -Value ('time,disk,' + ($paths.Keys -join ',') + ',defender_pid,defender_cpu_seconds,defender_working_set_bytes') -Encoding utf8NoBOM
    Get-Counter -Counter @($paths.Values) -SampleInterval 5 -Continuous -ErrorAction SilentlyContinue | ForEach-Object {
        $values = foreach ($path in $paths.Values) {
            $suffix = ($path -replace '^\\[^\\]+', '').ToLowerInvariant()
            $sample = $_.CounterSamples | Where-Object { $_.Path.ToLowerInvariant().EndsWith($suffix) } | Select-Object -First 1
            if ($sample) { [string]::Format([cultureinfo]::InvariantCulture, '{0:0.######}', $sample.CookedValue) } else { '' }
        }
        # Cumulative process CPU time allows interval deltas without starting
        # an invasive trace or collecting filenames. Missing access is blank.
        $defenderPid = ''; $defenderCpu = ''; $defenderMemory = ''
        try {
            $defender = Get-Process -Name MsMpEng -ErrorAction Stop | Select-Object -First 1
            $defenderPid = [string]$defender.Id
            if ($null -ne $defender.CPU) {
                $defenderCpu = [string]::Format([cultureinfo]::InvariantCulture, '{0:0.######}', $defender.CPU)
            }
            $defenderMemory = [string]$defender.WorkingSet64
        } catch { }
        Add-Content -LiteralPath $Output -Value (($_.Timestamp.ToUniversalTime().ToString('o'), $drive) + $values + $defenderPid + $defenderCpu + $defenderMemory -join ',') -Encoding utf8NoBOM
    }
    return
}

function Start-HostSampler([string]$Name) {
    $output = Join-Path $env:RUNNER_TEMP "$Name.csv"
    $process = Start-Process -FilePath (Get-Process -Id $PID).Path -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoProfile', '-File', "`"$PSCommandPath`"", '-SampleCounters', '-Output', "`"$output`"")
    @{ id = $process.Id; started = $process.StartTime.ToUniversalTime().Ticks.ToString() } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $env:RUNNER_TEMP "$Name.pid")
    "Host counter sampling ($Name) started every 5 s on $(Split-Path -Qualifier $output)"
}

function Stop-HostSampler([string]$Name) {
    $pidFile = Join-Path $env:RUNNER_TEMP "$Name.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $identity = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
        $process = Get-Process -Id ([int]$identity.id) -ErrorAction SilentlyContinue
        if ($process -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $identity.started) {
            Stop-Process -InputObject $process -ErrorAction SilentlyContinue
            $process.WaitForExit(2000) | Out-Null
        }
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    }
}

if ($StartCounters) {
    try { Stop-HostSampler 'host-setup-counters' } catch { Write-Warning 'Setup counter sampling could not be stopped' }
    try { Start-HostSampler 'host-counters' } catch { Write-Warning 'Host counter sampling did not start' }
    return
}

if ($StopCounters) {
    foreach ($name in @('host-counters', 'host-setup-counters')) {
        try {
            Stop-HostSampler $name
            $source = Join-Path $env:RUNNER_TEMP "$name.csv"
            if (Test-Path -LiteralPath $source) {
                New-Item -ItemType Directory -Force $Destination | Out-Null
                Copy-Item -LiteralPath $source -Destination (Join-Path $Destination "$name.csv")
                "Host counter samples (${name}): $(@(Get-Content -LiteralPath $source).Count - 1)"
            } else {
                Write-Warning "No $name samples were recorded"
            }
        } catch {
            Write-Warning "Host counter sampling ($name) did not stop cleanly"
        }
    }
    return
}

"Runner image: $env:ImageOS $env:ImageVersion"

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

# Start before dependency installation, builds and independent reference collection.
# The existing explicit lane sampler remains separately bracketed by its workflow steps.
if ($env:GITHUB_ACTIONS -eq 'true' -and $env:RUNNER_TEMP) {
    try { Start-HostSampler 'host-setup-counters' } catch { Write-Warning 'Setup counter sampling did not start' }
}
