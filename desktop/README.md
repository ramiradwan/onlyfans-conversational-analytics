# Native desktop host

<!-- CODE-VERIFY: src-tauri/Cargo.toml src-tauri/build.rs src-tauri/src/main.rs src-tauri/src/host.rs src-tauri/src/windows.rs src-tauri/capabilities/desktop.json -->

Bridge hosts the local application in Windows WebView2. It starts the adjacent `Brain.exe --brain`, checks the child-owned loopback listener, and admits the local document only after the handoff succeeds. The complete frozen Brain directory must accompany the executable.

The host uses its ephemeral child-launch credential to request `/api/v1/desktop/entry`. It navigates to the fixed `/onboarding/entry` landing with a one-use fragment code. The backend projection selects the remaining local task or offers dashboard access. The host does not read an enrollment bootstrap token or select the page from `runtime.env`.

Browser tasks open in installed Chrome through `desktop_browser_task`. Its sole input is a one-use entry code for the fixed local helper landing. The host chooses no Chrome profile and reads no profile files. The helper returns recent foreground tasks to the matching desktop journey automatically, subject to the platform app-link prompt. A contextual native retry prepares and launches the permitted original task. Chrome continues to own profile selection. Expired presentation admission is refreshed through the native launcher for the same saved journey; domain deadlines are unchanged. Lost refresh replies recover only that exact successor and cannot replace another presentation.

`desktop_update_status` observes Windows servicing metadata only after live local presentation admission. It accepts no URL or update selection. The native host binds the immutable release descriptor to Windows identity, installation origin, signature kind and the exact registered App Installer URI. A direct query has a 20-second budget, observes cancellation and admits only one concurrent check. Missing sources and failed checks remain explicit; Store installations do not query the direct feed. This command does not stage packages, drain Brain or restart the app.

The native servicing coordinator reads at most 16 KiB of metadata from that exact source, with TLS, fixed timeouts and no redirects. It requires the registered automatic-update policy and the producer's closed descriptor, then asks Windows to stage the signed package. It resolves only the expected same-family staged full name through `GetStagedPackagePathByFullName`, rejects unsigned origins, rechecks source and policy, and requests deferred registration through the Windows-owned manifest. Cancellation or failure cannot authorize drain. Windows package enumeration requiring administrator rights is not used.

After verified drain and child exit, the authenticated native helper waits outside package identity and activates the stable application ID. Explicit Quit cancels relaunch. Ordinary isolated A→B servicing has exercised this path with a complete runtime and a real schema transition; [MSIX qualification](../packaging/msix/README.md) records its coordinates and remaining customer-delivery and continuity gates.

The separately built browser helper connects to the exact extension through its task-owned port. Extension-first attachment requires the saved journey and launch/adoption evidence. Consent and permissions remain extension-owned; only a permitted coarse account observation reaches Brain. Installing the extension refreshes matching helper documents without navigating or closing unrelated tabs. The existing explicit cross-device receiving flow remains in the extension.

Close hides the window. The tray offers Open and Quit. Quit closes the parent's lifetime pipe and waits for ordinary shutdown before terminating the owned Job Object if necessary. A host crash closes the Job Object. Unexpected child failure requires an explicit application restart; configuration completion permits one controlled restart.

The window has no OS title bar. The app header carries the name, drag region and keyboard-accessible minimize, maximize/restore and close controls. Double-click the noninteractive header to maximize or restore. Windows edge resizing and keyboard window shortcuts remain available. Native-owned controls also cover startup and service failure, with a fallback header when a local document cannot render. Ordinary Chrome pages leave the custom control element inert. The separately scoped window command cannot launch, navigate, quit the service or access files.

Build from an x64 MSVC developer shell with Rust 1.98.1 and the Windows SDK:

```powershell
cargo build --manifest-path desktop/src-tauri/Cargo.toml --locked --release --bin Bridge --no-default-features --features custom-protocol --target x86_64-pc-windows-msvc
cargo test --manifest-path desktop/src-tauri/Cargo.toml --locked --features qualification-tools -- --test-threads=1
cargo clippy --manifest-path desktop/src-tauri/Cargo.toml --locked --all-targets --features qualification-tools -- -D warnings
cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check
```

`packaging/build-windows.ps1` invokes `packaging/build-desktop.ps1`, builds the locked release `Bridge` target and stages it beside the complete frozen Brain distribution. Both components use the authoritative Brain version. One Inno installer registers the app link and shortcut for Bridge and offers to open it on Finish. The installer checks the shared Evergreen WebView2 Runtime and installs Microsoft's signed bootstrapper only if the runtime is absent; uninstall retains the shared runtime. The release manifest records the native executable and Cargo lock digests.

The WebView2 prerequisite follows [Microsoft's Evergreen distribution procedure](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution). An unavailable or invalidly signed download fails setup before application files are installed.

The qualification feature builds a process fixture for OS lifetime tests. Release packaging must build only `Bridge` without that feature. The fixture is not a substitute for qualification against installed Brain and Windows Hello.

Application commands are declared in `build.rs` and individually allowed by the `desktop` capability. The browser-task surface has no native capability. External navigation, pop-up windows, downloads, developer tools and file drops are disabled in the application window.

Permission grants requested from desktop Settings may return focus once to the same current creator and presentation within a bounded request window. Native focus requires the app or the installed Chrome process to be in the foreground under the current Windows user. An unrelated foreground app prevents the return.
