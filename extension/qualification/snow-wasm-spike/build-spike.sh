#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rustc --version
cargo --version
wasm-bindgen --version
node ../../crypto/snow/build.mjs --check
node verify-glue.mjs | tee package-audit.json
node supply-chain.mjs >/dev/null
node --test tests/*.test.mjs
mkdir -p mv3-dist/pkg
cp mv3/manifest.json mv3/background.mjs web/*.mjs mv3-dist/
cp ../../vendor/companion-snow/* mv3-dist/pkg/
