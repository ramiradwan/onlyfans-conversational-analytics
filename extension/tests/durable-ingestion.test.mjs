import assert from 'node:assert/strict';
import test from 'node:test';

import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const ACCOUNT = 'creator-account-1';
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';
const CONNECTION_ID = '10000000-0000-4000-8000-000000000001';
let sequence = 0;
const id = () => `90000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}`;
const chat = (chatId = 'chat-1') => ({
  type: 'chat.upsert',
  chat: {
    chat_id: chatId,
    record_kind: 'full',
    platform_user_id: `fan-${chatId}`,
    display_name: 'Alex',
    updated_at: '2026-07-19T08:00:00Z',
  },
});
const message = {
  type: 'message.upsert',
  message: {
    message_id: 'message-1',
    chat_id: 'chat-1',
    sender_platform_user_id: 'fan-chat-1',
    text: 'Hello',
    sent_at: '2026-07-19T08:01:00Z',
    direction: 'inbound',
  },
};

class MockSocket {
  constructor(log = []) {
    this.log = log;
    this.readyState = 0;
    this.sent = [];
  }
  send(value) { this.log.push('send'); this.sent.push(value); }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
  drop() { this.readyState = 3; this.onclose?.(); }
  close() { this.drop(); }
}

function scheduler() {
  const timeouts = [];
  return {
    timeouts,
    setTimeout(handler) {
      const task = { handler, cleared: false };
      timeouts.push(task);
      return task;
    },
    clearTimeout(task) { task.cleared = true; },
    setInterval() { return {}; },
    clearInterval() {},
    runNext() {
      const task = timeouts.find((candidate) => !candidate.cleared);
      task.cleared = true;
      task.handler();
    },
  };
}

function session(overrides = {}) {
  return {
    type: 'agent.session',
    protocol_version: '2',
    message_id: id(),
    payload: {
      connection_id: CONNECTION_ID,
      fencing_token: 'fence-1',
      creator_account_id: ACCOUNT,
      agent_installation_id: INSTALLATION_ID,
      agent_stream_id: STREAM_ID,
      committed_source_seq: 0,
      resume_action: 'resume',
      required_config_revision: 'config-1',
      reconnect_auth_ticket: 'reconnect-ticket-1',
      config_auth_ticket: 'config-ticket-1',
      pending_snapshot_id: null,
      next_expected_chunk_index: 0,
      lease: { heartbeat_interval_seconds: 20, lease_timeout_seconds: 60 },
      ...overrides,
    },
  };
}

function syncRequired(overrides = {}) {
  return {
    type: 'sync.required',
    protocol_version: '2',
    message_id: id(),
    payload: {
      connection_id: CONNECTION_ID,
      creator_account_id: ACCOUNT,
      reason: 'sequence_gap',
      expected_agent_stream_id: STREAM_ID,
      expected_next_source_seq: 1,
      pending_snapshot_id: null,
      next_expected_chunk_index: 0,
      snapshot: {
        include_chats: true,
        include_messages: true,
        include_coverage_evidence: true,
        max_frame_bytes: 524288,
        max_records_per_chunk: 100,
      },
      ...overrides,
    },
  };
}

function ack({ committed = 0, snapshotId = null, progress = null } = {}) {
  return {
    type: 'ingest.ack',
    protocol_version: '2',
    message_id: id(),
    payload: {
      connection_id: CONNECTION_ID,
      creator_account_id: ACCOUNT,
      agent_stream_id: STREAM_ID,
      snapshot_id: snapshotId,
      committed_source_seq: committed,
      snapshot_progress: progress,
    },
  };
}

async function harness(storage = new InMemoryIngestionStorage(), log = []) {
  let localIds = 0;
  const outbox = new DurableIngestOutbox({
    storage,
    creatorAccountId: ACCOUNT,
    idFactory: () => (localIds++ === 0 ? STREAM_ID : id()),
  });
  const persisted = await outbox.initialize();
  const sockets = [];
  const clock = scheduler();
  const client = new AgentWebSocketClient({
    creatorAccountId: ACCOUNT,
    authTicket: 'ticket-1',
    identity: {
      agentInstallationId: INSTALLATION_ID,
      agentStreamId: persisted.agent_stream_id,
      lastAcknowledgedSourceSeq: persisted.acknowledged_source_seq,
      appliedConfigRevision: 'config-1',
    },
    outbox,
    scheduler: clock,
    idFactory: id,
    webSocketFactory: () => {
      const socket = new MockSocket(log);
      sockets.push(socket);
      return socket;
    },
  });
  return { client, outbox, sockets, clock };
}

async function connect(h, document = session()) {
  h.client.start();
  h.sockets[0].open();
  h.sockets[0].receive(document);
  await new Promise((resolve) => setImmediate(resolve));
}

test('capture persists before send and restart reconstructs the durable account outbox', async () => {
  const log = [];
  const storage = new InMemoryIngestionStorage(log);
  const h = await harness(storage, log);
  await connect(h);
  log.length = 0;
  const captured = await h.client.captureDelta(chat());
  assert.equal(captured.source_seq, 1);
  assert.deepEqual(log.slice(0, 2), ['persist', 'send']);

  const restarted = new DurableIngestOutbox({
    storage,
    creatorAccountId: ACCOUNT,
    idFactory: id,
  });
  await restarted.initialize();
  assert.deepEqual(await restarted.entries(), [captured]);
});

test('message upserts cannot bypass dependency-closed capture', async () => {
  const h = await harness();
  await assert.rejects(h.client.captureDelta(message), /captureMessageWithParent/);
  assert.deepEqual(await h.outbox.entries(), []);
});

test('reconnect replays retained deltas in sequence with the renewed fence', async () => {
  const h = await harness();
  await h.outbox.enqueue(chat(), id(), 'passive');
  await h.outbox.enqueue(message, id(), 'signer');
  await connect(h);
  h.sockets[0].drop();
  h.clock.runNext();
  h.sockets[1].open();
  h.sockets[1].receive(session({
    connection_id: '10000000-0000-4000-8000-000000000099',
    fencing_token: 'fence-99',
  }));
  await new Promise((resolve) => setImmediate(resolve));
  const deltas = h.sockets[1].sent.map(JSON.parse).filter((frame) => frame.type === 'ingest.delta');
  assert.deepEqual(deltas.map((frame) => frame.payload.source_seq), [1, 2]);
  assert.ok(deltas.every((frame) => frame.payload.fencing_token === 'fence-99'));
  assert.deepEqual(deltas.map((frame) => frame.payload.acquisition_origin), ['passive', 'signer']);
});

test('snapshot begin/chunks/commit resume by acknowledged chunk and gate later deltas', async () => {
  const h = await harness();
  await h.outbox.enqueue(chat(), id(), 'passive');
  await h.outbox.enqueue(message, id(), 'signer');
  await connect(h);
  h.sockets[0].receive(syncRequired());
  await new Promise((resolve) => setImmediate(resolve));
  const sent = () => h.sockets[0].sent.map(JSON.parse);
  const begin = sent().find((frame) => frame.type === 'ingest.snapshot');
  assert.equal(begin.payload.frame_kind, 'begin');
  assert.equal(begin.payload.chunk_count, 2);

  await h.client.captureDelta(chat('chat-2'));
  assert.equal(sent().some((frame) => frame.payload?.source_seq === 3), false);

  h.sockets[0].receive(ack({
    snapshotId: begin.payload.snapshot_id,
    progress: {
      snapshot_id: begin.payload.snapshot_id,
      next_expected_chunk_index: 0,
      committed: false,
    },
  }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sent().at(-1).payload.chunk_index, 0);

  h.sockets[0].receive(ack({
    snapshotId: begin.payload.snapshot_id,
    progress: {
      snapshot_id: begin.payload.snapshot_id,
      next_expected_chunk_index: 1,
      committed: false,
    },
  }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sent().at(-1).payload.chunk_index, 1);

  h.sockets[0].receive(ack({
    snapshotId: begin.payload.snapshot_id,
    progress: {
      snapshot_id: begin.payload.snapshot_id,
      next_expected_chunk_index: 2,
      committed: false,
    },
  }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sent().at(-1).payload.frame_kind, 'commit');

  h.sockets[0].receive(ack({
    committed: 2,
    snapshotId: begin.payload.snapshot_id,
    progress: {
      snapshot_id: begin.payload.snapshot_id,
      next_expected_chunk_index: 2,
      committed: true,
    },
  }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sent().filter((frame) => frame.payload?.source_seq === 3).length, 1);
  assert.deepEqual((await h.outbox.entries()).map((entry) => entry.source_seq), [3]);
});
