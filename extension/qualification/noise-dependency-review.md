# MV3 Noise dependency qualification

<!-- CODE-VERIFY: Check the Snow spike, locked dependency graph, and feasibility workflow before updating qualification claims. -->

Snow 0.10.0 compiled to locally packaged WASM supports `Noise_KK_25519_ChaChaPoly_SHA256` in an MV3 service worker. Browser feasibility is established; production adoption remains subject to CSP, supply-chain, and release review.

[Qualification run 34634685038](https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/34634685038) passed eight harness/vector/negative tests and MV3 qualification on Chrome 132.0.6834.159 and 153.0.8010.12. It demonstrates Python interoperability, fresh handshake state, and rejection of a hostile loopback port owner without observed application plaintext or remote HTTP requests.

The `snow-wasm-feasibility` evidence archive has SHA-256 `692fb8e1c8aff192ddc3544aec943a03d566b52e8648cd25758c815accaa0e35`. It identifies source commit `1d70b269d55cd2a2265806730986f03c927fd1b7`; subsequent source changes require new qualification. Its Cargo lockfile is retained to fix the demonstrated dependency graph.

Production adoption requires explicit review of `wasm-unsafe-eval`, locally packaged WASM/glue provenance and licenses, reproducible build treatment, and exact ZIP allowlist/audit integration. Remote code remains prohibited. Feasibility evidence does not qualify the shipping extension or establish production pairing provenance.

See [ADR 0024](../../docs/adr/0024-authenticated-companion-sessions.md) for authenticated pairing, endpoint persistence, transport migration, and final acceptance requirements.
