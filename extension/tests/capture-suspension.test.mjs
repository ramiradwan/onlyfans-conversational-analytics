import assert from 'node:assert/strict';
import test from 'node:test';

import { parseAgentToBrainMessage } from '../protocol/index.mjs';
import { createAgentRuntime } from '../transport/agent-runtime.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import {
  CAPTURE_MESSAGE_TYPE,
  CAPTURE_PROTOCOL_VERSION,
  CaptureIngestionService,
  createCaptureMessageBridge,
} from '../transport/capture-ingestion.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const ACCOUNT_ID = 'synthetic-account';
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';
const DATABASE_NAME = 'capture-suspension-test';

const CHAT_OBSERVATION = {
  event_type: 'chat.observed',
  observed_at: '2026-07-19T08:00:00Z',
  source_path: '/api2/v2/chats',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: null,
  record: {
    id: 'chat-synthetic',
    withUser: { id: 'fan-synthetic', name: 'Synthetic Fan' },
    updatedAt: '2026-07-19T08:00:00Z',
  },
};
const MESSAGE_OBSERVATION = {
  event_type: 'message.observed',
  observed_at: '2026-07-19T08:01:00Z',
  source_path: '/api2/v2/chats/chat-synthetic/messages',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: 'chat-synthetic',
  record: {
    id: 'message-synthetic',
    fromUser: { id: 'fan-synthetic' },
    text: 'Synthetic inbound message',
    createdAt: '2026-07-19T08:01:00Z',
  },
};

class MockSocket {
  constructor() {
    this.readyState = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }

  send(value) {
    this.sent.push(value);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(document) {
    this.onmessage?.({ data: JSON.stringify(document) });
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

function scheduler() {
  return {
    setTimeout() { return {}; },
    clearTimeout() {},
    setInterval() { return {}; },
    clearInterval() {},
  };
}

function captureDocument() {
  return {
    capture_policy: {
      rules: [
        { resource: 'chats', url_pattern: '/api2/v2/chats', enabled: true },
        { resource: 'messages', url_pattern: '/api2/v2/chats/*/messages', enabled: true },
      ],
    },
  };
}

function chromeMessageApi() {
  const listeners = [];
  return {
    listeners,
    runtime: {
      id: 'synthetic-extension-id',
      onMessage: {
        addListener(listener) { listeners.push(listener); },
        removeListener() {},
      },
    },
  };
}

function worker(fakeIndexedDb) {
  const sockets = [];
  const chromeApi = chromeMessageApi();
  const acknowledgmentWaiters = [];
  const acknowledgedSourceSeqs = new Set();
  let generatedId = 0;
  const chromeAdapter = {
    onWake() { return () => {}; },
    async loadAgentIdentity() {
      return {
        agentInstallationId: INSTALLATION_ID,
        agentStreamId: STREAM_ID,
        lastAcknowledgedSourceSeq: 0,
        appliedConfigRevision: 'synthetic-config',
      };
    },
    async loadLegacyIngestionState() { return null; },
    async deleteLegacyIngestionState() {},
    async saveAcknowledgedSourceSeq(sourceSeq) {
      acknowledgedSourceSeqs.add(sourceSeq);
      for (const waiter of acknowledgmentWaiters.splice(0)) waiter(sourceSeq);
    },
  };
  const runtime = createAgentRuntime({
    creatorAccountId: ACCOUNT_ID,
    chromeAdapter,
    ingestionStorageFactory: () => createIndexedDbIngestionStorage(fakeIndexedDb, {
      databaseName: DATABASE_NAME,
    }),
    configHttpFactory: () => ({}),
    configActivatorFactory: () => ({}),
    configClientFactory: () => ({
      activeDocument: captureDocument(),
      async initialize() {},
      healthSummary() { return { status: 'healthy', detail: null }; },
    }),
    transportFactory: (options) => new AgentWebSocketClient({
      ...options,
      scheduler: scheduler(),
      random: () => 0.5,
      idFactory: () => `90000000-0000-4000-8000-${String(++generatedId).padStart(12, '0')}`,
      webSocketFactory: () => {
        const socket = new MockSocket();
        sockets.push(socket);
        return socket;
      },
    }),
  });
  const ingestion = new CaptureIngestionService({ runtime });
  const bridge = createCaptureMessageBridge({ ingestion, chromeApi: { runtime: chromeApi.runtime } });
  bridge.register();
  runtime.registerListeners();
  return {
    runtime,
    sockets,
    listener: chromeApi.listeners[0],
    waitForAcknowledgment(sourceSeq) {
      if (acknowledgedSourceSeqs.has(sourceSeq)) return Promise.resolve();
      return new Promise((resolve) => {
        acknowledgmentWaiters.push((savedSourceSeq) => {
          if (savedSourceSeq === sourceSeq) resolve();
        });
      });
    },
  };
}

function captureMessage(observation) {
  return {
    type: CAPTURE_MESSAGE_TYPE,
    protocol_version: CAPTURE_PROTOCOL_VERSION,
    observation,
  };
}

function dispatchCapture(listener, observation) {
  return new Promise((resolve) => {
    const keepAlive = listener(
      captureMessage(observation),
      {
        id: 'synthetic-extension-id',
        frameId: 0,
        url: 'https://onlyfans.com/my/chats',
      },
      resolve,
    );
    assert.equal(keepAlive, true);
  });
}

function session(connectionId, committedSourceSeq) {
  return {
    type: 'agent.session',
    protocol_version: '1',
    message_id: '00000000-0000-4000-8000-000000000021',
    payload: {
      connection_id: connectionId,
      fencing_token: `fence-${committedSourceSeq}`,
      creator_account_id: ACCOUNT_ID,
      agent_installation_id: INSTALLATION_ID,
      agent_stream_id: STREAM_ID,
      committed_source_seq: committedSourceSeq,
      resume_action: 'resume',
      required_config_revision: 'synthetic-config',
      lease: {
        heartbeat_interval_seconds: 20,
        lease_timeout_seconds: 60,
      },
    },
  };
}

function acknowledgment(connectionId, committedSourceSeq) {
  return {
    type: 'ingest.ack',
    protocol_version: '1',
    message_id: '00000000-0000-4000-8000-000000000022',
    payload: {
      connection_id: connectionId,
      creator_account_id: ACCOUNT_ID,
      agent_stream_id: STREAM_ID,
      snapshot_id: null,
      committed_source_seq: committedSourceSeq,
    },
  };
}

async function bind(workerInstance, connectionId, committedSourceSeq) {
  await workerInstance.runtime.start();
  const socket = workerInstance.sockets[0];
  socket.open();
  socket.receive(session(connectionId, committedSourceSeq));
  await new Promise((resolve) => setImmediate(resolve));
  return socket;
}

test('worker suspension replays the same IndexedDB outbox once, in order, on the bound account', async () => {
  const fakeIndexedDb = new FakeIndexedDb();
  const firstWorker = worker(fakeIndexedDb);
  assert.deepEqual(await dispatchCapture(firstWorker.listener, CHAT_OBSERVATION), {
    ok: true,
    event_type: 'chat.observed',
    source_seq: 1,
  });
  assert.deepEqual(await dispatchCapture(firstWorker.listener, MESSAGE_OBSERVATION), {
    ok: true,
    event_type: 'message.observed',
    source_seq: 2,
  });
  const retainedEventIds = (await firstWorker.runtime.transport.outbox.entries())
    .map((item) => item.event_id);
  firstWorker.runtime.transport.stop();

  const restartedWorker = worker(fakeIndexedDb);
  const connectionId = '10000000-0000-4000-8000-000000000051';
  const replaySocket = await bind(restartedWorker, connectionId, 0);
  const replayed = replaySocket.sent
    .map((value) => JSON.parse(value))
    .filter((document) => document.type === 'ingest.delta')
    .map(parseAgentToBrainMessage);
  assert.equal(replayed.length, 2);
  assert.deepEqual(replayed.map((message) => message.payload.source_seq), [1, 2]);
  assert.deepEqual(replayed.map((message) => message.payload.change.type), [
    'chat.upsert',
    'message.upsert',
  ]);
  assert.deepEqual(replayed.map((message) => message.payload.event_id), retainedEventIds);
  assert.equal(new Set(replayed.map((message) => message.payload.event_id)).size, 2);
  assert.ok(replayed.every((message) => message.payload.creator_account_id === ACCOUNT_ID));
  assert.ok(replayed.every((message) => message.payload.fencing_token === 'fence-0'));

  replaySocket.receive(acknowledgment(connectionId, 2));
  await restartedWorker.waitForAcknowledgment(2);
  restartedWorker.runtime.transport.stop();

  const acknowledgedWorker = worker(fakeIndexedDb);
  const acknowledgedSocket = await bind(
    acknowledgedWorker,
    '10000000-0000-4000-8000-000000000052',
    2,
  );
  const hello = parseAgentToBrainMessage(JSON.parse(acknowledgedSocket.sent[0]));
  assert.equal(hello.payload.last_acknowledged_source_seq, 2);
  assert.equal(
    acknowledgedSocket.sent
      .map((value) => JSON.parse(value))
      .filter((document) => document.type === 'ingest.delta')
      .length,
    0,
  );
  acknowledgedWorker.runtime.transport.stop();
});

test('message-first capture uses the production IndexedDB transaction for its parent pair', async () => {
  const fakeIndexedDb = new FakeIndexedDb();
  const instance = worker(fakeIndexedDb);

  assert.deepEqual(await dispatchCapture(instance.listener, MESSAGE_OBSERVATION), {
    ok: true,
    event_type: 'message.observed',
    source_seq: 2,
  });
  let state = instance.runtime.transport.outbox.snapshotState();
  assert.deepEqual(state.outbox.map((item) => item.source_seq), [1, 2]);
  assert.deepEqual(state.outbox.map((item) => item.change.type), [
    'chat.upsert',
    'message.upsert',
  ]);
  assert.equal(state.chats[0].display_name, null);

  assert.deepEqual(await dispatchCapture(instance.listener, CHAT_OBSERVATION), {
    ok: true,
    event_type: 'chat.observed',
    source_seq: 3,
  });
  assert.deepEqual(await dispatchCapture(instance.listener, {
    ...MESSAGE_OBSERVATION,
    observed_at: '2026-07-19T08:02:00Z',
    record: {
      ...MESSAGE_OBSERVATION.record,
      id: 'message-synthetic-2',
      createdAt: '2026-07-19T08:02:00Z',
    },
  }), {
    ok: true,
    event_type: 'message.observed',
    source_seq: 4,
  });
  state = instance.runtime.transport.outbox.snapshotState();
  assert.deepEqual(state.outbox.map((item) => item.source_seq), [1, 2, 3, 4]);
  assert.equal(
    state.outbox.filter((item) => item.change.type === 'chat.upsert').length,
    2,
  );
  assert.equal(state.chats[0].display_name, 'Synthetic Fan');
  assert.equal(state.messages.length, 2);
  instance.runtime.transport.stop();
});
