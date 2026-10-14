import assert from 'node:assert/strict';
import test from 'node:test';
import vectors from '../node_modules/local-authenticated-read-connector/publication/vectors.json' with { type: 'json' };
import numericVector from './fixtures/signing-parameter-numeric-representation.json' with { type: 'json' };
import { verifyPublisherKeyset, verifyParameterCatalog, verifyParameterObject, decodePublicationBase64url,
  createVerifiedSigningParameterSource } from 'local-authenticated-read-connector/browser-signing';
import { createWorkerParameterOwner, createParameterPersistence, prepareSigningDependency,
  PARAMETER_STATE_KEY, PARAMETER_ALARM } from '../runtime/signing-parameters.mjs';
import { createObserverSigningProvider } from '../runtime/observer-signer.mjs';
import { createSignerReleaseFixture, RULE, TIMESTAMP } from './signer-release-fixture.mjs';
import { syntheticTrust, publications } from './synthetic-publications.mjs';
import { AcquisitionAllowance } from '../transport/acquisition-allowance.mjs';
import { createLazyAccountSigner } from '../transport/agent-runtime-core.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { HistoryAcquisitionCoordinator as ReadOnlyCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { DurableIngestOutbox as ReadOnlyOutbox } from '../transport/read-only-durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { createReadOnlyIndexedDbIngestionStorage } from '../transport/read-only-indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { TRAVERSAL_ACCOUNT, TRAVERSAL_TIME, TRAVERSAL_KEY, nativeTraversalBody, traversalConfiguration } from './signer-traversal-scenario.mjs';

for (const fixture of vectors.fixtures) test(`packaged WebCrypto producer vector ${fixture.name}`, async () => {
  let accepted = false;
  try {
    const keys = await verifyPublisherKeyset(fixture.keyset, vectors.trust, { now: fixture.now });
    const catalog = await verifyParameterCatalog(fixture.catalog, keys, { now: fixture.now });
    await verifyParameterObject(decodePublicationBase64url(fixture.object, 16384),
      catalog.payload.rules.find(entry => entry.source_revision === fixture.source_revision));
    accepted = true;
  } catch { /* independent verifier refusal */ }
  assert.equal(accepted, fixture.valid);
});

test('real producer digest-pinned integral numeric object verifies through packaged JS', async () => {
  const f = numericVector;
  assert.equal(f.producer_source, '5c635031fdce2f723470d738b8cfb768e0ed9dbe');
  const keys = await verifyPublisherKeyset(f.keyset, f.trust, { now: f.now });
  const catalog = await verifyParameterCatalog(f.catalog, keys, { now: f.now });
  const raw = decodePublicationBase64url(f.object, 16384);
  assert.match(new TextDecoder().decode(raw), /-17e0/u);
  const verified = await verifyParameterObject(raw, catalog.payload.rules[0]);
  assert.equal(verified.format.checksum_constant, -17);
});

function chromeStorage() {
  const values = {}, alarms = new Map();
  const storage = {
    async get(keys) { return Object.fromEntries(keys.filter(key => Object.hasOwn(values, key)).map(key => [key, structuredClone(values[key])])); },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async setAccessLevel({ accessLevel }) { assert.equal(accessLevel, 'TRUSTED_CONTEXTS'); },
  };
  return { values, alarms, chromeApi: { storage: { local: storage }, alarms: {
    get: async name => alarms.get(name), create: async (name, config) => alarms.set(name, { scheduledTime: config.when }),
    clear: async name => alarms.delete(name),
  } } };
}

test('denied refresh admission does not suppress same-clock admitted retry', async () => {
  const h = chromeStorage(), publication = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let requests = 0;
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: async () => {
      requests++;
      return { schema: 'signing-parameter-resolution/v1', status: 'available',
        keyset: publication.keyset, catalog: publication.catalog, object: publication.object };
    } });
  const admitted = { assertCurrent() {} };
  await assert.rejects(owner.refresh({ assertCurrent() { throw new Error('No current authority'); } }, RULE.source_revision));
  assert.equal(requests, 0);
  await owner.refresh(admitted, RULE.source_revision);
  assert.equal(requests, 1);
  assert.equal((await owner.source()).inspect(RULE.source_revision), 'eligible');
  await owner.close();
});

test('authority loss during refresh does not arm a deadline for its replacement', async () => {
  const h = chromeStorage(), published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let admitted = true, requests = 0;
  const context = { assertCurrent() { if (!admitted) throw new Error('Authority ended'); } };
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: async () => {
      if (++requests === 1) admitted = false;
      return { schema: 'signing-parameter-resolution/v1', status: 'available',
        keyset: published.keyset, catalog: published.catalog, object: published.object };
    } });
  await assert.rejects(owner.refresh(context, RULE.source_revision));
  admitted = true;
  await owner.refresh(context, RULE.source_revision);
  assert.equal(requests, 2);
  await owner.close();
});

test('expected refresh failure retries after short advice and accepts a withdrawal', async () => {
  const h = chromeStorage();
  let clock = TIMESTAMP, requests = 0;
  let published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let failure = false;
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => clock,
    resolver: async () => {
      requests++;
      return failure
        ? { schema: 'signing-parameter-resolution/v1', status: 'unavailable', keyset: null, catalog: null, reason: 'network', retry_after_ms: 1000 }
        : { schema: 'signing-parameter-resolution/v1', keyset: published.keyset, catalog: published.catalog,
          ...(JSON.parse(Buffer.from(published.catalog.split('.')[1], 'base64url')).rules.length
            ? { status: 'available', object: published.object } : { status: 'not_published', retry_after_ms: 1000 }) };
    } });
  const context = { assertCurrent() {} }, source = await owner.source();
  await owner.operation(context, current => current.resolve(RULE.source_revision));
  failure = true;
  await assert.rejects(owner.refresh(context, RULE.source_revision), { code: 'parameters_unavailable' });
  failure = false;
  published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000), sequence: 2, empty: true });
  clock += 999;
  await owner.refresh(context, RULE.source_revision);
  assert.equal(requests, 2);
  clock++;
  await assert.rejects(owner.refresh(context, RULE.source_revision), { code: 'parameters_pending' });
  assert.equal(source.inspect(RULE.source_revision), 'not_published');
  await owner.close();
});

test('concurrent ordinary refreshes coalesce only after admitted success', async () => {
  const h = chromeStorage(), published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let requests = 0;
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: async () => { requests++; await new Promise(resolve => setImmediate(resolve));
      return { schema: 'signing-parameter-resolution/v1', status: 'available',
        keyset: published.keyset, catalog: published.catalog, object: published.object }; } });
  const context = { assertCurrent() {} };
  await Promise.all(Array.from({ length: 3 }, () => owner.refresh(context, RULE.source_revision)));
  assert.equal(requests, 1);
  await owner.close();
});

test('one worker authority survives account-owner changes and missing checkpoints refuse', async () => {
  const h = chromeStorage();
  const publication = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let requests = 0;
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: async () => { requests++; return { schema: 'signing-parameter-resolution/v1', status: 'available',
      keyset: publication.keyset, catalog: publication.catalog, object: publication.object }; } });
  const source = await owner.source();
  assert.equal(requests, 0);
  assert.equal(source.status().state, 'empty');
  const context = { creatorAccountId: 'a', assertCurrent() {} };
  await owner.operation(context, current => current.resolve(RULE.source_revision));
  assert.equal(source.inspect(RULE.source_revision), 'eligible');
  assert.equal(await owner.source(), source);
  assert.equal(Object.hasOwn(h.values[PARAMETER_STATE_KEY], 'creator_account_id'), false);
  await owner.operation({ creatorAccountId: 'b', assertCurrent() {} }, current => current.resolve(RULE.source_revision));
  assert.equal(requests, 1, 'account replacement does not replace publication owner or cached rule');
  await owner.close();
  delete h.values[PARAMETER_STATE_KEY];
  const broken = await createVerifiedSigningParameterSource({ trust: syntheticTrust, now: () => TIMESTAMP,
    persistence: createParameterPersistence(h.chromeApi.storage.local), resolver: () => { throw new Error('Must not reset authority'); } });
  assert.equal(broken.status().state, 'unavailable');
  await broken.close();
});

test('timed-out first-use marker write remains owned until drain before replacement', async () => {
  const h = chromeStorage();
  let release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  const original = h.chromeApi.storage.local.set;
  h.chromeApi.storage.local.set = async update => { entered(); await blocked; await original(update); };
  const persistence = createParameterPersistence(h.chromeApi.storage.local);
  const source = await createVerifiedSigningParameterSource({ trust: syntheticTrust, persistence,
    now: () => TIMESTAMP, resolveTimeoutMs: 10, resolver() { throw new Error('Unavailable owned source cannot retrieve'); } });
  await started;
  assert.equal(source.status().state, 'unavailable');
  await source.close();
  let drained = false;
  const drain = persistence.drain().then(() => { drained = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(drained, false);
  release(); await drain;
  const replacement = await createVerifiedSigningParameterSource({ trust: syntheticTrust,
    persistence: createParameterPersistence(h.chromeApi.storage.local), now: () => TIMESTAMP, resolver() {} });
  assert.equal(replacement.status().state, 'empty');
  await replacement.close();
});

test('same-document backend drift and missing marker preserve policy3; new document waits without fallback', async () => {
  const f = createSignerReleaseFixture({ observeExisting: true });
  const h = chromeStorage();
  let published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  let resolverCalls = 0;
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: async request => {
      resolverCalls++;
      const entries = JSON.parse(Buffer.from(published.catalog.split('.')[1], 'base64url')).rules;
      return { schema: 'signing-parameter-resolution/v1', keyset: published.keyset, catalog: published.catalog,
        ...(entries.some(entry => entry.source_revision === request.source_revision)
          ? { status: 'available', object: published.object } : { status: 'not_published', retry_after_ms: 1000 }) };
    } });
  const source = await owner.source();
  const context = { assertCurrent() {} };
  const provider = await createObserverSigningProvider({ parameterSource: source, chromeApi: f.chromeApi,
    persistence: { ...f.persistence, drain: async () => {} }, captureMode: 'observe-only', expectedIdentity: '9001', now: () => TIMESTAMP },
    { observer: { helper: null }, observationWindowMs: 50 });
  const prepare = () => owner.operation(context, () => provider.prepare({}));
  const read = () => owner.operation(context, () => provider.read({ operation: 'identity', refreshMode: 'allow' }));
  await prepare();
  assert.equal((await read()).success, true);
  assert.equal(f.snapshot().active.proof_policy_version, 3);
  const generation = f.snapshot().active.id;
  const before = { captures: f.calls.captures.length, saves: f.calls.saves, calls: resolverCalls, reloads: f.calls.reloads };
  f.responseRevision = 'backend-B';
  await prepare();
  assert.equal((await read()).success, true);
  f.responseRevision = null;
  await prepare();
  assert.equal((await read()).success, true);
  assert.equal(f.snapshot().active.id, generation);
  assert.deepEqual({ captures: f.calls.captures.length, saves: f.calls.saves, calls: resolverCalls, reloads: f.calls.reloads }, before);
  f.rule = { ...RULE, source_revision: 'frontend-B', static_param: 'new-synthetic-static-param' };
  f.responseRevision = 'backend-C';
  f.navigate();
  const readsBefore = f.calls.reads.length;
  await assert.rejects(prepare(), error => error.code === 'parameters_pending' && error.neededRevision === 'frontend-B');
  assert.equal(f.calls.reads.length, readsBefore, 'missing B sent no signed request or fallback A');
  published = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 2 });
  await owner.prepareWait(context, { needed_revision: 'frontend-B', reason: 'parameters_pending', next_attempt_at: TIMESTAMP });
  await prepare();
  assert.equal((await read()).success, true);
  assert.equal(f.snapshot().active.config.source_revision, 'frontend-B');
  assert.equal(f.snapshot().active.proof_policy_version, 3);
  assert.equal(f.calls.reloads, 0);
  await owner.close();
});

test('periodic unchanged-digest renewal reuses account proof; accepted withdrawal blocks cached-only dispatch', async () => {
  const h = chromeStorage(), f = createSignerReleaseFixture({ observeExisting: true });
  let published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => TIMESTAMP,
    resolver: () => ({ schema: 'signing-parameter-resolution/v1', status: 'available',
      keyset: published.keyset, catalog: published.catalog, object: published.object }) });
  const source = await owner.source(), context = { assertCurrent() {} };
  const provider = await f.createProvider({ parameterSource: source, captureMode: 'observe-only' });
  await owner.operation(context, () => provider.read({ operation: 'identity' }));
  const before = { captures: f.calls.captures.length, reads: f.calls.reads.length, saves: f.calls.saves, generation: f.snapshot().active.id };
  published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000), sequence: 2, expiresAt: Math.floor(TIMESTAMP / 1000) + 2 * 86400 });
  await owner.refresh(context, RULE.source_revision, { force: true });
  assert.equal((await owner.operation(context, () => provider.read({ operation: 'identity', refreshMode: 'never' }))).success, true);
  assert.equal(f.calls.captures.length, before.captures);
  assert.equal(f.calls.saves, before.saves);
  assert.equal(f.snapshot().active.id, before.generation);
  published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000), sequence: 3, empty: true });
  await assert.rejects(owner.refresh(context, RULE.source_revision, { force: true }), { code: 'parameter_publication_rejected' });
  // Even a malformed available result cannot resurrect the newly accepted empty catalog.
  assert.equal(h.values[PARAMETER_STATE_KEY].catalog.sequence, 3);
  const reads = f.calls.reads.length;
  await assert.rejects(owner.operation(context, () => provider.read({ operation: 'identity', refreshMode: 'never' })));
  assert.equal(f.calls.reads.length, reads);
  assert.equal(f.calls.captures.length, before.captures);
  await owner.close();
});

test('dependency wait saves before alarm and preserves cursor, failure counts and page allowance', async () => {
  const h = chromeStorage();
  const owner = createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, resolver() {}, now: () => 1000 });
  const job = { job_id: 'j', cursor: 'opaque', retry_count: 2, committed_pages: 7 };
  let saved;
  const signer = { parameterOwner: owner, prepare: async () => { throw Object.assign(new Error('Dependency'), {
    code: 'parameters_pending', neededRevision: 'B', parameter_retry_after_ms: 999999 }); } };
  const save = async next => { assert.equal(h.alarms.size, 0); saved = structuredClone(next); };
  assert.equal(await prepareSigningDependency({ signer, job, save, clock: () => 1000 }), false);
  assert.deepEqual({ ...saved, parameter_wait: undefined }, { ...job, parameter_wait: undefined });
  assert.equal(saved.parameter_wait.next_attempt_at, 301000);
  assert.equal(h.alarms.get(PARAMETER_ALARM).scheduledTime, 301000);
  await owner.close();
});

test('exhausted acquisition capacity does not debit or authorize signing preparation', async () => {
  const state = { used: 3, minute: 1, days: { '1970-01-01': { automatic: 1000, requests: { history_list: 3 } } } };
  const allowance = new AcquisitionAllowance({ outbox: { updateAcquisitionState: async fn => { await fn(state); return structuredClone(state); } }, clock: () => 60000 });
  let preparation = 0;
  if (await allowance.canReserve('history_list', 3)) preparation++;
  if (await allowance.canReserve('catchup_list', 3, { automatic: true })) preparation++;
  assert.equal(preparation, 0);
  assert.equal(state.used, 3);
  assert.equal(state.days['1970-01-01'].automatic, 1000);
});

test('a concurrent final page reservation refuses proof before identity accounting', async () => {
  const state = { used: 0, minute: 1, days: {} };
  const allowance = new AcquisitionAllowance({ outbox: { updateAcquisitionState: async fn => {
    await fn(state); return structuredClone(state);
  } }, clock: () => 60000 });
  assert.equal(await allowance.canReserve('history_list', 1), true);
  assert.equal(await allowance.reserve('history_list', 1), true);
  assert.equal(await allowance.reserve('identity', 1, { requirePageCapacity: true }), false);
  assert.equal(state.days['1970-01-01'].requests.identity, 0);
  assert.equal(state.used, 1);
});

for (const [variant, Coordinator, Outbox, storageFactory] of [
  ['authoring', HistoryAcquisitionCoordinator, DurableIngestOutbox, createIndexedDbIngestionStorage],
  ['read-only', ReadOnlyCoordinator, ReadOnlyOutbox, createReadOnlyIndexedDbIngestionStorage],
]) test(`${variant} real encrypted jobs wait for exact B across worker reconstruction without cursor or allowance debit`, async () => {
  const indexedDb = new FakeIndexedDb(), h = chromeStorage();
  const f = createSignerReleaseFixture({ observeExisting: true });
  f.reply = request => f.response(nativeTraversalBody(request));
  let clock = TIMESTAMP;
  let published = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  const createOwner = () => createWorkerParameterOwner({ chromeApi: h.chromeApi, trust: syntheticTrust, now: () => clock,
    resolver: async request => {
      const entries = JSON.parse(Buffer.from(published.catalog.split('.')[1], 'base64url')).rules;
      return { schema: 'signing-parameter-resolution/v1', keyset: published.keyset, catalog: published.catalog,
        ...(entries.some(entry => entry.source_revision === request.source_revision)
          ? { status: 'available', object: published.object } : { status: 'not_published', retry_after_ms: 1000 }) };
    } });
  let owner = createOwner();
  const build = async () => {
    const storage = storageFactory(indexedDb, { databaseName: `parameter-wait-${variant}`, encryptionKey: TRAVERSAL_KEY });
    const outbox = new Outbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
    await outbox.initialize();
    const controller = new AbortController();
    const signer = createLazyAccountSigner({ storage, creatorAccountId: TRAVERSAL_ACCOUNT, chromeApi: f.chromeApi,
      signal: controller.signal, expectedIdentity: () => '9001', parameterOwner: owner,
      parameterContext: { signal: controller.signal, creatorAccountId: TRAVERSAL_ACCOUNT, assertCurrent() {} },
      factory: options => createObserverSigningProvider({ ...options, now: () => TIMESTAMP },
        { observer: { helper: null }, observationWindowMs: 50 }),
    });
    const coordinator = new Coordinator({ outbox, signer, configuration: () => traversalConfiguration(),
      session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1' }),
      now: () => TRAVERSAL_TIME, clock: () => clock });
    return { storage, outbox, coordinator, controller };
  };
  let current = await build();
  assert.equal((await current.coordinator.wake()).pages, 1);
  assert.equal((await current.outbox.updateAcquisitionState(() => {})).days[new Date(clock).toISOString().slice(0, 10)].requests.identity, 3,
    'candidate, control and requested identity are durably counted before each dispatch');
  let inventory = (await current.outbox.historyJobs()).find(job => job.kind === 'inventory');
  assert.notEqual(inventory.cursor, null);
  const cursor = inventory.cursor, sourceSeq = current.outbox.identityState().last_source_seq;
  const counts = (await current.outbox.updateAcquisitionState(() => {})).days;
  clock += 60_000;
  f.rule = { ...RULE, source_revision: 'frontend-B', static_param: 'synthetic-next-rule' };
  f.responseRevision = 'backend-independent';
  f.navigate();
  const platformRequests = f.calls.reads.length;
  assert.equal((await current.coordinator.wake()).pages, 0);
  inventory = await current.outbox.historyJob(inventory.job_id);
  assert.equal(inventory.parameter_wait.needed_revision, 'frontend-B');
  assert.equal(inventory.cursor, cursor);
  assert.equal(inventory.retry_count, 0);
  assert.equal(current.outbox.identityState().last_source_seq, sourceSeq);
  assert.deepEqual((await current.outbox.updateAcquisitionState(() => {})).days, counts);
  assert.equal(f.calls.reads.length, platformRequests);
  current.coordinator.stop(); current.controller.abort(); await owner.close();
  published = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 2 });
  clock += 1000;
  owner = createOwner();
  current = await build();
  assert.equal((await current.coordinator.wake()).pages, 1);
  assert.equal((await current.outbox.historyJob(inventory.job_id)).parameter_wait, null);
  assert.equal(f.calls.reloads, 0);
  assert.equal(h.alarms.size, 0);
  current.coordinator.stop(); current.controller.abort(); await owner.close();
  // The next catalog withdraws the active revision before a missing-revision
  // dependency has ever been recorded. Renewing the old activation is advisory;
  // the exact new browser document may reproduce the newly published revision.
  f.rule = { ...f.rule, source_revision: 'frontend-C', static_param: 'synthetic-third-rule' };
  f.navigate(); clock += 60_000;
  published = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 3 });
  const beforeC = f.calls.reads.length;
  owner = createOwner(); current = await build();
  assert.equal((await current.coordinator.wake()).pages, 1, 'withdrawn old activation does not trap a newly published native revision');
  assert.ok(f.calls.reads.slice(beforeC).every(request => request.headers['x-of-rev'] === 'frontend-C'));
  const beforeEmpty = { reads: f.calls.reads.length, captures: f.calls.captures.length };
  published = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 4, empty: true });
  await owner.refresh({ signal: current.controller.signal, creatorAccountId: TRAVERSAL_ACCOUNT, assertCurrent() {} }, 'frontend-C', { force: true }).catch(() => {});
  clock += 60_000;
  assert.equal((await current.coordinator.wake()).pages, 0, 'same proved document refuses withdrawal without observation or proof');
  assert.equal(f.calls.captures.length, beforeEmpty.captures);
  assert.equal(f.calls.reads.length, beforeEmpty.reads);
  f.rule = { ...f.rule, source_revision: 'frontend-D', static_param: 'synthetic-fourth-rule' };
  f.navigate(); clock += 300_001;
  assert.equal((await current.coordinator.wake()).pages, 0);
  assert.equal((await current.outbox.historyJobs()).find(job => job.parameter_wait)?.parameter_wait.needed_revision, 'frontend-D',
    'an empty catalog still records the exact newly observed native revision');
  assert.equal(f.calls.reads.length, beforeEmpty.reads, 'unpublished D authorizes no candidate/control/read');
  published = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 5 });
  clock += 300_001;
  assert.equal((await current.coordinator.wake()).pages, 1);
  assert.ok(f.calls.reads.slice(beforeEmpty.reads).every(request => request.headers['x-of-rev'] === 'frontend-D'));
  current.coordinator.stop(); current.controller.abort(); await owner.close();
});

test('withdrawal advisory cannot open a helper without an existing browser document', async () => {
  let recoveries = 0;
  const provider = await createObserverSigningProvider({ persistence: { drain: async () => {} } }, {
    observer: { helper: null, recoverSigning: async () => { recoveries++; } },
    createProvider: async () => ({ store: { active: async () => ({ proof_policy_version: 3 }) },
      host: { openOperation: async () => { throw Object.assign(new Error('No current document'), { code: 'browser_unavailable' }); } } }),
  });
  await assert.rejects(provider.prepare({ allowRecovery: false }), { code: 'browser_unavailable' });
  assert.equal(recoveries, 0);
});

test('changed bytes under the same revision need fresh reproduction and proof', async () => {
  const f = createSignerReleaseFixture({ observeExisting: true });
  let publication = await publications(RULE, { now: Math.floor(TIMESTAMP / 1000) });
  // Keep the wire result closed, excluding the helper's local hash receipt.
  const source = await createVerifiedSigningParameterSource({ trust: syntheticTrust, now: () => TIMESTAMP,
    persistence: { load: () => null, save() {} }, resolver: () => ({ schema: 'signing-parameter-resolution/v1', status: 'available',
      keyset: publication.keyset, catalog: publication.catalog, object: publication.object }) });
  const provider = await createObserverSigningProvider({ parameterSource: source, chromeApi: f.chromeApi,
    persistence: { ...f.persistence, drain: async () => {} }, captureMode: 'observe-only', expectedIdentity: '9001', now: () => TIMESTAMP },
    { observer: { helper: null }, observationWindowMs: 50 });
  await provider.prepare({});
  const before = f.snapshot().active.id, captures = f.calls.captures.length;
  f.rule = { ...RULE, static_param: 'replacement-bytes-same-revision' };
  publication = await publications(f.rule, { now: Math.floor(TIMESTAMP / 1000), sequence: 2 });
  await source.resolve(RULE.source_revision, { refresh: true });
  await provider.prepare({});
  assert.notEqual(f.snapshot().active.id, before);
  assert.equal(f.calls.captures.length, captures + 1);
  assert.equal(f.snapshot().active.proof_policy_version, 3);
  await source.close();
});
