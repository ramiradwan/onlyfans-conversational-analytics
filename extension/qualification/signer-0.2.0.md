# Signer 0.2.0 consumer integration

<!-- CODE-VERIFY: Check signer-release.mjs, build.mjs, agent-runtime-core.mjs, read-only-history-coordinator.mjs, signer-release tests, browser recovery harness, and tools/packaged-signing-rule/verify.mjs. Published identities were checked against authenticated release assets and the offline verifier. -->

The extension adopts the unchanged published archive through the public `local-authenticated-read-connector/browser-signing` export. The production entry remains `background-read-only.js`, with its read-only runtime, coordinator, normalization and durable outbox. The authoring counterparts retain parity.

## Artifact identities

| Input | Identity |
| --- | --- |
| Private release | `ramiradwan/local-of-signer`, `v0.2.0`, release `386910504` |
| Package | `local-authenticated-read-connector@0.2.0`, asset `556904013` |
| Archive SHA-256 | `d32da93c6c1e863b49ca96ed0bc86f2c9c615174b88ccee04310f817dd88a281` |
| Signer source | `672ba94db26aa582451c817ec72a04304a2a238c` |
| Signer source tree | `8f42fe8ac4044478a9c62b0de35a47cbbdd43e7b` |
| Offline verifier | `cd3ae68107f7aa20f73b0909dbac86f08429edfe` |
| Published evidence | `signer-production-evidence-0.2.0-v2.tgz`, asset `556904026` |
| Evidence SHA-256 | `c27a03c0f9b48ee5a4bd137a85ba1bea3df7e4e3528b232cdf05da97bc4059c1` |

The dependency, generated lockfile, vendored archive, build pins, generated notices and ZIP metadata agree. `signer-release.mjs` checks the archive SHA-256, installed files and resolved browser entry. Compilation consumes the compared release source bytes, and notices use the verified archive license. Both ZIP audit paths verify the signer identity and release coordinates.

All twelve published payloads were checked against authenticated GitHub asset digests. The published verifier was reconstructed in an isolated checkout with raw Git line endings and no installed dependencies. Its `verify` and `release-check` commands passed against the evidence bundle. Historical offline publication flags describe the original decision; the later publication record and authenticated GitHub release establish publication. The unchanged signer environment matrix was not rerun for this consumer integration.

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

The initial release has no existing users and targets fresh installations. Legacy history migration is therefore outside the release scope and is not a production blocker. Use fresh browser profiles for release acceptance. Handling any retained development data is a separate choice; the existing account-binding checks remain enabled.

The final signer qualification used rule revision `202609101529-ed4efd662b`, SHA-256 `218faeca41e0cd1ee649ad59100a10df2ea6789393dd075c76c50e2fc66b9ea4`. Its exact retained bytes are at `acceptance/live-driver-files/reviewed-rule.json` inside the verified evidence bundle. Those bytes may be used for integration qualification.

The separate `signing-rule-prod-v1` release, asset `545500717`, contains different bytes: SHA-256 `3f8278caae456f41d5474f91a7b1f7102caf71890a6f0f1b0e70a1f029183e33`. At integration time no standalone release asset matched the final qualified rule. This existing asset must not be relabeled or treated as qualification of the final rule.

Before production packaging, the signer maintainer must supply approved release coordinates for the reviewed rule through the existing release process. Configure `SIGNING_RULE_RELEASE_TAG`, `SIGNING_RULE_RELEASE_ASSET_ID`, `SIGNING_RULE_DIGEST` and `SIGNING_RULE_SOURCE_REVISION` together. Preserve `tools/packaged-signing-rule/verify.mjs` and its authenticated retrieval gate; extracting an evidence-bundle rule is not a substitute for that gate.

Production packaging also requires verified Legal release bindings, the actual HTTPS privacy policy, a browser-trusted authenticated companion, and recorded native-permission acceptance of the exact ZIP on Chrome 132 and the current browser. Synthetic Legal inputs qualify implementation only. Scoped live traversal needs an authorized account, bounded scope, observed counts, deduplication and reconstruction evidence; the upstream 11-conversation/144-message snapshot does not establish consumer live-history completeness. Deployment remains a separate action.
