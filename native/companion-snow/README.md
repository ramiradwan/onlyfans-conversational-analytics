<!-- CODE-VERIFY: Cargo.toml pyproject.toml rust-toolchain.toml ../../app/security/companion_noise.py ../../packaging/pyinstaller/brain.spec ../../.github/workflows/native-snow-brain-feasibility.yml -->

# Native companion Noise responder

This package supplies Brain's `ofca_native_snow` extension module. `app.security.companion_noise.create_noise_responder` opens the purpose-protected key from a confirmed companion pin and constructs its responder.

The wrapper fixes `Noise_KK_25519_ChaChaPoly_SHA256` and delegates cryptography to Snow 0.10.0. Constructor inputs are the Brain private key, pinned Agent public key, and contract prologue. Its Python exceptions carry no payload. Invalid authentication or ordering destroys the native session.

Rust 1.98.1, PyO3 0.29.2, maturin 1.15.0, and the committed Cargo lock define the build. Production uses Python 3.11; development builds also support Python 3.12 and 3.13. Install the repository requirements from its root to build the module. Build tools do not ship in Brain.

Run `python tools/native-snow-brain-spike/test_native_snow.py -v` and `python tools/native-snow-brain-spike/reference_oracle.py` with the qualification dependencies installed. The Windows qualification workflow builds the native module twice, compares its bytes, and checks a frozen peer against the packaged MV3 WASM implementation.

`Brain.exe --companion-runtime-report` probes the actual production factory with ephemeral keys. Set `BRAIN_COMPANION_RUNTIME_REPORT_PATH` to receive its payload-free artifact identity and verdicts from the GUI executable.
