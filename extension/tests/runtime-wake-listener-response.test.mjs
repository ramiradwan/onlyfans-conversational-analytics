import assert from 'node:assert/strict';
import test from 'node:test';

import { AgentRuntime } from '../transport/agent-runtime.mjs';

function transport() {
  return {
    starts: 0,
    reconciles: 0,
    start() { this.starts += 1; },
    stop() {},
    reconcileConnection() { this.reconciles += 1; },
  };
}

test('generic wake listener is fire-and-forget and never becomes an onMessage response', async () => {
  let listener = null;
  const activeTransport = transport();
  const runtime = new AgentRuntime({
    registerWakeListeners(candidate) { listener = candidate; },
    async initialize() { return { transport: activeTransport }; },
  });

  await runtime.start();
  assert.equal(typeof listener, 'function');
  assert.equal(activeTransport.starts, 1);

  const returned = listener({ type: 'synthetic-message' }, {}, () => {});
  assert.equal(returned, undefined);

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(activeTransport.reconciles, 1);
});
