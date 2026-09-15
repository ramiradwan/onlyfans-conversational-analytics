import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';

const source = new URL('../crypto/snow/', import.meta.url);
const vendor = new URL('../vendor/companion-snow/', import.meta.url);
export const SNOW_WASM_FILE = 'ofca_snow_wasm_bg.wasm';
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');

export async function auditPackagedSnow() {
  const release = JSON.parse(await readFile(new URL('release.json', vendor), 'utf8'));
  assert.equal(release.schema, 'ofca-companion-snow-wasm/v1');
  assert.equal(release.suite, 'Noise_KK_25519_ChaChaPoly_SHA256');
  assert.equal(release.snow, '0.10.0');
  assert.equal(release.rust, '1.98.1');
  assert.equal(release.wasm_bindgen, '0.2.128');
  assert.equal(release.target, 'wasm32-unknown-unknown');
  assert.equal(release.source_paths, 'complete-filenames/v1');
  assert.equal(release.debug_sections, false);
  assert.deepEqual(Object.keys(release.inputs).sort(), ['Cargo.lock', 'Cargo.toml', 'build-inputs.mjs', 'build.mjs', 'rust-toolchain.toml', 'src/lib.rs']);
  assert.deepEqual(Object.keys(release.outputs).sort(), ['THIRD_PARTY_NOTICES.txt', 'ofca_snow_wasm.js', SNOW_WASM_FILE, 'package.json'].sort());
  assert.deepEqual((await readdir(vendor)).sort(), [...Object.keys(release.outputs), 'release.json'].sort());
  for (const [file, digest] of Object.entries(release.inputs)) {
    assert.equal(sha(await readFile(new URL(file, source))), digest, `Snow source differs: ${file}`);
  }
  const files = new Map();
  for (const [file, digest] of Object.entries(release.outputs)) {
    const bytes = await readFile(new URL(file, vendor));
    assert.equal(sha(bytes), digest, `Snow output differs: ${file}`);
    files.set(file, bytes);
  }
  const glue = files.get('ofca_snow_wasm.js').toString('utf8');
  for (const pattern of [/\beval\s*\(/u, /new\s+Function\b/u, /https?:\/\//u, /\brequire\s*\(/u, /node:/u]) {
    assert.doesNotMatch(glue, pattern);
  }
  assert.match(glue, /new URL\(['"]ofca_snow_wasm_bg\.wasm['"], import\.meta\.url\)/u);
  assert.equal(WebAssembly.validate(files.get(SNOW_WASM_FILE)), true);
  const module = new WebAssembly.Module(files.get(SNOW_WASM_FILE));
  assert.equal(WebAssembly.Module.customSections(module, 'name').length, 0);
  assert.equal(WebAssembly.Module.customSections(module, 'producers').length, 0);
  assert.match(files.get('THIRD_PARTY_NOTICES.txt').toString('utf8'), /snow 0\.10\.0/);
  return { release, files };
}
