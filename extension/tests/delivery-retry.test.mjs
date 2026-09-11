import assert from 'node:assert/strict';
import test from 'node:test';

import { CaptureDeliveryQueue } from '../capture/delivery-queue.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { DeliveryAcceptance } from '../transport/delivery-acceptance-core.mjs';

const DELIVERY = Object.freeze({
  type: 'ofca.capture.delivery',
  version: 1,
  delivery_id: '90000000-0000-4000-8000-000000000001',
  created_at_ms: 1_000,
  consent_epoch: '90000000-0000-4000-8000-000000000004',
  observation: {
    event_type: 'chat.observed',
    observed_at: '2030-01-08T12:00:00Z',
    source_path: '/api2/v2/chats',
    creator_platform_user_id: 'creator-a',
    context_chat_id: null,
    page_epoch: '90000000-0000-4000-8000-000000000002',
    record: {
      chat_id: 'chat-a',
      platform_user_id: 'fan-a',
      display_name: 'Fan A',
      updated_at: '2030-01-08T12:00:00Z',
    },
  },
});
const CHANGE = Object.freeze({
  type: 'chat.upsert',
  chat: {
    chat_id: 'chat-a',
    record_kind: 'full',
    platform_user_id: 'fan-a',
    display_name: 'Fan A',
    updated_at: '2030-01-08T12:00:00.000Z',
  },
});

for (const name of ['durable-outbox.mjs', 'read-only-durable-outbox.mjs']) {
  const { DurableIngestOutbox } = await import(`../transport/${name}`);
  function harness(storage = new InMemoryIngestionStorage(), now = () => 2_000) {
    const outbox = new DurableIngestOutbox({ storage, creatorAccountId: 'companion-a' });
    let flushCount = 0;
    const acceptance = new DeliveryAcceptance({ outbox, now,
      transport: { async flushOutbox() { flushCount += 1; } },
    });
    return { outbox, storage, acceptance, flushCount: () => flushCount };
  }

  test(`${name}: receipt and material commit atomically before ACK`, async () => {
    const h = harness();
    const first = await h.acceptance.accept({ delivery: DELIVERY, change: CHANGE });
    assert.equal(first.accepted, true);
    assert.equal(first.source_seq, 1);
    const transaction = h.storage.transactions.find((tx) => tx.storeNames.includes('delivery_receipts'));
    assert.equal(transaction.committed, true);
    assert.deepEqual(new Set(transaction.writes.map((write) => write.store)),
      new Set(['chats', 'outbox', 'meta', 'delivery_receipts']));
    assert.equal(h.flushCount(), 1);
    await h.outbox.enqueue({ ...CHANGE, chat: { ...CHANGE.chat, display_name: 'Updated',
      updated_at: '2030-01-08T13:00:00.000Z' } });
    await h.outbox.acknowledge(2);
    const restarted = harness(h.storage);
    assert.deepEqual(await restarted.acceptance.accept({ delivery: DELIVERY, change: CHANGE }), first);
    assert.equal(restarted.outbox.meta.last_source_seq, 2);
    assert.equal(h.storage.stores.get('chats').get('chat-a').display_name, 'Updated');
    assert.equal(h.storage.stores.get('delivery_receipts').size, 1);
  });

  test(`${name}: interruption at every acceptance write rolls back the entire delivery`, async () => {
    for (let write = 1; write <= 4; write += 1) {
      const h = harness();
      await h.outbox.initialize();
      h.storage.failNextWriteTransactionAfter(write);
      await assert.rejects(h.acceptance.accept({ delivery: DELIVERY, change: CHANGE }));
      for (const store of ['chats', 'outbox', 'delivery_receipts']) assert.equal(h.storage.stores.get(store).size, 0);
      const restarted = harness(h.storage);
      assert.equal((await restarted.acceptance.accept({ delivery: DELIVERY, change: CHANGE })).source_seq, 1);
    }
  });

  test(`${name}: conflicting IDs and expired deliveries never write`, async () => {
    const h = harness();
    await h.acceptance.accept({ delivery: DELIVERY, change: CHANGE });
    await assert.rejects(h.acceptance.accept({ delivery: DELIVERY,
      change: { ...CHANGE, chat: { ...CHANGE.chat, display_name: 'Different' } },
    }), { code: 'delivery_id_conflict' });
    const expired = harness(h.storage, () => DELIVERY.created_at_ms + 120_000);
    await assert.rejects(expired.acceptance.accept({ delivery: DELIVERY, change: CHANGE }), { code: 'delivery_expired' });
    assert.equal(h.outbox.meta.last_source_seq, 1);
  });

  test(`${name}: invalidation at the receipt write rolls back all material`, async () => {
    const h = harness();
    await h.outbox.initialize();
    const run = h.storage.runTransaction.bind(h.storage);
    let current = true;
    h.storage.runTransaction = (mode, stores, work) => run(mode, stores, (tx) => work({ ...tx,
      put(store, value, key) {
        tx.put(store, value, key);
        if (store === 'delivery_receipts') current = false;
      },
    }));
    await assert.rejects(h.acceptance.accept({ delivery: DELIVERY, change: CHANGE,
      guard: { assertCurrent() { if (!current) throw Object.assign(new Error('stale_capture_context'),
        { code: 'stale_capture_context' }); } },
    }), { code: 'stale_capture_context' });
    assert.equal(h.storage.stores.get('chats').size, 0);
    assert.equal(h.storage.stores.get('delivery_receipts').size, 0);
  });

  test(`${name}: receipt pruning advances across complete batches`, async () => {
    const h = harness();
    await h.outbox.initialize();
    for (let i = 0; i < 205; i += 1) h.storage.stores.get('delivery_receipts').set(`expired-${i}`, {
      delivery_id: `expired-${i}`, schema: 'ofca-delivery-receipt/v1', expires_at: 1,
    });
    for (let i = 0; i < 3; i += 1) await h.acceptance.accept({
      delivery: { ...DELIVERY, delivery_id: `90000000-0000-4000-8000-${String(i + 10).padStart(12, '0')}` }, change: CHANGE,
    });
    assert.equal(h.storage.stores.get('delivery_receipts').size, 3);
  });
}

test('content delivery queue retries transient failures with the same delivery id', async () => {
  const ids = [];
  let attempts = 0;
  const queue = new CaptureDeliveryQueue({
    now: () => 1_001,
    random: () => 0,
    send: async (delivery) => {
      ids.push(delivery.delivery_id);
      attempts += 1;
      return attempts < 3
        ? { ok: false, retryable: true }
        : { ok: true };
    },
  });

  await queue.enqueue(DELIVERY);
  assert.equal(attempts, 3);
  assert.deepEqual(ids, [DELIVERY.delivery_id, DELIVERY.delivery_id, DELIVERY.delivery_id]);
});

test('content delivery queue rejects bounded-memory overflow', () => {
  const queue = new CaptureDeliveryQueue({
    maxEntries: 1,
    now: () => 1_001,
    send: () => new Promise(() => {}),
  });
  void queue.enqueue(DELIVERY).catch(() => undefined);
  assert.throws(
    () => queue.enqueue({ ...DELIVERY, delivery_id: '90000000-0000-4000-8000-000000000003' }),
    { code: 'delivery_queue_full' },
  );
  queue.close();
});

test('permanent rejection releases one item and later deliveries still succeed', async () => {
  const queue = new CaptureDeliveryQueue({ now: () => 1_001,
    send: async (delivery) => delivery.delivery_id === DELIVERY.delivery_id
      ? { ok: false, retryable: false, code: 'account_mismatch' } : { ok: true },
  });
  const rejected = assert.rejects(queue.enqueue(DELIVERY), { code: 'account_mismatch' });
  const accepted = queue.enqueue({ ...DELIVERY, delivery_id: '90000000-0000-4000-8000-000000000005' });
  await Promise.all([rejected, accepted]);
  assert.equal(queue.bytes, 0);
  assert.equal(queue.entries.length, 0);
});

test('stalled sends are bounded and stop releases all queued references', async () => {
  let now = 1_001;
  const queue = new CaptureDeliveryQueue({ now: () => now, attemptTimeoutMs: 5,
    send: async () => { now = 121_001; return new Promise(() => {}); },
  });
  await assert.rejects(queue.enqueue(DELIVERY), { code: 'delivery_expired' });
  assert.equal(queue.bytes, 0);
  now = 1_001;
  const stopped = assert.rejects(queue.enqueue(DELIVERY), { code: 'capture_stopped' });
  queue.close();
  await stopped;
  assert.equal(queue.bytes, 0);
  assert.throws(() => queue.enqueue(DELIVERY), { code: 'capture_stopped' });
});
