import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readArchiveEntries } from './archive-entries.mjs';
import { auditLegalBindingLiterals } from './legal-binding-literals.mjs';
import { auditSignerMetadata } from './signer-release.mjs';
import { auditPackagedSnow, SNOW_WASM_FILE } from './companion-snow-release.mjs';

import { canonicalLegalBindingsJson } from '../../tools/legal-release-bindings/canonical-json.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_ROOT = path.dirname(ROOT);
const BRIDGE_ORIGIN = 'http://bridge.localhost:17871';
const COMPANION_WS = 'ws://127.0.0.1:17871/ws/agent';
const EXPECTED_CSP = "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'; connect-src 'self' ws://127.0.0.1:17871;";

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function argumentValue(name, argv = process.argv.slice(2)) {
  const inline = argv.find((argument) => argument.startsWith(`${name}=`));
  if (inline !== undefined) return inline.slice(name.length + 1);
  const index = argv.indexOf(name);
  return index === -1 ? null : argv[index + 1] ?? null;
}

function normalizedEntries(bytes) {
  const entries = readArchiveEntries(bytes);
  const result = new Map();
  for (const [rawName, value] of Object.entries(entries)) {
    const name = rawName.replaceAll('\\', '/');
    assert.equal(name.startsWith('/'), false, 'release archive contains an absolute path');
    assert.equal(name.split('/').includes('..'), false, 'release archive contains a parent path');
    assert.equal(name.endsWith('/'), false, 'release archive contains a directory entry');
    assert.equal(result.has(name), false, `release archive duplicates ${name}`);
    result.set(name, value);
  }
  return result;
}

function text(entries, name) {
  const bytes = entries.get(name);
  if (bytes === undefined) throw new Error(`release archive is missing ${name}`);
  return new TextDecoder().decode(bytes);
}

function json(entries, name) {
  return JSON.parse(text(entries, name));
}

export async function auditReleaseArchive({ artifact, legalBindings }) {
  const archiveBytes = await readFile(artifact);
  const entries = normalizedEntries(archiveBytes);
  const manifest = json(entries, 'manifest.json');
  assert.equal(manifest.minimum_chrome_version, '132');
  assert.deepEqual(manifest.optional_host_permissions, [
    'https://onlyfans.com/*',
  ]);
  assert.deepEqual(manifest.externally_connectable?.matches, [`${BRIDGE_ORIGIN}/*`]);
  assert.equal(manifest.content_security_policy?.extension_pages, EXPECTED_CSP);
  assert.equal(manifest.host_permissions, undefined);

  const config = json(entries, 'extension-config.json');
  assert.equal(config.dashboard_url, `${BRIDGE_ORIGIN}/`);
  assert.equal(config.history_settings_url, `${BRIDGE_ORIGIN}/settings`);

  const metadata = json(entries, 'build-meta.json');
  auditSignerMetadata(metadata);
  const snow = await auditPackagedSnow();
  assert.deepEqual(metadata.companion_snow, snow.release);
  assert.deepEqual(Buffer.from(entries.get(SNOW_WASM_FILE) ?? []), snow.files.get(SNOW_WASM_FILE));
  assert.equal(metadata.outputs[SNOW_WASM_FILE], `sha256:${sha256(entries.get(SNOW_WASM_FILE))}`);
  const trust = await readFile(new URL('../../contracts/production/grant-profile-v1/trust-set.json', import.meta.url));
  assert.deepEqual(Buffer.from(entries.get('companion-grant-trust.json') ?? []), trust);
  assert.equal(metadata.outputs['companion-grant-trust.json'], `sha256:${sha256(trust)}`);
  assert.equal(metadata.target, `chrome${manifest.minimum_chrome_version}`);
  assert.equal(metadata.determinism_verified, true);

  const legalBytes = await readFile(legalBindings);
  const legalDocument = JSON.parse(legalBytes.toString('utf8'));
  const canonical = canonicalLegalBindingsJson(legalDocument);
  assert.equal(legalBytes.toString('utf8'), canonical, 'release Legal bindings are not canonical JSON');
  const legalDigest = sha256(Buffer.from(canonical));
  assert.equal(metadata.legal_bindings?.legal_bindings_digest, legalDigest);

  const background = text(entries, 'background.js');
  auditLegalBindingLiterals(background, { canonical, digest: legalDigest });
  assert.equal(background.includes(COMPANION_WS), true, 'built background omits companion Noise transport');
  assert.match(background, /session\.authorization/);
  assert.doesNotMatch(background, /ofca_full_storage_bootstrap_v1|ofca\.brain\.bind/);

  return Object.freeze({
    artifact: path.resolve(artifact),
    sha256: sha256(archiveBytes),
    extension_version: manifest.version,
    minimum_chrome_version: manifest.minimum_chrome_version,
    target: metadata.target,
    archive_files: Object.freeze([...entries.keys()].sort()),
    legal_bindings_digest: legalDigest,
    companion_wasm_sha256: sha256(entries.get(SNOW_WASM_FILE)),
    companion_trust_sha256: sha256(trust),
  });
}

async function main() {
  const artifact = argumentValue('--artifact');
  const legalBindings = argumentValue('--legal-release-bindings');
  if (!artifact || !legalBindings) {
    throw new Error('artifact audit requires --artifact and --legal-release-bindings');
  }
  const result = await auditReleaseArchive({
    artifact: path.resolve(artifact),
    legalBindings: path.resolve(legalBindings),
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) await main();
