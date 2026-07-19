import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AgentRuntime,
  createAgentRuntime,
} from '../transport/agent-runtime.mjs';

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
  const chromeAdapter = {
    onWake() { return () => {}; },
    async loadAgentIdentity() {
      identityLoads += 1;
      return { ...stableIdentity };
    },
  };
  const options = {
    chromeAdapter,
    ingestionStorageFactory: () => ({}),
    outboxFactory: () => ({
      async initialize() {
        outboxLoads += 1;
        return { acknowledged_source_seq: 7 };
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
    transportFactory: ({ identity }) => {
      observedIdentities.push({ ...identity });
      const transport = fakeTransport();
      transports.push(transport);
      return transport;
    },
  };

  const firstWorker = createAgentRuntime(options);
  await firstWorker.start();
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
});
