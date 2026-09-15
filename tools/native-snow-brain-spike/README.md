<!-- CODE-VERIFY: ../../native/companion-snow/Cargo.toml ../../native/companion-snow/pyproject.toml test_native_snow.py reference_oracle.py frozen_server.py ../../.github/workflows/native-snow-brain-feasibility.yml -->

# Production native Snow qualification

This harness tests the installed `ofca-native-snow` package from `native/companion-snow/`. It does not ship in Brain.

`test_native_snow.py` checks the fixed suite, input bounds, ordering, closure, and payload-free errors. `reference_oracle.py` independently checks its transcript and transport against `noiseprotocol==0.3.1`, a test-only dependency.

The Windows workflow uses Python 3.11, PyInstaller 6.11.1, Rust 1.98.1, PyO3 0.29.2, maturin 1.15.0, and the committed native Cargo lock. It compares two clean native builds, freezes the qualification server, and exchanges records with the production WASM binary in an actual MV3 worker.

Generated evidence records toolchain and artifact identities, the native dependency graph, matching handshake digests, and negative-case verdicts. The production Brain factory has a separate frozen-executable probe in `tests/test_packaged_runtime.py`.
