import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  AgentConfigClient,
  AtomicConfigActivator,
  calculateConfigDigest,
} from '../transport/agent-config-client.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { createChromeAdapter } from '../transport/chrome-adapter.mjs';
import { createConfigHttpAdapter } from '../transport/config-http-adapter.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_ROOT = path.resolve(HERE, '../../shared/fixtures/protocol/v2');
const INSTALLATION_ID = '20000000-0000-4000-8000-000000000001';
const STREAM_ID = '30000000-0000-4000-8000-000000000001';
const ACCOUNT_ID = 'dev-creator-account';

const clone = (value) => JSON.parse(JSON.stringify(value));

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURE_ROOT, `${name}.json`), 'utf8'));
}

async function configDocument(revision, overrides = {}) {
  const document = {
    operation: 'agent.config.document',
    protocol_version: '2',
    creator_account_id: ACCOUNT_ID,
    config_revision: revision,
    config_schema_version: '2',
    digest: `sha256:${'0'.repeat(64)}`,
    etag: revision,
    issued_at: '2026-07-18T10:00:00Z',
    capture_policy: {
      observation_interval_seconds: 30,
      rules: [
        {
          resource: 'chats',
          url_pattern: '/api2/v2/chats',
          enabled: true,
        },
        {
          resource: 'messages',
          url_pattern: '/api2/v2/chats/*/messages',
          enabled: true,
        },
      ],
    },
    command_policy: {
      allowed_actions: ['message.send'],
      max_text_length: 1000,
      require_idempotency: true,
    },
    history_acquisition: {
      enabled: false,
      consent_revision: null,
      authorized_platform_creator_id: null,
      recent_window_days: 30,
      page_size: 50,
      pages_per_wake: 2,
      request_interval_ms: 1000,
      retry_limit: 3,
    },
    ...overrides,
  };
  document.digest = await calculateConfigDigest(document);
  return document;
}

function memoryPersistence(initial = null) {
  let saved = initial === null ? null : clone(initial);
  const writes = [];
  return {
    writes,
    async loadAppliedConfig() {
      return saved === null ? null : clone(saved);
    },
    async saveAppliedConfig(document) {
      saved = clone(document);
      writes.push(clone(document));
    },
    current() {
      return saved === null ? null : clone(saved);
    },
  };
}

function scheduler() {
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
  };
}

async function clientHarness({
  initial,
  response,
  fetchConfig,
  identityRevision = initial?.config_revision ?? null,
  capabilities,
} = {}) {
  const identity = {
    agentInstallationId: INSTALLATION_ID,
    agentStreamId: STREAM_ID,
    lastAcknowledgedSourceSeq: 10,
    appliedConfigRevision: identityRevision,
  };
  const persistence = memoryPersistence(initial ?? null);
  const activator = new AtomicConfigActivator();
  const reports = [];
  const retryScheduler = scheduler();
  const requests = [];
  const http = {
    async fetchConfig(request) {
      requests.push(clone(request));
      if (fetchConfig) return fetchConfig(request);
      return {
        status: 200,
        etag: response.etag,
        document: clone(response),
      };
    },
  };
  const client = new AgentConfigClient({
    identity,
    creatorAccountId: ACCOUNT_ID,
    configAuthTicket: 'agent-config-ticket-42',
    http,
    persistence,
    activator,
    scheduler: retryScheduler,
    ...(capabilities ? { capabilities } : {}),
    reportApplied: (report) => reports.push(clone(report)),
  });
  await client.initialize();
  return {
    client,
    identity,
    persistence,
    activator,
    reports,
    scheduler: retryScheduler,
    requests,
  };
}

test('valid configuration activates as one document, persists, and reports applied', async () => {
  const oldDocument = await configDocument('config-7');
  const nextDocument = await configDocument('config-8', {
    capture_policy: {
      observation_interval_seconds: 60,
      rules: [
        {
          resource: 'presence',
          url_pattern: '/api2/v2/users/list',
          enabled: true,
        },
      ],
    },
  });
  nextDocument.digest = await calculateConfigDigest(nextDocument);
  const h = await clientHarness({ initial: oldDocument, response: nextDocument });

  const result = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: nextDocument.digest,
  });

  assert.equal(result.status, 'applied');
  assert.equal(h.identity.appliedConfigRevision, 'config-8');
  assert.deepEqual(h.activator.current(), nextDocument);
  assert.deepEqual(h.persistence.current(), nextDocument);
  assert.equal(h.persistence.writes.length, 1);
  assert.equal(h.reports.at(-1).outcome, 'applied');
  assert.equal(h.reports.at(-1).config_revision, 'config-8');
  assert.equal(
    h.reports.at(-1).capabilities.some(({ capability }) => capability === 'history.sync'),
    true,
  );
});

test('digest mismatch and unsupported schema retain the last known good document', async () => {
  const good = await configDocument('config-7');
  const mismatch = await configDocument('config-8');
  mismatch.capture_policy.observation_interval_seconds = 90;
  const mismatched = await clientHarness({ initial: good, response: mismatch });
  const mismatchResult = await mismatched.client.requireConfig({
    required_config_revision: 'config-8',
    digest: mismatch.digest,
  });

  assert.equal(mismatchResult.status, 'failed');
  assert.equal(mismatched.identity.appliedConfigRevision, 'config-7');
  assert.deepEqual(mismatched.activator.current(), good);
  assert.deepEqual(mismatched.persistence.current(), good);
  assert.equal(mismatched.reports.at(-1).outcome, 'rejected');
  assert.equal(mismatched.scheduler.timeouts[0].delay, 1_000);

  const unsupported = await configDocument('config-8', {
    config_schema_version: '3',
  });
  unsupported.digest = await calculateConfigDigest(unsupported);
  const schema = await clientHarness({ initial: good, response: unsupported });
  const schemaResult = await schema.client.requireConfig({
    required_config_revision: 'config-8',
    digest: unsupported.digest,
  });

  assert.equal(schemaResult.status, 'failed');
  assert.equal(schema.identity.appliedConfigRevision, 'config-7');
  assert.deepEqual(schema.activator.current(), good);
  assert.equal(schema.reports.at(-1).outcome, 'rejected');
});

test('timeout and 5xx retain last known good and schedule bounded retry', async () => {
  const good = await configDocument('config-7');
  const timeout = await clientHarness({
    initial: good,
    fetchConfig: async () => {
      const error = new Error('Agent configuration fetch timed out');
      error.code = 'timeout';
      throw error;
    },
  });
  await timeout.client.requireConfig({
    required_config_revision: 'config-8',
    digest: null,
  });
  assert.equal(timeout.identity.appliedConfigRevision, 'config-7');
  assert.equal(timeout.client.healthSummary().status, 'degraded');
  assert.equal(timeout.scheduler.timeouts[0].delay, 1_000);
  assert.equal(timeout.reports.at(-1).outcome, 'degraded');

  const server = await clientHarness({
    initial: good,
    fetchConfig: async () => ({ status: 503, etag: null, document: null }),
  });
  await server.client.requireConfig({
    required_config_revision: 'config-8',
    digest: null,
  });
  assert.equal(server.identity.appliedConfigRevision, 'config-7');
  assert.equal(server.scheduler.timeouts[0].delay, 1_000);
  assert.deepEqual(server.activator.current(), good);
});

test('restart activates the persisted validated document without refetching', async () => {
  const persisted = await configDocument('config-8');
  let fetches = 0;
  const h = await clientHarness({
    initial: persisted,
    identityRevision: null,
    fetchConfig: async () => {
      fetches += 1;
      throw new Error('must not fetch');
    },
  });

  assert.equal(h.identity.appliedConfigRevision, 'config-8');
  assert.deepEqual(h.activator.current(), persisted);
  const current = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: null,
  });
  assert.equal(current.status, 'current');
  assert.equal(fetches, 0);
});

test('304 reuses the validated cached document and its ETag', async () => {
  const cached = await configDocument('config-8');
  const h = await clientHarness({
    initial: cached,
    fetchConfig: async () => ({
      status: 304,
      etag: 'config-8',
      document: null,
    }),
  });

  const result = await h.client.requireConfig(
    {
      required_config_revision: 'config-8',
      digest: cached.digest,
    },
    { force: true },
  );
  assert.equal(result.status, 'reused');
  assert.equal(h.requests[0].currentEtag, 'config-8');
  assert.deepEqual(h.activator.current(), cached);
  assert.equal(h.reports.at(-1).outcome, 'applied');
});

class MockSocket {
  constructor() {
    this.readyState = 0;
    this.sent = [];
  }

  send(value) {
    this.sent.push(value);
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

test('applied revision is echoed by subsequent heartbeat and reconnect hello', async () => {
  const oldDocument = await configDocument('config-7');
  const nextDocument = await configDocument('config-8');
  const configScheduler = scheduler();
  const identity = {
    agentInstallationId: INSTALLATION_ID,
    agentStreamId: STREAM_ID,
    lastAcknowledgedSourceSeq: 10,
    appliedConfigRevision: 'config-7',
  };
  const persistence = memoryPersistence(oldDocument);
  const sockets = [];
  let websocketClient;
  let resolveApplied;
  const applied = new Promise((resolve) => {
    resolveApplied = resolve;
  });
  const configClient = new AgentConfigClient({
    identity,
    creatorAccountId: ACCOUNT_ID,
    persistence,
    scheduler: configScheduler,
    activator: new AtomicConfigActivator(),
    http: {
      async fetchConfig(request) {
        assert.equal(request.authTicket, 'agent-config-ticket-42');
        return {
          status: 200,
          etag: nextDocument.etag,
          document: clone(nextDocument),
        };
      },
    },
    reportApplied: (report) => {
      const sent = websocketClient.sendConfigApplied(report);
      if (report.outcome === 'applied') resolveApplied();
      return sent;
    },
  });
  await configClient.initialize();

  const wsScheduler = scheduler();
  let id = 1;
  websocketClient = new AgentWebSocketClient({
    identity,
    creatorAccountId: ACCOUNT_ID,
    authTicket: 'test-agent-auth-ticket',
    configClient,
    scheduler: wsScheduler,
    random: () => 0.5,
    idFactory: () => `90000000-0000-4000-8000-${String(id++).padStart(12, '0')}`,
    webSocketFactory: () => {
      const socket = new MockSocket();
      sockets.push(socket);
      return socket;
    },
  });
  websocketClient.start();
  sockets[0].open();
  const session = await fixture('agent.session');
  session.payload.required_config_revision = 'config-8';
  sockets[0].receive(session);
  await applied;

  assert.equal(identity.appliedConfigRevision, 'config-8');
  websocketClient.sendHeartbeat();
  const heartbeat = JSON.parse(sockets[0].sent.at(-1));
  assert.equal(heartbeat.type, 'agent.heartbeat');
  assert.equal(heartbeat.payload.applied_config_revision, 'config-8');

  sockets[0].drop();
  wsScheduler.timeouts.find((task) => !task.cleared).handler();
  sockets[1].open();
  const hello = JSON.parse(sockets[1].sent[0]);
  assert.equal(hello.type, 'agent.hello');
  assert.equal(hello.payload.applied_config_revision, 'config-8');
});
test('persistence failure rolls atomic activation back to last known good', async () => {
  const good = await configDocument('config-7');
  const next = await configDocument('config-8');
  const identity = {
    agentInstallationId: INSTALLATION_ID,
    agentStreamId: STREAM_ID,
    lastAcknowledgedSourceSeq: 10,
    appliedConfigRevision: 'config-7',
  };
  const activator = new AtomicConfigActivator();
  let firstSave = true;
  const persistence = {
    async loadAppliedConfig() {
      return clone(good);
    },
    async saveAppliedConfig() {
      if (firstSave) {
        firstSave = false;
        throw new Error('storage interrupted');
      }
    },
  };
  const client = new AgentConfigClient({
    identity,
    creatorAccountId: ACCOUNT_ID,
    configAuthTicket: 'agent-config-ticket-42',
    persistence,
    activator,
    scheduler: scheduler(),
    http: {
      async fetchConfig() {
        return { status: 200, etag: next.etag, document: clone(next) };
      },
    },
  });
  await client.initialize();
  const result = await client.requireConfig({
    required_config_revision: 'config-8',
    digest: next.digest,
  });
  assert.equal(result.status, 'failed');
  assert.equal(identity.appliedConfigRevision, 'config-7');
  assert.deepEqual(activator.current(), good);
});

test('HTTP adapter keeps the config ticket out of the URL and sends it as authorization', async () => {
  const seen = [];
  const adapter = createConfigHttpAdapter({
    endpoint: 'https://brain.example/api/v1/agent/config',
    fetchImpl: async (url, options) => {
      seen.push({ url, options });
      return {
        status: 304,
        headers: { get: () => 'config-8' },
      };
    },
  });
  const result = await adapter.fetchConfig({
    authTicket: 'agent-config-ticket-42',
    agentInstallationId: INSTALLATION_ID,
    creatorAccountId: ACCOUNT_ID,
    currentEtag: 'config-8',
    currentConfigRevision: 'config-8',
    supportedSchemaVersions: ['2'],
  });
  assert.equal(result.status, 304);
  assert.equal(seen[0].options.headers['If-None-Match'], 'config-8');
  assert.equal(seen[0].options.headers.Authorization, 'Bearer agent-config-ticket-42');
  const url = new URL(seen[0].url);
  assert.equal(url.searchParams.has('auth_ticket'), false);
  assert.equal(seen[0].url.includes('agent-config-ticket-42'), false);
  assert.equal(url.searchParams.get('agent_installation_id'), INSTALLATION_ID);
  assert.equal(url.searchParams.get('creator_account_id'), ACCOUNT_ID);
});

test('Chrome adapter persists only the installation-global identifier', async () => {
  const values = {};
  const writes = [];
  const chromeMock = {
    runtime: {},
    storage: {
      local: {
        get(keys, callback) {
          callback(Object.fromEntries(keys.filter((key) => key in values).map((key) => [
            key,
            values[key],
          ])));
        },
        set(update, callback) {
          writes.push(clone(update));
          Object.assign(values, clone(update));
          callback?.();
        },
      },
    },
  };
  const adapter = createChromeAdapter(chromeMock, () => INSTALLATION_ID);
  assert.deepEqual(await adapter.loadAgentIdentity(), { agentInstallationId: INSTALLATION_ID });
  assert.deepEqual(writes, [{ agent_installation_id: INSTALLATION_ID }]);
  assert.equal(adapter.saveAppliedConfig, undefined);
  assert.deepEqual(Object.keys(values), ['agent_installation_id']);
});
test('config.available is routed to a forced conditional refresh', async () => {
  const calls = [];
  const identity = {
    agentInstallationId: INSTALLATION_ID,
    agentStreamId: STREAM_ID,
    lastAcknowledgedSourceSeq: 10,
    appliedConfigRevision: 'config-8',
  };
  const sockets = [];
  const client = new AgentWebSocketClient({
    identity,
    creatorAccountId: ACCOUNT_ID,
    authTicket: 'test-agent-auth-ticket',
    configClient: {
      async requireConfig(requirement, options) {
        calls.push({ requirement: clone(requirement), options: clone(options ?? {}) });
      },
    },
    scheduler: scheduler(),
    idFactory: () => '90000000-0000-4000-8000-000000000001',
    webSocketFactory: () => {
      const socket = new MockSocket();
      sockets.push(socket);
      return socket;
    },
  });
  client.start();
  sockets[0].open();
  sockets[0].receive(await fixture('agent.session'));
  const available = await fixture('config.available');
  sockets[0].receive(available);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(calls.length, 1);
  assert.equal(calls[0].requirement.required_config_revision, 'config-8');
  assert.equal(calls[0].options.force, true);
});
test('revision without its persisted document is not claimed on restart', async () => {
  const next = await configDocument('config-8');
  const h = await clientHarness({
    initial: null,
    identityRevision: 'config-8',
    response: next,
  });
  assert.equal(h.identity.appliedConfigRevision, null);
  assert.equal(h.client.healthSummary().status, 'degraded');
  const result = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: next.digest,
  });
  assert.equal(result.status, 'applied');
  assert.equal(h.identity.appliedConfigRevision, 'config-8');
});

test('configuration requiring an unsupported capability is rejected', async () => {
  const good = await configDocument('config-7');
  const required = await configDocument('config-8', {
    capture_policy: {
      observation_interval_seconds: 30,
      rules: [
        {
          resource: 'presence',
          url_pattern: '/api2/v2/users/list',
          enabled: true,
        },
      ],
    },
  });
  required.digest = await calculateConfigDigest(required);
  const h = await clientHarness({
    initial: good,
    response: required,
    capabilities: ['capture.chats', 'capture.messages', 'command.message.send'],
  });
  const result = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: required.digest,
  });
  assert.equal(result.status, 'failed');
  assert.equal(h.identity.appliedConfigRevision, 'config-7');
  assert.match(result.error.message, /unsupported capability/);
});

test('enabled history acquisition requires the explicit history.sync capability', async () => {
  const good = await configDocument('config-7');
  const required = await configDocument('config-8', {
    history_acquisition: {
      enabled: true,
      consent_revision: 'consent-v1',
      authorized_platform_creator_id: 'platform-creator-1',
      recent_window_days: 30,
      page_size: 50,
      pages_per_wake: 2,
      request_interval_ms: 1000,
      retry_limit: 3,
    },
  });
  const h = await clientHarness({
    initial: good,
    response: required,
    capabilities: [
      'capture.chats',
      'capture.messages',
      'capture.presence',
      'command.message.send',
    ],
  });
  const result = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: required.digest,
  });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'unsupported_capability');
  assert.match(result.error.message, /history\.sync/);
  assert.equal(h.identity.appliedConfigRevision, 'config-7');
});

test('message-only capture configuration is rejected without replacing the last known good', async () => {
  const good = await configDocument('config-7');
  const unsafe = await configDocument('config-8', {
    capture_policy: {
      observation_interval_seconds: 30,
      rules: [{
        resource: 'messages',
        url_pattern: '/api2/v2/chats/*/messages',
        enabled: true,
      }],
    },
  });
  unsafe.digest = await calculateConfigDigest(unsafe);
  const h = await clientHarness({ initial: good, response: unsafe });
  const result = await h.client.requireConfig({
    required_config_revision: 'config-8',
    digest: unsafe.digest,
  });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'unsafe_capture_policy');
  assert.equal(h.identity.appliedConfigRevision, 'config-7');
  assert.deepEqual(h.activator.current(), good);
});
