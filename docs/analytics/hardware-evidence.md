<!-- CODE-VERIFY: Check tools/analytics_qualification_hardware_evidence.py, tools/analytics_qualification_storage.py, tools/analytics_qualification_guest.ps1 and acceptance-manifest.json before changing this contract. -->

# Virtual storage evidence

The declared Windows VM profiles retain the SSD requirement through separately observed host backing storage. Guest media reported as `Unspecified` stays unchanged in raw evidence. Qualification records the actual CPU, power configuration, virtualization and resource limits. These measurements do not establish physical-laptop performance.

## Supported topology

The validator accepts one file-backed SCSI attachment on a generation-2 VM with static memory. The guest must expose one boot and system disk, consistently identified by `Get-Disk`, `Win32_DiskDrive` and `Get-PhysicalDisk`. Its device model, PnP identity, interface path and virtual SCSI controller must match the supported Windows virtual storage device. Additional guest virtual disks, storage pools or unassociated volumes are rejected.

PowerShell Direct targets the recorded VM ID. Matching local and remote guest UUID, boot time and storage observations bind that VM to the qualification process. Within this restricted topology, the sole attached disk is the guest's sole boot disk. The validator does not convert guest disk identifiers into VHD identifiers.

Every VHDX node must have observed backing storage. A chain contains at most 16 distinct paths, follows each differencing parent and ends at a fixed or dynamic base. Each backing file maps through its actual volume and partition to a direct NVMe disk. Exactly one physical disk must match its nonempty identifier and serial, with matching bus and size and reported SSD media. Unknown, remote, pooled or ambiguous backing storage is unsupported.

## Snapshot contract

`validate_snapshot(snapshot, local_guest, paths, expected_profile)` returns a comparable topology or raises `ValueError`. `paths` maps workload roles to absolute local Windows paths. `expected_profile` supplies the manifest's OS, cores, memory and disk requirements.

The snapshot uses `schema: analytics-storage-snapshot.v1` and these records:

| Record | Required observations |
|---|---|
| `Transport` | `Kind: powershell-direct-vmid`, `VMId` |
| `Host` | `ObservedUtc`, `CpuModel`, `PowerMode`, `VM`, `Attachments`, `ScsiControllers`, `Chains`, `Backings`, `PhysicalInventory`, `StoragePools` |
| `Host.VM` | `Id`, `Generation`, `Version`, `ProcessorCount`, `MemoryStartup`, `DynamicMemoryEnabled` |
| `Host.Attachments[]` | `VMId`, `ControllerType`, `ControllerNumber`, `ControllerLocation`, `Path`, `DiskNumber`, `SupportPersistentReservations` |
| `Host.Chains[]` | `AttachmentPath`, ordered `Nodes` retaining path, identifier, type, format, size, parent and sector sizes from `Get-VHD` |
| `Host.Backings[]` | `Path`, `Volume`, `Partitions`, `Disks`, `PhysicalMatches`, retaining the provider associations and identifying fields |
| `Guest` | The output of the source-bound guest collector described below |

Raw summaries such as `AllSSD`, `Complete` or `Unchanged` do not replace validation of the arrays and joins. The host producer's selected command projections must retain the fields consumed by the validator. Unsupported observations fail instead of becoming empty arrays.

## Guest collector

`tools/analytics_qualification_guest.ps1 -PathsFile <path>` reads a UTF-8 JSON object mapping roles to paths and emits one UTF-8 JSON object. It supports Windows PowerShell 5.1. The same source-bound script runs locally and through PowerShell Direct.

It records `ObservedUtc`, `BootUtc`, `MachineUUID`, `SystemDrive`, `CpuModel`, `PowerMode`, `MemoryGiB`, `Cores`, `InstructionRequirements` and `AvailableMemoryBytes`. Processor architecture comes from `Win32_Processor.Architecture`. Power mode combines the sole active `Win32_PowerPlan` instance ID and name. Storage records are `DiskCount`, `Disks`, `Partitions`, `Volumes`, `VolumePartitions`, `DiskDevices`, `Controllers`, `PhysicalDisks`, `VirtualDisks`, `StoragePools` and `PathVolumes`.

Each `PathVolumes` entry retains `Role`, `RequestedPath`, `ResolvedPath`, `ExistingPath`, `VolumeUniqueId` and `PathComponents`. Components contain `Path` and numeric `Attributes` from the existing ancestor through the drive root. Reparse points, including volume mount points, are rejected. A future workload path uses its nearest existing directory's volume. The post observation checks the resulting path. Every observed volume must map to a partition on the single guest disk.

## Attempt handoff

The runner reads an external `analytics-hardware-handoff.v1` configuration containing `directory`, `vm_id` and the reviewed host producer's `producer_sha256`. This contains no credentials. The coordinator transports requests and responses through PowerShell Direct without enabling guest networking.

Each attempt has separate `pre.request.json` and `post.request.json` files. Requests use `analytics-hardware-evidence.v1`, a fresh nonce, phase, source-bound guest collector hash and the attempt binding. The response repeats that schema and supplies the request digest, producer hash, collector hash and raw `snapshot`. Publish each response as a complete file without replacing an existing response.

The runner brackets each response with local guest observations. The pre response must validate before worker launch. The post response follows joined worker cleanup. Each response has a bounded wait. Missing pre evidence blocks launch. Invalid or missing post evidence fails started work.

The final verifier reads the archived records, recomputes their joins and binds them to the source, manifest, attempt, worker and actual workload paths. Stable topology includes CPU, power and resource settings. It excludes observation times, process IDs, changing free memory and the nearest existing ancestor. Neither a copied SSD label nor evidence from another attempt qualifies the run. Worker budgets and latency thresholds remain those in the acceptance manifest.
