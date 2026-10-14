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

Before a migrating constructor, inspect retained state without creating replacement authority and obtain lifetime-held ownership. Use the existing migrator for encrypted, checksummed forward transitions. Installation-level maintenance coordinates a complete encrypted checkpoint and recovery intent. Its bounded local checkpoints differ from user-selected backups and cannot undo authenticated deletion or security withdrawals.

Preserve verified listener admission, parent-owned lifetime, Close-to-hide and explicit Quit. Bounded child-status framing may report maintenance without authorizing admission. Windows in-use deferral retains the running application; qualify hidden-app servicing and its restart boundary before claiming automatic delivery.

The build owner validates, inventories and packs immutable contents. The protected signing owner signs and verifies without installing tooling or rebuilding. Package signatures, expected identity and the exact installed closure are separate required checks. Serve immutable signed bytes before promoting the direct descriptor. Store-submitted bytes and Store-delivered evidence remain separate.

## Confirmation

The direct gate installs through ordinary signed HTTPS acquisition, completes setup, receives a genuine software/schema update through its registered source and proves continuity of data, Windows Hello, original keys, pairing, licenses and publication checkpoints. Include interruption, unavailable prerequisites, incompatible state, duplicate writers, uninstall/reinstall and irreversible deletion.

Store readiness requires capability/certification review. Actual Store delivery requires its own authorized installation/update. Neither package construction nor direct evidence proves Store delivery. Production identity, hosting, Store submission and public cutover require the exact release inputs and approvals specified by the implementation plan.
