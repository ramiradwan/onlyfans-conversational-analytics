"""Validate the declared single-disk virtual qualification profile."""
from __future__ import annotations

from datetime import datetime
import ntpath
from uuid import UUID


def _require(condition, reason):
    if not condition:
        raise ValueError("storage_" + reason)


def _object(value):
    _require(type(value) is dict, "object_required")
    return value


def _array(value, maximum=64):
    _require(type(value) is list and len(value) <= maximum, "array_required")
    return [_object(item) for item in value]


def _one(value):
    items = _array(value, 1)
    _require(len(items) == 1, "singleton_required")
    return items[0]


def _text(value):
    _require(isinstance(value, str) and 0 < len(value.strip()) <= 32768, "text_required")
    _require(not any(ord(char) < 32 for char in value), "text_control_character")
    return value.strip()


def _identity(value):
    return _text(value).casefold()


def _integer(value, minimum=0):
    _require(type(value) is int and value >= minimum, "integer_required")
    return value


def _guid(value):
    try:
        return str(UUID(_text(value)))
    except (ValueError, AttributeError) as error:
        raise ValueError("storage_guid_required") from error


def _path(value):
    original = value
    value = _text(value)
    _require(value == original, "path_not_normalized")
    drive, tail = ntpath.splitdrive(value)
    _require(len(drive) == 2 and drive[0].isascii() and drive[0].isalpha()
             and drive[1] == ":" and tail.startswith("\\")
             and not tail.startswith("\\\\") and ":" not in tail, "local_absolute_path_required")
    _require("/" not in value and not any(part.endswith((" ", "."))
             for part in tail.split("\\") if part), "path_not_normalized")
    return ntpath.normcase(ntpath.normpath(value))


def _parents(value):
    result = [value]
    while ntpath.dirname(result[-1]) != result[-1]:
        result.append(ntpath.dirname(result[-1]))
    return result


def _utc(value):
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        _require(parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
                 "utc_time_required")
        return parsed.isoformat()
    except ValueError as error:
        raise ValueError("storage_utc_time_required") from error


def _pools(value):
    pools = _array(value)
    _require(all(item.get("IsPrimordial") is True for item in pools), "storage_pool_unsupported")


def _guest(value, paths, profile):
    guest = _object(value)
    _utc(guest.get("ObservedUtc"))
    boot = _utc(guest.get("BootUtc"))
    _require(_integer(guest.get("DiskCount")) == 1, "guest_disk_count")
    disk = _one(guest.get("Disks"))
    device = _one(guest.get("DiskDevices"))
    physical = _one(guest.get("PhysicalDisks"))
    _require(not _array(guest.get("VirtualDisks")), "guest_virtual_disks_unsupported")
    _pools(guest.get("StoragePools"))
    number = _integer(disk.get("Number"))
    _require(disk.get("IsBoot") is True and disk.get("IsSystem") is True,
             "guest_boot_system_disk_required")
    _require(disk.get("PartitionStyle") == "GPT" and disk.get("BusType") in {"SAS", "SCSI"},
             "guest_disk_topology_unsupported")
    disk_path, disk_id = _identity(disk.get("Path")), _identity(disk.get("UniqueId"))
    size = _integer(disk.get("Size"), 1)
    pnp = _identity(device.get("PNPDeviceID"))
    pnp_prefix = "scsi\\disk&ven_msft&prod_virtual_disk\\"
    _require(pnp.startswith(pnp_prefix) and len(pnp.split("\\")) == 3
             and _text(device.get("Model")) == "Microsoft Virtual Disk"
             and _text(disk.get("Model")) == "Virtual Disk", "guest_virtual_scsi_device_required")
    _require(disk_path == "\\\\?\\" + pnp.replace("\\", "#")
             + "#{53f56307-b6bf-11d0-94f2-00a0c91efb8b}", "guest_device_path_mismatch")
    controllers = []
    for item in _array(guest.get("Controllers")):
        controller_id = _identity(item.get("PNPDeviceID"))
        _require(controller_id == _identity(item.get("DeviceID")), "guest_controller_identity")
        if controller_id.startswith("vmbus\\{ba6163d9-04a1-4d29-b605-72e2ffb1dc7f}\\"):
            _require(len(controller_id.split("\\")) == 3, "guest_controller_identity")
            controllers.append(_guid(controller_id.split("\\")[-1]))
        else:
            _require(controller_id == "root\\spaceport\\0000", "guest_controller_unsupported")
    _require(len(controllers) == 1, "guest_virtual_scsi_controller_required")
    _require(_integer(device.get("Index")) == number
             and _identity(device.get("DeviceID")) == ("\\\\.\\physicaldrive" + str(number))
             and device.get("InterfaceType") == "SCSI", "guest_device_mismatch")
    _require(_text(physical.get("DeviceId")) == str(number)
             and _identity(physical.get("UniqueId")) == disk_id
             and physical.get("BusType") == disk["BusType"]
             and _integer(physical.get("Size"), 1) == size, "guest_physical_disk_mismatch")
    media = _text(physical.get("MediaType"))
    _require(media in {"Unspecified", "SSD"}, "guest_media_unsupported")
    partitions = {}
    for partition in _array(guest.get("Partitions")):
        part_number = _integer(partition.get("PartitionNumber"), 1)
        _require(part_number not in partitions
                 and _integer(partition.get("DiskNumber")) == number
                 and _identity(partition.get("DiskId")) == disk_path, "guest_partition_inventory")
        access = partition.get("AccessPaths")
        _require(access is None or type(access) is list and len(access) <= 16,
                 "guest_partition_access_paths")
        partitions[part_number] = {
            "guid": _guid(partition.get("Guid")),
            "access_paths": sorted(_identity(item) for item in (access or [])),
            "offset": _integer(partition.get("Offset")), "size": _integer(partition.get("Size"), 1),
        }
    volumes = _array(guest.get("Volumes"))
    associations = _array(guest.get("VolumePartitions"))
    by_volume, by_association = {}, {}
    for volume in volumes:
        identity = _identity(volume.get("UniqueId"))
        _require(identity == _identity(volume.get("Path")) and identity not in by_volume,
                 "guest_volume_identity")
        _require(volume.get("FileSystemType") in {"NTFS", "FAT32"}, "guest_volume_unsupported")
        by_volume[identity] = volume
    for association in associations:
        identity = _identity(association.get("VolumeUniqueId"))
        _require(identity in by_volume and identity not in by_association, "guest_volume_association")
        partition = _one(association.get("Partitions"))
        part_number = _integer(partition.get("PartitionNumber"), 1)
        _require(_integer(partition.get("DiskNumber")) == number
                 and _identity(partition.get("DiskId")) == disk_path, "guest_partition_disk_mismatch")
        access = partition.get("AccessPaths")
        _require(type(access) is list and len(access) <= 16
                 and identity in [_identity(item) for item in access], "guest_partition_volume_mismatch")
        _require(part_number in partitions
                 and _guid(partition.get("Guid")) == partitions[part_number]["guid"]
                 and sorted(_identity(item) for item in access) == partitions[part_number]["access_paths"],
                 "guest_partition_association_mismatch")
        by_association[identity] = {
            "number": part_number,
            "guid": _guid(partition.get("Guid")),
            "access_paths": sorted(_identity(item) for item in access),
            "filesystem": by_volume[identity]["FileSystemType"],
        }
    _require(bool(by_volume) and by_volume.keys() == by_association.keys(), "guest_volume_inventory")
    path_volumes = {}
    for entry in _array(guest.get("PathVolumes"), 32):
        role = _text(entry.get("Role"))
        _require(role in paths and role not in path_volumes, "workload_path_role")
        requested, resolved = _path(entry.get("RequestedPath")), _path(entry.get("ResolvedPath"))
        _require(requested == paths[role] and resolved == requested, "workload_path_mismatch")
        existing = _path(entry.get("ExistingPath"))
        _require(existing in _parents(requested), "workload_ancestor_mismatch")
        components = _array(entry.get("PathComponents"), 256)
        _require([_path(item.get("Path")) for item in components] == _parents(existing),
                 "workload_ancestors_incomplete")
        _require(all(not (_integer(item.get("Attributes")) & 1024) for item in components),
                 "workload_reparse_point")
        _require(existing == requested or components[0]["Attributes"] & 16,
                 "workload_ancestor_not_directory")
        volume = _identity(entry.get("VolumeUniqueId"))
        _require(volume in by_volume, "workload_volume_missing")
        drive_root = ntpath.splitdrive(requested)[0] + "\\"
        _require(drive_root in by_association[volume]["access_paths"], "workload_volume_drive_mismatch")
        path_volumes[role] = {"path": requested, "volume": volume}
    _require(path_volumes.keys() == paths.keys(), "workload_path_inventory")
    _require(_integer(guest.get("MemoryGiB"), 1) == profile["memory_gib"]
             and _integer(guest.get("Cores"), 1) == profile["cores"], "guest_profile_mismatch")
    _integer(guest.get("AvailableMemoryBytes"))
    system = _text(guest.get("SystemDrive")).casefold()
    _require(system + "\\" in [item for value in by_association.values()
             for item in value["access_paths"]], "guest_system_volume_missing")
    return {
        "machine_uuid": _guid(guest.get("MachineUUID")), "boot_utc": boot,
        "system_drive": system, "cpu_model": _text(guest.get("CpuModel")),
        "power_mode": _text(guest.get("PowerMode")), "cores": guest["Cores"],
        "memory_gib": guest["MemoryGiB"],
        "instruction_requirements": _text(guest.get("InstructionRequirements")),
        "disk": {"number": number, "path": disk_path, "unique_id": disk_id,
                 "guid": _guid(disk.get("Guid")), "size": size, "bus": disk["BusType"],
                 "device": pnp, "controller": controllers[0], "media": media,
                 "scsi": [_integer(device.get(field)) for field in
                          ("SCSIBus", "SCSIPort", "SCSITargetId", "SCSILogicalUnit")]},
        "partitions": {str(key): value for key, value in sorted(partitions.items())},
        "volumes": by_association, "paths": path_volumes,
    }


def _backing(value, inventory):
    backing = _object(value)
    volume = _object(backing.get("Volume"))
    partition = _one(backing.get("Partitions"))
    disk = _one(backing.get("Disks"))
    physical = _one(backing.get("PhysicalMatches"))
    volume_id = _identity(volume.get("UniqueId"))
    _require(volume_id == _identity(volume.get("Path")) and volume.get("FileSystemType") == "NTFS",
             "host_volume_unsupported")
    access = partition.get("AccessPaths")
    _require(type(access) is list and len(access) <= 16
             and volume_id in [_identity(item) for item in access], "host_volume_partition_mismatch")
    _require(_integer(partition.get("DiskNumber")) == _integer(disk.get("Number"))
             and _identity(partition.get("DiskId")) == _identity(disk.get("Path")),
             "host_partition_disk_mismatch")
    unique, serial = _identity(disk.get("UniqueId")), _text(disk.get("SerialNumber"))
    matches = [item for item in inventory
               if isinstance(item.get("UniqueId"), str) and item["UniqueId"].strip().casefold() == unique
               and isinstance(item.get("SerialNumber"), str) and item["SerialNumber"].strip() == serial]
    _require(len(matches) == 1, "host_physical_identity_ambiguous")
    _require(_identity(physical.get("UniqueId")) == unique
             and _text(physical.get("SerialNumber")) == serial, "host_physical_identity_mismatch")
    _require(disk.get("BusType") == "NVMe", "host_storage_topology_unsupported")
    size = _integer(disk.get("Size"), 1)
    for observed in (physical, matches[0]):
        _require(observed.get("MediaType") == "SSD" and observed.get("BusType") == disk["BusType"]
                 and _integer(observed.get("Size"), 1) == size, "host_ssd_not_established")
    return {"path": _path(backing.get("Path")), "volume": volume_id,
            "partition_guid": _guid(partition.get("Guid")),
            "partition_number": _integer(partition.get("PartitionNumber"), 1),
            "partition_offset": _integer(partition.get("Offset")),
            "partition_size": _integer(partition.get("Size"), 1),
            "disk_path": _identity(disk.get("Path")), "disk_guid": _guid(disk.get("Guid")),
            "unique_id": unique, "serial": serial, "size": size, "bus": "NVMe", "media": "SSD"}


def _validate_snapshot(snapshot, local_guest, paths, expected_profile):
    snapshot, profile = _object(snapshot), _object(expected_profile)
    _require(snapshot.get("schema") == "analytics-storage-snapshot.v1", "schema_unsupported")
    _require(profile.get("os") == "Windows" and profile.get("disk") == "SSD", "profile_unsupported")
    _integer(profile.get("cores"), 1)
    _integer(profile.get("memory_gib"), 1)
    paths = _object(paths)
    _require(0 < len(paths) <= 32 and all(isinstance(role, str) and role.strip() == role and role
             for role in paths), "workload_paths_required")
    paths = {role: _path(path) for role, path in paths.items()}
    host, transport = _object(snapshot.get("Host")), _object(snapshot.get("Transport"))
    vm = _object(host.get("VM"))
    identity = _guid(vm.get("Id"))
    _require(transport.get("Kind") == "powershell-direct-vmid"
             and _guid(transport.get("VMId")) == identity, "transport_vm_mismatch")
    _utc(host.get("ObservedUtc"))
    _require(vm.get("Generation") == 2 and type(vm.get("Generation")) is int
             and vm.get("DynamicMemoryEnabled") is False, "vm_topology_unsupported")
    _require(_integer(vm.get("ProcessorCount"), 1) == profile["cores"]
             and _integer(vm.get("MemoryStartup"), 1) == profile["memory_gib"] * 1024**3,
             "host_profile_mismatch")
    attachment, controller = _one(host.get("Attachments")), _one(host.get("ScsiControllers"))
    _require(_guid(attachment.get("VMId")) == identity and _guid(controller.get("VMId")) == identity,
             "attachment_vm_mismatch")
    _require(type(attachment.get("ControllerType")) is int and attachment["ControllerType"] == 1
             and "DiskNumber" in attachment and attachment["DiskNumber"] is None
             and attachment.get("SupportPersistentReservations") is False, "attachment_unsupported")
    controller_number = _integer(attachment.get("ControllerNumber"))
    _require(controller_number == _integer(controller.get("ControllerNumber")), "controller_mismatch")
    location = _integer(attachment.get("ControllerLocation"))
    leaf = _path(attachment.get("Path"))
    chain = _one(host.get("Chains"))
    _require(_path(chain.get("AttachmentPath")) == leaf, "chain_attachment_mismatch")
    nodes = _array(chain.get("Nodes"), 16)
    _require(bool(nodes), "chain_missing")
    normalized, seen = [], set()
    for index, node in enumerate(nodes):
        path = _path(node.get("Path"))
        _require(path not in seen and (index != 0 or path == leaf), "chain_path_mismatch")
        seen.add(path)
        terminal = index == len(nodes) - 1
        parent = node.get("ParentPath")
        _require(node.get("VhdFormat") == "VHDX", "chain_format_unsupported")
        if terminal:
            _require(node.get("VhdType") in {"Fixed", "Dynamic"} and parent == "", "chain_incomplete")
        else:
            _require(node.get("VhdType") == "Differencing"
                     and _path(parent) == _path(nodes[index + 1].get("Path")), "chain_parent_mismatch")
        normalized.append({"path": path, "parent": "" if terminal else _path(parent),
                           "identifier": _guid(node.get("DiskIdentifier")), "type": node["VhdType"],
                           "size": _integer(node.get("Size"), 1),
                           "logical_sector": _integer(node.get("LogicalSectorSize"), 1),
                           "physical_sector": _integer(node.get("PhysicalSectorSize"), 1)})
    _pools(host.get("StoragePools"))
    inventory = _array(host.get("PhysicalInventory"))
    backings = [_backing(item, inventory) for item in _array(host.get("Backings"), 16)]
    _require(len(backings) == len(nodes) and {item["path"] for item in backings} == seen,
             "backing_inventory_mismatch")
    guest = _guest(snapshot.get("Guest"), paths, profile)
    _require(guest == _guest(local_guest, paths, profile), "local_remote_guest_mismatch")
    _require(all(item["size"] == guest["disk"]["size"] for item in normalized), "virtual_size_mismatch")
    return {"schema": "analytics-storage-topology.v1", "vm_id": identity,
            "host": {"generation": vm["Generation"], "version": _text(vm.get("Version")),
                     "cpu_model": _text(host.get("CpuModel")), "power_mode": _text(host.get("PowerMode")),
                     "cores": vm["ProcessorCount"], "memory_bytes": vm["MemoryStartup"]},
            "attachment": {"path": leaf, "controller": controller_number, "location": location},
            "chain": normalized, "backings": sorted(backings, key=lambda item: item["path"]), "guest": guest}


def validate_snapshot(snapshot, local_guest, paths, expected_profile):
    """Return comparable topology after validating both independently observed sides."""
    try:
        return _validate_snapshot(snapshot, local_guest, paths, expected_profile)
    except (TypeError, KeyError, AttributeError, IndexError) as error:
        raise ValueError("storage_snapshot_malformed") from error
