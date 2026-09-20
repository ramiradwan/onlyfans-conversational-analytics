import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';

import { createLazyAccountSigner } from '../transport/agent-runtime-core.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { createSignerReleaseFixture, EXPECTED_ID } from './signer-release-fixture.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const outcome = (promise) => promise.then((value) => ({ value }), (error) => ({ error }));

function harness({ factory, storage = new InMemoryIngestionStorage(), expectedIdentity = () => '42' } = {}) {
  const lifetime = new AbortController();
  const chromeApi = {
    tabs: { async query() { return [{ id: 7, active: true, frozen: false }]; }, async get() { return { id: 7, frozen: false }; } },
    scripting: { async executeScript() { return []; } },
  };
  return {
    lifetime,
    signer: createLazyAccountSigner({
      creatorAccountId: 'application-account', chromeApi, storage, factory, expectedIdentity,
      signal: lifetime.signal,
    }),
  };
}
const packagedRule = JSON.parse(await readFile(new URL('./fixtures/packaged-signing-rule.json', import.meta.url), 'utf8'));
const request = (signal) => ({ operation: 'identity', parameters: {}, refreshMode: 'allow', signal });

test('runtime supplies the exact bounded decimal platform identity and never gates reads on status metadata', async () => {
  for (const expected of ['42', '9'.repeat(256)]) {
    let observed;
    let reads = 0;
    const h = harness({
      expectedIdentity: () => expected,
      async factory(options) {
        observed = options;
        return {
          status() { throw new Error('Metadata must not gate authorization'); },
          async read() { reads += 1; return { success: true }; },
        };
      },
    });
    await h.signer.read(request());
    assert.equal(observed.expectedIdentity, expected);
    assert.notEqual(observed.expectedIdentity, 'application-account');
    assert.ok(observed.signal instanceof AbortSignal);
    assert.equal(Object.hasOwn(observed, 'captureTimeoutMs'), false);
    assert.equal(Object.hasOwn(observed, 'transportTimeoutMs'), false);
    assert.equal(reads, 1);
  }
  for (const expected of [null, undefined, 42, '0', '01', '-1', '1.0', 'application-account', '9'.repeat(257)]) {
    let constructions = 0;
    const h = harness({ expectedIdentity: () => expected, factory() { constructions += 1; } });
    await assert.rejects(h.signer.read(request()), { code: 'identity_required' });
    assert.equal(constructions, 0);
  }
});

test('concurrent first reads share one initialized provider and one account persistence writer', async () => {
  const start = deferred();
  const finish = deferred();
  let factories = 0;
  let reads = 0;
  const h = harness({ async factory() {
    factories += 1;
    start.resolve();
    await finish.promise;
    return { async read() { reads += 1; return {}; } };
  } });
  const first = h.signer.read(request());
  const second = h.signer.read(request());
  await start.promise;
  assert.equal(factories, 1);
  finish.resolve();
  await Promise.all([first, second]);
  assert.equal(factories, 1);
  assert.equal(reads, 2);
});

test('installed cached provider survives cancellation of its completed initialization read', async () => {
  const fixture = createSignerReleaseFixture();
  const lifetime = new AbortController();
  let constructions = 0;
  let initializationSignal;
  const signer = createLazyAccountSigner({
    creatorAccountId: 'application-account',
    storage: new InMemoryIngestionStorage(),
    chromeApi: fixture.chromeApi,
    expectedIdentity: () => EXPECTED_ID,
    signal: lifetime.signal,
    async factory(options) {
      constructions += 1;
      initializationSignal = options.signal;
      return fixture.createProvider(options);
    },
  });
  const first = new AbortController();
  assert.equal((await signer.read(request(first.signal))).success, true);
  first.abort('previous_wake_finished');
  assert.equal(initializationSignal.aborted, true);
  assert.equal((await signer.read(request())).success, true);
  assert.equal(constructions, 1);
  assert.equal(fixture.calls.reloads, 1);
  assert.equal(fixture.calls.reads.length, 4);
});

test('installed signer cannot repeat a cold-bootstrap reload after session cancellation and reconstruction', async () => {
  const fixture = createSignerReleaseFixture();
  const storage = new InMemoryIngestionStorage();
  const lifetime = new AbortController();
  const cancelled = new AbortController();
  const originalReload = fixture.chromeApi.tabs.reload;
  fixture.chromeApi.tabs.reload = async function (...args) {
    await originalReload.apply(this, args);
    cancelled.abort('Agent session ended during reload');
  };
  const restart = () => createLazyAccountSigner({
    creatorAccountId: 'application-account', storage, chromeApi: fixture.chromeApi,
    expectedIdentity: () => EXPECTED_ID, signal: lifetime.signal,
    factory: (options) => fixture.createProvider(options),
  });
  await assert.rejects(restart().read(request(cancelled.signal)));
  assert.equal(fixture.calls.reloads, 1);
  for (let wake = 0; wake < 10; wake += 1) {
    const result = await outcome(restart().read(request()));
    assert.notEqual(result.value?.success, true);
    assert.equal(fixture.calls.reloads, 1);
  }
});

test('a deadline during construction cannot cache a late provider or start a competing constructor', async () => {
  const start = deferred();
  const finish = deferred();
  let factories = 0;
  let firstSignal;
  const h = harness({ async factory(options) {
    factories += 1;
    if (factories === 1) {
      firstSignal = options.signal;
      start.resolve();
      await finish.promise; // Model a storage host that finishes after cancellation.
      return { read() { throw new Error('The abandoned provider must never read'); } };
    }
    return { async read() { return 'replacement'; } };
  } });
  const controller = new AbortController();
  const first = outcome(h.signer.read(request(controller.signal)));
  await start.promise;
  controller.abort('consumer_deadline');
  assert.equal((await first).error, 'consumer_deadline');
  assert.equal(firstSignal.aborted, true);
  const second = h.signer.read(request());
  await tick();
  assert.equal(factories, 1);
  finish.resolve();
  assert.equal(await second, 'replacement');
  assert.equal(factories, 2);
});

test('replacement waits for an entered signing save acknowledgement and retains the committed document', async () => {
  const memory = new InMemoryIngestionStorage();
  const saveEntered = deferred();
  const saveFinished = deferred();
  const storage = {
    async runTransaction(mode, stores, work) {
      const result = await memory.runTransaction(mode, stores, work);
      if (mode === 'readwrite') { saveEntered.resolve(); await saveFinished.promise; }
      return result;
    },
  };
  let expected = '42';
  let factories = 0;
  let loaded;
  const saved = { schema: 'browser-signing-state/v1', state_revision: 1, active: null, previous: null };
  const h = harness({ storage, expectedIdentity: () => expected, async factory(options) {
    factories += 1;
    if (factories === 1) return { read({ signal }) {
      void options.persistence.save(saved);
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    } };
    loaded = await options.persistence.load();
    return { async read() { return 'new-owner'; } };
  } });
  const controller = new AbortController();
  const first = outcome(h.signer.read(request(controller.signal)));
  await saveEntered.promise;
  controller.abort(false);
  assert.equal((await first).error, false);
  expected = '43';
  const second = h.signer.read(request());
  await tick();
  assert.equal(factories, 1, 'new store must not read before prior save acknowledgement');
  saveFinished.resolve();
  assert.equal(await second, 'new-owner');
  assert.equal(factories, 2);
  assert.deepEqual(loaded, saved);
});

test('factory and established read preserve arbitrary cancellation reasons', async () => {
  for (const reason of [null, false, 0, 'cancelled', { cause: 'test' }]) {
    const entered = deferred();
    const h = harness({ async factory() { return { read({ signal }) {
      entered.resolve();
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    } }; } });
    const controller = new AbortController();
    const result = outcome(h.signer.read(request(controller.signal)));
    await entered.promise;
    controller.abort(reason);
    assert.strictEqual((await result).error, reason);
  }
});

for (const [code, state] of [
  ['storage_schema_unsupported', { schema: 'unsupported-signing-state', payload: 'retained' }],
  ['storage_corrupt', { schema: 'browser-signing-state/v1', state_revision: -1, active: null, previous: null }],
]) test(`installed signer preserves ${code} across runtime retries without resetting state`, async () => {
  const memory = new InMemoryIngestionStorage();
  await memory.runTransaction('readwrite', ['credentials'], (tx) => tx.put('credentials', {
    key: 'signer-state', creator_account_id: 'application-account', state,
  }));
  let constructions = 0;
  const h = harness({ storage: memory, async factory({ persistence }) {
    constructions += 1;
    assert.deepEqual(await persistence.load(), state);
    return createChromeBrowserSigningProvider({ persistence, packagedRule });
  } });
  await assert.rejects(h.signer.read(request()), { code });
  await assert.rejects(h.signer.read(request()), { code });
  assert.equal(constructions, 2);
  const record = await memory.runTransaction('readonly', ['credentials'], (tx) => tx.get('credentials', 'signer-state'));
  assert.deepEqual(record.state, state);
});
