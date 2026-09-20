import assert from 'node:assert/strict';
import test from 'node:test';
import { DurableIngestOutbox, INGESTION_STORES } from '../transport/durable-outbox.mjs';
import { DurableIngestOutbox as ReadOnlyOutbox } from '../transport/read-only-durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { HistoryAcquisitionCoordinator as ReadOnlyCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

let sequence = 0;
const id = () => `10000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}`;
const account = 'creator-account-1';
const stamp = '2026-07-19T08:00:00.000Z';
const chat = (overrides = {}) => ({ type: 'chat.upsert', chat: {
  chat_id: 'chat-1', record_kind: 'full', platform_user_id: 'fan-1',
  display_name: 'Legacy label', updated_at: stamp, ...overrides,
} });
const renamed = () => chat({ display_name: 'Current profile label' });
const variants = [
  ['authoring', DurableIngestOutbox, HistoryAcquisitionCoordinator],
  ['read-only', ReadOnlyOutbox, ReadOnlyCoordinator],
];

for (const [name, Outbox, Coordinator] of variants) {
  async function fixture(kind = 'inventory') {
    const storage = new InMemoryIngestionStorage();
    const make = () => new Outbox({ storage, creatorAccountId: account, idFactory: id });
    const oldWorker = make();
    await oldWorker.initialize();
    await oldWorker.enqueue(chat());
    // Reopen the same account state: old labels must not require a data reset.
    const outbox = make();
    const state = await outbox.initialize();
    const jobId = id();
    await outbox.saveHistoryJob({ job_id: jobId, kind, account_epoch: state.account_epoch,
      lease_token: 'lease', cursor: null, committed_pages: 0 });
    const commit = (changes = [renamed()], options = {}) => outbox.commitPage({
      jobId, expectedAccountEpoch: state.account_epoch, expectedLeaseToken: 'lease',
      changes, evidence: [{ type: 'inventory.member', generation_id: jobId, conversation_id: 'chat-1' }],
      nextCursor: 'next-page', ...options,
    });
    return { storage, outbox, state, jobId, commit };
  }

  test(`${name}: old label survives while inventory membership commits after worker restart`, async () => {
    const h = await fixture();
    await h.commit();
    assert.equal(h.storage.stores.get(INGESTION_STORES.chats).get('chat-1').display_name, 'Legacy label');
    const entries = await h.outbox.entries();
    assert.deepEqual(entries.map((entry) => entry.change.type), ['chat.upsert', 'coverage.observed']);
    assert.equal((await h.outbox.historyJobs())[0].cursor, 'next-page');
    assert.equal(h.outbox.identityState().last_source_seq, 2);
    // The exception belongs to inventory acquisition, never ordinary ingestion.
    await assert.rejects(h.outbox.enqueue(renamed()), { code: 'material_conflict' });
  });

  test(`${name}: identity conflicts and tombstones still roll back the entire inventory page`, async () => {
    for (const tombstone of [false, true]) {
      const h = await fixture();
      if (tombstone) await h.outbox.enqueue({ type: 'chat.delete', chat_id: 'chat-1' });
      const before = await h.outbox.entries();
      const changes = [chat({ chat_id: 'chat-2' }), tombstone ? renamed() : chat({ platform_user_id: 'another-fan' })];
      await assert.rejects(h.commit(changes), { code: tombstone ? 'tombstone_revive' : 'identity_conflict' });
      assert.deepEqual(await h.outbox.entries(), before);
      assert.equal(h.storage.stores.get(INGESTION_STORES.chats).has('chat-2'), false);
      assert.equal((await h.outbox.historyJobs())[0].cursor, null);
    }
  });

  test(`${name}: version progression, non-inventory conflicts and stale leases keep existing rules`, async () => {
    const h = await fixture();
    await h.commit([chat({ display_name: 'Newer label', updated_at: '2026-07-20T08:00:00.000Z' })]);
    assert.equal(h.storage.stores.get(INGESTION_STORES.chats).get('chat-1').display_name, 'Newer label');
    await assert.rejects(h.commit([], { expectedLeaseToken: 'stale' }), /lease is stale/);
    const other = await fixture('conversation');
    await assert.rejects(other.commit(), { code: 'material_conflict' });
    assert.equal((await other.outbox.historyJobs())[0].cursor, null);
  });

  test(`${name}: failed inventory transaction leaves old label, evidence and cursor intact`, async () => {
    const h = await fixture();
    const before = await h.outbox.entries();
    h.storage.failNextWriteTransactionAfter(2);
    await assert.rejects(h.commit(), /Injected transaction failure/);
    assert.deepEqual(await h.outbox.entries(), before);
    assert.equal((await h.outbox.historyJobs())[0].cursor, null);
    assert.equal(h.storage.stores.get(INGESTION_STORES.chats).get('chat-1').display_name, 'Legacy label');
    await h.commit();
    assert.equal((await h.outbox.historyJobs())[0].cursor, 'next-page');
  });

  test(`${name}: history reaches messages and closes with all boundary evidence despite an old label`, async () => {
    const storage = new InMemoryIngestionStorage();
    const old = new Outbox({ storage, creatorAccountId: account, idFactory: id });
    const oldState = await old.initialize();
    await old.enqueue(chat());
    const failedGeneration = id();
    await old.saveHistoryJob({
      job_id: `${failedGeneration}:inventory`, generation_id: failedGeneration, kind: 'inventory',
      phase: 'closed', as_of: '2026-07-19T09:00:00.000Z', cursor: null, boundary: null,
      committed_pages: 0, retry_count: 4, last_error_code: 'material_conflict',
      account_epoch: oldState.account_epoch, lease_token: 'previous-worker',
      creator_account_id: account, authorization_revision: 'consent-1',
      authorized_platform_creator_id: 'creator-platform-1', recent_window_days: 30,
    });
    const outbox = new Outbox({ storage, creatorAccountId: account, idFactory: id });
    const state = await outbox.initialize();
    const calls = [];
    const coordinator = new Coordinator({
      outbox, idFactory: id, now: () => '2026-07-20T09:00:00.000Z', delay: async () => {},
      configuration: () => ({ creator_account_id: account, config_revision: 'config-1', history_acquisition: {
        enabled: true, consent_revision: 'consent-1', authorized_platform_creator_id: 'creator-platform-1',
        recent_window_days: 30, page_size: 50, pages_per_wake: 10, request_interval_ms: 0, retry_limit: 2,
      } }),
      session: () => ({ creator_account_id: account, applied_config_revision: 'config-1', account_epoch: state.account_epoch }),
      signer: { async read(request) {
        calls.push(request.operation);
        if (request.operation === 'identity') return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
        if (request.operation === 'conversations') return { operation: request.operation, success: true, data: {
          items: [{ id: 'chat-1', platform_user_id: 'fan-1', display_name: 'Current profile label', updated_at: stamp }],
          continuation: null, boundary: 'inventory_end',
        } };
        return { operation: request.operation, success: true, data: {
          items: [{ id: 'message-1', chat_id: 'chat-1', sender_platform_user_id: 'fan-1',
            text: 'Synthetic history message', sent_at: stamp, direction: 'inbound' }],
          continuation: null, boundary: 'history_start',
        } };
      } },
    });
    await coordinator.wake();
    assert.deepEqual(calls, ['identity', 'conversations', 'message-page']);
    const jobs = await outbox.historyJobs();
    const inventory = jobs.find((job) => job.kind === 'inventory' && job.generation_id !== failedGeneration);
    assert.equal(inventory.phase, 'closed');
    assert.equal(inventory.boundary, 'inventory_end');
    assert.equal(jobs.find((job) => job.generation_id === failedGeneration).last_error_code, 'material_conflict');
    assert.equal(jobs.find((job) => job.kind === 'conversation').phase, 'complete');
    assert.equal(outbox.identityState().entity_counts.messages, 1);
    const entries = await outbox.entries();
    assert.equal(entries.filter((entry) => entry.change.type === 'chat.upsert').length, 1);
    assert.deepEqual(new Set(entries.filter((entry) => entry.change.type === 'coverage.observed').map((entry) => entry.change.evidence.type)), new Set([
      'generation.started', 'inventory.member', 'inventory.ended',
      'conversation.head_reconciled', 'conversation.history_started', 'generation.closed',
    ]));
    assert.equal(coordinator.historyErrorCode(), null);
  });
}
