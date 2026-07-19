import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import {
  DurableIngestOutbox,
  INGESTION_META_KEY,
  INGESTION_STATE_VERSION,
  INGESTION_STORES,
  SNAPSHOT_MAX_FRAME_BYTES,
  SNAPSHOT_MAX_RECORDS,
} from '../transport/durable-outbox.mjs';

const messageCountArgument = process.argv.find((argument) => argument.startsWith('--messages='));
const MESSAGE_COUNT = messageCountArgument === undefined
  ? 100_000
  : Number.parseInt(messageCountArgument.slice('--messages='.length), 10);
if (!Number.isSafeInteger(MESSAGE_COUNT) || MESSAGE_COUNT < 1) {
  throw new Error('--messages must be a positive safe integer');
}
const ACCOUNT_ID = 'qualification-account';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';
const SNAPSHOT_ID = '40000000-0000-4000-8000-000000000001';
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const CONNECTION_ID = '10000000-0000-4000-8000-000000000001';
const MAX_POST_BASELINE_HEAP_DELTA = 64 * 1024 * 1024;

const clone = (value) => structuredClone(value);
const stable = (value) => {
  if (Array.isArray(value)) return value.map(stable);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
};
const hashRecord = (hash, value) => hash.update(`${JSON.stringify(stable(value))}\n`);
const encodedBytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength;

const PRIMARY_KEYS = Object.freeze({
  [INGESTION_STORES.meta]: null,
  [INGESTION_STORES.outbox]: 'source_seq',
  [INGESTION_STORES.chats]: 'chat_id',
  [INGESTION_STORES.messages]: 'message_id',
  [INGESTION_STORES.coverageEvidence]: 'evidence_key',
  [INGESTION_STORES.historyJobs]: 'job_id',
  [INGESTION_STORES.commandResults]: 'key',
  [INGESTION_STORES.config]: 'key',
  [INGESTION_STORES.snapshotManifests]: 'snapshot_id',
  [INGESTION_STORES.snapshotChunks]: 'key',
  [INGESTION_STORES.snapshotOverrides]: 'key',
  [INGESTION_STORES.credentials]: 'key',
});

function lowerBound(keys, afterKey) {
  if (afterKey === null) return 0;
  let low = 0;
  let high = keys.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (keys[middle] <= afterKey) low = middle + 1;
    else high = middle;
  }
  return low;
}

/**
 * Qualification-only adapter: canonical fixture rows stay in the baseline heap while immutable
 * snapshot chunks are written to a temporary directory, matching production IndexedDB's
 * page-at-a-time process-memory behavior.
 */
class QualificationStorage {
  constructor(chunkDirectory) {
    this.chunkDirectory = chunkDirectory;
    this.chain = Promise.resolve();
    this.maxPageSize = 0;
    this.getAllCalls = 0;
    this.stores = new Map(
      Object.values(INGESTION_STORES).map((storeName) => [storeName, new Map()]),
    );
    this.orderedKeys = new Map();
  }

  chunkPath(key) {
    const safe = Buffer.from(String(key)).toString('base64url');
    return path.join(this.chunkDirectory, `${safe}.json`);
  }

  seed(meta, chat, messages) {
    this.stores.get(INGESTION_STORES.meta).set(INGESTION_META_KEY, clone(meta));
    this.stores.get(INGESTION_STORES.chats).set(chat.chat_id, clone(chat));
    const messageStore = this.stores.get(INGESTION_STORES.messages);
    const keys = [];
    for (const message of messages) {
      messageStore.set(message.message_id, message);
      keys.push(message.message_id);
    }
    this.orderedKeys.set(INGESTION_STORES.chats, [chat.chat_id]);
    this.orderedKeys.set(INGESTION_STORES.messages, keys);
    this.orderedKeys.set(INGESTION_STORES.coverageEvidence, []);
  }

  runTransaction(_mode, _storeNames, work) {
    const run = this.chain.then(async () => {
      const handle = {
        get: async (storeName, key) => {
          if (storeName === INGESTION_STORES.snapshotChunks) {
            try {
              return JSON.parse(await readFile(this.chunkPath(key), 'utf8'));
            } catch (error) {
              if (error?.code === 'ENOENT') return undefined;
              throw error;
            }
          }
          const value = this.stores.get(storeName).get(key);
          return value === undefined ? undefined : clone(value);
        },
        getAll: async (storeName) => {
          this.getAllCalls += 1;
          return [...this.stores.get(storeName).values()].map(clone);
        },
        getAllKeys: async (storeName) => [...this.stores.get(storeName).keys()].sort(),
        getAllKeysFromIndex: async () => [],
        getPage: async (storeName, { afterKey = null, limit = 100 } = {}) => {
          const store = this.stores.get(storeName);
          const keys = this.orderedKeys.get(storeName) ?? [...store.keys()].sort();
          const start = lowerBound(keys, afterKey);
          const pageKeys = keys.slice(start, start + limit);
          this.maxPageSize = Math.max(this.maxPageSize, pageKeys.length);
          return pageKeys.map((key) => ({ key, value: clone(store.get(key)) }));
        },
        getPageFromIndex: async (
          storeName,
          indexName,
          { afterIndexKey = null, limit = 100 } = {},
        ) => {
          const rows = [...this.stores.get(storeName)]
            .filter(([, value]) => (
              afterIndexKey === null || value[indexName] > afterIndexKey
            ))
            .sort(([leftKey, left], [rightKey, right]) => (
              left[indexName] - right[indexName] || String(leftKey).localeCompare(String(rightKey))
            ))
            .slice(0, limit)
            .map(([key, value]) => ({
              key,
              indexKey: value[indexName],
              value: clone(value),
            }));
          this.maxPageSize = Math.max(this.maxPageSize, rows.length);
          return rows;
        },
        put: async (storeName, value, suppliedKey = undefined) => {
          const keyPath = PRIMARY_KEYS[storeName];
          const key = suppliedKey ?? (keyPath === null ? undefined : value[keyPath]);
          if (key === undefined) throw new Error(`A key is required for ${storeName}`);
          if (storeName === INGESTION_STORES.snapshotChunks) {
            await writeFile(this.chunkPath(key), JSON.stringify(value), 'utf8');
            return;
          }
          this.stores.get(storeName).set(key, clone(value));
        },
        delete: async (storeName, key) => {
          if (storeName === INGESTION_STORES.snapshotChunks) {
            await rm(this.chunkPath(key), { force: true });
          } else {
            this.stores.get(storeName).delete(key);
          }
        },
        clear: async (storeName) => {
          if (storeName === INGESTION_STORES.snapshotChunks) {
            await rm(this.chunkDirectory, { force: true, recursive: true });
            await (await import('node:fs/promises')).mkdir(this.chunkDirectory, { recursive: true });
          } else {
            this.stores.get(storeName).clear();
          }
        },
      };
      return work(Object.freeze(handle));
    });
    this.chain = run.then(() => undefined, () => undefined);
    return run;
  }
}

function envelope(frame) {
  return {
    type: 'ingest.snapshot',
    protocol_version: '2',
    message_id: '00000000-0000-4000-8000-000000000001',
    payload: {
      connection_id: CONNECTION_ID,
      fencing_token: 'qualification-fence',
      creator_account_id: ACCOUNT_ID,
      agent_installation_id: INSTALLATION_ID,
      agent_stream_id: STREAM_ID,
      ...frame,
    },
  };
}

if (typeof globalThis.gc !== 'function') {
  throw new Error('Run the qualification with Node --expose-gc');
}

const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-snapshot-qualification-'));
try {
  const storage = new QualificationStorage(temporaryRoot);
  const durable = new DurableIngestOutbox({
    storage,
    creatorAccountId: ACCOUNT_ID,
    idFactory: () => STREAM_ID,
  });
  await durable.initialize();

  const oracle = createHash('sha256');
  const chat = {
    chat_id: 'chat-1',
    record_kind: 'full',
    platform_user_id: 'fan-1',
    display_name: 'Qualification fan',
    updated_at: '2026-07-19T08:00:00Z',
    last_source_seq: 1,
    last_origin: 'signer',
  };
  hashRecord(oracle, { tombstone: false, chat: {
    chat_id: chat.chat_id,
    record_kind: chat.record_kind,
    platform_user_id: chat.platform_user_id,
    display_name: chat.display_name,
    updated_at: chat.updated_at,
  } });

  const messages = [];
  for (let index = 0; index < MESSAGE_COUNT; index += 1) {
    const message = {
      message_id: `message-${String(index).padStart(6, '0')}`,
      chat_id: chat.chat_id,
      sender_platform_user_id: index % 2 === 0 ? 'fan-1' : 'creator-1',
      text: `qualification-message-${index}`,
      sent_at: new Date(Date.parse('2020-01-01T00:00:00Z') + index * 1_000).toISOString(),
      direction: index % 2 === 0 ? 'inbound' : 'outbound',
      last_source_seq: 1,
      last_origin: 'signer',
    };
    messages.push(message);
    hashRecord(oracle, { tombstone: false, message: {
      message_id: message.message_id,
      chat_id: message.chat_id,
      sender_platform_user_id: message.sender_platform_user_id,
      text: message.text,
      sent_at: message.sent_at,
      direction: message.direction,
    } });
  }
  storage.seed({
    version: INGESTION_STATE_VERSION,
    creator_account_id: ACCOUNT_ID,
    agent_stream_id: STREAM_ID,
    account_epoch: 1,
    last_source_seq: 1,
    acknowledged_source_seq: 0,
    applied_config_revision: 'qualification-config',
    outbox_count: 0,
    entity_counts: { chats: 1, messages: MESSAGE_COUNT, coverage_evidence: 0 },
    pending_snapshot: null,
  }, chat, messages);
  messages.length = 0;

  globalThis.gc();
  const baselineHeap = process.memoryUsage().heapUsed;
  let peakHeapAfterGc = baselineHeap;
  let maxFrameBytes = 0;
  let observedRecords = 0;
  const actual = createHash('sha256');

  let manifest = await durable.createSnapshot(SNAPSHOT_ID);
  while (manifest.state !== 'ready') {
    const result = await durable.buildNextSnapshotChunk();
    manifest = result.manifest;
    if (result.chunk !== null) {
      assert.ok(result.chunk.records.length <= SNAPSHOT_MAX_RECORDS);
      const frame = {
        frame_kind: 'chunk',
        snapshot_id: SNAPSHOT_ID,
        chunk_index: result.chunk.chunk_index,
        entity_kind: result.chunk.entity_kind,
        records: result.chunk.records,
      };
      maxFrameBytes = Math.max(maxFrameBytes, encodedBytes(envelope(frame)));
      observedRecords += result.chunk.records.length;
      for (const record of result.chunk.records) hashRecord(actual, record);
    }
    if (manifest.next_chunk_index % 25 === 0) {
      globalThis.gc();
      peakHeapAfterGc = Math.max(peakHeapAfterGc, process.memoryUsage().heapUsed);
    }
  }

  const begin = await durable.snapshotBeginFrame();
  const commit = await durable.snapshotCommitFrame();
  maxFrameBytes = Math.max(
    maxFrameBytes,
    encodedBytes(envelope(begin)),
    encodedBytes(envelope(commit)),
  );
  globalThis.gc();
  peakHeapAfterGc = Math.max(peakHeapAfterGc, process.memoryUsage().heapUsed);

  assert.equal(manifest.record_counts.chats, 1);
  assert.equal(manifest.record_counts.messages, MESSAGE_COUNT);
  assert.equal(observedRecords, MESSAGE_COUNT + 1);
  assert.equal(actual.digest('hex'), oracle.digest('hex'));
  assert.ok(maxFrameBytes < SNAPSHOT_MAX_FRAME_BYTES);
  assert.ok(storage.maxPageSize <= SNAPSHOT_MAX_RECORDS);
  assert.equal(storage.getAllCalls, 0);
  const heapDelta = Math.max(0, peakHeapAfterGc - baselineHeap);
  assert.ok(
    heapDelta < MAX_POST_BASELINE_HEAP_DELTA,
    `snapshot heap delta ${heapDelta} exceeded ${MAX_POST_BASELINE_HEAP_DELTA}`,
  );

  process.stdout.write(`${JSON.stringify({
    messages: MESSAGE_COUNT,
    chunks: manifest.chunk_count,
    max_frame_bytes: maxFrameBytes,
    maximum_page_records: storage.maxPageSize,
    post_baseline_heap_delta_bytes: heapDelta,
    material_hash_equal: true,
  }, null, 2)}\n`);
} finally {
  await rm(temporaryRoot, { force: true, recursive: true });
}
