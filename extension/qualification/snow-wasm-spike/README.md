# `snow` WASM MV3 feasibility spike

Research-only. This directory is intentionally outside the shipping extension graph. It does not change production transport, pairing, permissions, storage, Full-mode routing, or package dependencies.

The Rust `cdylib` wrapper pins upstream `snow` 0.10.0 and fixes `Noise_KK_25519_ChaChaPoly_SHA256`. Caller-provided static keys and the complete prologue are handed to `snow::Builder`; all Noise state transitions and cryptographic primitives remain inside upstream `snow`. The only extra crypto dependency declaration is `getrandom 0.3.4` with its documented `wasm_js` backend so `snow`'s existing RNG abstraction reaches browser `crypto.getRandomValues` on `wasm32-unknown-unknown`.

The JavaScript layer implements only OFCA record admission/lifecycle: 4 KiB frames, encrypted `client-ready`/`server-ready` confirmations, record discriminators, cancellation/deadline, and fail-closed session destruction. `noise-binding.mjs` independently implements the pairing contract's length-prefixed semantic binding using WebCrypto SHA-256 and is checked against the existing Python fixed vector.

## Pinned toolchain

- upstream `snow`: 0.10.0, release commit `4bb43f50370bdb3e8b1b57814ac662864db2704f`
- Rust: 1.98.1
- target: `wasm32-unknown-unknown`
- `wasm-bindgen`: 0.2.128 (library and CLI)
- `getrandom`: 0.3.4 with `wasm_js`
- Python peer: existing repository `noiseprotocol==0.3.1` spike

## Browser boundary

The isolated test extension declares `script-src 'self' 'wasm-unsafe-eval'`. Chrome MV3 requires that token for WebAssembly; the production manifest currently omits it and the production build audit pins the existing CSP exactly. A production adoption therefore requires an explicit security/release review and corresponding audit change. The generated `--target web` glue loads the `.wasm` with a URL relative to its own `chrome-extension://` module. The package audit rejects `eval`, `new Function`, Node `require`, `node:` imports, remote HTTP(S) code URLs, and nonliteral dynamic imports.

The Python WebSocket harness intentionally uses `/session-spike`, matching the current Python prototype. The approved transport endpoint remains ordinary loopback `ws://127.0.0.1:17871`; the path is a harness coordinate, not a protocol/security change.
