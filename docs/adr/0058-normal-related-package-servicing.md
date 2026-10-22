<!-- CODE-VERIFY: app/core/desktop_release.py, desktop/src-tauri/src/deployment.rs, servicing_feed.rs, servicing.rs, tools/msix_packaging.py, optional_msix.py, packaging/msix/release-artifacts.psm1, packaging/verify-msix.ps1 and shared/desktop-release/related-servicing-cases.json. -->

# ADR 0058: Service an optional package through the normal desktop update

- Status: Accepted contract; installed delivery and rollout require separate qualification.
- Date: 2026-10-22
- Extends: ADRs 0056 and 0057.

## Decision

Version 1 descriptors, profiles and singleton release records retain their existing semantics. Version 2 is a separate closed direct-AppInstaller contract. Store profiles remain version 1. Unsupported versions and malformed nested variants fail closed without a version 1 fallback.

The descriptor records its installed artifact layout and allowed target capability separately. A MainPackage installation may declare MainPackage only, or MainPackage and related-bundle targets. A related-bundle installation admits only a related-bundle target and declares its exact related-set identifier and Engine identity/version. Effective admission is the intersection of declared capability and installed layout. A related installation cannot downgrade to a MainPackage target.

Engine identity includes name, Publisher, Windows package family and x64 architecture. It is distinct from Main, has the same Publisher, and uses the actual Main Windows publisherId suffix. Consumers obtain that suffix from trusted Windows identity evidence; they do not hash a Publisher string or trust an arbitrary fixture suffix. Existing four-field Python identity callers remain compatible for version 1. Current-process admission checks the compiled application version. Pure future-target parsing instead validates the target's own identity and version.

The normal direct updater preserves the installed Engine choice and queries whether Engine is installed. With Engine absent, stage/register does not resolve, read or acquire its artifact. With Engine present, it uses only the selected approved family and explicit related URI, rejects an Engine downgrade, and validates the staged Main/Engine relation before registration. Core must advance. Engine-only updates at the same Core version remain deferred. Windows owns package servicing and deferred registration; this contract creates no independent updater or executable fallback.

The version 2 producer carries one tagged artifact set through packing, external unsigned inventory, build receipt, signing verification, staged release inputs and final release records. A MainPackage set contains Core. A related set contains Core, the related-set identifier and the exact Engine release/artifact. Engine has an independent canonical four-component version. Each signed artifact has its own size and SHA256; final signature verification checks both enclosing bundles, identities and the external relation. Payload inventory and package-signature verification remain separate boundaries.

Canonical names are `ConversationAnalytics_<CoreVersion>_x64.msix` or `.msixbundle`, and `TextAnalysisEngine_<EngineVersion>_x64.msixbundle`. The bundle embeds the canonical Core application filename and declares the canonical external Engine bundle/application filenames. Each feed URI uses its own artifact hash directory under `packages/<sha256>/`; the Engine URI is not inferred as a sibling of Core. These filename constraints are release-format policy, stricter than the Microsoft schema. Qualification uses the existing approved `probe.appinstaller` parent and isolated binding.

Packing a related release requires an explicit prebuilt Engine bundle whose metadata matches the selected profile. It does not build or admit an analysis runtime. No production or Store profile is switched by this change, and the workflow gains no Engine acquisition source. A future profile transition must supply a qualified producer input and supported-client/channel migration policy. Both protected publication consumers explicitly refuse related stable promotion until that policy exists. Read-only serving of canonical bundle paths creates no publication authority.

## Qualification boundaries

Shared fixtures and invented byte tests establish parsing, layout admission, independent coordinates and closed producer bindings. They do not establish Windows trust, HTTPS source association, cold transfer, installed Engine-present/absent servicing, deferred registration, real application activation, or inference compatibility. Those require their own installed evidence before rollout. Local debug-context package tests are API evidence only and do not qualify normal application activation or the production updater.

## References

- [Microsoft OptionalBundle schema](https://learn.microsoft.com/en-us/uwp/schemas/bundlemanifestschema/element-optionalbundle).
- [Create an AppInstaller file](https://learn.microsoft.com/en-us/windows/msix/app-installer/how-to-create-appinstaller-file).
- [StagePackageOptions](https://learn.microsoft.com/en-us/uwp/api/windows.management.deployment.stagepackageoptions).
- [RegisterPackageOptions](https://learn.microsoft.com/en-us/uwp/api/windows.management.deployment.registerpackageoptions).
