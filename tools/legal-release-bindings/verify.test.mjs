/**
 * Falsifiers for the release bindings gate.
 *
 * The gate is the step that stands between a declared release and a Store
 * candidate, so every check it makes is exercised from both sides: the same
 * request with one coordinate changed, and the staged document asserted absent
 * rather than the exit code asserted non-zero.
 *
 * The Legal repository is replaced by a loopback server that speaks the same
 * three routes. No credential and no Legal document reaches this suite: the
 * signing key is generated per run and the documents are the checked-in
 * synthetic fixture and the published contract vector.
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

import { canonicalLegalBindingsJson } from './canonical-json.mjs';

const run = promisify(execFile);
const GATE = fileURLToPath(new URL('./verify.mjs', import.meta.url));
const FIXTURES = new URL('../../extension/tests/fixtures/', import.meta.url);
const SYNTHETIC = fileURLToPath(new URL('./legal-instrument-bindings.synthetic.json', FIXTURES));
const CONTRACT_VECTOR = fileURLToPath(
  new URL('./legal-instrument-bindings.contract-vector.json', FIXTURES),
);
const CONTRACT_VECTOR_DIGEST =
  'ba66e07ff67cd341f979791c71b1208ff7d274aa9217692a2e3e8d167eb1f2e9';

const EXIT_COORDINATE_REJECTED = 2;
const EXIT_CREDENTIAL_ABSENT = 3;
const EXIT_RETRIEVAL_FAILED = 4;
const EXIT_NOT_CANONICAL = 5;
const EXIT_DIGEST_MISMATCH = 6;
const EXIT_SOURCE_REVISION_MISMATCH = 7;
const EXIT_SOURCE_REVISION_ABSENT = 8;

// A fetch revision distinct from every document's embedded source revision.
// The Legal contract keeps the two independent, so the suite never supplies a
// pair that would let an equality check pass unnoticed.
const FETCH_REVISION = '1f0d2c3b4a596877665544332211ffeeddccbbaa';
const DOCUMENT_PATH = 'compliance/cws/releases/2.0.1/legal-release-bindings.json';
const REPOSITORY = 'test-owner/test-legal';
const PRODUCT_REVISION = '9988776655443322110000ffeeddccbbaa998877';
const INSTALLATION_ID = '424242';
const APPLICATION_ID = '1234';
const INSTALLATION_TOKEN = 'ghs-synthetic-installation-token';

const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const PRIVATE_KEY_B64 = Buffer.from(
  privateKey.export({ type: 'pkcs8', format: 'pem' }),
).toString('base64');

function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/**
 * A loopback stand-in for the three Legal repository routes the gate uses.
 * It records every request so a test can assert that a refusal happened
 * before the gate reached the network at all.
 */
async function withLegalRepository(state, body) {
  const requests = [];
  const server = createServer((request, response) => {
    requests.push(request.url);
    const url = new URL(request.url, 'http://127.0.0.1');
    if (request.method === 'POST' && url.pathname.endsWith('/access_tokens')) {
      response.writeHead(201, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ token: INSTALLATION_TOKEN }));
      return;
    }
    const commit = url.pathname.match(/\/commits\/([0-9a-f]{40})$/);
    if (request.method === 'GET' && commit) {
      const known = state.commits.includes(commit[1]);
      response.writeHead(known ? 200 : 422, { 'content-type': 'application/json' });
      response.end(JSON.stringify(known ? { sha: commit[1] } : { message: 'No commit found' }));
      return;
    }
    const contents = url.pathname.startsWith(`/repos/${REPOSITORY}/contents/`)
      ? url.pathname.slice(`/repos/${REPOSITORY}/contents/`.length)
      : null;
    const authorized = request.headers.authorization === `Bearer ${INSTALLATION_TOKEN}`;
    if (request.method === 'GET' && contents !== null && authorized) {
      const held = state.documents[`${url.searchParams.get('ref')}:${decodeURI(contents)}`];
      if (held === undefined) {
        response.writeHead(404, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ message: 'Not Found' }));
        return;
      }
      response.writeHead(200, { 'content-type': 'application/vnd.github.raw' });
      response.end(held);
      return;
    }
    response.writeHead(404, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ message: 'Not Found' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const directory = await mkdtemp(path.join(tmpdir(), 'ofca-release-bindings-'));
  try {
    return await body({
      baseUrl: `http://127.0.0.1:${server.address().port}`,
      output: path.join(directory, 'staged', 'legal-release-bindings.json'),
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
    PRODUCT_REVISION,
    GITHUB_SHA: PRODUCT_REVISION,
    LEGAL_BINDINGS_API_BASE_URL: context.baseUrl,
    LEGAL_BINDINGS_REPOSITORY: REPOSITORY,
    LEGAL_BINDINGS_APP_ID: APPLICATION_ID,
    LEGAL_BINDINGS_APP_PRIVATE_KEY_B64: PRIVATE_KEY_B64,
    LEGAL_BINDINGS_INSTALLATION_ID: INSTALLATION_ID,
    LEGAL_BINDINGS_PATH: DOCUMENT_PATH,
    LEGAL_BINDINGS_REPOSITORY_REVISION: FETCH_REVISION,
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
    'a refused gate staged a bindings document for the packaging step to read',
  );
}

/** A document, its canonical bytes, and the coordinates that describe it. */
async function bindings(fixture = SYNTHETIC) {
  const bytes = await readFile(fixture);
  const document = JSON.parse(bytes.toString('utf8'));
  return {
    document,
    bytes,
    digest: sha256Hex(bytes),
    sourceRevision: document.legal_repository_revision,
  };
}

/** The default world: the document is held at the fetch revision. */
async function heldState(fixture = SYNTHETIC) {
  const held = await bindings(fixture);
  return {
    held,
    state: {
      commits: [held.sourceRevision, FETCH_REVISION],
      documents: { [`${FETCH_REVISION}:${DOCUMENT_PATH}`]: held.bytes },
    },
  };
}

test('a declared release stages the document its coordinates name', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    // The source revision and the fetch revision are different commits here,
    // which is the shape the Legal contract defines. A gate that required them
    // to be equal would refuse this release.
    assert.notEqual(held.sourceRevision, FETCH_REVISION);
    const result = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    });
    assert.equal(result.code, 0, result.output);
    assert.match(result.output, new RegExp(held.digest));
    assert.equal(Buffer.compare(await readFile(context.output), held.bytes), 0);
  });
});

test('a source revision equal to the fetch revision is also accepted', async () => {
  // The contract leaves the two coordinates independent in both directions, so
  // neither equality nor inequality may be a condition of the release.
  const held = await bindings();
  const document = { ...held.document, legal_repository_revision: FETCH_REVISION };
  const bytes = Buffer.from(canonicalLegalBindingsJson(document), 'utf8');
  const state = {
    commits: [FETCH_REVISION],
    documents: { [`${FETCH_REVISION}:${DOCUMENT_PATH}`]: bytes },
  };
  await withLegalRepository(state, async (context) => {
    const result = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: FETCH_REVISION,
      LEGAL_BINDINGS_DIGEST: sha256Hex(bytes),
    });
    assert.equal(result.code, 0, result.output);
  });
});

test('the gate reproduces the published contract vector digest', async () => {
  const { held, state } = await heldState(CONTRACT_VECTOR);
  assert.equal(held.digest, CONTRACT_VECTOR_DIGEST);
  await withLegalRepository(state, async (context) => {
    const result = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: CONTRACT_VECTOR_DIGEST,
    });
    assert.equal(result.code, 0, result.output);
    assert.equal(sha256Hex(await readFile(context.output)), CONTRACT_VECTOR_DIGEST);
  });
});

test('a digest that does not describe the document stages nothing', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const wrong = `${held.digest.slice(0, 63)}${held.digest.endsWith('a') ? 'b' : 'a'}`;
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: wrong,
    });
    assert.equal(refused.code, EXIT_DIGEST_MISMATCH, refused.output);
    assert.match(refused.output, /does not match the declared digest/);
    await assertNothingStaged(context);

    const accepted = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    });
    assert.equal(accepted.code, 0, accepted.output);
  });
});

test('a fetch revision that does not hold the document stages nothing', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
      LEGAL_BINDINGS_REPOSITORY_REVISION: held.sourceRevision,
    });
    assert.equal(refused.code, EXIT_RETRIEVAL_FAILED, refused.output);
    assert.match(refused.output, /not retrievable at the declared/);
    await assertNothingStaged(context);
  });
});

test('a path the fetch revision does not hold stages nothing', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
      LEGAL_BINDINGS_PATH: 'compliance/cws/releases/9.9.9/legal-release-bindings.json',
    });
    assert.equal(refused.code, EXIT_RETRIEVAL_FAILED, refused.output);
    await assertNothingStaged(context);
  });
});

test('a document approved at another source revision stages nothing', async () => {
  const { held, state } = await heldState();
  const other = 'aaaabbbbccccddddeeeeffff00001111222233ff';
  state.commits.push(other);
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: other,
      LEGAL_BINDINGS_DIGEST: held.digest,
    });
    assert.equal(refused.code, EXIT_SOURCE_REVISION_MISMATCH, refused.output);
    assert.match(refused.output, /approved at a source revision other than the declared one/);
    await assertNothingStaged(context);
  });
});

test('a source revision absent from the Legal repository stages nothing', async () => {
  const { held, state } = await heldState();
  state.commits = [FETCH_REVISION];
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    });
    assert.equal(refused.code, EXIT_SOURCE_REVISION_ABSENT, refused.output);
    assert.match(refused.output, /does not name a commit in the Legal/);
    await assertNothingStaged(context);
  });
});

test('a non-canonical document refuses distinguishably from a digest mismatch', async () => {
  const held = await bindings();
  // Pretty-printed, so the document is semantically identical and only its
  // serialization differs. The canonical digest is supplied, which is the
  // digest a check over canonicalized content would compute and accept.
  const pretty = Buffer.from(`${JSON.stringify(held.document, null, 2)}\n`, 'utf8');
  const state = {
    commits: [held.sourceRevision, FETCH_REVISION],
    documents: { [`${FETCH_REVISION}:${DOCUMENT_PATH}`]: pretty },
  };
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    });
    assert.equal(refused.code, EXIT_NOT_CANONICAL, refused.output);
    assert.notEqual(refused.code, EXIT_DIGEST_MISMATCH);
    assert.match(refused.output, /not stored in its canonical form/);
    await assertNothingStaged(context);
  });
});

test('a duplicate member name refuses as a non-canonical document', async () => {
  // The canonicalizer alone cannot see a duplicate member name, because the
  // parse collapses it first. The byte comparison at this call site is what
  // rejects it, so the comparison is pinned here and not only where the
  // extension build makes it.
  const held = await bindings();
  const duplicated = Buffer.from(
    held.bytes.toString('utf8').replace('{"activation_schema"', '{"schema":"x","activation_schema"'),
    'utf8',
  );
  assert.notEqual(Buffer.compare(duplicated, held.bytes), 0, 'the duplicate was not introduced');
  const state = {
    commits: [held.sourceRevision, FETCH_REVISION],
    documents: { [`${FETCH_REVISION}:${DOCUMENT_PATH}`]: duplicated },
  };
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: sha256Hex(duplicated),
    });
    assert.equal(refused.code, EXIT_NOT_CANONICAL, refused.output);
    await assertNothingStaged(context);
  });
});

test('an absent retrieval credential stops before the gate reaches the repository', async () => {
  const { held, state } = await heldState();
  for (const absent of [
    'LEGAL_BINDINGS_REPOSITORY',
    'LEGAL_BINDINGS_APP_ID',
    'LEGAL_BINDINGS_APP_PRIVATE_KEY_B64',
    'LEGAL_BINDINGS_INSTALLATION_ID',
  ]) {
    await withLegalRepository(state, async (context) => {
      const refused = await runGate(context, {
        LEGAL_REPOSITORY_REVISION: held.sourceRevision,
        LEGAL_BINDINGS_DIGEST: held.digest,
        [absent]: undefined,
      });
      assert.equal(refused.code, EXIT_CREDENTIAL_ABSENT, refused.output);
      assert.match(refused.output, new RegExp(`credential is not configured[^]*${absent}`));
      assert.deepEqual(context.requests, [], 'the gate contacted the repository unauthenticated');
      await assertNothingStaged(context);
    });
  }
});

test('a configured credential that cannot sign stops before any retrieval', async () => {
  const { held, state } = await heldState();
  for (const unusable of ['', 'bm90LWEta2V5', Buffer.from('----- not a key -----').toString('base64')]) {
    await withLegalRepository(state, async (context) => {
      const refused = await runGate(context, {
        LEGAL_REPOSITORY_REVISION: held.sourceRevision,
        LEGAL_BINDINGS_DIGEST: held.digest,
        LEGAL_BINDINGS_APP_PRIVATE_KEY_B64: unusable,
      });
      assert.equal(refused.code, EXIT_CREDENTIAL_ABSENT, refused.output);
      assert.deepEqual(context.requests, [], 'the gate contacted the repository unauthenticated');
      await assertNothingStaged(context);
    });
  }
});

test('a coordinate that can move is rejected before any retrieval', async () => {
  const { held, state } = await heldState();
  const movable = [
    ['LEGAL_BINDINGS_REPOSITORY_REVISION', 'master'],
    ['LEGAL_BINDINGS_REPOSITORY_REVISION', 'refs/heads/main'],
    ['LEGAL_BINDINGS_REPOSITORY_REVISION', FETCH_REVISION.slice(0, 12)],
    ['LEGAL_REPOSITORY_REVISION', 'latest'],
    ['LEGAL_REPOSITORY_REVISION', ''],
    ['LEGAL_BINDINGS_PATH', ''],
    ['LEGAL_BINDINGS_PATH', 'compliance/cws/releases/current'],
    ['LEGAL_BINDINGS_PATH', '../legal-release-bindings.json'],
    ['LEGAL_BINDINGS_DIGEST', ''],
    ['LEGAL_BINDINGS_DIGEST', `sha256:${held.digest}`],
  ];
  await withLegalRepository(state, async (context) => {
    for (const [name, value] of movable) {
      const refused = await runGate(context, {
        LEGAL_REPOSITORY_REVISION: held.sourceRevision,
        LEGAL_BINDINGS_DIGEST: held.digest,
        [name]: value,
      });
      assert.equal(refused.code, EXIT_COORDINATE_REJECTED, `${name}=${value}: ${refused.output}`);
      await assertNothingStaged(context);
    }
    assert.deepEqual(context.requests, [], 'a rejected coordinate still reached the repository');
  });
});

test('a Product revision other than the dispatched one stages nothing', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const coordinates = {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    };
    const detached = await runGate(context, {
      ...coordinates,
      GITHUB_SHA: '0011223344556677889900aabbccddeeff001122',
    });
    assert.equal(detached.code, EXIT_COORDINATE_REJECTED, detached.output);
    assert.match(detached.output, /not the revision this run was dispatched at/);
    assert.deepEqual(context.requests, [], 'a rejected Product revision still reached the repository');
    await assertNothingStaged(context);

    for (const movable of ['main', 'v2.0.1', PRODUCT_REVISION.slice(0, 12), '']) {
      const rejected = await runGate(context, { ...coordinates, PRODUCT_REVISION: movable, GITHUB_SHA: movable });
      assert.equal(rejected.code, EXIT_COORDINATE_REJECTED, `${movable}: ${rejected.output}`);
      await assertNothingStaged(context);
    }

    const bound = await runGate(context, coordinates);
    assert.equal(bound.code, 0, bound.output);
  });
});

test('the sha256-prefixed digest form names its own rejection', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: `sha256:${held.digest}`,
    });
    assert.match(refused.output, /bare lowercase 64-hex, not the sha256:-prefixed form/);
  });
});

test('the verified document is stageable only on ephemeral runner storage', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const coordinates = {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
    };
    const outside = path.join(path.dirname(context.runnerTemp), 'outside-bindings.json');
    const escaping = await runGate(context, coordinates, outside);
    assert.equal(escaping.code, EXIT_COORDINATE_REJECTED, escaping.output);
    assert.match(escaping.output, /must resolve inside RUNNER_TEMP/);
    await assert.rejects(stat(outside), (error) => error.code === 'ENOENT');

    const unset = await runGate(context, { ...coordinates, RUNNER_TEMP: undefined });
    assert.equal(unset.code, EXIT_COORDINATE_REJECTED, unset.output);

    const workspace = await runGate(context, {
      ...coordinates,
      GITHUB_WORKSPACE: path.dirname(context.runnerTemp),
    });
    assert.equal(workspace.code, EXIT_COORDINATE_REJECTED, workspace.output);
    assert.match(workspace.output, /must not resolve inside the workspace/);
    await assertNothingStaged(context);
  });
});

test('retrieval cannot be redirected away from the public API', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const redirected = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: held.digest,
      LEGAL_BINDINGS_API_BASE_URL: 'https://legal-mirror.example.com',
    });
    assert.equal(redirected.code, EXIT_COORDINATE_REJECTED, redirected.output);
    assert.match(redirected.output, /may only name a loopback address/);
    await assertNothingStaged(context);
  });
});

test('a refusal prints no coordinate value and no document content', async () => {
  const { held, state } = await heldState();
  await withLegalRepository(state, async (context) => {
    const refused = await runGate(context, {
      LEGAL_REPOSITORY_REVISION: held.sourceRevision,
      LEGAL_BINDINGS_DIGEST: `${held.digest.slice(0, 63)}0`,
    });
    assert.notEqual(refused.code, 0);
    for (const secret of [
      DOCUMENT_PATH,
      REPOSITORY,
      INSTALLATION_TOKEN,
      held.document.public_origin,
      held.document.instruments.terms_of_service.rendered_sha256,
    ]) {
      assert.equal(refused.output.includes(secret), false, `the refusal printed ${secret}`);
    }
  });
});
