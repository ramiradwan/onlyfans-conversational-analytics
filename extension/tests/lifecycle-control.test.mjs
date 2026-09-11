import assert from 'node:assert/strict';
import test from 'node:test';

import { createLifecycleStorage } from '../runtime/lifecycle-storage.mjs';
import { OperationScope, SerialExecutor } from '../runtime/operation-scope.mjs';
import { AgentRuntime } from '../transport/agent-runtime-core.mjs';

test('operation scope invalidates admitted work and drains before reopen', async () => {
  const scope = new OperationScope();
  scope.reopen();
  let release;
  const blocked = new Promise((resolve) => { release = resolve; });
  const operation = scope.run(async (lease) => {
    await blocked;
    lease.assertCurrent();
  });
  scope.close('capture_stopped');
  release();
  await assert.rejects(operation, { code: 'capture_stopped' });
  await scope.drain();
  scope.reopen();
  assert.equal(await scope.run(async () => 7), 7);
});

test('serial executor preserves control-operation order after a rejection', async () => {
  const executor = new SerialExecutor();
  const order = [];
  const first = executor.run(async () => {
    order.push('first');
    throw new Error('expected');
  });
  const second = executor.run(async () => {
    order.push('second');
    return 2;
  });
  await assert.rejects(first, /expected/);
  assert.equal(await second, 2);
  assert.deepEqual(order, ['first', 'second']);
});

test('lifecycle storage rejects a transaction whose runtime is suspended before commit', async () => {
  const controller = new AbortController();
  let committed = false;
  const raw = {
    databaseName: Promise.resolve('synthetic'),
    async runTransaction(_mode, _stores, work) {
      const result = await work({ value: 1 });
      committed = true;
      return result;
    },
  };
  const storage = createLifecycleStorage(raw, controller.signal);
  await assert.rejects(
    storage.runTransaction('readwrite', ['x'], async () => {
      controller.abort(new Error('stopped'));
      return 'value';
    }),
    /stopped/,
  );
  assert.equal(committed, false);
});

test('runtime suspension prevents stale startup components from publishing', async () => {
  let entered;
  const entering = new Promise((resolve) => { entered = resolve; });
  let resolveInitialization;
  const initialized = new Promise((resolve) => { resolveInitialization = resolve; });
  let starts = 0;
  let stops = 0;
  const runtime = new AgentRuntime({
    registerWakeListeners: () => () => {},
    initialize: async ({ signal }) => {
      entered();
      await initialized;
      return {
        transport: {
          start() { starts += 1; },
          stop() { stops += 1; },
        },
        signal,
      };
    },
  });

  const waking = runtime.wake();
  await entering;
  const suspending = runtime.suspend();
  resolveInitialization();
  await suspending;
  await assert.rejects(waking);
  assert.equal(starts, 0);
  assert.equal(stops, 1);
  assert.equal(runtime.transport, null);
});
