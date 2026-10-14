<!-- CODE-VERIFY: Check build-windows.ps1, build-msix.ps1, verify-msix.ps1, tools/msix_packaging.py, runtime-files.json and tests/test_msix_packaging.py before changing commands or qualification claims. -->

# MSIX packaging

This adapter packs the qualified Tauri Bridge and one-directory frozen Brain together. It shares runtime qualification with Inno. Inno remains the default. [ADR 0056](../../docs/adr/0056-msix-packaging-and-managed-updates.md) records the implementation decision; construction does not qualify installed storage, startup or updates.

## Approved inputs and release boundary

The customer MSIX baseline is **Windows 11 25H2 or later, x64** (`10.0.26200.0`). Isolated qualification can exercise the selected feature floor at Windows 11 24H2 (`10.0.26100.0`). Minimum and tested versions are explicit profile inputs; the producer does not infer them from its host.

The approved nonsecret defaults are Application Id `Bridge`, PublisherDisplayName `Dipsy`, and configurable direct Name `Dipsy.OnlyFansConversationalAnalytics`. Schema defaults document these choices; they do not fill missing values during validation.

Production certificate Subject DN, Store reservation and live HTTPS App Installer URL remain external provisioning gates. Copy the exact Subject DN from the verified signing identity, without example locality placeholders or manual normalization. Keep the Name and Publisher stable across certificate renewal. Capture Windows-derived full/family names in release verification and compare the existing approved family on later releases.

`production-direct.template.json` deliberately fails validation: Publisher is empty and the feed URI is null. Its WebView2 version is an initial fixture value, not an installed prerequisite qualification claim. There is no automatic fallback from an incomplete production profile to a synthetic identity. Neither a validated profile nor a package build authorizes production publication or distribution cutover.

| Profile | Purpose | Update owner |
| --- | --- | --- |
| `profiles/qualification-direct.json` | Isolated synthetic Name and Publisher; `.example.test` URI is a rendering fixture | Direct App Installer |
| `profiles/qualification-store.json` | Separate synthetic Name and Publisher; no direct feed | Microsoft Store candidate |
| `profiles/production-direct.template.json` | Incomplete production input template; must refuse until externally verified inputs are supplied | Direct App Installer |

Qualification identities must not be used for customer distribution, Store submission or retained production state. Qualification scripts require an explicit `-AllowQualification` for release verification. Production verification refuses these profiles.

The deployment counter maps to `1.<sequence div 65536>.<sequence mod 65536>.0`. It advances independently of application version and licensed major. Both distributions can use the same executable build, but their identities and channel metadata are separate outputs. There is no downgrade setting in the generated App Installer descriptor.

## Build and inspect

Use an isolated build Python containing the pinned runtime dependencies, freshly built fixed SQLCipher wheel and `packaging/requirements-build.txt`. Do not freeze the development `.venv`. Node 22 must meet dependency engine requirements; the native build needs the pinned Rust toolchain and registered MSVC C++ tools. Install Windows SDK `10.0.26100.0`, including its x64 MakeAppx/SignTool and UAP v17 schema. Validation is mandatory; `/nv` is never used.

From the repository root, build a qualification package with a fresh output directory outside the repository:

```powershell
.\packaging\build-windows.ps1 `
  -BuildPython C:\build\ofca-msix\Scripts\python.exe `
  -OutputRoot C:\build\ofca-msix-output `
  -DesktopPackageTarget msix `
  -MsixPackageProfile .\packaging\msix\profiles\qualification-direct.json `
  -LegalReleaseBindings .\extension\tests\fixtures\legal-instrument-bindings.synthetic.json `
  -DevelopmentAgentBundle
```

`-DevelopmentAgentBundle` keeps the Agent output explicitly developmental; it does not prove the production Chrome journey. The reviewed synthetic Legal fixture above is for qualification only. The complete-stage gate still requires its Legal declaration. Customer release mode retains its real Legal bindings and extension qualification. `-DesktopPackageTarget both` runs both adapters around the same qualified stage. An MSIX-only build does not need an Inno compiler or stage Inno's WebView2 installer helper.

These initial outputs qualify construction. Do not activate them against retained customer state: qualification data-root isolation and native storage/admission fences remain required before installed testing. No qualification package has been released or installed by these producer checks.

For an already qualified complete runtime stage:

```powershell
.\packaging\build-msix.ps1 `
  -BuildPython C:\build\ofca-msix\Scripts\python.exe `
  -StagingRoot C:\build\qualified-runtime `
  -PackageProfile .\packaging\msix\profiles\qualification-store.json `
  -OutputRoot C:\build\ofca-store-output
```

The producer verifies the **whole stage before selecting files**. Agent metadata, public signing-publication trust, frontend closure, SQL catalogs and forbidden-material checks still apply. `runtime-files.json` owns the adapter classification: Agent is a qualified linked build input, Inno's helper is an Inno resource, and the MSIX descriptor/digests are generated after payload selection.

The full MSIX wrapper extracts the embedded Brain and Bridge PE manifests with `verify-pe-manifests.ps1`. Both must declare `longPathAware` and ordinary `asInvoker` privileges. The pinned PyInstaller default supplies Brain's declaration; Bridge uses `desktop/src-tauri/bridge.manifest`. Keep the legacy installed path budget regardless of these declarations, since native dependencies and OS long-path configuration still need qualification. The inspection receipt stays outside the package.

The package contains `app/Bridge.exe`, `app/Brain.exe`, `app/_internal`, the common runtime resources, `app/desktop-release.json`, `app/sha256sums.txt`, the manifest and generated logos. Inventory, profile and build receipt are outside the package. MakeAppx packs with semantic validation, unpacks to a fresh directory, and the producer compares the unpacked closure and hashes with the sealed inventory. Production preparation also requires clean committed producer source.

The build receipt marks a dirty qualification producer explicitly; its base commit is not a claim that uncommitted sources are that exact revision. MSIX ZIP part names can percent-escape installed names, including tzdata's `GMT+1`. Verification decodes those names once and still rejects traversal, encoded separators and aliases. The unpacked filesystem closure is checked independently.

The current package signing policy does not add embedded signatures to every inner PE. If a measured WDAC or distribution requirement changes that policy, sign the inner PEs **before** common release manifests, inner digests and MSIX inventory are frozen. Add a separate controlled signing boundary and return signed contents to the packing owner; the outer signing job must not rebuild or alter them. A signed package's content protection does not establish compatibility with every customer's WDAC policy. [Microsoft package signing](https://learn.microsoft.com/en-us/windows/msix/package/signing-package-overview)

## Verify the release artifact

`verify-msix.ps1` requires PowerShell 7.4+ and uses installed Windows/.NET verification facilities. It does not install dependencies, register a package or build application/package contents. Its small Appx COM binding computes identity through Windows, rather than inventing a publisher-hash algorithm. Loading that binding is verification code, not an application build step.

Direct release verification requires the exact `.msix`, its inventory and profile, the approved manifest policy, all installed payload hashes, a valid SignTool trust chain, the exact signer Subject DN and a timestamp. An EXE signature, empty directory, unsigned package or synthetic profile cannot satisfy this gate. Pass the previously approved Windows-derived family name for release continuity:

```powershell
pwsh -File .\packaging\verify-msix.ps1 `
  -Package C:\release\desktop.msix `
  -Inventory C:\release\desktop.inventory.json `
  -PackageProfile C:\release\desktop.profile.json `
  -ExpectedFamilyName '<approved Windows-derived package family>' `
  -ReceiptPath C:\release\desktop.verification.json
```

`-VerificationMode StoreSubmission` requires a Store profile and the unsigned candidate. That receipt proves submitted payload/identity; Store re-signing and actual Store-delivered installation/update evidence are distinct facts. Do not compare the submission's outer hash with a Store-re-signed package or present the unsigned receipt as trusted installed delivery.

Add `-AllowQualification` only for isolated tests. Real SDK tests use stand-in Windows PEs to prove semantic packing and closure. They do **not** prove PyInstaller/Tauri startup, a trusted timestamped direct signature or an installed customer journey. The direct positive signature and chain/timestamp failure matrix remain required release qualification.

## App Installer and publication

`build-msix.ps1 -PackageUri <immutable HTTPS .msix URL>` emits a direct descriptor. Its exact self-URI comes from the profile; the package URI must share that approved HTTPS origin. Store profiles cannot emit or register a direct feed. The descriptor uses a six-hour launch-check interval, background checking, no routine prompt, no activation block and no unrestricted downgrade. Windows documents a separate eight-hour background cadence; policy and offline state can prevent checks. [App Installer update settings](https://learn.microsoft.com/en-us/windows/msix/app-installer/update-settings)

Publication remains disabled until the real hosting route is verified. Upload immutable signed bytes first, verify served bytes, then promote the short-cached descriptor. Qualification must check exact self-URI/no redirects, MIME types, GET/HEAD lengths, byte ranges/resume, no transparent compression/transformation, TLS and 404-cache behavior. Do not infer these properties from an SDK pack or a Worker configuration alone. The `.example.test` profile cannot provide an actual customer installation route.

## External-review disposition and installed gates

These are release obligations, not claims that the producer has fixed the runtime:

| Review finding | Disposition / decisive check |
| --- | --- |
| WindowsApps path checks, versioned paths and MAX_PATH | Keep strict listener PID/image/SID/origin admission. Qualify image verification with a standard user in real WindowsApps; prefer file identity over fragile textual aliases. Never persist a versioned executable path. Producer rejects paths exceeding the legacy UTF-16 budget. Inspect both real PE manifests and native dependency file operations; `longPathAware` alone is insufficient without the global `LongPathsEnabled` opt-in. [Windows long paths](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation) |
| Reset/Repair and WebView2 profile loss | Set a governed stable writable profile location or demonstrate independent WebView2 loss recovery against retained encrypted authority. Test actual Reset/Repair separately from uninstall; package-container removal and the excluded Product root have different lifetimes. [Flexible virtualization](https://learn.microsoft.com/en-us/windows/msix/desktop/flexible-virtualization), [WebView2 user-data folder](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/user-data-folder) |
| Deferred servicing and self-restart | Manifest declares `defer`; qualify the entire Bridge/Brain/WebView2 lifetime and pending package through a real A→B update. Do not self-spawn Bridge before exit. Normal servicing must not rely on the 15-second forced child cleanup. `AppInstance.Restart` supports packaged Win32, but its use here and successful pending-package replacement still need proof; the API name alone does not guarantee the review's proposed behavior. [Restart API](https://learn.microsoft.com/en-us/windows/apps/windows-app-sdk/applifecycle/applifecycle-restart), [in-use declaration](https://learn.microsoft.com/en-us/uwp/schemas/appxpackage/uapmanifestschema/element-uap17-updatewhileinuse) |
| Inner PE signing | Keep the current policy until a concrete requirement changes it. Test relevant WDAC/AV environments; do not treat universal blocking or false positives as established platform behavior. Any additional inner signatures precede immutable hashes/packing. |
| CWD and temporary files | Qualify Start/protocol activation from System32 and arbitrary working directories; establish safe writable CWD/temp explicitly where needed. Bound temp cleanup and keep it away from authoritative files and checkpoints. Do not assume every activation always has one CWD. |
| Chrome return foreground rights | Pinned `tauri-plugin-single-instance` 2.5.3 already grants the primary PID foreground permission on Windows. Preserve it and test cold, warm and hidden Chrome returns; duplicate native calls are not the first remedy. |
| Delivery Optimization / exact URI | Validate actual served headers and byte behavior, including no transformation/compression, exact self-URI and no redirect. The review's universal error-code/Content-Length claim is not a substitute for route testing. |
| Certificate renewal | Compare the exact approved Publisher plus Windows-derived family/full name and direct signing certificate subject. Verify each renewal before release; a guessed example DN is forbidden. [Microsoft signing guide](https://learn.microsoft.com/en-us/windows/msix/package/sign-msix-package-guide) |
| Store capability rejection | Obtain restricted-capability approval separately. Refusal requires a deliberate product/distribution decision; do not silently remove the exclusion or move retained authority into package-managed storage. A universal SQLite corruption claim over redirection is unproven. Any approved alternative needs its own explicit LocalFolder/path, WAL concurrency, retention and reset qualification. |

The implemented [startup storage boundary](../../docs/operations/desktop-startup-storage.md) holds a process-lifetime root lease, binds package descriptors to Windows identity, isolates qualification roots and sets a stable WebView2 profile path. Coordinated encrypted checkpoints, forward migration/resume and child-owned status framing now have source and native OS tests. Newly built A/B schema transitions through the real launcher and large-data timing still require qualification before that maintenance milestone is accepted. Remaining release gates include older/mixed-install transition detection, corrective-target recovery, native prerequisites, registered-source validation and graceful servicing, protected workflow integration, and installed direct A→B continuity. The production feed and Store route require their own verified external inputs. Inno retirement and public cutover remain separately approved actions.

Regenerate the technical MSIX PNG sizes from the approved existing icon with `generate-assets.ps1`. Do not hand-edit those generated files or introduce separate branding for package qualification.
