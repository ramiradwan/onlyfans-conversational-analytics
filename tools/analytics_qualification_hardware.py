"""Record native Windows profile facts without changing the host."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess


def observe():
    if os.name != "nt":
        raise ValueError("declared_windows_profile_unavailable")
    shell = shutil.which("powershell.exe")
    if shell is None:
        raise ValueError("windows_profile_observer_unavailable")
    command = r'''
$ErrorActionPreference = 'Stop'
$osInfo = Get-CimInstance Win32_OperatingSystem
$processors = @(Get-CimInstance Win32_Processor)
$disk = Get-Partition -DriveLetter ($env:SystemDrive.TrimEnd(':')) | Get-Disk
$physical = @(Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $disk.Number })
if ($physical.Count -ne 1) { throw 'disk_profile_not_established' }
$power = (& powercfg /getactivescheme | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $power) { throw 'power_mode_unavailable' }
[ordered]@{
 os = 'Windows'
 memory_gib = [int][Math]::Round([double]$osInfo.TotalVisibleMemorySize / 1048576)
 cores = [int](($processors | Measure-Object NumberOfCores -Sum).Sum)
 disk = [string]$physical[0].MediaType
 cpu_model = (($processors | Select-Object -ExpandProperty Name) -join ', ')
 power_mode = $power
 instruction_requirements = $env:PROCESSOR_ARCHITECTURE
 available_memory_bytes = [long]$osInfo.FreePhysicalMemory * 1024
} | ConvertTo-Json -Compress
'''
    raw = subprocess.check_output([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                  text=True, timeout=30)
    value = json.loads(raw)
    value["platform"] = platform.platform()
    return value


def observe_network_isolation():
    """Require a guest whose non-loopback network interfaces are disconnected."""
    if os.name != "nt":
        raise ValueError("network_isolation_observer_requires_windows")
    command = r'''
$ErrorActionPreference = 'Stop'
$adapters = @(Get-NetAdapter -IncludeHidden | Where-Object Status -eq 'Up')
$routes = @(Get-NetRoute | Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0','::/0') })
[ordered]@{ active_adapters=$adapters.Count; default_routes=$routes.Count } | ConvertTo-Json -Compress
'''
    result = json.loads(subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", command], text=True, timeout=30))
    if result != {"active_adapters": 0, "default_routes": 0}:
        raise ValueError("network_disabled_guest_required")
    return result
