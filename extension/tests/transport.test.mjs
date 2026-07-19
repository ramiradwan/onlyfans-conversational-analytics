import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  AgentWebSocketClient,
  LEASE_EXPIRED_CLOSE_CODE,
} from '../transport/agent-websocket.mjs';
import {
  RECONCILE_ALARM_NAME,
  createChromeAdapter,
} from '../transport/chrome-adapter.mjs';
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
  const client = new AgentWebSocketClient({
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
  assert.equal(second.closeCode, 1008);
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
  assert.equal(h.scheduler.timeouts[0].delay, 500);
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

  assert.equal(socket.closeCode, 1011);
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
  assert.equal(h.scheduler.timeouts.length, 1);
  h.scheduler.runNextTimeout();
  assert.equal(h.sockets.length, 2);
  h.sockets[1].open();
  assert.equal(JSON.parse(h.sockets[1].sent[0]).type, 'agent.hello');
});

test('outbound snapshot and presence are fenced and Phase 1 validated', async () => {
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
  assert.equal(socket.closeCode, 1002);
  assert.equal(validationErrors.length, 1);

  const fatalHarness = harness();
  const fatalSocket = fatalHarness.sockets[0] ?? (fatalHarness.client.start(), fatalHarness.sockets[0]);
  fatalSocket.open();
  const error = await fixture('protocol.error');
  error.payload.fatal = true;
  error.payload.retryable = false;
  fatalSocket.receive(error);
  assert.equal(fatalSocket.closeCode, 1002);
  assert.equal(fatalHarness.scheduler.timeouts.length, 0);
});

test('Chrome adapter persists only stable installation identity and exposes wake events', async () => {
  const values = {};
  const listeners = [];
  const alarmListeners = [];
  const alarms = [];
  const event = {
    addListener(listener) { listeners.push(listener); },
    removeListener() {},
  };
  const chromeMock = {
    runtime: { onStartup: event, onInstalled: event, onMessage: event },
    tabs: { onUpdated: event },
    alarms: {
      create(name, options) { alarms.push({ name, options }); },
      onAlarm: {
        addListener(listener) { alarmListeners.push(listener); },
        removeListener() {},
      },
    },
    storage: {
      local: {
        get(_keys, callback) { callback({ ...values }); },
        set(update, callback) { Object.assign(values, update); callback?.(); },
      },
    },
  };
  let generated = 0;
  const adapter = createChromeAdapter(
    chromeMock,
    () => `90000000-0000-4000-8000-${String(++generated).padStart(12, '0')}`,
  );
  const identity = await adapter.loadAgentIdentity();
  assert.equal(identity.agentInstallationId, '90000000-0000-4000-8000-000000000001');
  assert.deepEqual(Object.keys(values), ['agent_installation_id']);
  assert.equal(adapter.saveAcknowledgedSourceSeq, undefined);
  assert.equal(adapter.saveCommandState, undefined);

  let wakes = 0;
  adapter.onWake(() => { wakes += 1; });
  assert.deepEqual(alarms, [{
    name: RECONCILE_ALARM_NAME,
    options: { delayInMinutes: 1, periodInMinutes: 1 },
  }]);
  listeners[0]();
  assert.equal(wakes, 1);
  alarmListeners[0]({ name: 'unrelated-alarm' });
  assert.equal(wakes, 1);
  alarmListeners[0]({ name: RECONCILE_ALARM_NAME });
  assert.equal(wakes, 2);
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
