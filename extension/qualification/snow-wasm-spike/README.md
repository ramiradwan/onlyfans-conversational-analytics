<!-- CODE-VERIFY: build-spike.sh tests/helpers.mjs ../../crypto/snow/build.mjs ../../vendor/companion-snow/release.json ../../../.github/workflows/snow-wasm-feasibility.yml -->

# Packaged Snow WASM qualification

This harness tests the production WASM and glue under `extension/vendor/companion-snow/`. The canonical Rust source and build are under `extension/crypto/snow/`.

`build-spike.sh` requires an identical fresh rebuild, checks the generated glue, runs contract and independent Python interoperability tests, and stages an isolated MV3 qualification extension. The browser checks cover wrong pins, modified bindings and records, replay, ordering, deadlines, cancellation, and reconnect state.

The workflow requires both current Chromium and Chrome 132 qualification. Its isolated `/session-spike` route carries fixture data only. Production transport and authorization behavior are tested through the production companion modules and endpoints separately.
