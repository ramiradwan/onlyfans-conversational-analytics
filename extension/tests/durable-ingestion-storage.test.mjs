import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DurableIngestOutbox,
  INGESTION_META_KEY,
  INGESTION_STORES,
} from '../transport/durable-outbox.mjs';
import { createChromeAdapter } from '../transport/chrome-adapter.mjs';
import {
  InMemoryIngestionStorage,
  InMemoryLegacyIngestionStorage,
} from './in-memory-ingestion-storage.mjs';

const chatChange = (chatId, displayName = chatId) => ({
  type: 'chat.upsert',
  chat: {
    chat_id: chatId,
    platform_user_id: `fan-${chatId}`,
    display_name: displayName,
    updated_at: '2026-07-18T10:00:00Z',
  },
});

const messageChange = (messageId, chatId) => ({
  type: 'message.upsert',
  message: {
    message_id: messageId,
    chat_id: chatId,
    sender_platform_user_id: `fan-${chatId}`,
    text: messageId,
    sent_at: '2026-07-18T10:01:00Z',
    direction: 'inbound',
  },
});

const eventId = (sequence) => `50000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`;

test('invariant 1: each enqueue assigns a monotonic contiguous source_seq', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();

  const first = await outbox.enqueue(chatChange('chat-1'), eventId(1));
  const second = await outbox.enqueue(messageChange('message-1', 'chat-1'), eventId(2));
  const third = await outbox.enqueue(chatChange('chat-2'), eventId(3));

  assert.deepEqual([first.source_seq, second.source_seq, third.source_seq], [1, 2, 3]);
  assert.deepEqual((await outbox.entries()).map((item) => item.source_seq), [1, 2, 3]);
});

test('invariant 2: mutation store groups are atomic and a mid-transaction failure rolls back', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();
  const before = outbox.snapshotState();

  storage.failNextWriteTransactionAfter(1);
  await assert.rejects(
    outbox.enqueue(chatChange('chat-1'), eventId(1)),
    /Injected transaction failure/,
  );
  assert.deepEqual(outbox.snapshotState(), before);

  const failed = storage.transactions.at(-1);
  assert.equal(failed.committed, false);
  assert.deepEqual(failed.storeNames, [
    INGESTION_STORES.meta,
    INGESTION_STORES.outbox,
    INGESTION_STORES.chats,
    INGESTION_STORES.messages,
  ]);

  const restarted = new DurableIngestOutbox({ storage });
  await restarted.initialize();
  assert.deepEqual(restarted.snapshotState(), before);
  assert.equal((await restarted.enqueue(chatChange('chat-1'), eventId(1))).source_seq, 1);

  let transactionCount = storage.transactions.length;
  const snapshot = await restarted.createSnapshot('snapshot-1');
  assert.equal(storage.transactions.length, transactionCount + 1);
  assert.deepEqual(storage.transactions.at(-1).storeNames, [
    INGESTION_STORES.meta,
    INGESTION_STORES.chats,
    INGESTION_STORES.messages,
    INGESTION_STORES.snapshot,
  ]);

  transactionCount = storage.transactions.length;
  await restarted.acknowledge(1, snapshot.snapshot_id);
  assert.equal(storage.transactions.length, transactionCount + 1);
  assert.deepEqual(storage.transactions.at(-1).storeNames, [
    INGESTION_STORES.meta,
    INGESTION_STORES.outbox,
    INGESTION_STORES.snapshot,
  ]);
  assert.throws(
    () => storage.lastTransactionHandle.get(INGESTION_STORES.meta, INGESTION_META_KEY),
    /outside the transaction/,
  );
});

test('invariant 3: initialization rejects version, contiguity, and sequence-bound corruption', async (t) => {
  const cases = [
    {
      name: 'unsupported version',
      meta: { version: 2, last_source_seq: 0, acknowledged_source_seq: 0, pending_snapshot: null },
      outbox: [],
      error: /Stored durable ingestion state is invalid/,
    },
    {
      name: 'outbox gap',
      meta: { version: 1, last_source_seq: 2, acknowledged_source_seq: 0, pending_snapshot: null },
      outbox: [{ event_id: eventId(2), source_seq: 2, change: chatChange('chat-2') }],
      error: /not contiguous and ordered/,
    },
    {
      name: 'source sequence behind outbox',
      meta: { version: 1, last_source_seq: 0, acknowledged_source_seq: 0, pending_snapshot: null },
      outbox: [{ event_id: eventId(1), source_seq: 1, change: chatChange('chat-1') }],
      error: /behind the durable outbox/,
    },
  ];

  for (const corruption of cases) {
    await t.test(corruption.name, async () => {
      const storage = new InMemoryIngestionStorage();
      await storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.meta, INGESTION_STORES.outbox],
        async (tx) => {
          await tx.put(INGESTION_STORES.meta, corruption.meta, INGESTION_META_KEY);
          for (const item of corruption.outbox) await tx.put(INGESTION_STORES.outbox, item);
        },
      );
      const outbox = new DurableIngestOutbox({ storage });
      await assert.rejects(outbox.initialize(), corruption.error);
    });
  }
});

test('invariant 4: mirrors are keyed and snapshot chronology uses deterministic identifier sorts', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();
  await outbox.enqueue(chatChange('chat-b'), eventId(1));
  await outbox.enqueue(chatChange('chat-a'), eventId(2));
  await outbox.enqueue(chatChange('chat-b', 'updated'), eventId(3));
  await outbox.enqueue(messageChange('message-b', 'chat-b'), eventId(4));
  await outbox.enqueue(messageChange('message-a', 'chat-a'), eventId(5));

  const snapshot = await outbox.createSnapshot('snapshot-sorted');
  assert.deepEqual(snapshot.chats.map((chat) => chat.chat_id), ['chat-a', 'chat-b']);
  assert.deepEqual(snapshot.messages.map((message) => message.message_id), [
    'message-a',
    'message-b',
  ]);
  assert.equal(snapshot.chats.find((chat) => chat.chat_id === 'chat-b').display_name, 'updated');

  await storage.runTransaction(
    'readonly',
    [INGESTION_STORES.chats, INGESTION_STORES.messages],
    async (tx) => {
      assert.deepEqual(await tx.getAllKeys(INGESTION_STORES.chats), ['chat-a', 'chat-b']);
      assert.deepEqual(await tx.getAllKeys(INGESTION_STORES.messages), [
        'message-a',
        'message-b',
      ]);
    },
  );
});

test('invariant 5: a pending snapshot remains byte-for-byte stable across retry and restart', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();
  await outbox.enqueue(chatChange('chat-1'), eventId(1));
  await outbox.enqueue(messageChange('message-1', 'chat-1'), eventId(2));
  const original = await outbox.createSnapshot('snapshot-stable');

  await outbox.enqueue(chatChange('chat-2'), eventId(3));
  assert.deepEqual(await outbox.createSnapshot('snapshot-ignored'), original);

  const restarted = new DurableIngestOutbox({ storage });
  await restarted.initialize();
  assert.deepEqual(await restarted.createSnapshot('snapshot-also-ignored'), original);
  await restarted.acknowledge(2, 'wrong-snapshot');
  assert.deepEqual(await restarted.createSnapshot('snapshot-still-ignored'), original);

  await restarted.acknowledge(2, original.snapshot_id);
  const replacement = await restarted.createSnapshot('snapshot-replacement');
  assert.equal(replacement.snapshot_id, 'snapshot-replacement');
  assert.equal(replacement.through_seq, 3);
  assert.deepEqual(replacement.chats.map((chat) => chat.chat_id), ['chat-1', 'chat-2']);
});

test('invariant 6: enqueue writes only meta, one outbox item, and the affected mirror key', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();
  for (let index = 1; index <= 25; index += 1) {
    await outbox.enqueue(chatChange(`chat-${index}`), eventId(index));
  }
  storage.transactions.length = 0;

  await outbox.enqueue(chatChange('chat-13', 'one-record-update'), eventId(26));

  assert.equal(storage.transactions.length, 1);
  const writes = storage.transactions[0].writes;
  assert.deepEqual(
    writes.map(({ operation, store, key }) => [operation, store, key]),
    [
      ['put', INGESTION_STORES.meta, INGESTION_META_KEY],
      ['put', INGESTION_STORES.outbox, 26],
      ['put', INGESTION_STORES.chats, 'chat-13'],
    ],
  );
  assert.equal('chats' in writes[0].value, false);
  assert.equal('messages' in writes[0].value, false);
  assert.equal('outbox' in writes[0].value, false);
});

test('legacy version-1 state imports once, deletes the old record, and validates on restart', async () => {
  const legacyState = {
    version: 1,
    last_source_seq: 2,
    acknowledged_source_seq: 0,
    outbox: [
      { event_id: eventId(1), source_seq: 1, change: chatChange('chat-1') },
      { event_id: eventId(2), source_seq: 2, change: messageChange('message-1', 'chat-1') },
    ],
    chats: [chatChange('chat-1').chat],
    messages: [messageChange('message-1', 'chat-1').message],
    pending_snapshot: {
      snapshot_id: 'snapshot-imported',
      through_seq: 2,
      chats: [chatChange('chat-1').chat],
      messages: [messageChange('message-1', 'chat-1').message],
    },
  };
  const storage = new InMemoryIngestionStorage();
  const legacyStorage = new InMemoryLegacyIngestionStorage(legacyState);
  const outbox = new DurableIngestOutbox({ storage, legacyStorage });

  assert.deepEqual(await outbox.initialize(), legacyState);
  assert.equal(legacyStorage.loadCount, 1);
  assert.equal(legacyStorage.deleteCount, 1);
  assert.equal(legacyStorage.value, null);

  const restarted = new DurableIngestOutbox({ storage, legacyStorage });
  assert.deepEqual(await restarted.initialize(), legacyState);
  assert.equal(legacyStorage.loadCount, 2);
  assert.equal(legacyStorage.deleteCount, 1);
});

test('the Chrome seam reads and removes only the legacy ingestion record', async () => {
  const legacyState = {
    version: 1,
    last_source_seq: 0,
    acknowledged_source_seq: 0,
    outbox: [],
    chats: [],
    messages: [],
    pending_snapshot: null,
  };
  const values = { durable_ingestion_v1: legacyState, durable_command_results_v1: 'untouched' };
  const chromeAdapter = createChromeAdapter({
    runtime: {},
    storage: {
      local: {
        get(keys, callback) {
          callback(Object.fromEntries(keys.filter((key) => key in values).map(
            (key) => [key, structuredClone(values[key])],
          )));
        },
        set(update, callback) {
          Object.assign(values, structuredClone(update));
          callback?.();
        },
        remove(keys, callback) {
          for (const key of keys) delete values[key];
          callback?.();
        },
      },
    },
  });

  assert.deepEqual(await chromeAdapter.loadLegacyIngestionState(), legacyState);
  await chromeAdapter.deleteLegacyIngestionState();
  assert.equal('durable_ingestion_v1' in values, false);
  assert.equal(values.durable_command_results_v1, 'untouched');
});
