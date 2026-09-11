# Signer 0.2.0 consumer integration

<!-- CODE-VERIFY: Check signer-release.mjs, build.mjs, agent-runtime-core.mjs, read-only-history-coordinator.mjs, signer-release tests, browser recovery harness, and tools/packaged-signing-rule/verify.mjs. Published identities were checked against authenticated release assets and the offline verifier. -->

The extension loads the published archive through the public `local-authenticated-read-connector/browser-signing` export. The production entry is `background-read-only.js`, with its read-only runtime, coordinator, normalization and durable outbox. The authoring counterparts retain parity.

## Artifact identities

| Input | Identity |
| --- | --- |
| Signer release | `v0.2.0`, release `386910504` in the configured private signer repository |
| Package | `local-authenticated-read-connector@0.2.0`, asset `556904013` |
| Archive SHA-256 | `d32da93c6c1e863b49ca96ed0bc86f2c9c615174b88ccee04310f817dd88a281` |
| Signer source | `672ba94db26aa582451c817ec72a04304a2a238c` |
| Signer source tree | `8f42fe8ac4044478a9c62b0de35a47cbbdd43e7b` |
| Offline verifier | `cd3ae68107f7aa20f73b0909dbac86f08429edfe` |
| Published evidence | `signer-production-evidence-0.2.0-v2.tgz`, asset `556904026` |
| Evidence SHA-256 | `c27a03c0f9b48ee5a4bd137a85ba1bea3df7e4e3528b232cdf05da97bc4059c1` |

The dependency, generated lockfile, vendored archive, build pins, generated notices and ZIP metadata agree. `signer-release.mjs` checks the archive SHA-256, installed files and resolved browser entry. Compilation consumes the compared release source bytes, and notices use the verified archive license. Both ZIP audit paths verify the signer identity and release coordinates.

Package verification covers all twelve published payload digests and the offline verifier’s `verify` and `release-check` commands against the evidence bundle. Reproduce offline verification in an isolated checkout with raw Git line endings and no installed dependencies. Consumer qualification covers the integration behavior described below; the published evidence bundle supplies the signer environment matrix.

## Compatibility and consumer ownership

The runtime requires the exact authorized positive-decimal platform identity, including when native bootstrap omits `user-id`. The application account UUID cannot substitute for it. Initialization is shared per account owner and respects cancellation. Replacing a signer owner drains any signing-state save already entered, preventing a new store from loading before its acknowledgement. Persisted signing documents remain signer-owned, including legacy proofs and ambiguous save outcomes; the consumer does not reset or patch them.

The existing 20-second acquisition deadline bounds cold bootstrap and proof migration too. A timed-out attempt cannot return a page or cross the consumer's authorization, account-epoch and commit fences. Retries continue to use the signer's bounded refresh policy. Ordinary HTTP errors, rate limits, malformed responses and transport failures do not cause consumer-directed reloads. Every MAIN-world dispatch retains the frozen-tab guard and Promise method receiver.

The coordinator validates exact canonical pages and terminal boundaries, stores opaque cursors unchanged, clears terminal cursors, and retains page traversal, deduplication and coverage ownership. Repeated inventory members preserve an existing conversation job's progress and lease while immutable identity conflicts still reject the transaction. Canonical messages require `sent_at`; passive observation normalization keeps its separate contract. Persisted diagnostics use fixed consumer codes or the signer's public failure and validation allowlists.

New history jobs bind their authorized platform creator identity. A different binding cannot resume the generation or report it current. Legacy jobs without this binding fail closed while retaining signing state, material, jobs and evidence. Resuming those partitions while preserving their data would require an explicit reviewed migration; this integration does not infer an identity or clear their data. A new consent revision for the same platform identity starts a newly verified generation.

History cancellation and absolute deadline checks reach native transaction controls, including after the storage callback returns. Both encrypted storage variants schedule requests inside native IndexedDB callbacks so WebCrypto completion remains compatible with Chrome 132.

## Qualification scope

Run `npm ci --ignore-scripts`, `npm test`, and `npm run check:architecture`. The installed-package tests exercise real browser-provider bootstrap, candidate and negative-control proof, account enforcement, legacy proof migration, ordinary failures, corruption, reconstruction, cancellation and entered-save completion through the consumer's frozen-tab proxy. Browser/network fixtures use synthetic identities and independent signing arithmetic. Provider and parser implementations are the actual installed release.

Coordinator integration tests drive genuine signer pages through the production read-only coordinator and encrypted storage, including overlapping inventory, opaque descending message continuations, terminal nonempty pages, reconstruction, deduplication and coverage. Consumer deadline and authorization tests verify that late completion cannot commit history.

The browser worker recovery harness is a separate QA bundle of these production modules. It uses real Chromium IndexedDB and actual worker termination with synthetic platform responses. Its report must not be presented as a live-account test, a service-worker crash test, or acceptance of a shipping extension ZIP. Exact ZIP smoke qualification uses the existing release runner and its distinct production acceptance gate.

Run `npm run test:browser:signer-recovery -- --browser-executable=<chromium.exe> --outdir=<evidence-directory>` for each supported browser. The report identifies the engine, signer archive and QA bundle. It checks committed-page reconstruction, rollback of an entered page transaction after worker termination, cancellation after the callback returns, opaque cursor resumption, deduplication and coverage using a synthetic three-conversation/five-message scenario.

## Required production inputs

Extension 2.0.1 supports fresh installations. Release acceptance uses fresh browser profiles. Migration of history jobs without an account binding is outside this release’s scope; those jobs fail closed without deleting their data.

The required signing rule has revision `202609101529-ed4efd662b`, SHA-256 `218faeca41e0cd1ee649ad59100a10df2ea6789393dd075c76c50e2fc66b9ea4`. The matching qualification fixture is at `acceptance/live-driver-files/reviewed-rule.json` inside the verified evidence bundle. Those bytes may be used for integration qualification.

Production packaging retrieves `packaged-signing-rule.json` using these coordinates and the repository identity in private release configuration:

```ini
SIGNING_RULE_RELEASE_TAG=signing-rule-202609101529-ed4efd662b
SIGNING_RULE_RELEASE_ASSET_ID=557256152
SIGNING_RULE_DIGEST=218faeca41e0cd1ee649ad59100a10df2ea6789393dd075c76c50e2fc66b9ea4
SIGNING_RULE_SOURCE_REVISION=202609101529-ed4efd662b
```

Supply all four coordinates together to production packaging. `tools/packaged-signing-rule/verify.mjs` authenticates retrieval and verifies the asset identity and bytes before each build.

## Legal bindings

Extension 2.0.1 requires these approved Legal bindings coordinates:

```ini
LEGAL_REPOSITORY_REVISION=ad4f38ddd82f55bc054a756212385dd22655270b
LEGAL_BINDINGS_REPOSITORY_REVISION=1af27b19efa1f4701bd6d619530f9d44dc431148
LEGAL_BINDINGS_PATH=compliance/cws/releases/2.0.1/legal-release-bindings.json
LEGAL_BINDINGS_DIGEST=a42899ee8b86b978ef52dfb473c8b93b297f1fa49d1d4bf00322a72d09f1771f
```

`LEGAL_REPOSITORY_REVISION` identifies the document approval; `LEGAL_BINDINGS_REPOSITORY_REVISION` identifies the commit containing the bindings artifact. The retrieval gate verifies the canonical document bytes and derives `https://assets.dipsy.fi/legal/provider-privacy` from the approved privacy instrument. Repository identities and credentials belong in private configuration. The qualification workflow removes staged private inputs and publishes only verification results and artifact identities.

## Verification evidence

[Run 34610865649](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/34610865649) verifies authenticated retrieval of both inputs, all 24 Legal gate regression tests, and the extension build and archive audit at Product commit `00a956e7fed1ae83a42dfb60d4720fa80481eb67`.

The CI qualification ZIP SHA-256 is `286edf7ba6740b3d8d8a1f0e03bf9aa78d654cc743afaba914d35a3ce08477e4`. A local build with the same verified input identities produced `a8d2db0ab65eabf7379842f5ec367eaa2a3528bb0f38594d48ac3b027237ff62`; its exact extracted ZIP passed the release smoke scenario on Chromium 132.0.6834.159 and 153.0.8010.12. These are distinct artifacts: the local browser results do not attest the CI ZIP, and cross-environment byte reproducibility has not been established. The production acceptance gate must qualify the exact final artifact.

## Production acceptance requirements

Verify public Legal route availability and approved document bytes before Store submission. Authenticated bindings retrieval and local packaging do not fetch those routes, so a successful build does not establish public availability.

Production acceptance requires a browser-trusted authenticated companion and recorded native-permission acceptance of the exact ZIP on Chrome 132 and the current browser. Scoped live traversal needs an authorized account, bounded scope, observed counts, deduplication and reconstruction evidence. Synthetic fixtures and upstream signer evidence do not establish consumer live-history completeness.
