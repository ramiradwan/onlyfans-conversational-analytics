import assert from 'node:assert/strict';
import test from 'node:test';
import { ConsentController } from '../runtime/consent-controller.mjs';
import { CaptureDeliveryQueue } from '../capture/delivery-queue.mjs';

test('capture reports reuse page status and preserve sleeping tabs', async () => {
  const controller = Object.create(ConsentController.prototype);
  controller.state = { mode: 'full' };
  controller.phase = 'full';
  controller.captureScope = { isOpen: true };
  controller.runtime = { configuration: { activeDocument: { history_acquisition: { enabled: true } } } };
  controller.scheduler = { setTimeout, clearTimeout };
  let tabs = [{ id: 1, frozen: false, discarded: false }];
  const actions = [];
  controller.observer = { snapshot: () => ({ tabs: tabs.map((tab) => ({ ...tab,
    status: { mode: 'full', active: true, forwarding: true, ws2_socket_open: true } })),
    drops: { expired: 2, rejected: 1 }, drop_sources: {} }) };
  controller.chromeApi = { tabs: { query: async () => tabs, sendMessage: async (id, value) => {
    actions.push(value.action);
    return value.action === 'status' ? { mode: 'full', active: true, forwarding: true, ws2_socket_open: true }
      : { document: 'fixture-document', expired: 2, rejected: 1 };
  } } };
  assert.equal((await controller.captureState()).reason, 'ok');
  assert.deepEqual((await controller.captureState()).drops, { expired: 2, rejected: 1 });
  tabs = [{ id: 1, frozen: true, discarded: false }];
  const before = actions.length;
  assert.equal((await controller.captureState()).reason, 'tab_frozen');
  assert.equal(actions.length, before);
  controller.state.mode = 'paused';
  assert.equal((await controller.captureState()).reason, 'paused');
});

test('delivery queue exposes expired and rejected drops', async () => {
  const drops = [];
  const queue = new CaptureDeliveryQueue({ send: async () => ({ ok: false, retryable: false }),
    onDrop: reason => drops.push(reason), now: () => 1_000_000 });
  await assert.rejects(queue.enqueue({ created_at_ms: 0 }));
  await assert.rejects(queue.enqueue({ created_at_ms: 1_000_000 }));
  assert.deepEqual(drops, ['expired', 'rejected']);
});

test('delivery drop reporting survives content script reinjection', async () => {
  const previous = globalThis.chrome;
  const listeners = [];
  globalThis.chrome = { runtime: { onMessage: { addListener: value => listeners.push(value) },
    sendMessage: async () => {} } };
  try {
    for (const name of ['first', 'second']) {
      const { CaptureDeliveryQueue: Queue } = await import(`../capture/delivery-queue.mjs?${name}`);
      const queue = new Queue({ send: async () => ({ ok: false, retryable: false }), now: () => 1_000_000 });
      await assert.rejects(queue.enqueue({ created_at_ms: 0 }));
    }
    assert.equal(listeners.length, 1);
    let status;
    listeners[0]({ type: 'ofca.capture.queue.status' }, {}, value => { status = value; });
    assert.equal(status.expired, 2);
    assert.equal(status.rejected, 0);
  } finally { globalThis.chrome = previous; }
});
