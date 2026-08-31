import assert from 'node:assert/strict';
import test from 'node:test';

import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { DurableIngestOutbox } from '../transport/durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const ACCOUNT = 'dev-creator-account';
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000081';
const STORAGE_KEY = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

class MockSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
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
}

function scheduler() {
  const pending = [];
  return {
    setTimeout(handler, delay) {
      const task = { handler, delay, cleared: false };
      pending.push(task);
      return task;
    },
    clearTimeout(task) {
      task.cleared = true;
    },
    setInterval() {
      return { cleared: false };
    },
    clearInterval(task) {
      task.cleared = true;
    },
    runReconnect() {
      const task = pending.find((candidate) => !candidate.cleared);
      assert.ok(task);
      task.cleared = true;
      task.handler();
    },
  };
}

function session({ streamId, connectionId, fence, reconnectTicket, messageId }) {
  return {
    type: 'agent.session',
    protocol_version: '2',
    message_id: messageId,
    payload: {
      connection_id: connectionId,
      fencing_token: fence,
      creator_account_id: ACCOUNT,
      agent_installation_id: INSTALLATION_ID,
      agent_stream_id: streamId,
      committed_source_seq: 0,
      resume_action: 'resume',
      required_config_revision: 'config-retention-1',
      reconnect_auth_ticket: reconnectTicket,
      config_auth_ticket: `${reconnectTicket}-config`,
      pending_snapshot_id: null,
      next_expected_chunk_index: 0,
      lease: {
        heartbeat_interval_seconds: 20,
        lease_timeout_seconds: 60,
      },
    },
  };
}

async function waitForIngestDeltas(socket, expectedCount) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    const frames = socket.sent.filter((frame) => frame.type === 'ingest.delta');
    if (frames.length >= expectedCount) return frames;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return socket.sent.filter((frame) => frame.type === 'ingest.delta');
}

test('durable outbox retransmits the same unacknowledged events after reconnect', async () => {
  let idCounter = 0;
  const id = () => `90000000-0000-4000-8000-${String(++idCounter).padStart(12, '0')}`;
  const indexedDb = new FakeIndexedDb();
  const outbox = new DurableIngestOutbox({
    storage: createIndexedDbIngestionStorage(indexedDb, {
      creatorAccountId: ACCOUNT,
      databaseName: 'retention-outbox-retry',
      encryptionKey: STORAGE_KEY,
    }),
    creatorAccountId: ACCOUNT,
    idFactory: id,
  });
  const durableIdentity = await outbox.initialize();
  await outbox.enqueueMessageWithParent(
    {
      type: 'message.upsert',
      message: {
        message_id: 'message-1',
        chat_id: 'chat-1',
        sender_platform_user_id: 'participant-1',
        text: 'stale private text',
        sent_at: '2026-08-30T10:01:00Z',
        direction: 'inbound',
      },
    },
    {
      type: 'chat.upsert',
      chat: {
        record_kind: 'full',
        chat_id: 'chat-1',
        platform_user_id: 'participant-1',
        display_name: 'Participant',
        updated_at: '2026-08-30T10:00:00Z',
      },
    },
  );
  const stored = await outbox.entries();
  assert.equal(stored.length, 2);

  const sockets = [];
  const acceptedSessions = [];
  const validationErrors = [];
  const reconnectScheduler = scheduler();
  const client = new AgentWebSocketClient({
    creatorAccountId: ACCOUNT,
    authTicket: 'bootstrap-ticket',
    identity: {
      agentInstallationId: INSTALLATION_ID,
      agentStreamId: durableIdentity.agent_stream_id,
      lastAcknowledgedSourceSeq: 0,
      appliedConfigRevision: 'config-retention-1',
    },
    outbox,
    scheduler: reconnectScheduler,
    random: () => 0.5,
    idFactory: id,
    onSession: (value) => acceptedSessions.push(value.connection_id),
    onValidationError: (error) => validationErrors.push(String(error?.stack ?? error)),
    webSocketFactory: (url) => {
      const socket = new MockSocket(url);
      sockets.push(socket);
      return socket;
    },
  });

  client.start();
  const first = sockets[0];
  first.open();
  first.receive(session({
    streamId: durableIdentity.agent_stream_id,
    connectionId: '10000000-0000-4000-8000-000000000081',
    fence: 'fence-retention-1',
    reconnectTicket: 'reconnect-retention-1',
    messageId: id(),
  }));

  const firstAttempt = await waitForIngestDeltas(first, 2);
  assert.equal(firstAttempt.length, 2);
  assert.deepEqual(
    firstAttempt.map((frame) => [frame.payload.source_seq, frame.payload.event_id]),
    stored.map((item) => [item.source_seq, item.event_id]),
  );

  first.drop();
  reconnectScheduler.runReconnect();
  const second = sockets[1];
  second.open();
  second.receive(session({
    streamId: durableIdentity.agent_stream_id,
    connectionId: '10000000-0000-4000-8000-000000000082',
    fence: 'fence-retention-2',
    reconnectTicket: 'reconnect-retention-2',
    messageId: id(),
  }));

  const secondAttempt = await waitForIngestDeltas(second, 2);
  if (secondAttempt.length !== 2) {
    const durableAfterReconnect = await outbox.entries();
    assert.fail(JSON.stringify({
      expected_ingest_deltas: 2,
      actual_ingest_deltas: secondAttempt.length,
      accepted_sessions: acceptedSessions,
      validation_errors: validationErrors,
      client_session_connection_id: client.session?.connection_id ?? null,
      client_socket_is_second: client.socket === second,
      second_socket_ready_state: second.readyState,
      sync_required: client.syncRequired,
      acknowledged_source_seq: client.identity.lastAcknowledgedSourceSeq,
      sent_source_seqs: [...client.sentSourceSeqs],
      flush_pending: client.flushPromise !== null,
      durable_source_seqs: durableAfterReconnect.map((item) => item.source_seq),
      second_frame_types: second.sent.map((frame) => frame.type),
    }, null, 2));
  }
  assert.deepEqual(
    secondAttempt.map((frame) => [frame.payload.source_seq, frame.payload.event_id]),
    firstAttempt.map((frame) => [frame.payload.source_seq, frame.payload.event_id]),
  );
  assert.notEqual(
    secondAttempt[0].payload.connection_id,
    firstAttempt[0].payload.connection_id,
  );
  assert.notEqual(
    secondAttempt[0].payload.fencing_token,
    firstAttempt[0].payload.fencing_token,
  );
  assert.deepEqual(await outbox.entries(), stored);

  client.stop();
});
