<!-- CODE-VERIFY: app/core/desktop_release.py app/core/runtime_paths.py app/core/startup_maintenance.py app/core/bootstrap_status.py app/persistence/upgrade_lifecycle.py app/packaged_entry.py app/security/local_data_key.py desktop/src-tauri/src/bootstrap.rs desktop/src-tauri/src/deployment.rs desktop/src-tauri/src/prerequisite.rs desktop/src-tauri/src/policy.rs desktop/src-tauri/src/main.rs desktop/src-tauri/tests/prerequisite.rs shared/desktop-release/prerequisite-policy.json tests/test_startup_maintenance.py tests/test_upgrade_checkpoint.py tests/test_upgrade_corrective.py tests/test_bootstrap_status.py tests/test_desktop_release.py. -->

# Desktop startup storage

Bridge checks WebView2 before constructing the Tauri runtime, a WebView profile or Brain. Its read-only loader query must find a compatible Evergreen version. The [embedded prerequisite policy](../../shared/desktop-release/prerequisite-policy.json) owns the legacy minimum and fixed Microsoft acquisition page; a package descriptor may require a higher version. The producer rejects profiles below this common floor.

Unavailable or incompatible runtimes show a Windows-native recovery dialog. **Check again** repeats the loader query. **Get WebView2** opens only the approved Microsoft page and closes Bridge; acquisition does not authorize startup. Customers reopen the app after installation. The dialog also gives offline/managed-installation guidance. Renderer commands cannot select URLs, programs or runtime folders.

The native OS test observes the actual recovery controls and the real Bridge process. A child-local missing-runtime override tests loader failure without altering the machine's shared installation. The compatible installed-runtime probe is a separate opt-in test. This evidence does not replace absent-runtime VM tests, actual online/offline acquisition, standard-user routes or Store delivery. App Installer's external dependency applies only to its installation route. [Microsoft runtime distribution](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)

The packaged Brain acquires a process-held data-root lease before importing the configured application or constructing a provisioning store. The OS releases that lease when the process terminates. Reconfiguration exit 75 uses a new process; it does not release ownership early while the old process can still write.

The lock order is the lifetime `.ofca-runtime-writer.lock`, the existing per-store migration lock, then database connections. Startup uses the existing migration-lock implementation on a separate file for the lifetime lease. It does not recursively acquire a migration lock. A competing supported launch refuses before a migrating constructor. Bridge and Brain both hold the installer-running signal; that signal is not writer-exclusion proof. Unsupported older binaries and registered conflicting installations still require explicit transition detection and qualification; they cannot be assumed to honor the root lease.

Root inspection creates no configuration, keys or databases and repairs no permissions. It distinguishes fresh, saved provisioning and configured state. A retained runtime database without configuration, orphaned WAL/shared-memory files, an unknown database layout, unsafe paths or an unavailable original key refuses boot. Configured state must reference the same four managed database paths.

Before any migrating constructor, schema admission opens each retained store with its original key and read-only SQLCipher. It reuses the migrator's exact lineage, contiguous ledger, checksums, integrity and foreign-key validation. Missing ledgers, future schemas and inconsistent checksums refuse.

An authoritative transition creates one verified encrypted auth/canonical checkpoint using SQLite backup, plus a current-user-protected copy of exact configuration and protected master-key material. Hardware-key references remain inside the encrypted auth snapshot; private hardware keys are not exported. The private, protected `.ofca-upgrade-state.dpapi` records intent only after the complete checkpoint verifies. Existing per-store `MigrationRunner` operations perform forward SQL under their ordinary locks. Completion requires all retained schemas and configuration material to validate before ordinary application construction.

An interruption resumes forward under the recorded release and catalog target. Version 2 evidence also permits a strictly newer corrective release with the same delivery identity and an unchanged catalog prefix. The recorded prefix includes pending SQL, not only committed ledger entries. Packaged releases use the Windows-validated package version; legacy releases require a newer build-owned application version. Missing version evidence and version 1 journals retain exact-target recovery.

Corrective admission verifies the original checkpoint before atomically recording successor intent. The checkpoint remains unchanged; the journal records the latest accepted release and catalog before successor SQL runs. Changed material, missing stores, damaged checkpoints, rewritten migration history or conflicting delivery identities refuse. No auth or canonical snapshot restore operation is provided.

A partially built checkpoint can be rebuilt only while its recorded source schemas and protected material still match. Completed checkpoints are removed before normal mutations can occur; the completion journal checks regression against the latest accepted target. An unchanged launch does not create a full-data backup. Projection-only migrations use the existing encrypted per-store path without duplicating authoritative data.

One active cohort is pinned outside the ordinary recovery-file pruner; its bounded state is not a fifth database or customer authority. Completion removes it immediately, including after interrupted cleanup. Existing per-store migration backups retain their existing governed deletion-barrier restore and retention rules. Source tests cover corrective admission and interruption after successor intent; installed corrective updates remain a separate qualification gate.

Closed databases without a WAL use immutable read-only access while the root lease excludes supported writers. Databases with a WAL use ordinary read-only access so committed WAL changes remain visible. An existing private WAL index is required; its loss requires explicit recovery. SQLite can update that existing scratch index, but admission cannot mutate SQL authority or create a new index. Immutable access would miss WAL commits; see [SQLite WAL documentation](https://www.sqlite.org/wal.html).

The key helper's noncreating path verifies private-file permissions and reopens the existing DPAPI material. Its explicit test key remains confined to the existing test-mode port. Missing keys beside retained databases never cause a new master key to be generated. Actual original hardware-key reopening and installed DPAPI use still need separate qualification.

Parent-lifetime observation begins before this inspection and before application construction. SQL progress, backup steps and checkpoint hashing observe cancellation. An interrupted migration rolls back its uncommitted transaction before returning; already committed migrations remain committed. Ordinary constructors and ASGI startup inherit the same supervision. The separate child-owned [bootstrap status pipe](../../shared/bootstrap-status/README.md) covers inspection and migration with bounded bytes, phase time, silence and absolute deadlines. Ready is emitted after ASGI startup completes and before Uvicorn binds the listener. It grants no listener or account authority. Closing the native parent also closes the status reader so a pending write cannot block cancellation. The Job Object remains the abnormal crash fallback.

Expected `StartupRefused` failures report the closed recovery status and return process exit 78. They do not escape into the windowed PyInstaller bootloader's exception handling. Native startup drains status before classifying an exited child. Unexpected exceptions retain their existing failure behavior.

Source tests cover real encrypted snapshots, process death partway through checkpointing and between store commits, forward resume, interrupted SQL rollback and parent EOF during pre-listener SQL. Native qualification tests cover separate handle ownership and normal pipe shutdown without forced Job cleanup.

The opt-in [frozen recovery test](../../desktop/src-tauri/tests/process_lifetime.rs) uses independently frozen auth32/auth33 artifacts. A creates the original DPAPI authority. The [synthetic fixture](../../tools/startup_maintenance_qualification_fixture.py) adds pending onboarding and license deliveries, a command and 150,000 messages in the supported canonical v11 lineage. This canonical fixture is historical retained state; it is not claimed to have been produced by the A binary, which bundles canonical v12.

Qualification interrupts the encrypted canonical checkpoint by terminating its owned Brain, then resumes and cancels normally after auth33 commits while canonical v12 is still pending. It adds a signed synthetic publication checkpoint to the live auth33 store after that cancellation. Forward resume must retain it even though the encrypted auth checkpoint predates it. Hashes and counts verify pending work, protected text, exact configuration and the original DPAPI sidecar. A second B launch must skip full-data checkpointing. These tests qualify native frozen startup and recovery, including startup beyond 30 seconds. They do not establish installed package updates, synthetic signer acceptance by production trust, original TPM-key proof, Windows Hello or the full customer launcher journey.

Run the opt-in test from `desktop/src-tauri` after building the independent artifacts. Supply absolute paths to each unpackaged common-stage Brain and a qualification Python with the pinned SQLCipher runtime. Keep these paths outside production state:

```powershell
$env:BRAIN_QUALIFICATION_A_IMAGE = '<A auth32 stage>\Brain.exe'
$env:BRAIN_QUALIFICATION_IMAGE = '<B auth33 stage>\Brain.exe'
$env:BRAIN_QUALIFICATION_PYTHON = '<qualification environment>\Scripts\python.exe'
cargo test --locked --features qualification-tools --test process_lifetime frozen_recovery_preserves_pending_work_and_newer_publication_without_restore -- --ignored --exact --nocapture --test-threads=1
```

The native test owns a unique temporary root and TCP port 17871. Run other real Brain startup tests sequentially. `BRAIN_QUALIFICATION_ROWS` can vary the synthetic workload between 100 and 500,000 rows; record the chosen size with the result. The fixture does not ship in the runtime.

## Package metadata and qualification isolation

Native and backend consumers validate the immutable `desktop-release.json` against Windows `GetCurrentPackageId`. Missing metadata in a packaged process, a descriptor in an unpackaged process, identity mismatch, unknown/duplicate fields or a Store descriptor carrying a direct feed refuses. The descriptor describes executable distribution and storage; it grants no account or update authority. Update-source registration validation remains required before servicing.

Production and legacy default to `%LOCALAPPDATA%\OnlyFans Conversational Analytics`. Isolated qualification defaults to its `qualification\<package Name>` subdirectory. Qualification overrides can use a dedicated external test root but cannot adopt the production root or another qualification profile. Native and Python path selection reject aliases and installation containment before creating retained state. Native selection also avoids creating an absent root.

Bridge explicitly puts its WebView2 profile in `<resolved data root>\webview`. This is writable storage under the governed root and is independent of the versioned WindowsApps path. It is presentation state, separate from encrypted authority. Reset/Repair and independent profile loss still need installed recovery tests. No Chrome profile is read or copied.

Contract fixtures and regeneration commands are in [shared desktop release fixtures](../../shared/desktop-release/README.md). Qualification construction does not qualify installed storage or updates; see [MSIX packaging](../../packaging/msix/README.md).
