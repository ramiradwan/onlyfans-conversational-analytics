/**
 * Falsifiers for the packaged signing rule gate.
 *
 * The gate stands between a declared release and the rule a Store candidate is
 * built with, so every check it makes is exercised from both sides: the same
 * request with one coordinate changed, and the staged rule asserted absent
 * rather than the exit code asserted non-zero.
 *
 * The signer repository is replaced by a loopback server that speaks the same
 * three routes. No credential and no production rule reaches this suite: the
 * signing key is generated per run and the document is the checked-in synthetic
 * fixture, re-serialized so a working tree with either line ending measures the
 * same bytes the repository holds.
 */

import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash, generateKeyPairSync } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const GATE = fileURLToPath(new URL('./verify.mjs', import.meta.url));
const FIXTURE = fileURLToPath(
  new URL('../../extension/tests/fixtures/packaged-signing-rule.json', import.meta.url),
);

const EXIT_COORDINATE_REJECTED = 2;
const EXIT_CREDENTIAL_ABSENT = 3;
const EXIT_RETRIEVAL_FAILED = 4;
const EXIT_NOT_CANONICAL = 5;
const EXIT_DIGEST_MISMATCH = 6;
const EXIT_SOURCE_REVISION_MISMATCH = 7;
const EXIT_SCHEMA_REJECTED = 9;
const EXIT_ASSET_ABSENT = 10;

const RELEASE_TAG = 'packaged-signing-rule-2026-08';
const ASSET_ID = '918273645';
// An asset the release under the declared tag does not publish. The suite holds
// the correct rule under it, so only the tag-to-asset cross-check can refuse a
// release that declares it; a gate that fetched by identifier alone would
// accept these bytes and match the declared digest.
const UNPUBLISHED_ASSET_ID = '111222333';
const REPOSITORY = 'test-owner/test-signer';
const INSTALLATION_ID = '515151';
const APPLICATION_ID = '4321';
const INSTALLATION_TOKEN = 'ghs-synthetic-signer-installation-token';

const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const PRIVATE_KEY_B64 = Buffer.from(
  privateKey.export({ type: 'pkcs8', format: 'pem' }),
).toString('base64');

function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/** The serialization the extension build packs a rule under. */
function packagedBytes(document) {
  return Buffer.from(`${JSON.stringify(document, null, 2)}\n`, 'utf8');
}

/** A loopback stand-in for the three signer repository routes the gate uses. */
async function withSignerRepository(state, body) {
  const requests = [];
  const server = createServer((request, response) => {
    requests.push(request.url);
    const url = new URL(request.url, 'http://127.0.0.1');
    const notFound = () => {
      response.writeHead(404, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ message: 'Not Found' }));
    };
    if (request.method === 'POST' && url.pathname.endsWith('/access_tokens')) {
      response.writeHead(201, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ token: INSTALLATION_TOKEN }));
      return;
    }
    const authorized = request.headers.authorization === `Bearer ${INSTALLATION_TOKEN}`;
    if (request.method !== 'GET' || !authorized) {
      notFound();
      return;
    }
    const tagPrefix = `/repos/${REPOSITORY}/releases/tags/`;
    if (url.pathname.startsWith(tagPrefix)) {
      // The API answers with numeric identifiers, so the gate is exercised
      // against the coercion it has to make rather than against strings.
      const published = state.releases[decodeURIComponent(url.pathname.slice(tagPrefix.length))];
      if (published === undefined) {
        notFound();
        return;
      }
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ assets: published.map((id) => ({ id: Number(id) })) }));
      return;
    }
    const assetPrefix = `/repos/${REPOSITORY}/releases/assets/`;
    if (url.pathname.startsWith(assetPrefix)) {
      const held = state.assets[url.pathname.slice(assetPrefix.length)];
      if (held === undefined) {
        notFound();
        return;
      }
      response.writeHead(200, { 'content-type': 'application/octet-stream' });
      response.end(held);
      return;
    }
    notFound();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const directory = await mkdtemp(path.join(tmpdir(), 'ofca-signing-rule-'));
  try {
    return await body({
      baseUrl: `http://127.0.0.1:${server.address().port}`,
      output: path.join(directory, 'staged', 'packaged-signing-rule.json'),
      runnerTemp: directory,
      requests,
    });
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { force: true, recursive: true });
  }
}

function environmentFor(context, overrides = {}) {
  return {
    PATH: process.env.PATH,
    SystemRoot: process.env.SystemRoot,
    RUNNER_TEMP: context.runnerTemp,
    SIGNING_RULE_API_BASE_URL: context.baseUrl,
    SIGNING_RULE_REPOSITORY: REPOSITORY,
    SIGNING_RULE_APP_ID: APPLICATION_ID,
    SIGNING_RULE_APP_PRIVATE_KEY_B64: PRIVATE_KEY_B64,
    SIGNING_RULE_INSTALLATION_ID: INSTALLATION_ID,
    SIGNING_RULE_RELEASE_TAG: RELEASE_TAG,
    SIGNING_RULE_RELEASE_ASSET_ID: ASSET_ID,
    ...overrides,
  };
}

async function runGate(context, overrides = {}, output = context.output) {
  const environment = environmentFor(context, overrides);
  for (const [name, value] of Object.entries(environment)) {
    if (value === undefined) delete environment[name];
  }
  try {
    const { stdout, stderr } = await run(process.execPath, [GATE, `--output=${output}`], {
      env: environment,
    });
    return { code: 0, output: stdout + stderr };
  } catch (error) {
    return { code: error.code ?? 1, output: (error.stdout ?? '') + (error.stderr ?? '') };
  }
}

async function assertNothingStaged(context) {
  await assert.rejects(
    stat(context.output),
    (error) => error.code === 'ENOENT',
    'a refused gate staged a rule for the packaging step to read',
  );
}

/** The fixture rule, its packaged bytes, and the coordinates describing them. */
async function rule(overrides = {}) {
  const document = { ...JSON.parse(await readFile(FIXTURE, 'utf8')), ...overrides };
  const bytes = packagedBytes(document);
  return { document, bytes, digest: sha256Hex(bytes) };
}

/**
 * A signer repository publishing the rule as the declared asset, and holding
 * the same bytes under an asset the release does not publish.
 */
function heldAt(bytes) {
  return {
    releases: { [RELEASE_TAG]: [ASSET_ID] },
    assets: { [ASSET_ID]: bytes, [UNPUBLISHED_ASSET_ID]: bytes },
  };
}

function coordinatesFor(held) {
  return {
    SIGNING_RULE_DIGEST: held.digest,
    SIGNING_RULE_SOURCE_REVISION: held.document.source_revision,
  };
}

test('a declared release stages the rule its coordinates name', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const result = await runGate(context, coordinatesFor(held));
    assert.equal(result.code, 0, result.output);
    assert.match(result.output, new RegExp(held.digest));
    assert.equal(Buffer.compare(await readFile(context.output), held.bytes), 0);
  });
});

test('a digest that does not describe the rule stages nothing', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const wrong = `${held.digest.slice(0, 63)}${held.digest.endsWith('a') ? 'b' : 'a'}`;
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_DIGEST: wrong,
    });
    assert.equal(refused.code, EXIT_DIGEST_MISMATCH, refused.output);
    assert.match(refused.output, /does not match the declared digest/);
    await assertNothingStaged(context);

    const accepted = await runGate(context, coordinatesFor(held));
    assert.equal(accepted.code, 0, accepted.output);
  });
});

test('a rule reproducing another platform revision stages nothing', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_SOURCE_REVISION: 'another-platform-revision',
    });
    assert.equal(refused.code, EXIT_SOURCE_REVISION_MISMATCH, refused.output);
    assert.match(refused.output, /reproduces a source revision other than the declared one/);
    await assertNothingStaged(context);
  });
});

test('a document that is not a packaged signing rule stages nothing', async () => {
  const held = await rule();
  const rejected = [
    packagedBytes({ ...held.document, schema: 'local-packaged-signing-rule/v2' }),
    packagedBytes([held.document]),
    // The same members, reordered. The build repacks in schema order, so bytes
    // stored this way would not be the bytes the declared digest names.
    packagedBytes({
      source_revision: held.document.source_revision,
      schema: held.document.schema,
      static_param: held.document.static_param,
      format: held.document.format,
    }),
    packagedBytes({
      ...held.document,
      format: {
        suffix: held.document.format.suffix,
        prefix: held.document.format.prefix,
        checksum_indexes: held.document.format.checksum_indexes,
        checksum_constant: held.document.format.checksum_constant,
      },
    }),
  ];
  for (const bytes of rejected) {
    await withSignerRepository(heldAt(bytes), async (context) => {
      const refused = await runGate(context, {
        ...coordinatesFor(held),
        SIGNING_RULE_DIGEST: sha256Hex(bytes),
      });
      assert.equal(refused.code, EXIT_SCHEMA_REJECTED, refused.output);
      await assertNothingStaged(context);
    });
  }
});

test('a rule stored under another serialization refuses distinguishably', async () => {
  const held = await rule();
  // Compact, so the rule is semantically identical and only its serialization
  // differs. Its own digest is supplied, which is what a check that hashed
  // whatever it was given would compute and accept.
  const compact = Buffer.from(JSON.stringify(held.document), 'utf8');
  assert.notEqual(Buffer.compare(compact, held.bytes), 0);
  await withSignerRepository(heldAt(compact), async (context) => {
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_DIGEST: sha256Hex(compact),
    });
    assert.equal(refused.code, EXIT_NOT_CANONICAL, refused.output);
    assert.notEqual(refused.code, EXIT_DIGEST_MISMATCH);
    assert.match(refused.output, /not stored in the serialization the package uses/);
    await assertNothingStaged(context);
  });
});

test('an absent retrieval credential stops before the gate reaches the repository', async () => {
  const held = await rule();
  for (const absent of [
    'SIGNING_RULE_REPOSITORY',
    'SIGNING_RULE_APP_ID',
    'SIGNING_RULE_APP_PRIVATE_KEY_B64',
    'SIGNING_RULE_INSTALLATION_ID',
  ]) {
    await withSignerRepository(heldAt(held.bytes), async (context) => {
      const refused = await runGate(context, {
        ...coordinatesFor(held),
        [absent]: undefined,
      });
      assert.equal(refused.code, EXIT_CREDENTIAL_ABSENT, refused.output);
      assert.match(refused.output, new RegExp(`credential is not configured[^]*${absent}`));
      assert.deepEqual(context.requests, [], 'the gate contacted the repository unauthenticated');
      await assertNothingStaged(context);
    });
  }
});

test('a coordinate the schema cannot carry is rejected before any retrieval', async () => {
  const held = await rule();
  const rejected = [
    ['SIGNING_RULE_RELEASE_TAG', ''],
    ['SIGNING_RULE_RELEASE_TAG', '../another-release'],
    ['SIGNING_RULE_RELEASE_TAG', '-leading-dash'],
    ['SIGNING_RULE_RELEASE_TAG', 'a release with spaces'],
    ['SIGNING_RULE_RELEASE_TAG', 'x'.repeat(256)],
    // An asset name is what an operator reaches for first, and it is exactly
    // the coordinate that can be reused across releases.
    ['SIGNING_RULE_RELEASE_ASSET_ID', ''],
    ['SIGNING_RULE_RELEASE_ASSET_ID', 'packaged-signing-rule.json'],
    ['SIGNING_RULE_DIGEST', ''],
    ['SIGNING_RULE_DIGEST', `sha256:${held.digest}`],
    ['SIGNING_RULE_SOURCE_REVISION', ''],
    ['SIGNING_RULE_SOURCE_REVISION', 'x'.repeat(129)],
  ];
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    for (const [name, value] of rejected) {
      const refused = await runGate(context, { ...coordinatesFor(held), [name]: value });
      assert.equal(refused.code, EXIT_COORDINATE_REJECTED, `${name}=${value}: ${refused.output}`);
      await assertNothingStaged(context);
    }
    assert.deepEqual(context.requests, [], 'a rejected coordinate still reached the repository');
  });
});

test('a tag publishing no release stages nothing', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_RELEASE_TAG: 'packaged-signing-rule-2026-09',
    });
    assert.equal(refused.code, EXIT_RETRIEVAL_FAILED, refused.output);
    assert.match(refused.output, /no release holding the .* is published under the declared tag/);
    await assertNothingStaged(context);
  });
});

test('an asset the declared release does not publish stages nothing', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    // The rule is retrievable under this identifier and matches the declared
    // digest, so every check except the tag-to-asset cross-check would pass it.
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_RELEASE_ASSET_ID: UNPUBLISHED_ASSET_ID,
    });
    assert.equal(refused.code, EXIT_ASSET_ABSENT, refused.output);
    assert.match(refused.output, /does not carry the declared asset/);
    assert.equal(
      context.requests.some((url) => url.includes(`/releases/assets/${UNPUBLISHED_ASSET_ID}`)),
      false,
      'the gate fetched an asset the resolved release does not publish',
    );
    await assertNothingStaged(context);

    const accepted = await runGate(context, coordinatesFor(held));
    assert.equal(accepted.code, 0, accepted.output);
  });
});

test('an asset identifier no release publishes stages nothing', async () => {
  const held = await rule();
  await withSignerRepository(
    { releases: { [RELEASE_TAG]: ['424242'] }, assets: {} },
    async (context) => {
      const refused = await runGate(context, {
        ...coordinatesFor(held),
        SIGNING_RULE_RELEASE_ASSET_ID: '424242',
      });
      assert.equal(refused.code, EXIT_RETRIEVAL_FAILED, refused.output);
      assert.match(refused.output, /not retrievable as the declared release asset/);
      await assertNothingStaged(context);
    },
  );
});

test('the verified rule is stageable only on ephemeral runner storage', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const outside = path.join(path.dirname(context.runnerTemp), 'outside-signing-rule.json');
    const escaping = await runGate(context, coordinatesFor(held), outside);
    assert.equal(escaping.code, EXIT_COORDINATE_REJECTED, escaping.output);
    assert.match(escaping.output, /must resolve inside RUNNER_TEMP/);
    await assert.rejects(stat(outside), (error) => error.code === 'ENOENT');

    const unset = await runGate(context, { ...coordinatesFor(held), RUNNER_TEMP: undefined });
    assert.equal(unset.code, EXIT_COORDINATE_REJECTED, unset.output);

    const workspace = await runGate(context, {
      ...coordinatesFor(held),
      GITHUB_WORKSPACE: path.dirname(context.runnerTemp),
    });
    assert.equal(workspace.code, EXIT_COORDINATE_REJECTED, workspace.output);
    assert.match(workspace.output, /must not resolve inside the workspace/);
    await assertNothingStaged(context);
  });
});

test('retrieval cannot be redirected away from the public API', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const redirected = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_API_BASE_URL: 'https://signer-mirror.example.com',
    });
    assert.equal(redirected.code, EXIT_COORDINATE_REJECTED, redirected.output);
    assert.match(redirected.output, /may only name a loopback address/);
    await assertNothingStaged(context);
  });
});

test('a refusal prints no coordinate value and no rule content', async () => {
  const held = await rule();
  await withSignerRepository(heldAt(held.bytes), async (context) => {
    const refused = await runGate(context, {
      ...coordinatesFor(held),
      SIGNING_RULE_DIGEST: `${held.digest.slice(0, 63)}0`,
    });
    assert.notEqual(refused.code, 0);
    for (const secret of [
      RELEASE_TAG,
      REPOSITORY,
      INSTALLATION_TOKEN,
      held.document.static_param,
      held.document.format.prefix,
      held.document.format.suffix,
    ]) {
      assert.equal(refused.output.includes(secret), false, `the refusal printed ${secret}`);
    }
  });
});
