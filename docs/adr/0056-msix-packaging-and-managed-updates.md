<!-- CODE-VERIFY: packaging/build-msix.ps1 packaging/msix/ tools/msix_packaging.py tools/packaging_policy.py app/packaged_entry.py desktop/src-tauri/src/host.rs; record installed qualification separately from this implementation decision. -->

# ADR 0056: Package the complete desktop runtime for Windows managed updates

- Status: Accepted implementation decision; installed qualification and distribution cutover remain release gates.
- Decision authority: Product instruction to implement the reviewed MSIX packaging plan.
- Supersedes: ADR 0050's Inno-only packaging choice and ADR 0009's executable-update mechanism and managed upgrade-checkpoint policy. Their runtime, authority, user-selected backup and retained-data guarantees remain authoritative.
- Complements: ADRs 0019, 0046, 0054 and 0055.

## Decision

Produce Inno and full-trust MSIX through explicit adapters after one complete runtime qualification. Keep the pinned Tauri Bridge and one-directory frozen Brain layout. Agent remains a qualified linked artifact. Inno remains the default until its replacement journeys pass.

Windows owns MSIX executable replacement. Direct delivery uses one approved HTTPS App Installer source; Store delivery has its own profile and identity. Neither route launches Inno or downloads a separate Brain executable. Signing-parameter publication retains its independent verifier, account authority and authenticated delivery.

Require explicit closed package profiles. Production identity comes from its actual certificate or reserved Store product. Qualification profiles use isolated identities and cannot enter production publication. The deployment counter maps to `1.<counter div 65536>.<counter mod 65536>.0`. Application version and licensed major remain independent. Profile inputs record the tested OS and WebView2 minimum. The customer MSIX baseline is Windows 11 25H2 or later, x64, build 26200; qualification may exercise the feature floor at build 26100.

Use a narrow virtualization exclusion for the retained Product root and the justified `unvirtualizedResources` capability. Preserve configuration, encrypted stores, original key usability, deletion barriers, pending work and publisher checkpoints across updates and uninstall. Refuse conflicting roots or competing writers. Supported cross-installer adoption requires one deliberate same-user handoff.

Fence native presentation writes with a root mutex before creating a WebView profile. Bind single-instance activation to the resolved root, Windows user and delivery identity, independently of package version. Verify the primary process before forwarding. The Brain lifetime lease remains the persistence writer boundary. Read known legacy registration without retiring it; incompatible registered ownership requires the deliberate transition rather than silent adoption.

Private Windows objects belong to the effective Windows user SID, including when created from an elevated token whose default owner is Administrators. A group-owner-only ACL cannot satisfy read-only retained-state admission. Existing authority is never silently repaired during that admission. Native early-failure windows expose only a static error-code property for local qualification and give data-access guidance separately from installation-repair guidance.

Before a migrating constructor, inspect retained state without creating replacement authority and obtain lifetime-held ownership. Use the existing migrator for encrypted, checksummed forward transitions. Installation-level maintenance coordinates a complete encrypted checkpoint and recovery intent. Its bounded local checkpoints differ from user-selected backups and cannot undo authenticated deletion or security withdrawals.

An interrupted transition may admit a strictly newer corrective release only with matching delivery identity and protected proof that its complete recorded catalog remains an unchanged prefix. Preserve the original checkpoint and atomically record successor intent before forward SQL. Evidence lacking this proof permits only exact-target recovery. [Startup storage](../operations/desktop-startup-storage.md) defines the version and evidence boundaries; installed qualification remains required.

Preserve verified listener admission, parent-owned lifetime, Close-to-hide and explicit Quit. Bounded child-status framing may report maintenance without authorizing admission. Windows in-use deferral retains the running application; qualify hidden-app servicing and its restart boundary before claiming automatic delivery.

The direct native supervisor checks its exact registered source after admission and then at six-hour intervals. Windows stages and defers the signed same-family package. Before leaving, Bridge requires Brain's successful drain observation, exit zero and an empty owned Job Object. A failed drain retains containment until explicit Quit; it cannot force an update or readmit normal work.

Preparation reads the closed descriptor from that registered source and requires a canonical forward target. Informational Windows availability is not its authority gate. Windows signed staging, exact identity/origin verification, effective update-policy checks and deferred registration remain required before drain. Passive native window properties expose bounded compiler-owned servicing codes for local failure tracing; they contain no paths, URLs or application authority.

Cross the final package-lifetime boundary with a bounded native helper copied from the current installed launcher into a private, effective-user-owned temporary directory. This narrowly scoped helper has no WebView, Brain, account-state writer, network downloader or executable replacement role. Authenticate it through inherited private handles, the live parent process's user, package identity, exact installed image and matching copied-image hash. Commit private durable intent before authorizing relaunch. After the parent exits with the servicing code and same-user package processes exit, activate the stable package AUMID through Windows. Require the target or a newer same-identity version before admitting Brain. Interrupted preparation can be cancelled under the runtime-root lease; committed old code remains refused. Explicit Quit signals cancellation and never requests relaunch. Installed qualification must exercise this implementation, not infer it from a WinRT restart call or package declaration.

At normal server shutdown, close desktop presentation admission and wake its event streams before waiting for HTTP connections. Accepted owner work remains tracked and drains through the existing application lifecycle. Do not cancel that work or shorten native containment deadlines to hide a presentation-stream deadlock.

Check shared Evergreen WebView2 before constructing the native WebView runtime or creating writable presentation state. Missing or incompatible runtimes use Windows-native recovery with one approved Microsoft acquisition action. Acquisition does not prove installation, and renderer inputs cannot choose its URL or executable. App Installer's external dependency and the native check have distinct responsibilities; qualify actual online/offline routes separately.

The build owner validates, inventories and packs immutable contents. The protected signing owner signs and verifies without installing tooling or rebuilding. Package signatures, expected identity and the exact installed closure are separate required checks. Serve immutable signed bytes before promoting the direct descriptor. Store-submitted bytes and Store-delivered evidence remain separate.

## Confirmation

Controlled qualification has demonstrated ordinary non-elevated A activation, Close-to-hide, registered-source staging, authenticated native relaunch and a real two-store schema transition before B admission. The controller did not activate B. These locally trusted synthetic packages establish that specific installed servicing path; interrupted installed recovery, original hardware-key/pairing/license continuity and the customer distribution route still require their own evidence. The [package README](../../packaging/msix/README.md) records exact runtime coordinates and limits.

The direct gate installs through ordinary signed HTTPS acquisition, completes setup, receives a genuine software/schema update through its registered source and proves continuity of data, Windows Hello, original keys, pairing, licenses and publication checkpoints. Include interruption, unavailable prerequisites, incompatible state, duplicate writers, uninstall/reinstall and irreversible deletion.

Store readiness requires capability/certification review. Actual Store delivery requires its own authorized installation/update. Neither package construction nor direct evidence proves Store delivery. Production identity, hosting, Store submission and public cutover require the exact release inputs and approvals specified by the implementation plan.
