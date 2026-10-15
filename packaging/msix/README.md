<!-- CODE-VERIFY: Check build-windows.ps1, build-msix.ps1, stage-msix-release.ps1, assert-desktop-release-inputs.ps1, verify-desktop-release.ps1, verify-msix.ps1, msix/release-artifacts.psm1, tools/msix_packaging.py, runtime-files.json and the release/producer tests before changing commands or qualification claims. -->

# MSIX packaging

This adapter packs the qualified Tauri Bridge and one-directory frozen Brain together. It shares runtime qualification with Inno. Inno remains the default. [ADR 0056](../../docs/adr/0056-msix-packaging-and-managed-updates.md) records the implementation decision; construction does not qualify installed storage, startup or updates.

## Approved inputs and release boundary

The customer MSIX baseline is **Windows 11 25H2 or later, x64** (`10.0.26200.0`). Isolated qualification can exercise the selected feature floor at Windows 11 24H2 (`10.0.26100.0`). Minimum and tested versions are explicit profile inputs; the producer does not infer them from its host.

The approved nonsecret defaults are Application Id `Bridge`, PublisherDisplayName `Dipsy`, and configurable direct Name `Dipsy.OnlyFansConversationalAnalytics`. Schema defaults document these choices; they do not fill missing values during validation.

The direct identity and serving infrastructure are provisioned. [production-direct.json](profiles/production-direct.json) and [direct-delivery.json](direct-delivery.json) record the exact Publisher `CN=Dipsy, O=Dipsy, L=London, C=GB`, expected family and approved HTTPS App Installer URI. Keep the Name and Publisher stable across certificate renewal. Capture Windows-derived full/family names in release verification and compare the approved family on later releases. The integrated deployment sequence is 9; application version remains 0.7.5 and licensed-major policy remains independent.

The owner supplied the Store draft reservation for `Dipsy.ConversationAnalytics`, Store ID `9PD4KGLX42JD`. [store-reservation.json](store-reservation.json) retains its provenance and exact identity; the production Store profile uses those values. Windows-derived PFN matches the supplied reservation. This does not establish capability approval, submission or Store delivery, and it supplies no direct-distribution signing identity.

`production-direct.template.json` deliberately fails validation: Publisher is empty and the feed URI is null. Its WebView2 version is an initial fixture value, not an installed prerequisite qualification claim. There is no automatic fallback from an incomplete production profile to a synthetic identity. Neither a validated profile nor a package build authorizes production publication or distribution cutover.

| Profile | Purpose | Update owner |
| --- | --- | --- |
| `profiles/qualification-direct.json` | Isolated synthetic Name and Publisher; `.example.test` URI is a rendering fixture | Direct App Installer |
| `profiles/qualification-store.json` | Separate synthetic Name and Publisher; no direct feed | Microsoft Store candidate |
| `profiles/production-direct.template.json` | Incomplete production input template; must refuse until externally verified inputs are supplied | Direct App Installer |
| `profiles/production-store.json` | Owner-supplied reserved Store identity; submission and delivery gates remain | Microsoft Store |

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

The owner supplied the reserved **Conversation Analytics** Store draft on 2026-10-14. [production-store.json](profiles/production-store.json) records its exact package Name, Publisher and display name. [store-reservation.json](store-reservation.json) records the supplied PFN, SID, Store ID and provenance. Windows Appx derives the same PFN from the manifest in the producer test, without registering a package.

These inputs resolve the Store reservation gate. They do not authorize submission or establish Store certification, capability approval or installed delivery. Keep qualification identities isolated. The Store Publisher is not the direct-distribution certificate subject. The intentionally invalid direct template remains a negative fixture alongside the complete direct profile.

`build-msix.ps1 -PackageUri <immutable HTTPS .msix URL>` emits a direct descriptor. Its exact self-URI comes from the profile; the package URI must share that approved HTTPS origin. Store profiles cannot emit or register a direct feed. The descriptor uses a six-hour launch-check interval, background checking, no routine prompt, no activation block and no unrestricted downgrade. Windows documents a separate eight-hour background cadence; policy and offline state can prevent checks. [App Installer update settings](https://learn.microsoft.com/en-us/windows/msix/app-installer/update-settings)

The deployed route has infrastructure serving evidence; the production bucket remains empty and the stable descriptor returns an uncached 404. Publication remains closed pending installed qualification and release approval. Upload immutable signed bytes first, verify served bytes, then promote the no-store descriptor. Each release must check exact self-URI/no redirects, MIME types, GET/HEAD lengths, byte ranges/resume, no transparent compression/transformation, TLS and 404-cache behavior. Do not infer installed delivery from the serving checks. The existing release workflow runs the supplied Worker tests and now selects explicit Inno, direct-MSIX or Store-MSIX artifact routes. Its R2 upload, served-byte verification and approved promotion steps remain unfinished.

### Protected workflow artifact routes

`windows-package.yml` retains its immutable tag, exact Product/Legal coordinates, release environment, pinned actions and Inno default. `desktop_target` chooses `inno`, `msix-direct` or `msix-store`; it cannot supply identity, URLs or version overrides. The build job qualifies the common complete runtime through the existing Inno first-run and frozen native checks, then `stage-msix-release.ps1` packs that stage for the selected MSIX profile. The Inno check is common-runtime evidence, not installed MSIX qualification.

The MSIX handoff contains one package, one linked Agent artifact, sealed profile/inventory/build records and exact source commit/tree. Direct delivery also freezes its approved nonsecret inputs and an App Installer template. Signing validates those bytes and the release environment before Azure login. Only the exact direct MSIX is signed and timestamped; Store candidates skip signing and retain their unsigned-submission scope.

`verify-desktop-release.ps1` dispatches to the existing Inno verifier or strict MSIX verifier. Direct output records the final signed hash before substituting its immutable package URI into the closed template. The descriptor permits neither unrestricted downgrade nor arbitrary update settings. Final verification, version/identity evidence and linked artifact hashes remain outside package contents. None of these scripts rebuilds or changes inner package files in the signing job. Hosted signing and the final Product signature have not been executed by the local source checks; artifact collection is not R2 publication or Store submission.

The separate protected direct publication job requires `publish_direct`, which defaults to false, and successful build/sign jobs. It independently verifies final MSIX bytes before receiving upload secrets. The [direct delivery client](../../tools/desktop-delivery/README.md#signing-and-publication-boundary) defines source-bound tooling, immutable upload, exact served-byte verification and conditional descriptor promotion. Its local tests do not establish live Product publication or authorize requesting that job. Installed compatibility evidence and customer release approval remain required.

Chrome Agent engineering attestation accepts its linked ZIP from either the original Inno archive or a closed MSIX release archive. The adapter resolves the production profile at the qualified Git commit, checks source/tree and all container hashes, and preserves the existing Agent audit and attestation schema. It does not attest Windows signature trust, installed MSIX behavior or Microsoft Store delivery. The original required signing-job name remains stable for historical and current qualification.

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

The implemented [startup storage boundary](../../docs/operations/desktop-startup-storage.md) holds a process-lifetime root lease, binds package descriptors to Windows identity, isolates qualification roots and sets a stable WebView2 profile path. Coordinated encrypted checkpoints, forward migration/resume and child-owned status framing have source and native OS tests. Independently frozen A/B artifacts now exercise genuine auth32→33, historical canonical11→12, interrupted checkpointing, graceful pre-listener cancellation, retained pending work and later publication-state preservation. The linked storage document explains the fixture and evidence limits.

Native source observation now distinguishes a raw signed installation with no registered feed from a direct App Installer installation. A controlled six-file API fixture installed through an HTTPS App Installer descriptor observed A `1.0.3.0` with no updates, B `1.0.4.0` available from the same source, and no updates after B was installed. Exact served hashes and URI binding were checked. Temporary registration, trust, signing key and both qualification objects were removed; HEAD/GET confirmed 404. This uses PowerShell/package-context activation and an isolated test certificate. It does not qualify ordinary Product acquisition, Brain state continuity, automatic hidden-app servicing or a trusted customer release. The [native command](../../desktop/README.md) remains read-only; safe staging and restart coordination are unfinished.

In-use API probes found that `AddPackageByAppInstallerFileAsync` without force options fails with `0x80073D02`, while an App Installer URI passed to `AddPackageByUriAsync` with explicit registration deferral returns `E_INVALIDARG` on the qualification host. These failures do not establish a pending update. The nonshipping experiment requires an isolated qualification descriptor and cannot run against production profiles. A controlled raw MSIX deferral staged `1.0.10.0` while `1.0.9.0` stayed alive; after the old process exited, activation by stable application ID registered and launched the new version, preserving its registered App Installer source. This qualifies an OS primitive, not automatic registered-source delivery or Brain continuity. All three probes removed their owned registrations, temporary trust and signing keys, and exact remote objects with HEAD/GET 404 confirmation.

The native supervisor now connects registered-source staging, durable handoff intent, owned Brain drain and stable-ID relaunch. A timeout preserves containment and cannot authorize relaunch. Repeated source checks use fresh owned COM activation factories across worker apartment teardown. [ADR 0056](../../docs/adr/0056-msix-packaging-and-managed-updates.md) defines the helper boundary; full installed Product continuity remains a release gate.

A later registered-source native fixture stages the package with `StagePackageByUriAsync` and defers registration through its Windows-owned manifest. The plural full-name registration probe failed with access denied; it is not the selected path. The selected path passed through Explorer activation with a verified non-elevated token: A `1.0.9.0` remained running while B `1.0.10.0` was deferred, then stable application-ID activation registered B after A's OS process exited. The registered feed still resolved with no newer update. Metadata size, exact identity/source, forward version and update settings remain admission requirements; Windows validates and downloads package bytes. Both API campaigns removed all owned package versions, temporary signing/trust material and the two exact qualification objects, with HEAD and GET returning 404. These historical fixtures contain no Brain or customer state, and the controller initiated the second activation. They do not qualify automatic Product servicing.

The integrated handoff fixture subsequently authenticated the production native helper in isolated A `1.0.23.0`, committed intent and exited with the servicing code. The helper waited outside package identity and activated B `1.0.24.0` through the stable AUMID, without a controller launching B. B observed the matching intent and a non-elevated token. Owned processes, both package versions, temporary trust and signing material were removed; both exact qualification objects returned HEAD/GET 404 after deletion. This qualifies the native handoff primitive only. It contains no Brain, WebView or authoritative Product state; later framing and scratch-file hardening require their own affected checks.

Further installed API probes reject two proposed shortcuts. With a deferred B `1.0.20.0`, Windows App SDK `AppInstance.Restart` restarted A `1.0.19.0`. The fixture declared a runtime dependency minimum of 2.3.1; the exact loaded runtime version was not captured. A separate native fixture registered for OS restart, waited beyond the documented 60-second minimum, and requested target-only forced registration; Windows returned `0x80073D02` under `defer`. An image still inside the package retained that identity even with the documented child breakaway policy. An exact copied image outside the package had no package identity and could remain suspended across controller-driven stable-ID activation of B `1.0.22.0`. That last probe establishes a process-boundary primitive only: the copy did not perform the relaunch, authenticate a handoff, observe Brain drain or qualify executable trust outside the package. All owned versions, signing/trust material and exact qualification objects were removed with HEAD/GET 404 confirmation. Shipping supervision still requires a bounded authenticated helper, explicit Quit cancellation, durable intent and ordinary installed continuity evidence.

Bridge now checks the installed WebView2 version before constructing Tauri or creating presentation/Brain state. The native recovery surface offers a fixed Microsoft acquisition action, a fresh check and Close. Its shared minimum and approved page are in [prerequisite-policy.json](../../shared/desktop-release/prerequisite-policy.json). Native OS tests observe real controls and the actual Bridge refusal path. The [startup document](../../docs/operations/desktop-startup-storage.md) distinguishes this evidence from installed acquisition qualification.

Source maintenance now supports governed corrective targets through protected catalog proofs and durable successor intent. Controlled package-debugging activation has corrective-recovery evidence; ordinary installed corrective recovery remains unqualified. A fresh isolated version9 package built from 988a4f29 reached onboarding and its owned listener through non-elevated Explorer activation and preserved its original DPAPI bytes on relaunch. Version10, built from 4d09d78f and manually installed over the same retained state, reached Browser access through Explorer activation. Tray Quit closed both Bridge and Brain with exit0 in 431 ms after the presentation-stream shutdown correction. This qualifies that ordinary shutdown, not automatic servicing or a schema transition. Remaining release gates include original hardware-key use, the full launcher journey, older/mixed-install transition detection, installed online/offline prerequisite recovery, registered-source validation and graceful servicing, protected MSIX workflow integration, and installed direct A→B continuity. Store capability approval, submission and actual delivery remain separate gates after reservation. Inno retirement and public cutover remain separately approved actions.

Version11, built from ddb151b8, adds a packaged render-blocking stylesheet to the fixed admission landing between native waiting and Browser access. Both landing routes remained styled with the one-use response held open in Chromium. Ordinary non-elevated Explorer launch and relaunch reached styled Browser access, served the installed stylesheet and preserved the original protected key. Both Quit checks ended with Bridge and Brain exit0 in under 400 ms. Discrete native captures are not continuous first-paint evidence. The retained results are 144 affected source/producer/policy checks and 7 frozen-runtime passes with 1 installed Inno launcher test skipped; these overlapping scopes are not summed. Temporary registration, test trust and signing key were removed after the owned tree exited, preserving the qualification data.

Regenerate the technical MSIX PNG sizes from the approved existing icon with `generate-assets.ps1`. Do not hand-edit those generated files or introduce separate branding for package qualification.
