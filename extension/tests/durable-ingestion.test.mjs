import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { parseAgentToBrainMessage } from '../protocol/index.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.resolve(HERE, '../../shared/fixtures/protocol/v1');
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';

const chatChange = (chatId = 'chat-1') => ({
  type: 'chat.upsert',
  chat: {
    chat_id: chatId,
    platform_user_id: `fan-${chatId}`,
    display_name: 'Alex',
    updated_at: '2026-07-18T10:00:00Z',
  },
});

const messageChange = {
  type: 'message.upsert',
  message: {
    message_id: 'message-1',
    chat_id: 'chat-1',
    sender_platform_user_id: 'fan-chat-1',
    text: 'Hello',
    sent_at: '2026-07-18T10:01:00Z',
    direction: 'inbound',
  },
};

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

class MockSocket {
  constructor(log) {
    this.log = log;
    this.readyState = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }

  send(data) {
    this.log.push('send');
    this.sent.push(data);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(document) {
    this.onmessage?.({ data: JSON.stringify(document) });
  }

  drop() {
    this.readyState = 3;
    this.onclose?.();
  }

  close() {
    this.drop();
  }
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
      assert.ok(task);
      task.cleared = true;
      task.handler();
    },
  };
}

function clientHarness(outbox, log = []) {
  const sockets = [];
  const clock = scheduler();
  let id = 1;
  const client = new AgentWebSocketClient({
    identity: {
      agentInstallationId: INSTALLATION_ID,
      agentStreamId: STREAM_ID,
      lastAcknowledgedSourceSeq: 0,
      appliedConfigRevision: 'config-7',
    },
    outbox,
    scheduler: clock,
    random: () => 0.5,
    idFactory: () => `90000000-0000-4000-8000-${String(id++).padStart(12, '0')}`,
    webSocketFactory: () => {
      const socket = new MockSocket(log);
      sockets.push(socket);
      return socket;
    },
  });
  return { client, clock, sockets };
}

async function bind(socket, { connectionId, fence } = {}) {
  const session = await fixture('agent.session');
  session.payload.committed_source_seq = 0;
  session.payload.connection_id = connectionId ?? session.payload.connection_id;
  session.payload.fencing_token = fence ?? session.payload.fencing_token;
  socket.receive(session);
  await new Promise((resolve) => setImmediate(resolve));
  return session;
}

test('capture persists before send and a new worker reconstructs the durable outbox', async () => {
  const log = [];
  const storage = new InMemoryIngestionStorage(log);
  const outbox = new DurableIngestOutbox({ storage, idFactory: () => crypto.randomUUID() });
  await outbox.initialize();
  const h = clientHarness(outbox, log);
  h.client.start();
  h.sockets[0].open();
  await bind(h.sockets[0]);
  log.length = 0;

  const captured = await h.client.captureDelta(chatChange());
  assert.equal(captured.source_seq, 1);
  assert.deepEqual(log.slice(0, 2), ['persist', 'send']);

  const restarted = new DurableIngestOutbox({ storage });
  await restarted.initialize();
  assert.deepEqual(await restarted.entries(), [captured]);
});

test('reconnect flushes the retained outbox in order with the new fence', async () => {
  const storage = new InMemoryIngestionStorage();
  let event = 1;
  const outbox = new DurableIngestOutbox({
    storage,
    idFactory: () => `50000000-0000-4000-8000-${String(event++).padStart(12, '0')}`,
  });
  await outbox.initialize();
  await outbox.enqueue(chatChange());
  await outbox.enqueue(messageChange);
  const h = clientHarness(outbox);
  h.client.start();
  h.sockets[0].open();
  await bind(h.sockets[0]);
  assert.deepEqual((await outbox.entries()).map((item) => item.source_seq), [1, 2]);

  h.sockets[0].drop();
  h.clock.runNext();
  h.sockets[1].open();
  await bind(h.sockets[1], {
    connectionId: '10000000-0000-4000-8000-000000000099',
    fence: 'fence-99',
  });
  const resent = h.sockets[1].sent
    .map((value) => JSON.parse(value))
    .filter((document) => document.type === 'ingest.delta')
    .map(parseAgentToBrainMessage);
  assert.deepEqual(resent.map((message) => message.payload.source_seq), [1, 2]);
  assert.ok(resent.every((message) => message.payload.fencing_token === 'fence-99'));
});

test('only a covering ingest.ack deletes retained events', async () => {
  const storage = new InMemoryIngestionStorage();
  const outbox = new DurableIngestOutbox({ storage });
  await outbox.initialize();
  await outbox.enqueue(chatChange(), '50000000-0000-4000-8000-000000000001');
  await outbox.enqueue(messageChange, '50000000-0000-4000-8000-000000000002');
  const h = clientHarness(outbox);
  h.client.start();
  h.sockets[0].open();
  const session = await bind(h.sockets[0]);
  assert.equal((await outbox.entries()).length, 2);

  const ack = await fixture('ingest.ack');
  ack.payload.connection_id = session.payload.connection_id;
  ack.payload.committed_source_seq = 1;
  h.sockets[0].receive(ack);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual((await outbox.entries()).map((item) => item.source_seq), [2]);

  ack.message_id = '00000000-0000-4000-8000-000000000099';
  ack.payload.committed_source_seq = 2;
  h.sockets[0].receive(ack);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await outbox.entries(), []);
});

test('sync.required pauses deltas, sends a stable through_seq snapshot, then resumes', async () => {
  const storage = new InMemoryIngestionStorage();
  let event = 1;
  const outbox = new DurableIngestOutbox({
    storage,
    idFactory: () => `50000000-0000-4000-8000-${String(event++).padStart(12, '0')}`,
  });
  await outbox.initialize();
  await outbox.enqueue(chatChange());
  await outbox.enqueue(messageChange);
  const h = clientHarness(outbox);
  h.client.start();
  h.sockets[0].open();
  const session = await bind(h.sockets[0]);

  const sync = await fixture('sync.required');
  sync.payload.connection_id = session.payload.connection_id;
  sync.payload.expected_next_source_seq = 1;
  h.sockets[0].receive(sync);
  await new Promise((resolve) => setImmediate(resolve));
  const snapshots = h.sockets[0].sent.map((value) => JSON.parse(value)).filter(
    (document) => document.type === 'ingest.snapshot',
  );
  assert.equal(snapshots.length, 1);
  assert.equal(snapshots[0].payload.through_seq, 2);
  assert.deepEqual(snapshots[0].payload.chats.map((chat) => chat.chat_id), ['chat-1']);
  assert.deepEqual(snapshots[0].payload.messages.map((message) => message.message_id), ['message-1']);

  await h.client.captureDelta(chatChange('chat-2'));
  const beforeAck = h.sockets[0].sent.map((value) => JSON.parse(value));
  assert.equal(beforeAck.filter((document) => document.type === 'ingest.delta').length, 2);

  const ack = await fixture('ingest.ack');
  ack.payload.connection_id = session.payload.connection_id;
  ack.payload.snapshot_id = snapshots[0].payload.snapshot_id;
  ack.payload.committed_source_seq = 2;
  h.sockets[0].receive(ack);
  await new Promise((resolve) => setImmediate(resolve));
  const afterAck = h.sockets[0].sent.map((value) => JSON.parse(value));
  const resumed = afterAck.filter(
    (document) => document.type === 'ingest.delta' && document.payload.source_seq === 3,
  );
  assert.equal(resumed.length, 1);
});
