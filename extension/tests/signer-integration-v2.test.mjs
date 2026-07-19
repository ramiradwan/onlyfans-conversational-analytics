import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildOperationRequest,
  parseTypedResponse,
} from 'local-authenticated-read-connector/browser-signing';

import {
  DurableIngestOutbox,
  COVERAGE_SOURCE_SEQUENCE_INDEX,
  INGESTION_META_KEY,
  INGESTION_STORES,
  SNAPSHOT_MAX_FRAME_BYTES,
} from '../transport/durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/history-coordinator.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import {
  normalizeSignerConversation,
  normalizeSignerMessage,
} from '../transport/signer-normalization.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const ACCOUNT = 'creator-account-1';
let idSequence = 0;
const id = () => `10000000-0000-4000-8000-${String(++idSequence).padStart(12, '0')}`;
const chat = (overrides = {}) => ({
  type: 'chat.upsert',
  chat: {
    chat_id: 'chat-1',
    record_kind: 'full',
    platform_user_id: 'fan-1',
    display_name: 'Alex',
    updated_at: '2026-07-19T08:00:00Z',
    ...overrides,
  },
});
const message = (overrides = {}) => ({
  type: 'message.upsert',
  message: {
    message_id: 'message-1',
    chat_id: 'chat-1',
    sender_platform_user_id: 'fan-1',
    text: 'Hello',
    sent_at: '2026-07-19T08:01:00Z',
    direction: 'inbound',
    ...overrides,
  },
});

function outbox(storage = new InMemoryIngestionStorage()) {
  return new DurableIngestOutbox({
    storage,
    creatorAccountId: ACCOUNT,
    idFactory: id,
  });
}

test('vendored signer canonical entities are the extension normalization boundary', () => {
  const conversationPage = parseTypedResponse({
    operation: 'conversations',
    status: 200,
    contentType: 'application/json',
    body: {
      list: [{
        id: 'chat-1',
        withUser: { id: 'fan-1', name: 'Alex' },
        updatedAt: '2026-07-19T08:00:00Z',
      }],
      boundary: 'inventory_end',
    },
  });
  assert.equal(conversationPage.summary.semantic_success, true);
  assert.deepEqual(normalizeSignerConversation(conversationPage.data.items[0], {
    observedAt: '2026-07-19T09:00:00Z',
    creatorPlatformId: 'creator-platform-1',
  }), chat({ updated_at: '2026-07-19T08:00:00.000Z' }));

  const messagePage = parseTypedResponse({
    operation: 'message-page',
    status: 200,
    contentType: 'application/json',
    body: {
      messages: [{
        id: 'message-1',
        fromUser: { id: 'fan-1' },
        text: 'Hello',
        createdAt: '2026-07-19T08:01:00Z',
      }],
      boundary: 'history_start',
    },
  });
  assert.equal(messagePage.summary.semantic_success, true);
  assert.deepEqual(normalizeSignerMessage(messagePage.data.items[0], {
    observedAt: '2026-07-19T09:00:00Z',
    creatorPlatformId: 'creator-platform-1',
    conversationId: 'chat-1',
  }), message({ sent_at: '2026-07-19T08:01:00.000Z' }));

  const unknownNestedField = parseTypedResponse({
    operation: 'conversations',
    status: 200,
    contentType: 'application/json',
    body: {
      list: [{
        id: 'chat-1',
        withUser: { id: 'fan-1', name: 'Alex', private: 'reject' },
        updatedAt: '2026-07-19T08:00:00Z',
      }],
      boundary: 'inventory_end',
    },
  });
  assert.equal(unknownNestedField.summary.semantic_success, false);
  assert.equal(unknownNestedField.data, null);
});

test('account partition metadata rejects cross-account reuse and stores no global state', async () => {
  const storage = new InMemoryIngestionStorage();
  const first = outbox(storage);
  await first.initialize();
  const second = new DurableIngestOutbox({
    storage,
    creatorAccountId: 'creator-account-2',
    idFactory: id,
  });
  await assert.rejects(second.initialize(), /account-partitioned Agent state is invalid/);
});

test('deterministic no-ops allocate no sequence and signer/passive order has one material transition', async () => {
  const durable = outbox();
  await durable.initialize();
  const passive = await durable.enqueue(chat(), id(), 'passive');
  const signer = await durable.enqueue(chat(), id(), 'signer');
  assert.equal(passive.source_seq, 1);
  assert.equal(signer, null);
  assert.equal(durable.identityState().last_source_seq, 1);
  assert.equal((await durable.entries()).length, 1);
});

test('commitPage atomically stores normalized material, evidence, cursor, and outbox', async () => {
  const storage = new InMemoryIngestionStorage();
  const durable = outbox(storage);
  const state = await durable.initialize();
  await durable.saveHistoryJob({
    job_id: 'job-1',
    account_epoch: state.account_epoch,
    lease_token: 'lease-1',
    cursor: null,
    committed_pages: 0,
  });
  storage.failNextWriteTransactionAfter(3);
  await assert.rejects(durable.commitPage({
    jobId: 'job-1',
    expectedAccountEpoch: state.account_epoch,
    expectedLeaseToken: 'lease-1',
    changes: [chat()],
    evidence: [{
      type: 'generation.started',
      generation_id: '20000000-0000-4000-8000-000000000001',
      as_of: '2026-07-19T08:00:00Z',
      authorization_revision: 'consent-1',
    }],
    nextCursor: 'opaque-next',
  }), /Injected transaction failure/);
  assert.equal(durable.identityState().last_source_seq, 0);
  assert.deepEqual(await durable.entries(), []);
  assert.equal((await durable.historyJobs())[0].cursor, null);
});

test('copy-on-write snapshot keeps the through_seq view and resumes bounded chunks', async () => {
  const durable = outbox();
  await durable.initialize();
  await durable.enqueue(chat(), id(), 'passive');
  const manifest = await durable.createSnapshot(id());
  await durable.enqueue(chat({
    display_name: 'New name',
    updated_at: '2026-07-19T09:00:00Z',
  }), id(), 'passive');
  await durable.prepareSnapshot(manifest.snapshot_id);
  const begin = await durable.snapshotBeginFrame();
  const chunk = await durable.snapshotChunkFrame(0);
  assert.equal(begin.through_seq, 1);
  assert.equal(chunk.records[0].chat.display_name, 'Alex');
  assert.equal(durable.identityState().last_source_seq, 2);

  await durable.acknowledge(0, begin.snapshot_id, {
    snapshot_id: begin.snapshot_id,
    next_expected_chunk_index: 1,
    committed: false,
  });
  assert.equal(durable.identityState().acknowledged_source_seq, 0);
  await durable.acknowledge(1, begin.snapshot_id, {
    snapshot_id: begin.snapshot_id,
    next_expected_chunk_index: begin.chunk_count,
    committed: true,
  });
  assert.deepEqual((await durable.entries()).map((entry) => entry.source_seq), [2]);
});

test('worker death during snapshot chunk construction rolls back scan and resumes exactly', async (t) => {
  for (const failedWrite of [1, 2, 3]) {
    await t.test(`transaction write ${failedWrite}`, async () => {
      const storage = new InMemoryIngestionStorage();
      const firstWorker = outbox(storage);
      await firstWorker.initialize();
      await firstWorker.enqueue(chat(), id(), 'signer');
      await firstWorker.enqueue(message(), id(), 'signer');
      const snapshotId = id();
      await firstWorker.createSnapshot(snapshotId);
      storage.failNextWriteTransactionAfter(failedWrite);

      await assert.rejects(
        firstWorker.buildNextSnapshotChunk(),
        /Injected transaction failure/,
      );
      const failedManifest = await firstWorker.currentSnapshotManifest();
      assert.equal(failedManifest.state, 'building');
      assert.equal(failedManifest.scan_kind_index, 0);
      assert.equal(failedManifest.scan_after_key, null);
      assert.equal(failedManifest.next_chunk_index, 0);

      const restartedWorker = outbox(storage);
      await restartedWorker.initialize();
      const manifest = await restartedWorker.prepareSnapshot(snapshotId);
      const records = [];
      for (let index = 0; index < manifest.chunk_count; index += 1) {
        records.push(...(await restartedWorker.snapshotChunkFrame(index)).records);
      }
      assert.deepEqual(records, [
        { tombstone: false, chat: chat().chat },
        { tombstone: false, message: message().message },
      ]);
    });
  }
});

test('coverage snapshot uses source chronology across reversed generations and worker restart', async () => {
  const indexedDb = new FakeIndexedDb();
  const databaseName = 'coverage-source-order-restart';
  const storage = createIndexedDbIngestionStorage(indexedDb, {
    creatorAccountId: ACCOUNT,
    databaseName,
  });
  const firstWorker = outbox(storage);
  await firstWorker.initialize();
  const generations = [
    'ffffffff-ffff-4fff-8fff-ffffffffffff',
    '00000000-0000-4000-8000-000000000001',
  ];
  const expected = [];
  for (const generationId of generations) {
    const evidence = [{
      type: 'generation.started',
      generation_id: generationId,
      as_of: '2026-07-19T08:00:00Z',
      authorization_revision: 'consent-1',
    }];
    for (let index = 0; index < 60; index += 1) evidence.push({
      type: 'inventory.member',
      generation_id: generationId,
      conversation_id: `${generationId.slice(0, 4)}-chat-${String(index).padStart(2, '0')}`,
    });
    evidence.push({
      type: 'inventory.ended',
      generation_id: generationId,
      observed_at: '2026-07-19T08:01:00Z',
    });
    for (const item of evidence) {
      expected.push(item);
      await firstWorker.enqueue({ type: 'coverage.observed', evidence: item }, id(), 'signer');
    }
  }
  const snapshotId = id();
  await firstWorker.createSnapshot(snapshotId);
  await firstWorker.buildNextSnapshotChunk(); // empty chats
  await firstWorker.buildNextSnapshotChunk(); // empty messages
  const firstCoverage = await firstWorker.buildNextSnapshotChunk();
  assert.equal(firstCoverage.chunk.entity_kind, 'coverage_evidence');
  assert.equal(firstCoverage.chunk.records.length, 100);

  const restartedWorker = outbox(storage);
  await restartedWorker.initialize();
  const manifest = await restartedWorker.prepareSnapshot(snapshotId);
  const actual = [];
  for (let index = 0; index < manifest.chunk_count; index += 1) {
    const frame = await restartedWorker.snapshotChunkFrame(index);
    if (frame.entity_kind === 'coverage_evidence') actual.push(...frame.records);
  }
  assert.deepEqual(actual, expected);
  assert.deepEqual(
    actual.map((item) => item.generation_id),
    expected.map((item) => item.generation_id),
  );
  assert.equal(
    indexedDb.databases
      .get(databaseName)
      .stores
      .get(INGESTION_STORES.coverageEvidence)
      .indexes
      .get(COVERAGE_SOURCE_SEQUENCE_INDEX),
    'last_source_seq',
  );
});

test('10,000-message repair fixture stays within 100 records and 512 KiB per frame', async () => {
  const storage = new InMemoryIngestionStorage();
  const durable = outbox(storage);
  await durable.initialize();
  await storage.runTransaction(
    'readwrite',
    [INGESTION_STORES.meta, INGESTION_STORES.chats, INGESTION_STORES.messages],
    async (tx) => {
      const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
      await tx.put(INGESTION_STORES.chats, {
        ...chat().chat,
        last_source_seq: 1,
        last_origin: 'signer',
      });
      for (let index = 0; index < 10_000; index += 1) {
        const messageId = `message-${String(index).padStart(5, '0')}`;
        await tx.put(INGESTION_STORES.messages, {
          ...message({ message_id: messageId, text: `fixture-${index}` }).message,
          last_source_seq: 1,
          last_origin: 'signer',
        });
      }
      meta.last_source_seq = 1;
      meta.entity_counts = { chats: 1, messages: 10_000, coverage_evidence: 0 };
      await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
    },
  );
  const manifest = await durable.prepareSnapshot(id());
  assert.equal(manifest.record_counts.messages, 10_000);
  assert.ok(manifest.chunk_count >= 101);
  for (let index = 0; index < manifest.chunk_count; index += 1) {
    const frame = await durable.snapshotChunkFrame(index);
    assert.ok(frame.records.length <= 100);
    const envelope = {
      type: 'ingest.snapshot',
      protocol_version: '2',
      message_id: id(),
      payload: {
        connection_id: id(),
        fencing_token: 'fence',
        creator_account_id: ACCOUNT,
        agent_installation_id: id(),
        agent_stream_id: durable.identityState().agent_stream_id,
        ...frame,
      },
    };
    assert.ok(new TextEncoder().encode(JSON.stringify(envelope)).byteLength < SNAPSHOT_MAX_FRAME_BYTES);
  }
});

test('snapshot builder splits near-target records and rejects one normalized record over 384 KiB', async () => {
  const bounded = outbox();
  await bounded.initialize();
  await bounded.enqueue(chat(), id(), 'signer');
  for (let index = 0; index < 5; index += 1) {
    await bounded.enqueue(message({
      message_id: `large-${index}`,
      text: `${index}${'x'.repeat(149_999)}`,
    }), id(), 'signer');
  }
  const boundedManifest = await bounded.prepareSnapshot(id());
  assert.ok(boundedManifest.chunk_count >= 3);
  for (let index = 0; index < boundedManifest.chunk_count; index += 1) {
    const frame = await bounded.snapshotChunkFrame(index);
    const encoded = new TextEncoder().encode(JSON.stringify({
      type: 'ingest.snapshot',
      protocol_version: '2',
      message_id: id(),
      payload: {
        connection_id: id(),
        fencing_token: 'fence',
        creator_account_id: ACCOUNT,
        agent_installation_id: id(),
        agent_stream_id: bounded.identityState().agent_stream_id,
        ...frame,
      },
    })).byteLength;
    assert.ok(encoded < SNAPSHOT_MAX_FRAME_BYTES);
  }

  const oversizedStorage = new InMemoryIngestionStorage();
  const oversized = outbox(oversizedStorage);
  await oversized.initialize();
  await oversizedStorage.runTransaction(
    'readwrite',
    [INGESTION_STORES.meta, INGESTION_STORES.chats, INGESTION_STORES.messages],
    async (tx) => {
      const meta = await tx.get(INGESTION_STORES.meta, INGESTION_META_KEY);
      await tx.put(INGESTION_STORES.chats, {
        ...chat().chat,
        last_source_seq: 1,
        last_origin: 'signer',
      });
      await tx.put(INGESTION_STORES.messages, {
        ...message({ text: 'y'.repeat(393_216) }).message,
        last_source_seq: 1,
        last_origin: 'signer',
      });
      meta.last_source_seq = 1;
      meta.entity_counts = { chats: 1, messages: 1, coverage_evidence: 0 };
      await tx.put(INGESTION_STORES.meta, meta, INGESTION_META_KEY);
    },
  );
  await assert.rejects(
    oversized.prepareSnapshot(id()),
    (error) => error?.code === 'snapshot_record_oversize',
  );
});

test('history coordinator permits signer-owned safe bootstrap/renewal and emits typed evidence', async () => {
  const durable = outbox();
  const state = await durable.initialize();
  const calls = [];
  const signer = {
    async read(request) {
      const canonicalRequest = buildOperationRequest(request.operation, request.parameters);
      calls.push(request);
      if (request.operation === 'identity') {
        assert.equal(canonicalRequest.url.pathname, '/api2/v2/users/me');
        return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
      }
      if (request.operation === 'conversations') {
        assert.equal(canonicalRequest.url.pathname, '/api2/v2/chats');
        assert.equal(canonicalRequest.url.search, '?limit=50');
        return {
          operation: 'conversations',
          success: true,
          data: {
            items: [{
              id: 'chat-1',
              platform_user_id: 'fan-1',
              display_name: 'Alex',
              updated_at: '2026-07-19T08:00:00.000Z',
            }],
            continuation: null,
            boundary: 'inventory_end',
          },
        };
      }
      assert.equal(canonicalRequest.url.pathname, '/api2/v2/chats/chat-1/messages');
      assert.equal(canonicalRequest.url.search, '?limit=50');
      return {
        operation: 'message-page',
        success: true,
        data: {
          items: [{
            id: 'message-1',
            chat_id: 'chat-1',
            sender_platform_user_id: 'fan-1',
            text: 'Hello',
            sent_at: '2026-07-19T08:01:00.000Z',
            direction: 'inbound',
          }],
          continuation: null,
          boundary: 'history_start',
        },
      };
    },
  };
  let tick = 0;
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => `2026-07-19T08:${String(tick++).padStart(2, '0')}:00Z`,
    delay: async () => {},
    configuration: () => ({
      creator_account_id: ACCOUNT,
      config_revision: 'config-1',
      history_acquisition: {
        enabled: true,
        consent_revision: 'consent-1',
        authorized_platform_creator_id: 'creator-platform-1',
        recent_window_days: 30,
        page_size: 50,
        pages_per_wake: 10,
        request_interval_ms: 0,
        retry_limit: 2,
      },
    }),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });
  const result = await coordinator.wake();
  assert.equal(result.status, 'progressed');
  assert.deepEqual(calls.map((call) => call.operation), [
    'identity',
    'conversations',
    'message-page',
  ]);
  assert.deepEqual(calls.map((call) => call.parameters), [
    {},
    { query: { limit: 50, cursor: null } },
    { conversationId: 'chat-1', query: { limit: 50, cursor: null } },
  ]);
  assert.ok(calls.every((call) => call.refreshMode === 'allow'));
  const changes = (await durable.entries()).map((entry) => entry.change);
  const evidence = changes
    .filter((change) => change.type === 'coverage.observed')
    .map((change) => change.evidence);
  assert.deepEqual(evidence.map((item) => item.type), [
    'generation.started',
    'inventory.member',
    'inventory.ended',
    'conversation.head_reconciled',
    'conversation.history_started',
    'generation.closed',
  ]);
  assert.ok(evidence.every((item) => !Object.hasOwn(item, 'complete')));
  assert.ok(changes.findIndex((change) => change.type === 'chat.upsert')
    < changes.findIndex((change) => change.evidence?.type === 'inventory.member'));
  assert.ok(changes.findIndex((change) => change.type === 'message.upsert')
    < changes.findIndex((change) => change.evidence?.type === 'conversation.history_started'));
});

test('history scheduling finds pending work and closes a generation beyond 10,000 jobs', async () => {
  const storage = new InMemoryIngestionStorage();
  const durable = outbox(storage);
  const state = await durable.initialize();
  const generationId = '70000000-0000-4000-8000-000000000001';
  const finalConversationId = 'chat-10000';
  await storage.runTransaction(
    'readwrite',
    [INGESTION_STORES.historyJobs],
    async (tx) => {
      for (let index = 0; index <= 10_000; index += 1) {
        const conversationId = `chat-${String(index).padStart(5, '0')}`;
        await tx.put(INGESTION_STORES.historyJobs, {
          job_id: `${generationId}:conversation:${conversationId}`,
          generation_id: generationId,
          kind: 'conversation',
          conversation_id: conversationId,
          phase: conversationId === finalConversationId ? 'history' : 'complete',
          as_of: '2026-07-19T09:00:00Z',
          cursor: null,
          boundary: conversationId === finalConversationId ? null : 'history_start',
          committed_pages: conversationId === finalConversationId ? 0 : 1,
          retry_count: 0,
          earliest_observed_at: null,
          head_reconciled: conversationId !== finalConversationId,
          account_epoch: state.account_epoch,
          lease_token: 'prior-worker-lease',
          creator_account_id: ACCOUNT,
          authorization_revision: 'consent-1',
          last_activity_at: '2026-07-18T00:00:00Z',
          recent_priority: false,
        });
      }
      await tx.put(INGESTION_STORES.historyJobs, {
        job_id: `${generationId}:inventory`,
        generation_id: generationId,
        kind: 'inventory',
        phase: 'conversations',
        as_of: '2026-07-19T09:00:00Z',
        cursor: null,
        boundary: 'inventory_end',
        committed_pages: 101,
        retry_count: 0,
        account_epoch: state.account_epoch,
        lease_token: 'prior-worker-lease',
        creator_account_id: ACCOUNT,
        authorization_revision: 'consent-1',
        recent_window_days: 30,
      });
    },
  );

  const genericPageLimits = [];
  const conversationPageLimits = [];
  const readHistoryJobsPage = durable.historyJobsPage.bind(durable);
  const readConversationJobsPage = durable.historyConversationJobsPage.bind(durable);
  durable.historyJobs = async () => {
    throw new Error('Coordinator must not use the capped historyJobs scan');
  };
  durable.historyJobsPage = async (afterJobId, limit) => {
    genericPageLimits.push(limit);
    return readHistoryJobsPage(afterJobId, limit);
  };
  durable.historyConversationJobsPage = async (requestedGenerationId, options) => {
    conversationPageLimits.push(options.limit);
    return readConversationJobsPage(requestedGenerationId, options);
  };

  const messageReads = [];
  const signer = {
    async read(request) {
      assert.equal(request.operation, 'message-page');
      messageReads.push(request.parameters.conversationId);
      return {
        operation: 'message-page',
        success: true,
        data: { items: [], continuation: null, boundary: 'history_start' },
      };
    },
  };
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => '2026-07-19T09:01:00Z',
    delay: async () => {},
    configuration: () => authorizedConfiguration(true, { pages_per_wake: 2 }),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });

  assert.deepEqual(await coordinator.wake(), { status: 'progressed', pages: 1 });
  assert.deepEqual(messageReads, [finalConversationId]);
  assert.equal((await durable.historyJob(`${generationId}:inventory`)).phase, 'closed');
  assert.equal(
    (await durable.historyJob(`${generationId}:conversation:${finalConversationId}`)).phase,
    'complete',
  );
  assert.deepEqual(
    (await durable.entries()).map((entry) => entry.change.evidence?.type),
    [
      'conversation.head_reconciled',
      'conversation.history_started',
      'generation.closed',
    ],
  );
  assert.ok(genericPageLimits.length > 20);
  assert.ok(conversationPageLimits.length > 40);
  assert.ok([...genericPageLimits, ...conversationPageLimits]
    .every((limit) => limit === 500));
});

function authorizedConfiguration(enabled = true, overrides = {}) {
  return {
    creator_account_id: ACCOUNT,
    config_revision: 'config-1',
    history_acquisition: {
      enabled,
      consent_revision: 'consent-1',
      authorized_platform_creator_id: 'creator-platform-1',
      recent_window_days: 30,
      page_size: 50,
      pages_per_wake: 10,
      request_interval_ms: 0,
      retry_limit: 2,
      ...overrides,
    },
  };
}

test('raw signer body, signing material, and upstream cursor never enter ingestion frames', async () => {
  const cursor = 'opaque-upstream-cursor-marker';
  const rawBody = 'raw-response-body-marker';
  const signingHeader = 'private-signing-header-marker';
  const durable = outbox();
  const state = await durable.initialize();
  const signer = {
    async read(request) {
      if (request.operation === 'identity') {
        return {
          operation: 'identity',
          success: true,
          data: { id: 'creator-platform-1' },
          signing_headers: signingHeader,
        };
      }
      return {
        operation: 'conversations',
        success: true,
        data: { items: [], continuation: cursor, boundary: null },
        raw_body: rawBody,
        signing_headers: signingHeader,
      };
    },
  };
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => '2026-07-19T09:00:00Z',
    delay: async () => {},
    configuration: () => authorizedConfiguration(true, { pages_per_wake: 1 }),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });
  await coordinator.wake();

  const jobs = JSON.stringify(await durable.historyJobs());
  assert.match(jobs, new RegExp(cursor));
  assert.doesNotMatch(jobs, new RegExp(`${rawBody}|${signingHeader}`));
  const ingestionMaterial = JSON.stringify(await durable.entries());
  assert.doesNotMatch(
    ingestionMaterial,
    new RegExp(`${cursor}|${rawBody}|${signingHeader}`),
  );
  const manifest = await durable.prepareSnapshot(id());
  const frames = [];
  for (let index = 0; index < manifest.chunk_count; index += 1) {
    frames.push(await durable.snapshotChunkFrame(index));
  }
  assert.doesNotMatch(
    JSON.stringify(frames),
    new RegExp(`${cursor}|${rawBody}|${signingHeader}`),
  );
});

test('history coordinator rejects signer complete and page_digest claims', async (t) => {
  for (const [field, value] of [['complete', true], ['page_digest', 'content-fingerprint']]) {
    await t.test(field, async () => {
      const durable = outbox();
      const state = await durable.initialize();
      const signer = {
        async read(request) {
          if (request.operation === 'identity') {
            return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
          }
          return {
            operation: 'conversations',
            success: true,
            data: {
              items: [],
              continuation: null,
              boundary: 'inventory_end',
              [field]: value,
            },
          };
        },
      };
      const coordinator = new HistoryAcquisitionCoordinator({
        outbox: durable,
        signer,
        idFactory: id,
        now: () => '2026-07-19T09:00:00Z',
        configuration: () => authorizedConfiguration(),
        session: () => ({
          creator_account_id: ACCOUNT,
          applied_config_revision: 'config-1',
          account_epoch: state.account_epoch,
        }),
      });

      await assert.rejects(
        coordinator.wake(),
        /not a validated one-page result/,
      );
      const changes = (await durable.entries()).map((entry) => entry.change);
      assert.equal(
        changes.some((change) => change.evidence?.type === 'inventory.ended'),
        false,
      );
      const inventory = (await durable.historyJobs()).find((job) => job.kind === 'inventory');
      assert.equal(inventory.retry_count, 1);
      assert.equal(inventory.cursor, null);
    });
  }
});

test('history commit rechecks consent and account epoch after the signed page returns', async (t) => {
  for (const invalidation of ['consent', 'epoch']) {
    await t.test(invalidation, async () => {
      const durable = outbox();
      const state = await durable.initialize();
      let enabled = true;
      const signer = {
        async read(request) {
          if (request.operation === 'identity') {
            return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
          }
          if (invalidation === 'consent') enabled = false;
          else await durable.invalidateAccountEpoch();
          return {
            operation: 'conversations',
            success: true,
            data: {
              items: [{
                id: 'stale-chat',
                platform_user_id: 'stale-fan',
                display_name: 'Must not commit',
                updated_at: '2026-07-19T08:00:00.000Z',
              }],
              continuation: null,
              boundary: 'inventory_end',
            },
          };
        },
      };
      const coordinator = new HistoryAcquisitionCoordinator({
        outbox: durable,
        signer,
        idFactory: id,
        now: () => '2026-07-19T09:00:00Z',
        configuration: () => authorizedConfiguration(enabled),
        session: () => ({
          creator_account_id: ACCOUNT,
          applied_config_revision: 'config-1',
          account_epoch: state.account_epoch,
        }),
      });

      await assert.rejects(coordinator.wake(), /authorization changed|account epoch|invalidated/i);
      const changes = (await durable.entries()).map((entry) => entry.change);
      assert.equal(changes.some((change) => change.chat?.chat_id === 'stale-chat'), false);
      assert.equal(
        changes.some((change) => change.evidence?.type === 'inventory.member'),
        false,
      );
    });
  }
});

test('pause aborts the in-flight signer and a late page cannot commit or consume retry budget', async () => {
  const durable = outbox();
  const state = await durable.initialize();
  let enabled = true;
  let pageSignal = null;
  let resolvePage;
  const signer = {
    async read(request) {
      if (request.operation === 'identity') {
        return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
      }
      pageSignal = request.signal;
      return new Promise((resolve) => { resolvePage = resolve; });
    },
  };
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => '2026-07-19T09:00:00Z',
    configuration: () => authorizedConfiguration(enabled),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });
  const running = coordinator.wake();
  while (pageSignal === null) await new Promise((resolve) => setImmediate(resolve));
  enabled = false;
  const paused = coordinator.wake();
  assert.equal(pageSignal.aborted, true);
  resolvePage({
    operation: 'conversations',
    success: true,
    data: {
      items: [{
        id: 'late-chat',
        platform_user_id: 'late-fan',
        display_name: 'Must not commit',
        updated_at: '2026-07-19T08:00:00.000Z',
      }],
      continuation: null,
      boundary: 'inventory_end',
    },
  });

  await assert.rejects(running, (error) => error?.name === 'AbortError');
  assert.deepEqual(await paused, { status: 'disabled', pages: 0 });
  const changes = (await durable.entries()).map((entry) => entry.change);
  assert.equal(changes.some((change) => change.chat?.chat_id === 'late-chat'), false);
  const inventory = (await durable.historyJobs()).find((job) => job.kind === 'inventory');
  assert.equal(inventory.retry_count, 0);
  assert.equal(inventory.cursor, null);
});

test('lease loss and shutdown abort signer work without recording a failed page', async (t) => {
  for (const action of ['lease-loss', 'shutdown']) {
    await t.test(action, async () => {
      const durable = outbox();
      const state = await durable.initialize();
      let pageSignal = null;
      const signer = {
        async read(request) {
          if (request.operation === 'identity') {
            return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
          }
          pageSignal = request.signal;
          return new Promise((_, reject) => {
            request.signal.addEventListener('abort', () => reject(request.signal.reason), {
              once: true,
            });
          });
        },
      };
      const coordinator = new HistoryAcquisitionCoordinator({
        outbox: durable,
        signer,
        idFactory: id,
        now: () => '2026-07-19T09:00:00Z',
        configuration: () => authorizedConfiguration(),
        session: () => ({
          creator_account_id: ACCOUNT,
          applied_config_revision: 'config-1',
          account_epoch: state.account_epoch,
        }),
      });
      const running = coordinator.wake();
      while (pageSignal === null) await new Promise((resolve) => setImmediate(resolve));
      if (action === 'shutdown') coordinator.stop();
      else coordinator.cancelCurrent('Agent lease ended');
      assert.equal(pageSignal.aborted, true);
      await assert.rejects(running, (error) => error?.name === 'AbortError');
      const inventory = (await durable.historyJobs()).find((job) => job.kind === 'inventory');
      assert.equal(inventory.retry_count, 0);
      assert.equal(inventory.committed_pages, 1);
    });
  }
});

test('429 retry timing is durable, bounded, body-free, and stops at retry_limit', async () => {
  const durable = outbox();
  const state = await durable.initialize();
  let clock = Date.parse('2026-07-19T09:00:00Z');
  let pageReads = 0;
  const signer = {
    async read(request) {
      if (request.operation === 'identity') {
        return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
      }
      pageReads += 1;
      return {
        operation: 'conversations',
        success: false,
        response: { status: 429, retry_after_ms: 99_000_000 },
        raw_body: 'must-not-persist',
        data: null,
      };
    },
  };
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => new Date(clock).toISOString(),
    clock: () => clock,
    delay: async () => {},
    configuration: () => authorizedConfiguration(true, { retry_limit: 1 }),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });

  await coordinator.wake();
  let inventory = (await durable.historyJobs()).find((job) => job.kind === 'inventory');
  assert.equal(pageReads, 1);
  assert.equal(inventory.retry_count, 1);
  assert.equal(Date.parse(inventory.next_attempt_at) - clock, 3_600_000);
  assert.doesNotMatch(JSON.stringify(inventory), /must-not-persist/);
  await coordinator.wake();
  assert.equal(pageReads, 1);

  clock += 3_600_000;
  await coordinator.wake();
  inventory = (await durable.historyJobs()).find((job) => job.kind === 'inventory');
  assert.equal(pageReads, 2);
  assert.equal(inventory.retry_count, 2);
  assert.equal(inventory.phase, 'closed');
  assert.equal(inventory.next_attempt_at, null);
});

test('recent_window_days prioritizes recent conversations and a new chat opens a new generation', async () => {
  const durable = outbox();
  const state = await durable.initialize();
  let inventoryReads = 0;
  const messageOrder = [];
  const conversation = (chatId, updatedAt) => ({
    id: chatId,
    platform_user_id: `fan-${chatId}`,
    display_name: chatId,
    updated_at: updatedAt,
  });
  const signer = {
    async read(request) {
      if (request.operation === 'identity') {
        return { operation: 'identity', success: true, data: { id: 'creator-platform-1' } };
      }
      if (request.operation === 'conversations') {
        inventoryReads += 1;
        return {
          operation: 'conversations',
          success: true,
          data: {
            items: [
              conversation('a-old', '2026-05-01T00:00:00Z'),
              conversation('z-recent', '2026-07-18T00:00:00Z'),
              ...(inventoryReads > 1
                ? [conversation('new-chat', '2026-07-19T00:00:00Z')]
                : []),
            ],
            continuation: null,
            boundary: 'inventory_end',
          },
        };
      }
      messageOrder.push(request.parameters.conversationId);
      return {
        operation: 'message-page',
        success: true,
        data: { items: [], continuation: null, boundary: 'history_start' },
      };
    },
  };
  const coordinator = new HistoryAcquisitionCoordinator({
    outbox: durable,
    signer,
    idFactory: id,
    now: () => '2026-07-19T09:00:00Z',
    delay: async () => {},
    configuration: () => authorizedConfiguration(),
    session: () => ({
      creator_account_id: ACCOUNT,
      applied_config_revision: 'config-1',
      account_epoch: state.account_epoch,
    }),
  });

  await coordinator.wake();
  assert.deepEqual(messageOrder.slice(0, 2), ['z-recent', 'a-old']);
  await durable.enqueue(chat({
    chat_id: 'new-chat',
    platform_user_id: 'fan-new-chat',
    display_name: 'new-chat',
    updated_at: '2026-07-19T00:00:00.000Z',
  }), id(), 'passive');
  await coordinator.wake();
  const starts = (await durable.entries())
    .filter((entry) => entry.change.evidence?.type === 'generation.started');
  assert.equal(starts.length, 2);
  assert.equal(inventoryReads, 2);
});
