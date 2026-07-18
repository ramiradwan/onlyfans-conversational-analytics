import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  AgentCommandService,
  UnsupportedCommandExecutor,
} from '../transport/agent-command-service.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { createChromeAdapter } from '../transport/chrome-adapter.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_ROOT = path.resolve(HERE, '../../shared/fixtures/protocol/v1');
const NOW = Date.parse('2026-07-18T10:05:00Z');
const clone = (value) => JSON.parse(JSON.stringify(value));

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURE_ROOT, `${name}.json`), 'utf8'));
}

function appliedConfig(allowedActions = ['message.send']) {
  return {
    config_revision: 'config-7',
    command_policy: {
      allowed_actions: allowedActions,
      max_text_length: 1000,
      require_idempotency: true,
    },
  };
}

function session(overrides = {}) {
  return {
    connection_id: '10000000-0000-4000-8000-000000000001',
    fencing_token: 'fence-42',
    creator_account_id: 'dev-creator-account',
    ...overrides,
  };
}

function memoryPersistence(initial = null) {
  let saved = initial === null ? null : clone(initial);
  const writes = [];
  return {
    writes,
    async loadCommandState() {
      return saved === null ? null : clone(saved);
    },
    async saveCommandState(state) {
      saved = clone(state);
      writes.push(clone(state));
    },
    current() {
      return saved === null ? null : clone(saved);
    },
  };
}

function ids() {
  let next = 1;
  return () => `90000000-0000-4000-8000-${String(next++).padStart(12, '0')}`;
}

test('applied config allow-list gates execution and passes only the typed action', async () => {
  const execute = await fixture('command.execute');
  const actions = [];
  const allowed = new AgentCommandService({
    persistence: memoryPersistence(),
    appliedConfig: () => appliedConfig(),
    executor: {
      async execute(action) {
        actions.push(clone(action));
        return {
          status: 'succeeded',
          output: { external_message_id: 'platform-message-9' },
          error: null,
        };
      },
    },
    now: () => NOW,
    idFactory: ids(),
  });

  const result = await allowed.execute(execute.payload, session(), execute.message_id);
  assert.equal(result.status, 'succeeded');
  assert.deepEqual(actions, [execute.payload.action]);

  execute.payload.command_id = '70000000-0000-4000-8000-000000000002';
  const refusedActions = [];
  const refused = new AgentCommandService({
    persistence: memoryPersistence(),
    appliedConfig: () => appliedConfig([]),
    executor: { async execute(action) { refusedActions.push(action); } },
    now: () => NOW,
    idFactory: ids(),
  });
  const rejection = await refused.execute(execute.payload, session(), execute.message_id);
  assert.equal(rejection.status, 'failed');
  assert.equal(rejection.error.code, 'rejected');
  assert.match(rejection.error.detail, /applied configuration/);
  assert.equal(refusedActions.length, 0);
});

test('account, fence, and deadline refusals follow the required validation order', async () => {
  const execute = await fixture('command.execute');
  let executions = 0;
  const service = new AgentCommandService({
    persistence: memoryPersistence(),
    appliedConfig: () => appliedConfig(),
    executor: { async execute() { executions += 1; } },
    now: () => NOW,
    idFactory: ids(),
  });

  const wrongBoth = clone(execute.payload);
  wrongBoth.creator_account_id = 'wrong-account';
  wrongBoth.fencing_token = 'wrong-fence';
  const account = await service.execute(wrongBoth, session(), execute.message_id);
  assert.match(account.error.detail, /account/);

  const wrongFence = clone(execute.payload);
  wrongFence.command_id = '70000000-0000-4000-8000-000000000002';
  wrongFence.fencing_token = 'wrong-fence';
  const fence = await service.execute(wrongFence, session(), execute.message_id);
  assert.match(fence.error.detail, /fencing token/);

  const expired = clone(execute.payload);
  expired.command_id = '70000000-0000-4000-8000-000000000003';
  expired.deadline = new Date(NOW).toISOString();
  const deadline = await service.execute(expired, session(), execute.message_id);
  assert.equal(deadline.error.code, 'deadline_exceeded');
  assert.equal(executions, 0);
});

test('duplicate command_id executes once, including concurrent delivery', async () => {
  const execute = await fixture('command.execute');
  const persistence = memoryPersistence();
  let executions = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const service = new AgentCommandService({
    persistence,
    appliedConfig: () => appliedConfig(),
    executor: {
      async execute(action) {
        executions += 1;
        assert.deepEqual(action, execute.payload.action);
        await gate;
        return { status: 'accepted', output: null, error: null };
      },
    },
    now: () => NOW,
    idFactory: ids(),
  });

  const first = service.execute(execute.payload, session(), execute.message_id);
  const duplicate = service.execute(execute.payload, session(), execute.message_id);
  release();
  const [firstResult, duplicateResult] = await Promise.all([first, duplicate]);
  assert.equal(executions, 1);
  assert.deepEqual(duplicateResult, firstResult);
  assert.equal(persistence.current().pending_command_ids.length, 1);
});

test('terminal result and dedup survive restart; only a covering ack compacts pending replay', async () => {
  const execute = await fixture('command.execute');
  const persistence = memoryPersistence();
  const original = new AgentCommandService({
    persistence,
    appliedConfig: () => appliedConfig(),
    executor: {
      async execute() {
        return { status: 'succeeded', output: { external_message_id: null }, error: null };
      },
    },
    now: () => NOW,
    idFactory: ids(),
  });
  const result = await original.execute(execute.payload, session(), execute.message_id);

  let restartedExecutions = 0;
  const restarted = new AgentCommandService({
    persistence,
    appliedConfig: () => appliedConfig(),
    executor: { async execute() { restartedExecutions += 1; } },
    now: () => NOW,
    idFactory: ids(),
  });
  assert.equal((await restarted.pendingResults()).length, 1);
  assert.deepEqual(
    await restarted.execute(execute.payload, session(), execute.message_id),
    result,
  );
  assert.equal(restartedExecutions, 0);

  const wrongAck = {
    command_id: result.command_id,
    creator_account_id: 'dev-creator-account',
    result_id: '80000000-0000-4000-8000-000000000099',
  };
  assert.equal(await restarted.acknowledge(wrongAck), false);
  assert.equal((await restarted.pendingResults()).length, 1);

  assert.equal(await restarted.acknowledge({
    command_id: result.command_id,
    creator_account_id: 'dev-creator-account',
    result_id: result.result_id,
  }), true);
  assert.equal((await restarted.pendingResults()).length, 0);
  assert.ok(persistence.current().records[result.command_id]);
  assert.deepEqual(
    await restarted.execute(execute.payload, session(), execute.message_id),
    result,
  );
  assert.equal(restartedExecutions, 0);
  assert.deepEqual(await restarted.storedResult(result.command_id), result);
});

test('default executor provides a typed unsupported outcome without platform action', async () => {
  const execute = await fixture('command.execute');
  const service = new AgentCommandService({
    persistence: memoryPersistence(),
    appliedConfig: () => appliedConfig(),
    executor: new UnsupportedCommandExecutor(),
    now: () => NOW,
    idFactory: ids(),
  });
  const result = await service.execute(execute.payload, session(), execute.message_id);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'execution_error');
  assert.match(result.error.detail, /unsupported/);
});

class MockSocket {
  constructor() {
    this.readyState = 0;
    this.sent = [];
  }

  send(value) {
    this.sent.push(JSON.parse(value));
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
    setTimeout: () => ({}),
    clearTimeout() {},
    setInterval: () => ({}),
    clearInterval() {},
  };
}

function websocketClient(persistence, executor, socket, idFactory, config = appliedConfig()) {
  return new AgentWebSocketClient({
    identity: {
      agentInstallationId: '20000000-0000-4000-8000-000000000001',
      agentStreamId: '30000000-0000-4000-8000-000000000001',
      lastAcknowledgedSourceSeq: 10,
      appliedConfigRevision: 'config-7',
    },
    persistence,
    executor,
    configClient: {
      activeDocument: config,
      async requireConfig() {},
    },
    scheduler: scheduler(),
    now: () => NOW,
    idFactory,
    webSocketFactory: () => socket,
  });
}

async function bind(client, socket, sessionDocument) {
  client.start();
  socket.open();
  socket.receive(sessionDocument);
  await new Promise((resolve) => setImmediate(resolve));
}

test('WebSocket command routing uses the Phase 5 client applied document as authority', async () => {
  const socket = new MockSocket();
  const persistence = memoryPersistence();
  const executor = {
    calls: 0,
    async execute() {
      this.calls += 1;
      return { status: 'succeeded', output: null, error: null };
    },
  };
  const client = websocketClient(
    persistence,
    executor,
    socket,
    ids(),
    appliedConfig([]),
  );
  await bind(client, socket, await fixture('agent.session'));
  socket.receive(await fixture('command.execute'));
  await new Promise((resolve) => setImmediate(resolve));

  const result = socket.sent.find((message) => message.type === 'command.result');
  assert.ok(result);
  assert.equal(result.payload.status, 'failed');
  assert.equal(result.payload.error.code, 'rejected');
  assert.equal(executor.calls, 0);
});

test('unacknowledged result is rebound and resent after worker restarts until acked', async () => {
  const persistence = memoryPersistence();
  const idFactory = ids();
  const execute = await fixture('command.execute');
  const firstSession = await fixture('agent.session');
  const executor = {
    calls: 0,
    async execute() {
      this.calls += 1;
      return {
        status: 'succeeded',
        output: { external_message_id: 'platform-message-9' },
        error: null,
      };
    },
  };

  const firstSocket = new MockSocket();
  const first = websocketClient(persistence, executor, firstSocket, idFactory);
  await bind(first, firstSocket, firstSession);
  firstSocket.receive(execute);
  await new Promise((resolve) => setImmediate(resolve));
  const initialResult = firstSocket.sent.find((message) => message.type === 'command.result');
  assert.ok(initialResult);
  assert.equal(executor.calls, 1);
  first.stop();

  const secondSocket = new MockSocket();
  const secondSession = clone(firstSession);
  secondSession.payload.connection_id = '10000000-0000-4000-8000-000000000002';
  secondSession.payload.fencing_token = 'fence-43';
  const second = websocketClient(persistence, executor, secondSocket, idFactory);
  await bind(second, secondSocket, secondSession);
  const resent = secondSocket.sent.find((message) => message.type === 'command.result');
  assert.ok(resent);
  assert.equal(resent.payload.result_id, initialResult.payload.result_id);
  assert.equal(resent.payload.connection_id, secondSession.payload.connection_id);
  assert.equal(resent.payload.fencing_token, secondSession.payload.fencing_token);
  assert.equal(executor.calls, 1);
  second.stop();

  const thirdSocket = new MockSocket();
  const thirdSession = clone(firstSession);
  thirdSession.payload.connection_id = '10000000-0000-4000-8000-000000000003';
  thirdSession.payload.fencing_token = 'fence-44';
  const third = websocketClient(persistence, executor, thirdSocket, idFactory);
  await bind(third, thirdSocket, thirdSession);
  assert.ok(thirdSocket.sent.some((message) => message.type === 'command.result'));

  const ack = await fixture('command.result.ack');
  ack.payload.connection_id = thirdSession.payload.connection_id;
  ack.payload.command_id = initialResult.payload.command_id;
  ack.payload.result_id = initialResult.payload.result_id;
  thirdSocket.receive(ack);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(persistence.current().pending_command_ids, []);
  third.stop();

  const fourthSocket = new MockSocket();
  const fourthSession = clone(firstSession);
  fourthSession.payload.connection_id = '10000000-0000-4000-8000-000000000004';
  fourthSession.payload.fencing_token = 'fence-45';
  const fourth = websocketClient(persistence, executor, fourthSocket, idFactory);
  await bind(fourth, fourthSocket, fourthSession);
  assert.equal(
    fourthSocket.sent.filter((message) => message.type === 'command.result').length,
    0,
  );
});

test('Chrome adapter stores durable command state behind chrome.storage.local', async () => {
  const values = {};
  const chromeMock = {
    runtime: {},
    storage: {
      local: {
        get(keys, callback) {
          callback(Object.fromEntries(
            keys.filter((key) => key in values).map((key) => [key, clone(values[key])]),
          ));
        },
        set(update, callback) {
          Object.assign(values, clone(update));
          callback?.();
        },
      },
    },
  };
  const adapter = createChromeAdapter(chromeMock);
  const state = { version: 1, records: {}, pending_command_ids: [] };
  await adapter.saveCommandState(state);
  assert.deepEqual(await adapter.loadCommandState(), state);
  assert.deepEqual(values.durable_command_results_v1, state);
});
