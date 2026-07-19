import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AgentRuntime,
  createAccountSigningPersistence,
  createAgentRuntime,
} from '../transport/agent-runtime.mjs';
import { INGESTION_STORES } from '../transport/durable-outbox.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

function fakeTransport() {
  return {
    starts: 0,
    reconnectChecks: 0,
    reconcileChecks: 0,
    stops: 0,
    start() { this.starts += 1; },
    ensureConnected() { this.reconnectChecks += 1; },
    reconcileConnection() { this.reconcileChecks += 1; },
    stop() { this.stops += 1; },
    sendConfigApplied() { return true; },
  };
}

test('signing generations persist only in the exact account IndexedDB partition', async () => {
  const storage = new InMemoryIngestionStorage();
  const first = createAccountSigningPersistence(storage, 'creator-account-1');
  const state = {
    schema: 'browser-signing-state/v1',
    active: { id: 'generation-1' },
    previous: null,
  };

  assert.equal(await first.load(), null);
  await first.save(state);
  assert.deepEqual(await first.load(), state);
  assert.deepEqual(
    storage.stores.get(INGESTION_STORES.credentials).get('signer-state'),
    {
      key: 'signer-state',
      creator_account_id: 'creator-account-1',
      state,
    },
  );

  const wrongAccount = createAccountSigningPersistence(storage, 'creator-account-2');
  await assert.rejects(
    wrongAccount.load(),
    /does not match its account partition/,
  );
});

test('wake listeners register synchronously and startup is idempotent while storage loads', async () => {
  const listeners = [];
  let initializeCalls = 0;
  let releaseInitialization;
  const transport = fakeTransport();
  const runtime = new AgentRuntime({
    registerWakeListeners(listener) {
      listeners.push(listener);
    },
    initialize() {
      initializeCalls += 1;
      return new Promise((resolve) => {
        releaseInitialization = () => resolve({ transport });
      });
    },
  });

  const first = runtime.start();
  const duplicate = runtime.start();
  assert.equal(listeners.length, 1);
  assert.equal(initializeCalls, 0);
  assert.strictEqual(duplicate, first);

  await Promise.resolve();
  assert.equal(initializeCalls, 1);
  releaseInitialization();
  await first;
  assert.equal(transport.starts, 1);

  await listeners[0]();
  assert.equal(transport.reconcileChecks, 1);
  assert.equal(transport.reconnectChecks, 0);
  assert.equal(initializeCalls, 1);
});

test('a failed bootstrap is reported and the next wake retries initialization', async () => {
  const listeners = [];
  const failures = [];
  const transport = fakeTransport();
  let attempts = 0;
  const runtime = new AgentRuntime({
    registerWakeListeners(listener) {
      listeners.push(listener);
    },
    onStartupError(error) {
      failures.push(error.message);
    },
    async initialize() {
      attempts += 1;
      if (attempts === 1) throw new Error('temporary storage failure');
      return { transport };
    },
  });

  await assert.rejects(runtime.start(), /temporary storage failure/);
  assert.deepEqual(failures, ['temporary storage failure']);
  assert.equal(runtime.transport, null);

  await listeners[0]();
  assert.equal(attempts, 2);
  assert.equal(transport.starts, 1);
  assert.strictEqual(runtime.transport, transport);
});

test('a new worker runtime reconstructs durable identity and checkpoint before reconnecting', async () => {
  const stableIdentity = {
    agentInstallationId: '20000000-0000-4000-8000-000000000001',
    agentStreamId: '30000000-0000-4000-8000-000000000001',
    lastAcknowledgedSourceSeq: 2,
    appliedConfigRevision: null,
  };
  const transports = [];
  const observedIdentities = [];
  let identityLoads = 0;
  let outboxLoads = 0;
  let configLoads = 0;
  let reconnectCredential = null;
  const reconnectLoads = [];
  const reconnectSaves = [];
  const chromeAdapter = {
    onWake() { return () => {}; },
    async loadAgentInstallationId() {
      identityLoads += 1;
      return stableIdentity.agentInstallationId;
    },
    async loadReconnectAuthTicket(creatorAccountId) {
      reconnectLoads.push(creatorAccountId);
      return reconnectCredential;
    },
    async saveReconnectAuthTicket({ creatorAccountId, authTicket }) {
      reconnectSaves.push({ creatorAccountId, authTicket });
      reconnectCredential = authTicket;
    },
  };
  const options = {
    creatorAccountId: 'creator-account-1',
    authTicket: 'brain-ticket-1',
    chromeAdapter,
    ingestionStorageFactory: () => ({}),
    outboxFactory: () => ({
      async initialize() {
        outboxLoads += 1;
        return {
          agent_stream_id: stableIdentity.agentStreamId,
          acknowledged_source_seq: 7,
          applied_config_revision: null,
          account_epoch: 1,
        };
      },
    }),
    configHttpFactory: () => ({}),
    configActivatorFactory: () => ({}),
    configClientFactory: ({ identity }) => ({
      activeDocument: null,
      async initialize() {
        configLoads += 1;
        identity.appliedConfigRevision = 'bundled-safe-1';
      },
      healthSummary() { return { status: 'healthy', detail: null }; },
    }),
    transportFactory: (transportOptions) => {
      const { identity } = transportOptions;
      observedIdentities.push({ ...identity });
      const transport = fakeTransport();
      transport.bootstrapAuthTicket = transportOptions.authTicket;
      transport.reconnectAuthTicket = transportOptions.reconnectAuthTicket;
      transport.persistReconnectAuthTicket = transportOptions.persistReconnectAuthTicket;
      transports.push(transport);
      return transport;
    },
  };

  const firstWorker = createAgentRuntime(options);
  await firstWorker.start();
  await transports[0].persistReconnectAuthTicket('agent-reconnect-ticket-42');
  const restartedWorker = createAgentRuntime(options);
  await restartedWorker.start();

  assert.equal(identityLoads, 2);
  assert.equal(outboxLoads, 2);
  assert.equal(configLoads, 2);
  assert.deepEqual(
    observedIdentities.map((identity) => ({
      installation: identity.agentInstallationId,
      stream: identity.agentStreamId,
      acknowledged: identity.lastAcknowledgedSourceSeq,
      config: identity.appliedConfigRevision,
    })),
    [
      {
        installation: stableIdentity.agentInstallationId,
        stream: stableIdentity.agentStreamId,
        acknowledged: 7,
        config: 'bundled-safe-1',
      },
      {
        installation: stableIdentity.agentInstallationId,
        stream: stableIdentity.agentStreamId,
        acknowledged: 7,
        config: 'bundled-safe-1',
      },
    ],
  );
  assert.deepEqual(transports.map((transport) => transport.starts), [1, 1]);
  assert.deepEqual(reconnectLoads, ['creator-account-1', 'creator-account-1']);
  assert.deepEqual(reconnectSaves, [{
    creatorAccountId: 'creator-account-1',
    authTicket: 'agent-reconnect-ticket-42',
  }]);
  assert.deepEqual(
    transports.map((transport) => ({
      bootstrap: transport.bootstrapAuthTicket,
      reconnect: transport.reconnectAuthTicket,
    })),
    [
      { bootstrap: 'brain-ticket-1', reconnect: null },
      { bootstrap: 'brain-ticket-1', reconnect: 'agent-reconnect-ticket-42' },
    ],
  );
});

test('same-account credential rotation preserves jobs while a real switch invalidates stale work', async () => {
  let binding = { creatorAccountId: 'creator-account-1', authTicket: 'ticket-1' };
  const accountStates = new Map();
  const transports = [];
  const histories = [];
  const chromeAdapter = {
    onWake() { return () => {}; },
    async loadBrainBinding() { return { ...binding }; },
    async loadAgentInstallationId() {
      return '20000000-0000-4000-8000-000000000001';
    },
  };
  const runtime = createAgentRuntime({
    chromeAdapter,
    ingestionStorageFactory: () => ({}),
    outboxFactory: ({ creatorAccountId }) => {
      const state = accountStates.get(creatorAccountId) ?? {
        jobs: [`${creatorAccountId}:history-job`],
        invalidated: false,
        invalidations: 0,
      };
      accountStates.set(creatorAccountId, state);
      return {
        async initialize() {
          return {
            agent_stream_id: creatorAccountId === 'creator-account-1'
              ? '30000000-0000-4000-8000-000000000001'
              : '30000000-0000-4000-8000-000000000002',
            acknowledged_source_seq: 0,
            applied_config_revision: null,
            account_epoch: 1,
          };
        },
        async invalidateAccountEpoch() {
          state.invalidated = true;
          state.invalidations += 1;
        },
        assertWritable() {
          if (state.invalidated) throw new Error('Account partition was invalidated');
        },
        state,
      };
    },
    configHttpFactory: () => ({}),
    configActivatorFactory: () => ({}),
    configClientFactory: () => ({
      activeDocument: null,
      async initialize() {},
      healthSummary() { return { status: 'healthy', detail: null }; },
    }),
    signerFactory: async () => ({ read: async () => null }),
    historyCoordinatorFactory: () => {
      const history = {
        stops: 0,
        wake() { return Promise.resolve(); },
        stop() { this.stops += 1; },
        cancelCurrent() {},
      };
      histories.push(history);
      return history;
    },
    transportFactory: (options) => {
      const transport = fakeTransport();
      transport.outbox = options.outbox;
      transport.authTickets = [options.authTicket];
      transport.replaceAuthTicket = (ticket) => transport.authTickets.push(ticket);
      transports.push(transport);
      return transport;
    },
  });

  await runtime.start();
  const firstState = accountStates.get('creator-account-1');
  const preservedJobs = [...firstState.jobs];
  binding = { creatorAccountId: 'creator-account-1', authTicket: 'ticket-2' };
  await runtime.wake();
  assert.equal(transports.length, 1);
  assert.deepEqual(transports[0].authTickets, ['ticket-1', 'ticket-2']);
  assert.deepEqual(firstState.jobs, preservedJobs);
  assert.equal(firstState.invalidations, 0);
  assert.equal(histories[0].stops, 0);

  binding = { creatorAccountId: 'creator-account-2', authTicket: 'ticket-3' };
  await runtime.wake();
  assert.equal(transports.length, 2);
  assert.equal(transports[0].stops, 1);
  assert.equal(histories[0].stops, 1);
  assert.equal(firstState.invalidations, 1);
  assert.throws(() => transports[0].outbox.assertWritable(), /invalidated/);
  assert.deepEqual(firstState.jobs, preservedJobs);
  assert.deepEqual(transports[1].authTickets, ['ticket-3']);
});
