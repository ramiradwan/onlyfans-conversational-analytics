<!-- CODE-VERIFY: app/core/desktop_release.py app/core/runtime_paths.py app/core/startup_maintenance.py app/packaged_entry.py app/security/local_data_key.py desktop/src-tauri/src/deployment.rs desktop/src-tauri/src/policy.rs desktop/src-tauri/src/main.rs tests/test_startup_maintenance.py tests/test_desktop_release.py. -->

# Desktop startup storage

The packaged Brain acquires a process-held data-root lease before importing the configured application or constructing a provisioning store. The OS releases that lease when the process terminates. Reconfiguration exit 75 uses a new process; it does not release ownership early while the old process can still write.

The lock order is the lifetime `.ofca-runtime-writer.lock`, the existing `.bridge-installation-migration.lock`, then database connections. Startup uses the existing migration-lock implementation on a separate file for the lifetime lease. It does not recursively acquire the migration lock. A competing launch refuses before a migrating constructor. The existing named Brain mutex also refuses a same-session older process that still publishes the old installer signal. Older binaries in other sessions and registered conflicting installations still require explicit transition qualification; they cannot be assumed to honor this protocol.

Root inspection creates no configuration, keys or databases and repairs no permissions. It distinguishes fresh, saved provisioning and configured state. A retained runtime database without configuration, orphaned WAL/shared-memory files, an unknown database layout, unsafe paths or an unavailable original key refuses boot. Configured state must reference the same four managed database paths. Schema/catalog inspection and a coordinated cross-store checkpoint remain the next maintenance gate; the filesystem classification is not schema admission.

The key helper's noncreating path verifies private-file permissions and reopens the existing DPAPI material. Its explicit test key remains confined to the existing test-mode port. Missing keys beside retained databases never cause a new master key to be generated. Actual original hardware-key reopening and installed DPAPI use still need separate qualification.

Parent-lifetime observation begins before this inspection and before application construction. A closed parent prevents further startup work at the next safe boundary. The native job remains crash containment. Existing SQL transactions and graceful ASGI shutdown are retained; this observer does not claim cross-store migration cancellation is complete.

## Package metadata and qualification isolation

Native and backend consumers validate the immutable `desktop-release.json` against Windows `GetCurrentPackageId`. Missing metadata in a packaged process, a descriptor in an unpackaged process, identity mismatch, unknown/duplicate fields or a Store descriptor carrying a direct feed refuses. The descriptor describes executable distribution and storage; it grants no account or update authority. Update-source registration validation remains required before servicing.

Production and legacy default to `%LOCALAPPDATA%\OnlyFans Conversational Analytics`. Isolated qualification defaults to its `qualification\<package Name>` subdirectory. Qualification overrides can use a dedicated external test root but cannot adopt the production root or another qualification profile. Native and Python path selection reject aliases and installation containment before creating retained state. Native selection also avoids creating an absent root.

Bridge explicitly puts its WebView2 profile in `<resolved data root>\webview`. This is writable storage under the governed root and is independent of the versioned WindowsApps path. It is presentation state, separate from encrypted authority. Reset/Repair and independent profile loss still need installed recovery tests. No Chrome profile is read or copied.

Contract fixtures and regeneration commands are in [shared desktop release fixtures](../../shared/desktop-release/README.md). Qualification construction does not qualify installed storage or updates; see [MSIX packaging](../../packaging/msix/README.md).
