# Packaged Noise dependency qualification

<!-- CODE-VERIFY: Check extension/crypto/snow/build.mjs, extension/qualification/companion-snow-release.mjs, extension/vendor/companion-snow/release.json, extension/build.mjs, extension/manifest.json, native/companion-snow, and the Snow qualification workflows before updating build or evidence claims. -->

Agent ships Snow 0.10.0 as locally packaged WASM; Brain uses the pinned native Snow factory. Both implement the fixed `Noise_KK_25519_ChaChaPoly_SHA256` suite required by [ADR 0024](../../docs/adr/0024-authenticated-companion-sessions.md). The extension CSP permits `wasm-unsafe-eval` for the packaged module. Remote executable code is prohibited.

## Build and artifact checks

The [WASM build](../crypto/snow/README.md) pins Rust, wasm-bindgen and the Cargo dependency graph. Its release record binds build inputs, static glue, WASM and license notices. Qualification rebuilds the vendored bytes on Windows and Linux. Normal extension builds verify that record and audit the packaged binary, production grant trust set, manifest and exact ZIP allowlist.

The [native build](../../native/companion-snow/README.md) has its own locked graph and notices. Windows qualification requires identical module bytes from two independent builds, checks interoperability against an independent Noise implementation, and loads the native factory in the frozen runtime.

## Evidence

| Qualification | Source revision | Evidence archive |
| --- | --- | --- |
| [Packaged WASM, run 34692388496](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/34692388496) | `8cf36046a284aac3ee6d075b3ad97f42db3b1270` | Artifact `10298275138`, SHA-256 `a9d9a7177cb3b0bd226886063e103f8edf88940d60e02caf9186d5ca53c12740` |
| [Native and production sessions, run 34693118849](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/34693118849) | `c477fa807bea23678c5dd298b7b6b54bdf043f12` | Artifact `10298227411`, SHA-256 `3a3b815dd076eb337560d25ce38acd25d7457d5c002115ee04eda923e2cdb990` |

Both runs passed Chrome 132 and current Chromium. The production session run also covers the pairing window, encrypted operations, reconstruction, revocation, bounded records and a hostile loopback listener with seeded cookies. Its fixture grants and installation-key provider are test inputs.

These identities describe qualification archives, not submission ZIPs. [Exact-artifact release acceptance](README.md) still requires real provisioning and native permission interactions on the supported browser matrix. Changed source or dependency bytes require the corresponding checks again. The [companion pairing guide](companion-pairing.md) describes the shipping composition and qualification boundaries.
