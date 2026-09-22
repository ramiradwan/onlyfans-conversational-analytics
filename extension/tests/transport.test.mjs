import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  AgentWebSocketClient,
  LEASE_EXPIRED_CLOSE_CODE,
} from '../transport/agent-websocket.mjs';
import { ReadOnlyAgentWebSocketClient } from '../transport/read-only-agent-websocket.mjs';

import {
  parseAgentToBrainMessage,
  parseBrainToAgentMessage,
} from '../protocol/index.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_ROOT = path.resolve(HERE, '../../shared/fixtures/protocol/v2');
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';
const TEST_ACCOUNT_ID = 'dev-creator-account';
const TEST_AUTH_TICKET = 'test-agent-auth-ticket';

test('protocol clients refuse construction without an explicitly authenticated socket adapter', () => {
  for (const Client of [AgentWebSocketClient, ReadOnlyAgentWebSocketClient]) {
    assert.throws(() => new Client({ creatorAccountId: TEST_ACCOUNT_ID, authTicket: TEST_AUTH_TICKET,
      extensionVersion: '2.0.1', identity: {} }), /authenticated companion socket factory/);
  }
});

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURE_ROOT, `${name}.json`), 'utf8'));
}

class MockSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closeCode = null;
    this.closeReason = null;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
  }

  send(data) {
    this.sent.push(data);
  }

  close(code, reason) {
    this.closeCode = code;
    this.closeReason = reason;
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

function createScheduler() {
  const timeouts = [];
  const intervals = [];
  return {
    timeouts,
    intervals,
    setTimeout(handler, delay) {
      const task = { handler, delay, cleared: false };
      timeouts.push(task);
      return task;
    },
    clearTimeout(task) {
      task.cleared = true;
    },
    setInterval(handler, delay) {
      const task = { handler, delay, cleared: false };
      intervals.push(task);
      return task;
    },
    clearInterval(task) {
      task.cleared = true;
    },
    runNextTimeout() {
      const task = timeouts.find((candidate) => !candidate.cleared);
      assert.ok(task);
      task.cleared = true;
      task.handler();
      return task.delay;
    },
  };
}

function harness(overrides = {}) {
  const sockets = [];
  const scheduler = createScheduler();
  let id = 1;
  const Client = overrides.Client ?? AgentWebSocketClient;
  const client = new Client({
    extensionVersion: '2.0.1',
    creatorAccountId: TEST_ACCOUNT_ID,
    authTicket: TEST_AUTH_TICKET,
    identity: {
      agentInstallationId: INSTALLATION_ID,
      agentStreamId: STREAM_ID,
      lastAcknowledgedSourceSeq: 10,
      appliedConfigRevision: 'config-7',
    },
    scheduler,
    random: () => 0.5,
    now: () => Date.parse('2026-07-18T10:05:00Z'),
    idFactory: () => `90000000-0000-4000-8000-${String(id++).padStart(12, '0')}`,
    webSocketFactory: (url) => {
      const socket = new MockSocket(url);
      sockets.push(socket);
      return socket;
    },
    ...overrides,
  });
  return { client, scheduler, sockets };
}

async function connectAndBind(h) {
  h.client.start();
  const socket = h.sockets[0];
  socket.open();
  const hello = parseAgentToBrainMessage(JSON.parse(socket.sent[0]));
  assert.equal(hello.type, 'agent.hello');
  socket.receive(await fixture('agent.session'));
  return socket;
}

for (const Client of [AgentWebSocketClient, ReadOnlyAgentWebSocketClient]) {
  test(`${Client.name}: wake storms and short sessions cannot bypass backoff or its circuit cooldown`, async () => {
    const h = harness({ Client });
    let socket = await connectAndBind(h);
    for (const delay of [500, 1_000, 2_000, 4_000, 8_000, 300_000]) {
      socket.drop();
      const count = h.sockets.length;
      for (let wake = 0; wake < 100; wake += 1) h.client.reconcileConnection();
      assert.equal(h.sockets.length, count);
      assert.equal(h.scheduler.runNextTimeout(), delay);
      socket = h.sockets.at(-1);
      socket.open();
      socket.receive(await fixture('agent.session'));
    }
    h.client.stop();
    h.client.reconcileConnection();
    assert.equal(h.sockets.length, 7, 'a routine wake cannot restart a stopped transport');
  });
}

test('golden Agent hello/session starts validated heartbeats', async () => {
  let now = Date.parse('2026-07-18T10:05:00Z');
  const h = harness({ now: () => now });
  const socket = await connectAndBind(h);
  const hello = JSON.parse(socket.sent[0]);
  assert.equal(hello.payload.auth_ticket, TEST_AUTH_TICKET);
  assert.equal(hello.payload.requested_creator_account_id, TEST_ACCOUNT_ID);
  assert.equal(hello.payload.capabilities.includes('history.sync'), true);
  assert.equal(h.scheduler.intervals[0].delay, 20_000);

  now += 20_000;
  h.scheduler.intervals[0].handler();
  const heartbeat = parseAgentToBrainMessage(JSON.parse(socket.sent.at(-1)));
  assert.equal(heartbeat.type, 'agent.heartbeat');
  assert.equal(heartbeat.payload.fencing_token, 'fence-42');
});

test('a bootstrap pairing ticket is sent at most once when no session is established', () => {
  const validationErrors = [];
  const h = harness({ onValidationError: (error) => validationErrors.push(error.message) });
  h.client.start();
  const first = h.sockets[0];
  first.open();
  assert.equal(JSON.parse(first.sent[0]).payload.auth_ticket, TEST_AUTH_TICKET);

  first.drop();
  assert.equal(h.scheduler.runNextTimeout(), 500);
  const second = h.sockets[1];
  second.open();

  assert.equal(second.sent.length, 0);
  // Browsers reject reserved WebSocket close codes. The transport maps the
  // protocol's 1008 policy-violation outcome to its application-safe 4008.
  assert.equal(second.closeCode, 4008);
  assert.equal(second.closeReason, 'Agent reconnect credential unavailable');
  assert.deepEqual(validationErrors, ['No reusable Agent reconnect credential is available']);
  assert.equal(h.scheduler.timeouts.filter((task) => !task.cleared).length, 0);
});

test('a persisted reconnect ticket is preferred to the bootstrap pairing ticket', () => {
  const h = harness({ reconnectAuthTicket: 'persisted-reconnect-ticket' });
  h.client.start();
  h.sockets[0].open();
  const hello = parseAgentToBrainMessage(JSON.parse(h.sockets[0].sent[0]));
  assert.equal(hello.payload.auth_ticket, 'persisted-reconnect-ticket');
  assert.notEqual(hello.payload.auth_ticket, TEST_AUTH_TICKET);
});

test('heartbeat activity is scoped to a bound live session and stops on disconnect', async () => {
  const losses = [];
  const h = harness({ onSessionLost: (event) => losses.push(event.reason) });
  h.client.start();
  const socket = h.sockets[0];
  assert.equal(h.scheduler.intervals.length, 0);
  socket.open();
  assert.equal(h.scheduler.intervals.length, 0);

  socket.receive(await fixture('agent.session'));
  assert.equal(h.scheduler.intervals.length, 1);
  assert.equal(h.scheduler.intervals[0].cleared, false);

  socket.drop();
  assert.equal(h.scheduler.intervals[0].cleared, true);
  assert.deepEqual(losses, ['disconnected']);
});

test('wake reconciliation sends one heartbeat only when the negotiated interval is due', async () => {
  let now = Date.parse('2026-07-18T10:05:00Z');
  const h = harness({ now: () => now });
  const socket = await connectAndBind(h);
  const initialFrames = socket.sent.length;

  assert.equal(h.client.reconcileConnection(), false);
  now += 19_999;
  assert.equal(h.client.reconcileConnection(), false);
  assert.equal(socket.sent.length, initialFrames);

  now += 1;
  assert.equal(h.client.reconcileConnection(), true);
  assert.equal(socket.sent.length, initialFrames + 1);
  assert.equal(JSON.parse(socket.sent.at(-1)).type, 'agent.heartbeat');
  assert.equal(h.client.reconcileConnection(), false);
  assert.equal(socket.sent.length, initialFrames + 1);
});

test('connection drop rotates and persists reconnect auth separately from config auth', async () => {
  const persisted = [];
  const configBindings = [];
  const configClears = [];
  const h = harness({
    persistReconnectAuthTicket: async (ticket) => { persisted.push(ticket); },
    configClient: {
      activeDocument: null,
      bindSessionAuthorization(ticket) { configBindings.push(ticket); },
      clearSessionAuthorization() { configClears.push(true); },
      async requireConfig() {},
    },
  });
  const first = await connectAndBind(h);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(persisted, ['agent-reconnect-ticket-42']);
  assert.deepEqual(configBindings, ['agent-config-ticket-42']);
  first.drop();
  assert.equal(h.scheduler.timeouts.find((task) => !task.cleared).delay, 500);
  h.scheduler.runNextTimeout();
  assert.equal(h.sockets.length, 2);

  const second = h.sockets[1];
  second.open();
  const secondHello = JSON.parse(second.sent[0]);
  assert.equal(secondHello.type, 'agent.hello');
  assert.equal(secondHello.payload.auth_ticket, 'agent-reconnect-ticket-42');
  assert.notEqual(secondHello.payload.auth_ticket, 'agent-config-ticket-42');
  const session = await fixture('agent.session');
  session.payload.connection_id = '10000000-0000-4000-8000-000000000099';
  session.payload.fencing_token = 'fence-99';
  session.payload.reconnect_auth_ticket = 'agent-reconnect-ticket-43';
  session.payload.config_auth_ticket = 'agent-config-ticket-43';
  second.receive(session);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.client.session.connection_id, session.payload.connection_id);
  assert.equal(h.client.session.fencing_token, 'fence-99');
  assert.deepEqual(persisted, ['agent-reconnect-ticket-42', 'agent-reconnect-ticket-43']);
  assert.deepEqual(configBindings, ['agent-config-ticket-42', 'agent-config-ticket-43']);
  assert.equal(configClears.length, 1);

  second.drop();
  h.scheduler.runNextTimeout();
  const third = h.sockets[2];
  third.open();
  const thirdHello = JSON.parse(third.sent[0]);
  assert.equal(thirdHello.payload.auth_ticket, 'agent-reconnect-ticket-43');
  assert.notEqual(thirdHello.payload.auth_ticket, 'agent-config-ticket-43');
});

test('reconnect-ticket persistence failure closes the session without retrying', async () => {
  const validationErrors = [];
  const h = harness({
    persistReconnectAuthTicket: async () => { throw new Error('session storage unavailable'); },
    onValidationError: (error) => validationErrors.push(error.message),
  });
  const socket = await connectAndBind(h);
  await new Promise((resolve) => setImmediate(resolve));

  // 1011 is a reserved close code, so the browser-safe transport close is 4011.
  assert.equal(socket.closeCode, 4011);
  assert.equal(socket.closeReason, 'Agent reconnect credential could not be stored');
  assert.deepEqual(validationErrors, ['session storage unavailable']);
  assert.equal(h.client.session, null);
  assert.equal(h.scheduler.timeouts.filter((task) => !task.cleared).length, 0);
});

test('validated ack, sync, config, command, and result-ack dispatch is correlated', async () => {
  const acknowledged = [];
  const syncs = [];
  const configs = [];
  const resultAcks = [];
  const saved = [];
  const h = harness({
    persistence: { saveAcknowledgedSourceSeq: async (seq) => saved.push(seq) },
    onIngestAcknowledged: (payload) => acknowledged.push(payload),
    onSyncRequired: (payload) => syncs.push(payload),
    onConfigAvailable: (payload) => configs.push(payload),
    onCommand: async () => ({
      status: 'succeeded',
      output: { external_message_id: 'platform-message-9' },
      error: null,
    }),
    onCommandResultAcknowledged: (payload) => resultAcks.push(payload),
  });
  const socket = await connectAndBind(h);

  socket.receive(await fixture('ingest.ack'));
  await new Promise((resolve) => setImmediate(resolve));
  socket.receive(await fixture('sync.required'));
  socket.receive(await fixture('config.available'));
  socket.receive(await fixture('command.execute'));
  await new Promise((resolve) => setImmediate(resolve));

  const result = parseAgentToBrainMessage(JSON.parse(socket.sent.at(-1)));
  assert.equal(result.type, 'command.result');
  assert.equal(result.payload.command_id, '70000000-0000-4000-8000-000000000001');
  assert.equal(result.correlation_id, '00000000-0000-4000-8000-000000000023');

  socket.receive(await fixture('command.result.ack'));
  assert.deepEqual(saved, [11]);
  assert.equal(acknowledged.length, 1);
  assert.ok(syncs.length >= 1);
  assert.equal(configs.length, 1);
  assert.equal(resultAcks.length, 1);
});

test('stale-fence rejection closes the session and schedules a fresh handshake', async () => {
  const h = harness();
  const socket = await connectAndBind(h);
  const rejected = await fixture('ingest.rejected');
  rejected.payload.code = 'stale_fence';
  rejected.payload.retryable = false;
  rejected.payload.detail = 'The Agent connection no longer owns the active fencing token';
  socket.receive(rejected);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(socket.closeCode, LEASE_EXPIRED_CLOSE_CODE);
  assert.equal(socket.closeReason, 'Agent lease fencing token is stale');
  assert.equal(h.scheduler.timeouts.filter((task) => !task.cleared).length, 1);
  h.scheduler.runNextTimeout();
  assert.equal(h.sockets.length, 2);
  h.sockets[1].open();
  assert.equal(JSON.parse(h.sockets[1].sent[0]).type, 'agent.hello');
});

test('outbound snapshot and presence are fenced and protocol validated', async () => {
  const h = harness();
  const socket = await connectAndBind(h);
  const snapshot = await fixture('ingest.snapshot');
  const snapshotPayload = { ...snapshot.payload };
  delete snapshotPayload.connection_id;
  delete snapshotPayload.fencing_token;
  delete snapshotPayload.creator_account_id;
  delete snapshotPayload.agent_installation_id;
  delete snapshotPayload.agent_stream_id;
  assert.equal(h.client.sendSnapshot(snapshotPayload), true);
  assert.equal(parseAgentToBrainMessage(JSON.parse(socket.sent.at(-1))).type, 'ingest.snapshot');

  const observed = await fixture('presence.observed');
  const observedPayload = { ...observed.payload };
  delete observedPayload.connection_id;
  delete observedPayload.fencing_token;
  delete observedPayload.creator_account_id;
  assert.equal(h.client.sendPresenceObservation(observedPayload), true);
  assert.equal(parseAgentToBrainMessage(JSON.parse(socket.sent.at(-1))).type, 'presence.observed');
});

test('invalid fixtures and fatal protocol errors close safely without crashing', async () => {
  const validationErrors = [];
  const h = harness({ onValidationError: (error) => validationErrors.push(error) });
  const socket = await connectAndBind(h);
  const invalid = JSON.parse(
    await readFile(path.join(FIXTURE_ROOT, 'invalid/malformed-discriminator.unknown-command.json'), 'utf8'),
  );
  socket.receive(invalid);
  // 1002 is reserved by the WebSocket API and is represented on the wire as 4002.
  assert.equal(socket.closeCode, 4002);
  assert.equal(validationErrors.length, 1);

  const fatalHarness = harness();
  const fatalSocket = fatalHarness.sockets[0] ?? (fatalHarness.client.start(), fatalHarness.sockets[0]);
  fatalSocket.open();
  const error = await fixture('protocol.error');
  error.payload.fatal = true;
  error.payload.retryable = false;
  fatalSocket.receive(error);
  assert.equal(fatalSocket.closeCode, 4002);
  assert.equal(fatalHarness.scheduler.timeouts.filter((task) => !task.cleared).length, 0);
});


test('MV3 manifest grants the alarms permission used for reconciliation', async () => {
  const manifest = JSON.parse(
    await readFile(new URL('../manifest.json', import.meta.url), 'utf8'),
  );
  assert.ok(manifest.permissions.includes('alarms'));
});

test('all Brain-to-Agent fixtures remain accepted before client routing', async () => {
  const names = [
    'agent.session',
    'sync.required',
    'ingest.ack',
    'ingest.rejected',
    'protocol.error',
    'config.available',
    'command.execute',
    'command.result.ack',
  ];
  for (const name of names) assert.ok(parseBrainToAgentMessage(await fixture(name)));
});


for (const Client of [AgentWebSocketClient, ReadOnlyAgentWebSocketClient]) {
  test(`${Client.name}: a large replay stays bounded and resumes on acknowledgements`, async () => {
    const delta = (await fixture('ingest.delta')).payload;
    const entries = Array.from({ length: 20 }, (_, index) => ({
      ...delta,
      source_seq: 11 + index,
      event_id: `50000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
    }));
    let committed = 10;
    const errors = [];
    const h = harness({
      Client,
      onValidationError: (error) => errors.push(error),
      outbox: {
        async entriesPage(after, limit) {
          return entries.filter((item) => item.source_seq > Math.max(after, committed)).slice(0, limit);
        },
        async acknowledge(sequence) {
          committed = Math.max(committed, sequence);
          return { snapshotAcknowledged: false };
        },
      },
    });
    const sent = (socket) => socket.sent.map((raw) => JSON.parse(raw))
      .filter((frame) => frame.type === 'ingest.delta').map((frame) => frame.payload.source_seq);
    const settle = () => new Promise((resolve) => setImmediate(resolve));
    const first = await connectAndBind(h);
    await settle();
    await Promise.all([h.client.flushOutbox(), h.client.flushOutbox()]);
    assert.deepEqual(sent(first), [11, 12, 13, 14]);

    // A connection loss must replay unacknowledged records, without losing the window.
    first.drop();
    h.scheduler.runNextTimeout();
    const replacement = h.sockets[1];
    replacement.open();
    replacement.receive(await fixture('agent.session'));
    await settle();
    assert.deepEqual(sent(replacement), [11, 12, 13, 14]);

    for (let sequence = 11; sequence <= 30; sequence += 1) {
      assert.ok(sent(replacement).includes(sequence));
      const acknowledgement = await fixture('ingest.ack');
      acknowledgement.payload.committed_source_seq = sequence;
      replacement.receive(acknowledgement);
      await settle();
      assert.ok(sent(replacement).filter((value) => value > committed).length <= 4);
    }
    assert.deepEqual(sent(replacement), entries.map((item) => item.source_seq));
    assert.equal(h.client.identity.lastAcknowledgedSourceSeq, 30);
    assert.equal(h.client.sentSourceSeqs.size, 0);
    assert.equal(replacement.closeCode, null);
    assert.deepEqual(errors, []);
    h.client.stop();
  });

  test(`${Client.name}: direct construction enforces the secure endpoint and explicit version`, () => {
    assert.throws(() => harness({ Client, url: 'ws://bridge.localhost:17871/ws/agent' }), /invalid_agent_websocket_endpoint/);
    assert.throws(() => harness({ Client, url: 'wss://bridge.localhost:17871/ws/agent?ticket=secret' }), /invalid_agent_websocket_endpoint/);
    assert.throws(() => harness({ Client, extensionVersion: undefined }), /explicit extension version/);
  });

  test(`${Client.name}: the session deadline clears on acceptance and expires a stalled handshake`, async () => {
    const h = harness({ Client });
    await connectAndBind(h);
    assert.equal(h.scheduler.timeouts.filter((task) => !task.cleared).length, 0);
    h.client.stop();
    const stalled = harness({ Client });
    stalled.client.start();
    assert.equal(stalled.scheduler.runNextTimeout(), 10_000);
    assert.equal(stalled.sockets[0].closeCode, 4008);
    stalled.client.stop();
    assert.equal(stalled.scheduler.timeouts.filter((task) => !task.cleared).length, 0);
  });

  test(`${Client.name}: a delayed acknowledgement cannot commit after its connection closes`, async () => {
    let resume;
    let entered;
    let committed = false;
    let persistenceWrites = 0;
    const gate = new Promise((resolve) => { resume = resolve; });
    const started = new Promise((resolve) => { entered = resolve; });
    const h = harness({
      Client,
      outbox: {
        async entries() { return []; },
        async acknowledge(_seq, _snapshot, _progress, controls) {
          entered();
          await gate;
          controls.assertCurrent();
          committed = true;
          return { snapshotAcknowledged: false };
        },
      },
      persistence: { async saveAcknowledgedSourceSeq() { persistenceWrites += 1; } },
    });
    const socket = await connectAndBind(h);
    socket.receive(await fixture('ingest.ack'));
    await started;
    socket.drop();
    resume();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(committed, false);
    assert.equal(persistenceWrites, 0);
    assert.equal(h.client.identity.lastAcknowledgedSourceSeq, 10);
    h.client.stop();
  });
}
