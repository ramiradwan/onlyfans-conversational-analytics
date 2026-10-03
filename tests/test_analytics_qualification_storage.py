"""Virtual qualification requires complete local and host storage observations."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tools.analytics_qualification_storage import validate_snapshot


pytestmark = [pytest.mark.ci_tier("fast")]
PROFILE = {"os": "Windows", "memory_gib": 16, "cores": 4, "disk": "SSD"}
PATHS = {"data": r"C:\qualification\store\canonical.sqlite3"}
VM = "11111111-1111-4111-8111-111111111111"
VOLUME = "\\\\?\\Volume{22222222-2222-4222-8222-222222222222}\\"
DISK = r"\\?\scsi#disk&ven_msft&prod_virtual_disk#qualified-disk#{53f56307-b6bf-11d0-94f2-00a0c91efb8b}"
SIZE = 96 * 1024**3


def guest_observation():
    partition = {"DiskNumber": 0, "PartitionNumber": 3, "DiskId": DISK,
                 "Guid": "22222222-2222-4222-8222-222222222222",
                 "AccessPaths": ["C:\\", VOLUME], "Offset": 1024**2, "Size": SIZE - 1024**2}
    return {
        "ObservedUtc": "2026-01-01T00:00:02Z", "BootUtc": "2026-01-01T00:00:00Z",
        "MachineUUID": "33333333-3333-4333-8333-333333333333", "SystemDrive": "C:",
        "CpuModel": "Synthetic CPU", "PowerMode": "Synthetic power scheme", "MemoryGiB": 16,
        "Cores": 4, "InstructionRequirements": "AMD64", "AvailableMemoryBytes": 12 * 1024**3,
        "DiskCount": 1, "Disks": [{"Number": 0, "UniqueId": "guest-unique-disk",
            "Guid": "44444444-4444-4444-8444-444444444444", "PartitionStyle": "GPT",
            "BusType": "SAS", "Model": "Virtual Disk    ", "Path": DISK, "Size": SIZE, "IsBoot": True, "IsSystem": True}],
        "DiskDevices": [{"Index": 0, "DeviceID": r"\\.\PHYSICALDRIVE0", "InterfaceType": "SCSI",
            "PNPDeviceID": r"SCSI\DISK&VEN_MSFT&PROD_VIRTUAL_DISK\qualified-disk", "Model": "Microsoft Virtual Disk", "SCSIBus": 0,
            "SCSIPort": 0, "SCSITargetId": 0, "SCSILogicalUnit": 0}],
        "Controllers": [{"DeviceID": r"VMBUS\{BA6163D9-04A1-4D29-B605-72E2FFB1DC7F}\{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}",
                         "PNPDeviceID": r"VMBUS\{BA6163D9-04A1-4D29-B605-72E2FFB1DC7F}\{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}"}],
        "PhysicalDisks": [{"DeviceId": "0", "UniqueId": "guest-unique-disk", "BusType": "SAS",
            "Size": SIZE, "MediaType": "Unspecified"}],
        "Partitions": [partition],
        "Volumes": [{"Path": VOLUME, "UniqueId": VOLUME, "FileSystemType": "NTFS"}],
        "VolumePartitions": [{"VolumeUniqueId": VOLUME, "Partitions": [deepcopy(partition)]}],
        "VirtualDisks": [], "StoragePools": [{"IsPrimordial": True, "UniqueId": "guest-pool"}],
        "PathVolumes": [{"Role": "data", "RequestedPath": PATHS["data"], "ResolvedPath": PATHS["data"],
            "ExistingPath": r"C:\qualification", "VolumeUniqueId": VOLUME,
            "PathComponents": [{"Path": r"C:\qualification", "Attributes": 16},
                               {"Path": "C:\\", "Attributes": 16}]}],
    }


def storage_snapshot():
    physical = {"UniqueId": "host-device-id", "SerialNumber": "host-serial", "MediaType": "SSD",
                "BusType": "NVMe", "Size": 1024**4}
    nodes, backings = [], []
    for path, kind, parent in [(r"C:\virtual\child.avhdx", "Differencing", r"C:\virtual\base.vhdx"),
                               (r"C:\virtual\base.vhdx", "Fixed", "")]:
        nodes.append({"Path": path, "DiskIdentifier": "55555555-5555-4555-8555-555555555555",
            "VhdType": kind, "VhdFormat": "VHDX", "Size": SIZE, "ParentPath": parent,
            "LogicalSectorSize": 512, "PhysicalSectorSize": 4096})
        backings.append({"Path": path, "Volume": {"Path": VOLUME, "UniqueId": VOLUME,
            "FileSystemType": "NTFS"}, "Partitions": [{"DiskNumber": 2, "PartitionNumber": 3,
                "DiskId": r"\\?\nvme#host-device", "Guid": "66666666-6666-4666-8666-666666666666",
                "AccessPaths": [VOLUME, "C:\\"], "Offset": 1024**2, "Size": 900 * 1024**3}],
            "Disks": [{"Number": 2, "Path": r"\\?\nvme#host-device", "UniqueId": "host-device-id",
                "SerialNumber": "host-serial", "Guid": "77777777-7777-4777-8777-777777777777",
                "BusType": "NVMe", "Size": 1024**4}], "PhysicalMatches": [deepcopy(physical)]})
    return {"schema": "analytics-storage-snapshot.v1",
        "Transport": {"Kind": "powershell-direct-vmid", "VMId": VM},
        "Host": {"ObservedUtc": "2026-01-01T00:00:02Z", "CpuModel": "Synthetic CPU",
            "PowerMode": "Synthetic host power scheme", "VM": {"Id": VM, "Generation": 2,
                "Version": "12.0", "ProcessorCount": 4, "MemoryStartup": 16 * 1024**3,
                "DynamicMemoryEnabled": False},
            "Attachments": [{"VMId": VM, "ControllerType": 1, "ControllerNumber": 0,
                "ControllerLocation": 0, "Path": nodes[0]["Path"], "DiskNumber": None,
                "SupportPersistentReservations": False}],
            "ScsiControllers": [{"VMId": VM, "ControllerNumber": 0}],
            "Chains": [{"AttachmentPath": nodes[0]["Path"], "Nodes": nodes}], "Backings": backings,
            "PhysicalInventory": [physical], "StoragePools": [{"IsPrimordial": True}]},
        "Guest": guest_observation()}


def validate(snapshot, local=None, paths=None):
    return validate_snapshot(snapshot, deepcopy(snapshot["Guest"]) if local is None else local,
                             PATHS if paths is None else paths, PROFILE)


def test_singleton_binding_does_not_convert_guest_identifiers():
    snapshot = storage_snapshot()
    topology = validate(snapshot)
    assert topology["vm_id"] != topology["guest"]["machine_uuid"]
    assert topology["chain"][0]["identifier"] != topology["guest"]["disk"]["unique_id"]
    assert topology["guest"]["disk"]["media"] == "Unspecified"
    assert all(item["media"] == "SSD" for item in topology["backings"])


def test_observation_and_free_memory_changes_do_not_change_topology():
    snapshot = storage_snapshot()
    expected = validate(snapshot)
    snapshot["Host"]["ObservedUtc"] = "2026-01-01T00:02:00Z"
    snapshot["Guest"].update(ObservedUtc="2026-01-01T00:02:00Z", AvailableMemoryBytes=3 * 1024**3,
                              ProbeProcessId=987)
    local = deepcopy(snapshot["Guest"])
    local.update(ObservedUtc="2026-01-01T00:02:01Z", AvailableMemoryBytes=4 * 1024**3, ProbeProcessId=988)
    assert validate(snapshot, local) == expected


def test_new_workload_file_keeps_the_same_backing_topology():
    snapshot = storage_snapshot()
    expected = validate(snapshot)
    path = snapshot["Guest"]["PathVolumes"][0]
    path["ExistingPath"] = path["RequestedPath"]
    path["PathComponents"][:0] = [{"Path": PATHS["data"], "Attributes": 32},
                                  {"Path": r"C:\qualification\store", "Attributes": 16}]
    assert validate(snapshot) == expected


@pytest.mark.parametrize("field,value", [
    ("MachineUUID", "88888888-8888-4888-8888-888888888888"),
    ("BootUtc", "2026-01-01T00:00:01Z"), ("CpuModel", "Different CPU"),
    ("PowerMode", "Different power scheme"), ("InstructionRequirements", "ARM64")])
def test_remote_observation_must_identify_the_local_guest(field, value):
    snapshot = storage_snapshot()
    local = deepcopy(snapshot["Guest"])
    local[field] = value
    with pytest.raises(ValueError, match="local_remote_guest_mismatch"):
        validate(snapshot, local)


@pytest.mark.parametrize("field", ["CpuModel", "PowerMode"])
def test_host_cpu_and_power_changes_remain_visible_to_pair_verification(field):
    snapshot = storage_snapshot()
    before = validate(snapshot)
    snapshot["Host"][field] = "Different observed value"
    assert validate(snapshot) != before


def alter(snapshot, fault):
    host, guest = snapshot["Host"], snapshot["Guest"]
    if fault == "schema": snapshot["schema"] = "unknown.v1"
    elif fault == "transport": snapshot["Transport"]["Kind"] = "name_only"
    elif fault == "foreign_vm": snapshot["Transport"]["VMId"] = "99999999-9999-4999-8999-999999999999"
    elif fault == "dynamic_memory": host["VM"]["DynamicMemoryEnabled"] = True
    elif fault == "ram": host["VM"]["MemoryStartup"] = 8 * 1024**3
    elif fault == "cores": host["VM"]["ProcessorCount"] = 8
    elif fault == "missing_attachment": host["Attachments"] = []
    elif fault == "two_attachments": host["Attachments"] *= 2
    elif fault == "passthrough": host["Attachments"][0]["DiskNumber"] = 0
    elif fault == "controller": host["ScsiControllers"][0]["ControllerNumber"] = 1
    elif fault == "missing_parent": host["Chains"][0]["Nodes"].pop()
    elif fault == "cycle": host["Chains"][0]["Nodes"][1].update(VhdType="Differencing", ParentPath=host["Chains"][0]["Nodes"][0]["Path"])
    elif fault == "leaf_mismatch": host["Chains"][0]["AttachmentPath"] = r"C:\other.avhdx"
    elif fault == "missing_backing": host["Backings"].pop(0)
    elif fault == "duplicate_backing": host["Backings"][1] = deepcopy(host["Backings"][0])
    elif fault == "leaf_hdd": host["Backings"][0]["PhysicalMatches"][0]["MediaType"] = "HDD"
    elif fault == "base_unknown": host["Backings"][1]["PhysicalMatches"][0]["MediaType"] = "Unspecified"
    elif fault == "partition_disk": host["Backings"][0]["Partitions"][0]["DiskId"] = "different disk"
    elif fault == "volume_partition": host["Backings"][0]["Partitions"][0]["AccessPaths"] = ["D:\\"]
    elif fault == "serial": host["Backings"][0]["PhysicalMatches"][0]["SerialNumber"] = "foreign serial"
    elif fault == "ambiguous_host_disk": host["PhysicalInventory"] *= 2
    elif fault == "host_virtual_bus": host["Backings"][0]["Disks"][0]["BusType"] = "Spaces"
    elif fault == "host_pool": host["StoragePools"].append({"IsPrimordial": False})
    elif fault == "guest_pool": guest["StoragePools"].append({"IsPrimordial": False})
    elif fault == "guest_virtual_disk": guest["VirtualDisks"] = [{"UniqueId": "nested"}]
    elif fault == "two_guest_disks": guest["Disks"] *= 2
    elif fault == "hidden_disk": guest["DiskDevices"] *= 2
    elif fault == "hidden_physical": guest["PhysicalDisks"] *= 2
    elif fault == "not_boot": guest["Disks"][0]["IsBoot"] = False
    elif fault == "guest_device": guest["DiskDevices"][0]["Index"] = 1
    elif fault == "guest_pnp": guest["DiskDevices"][0]["PNPDeviceID"] = r"SCSI\DISK&VEN_OTHER\qualified-disk"
    elif fault == "guest_model": guest["Disks"][0]["Model"] = "Other disk"
    elif fault == "guest_scsi_controller": guest["Controllers"] = []
    elif fault == "guest_device_path": guest["Disks"][0]["Path"] = DISK.replace("qualified-disk", "different-disk")
    elif fault == "guest_physical": guest["PhysicalDisks"][0]["UniqueId"] = "another disk"
    elif fault == "guest_size": guest["PhysicalDisks"][0]["Size"] -= 512
    elif fault == "guest_partition": guest["VolumePartitions"][0]["Partitions"][0]["DiskId"] = "other disk"
    elif fault == "guest_partition_inventory": guest["Partitions"] = []
    elif fault == "unknown_volume": guest["PathVolumes"][0]["VolumeUniqueId"] = "unknown"
    elif fault == "other_drive":
        guest["PathVolumes"][0].update(RequestedPath=r"D:\data", ResolvedPath=r"D:\data", ExistingPath="D:\\",
            PathComponents=[{"Path": "D:\\", "Attributes": 16}])
    elif fault == "reparse": guest["PathVolumes"][0]["PathComponents"][0]["Attributes"] |= 1024
    elif fault == "missing_ancestor": guest["PathVolumes"][0]["PathComponents"].pop()
    elif fault == "file_ancestor": guest["PathVolumes"][0]["PathComponents"][0]["Attributes"] = 32
    elif fault == "empty_paths": guest["PathVolumes"] = []
    elif fault == "duplicate_paths": guest["PathVolumes"] *= 2
    elif fault == "redirected_path": guest["PathVolumes"][0]["ResolvedPath"] = r"C:\elsewhere"
    else: raise AssertionError(fault)


@pytest.mark.parametrize("fault", ["schema", "transport", "foreign_vm", "dynamic_memory", "ram", "cores",
    "missing_attachment", "two_attachments", "passthrough", "controller", "missing_parent", "cycle",
    "leaf_mismatch", "missing_backing", "duplicate_backing", "leaf_hdd", "base_unknown", "partition_disk",
    "volume_partition", "serial", "ambiguous_host_disk", "host_virtual_bus", "host_pool", "guest_pool",
    "guest_virtual_disk", "two_guest_disks", "hidden_disk", "hidden_physical", "not_boot", "guest_device",
    "guest_pnp", "guest_model", "guest_scsi_controller", "guest_device_path",
    "guest_physical", "guest_size", "guest_partition", "guest_partition_inventory", "unknown_volume",
    "other_drive", "reparse", "missing_ancestor", "file_ancestor", "empty_paths", "duplicate_paths",
    "redirected_path"])
def test_incomplete_or_ambiguous_storage_cannot_be_reported_as_ssd(fault):
    snapshot = storage_snapshot()
    alter(snapshot, fault)
    with pytest.raises(ValueError, match="storage_"):
        validate(snapshot, paths={"data": r"D:\data"} if fault == "other_drive" else None)


@pytest.mark.parametrize("path", [r"\\server\share\data", r"C:data", r"\data", r"C:\data:stream",
                                 r"C:\qualification\..\data", r"C:\data.", "C:/data",
                                 " C:\\data", "C:\\data "])
def test_workload_paths_require_unambiguous_local_drive_paths(path):
    with pytest.raises(ValueError, match="storage_"):
        validate(storage_snapshot(), paths={"data": path})


def test_unrelated_host_disk_without_a_serial_does_not_supply_backing_evidence():
    snapshot = storage_snapshot()
    before = validate(snapshot)
    snapshot["Host"]["PhysicalInventory"].append({"UniqueId": "unrelated", "SerialNumber": None})
    assert validate(snapshot) == before


@pytest.mark.parametrize("value", [None, [], {}, True, 1])
def test_malformed_nested_values_produce_a_validation_error(value):
    snapshot = storage_snapshot()
    snapshot["Guest"]["Disks"][0]["BusType"] = value
    with pytest.raises(ValueError, match="storage_"):
        validate(snapshot)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell path and storage projection contract")
@pytest.mark.windows_compat
def test_guest_script_uses_the_declared_projection_without_live_storage_queries(tmp_path):
    """Allow shell startup within a functional test's infrastructure guard.

    The real observer retains its separate 30-second qualification limit.
    """
    shell = shutil.which("powershell.exe")
    assert shell is not None
    data = tmp_path / "future" / "canonical.sqlite3"
    paths = {"data": str(data)}
    fixture = guest_observation()
    fixture["SystemDrive"] = tmp_path.drive
    for partition in fixture["Partitions"] + fixture["VolumePartitions"][0]["Partitions"]:
        partition["AccessPaths"][0] = tmp_path.drive + "\\"
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    (tmp_path / "paths.json").write_text(json.dumps(paths), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "tools/analytics_qualification_guest.ps1"
    harness = tmp_path / "harness.ps1"
    harness.write_text(r'''
param([string]$Fixture, [string]$Collector, [string]$Paths)
$ErrorActionPreference = 'Stop'
[Console]::Error.WriteLine('projection-fixture-loading')
$raw = Get-Content -Raw -LiteralPath $Fixture | ConvertFrom-Json
function Get-Disk { $raw.Disks }
function Get-Partition { param($Volume); $raw.Partitions }
function Get-Volume { param($FilePath); $raw.Volumes }
function Get-PhysicalDisk { $raw.PhysicalDisks }
function Get-VirtualDisk { $raw.VirtualDisks }
function Get-StoragePool { $raw.StoragePools }
function Get-CimInstance {
    param([string]$ClassName, [string]$Namespace)
    switch ($ClassName) {
        'Win32_OperatingSystem' { [pscustomobject]@{LastBootUpTime=[datetime]'2026-01-01T00:00:00Z';SystemDrive=$raw.SystemDrive;TotalVisibleMemorySize=16777216;FreePhysicalMemory=8388608} }
        'Win32_Processor' { [pscustomobject]@{Name=$raw.CpuModel;NumberOfCores=4;Architecture=9} }
        'Win32_ComputerSystemProduct' { [pscustomobject]@{UUID=$raw.MachineUUID} }
        'Win32_DiskDrive' { $raw.DiskDevices }
        'Win32_SCSIController' { $raw.Controllers }
        'Win32_PowerPlan' {
            if ($Namespace -ne 'root/cimv2/power') { throw 'Unexpected power namespace' }
            [pscustomobject]@{IsActive=$true;InstanceID='Synthetic scheme';ElementName='Synthetic plan'}
        }
        default { throw 'Unexpected observation' }
    }
}
$env:PROCESSOR_ARCHITECTURE=''
[Console]::Error.WriteLine('projection-collector-starting')
& $Collector -PathsFile $Paths
[Console]::Error.WriteLine('projection-collector-completed')
''', encoding="utf-8")
    try:
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(harness), "-Fixture", str(tmp_path / "fixture.json"), "-Collector", str(script),
            "-Paths", str(tmp_path / "paths.json")], capture_output=True, timeout=120,
            env={key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        diagnostics = (error.stderr or b"").decode("utf-8", errors="replace")
        pytest.fail(f"Synthetic projection harness exceeded {error.timeout} seconds. Captured stderr:\n"
                    f"{diagnostics or '<no phase markers or stderr captured>'}", pytrace=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    guest = json.loads(result.stdout.decode("utf-8-sig"))
    snapshot = storage_snapshot()
    snapshot["Guest"] = guest
    assert validate_snapshot(snapshot, deepcopy(guest), paths, PROFILE)["guest"]["paths"]["data"]["path"] == str(data).lower()
    assert guest["PathVolumes"][0]["ExistingPath"] == str(tmp_path)
    assert guest["PhysicalDisks"][0]["MediaType"] == "Unspecified"
    assert guest["InstructionRequirements"] == "AMD64"
    assert guest["PowerMode"] == "Synthetic scheme Synthetic plan"
