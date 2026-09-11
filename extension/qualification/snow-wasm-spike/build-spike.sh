#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rustc --version
cargo --version
wasm-bindgen --version
cargo build --locked --release --target wasm32-unknown-unknown
rm -rf pkg mv3-dist
wasm-bindgen --target web --no-typescript --out-dir pkg target/wasm32-unknown-unknown/release/ofca_snow_wasm_spike.wasm
node verify-glue.mjs | tee package-audit.json
node supply-chain.mjs >/dev/null
node --test tests/*.test.mjs
mkdir -p mv3-dist/pkg
cp mv3/manifest.json mv3/background.mjs web/*.mjs mv3-dist/
cp pkg/* mv3-dist/pkg/
