param([Parameter(Mandatory = $true)][string]$PathsFile)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

function Get-WorkloadPathVolume([string]$Role, [string]$RequestedPath) {
    if ($RequestedPath -notmatch '^[A-Za-z]:\\' -or $RequestedPath.Substring(2).Contains(':') -or
        $RequestedPath.Contains('/') -or $RequestedPath.Substring(2).StartsWith('\\')) {
        throw 'storage_local_absolute_path_required'
    }
    foreach ($part in $RequestedPath.Substring(2).Split('\')) {
        if ($part -and ($part.EndsWith(' ') -or $part.EndsWith('.'))) {
            throw 'storage_path_not_normalized'
        }
    }
    $resolved = [IO.Path]::GetFullPath($RequestedPath)
    if ($resolved.Length -gt [IO.Path]::GetPathRoot($resolved).Length) { $resolved = $resolved.TrimEnd('\') }
    $existing = $resolved
    while (-not (Test-Path -LiteralPath $existing -ErrorAction Stop)) {
        $parent = [IO.Path]::GetDirectoryName($existing)
        if (-not $parent -or $parent -eq $existing) { throw 'storage_existing_ancestor_missing' }
        $existing = $parent
    }
    $components = [Collections.Generic.List[object]]::new()
    $current = $existing
    while ($true) {
        if ($components.Count -ge 256) { throw 'storage_path_depth_exceeded' }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'storage_workload_reparse_point'
        }
        if ($components.Count -eq 0 -and $existing -ne $resolved -and -not $item.PSIsContainer) {
            throw 'storage_workload_ancestor_not_directory'
        }
        $components.Add([pscustomobject]@{Path=$current;Attributes=[long]$item.Attributes})
        $parent = [IO.Path]::GetDirectoryName($current)
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    $volumes = @(Get-Volume -FilePath $existing -ErrorAction Stop)
    if ($volumes.Count -ne 1) { throw 'storage_workload_volume_not_singular' }
    [pscustomobject]@{
        Role=$Role;RequestedPath=$resolved;ResolvedPath=$resolved;ExistingPath=$existing
        VolumeUniqueId=$volumes[0].UniqueId;PathComponents=@($components.ToArray())
    }
}

$pathValues = [IO.File]::ReadAllText($PathsFile, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
if ($pathValues -isnot [pscustomobject] -or @($pathValues.PSObject.Properties).Count -lt 1 -or
    @($pathValues.PSObject.Properties).Count -gt 32) { throw 'storage_workload_paths_required' }
$pathVolumes = @($pathValues.PSObject.Properties | ForEach-Object {
    if ($_.Value -isnot [string] -or -not $_.Name -or $_.Name.Trim() -ne $_.Name) {
        throw 'storage_workload_path_invalid'
    }
    Get-WorkloadPathVolume -Role $_.Name -RequestedPath $_.Value
})
$disks = @(Get-Disk -ErrorAction Stop)
$partitions = @(Get-Partition -ErrorAction Stop)
$volumes = @(Get-Volume -ErrorAction Stop)
$osInfo = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
$processors = @(Get-CimInstance Win32_Processor -ErrorAction Stop)
$architectures = @($processors | Select-Object -ExpandProperty Architecture -Unique)
$architectureNames = @{9='AMD64';12='ARM64'}
if ($architectures.Count -ne 1 -or -not $architectureNames.ContainsKey([int]$architectures[0])) {
    throw 'storage_processor_architecture_unsupported'
}
$powerPlans = @(Get-CimInstance -Namespace 'root/cimv2/power' -ClassName Win32_PowerPlan -ErrorAction Stop |
    Where-Object { $_.IsActive -eq $true })
if ($powerPlans.Count -ne 1 -or -not ([string]$powerPlans[0].InstanceID).Trim() -or
    -not ([string]$powerPlans[0].ElementName).Trim()) { throw 'storage_power_mode_unavailable' }
$power = ([string]$powerPlans[0].InstanceID).Trim() + ' ' + ([string]$powerPlans[0].ElementName).Trim()
$result = [pscustomobject]@{
    ObservedUtc=[datetime]::UtcNow.ToString('o')
    BootUtc=$osInfo.LastBootUpTime.ToUniversalTime().ToString('o')
    ProbeProcessId=$PID
    SystemDrive=$osInfo.SystemDrive
    MachineUUID=(Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop).UUID
    CpuModel=(($processors | Select-Object -ExpandProperty Name) -join ', ')
    PowerMode=$power
    MemoryGiB=[int][Math]::Round([double]$osInfo.TotalVisibleMemorySize / 1048576)
    Cores=[int](($processors | Measure-Object NumberOfCores -Sum).Sum)
    InstructionRequirements=$architectureNames[[int]$architectures[0]]
    AvailableMemoryBytes=[long]$osInfo.FreePhysicalMemory * 1024
    DiskCount=$disks.Count
    Disks=@($disks | Select-Object Number,UniqueId,UniqueIdFormat,SerialNumber,Guid,
        @{Name='PartitionStyle';Expression={$_.PartitionStyle.ToString()}},
        @{Name='BusType';Expression={$_.BusType.ToString()}},FriendlyName,Model,Path,Location,Size,IsBoot,IsSystem)
    Partitions=@($partitions | Select-Object DiskNumber,PartitionNumber,DiskId,Guid,UniqueId,
        DriveLetter,AccessPaths,Offset,Size,Type,IsBoot,IsSystem)
    Volumes=@($volumes | Select-Object DriveLetter,Path,UniqueId,ObjectId,
        @{Name='FileSystemType';Expression={$_.FileSystemType.ToString()}},Size,SizeRemaining)
    VolumePartitions=@($volumes | ForEach-Object {
        [pscustomobject]@{
            VolumeUniqueId=$_.UniqueId
            Partitions=@(Get-Partition -Volume $_ -ErrorAction Stop |
                Select-Object DiskNumber,PartitionNumber,DiskId,Guid,AccessPaths)
        }
    })
    DiskDevices=@(Get-CimInstance Win32_DiskDrive -ErrorAction Stop | Select-Object Index,DeviceID,
        PNPDeviceID,SerialNumber,Model,InterfaceType,SCSIBus,SCSIPort,SCSITargetId,SCSILogicalUnit,Size)
    Controllers=@(Get-CimInstance Win32_SCSIController -ErrorAction Stop |
        Select-Object DeviceID,PNPDeviceID,Name)
    PhysicalDisks=@(Get-PhysicalDisk -ErrorAction Stop | Select-Object DeviceId,UniqueId,SerialNumber,
        @{Name='MediaType';Expression={$_.MediaType.ToString()}},
        @{Name='BusType';Expression={$_.BusType.ToString()}},Model,FriendlyName,Size,PhysicalLocation)
    VirtualDisks=@(Get-VirtualDisk -ErrorAction Stop | Select-Object ObjectId,UniqueId,Name,ResiliencySettingName,Size)
    StoragePools=@(Get-StoragePool -ErrorAction Stop | Select-Object UniqueId,IsPrimordial)
    PathVolumes=$pathVolumes
}
ConvertTo-Json -InputObject $result -Depth 12 -Compress
