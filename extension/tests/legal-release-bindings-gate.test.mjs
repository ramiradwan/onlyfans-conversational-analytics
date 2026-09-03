import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import { validateLegalReleaseBindingsDocument } from '../build.mjs';

const run = promisify(execFile);
const EXTENSION_ROOT = fileURLToPath(new URL('..', import.meta.url));
const BUILD_SCRIPT = path.join(EXTENSION_ROOT, 'build.mjs');
const SYNTHETIC_BINDINGS = fileURLToPath(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
);
const PRIVACY_POLICY_URL = 'https://legal-evidence.example.com/legal/privacy';
const SYNTHETIC_SIGNING_RULE = {
  schema: 'local-packaged-signing-rule/v1',
  source_revision: 'test-fixture-revision',
  static_param: 'test-fixture-static-param',
  format: {
    prefix: 'test-prefix',
    suffix: 'test-suffix',
    checksum_indexes: [0, 7, 13],
    checksum_constant: 17,
  },
};

async function readSyntheticBindings() {
  return JSON.parse(await readFile(SYNTHETIC_BINDINGS, 'utf8'));
}

/** Run build.mjs and report its exit status and combined output. */
async function runBuild(argumentList) {
  try {
    const { stdout, stderr } = await run(process.execPath, [BUILD_SCRIPT, ...argumentList], {
      cwd: EXTENSION_ROOT,
    });
    return { code: 0, output: stdout + stderr };
  } catch (error) {
    return { code: error.code ?? 1, output: (error.stdout ?? '') + (error.stderr ?? '') };
  }
}

async function withScratchDirectory(body) {
  const directory = await mkdtemp(path.join(tmpdir(), 'ofca-legal-bindings-'));
  try {
    const signingRule = path.join(directory, 'packaged-signing-rule.json');
    await writeFile(signingRule, JSON.stringify(SYNTHETIC_SIGNING_RULE), 'utf8');
    return await body({ directory, signingRule });
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
}

test('release bindings reuse the runtime instrument validator', async () => {
  const bindings = await readSyntheticBindings();
  const validated = validateLegalReleaseBindingsDocument(bindings);
  assert.deepEqual(
    Object.keys(validated.instruments).sort(),
    ['extension_privacy_notice', 'privacy_policy', 'risk_disclosure', 'terms_of_service'],
  );

  const missingInstrument = await readSyntheticBindings();
  delete missingInstrument.instruments.risk_disclosure;
  assert.throws(() => validateLegalReleaseBindingsDocument(missingInstrument));

  const missingRenderedHash = await readSyntheticBindings();
  delete missingRenderedHash.instruments.terms_of_service.rendered_sha256;
  assert.throws(() => validateLegalReleaseBindingsDocument(missingRenderedHash));

  const unresolvableOrigin = await readSyntheticBindings();
  unresolvableOrigin.public_origin = 'https://legal.invalid';
  assert.throws(() => validateLegalReleaseBindingsDocument(unresolvableOrigin));
});

test('a production package is refused without Legal release bindings', async () => {
  await withScratchDirectory(async ({ signingRule }) => {
    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
    ]);
    assert.notEqual(result.code, 0, result.output);
    assert.match(result.output, /ADR 0022/);
    assert.match(result.output, /production package must not be created without valid Legal/);
    assert.match(result.output, /--legal-release-bindings=<path>/);
    for (const instrument of [
      'terms_of_service',
      'privacy_policy',
      'extension_privacy_notice',
      'risk_disclosure',
    ]) {
      assert.match(result.output, new RegExp(instrument));
    }
  });
});

test('a production package is refused when Legal release bindings are invalid', async () => {
  await withScratchDirectory(async ({ directory, signingRule }) => {
    const bindings = await readSyntheticBindings();
    delete bindings.instruments.risk_disclosure;
    const bindingsPath = path.join(directory, 'incomplete-bindings.json');
    await writeFile(bindingsPath, JSON.stringify(bindings), 'utf8');

    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${bindingsPath}`,
    ]);
    assert.notEqual(result.code, 0, result.output);
    assert.match(result.output, /ADR 0022/);
    assert.match(result.output, /are invalid/);
    assert.match(result.output, /legal instruments contains unexpected or missing fields/);
  });
});

test('a production package is created with valid Legal release bindings', async () => {
  await withScratchDirectory(async ({ signingRule }) => {
    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${SYNTHETIC_BINDINGS}`,
    ]);
    assert.equal(result.code, 0, result.output);
    assert.match(result.output, /Chrome package created/);
  });
});

test('a development build stays permitted without Legal release bindings', async () => {
  const result = await runBuild([]);
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /Deterministic extension build and audit passed/);
});
