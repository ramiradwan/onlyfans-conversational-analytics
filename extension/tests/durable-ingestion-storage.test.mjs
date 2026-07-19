import assert from 'node:assert/strict';
import test from 'node:test';

import {
  COVERAGE_SOURCE_SEQUENCE_INDEX,
  DurableIngestOutbox,
  INGESTION_STORES,
} from '../transport/durable-outbox.mjs';
import {
  INGESTION_DATABASE_VERSION,
  accountDatabaseName,
  createIndexedDbIngestionStorage,
} from '../transport/indexeddb-ingestion-storage.mjs';
import { isAgentToBrainMessage } from '../protocol/index.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const ACCOUNT_A = 'creator-account-a';
const ACCOUNT_B = 'creator-account-b';
let sequence = 0;
const id = () => `50000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}`;
const chat = {
  type: 'chat.upsert',
  chat: {
    chat_id: 'chat-1',
    record_kind: 'full',
    platform_user_id: 'fan-1',
    display_name: 'Alex',
    updated_at: '2026-07-19T08:00:00Z',
  },
};

function storage(indexedDb, creatorAccountId) {
  return createIndexedDbIngestionStorage(indexedDb, { creatorAccountId });
}

test('IndexedDB schema version is independent and account database names are stable hashes', async () => {
  assert.equal(INGESTION_DATABASE_VERSION, 4);
  const first = await accountDatabaseName(ACCOUNT_A);
  const repeated = await accountDatabaseName(ACCOUNT_A);
  const other = await accountDatabaseName(ACCOUNT_B);
  assert.equal(first, repeated);
  assert.notEqual(first, other);
  assert.equal(first.includes(ACCOUNT_A), false);
});

test('IndexedDB v4 upgrades coverage indexes and adds account credential storage', async () => {
  const indexedDb = new FakeIndexedDb();
  const databaseName = 'coverage-index-upgrade';
  await new Promise((resolve, reject) => {
    const request = indexedDb.open(databaseName, 2);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(
        INGESTION_STORES.coverageEvidence,
        { keyPath: 'evidence_key' },
      );
    };
    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve();
  });
  const durable = new DurableIngestOutbox({
    storage: createIndexedDbIngestionStorage(indexedDb, {
      creatorAccountId: ACCOUNT_A,
      databaseName,
    }),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  await durable.initialize();

  assert.equal(
    indexedDb.databases
      .get(databaseName)
      .stores
      .get(INGESTION_STORES.coverageEvidence)
      .indexes
      .get(COVERAGE_SOURCE_SEQUENCE_INDEX),
    'last_source_seq',
  );
  assert.equal(
    indexedDb.databases.get(databaseName).stores.has(INGESTION_STORES.credentials),
    true,
  );
});

test('a worker restart reconstructs the account stream, checkpoint, entities, and outbox', async () => {
  const indexedDb = new FakeIndexedDb();
  const first = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  const identity = await first.initialize();
  const item = await first.enqueue(chat, id(), 'passive');
  assert.equal(item.source_seq, 1);

  const restarted = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  const restartedIdentity = await restarted.initialize();
  assert.equal(restartedIdentity.agent_stream_id, identity.agent_stream_id);
  assert.equal(restartedIdentity.last_source_seq, 1);
  assert.deepEqual(await restarted.entries(), [item]);
});

test('unknown message deletes retain their parent through restart and snapshot repair', async () => {
  const indexedDb = new FakeIndexedDb();
  const first = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  await first.initialize();
  const deletion = {
    type: 'message.delete',
    message_id: 'message-unknown',
    chat_id: 'chat-parent',
  };
  const item = await first.enqueue(deletion, id(), 'passive');

  const restarted = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  await restarted.initialize();
  await assert.rejects(
    restarted.enqueue({ ...deletion, chat_id: 'chat-conflict' }, id(), 'signer'),
    (error) => error?.code === 'identity_conflict',
  );
  assert.equal(restarted.identityState().last_source_seq, 1);
  assert.deepEqual(await restarted.entries(), [item]);

  const manifest = await restarted.prepareSnapshot(id());
  assert.equal(manifest.record_counts.messages, 1);
  const frame = await restarted.snapshotChunkFrame(0);
  assert.equal(frame.entity_kind, 'message');
  assert.deepEqual(frame.records, [{
    tombstone: true,
    message_id: deletion.message_id,
    chat_id: deletion.chat_id,
  }]);
  assert.equal(isAgentToBrainMessage({
    type: 'ingest.snapshot',
    protocol_version: '2',
    message_id: id(),
    payload: {
      connection_id: id(),
      fencing_token: 'fence-1',
      creator_account_id: ACCOUNT_A,
      agent_installation_id: id(),
      agent_stream_id: restarted.identityState().agent_stream_id,
      ...frame,
    },
  }), true);
});

test('overlapping platform IDs remain isolated in separate account databases', async () => {
  const indexedDb = new FakeIndexedDb();
  const a = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  const b = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_B),
    creatorAccountId: ACCOUNT_B,
    idFactory: id,
  });
  await Promise.all([a.initialize(), b.initialize()]);
  await a.enqueue(chat, id(), 'passive');
  await b.enqueue({
    ...chat,
    chat: { ...chat.chat, display_name: 'Different account' },
  }, id(), 'signer');
  assert.equal((await a.entries())[0].change.chat.display_name, 'Alex');
  assert.equal((await b.entries())[0].change.chat.display_name, 'Different account');
  assert.equal(indexedDb.databases.size, 2);
});

test('applied configuration and command results are account-partitioned in IndexedDB', async () => {
  const indexedDb = new FakeIndexedDb();
  const durable = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  await durable.initialize();
  const config = { config_revision: 'config-2', creator_account_id: ACCOUNT_A };
  const commands = { version: 1, records: [] };
  await durable.saveAppliedConfig(config);
  await durable.saveCommandState(commands);

  const restarted = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  const state = await restarted.initialize();
  assert.equal(state.applied_config_revision, 'config-2');
  assert.deepEqual(await restarted.loadAppliedConfig(), config);
  assert.deepEqual(await restarted.loadCommandState(), commands);
});

test('account invalidation advances the epoch and rejects all later stale mutations', async () => {
  const indexedDb = new FakeIndexedDb();
  const durable = new DurableIngestOutbox({
    storage: storage(indexedDb, ACCOUNT_A),
    creatorAccountId: ACCOUNT_A,
    idFactory: id,
  });
  const before = await durable.initialize();
  await durable.invalidateAccountEpoch();
  assert.equal(durable.identityState().account_epoch, before.account_epoch + 1);
  await assert.rejects(durable.enqueue(chat), /invalidated/);
});
