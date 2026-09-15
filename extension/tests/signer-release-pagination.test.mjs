import { createAccountSigningPersistence } from '../transport/agent-runtime-core.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { createSignerReleaseFixture, deferred } from './signer-release-fixture.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { HistoryAcquisitionCoordinator as ReadOnlyCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { DurableIngestOutbox as ReadOnlyOutbox } from '../transport/read-only-durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { createReadOnlyIndexedDbIngestionStorage } from '../transport/read-only-indexeddb-ingestion-storage.mjs';
import { TRAVERSAL_ACCOUNT, TRAVERSAL_CREATOR, TRAVERSAL_TIME, TRAVERSAL_KEY,
  TRAVERSAL_CHAT_IDS, TRAVERSAL_MESSAGE_IDS, nativeTraversalBody, traversalConfiguration,
  expectedTraversalMessage } from './signer-traversal-scenario.mjs';

const variants = [
  ['authoring', HistoryAcquisitionCoordinator, DurableIngestOutbox, createIndexedDbIngestionStorage],
  ['read-only', ReadOnlyCoordinator, ReadOnlyOutbox, createReadOnlyIndexedDbIngestionStorage],
];

for (const [name, Coordinator, Outbox, storageFactory] of variants) {
  test(`${name}: installed signer traverses native opaque pages with reconstruction after every page`, async () => {
    const indexedDb = new FakeIndexedDb();
    const f = createSignerReleaseFixture();
    f.reply = (request) => f.response(nativeTraversalBody(request));
    const cursors = [];
    let lastState;
    let generationId;
    let finalJobs;
    let finalStorage;
    for (let wake = 0; wake < 10; wake += 1) {
      // New adapters, signer provider, outbox and coordinator; only encrypted IDB persists.
      const storage = storageFactory(indexedDb, { databaseName: `release-traversal-${name}`, encryptionKey: TRAVERSAL_KEY });
      const outbox = new Outbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
      await outbox.initialize();
      f.persistence = createAccountSigningPersistence(storage, TRAVERSAL_ACCOUNT);
      const provider = await f.createProvider();
      const coordinator = new Coordinator({ outbox, signer: { async read(request) {
        cursors.push({ operation: request.operation, conversationId: request.parameters?.conversationId,
          cursor: request.parameters?.query?.cursor });
        return provider.read(request);
      } }, configuration: () => traversalConfiguration(),
      session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1' }),
      now: () => TRAVERSAL_TIME });
      const result = await coordinator.wake();
      assert.equal(result.status, 'progressed');
      finalJobs = await outbox.historyJobs();
      const inventory = finalJobs.find((job) => job.kind === 'inventory');
      generationId ??= inventory.generation_id;
      assert.equal(inventory.generation_id, generationId);
      if (lastState) assert.equal(outbox.identityState().agent_stream_id, lastState.agent_stream_id);
      lastState = outbox.identityState();
      coordinator.stop();
      finalStorage = storage;
      if (inventory.phase === 'closed') break;
    }
    assert.equal(finalJobs.find((job) => job.kind === 'inventory').phase, 'closed');
    assert.ok(finalJobs.every((job) => job.cursor === null), 'terminal boundary clears the durable cursor');
    assert.ok(finalJobs.every((job) => job.authorized_platform_creator_id === TRAVERSAL_CREATOR));
    assert.deepEqual(finalJobs.filter((job) => job.kind === 'conversation').map((job) => job.conversation_id).sort(), TRAVERSAL_CHAT_IDS);
    assert.ok(finalJobs.filter((job) => job.kind === 'conversation').every((job) => job.phase === 'complete'));
    await finalStorage.runTransaction('readonly', ['chats', 'messages', 'coverage_evidence', 'outbox'], async (tx) => {
      assert.deepEqual((await tx.getAll('chats')).map((chat) => chat.chat_id).sort(), TRAVERSAL_CHAT_IDS);
      const messages = (await tx.getAll('messages')).sort((left, right) => left.message_id.localeCompare(right.message_id));
      assert.deepEqual(messages.map((message) => message.message_id), TRAVERSAL_MESSAGE_IDS);
      for (const message of messages) {
        const { last_source_seq, last_origin, ...material } = message;
        assert.deepEqual(material, expectedTraversalMessage(message.message_id));
      }
      const evidence = (await tx.getAll('coverage_evidence')).map((row) => row.evidence);
      assert.deepEqual(evidence.filter((row) => row.type === 'conversation.history_started')
        .map((row) => row.conversation_id).sort(), TRAVERSAL_CHAT_IDS);
      const rows = await tx.getAll('outbox');
      assert.equal(rows.filter((row) => row.change.type === 'message.upsert').length, 5, 'overlap does not allocate duplicate source transitions');
      assert.equal(JSON.stringify(rows).includes('msg1.'), false, 'opaque cursors never enter ingestion frames');
    });
    assert.deepEqual(cursors.filter((row) => row.conversationId === '101').map((row) => row.cursor), [null, 'msg1.101.899', 'msg1.101.898']);
    assert.equal(f.calls.reloads, 1, 'reconstruction retains the current account signing generation');
  });
}

async function releaseHarness(variant, policy = {}, { wrapStorage = (value) => value } = {}) {
  const [name, Coordinator, Outbox, storageFactory] = variant;
  const indexedDb = new FakeIndexedDb();
  const storage = wrapStorage(storageFactory(indexedDb, { databaseName: `release-case-${name}`, encryptionKey: TRAVERSAL_KEY }));
  const outbox = new Outbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
  await outbox.initialize();
  const f = createSignerReleaseFixture(); f.persistence = createAccountSigningPersistence(storage, TRAVERSAL_ACCOUNT);
  const provider = await f.createProvider();
  const configuration = traversalConfiguration(policy);
  const coordinator = new Coordinator({ outbox, signer: provider, configuration: () => configuration,
    session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1' }),
    now: () => TRAVERSAL_TIME });
  return { f, provider, coordinator, outbox, storage, configuration };
}

for (const variant of variants) {
  const [name] = variant;
  for (const size of [0, 1, 100]) {
    test(`${name}: installed canonical terminal inventory preserves ${size} exact items`, async () => {
      const h = await releaseHarness(variant, { page_size: Math.max(1, size) });
      h.f.reply = (request) => h.f.response(request.operation === 'identity' ? { id: TRAVERSAL_CREATOR }
        : { list: Array.from({ length: size }, (_, index) => ({ id: String(index + 1),
          withUser: { id: String(index + 1) }, lastMessage: null })), hasMore: false });
      await h.coordinator.wake(); h.coordinator.stop();
      const jobs = await h.outbox.historyJobs();
      const inventory = jobs.find((job) => job.kind === 'inventory');
      assert.equal(inventory.boundary, 'inventory_end');
      assert.equal(inventory.cursor, null);
      assert.equal(jobs.filter((job) => job.kind === 'conversation').length, size);
      assert.deepEqual((await h.outbox.chatIds()).sort(), Array.from({ length: size }, (_, i) => String(i + 1)).sort());
    });
  }

  for (const [status, code] of [[400, 'upstream_rejected'], [401, 'authorization_failed'],
    [403, 'authorization_failed'], [429, 'rate_limited'], [503, 'upstream_unavailable']]) {
    test(`${name}: installed HTTP ${status} preserves fixed failure facts without refresh`, async () => {
      const h = await releaseHarness(variant);
      h.f.reply = (request) => request.operation === 'identity' ? h.f.response({ id: TRAVERSAL_CREATOR })
        : h.f.response({ error: 'synthetic-upstream-private-message' }, status, { retryAfterMs: 45_000 });
      if (status === 429) await h.coordinator.wake();
      else await assert.rejects(h.coordinator.wake(), { code });
      const inventory = (await h.outbox.historyJobs()).find((job) => job.kind === 'inventory');
      assert.equal(inventory.last_error_code, code);
      assert.equal(inventory.retry_count, 1);
      assert.equal(inventory.cursor, null);
      assert.equal(h.f.calls.reloads, 1, 'the cold bootstrap is the only reload');
      assert.equal(JSON.stringify(inventory).includes('synthetic-upstream-private-message'), false);
      h.coordinator.stop();
    });
  }

  test(`${name}: installed invalid descending order fails without inventing terminal coverage`, async () => {
    const h = await releaseHarness(variant);
    const message = (id) => ({ id, fromUser: { id: '101' }, text: 'Synthetic', createdAt: TRAVERSAL_TIME });
    h.f.reply = (request) => h.f.response(request.operation === 'identity' ? { id: TRAVERSAL_CREATOR }
      : request.operation === 'conversations' ? { list: [{ id: '101', withUser: { id: '101' } }], hasMore: false }
        : { list: [message('899'), message('900')], hasMore: true });
    await h.coordinator.wake();
    await assert.rejects(h.coordinator.wake(), { code: 'invalid_response' });
    const job = (await h.outbox.historyJobs()).find((item) => item.kind === 'conversation');
    assert.equal(job.last_error_code, 'invalid_response');
    assert.equal(job.last_validation_error, 'invalid_continuation');
    assert.equal(job.cursor, null); assert.equal(job.boundary, null);
    assert.equal(h.outbox.identityState().entity_counts.messages, 0);
    h.coordinator.stop();
  });

  test(`${name}: installed valid pages cannot repeat a durable opaque message cursor`, async () => {
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(request.operation === 'identity' ? { id: TRAVERSAL_CREATOR }
      : request.operation === 'conversations' ? { list: [{ id: '101', withUser: { id: '101' } }], hasMore: false }
        : { list: [{ id: '900', fromUser: { id: '101' }, text: 'Synthetic', createdAt: TRAVERSAL_TIME }], hasMore: true });
    await h.coordinator.wake(); await h.coordinator.wake();
    const before = h.outbox.identityState().entity_counts.messages;
    await assert.rejects(h.coordinator.wake(), { code: 'cursor_repeated' });
    const job = (await h.outbox.historyJobs()).find((item) => item.kind === 'conversation');
    assert.equal(job.cursor, 'msg1.101.900'); assert.equal(job.boundary, null);
    assert.equal(job.last_error_code, 'cursor_repeated');
    assert.equal(h.outbox.identityState().entity_counts.messages, before);
    h.coordinator.stop();
  });

  test(`${name}: a persisted cursor moved to another conversation is rejected by the installed signer`, async () => {
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(request.operation === 'identity' ? { id: TRAVERSAL_CREATOR }
      : { list: [{ id: '102', withUser: { id: '102' } }], hasMore: false });
    await h.coordinator.wake();
    const job = (await h.outbox.historyJobs()).find((item) => item.kind === 'conversation');
    await h.outbox.saveHistoryJob({ ...job, cursor: 'msg1.101.900' });
    const requests = h.f.calls.reads.length;
    await assert.rejects(h.coordinator.wake(), { name: 'IntentValidationError' });
    const failed = await h.outbox.historyJob(job.job_id);
    assert.equal(failed.last_error_code, 'invalid_request');
    assert.equal(failed.cursor, 'msg1.101.900'); assert.equal(failed.boundary, null);
    assert.equal(h.f.calls.reads.length, requests, 'invalid cursor rejected before upstream dispatch');
    h.coordinator.stop();
  });
}

for (const variant of variants) {
  const [name] = variant;
  test(`${name}: the consumer deadline fences the real cold signer bootstrap and its late response`, async () => {
    const h = await releaseHarness(variant);
    const entered = deferred(); const finish = deferred();
    const timers = [];
    h.coordinator.setTimeoutImpl = (handler) => { timers.push(handler); return 1; };
    h.coordinator.clearTimeoutImpl = () => {};
    h.f.reply = async () => { entered.resolve(); await finish.promise; return h.f.response({ id: TRAVERSAL_CREATOR }); };
    const running = h.coordinator.wake();
    const rejected = assert.rejects(running, { code: 'history_run_deadline' });
    await entered.promise;
    timers[0](); await rejected;
    finish.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    await h.f.persistence.drain();
    assert.equal((await h.outbox.historyJobs()).length, 0);
    assert.equal(h.outbox.identityState().last_source_seq, 0);
    assert.equal(h.f.calls.reloads, 1);
    assert.equal(h.f.calls.reads.filter((read) => read.operation !== 'identity').length, 0);
  });

  const hostileReason = Object.defineProperty({}, 'code', {
    get() { throw new Error('Synthetic arbitrary abort reason must not be inspected'); },
  });
  for (const reason of [false, null, 'synthetic-private-cancel-reason', hostileReason]) {
    test(`${name}: arbitrary cancellation during the real signer save cannot authorize a late history commit (${String(reason)})`, async () => {
      const entered = deferred(); const finish = deferred();
      let intercept = true;
      const h = await releaseHarness(variant, {}, { wrapStorage: (storage) => ({ ...storage,
        runTransaction(mode, stores, work, controls) {
          return storage.runTransaction(mode, stores, async (tx) => {
            const result = await work(tx);
            if (intercept && mode === 'readwrite' && stores.includes('credentials')) {
              intercept = false; entered.resolve(); await finish.promise;
            }
            return result;
          }, controls);
        },
      }) });
      const running = h.coordinator.wake();
      const rejected = assert.rejects(running, (error) => error === reason);
      await entered.promise;
      h.coordinator.cancelCurrent(reason); await rejected;
      finish.resolve(); await h.f.persistence.drain();
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal((await h.outbox.historyJobs()).length, 0);
      assert.equal(h.outbox.identityState().last_source_seq, 0);
      const signingState = await h.f.persistence.load();
      assert.ok(signingState, 'an entered signing save may finish; it does not authorize captured pages');
    });
  }

  for (const stage of ['job-save', 'page-commit']) {
    test(`${name}: cancellation after ${stage} callback aborts the native history transaction`, async () => {
      let h;
      let armed = false;
      const reason = Object.assign(new Error('Synthetic history transaction cancelled'), { code: 'synthetic_cancelled' });
      h = await releaseHarness(variant, {}, { wrapStorage: (storage) => ({ ...storage,
        runTransaction(mode, stores, work, controls) {
          return storage.runTransaction(mode, stores, async (tx) => {
            const result = await work(tx);
            const match = stage === 'job-save' ? stores.length === 2 : stores.includes('outbox');
            if (armed && mode === 'readwrite' && stores.includes('history_jobs') && match) {
              armed = false; h.coordinator.cancelCurrent(reason);
            }
            return result;
          }, controls);
        },
      }) });
      armed = true;
      await assert.rejects(h.coordinator.wake(), (error) => error === reason);
      const jobs = await h.outbox.historyJobs();
      if (stage === 'job-save') assert.equal(jobs.length, 0);
      else assert.equal(jobs.find((job) => job.kind === 'inventory').phase, 'start');
      assert.equal(h.outbox.identityState().last_source_seq, 0);
    });
  }
}

for (const variant of variants) {
  test(`${variant[0]}: wall-clock deadline fences a real signer result before a delayed timer callback`, async () => {
    const h = await releaseHarness(variant);
    let clock = 0;
    let timerFired = false;
    h.coordinator.clock = () => clock;
    h.coordinator.setTimeoutImpl = () => { timerFired = false; return 1; };
    h.coordinator.clearTimeoutImpl = () => {};
    h.f.reply = (request) => {
      if (request.operation === 'identity') return h.f.response({ id: TRAVERSAL_CREATOR });
      clock = 20_001;
      return h.f.response({ list: [{ id: '101', withUser: { id: '101' } }], hasMore: false });
    };
    await assert.rejects(h.coordinator.wake(), { code: 'history_run_deadline' });
    assert.equal(timerFired, false);
    const inventory = (await h.outbox.historyJobs()).find((job) => job.kind === 'inventory');
    assert.equal(inventory.phase, 'inventory');
    assert.equal(inventory.boundary, null);
    assert.equal(inventory.retry_count, 0);
    assert.equal(h.outbox.identityState().entity_counts.chats, 0);
  });
}

for (const prefix of ['', 'read-only-']) {
  const { normalizeSignerConversation, normalizeSignerMessage } = await import(`../transport/${prefix}signer-normalization.mjs`);
  test(`${prefix || 'authoring-'}canonical normalization never repairs malformed signer scalar types`, () => {
    const context = { creatorPlatformId: TRAVERSAL_CREATOR, conversationId: '101', observedAt: TRAVERSAL_TIME };
    const message = { id: '900', chat_id: '101', sender_platform_user_id: '101', text: 'Synthetic',
      sent_at: TRAVERSAL_TIME, direction: null };
    for (const sent_at of [null, 123, 'not-a-time']) {
      assert.throws(() => normalizeSignerMessage({ ...message, sent_at }, context), { code: 'invalid_response' });
    }
    assert.throws(() => normalizeSignerMessage({ ...message, id: 900 }, context), { code: 'invalid_response' });
    const conversation = { id: '101', platform_user_id: '101', display_name: null, updated_at: null };
    for (const changed of [{ display_name: 123 }, { updated_at: 'not-a-time' }, { platform_user_id: 101 }]) {
      assert.throws(() => normalizeSignerConversation({ ...conversation, ...changed }, context), { code: 'invalid_response' });
    }
    assert.equal(normalizeSignerMessage(message, context).message.direction, 'inbound');
    assert.equal(normalizeSignerConversation(conversation, context).chat.updated_at, TRAVERSAL_TIME);
  });
}

for (const variant of variants) {
  for (const phase of ['open', 'closed']) {
    for (const incompatible of ['changed mapping', 'legacy inventory']) {
      test(`${variant[0]}: ${phase} history with ${incompatible} cannot resume or report current after reconstruction`, async () => {
        const [, Coordinator] = variant;
        const h = await releaseHarness(variant);
        h.f.reply = (request) => h.f.response(nativeTraversalBody(request));
        await h.coordinator.wake();
        if (phase === 'closed') {
          for (let wake = 0; wake < 10; wake += 1) {
            if ((await h.outbox.historyJobs()).find((job) => job.kind === 'inventory').phase === 'closed') break;
            await h.coordinator.wake();
          }
          assert.equal((await h.outbox.historyJobs()).find((job) => job.kind === 'inventory').phase, 'closed');
        }
        h.coordinator.stop();
        if (incompatible === 'changed mapping') {
          h.configuration.config_revision = 'synthetic-config-v2';
          h.configuration.history_acquisition.authorized_platform_creator_id = '9002';
        } else {
          await h.storage.runTransaction('readwrite', ['history_jobs'], async (tx) => {
            const inventory = (await tx.getAll('history_jobs')).find((job) => job.kind === 'inventory');
            delete inventory.authorized_platform_creator_id;
            await tx.put('history_jobs', inventory);
          });
        }
        const jobsBefore = await h.outbox.historyJobs();
        const stateBefore = h.outbox.identityState();
        const signingBefore = await h.f.persistence.load();
        const replacement = createSignerReleaseFixture({
          expectedIdentity: h.configuration.history_acquisition.authorized_platform_creator_id,
        });
        replacement.persistence = createAccountSigningPersistence(h.storage, TRAVERSAL_ACCOUNT);
        const provider = await replacement.createProvider();
        const coordinator = new Coordinator({ outbox: h.outbox, signer: provider,
          configuration: () => h.configuration,
          session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT,
            applied_config_revision: h.configuration.config_revision }), now: () => TRAVERSAL_TIME });
        await assert.rejects(coordinator.wake(), {
          code: incompatible === 'changed mapping' ? 'account_mismatch' : 'history_authorization_unbound',
        });
        assert.equal(replacement.calls.reads.length, 0, 'incompatible durable authorization blocks before any signer request');
        assert.equal(replacement.calls.reloads, 0);
        assert.deepEqual(await h.outbox.historyJobs(), jobsBefore, 'old cursor and generation remain retained');
        assert.deepEqual(h.outbox.identityState(), stateBefore);
        assert.deepEqual(await replacement.persistence.load(), signingBefore, 'retained signing state is never reset to adopt a mapping');
        if (incompatible === 'changed mapping') {
          await assert.rejects(provider.read({ operation: 'identity' }), { code: 'account_mismatch' });
          assert.equal(replacement.calls.reads.length, 0, 'installed signer also retains its independent expected-identity fence');
        }
        coordinator.stop();
      });
    }
  }

  test(`${variant[0]}: unbound conversation evidence cannot make a bound inventory current`, async () => {
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(request.operation === 'identity' ? { id: TRAVERSAL_CREATOR }
      : request.operation === 'conversations' ? { list: [{ id: '101', withUser: { id: '101' } }], hasMore: false }
        : { list: [], hasMore: false });
    await h.coordinator.wake(); await h.coordinator.wake(); await h.coordinator.wake();
    const jobs = await h.outbox.historyJobs();
    assert.equal(jobs.find((job) => job.kind === 'inventory').phase, 'closed');
    await h.storage.runTransaction('readwrite', ['history_jobs'], async (tx) => {
      const conversation = jobs.find((job) => job.kind === 'conversation');
      delete conversation.authorized_platform_creator_id;
      await tx.put('history_jobs', conversation);
    });
    const reads = h.f.calls.reads.length;
    await assert.rejects(h.coordinator.wake(), { code: 'history_authorization_unbound' });
    assert.equal(h.f.calls.reads.length, reads);
    h.coordinator.stop();
  });

  test(`${variant[0]}: new consent creates a newly verified generation without adopting the prior cursor`, async () => {
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(nativeTraversalBody(request));
    await h.coordinator.wake();
    const previous = (await h.outbox.historyJobs()).find((job) => job.kind === 'inventory');
    assert.equal(previous.cursor, '2');
    const reads = h.f.calls.reads.length;
    h.configuration.history_acquisition.consent_revision = 'synthetic-consent-v2';
    await h.coordinator.wake();
    const newReads = h.f.calls.reads.slice(reads);
    assert.deepEqual(newReads.map((request) => request.operation), ['identity', 'conversations']);
    assert.equal(new URL(newReads[1].url).searchParams.get('offset'), null);
    const inventories = (await h.outbox.historyJobs()).filter((job) => job.kind === 'inventory');
    assert.equal(inventories.length, 2);
    assert.deepEqual(inventories.find((job) => job.job_id === previous.job_id), previous);
    const current = inventories.find((job) => job.job_id !== previous.job_id);
    assert.equal(current.authorization_revision, 'synthetic-consent-v2');
    assert.equal(current.authorized_platform_creator_id, TRAVERSAL_CREATOR);
    h.coordinator.stop();
  });

  test(`${variant[0]}: overlapping inventory cannot overwrite a conversation's durable platform binding`, async () => {
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(nativeTraversalBody(request));
    await h.coordinator.wake();
    const jobs = await h.outbox.historyJobs();
    const inventory = jobs.find((job) => job.kind === 'inventory');
    const conversation = jobs.find((job) => job.kind === 'conversation');
    await assert.rejects(h.outbox.commitPage({ jobId: inventory.job_id,
      expectedAccountEpoch: inventory.account_epoch, expectedLeaseToken: inventory.lease_token,
      nextCursor: '4', spawnJobs: [{ ...conversation, authorized_platform_creator_id: '9002' }],
    }), { code: 'history_job_conflict' });
    assert.deepEqual(await h.outbox.historyJobs(), jobs, 'a conflicting spawn rolls back its enclosing cursor transaction');
    h.coordinator.stop();
  });

  test(`${variant[0]}: a new account epoch retains old jobs and verifies a fresh generation`, async () => {
    const [, Coordinator, Outbox] = variant;
    const h = await releaseHarness(variant);
    h.f.reply = (request) => h.f.response(nativeTraversalBody(request));
    await h.coordinator.wake(); h.coordinator.stop();
    const previous = (await h.outbox.historyJobs()).find((job) => job.kind === 'inventory');
    await h.outbox.invalidateAccountEpoch();
    const outbox = new Outbox({ storage: h.storage, creatorAccountId: TRAVERSAL_ACCOUNT });
    await outbox.initialize();
    const provider = await h.f.createProvider();
    const reads = h.f.calls.reads.length;
    const coordinator = new Coordinator({ outbox, signer: provider, configuration: () => h.configuration,
      session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: h.configuration.config_revision }),
      now: () => TRAVERSAL_TIME });
    await coordinator.wake();
    const newReads = h.f.calls.reads.slice(reads);
    assert.deepEqual(newReads.map((request) => request.operation), ['identity', 'conversations']);
    assert.equal(new URL(newReads[1].url).searchParams.get('offset'), null);
    const inventories = (await outbox.historyJobs()).filter((job) => job.kind === 'inventory');
    assert.equal(inventories.length, 2);
    assert.deepEqual(inventories.find((job) => job.job_id === previous.job_id), previous);
    assert.equal(inventories.find((job) => job.job_id !== previous.job_id).account_epoch, previous.account_epoch + 1);
    coordinator.stop();
  });
}

for (const variant of variants) {
  test(`${variant[0]}: unknown thrown values cannot persist private error codes`, async () => {
    const h = await releaseHarness(variant);
    const hostile = Object.defineProperty({}, 'code', { get() { throw new Error('synthetic-private-getter'); } });
    h.coordinator.signer = { async read(request) {
      if (request.operation === 'identity') return h.provider.read(request);
      throw hostile;
    } };
    await assert.rejects(h.coordinator.wake(), (error) => error === hostile);
    const inventory = (await h.outbox.historyJobs()).find((job) => job.kind === 'inventory');
    assert.equal(inventory.last_error_code, 'signing_failed');
    assert.equal(inventory.last_validation_error, null);
    assert.equal(JSON.stringify(inventory).includes('synthetic-private-getter'), false);
    h.coordinator.stop();
  });
}
