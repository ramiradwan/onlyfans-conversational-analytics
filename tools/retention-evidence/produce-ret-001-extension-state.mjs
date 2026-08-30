#!/usr/bin/env node

import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

import {
  DurableIngestOutbox,
} from '../../extension/transport/durable-outbox.mjs';
import {
  createIndexedDbIngestionStorage,
} from '../../extension/transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from '../../extension/tests/fake-indexeddb.mjs';

const STORAGE_KEY = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
const ROOT = new URL('../../', import.meta.url);
const FIXTURE_URL = new URL('tests/fixtures/analytics/creator-beta.snapshot.json', ROOT);

function parseArgs(argv) {
  const result = { output: null, productRevision: null };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--output') {
      result.output = argv[index + 1];
      index += 1;
      continue;
    }
    if (argv[index] === '--product-revision') {
      result.productRevision = argv[index + 1];
      index += 1;
      continue;
    }
    throw new Error(`Unknown RET-001 Extension-state argument: ${argv[index]}`);
  }
  if (result.output === null) throw new Error('--output is required');
  if (!/^[0-9a-f]{40}$/.test(result.productRevision ?? '')) {
    throw new Error('--product-revision must be a lowercase 40-hex commit SHA');
  }
  return result;
}

function idFactory(firstGroup) {
  let sequence = 0;
  return () => `${firstGroup}-0000-4000-8000-${String(++sequence).padStart(12, '0')}`;
}

function storage(indexedDb, creatorAccountId) {
  return createIndexedDbIngestionStorage(indexedDb, {
    creatorAccountId,
    encryptionKey: STORAGE_KEY,
  });
}

function changes(fixture) {
  const sourceChat = fixture.chats[0];
  const sourceMessage = fixture.messages[0];
  return {
    chat: {
      type: 'chat.upsert',
      chat: {
        record_kind: 'full',
        chat_id: sourceChat.chat_id,
        platform_user_id: sourceChat.platform_user_id,
        display_name: sourceChat.display_name ?? null,
        updated_at: sourceChat.updated_at,
      },
    },
    message: {
      type: 'message.upsert',
      message: {
        message_id: sourceMessage.message_id,
        chat_id: sourceMessage.chat_id,
        sender_platform_user_id: sourceMessage.sender_platform_user_id,
        text: sourceMessage.text,
        sent_at: sourceMessage.sent_at,
        direction: sourceMessage.direction,
      },
    },
  };
}

async function snapshotFrames(durable, snapshotId) {
  const manifest = await durable.prepareSnapshot(snapshotId);
  const begin = await durable.snapshotBeginFrame();
  const chunks = [];
  for (let index = 0; index < manifest.chunk_count; index += 1) {
    chunks.push(await durable.snapshotChunkFrame(index));
  }
  const commit = await durable.snapshotCommitFrame();
  return { manifest, begin, chunks, commit };
}

async function buildPendingDelivery(fixture) {
  const indexedDb = new FakeIndexedDb();
  const ids = idFactory('61000000');
  const durable = new DurableIngestOutbox({
    storage: storage(indexedDb, fixture.creator_account_id),
    creatorAccountId: fixture.creator_account_id,
    idFactory: ids,
  });
  const identity = await durable.initialize();
  const material = changes(fixture);
  await durable.enqueueMessageWithParent(material.message, material.chat, 'passive');
  const beforeRestart = await durable.entries();

  const restarted = new DurableIngestOutbox({
    storage: storage(indexedDb, fixture.creator_account_id),
    creatorAccountId: fixture.creator_account_id,
    idFactory: ids,
  });
  const restartedIdentity = await restarted.initialize();
  const afterRestart = await restarted.entries();

  return {
    identity,
    restarted_identity: restartedIdentity,
    outbox_before_restart: beforeRestart,
    outbox_after_restart: afterRestart,
    message_source: material.message.message,
    chat_source: material.chat.chat,
  };
}

async function buildAcknowledgedMessageState(fixture) {
  const indexedDb = new FakeIndexedDb();
  const ids = idFactory('62000000');
  const durable = new DurableIngestOutbox({
    storage: storage(indexedDb, fixture.creator_account_id),
    creatorAccountId: fixture.creator_account_id,
    idFactory: ids,
  });
  await durable.initialize();
  const material = changes(fixture);
  await durable.enqueueMessageWithParent(material.message, material.chat, 'passive');
  const queued = await durable.entries();
  const highWater = queued.at(-1).source_seq;
  await durable.acknowledge(highWater);

  const restarted = new DurableIngestOutbox({
    storage: storage(indexedDb, fixture.creator_account_id),
    creatorAccountId: fixture.creator_account_id,
    idFactory: ids,
  });
  const identity = await restarted.initialize();
  const afterAcknowledgement = await restarted.entries();
  const snapshot = await snapshotFrames(
    restarted,
    '62000000-0000-4000-8000-000000000900',
  );

  return {
    identity,
    acknowledged_source_seq: highWater,
    outbox_after_acknowledgement: afterAcknowledgement,
    message_source: material.message.message,
    snapshot_after_acknowledgement: snapshot,
  };
}

async function buildSnapshotReplay(fixture) {
  const indexedDb = new FakeIndexedDb();
  const ids = idFactory('63000000');
  const durable = new DurableIngestOutbox({
    storage: storage(indexedDb, fixture.creator_account_id),
    creatorAccountId: fixture.creator_account_id,
    idFactory: ids,
  });
  const identity = await durable.initialize();
  const material = changes(fixture);
  await durable.enqueueMessageWithParent(material.message, material.chat, 'passive');

  const first = await snapshotFrames(
    durable,
    '63000000-0000-4000-8000-000000000901',
  );
  await durable.acknowledge(first.manifest.through_seq, first.manifest.snapshot_id, {
    snapshot_id: first.manifest.snapshot_id,
    next_expected_chunk_index: first.manifest.chunk_count,
    committed: true,
  });
  const outboxAfterFirstSnapshotAck = await durable.entries();
  const second = await snapshotFrames(
    durable,
    '63000000-0000-4000-8000-000000000902',
  );

  return {
    identity,
    message_source: material.message.message,
    chat_source: material.chat.chat,
    first_snapshot: first,
    outbox_after_first_snapshot_ack: outboxAfterFirstSnapshotAck,
    second_snapshot_from_same_extension_state: second,
  };
}

const args = parseArgs(process.argv.slice(2));
const fixture = JSON.parse(await readFile(FIXTURE_URL, 'utf8'));
const document = {
  schema: 'ofca-ret-001-extension-replay-fixtures/v1',
  product_revision: args.productRevision,
  creator_account_id: fixture.creator_account_id,
  synthetic_fixture: true,
  production_lifecycle_change_authorized: false,
  pending_delivery: await buildPendingDelivery(fixture),
  acknowledged_message_state: await buildAcknowledgedMessageState(fixture),
  snapshot_replay: await buildSnapshotReplay(fixture),
};
await writeFile(resolve(args.output), `${JSON.stringify(document, null, 2)}\n`, 'utf8');
process.stdout.write(`${JSON.stringify({
  schema: 'ofca-ret-001-extension-replay-fixture-run/v1',
  product_revision: args.productRevision,
  output: resolve(args.output),
  pending_outbox_count: document.pending_delivery.outbox_after_restart.length,
  acknowledged_outbox_count: document.acknowledged_message_state.outbox_after_acknowledgement.length,
  first_snapshot_chunks: document.snapshot_replay.first_snapshot.manifest.chunk_count,
  second_snapshot_chunks: document.snapshot_replay.second_snapshot_from_same_extension_state.manifest.chunk_count,
}, null, 2)}\n`);
