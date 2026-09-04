import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import {
  canonicalLegalBindingsJson,
  validateLegalReleaseBindingsDocument,
} from '../build.mjs';

const run = promisify(execFile);
const EXTENSION_ROOT = fileURLToPath(new URL('..', import.meta.url));
const BUILD_SCRIPT = path.join(EXTENSION_ROOT, 'build.mjs');
const SYNTHETIC_BINDINGS = fileURLToPath(
  new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url),
);
// The cross-repository pin: Legal publishes this document and the byte length
// and digest of its canonical form. It is stored verbatim and never edited.
const CONTRACT_VECTOR = fileURLToPath(
  new URL('./fixtures/legal-instrument-bindings.contract-vector.json', import.meta.url),
);
const CONTRACT_VECTOR_BYTE_LENGTH = 979;
const CONTRACT_VECTOR_DIGEST =
  'ba66e07ff67cd341f979791c71b1208ff7d274aa9217692a2e3e8d167eb1f2e9';
const PRIVACY_POLICY_URL = 'https://legal-evidence.example.com/legal/privacy';
// Removing this one line lets a package reach the archive writer with no
// bindings, which is how the audit's own refusal is exercised.
const PACKAGE_GATE =
  '  const legalBindings = await verifyLegalReleaseBindings({ required: packageRequested });';
const PACKAGE_GATE_REMOVED =
  '  const legalBindings = await verifyLegalReleaseBindings({ required: false });';
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

/** The digest a second party computes from the bindings file alone. */
function fileDigest(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/** The pretty-printed authored-order form the contract does not accept. */
function authoredOrderJson(document) {
  return `${JSON.stringify(document, null, 2)}\n`;
}

async function readBuildMetadata() {
  return JSON.parse(await readFile(path.join(EXTENSION_ROOT, 'dist', 'build-meta.json'), 'utf8'));
}

/** Run a build script and report its exit status and combined output. */
async function runBuildScript(script, argumentList) {
  try {
    const { stdout, stderr } = await run(process.execPath, [script, ...argumentList], {
      cwd: EXTENSION_ROOT,
    });
    return { code: 0, output: stdout + stderr };
  } catch (error) {
    return { code: error.code ?? 1, output: (error.stdout ?? '') + (error.stderr ?? '') };
  }
}

async function runBuild(argumentList) {
  return runBuildScript(BUILD_SCRIPT, argumentList);
}

/** Archives the package step leaves in the extension's dist directory. */
async function packagedArchives() {
  const names = await readdir(path.join(EXTENSION_ROOT, 'dist'));
  return names.filter((name) => /^conversation-analytics-.+\.zip$/.test(name)).sort();
}

async function clearPackagedArchives() {
  for (const name of await packagedArchives()) {
    await rm(path.join(EXTENSION_ROOT, 'dist', name), { force: true });
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

async function readBuiltBackground() {
  return readFile(path.join(EXTENSION_ROOT, 'dist', 'background.js'), 'utf8');
}

test('a packaged build binds the runtime to the verified instruments', async () => {
  await withScratchDirectory(async ({ signingRule }) => {
    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${SYNTHETIC_BINDINGS}`,
    ]);
    assert.equal(result.code, 0, result.output);

    const source = await readBuiltBackground();
    const bindings = await readSyntheticBindings();
    assert.doesNotMatch(
      source,
      /__OFCA_TEST_LEGAL_RELEASE_BINDINGS__/,
      'the packaged background must not read the unbound test seam',
    );
    assert.ok(source.includes(bindings.legal_repository_revision));
    assert.ok(source.includes(bindings.public_origin));
    for (const instrument of Object.values(bindings.instruments)) {
      for (const field of ['version', 'rendered_sha256', 'public_url', 'locale']) {
        assert.ok(
          source.includes(instrument[field]),
          `the packaged background must carry ${field}`,
        );
      }
    }
  });
});

test('a development build leaves the runtime unbound', async () => {
  await withScratchDirectory(async () => {
    const result = await runBuild([]);
    assert.equal(result.code, 0, result.output);

    const source = await readBuiltBackground();
    const bindings = await readSyntheticBindings();
    assert.match(
      source,
      /__OFCA_TEST_LEGAL_RELEASE_BINDINGS__/,
      'an unbound build must compile the fail-closed module verbatim',
    );
    assert.ok(!source.includes(bindings.legal_repository_revision));
  });
});

test('the recorded digest is the digest of the bindings file as fetched', async () => {
  await withScratchDirectory(async ({ signingRule }) => {
    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${SYNTHETIC_BINDINGS}`,
    ]);
    assert.equal(result.code, 0, result.output);

    const metadata = await readBuildMetadata();
    assert.deepEqual(
      Object.keys(metadata.legal_bindings).sort(),
      ['legal_bindings_digest', 'schema', 'source_revision'],
    );
    assert.equal(
      metadata.legal_bindings.legal_bindings_digest,
      fileDigest(await readFile(SYNTHETIC_BINDINGS)),
    );

    const bindings = await readSyntheticBindings();
    const recorded = JSON.stringify(metadata);
    const carried = [];
    for (const [name, instrument] of Object.entries(bindings.instruments)) {
      for (const field of ['version', 'rendered_sha256', 'public_url', 'locale']) {
        if (recorded.includes(JSON.stringify(instrument[field]))) carried.push(`${name}.${field}`);
      }
    }
    if (recorded.includes(JSON.stringify(bindings.public_origin))) carried.push('public_origin');
    assert.deepEqual(carried, [], 'the artifact must record no instrument value');
  });
});

/** Rebuild a value with every object's members in the opposite order. */
function withReversedMembers(value) {
  if (Array.isArray(value)) return value.map(withReversedMembers);
  if (value === null || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value)
      .reverse()
      .map(([name, member]) => [name, withReversedMembers(member)]),
  );
}

test('the canonical serializer matches the Legal contract vector', async () => {
  const vector = await readFile(CONTRACT_VECTOR);
  const document = JSON.parse(vector.toString('utf8'));
  const drift = 'the Legal release bindings canonicalizer has drifted from the Legal contract';
  assert.equal(vector.length, CONTRACT_VECTOR_BYTE_LENGTH, drift);
  // Serializing the document and its member-reversed twin must both land on the
  // published bytes, so the vector pins the sorting rule and not just the text.
  for (const candidate of [document, withReversedMembers(document)]) {
    const canonical = Buffer.from(canonicalLegalBindingsJson(candidate), 'utf8');
    assert.equal(canonical.length, CONTRACT_VECTOR_BYTE_LENGTH, drift);
    assert.equal(Buffer.compare(vector, canonical), 0, drift);
    assert.equal(fileDigest(canonical), CONTRACT_VECTOR_DIGEST, drift);
  }
});

test('the canonical serializer sorts members and keeps array order', () => {
  assert.equal(
    canonicalLegalBindingsJson({ b: 1, a: [3, 1, 2], A: null, 'å': true }),
    '{"A":null,"a":[3,1,2],"b":1,"\\u00e5":true}',
  );
  assert.throws(() => canonicalLegalBindingsJson({ value: Number.POSITIVE_INFINITY }));
});

test('a production package is refused when the bindings members are reordered', async () => {
  await withScratchDirectory(async ({ directory, signingRule }) => {
    // The same bindings validate to the same object either way, so only the
    // stored bytes distinguish them, and only the canonical order is stored.
    const bindings = await readSyntheticBindings();
    const reordered = JSON.stringify({
      instruments: bindings.instruments,
      public_origin: bindings.public_origin,
      activation_schema: bindings.activation_schema,
      legal_repository_revision: bindings.legal_repository_revision,
      schema: bindings.schema,
    });
    const reorderedPath = path.join(directory, 'reordered-bindings.json');
    await writeFile(reorderedPath, reordered, 'utf8');

    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${reorderedPath}`,
    ]);
    assert.notEqual(result.code, 0, result.output);
    assert.match(result.output, /are not canonical JSON/);
  });
});

test('a production package is refused when the bindings file is pretty-printed', async () => {
  await withScratchDirectory(async ({ directory, signingRule }) => {
    const bindingsPath = path.join(directory, 'pretty-bindings.json');
    await writeFile(bindingsPath, authoredOrderJson(await readSyntheticBindings()), 'utf8');

    const result = await runBuild([
      '--package',
      `--packaged-signing-rule=${signingRule}`,
      `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      `--legal-release-bindings=${bindingsPath}`,
    ]);
    assert.notEqual(result.code, 0, result.output);
    assert.match(result.output, /are not canonical JSON/);
  });
});

test('an archive that fails its own audit is not left behind', async () => {
  const source = await readFile(BUILD_SCRIPT, 'utf8');
  assert.ok(source.includes(PACKAGE_GATE), 'the package gate no longer matches the build script');
  const withoutGate = path.join(EXTENSION_ROOT, 'build.package-gate-removed.mjs');
  await writeFile(withoutGate, source.replace(PACKAGE_GATE, PACKAGE_GATE_REMOVED, 1), 'utf8');
  try {
    await withScratchDirectory(async ({ signingRule }) => {
      await clearPackagedArchives();
      const result = await runBuildScript(withoutGate, [
        '--package',
        `--packaged-signing-rule=${signingRule}`,
        `--privacy-policy-url=${PRIVACY_POLICY_URL}`,
      ]);
      assert.notEqual(result.code, 0, result.output);
      assert.match(result.output, /The artifact records none/);
      assert.deepEqual(await packagedArchives(), [], 'a refused package left an archive behind');
    });
  } finally {
    await rm(withoutGate, { force: true });
  }
});

test('a development build stays permitted without Legal release bindings', async () => {
  const result = await runBuild([]);
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /Deterministic extension build and audit passed/);
});
