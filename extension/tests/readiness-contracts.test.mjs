import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { zipSync } from 'fflate';
import { legalBindingsModuleSource } from '../build.mjs';
import { auditLegalBindingLiterals, extractStringConstant } from '../qualification/legal-binding-literals.mjs';
import { readArchiveEntries } from '../qualification/archive-entries.mjs';
import { RELEASE_SCENARIOS, validateAcceptanceEvidence } from '../qualification/acceptance-evidence.mjs';

const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
const packageDocument = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));

test('release manifest and package agree on the frozen-tab compatibility floor and version', () => {
  assert.equal(manifest.minimum_chrome_version, '132');
  assert.equal(manifest.version, packageDocument.version);
  assert.deepEqual(manifest.optional_host_permissions, [
    'https://onlyfans.com/*',
  ]);
});

test('Legal literal audit rejects changed bytes, duplicate declarations and executable expressions', () => {
  const canonical = '{"disclosure":"reviewed"}';
  const digest = createHash('sha256').update(canonical).digest('hex');
  const expected = { canonical, digest };
  const source = legalBindingsModuleSource(expected);
  auditLegalBindingLiterals(source, expected);
  assert.throws(() => auditLegalBindingLiterals(source.replace(digest, 'a'.repeat(64)), expected));
  assert.throws(() => auditLegalBindingLiterals(source, { ...expected, canonical: '{}' }));
  assert.throws(() => auditLegalBindingLiterals(`${source}\nconst LEGAL_RELEASE_BINDINGS_B64 = "";`, expected));
  assert.throws(() => extractStringConstant('const LEGAL_RELEASE_BINDINGS_B64 = (() => "danger")();', 'LEGAL_RELEASE_BINDINGS_B64'));
});

test('release extraction rejects traversal, Windows drive and stream paths, and unexpected entries', () => {
  const bytes = new TextEncoder().encode('example');
  for (const name of ['../escape', '/absolute', 'C:/escape', 'safe:stream', 'a/../escape', 'a\\escape']) {
    assert.throws(() => readArchiveEntries(zipSync({ [name]: bytes })), name);
  }
  assert.throws(() => readArchiveEntries(zipSync({ 'unexpected.js': bytes }), ['manifest.json']));
  assert.throws(() => readArchiveEntries(zipSync({ 'manifest.json': bytes }), ['manifest.json', 'popup.js']));
  // fflate's plain-object return must not hide entries through __proto__ assignment.
  const hidden = Buffer.from(zipSync({ '__prot0__': bytes, 'manifest.json': bytes }, { level: 0 }));
  let offset = 0;
  while ((offset = hidden.indexOf('__prot0__', offset)) !== -1) {
    hidden.write('__proto__', offset);
    offset += 9;
  }
  assert.throws(() => readArchiveEntries(hidden), /decoder omitted an entry/);
});

test('popup smoke evidence cannot promote a release without exact-ZIP production acceptance', () => {
  const expected = { artifactDigest: 'a'.repeat(64), sourceRevision: 'b'.repeat(40), currentMajor: 145 };
  const valid = {
    schema: 'ofca-extension-release-acceptance/v2', artifact_sha256: expected.artifactDigest,
    source_revision: expected.sourceRevision, companion_transport: 'ws://127.0.0.1:17871/ws/agent',
    cryptographic_session: 'Noise_KK_25519_ChaChaPoly_SHA256', pairing: 'verified_grants_and_comparison',
    ambient_credentials: 'absent', authentication: 'verified', permission_prompt: 'native',
    bypasses_used: false, companion_version: 'test-version', tester: 'test reviewer', performed_at: '2026-09-11T00:00:00Z',
    browsers: [132, 145].map((major) => ({ major, installation: 'supported', scenarios:
      RELEASE_SCENARIOS.map((id) => ({ id, result: 'passed', evidence: `local-report:${id}` })) })),
  };
  validateAcceptanceEvidence(valid, expected);
  const liveScenario = 'scoped_live_history_deduplication_and_reconstruction';
  assert.ok(RELEASE_SCENARIOS.includes(liveScenario), 'release acceptance must require scoped live history');
  for (const browserIndex of [0, 1]) {
    const withoutLiveHistory = structuredClone(valid);
    withoutLiveHistory.browsers[browserIndex].scenarios = withoutLiveHistory.browsers[browserIndex].scenarios
      .filter(({ id }) => id !== liveScenario);
    assert.throws(() => validateAcceptanceEvidence(withoutLiveHistory, expected), new RegExp(liveScenario));
    const withoutLiveRecord = structuredClone(valid);
    withoutLiveRecord.browsers[browserIndex].scenarios.find(({ id }) => id === liveScenario).evidence = '';
    assert.throws(() => validateAcceptanceEvidence(withoutLiveRecord, expected), new RegExp(liveScenario));
  }
  assert.throws(() => validateAcceptanceEvidence({ ...valid, artifact_sha256: 'c'.repeat(64) }, expected));
  assert.throws(() => validateAcceptanceEvidence({ ...valid, bypasses_used: true }, expected));
  assert.throws(() => validateAcceptanceEvidence({ ...valid, pairing: 'first_listener' }, expected));
  assert.throws(() => validateAcceptanceEvidence({ ...valid, ambient_credentials: 'present' }, expected));
  assert.throws(() => validateAcceptanceEvidence({ ...valid, cryptographic_session: 'plaintext' }, expected));
  assert.throws(() => validateAcceptanceEvidence({ ...valid, browsers: [...valid.browsers, valid.browsers[0]] }, expected));
  assert.throws(() => validateAcceptanceEvidence(valid, { ...expected, currentMajor: 132 }));
  const missing = structuredClone(valid);
  missing.browsers[1].scenarios.pop();
  assert.throws(() => validateAcceptanceEvidence(missing, expected));
});
