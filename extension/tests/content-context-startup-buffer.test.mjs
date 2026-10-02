import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { build } from 'esbuild';
import {
  CAPTURE_DELIVERY_TYPE,
  CAPTURE_MESSAGE_TYPE,
  CAPTURE_PROTOCOL_VERSION,
  isCaptureDelivery,
} from '../capture/envelopes.mjs';
import { CaptureDeliveryQueue } from '../capture/delivery-queue.mjs';

const CONSENT_EPOCH = '10000000-0000-4000-8000-000000000008';
const PAGE_EPOCH = '10000000-0000-4000-8000-000000000001';
const flush = () => new Promise((resolve) => setImmediate(resolve));

const bundle = (await build({
  entryPoints: [fileURLToPath(new URL('../content.js', import.meta.url))],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['chrome132'],
  write: false,
})).outputFiles[0].text;

function captureEnvelope(chatId) {
  return {
    type: CAPTURE_MESSAGE_TYPE,
    protocol_version: CAPTURE_PROTOCOL_VERSION,
    observation: {
      event_type: 'chat.observed',
      observed_at: '2030-01-08T12:00:00Z',
      source_path: '/api2/v2/chats',
      creator_platform_user_id: 'creator-platform-a',
      context_chat_id: null,
      page_epoch: PAGE_EPOCH,
      record: {
        chat_id: chatId,
        platform_user_id: `fan-${chatId}`,
        display_name: `Fan ${chatId}`,
        updated_at: '2030-01-08T12:00:00Z',
      },
    },
  };
}

function harness({ timers = null } = {}) {
  const pageListeners = [];
  const delivered = [];
  const controls = [];
  let contextCallback = null;
  const pageWindow = {
    location: { origin: 'https://onlyfans.com' },
    postMessage() {},
    addEventListener(name, listener) {
      if (name === 'message') pageListeners.push(listener);
    },
    removeEventListener(name, listener) {
      if (name !== 'message') return;
      const index = pageListeners.indexOf(listener);
      if (index >= 0) pageListeners.splice(index, 1);
    },
  };
  const chrome = {
    runtime: {
      lastError: null,
      onMessage: { addListener(listener) { controls.push(listener); } },
      sendMessage(message, callback) {
        if (message?.type === 'ofca.capture.state.query') {
          contextCallback = callback;
          return;
        }
        delivered.push(structuredClone(message));
        callback({ ok: true });
      },
    },
  };
  const context = vm.createContext({
    window: pageWindow,
    chrome,
    TextEncoder,
    TextDecoder,
    AbortController,
    DOMException,
    setTimeout: timers?.setTimeout ?? setTimeout,
    clearTimeout: timers?.clearTimeout ?? clearTimeout,
    structuredClone,
    crypto,
    console,
  });
  vm.runInContext(bundle, context);
  const dispatch = (envelope) => {
    const event = {
      source: pageWindow,
      origin: pageWindow.location.origin,
      data: structuredClone(envelope),
    };
    for (const listener of [...pageListeners]) listener(event);
  };
  return {
    delivered,
    dispatch,
    pause() { for (const control of controls) control({ type: 'ofca.capture.control', version: 1, action: 'pause' }, {}, () => {}); },
    resolveContext(response) {
      assert.equal(typeof contextCallback, 'function');
      const callback = contextCallback;
      contextCallback = null;
      callback(response);
    },
    tick(ms) { timers?.tick(ms); },
  };
}

function fakeTimers() {
  let now = 0;
  let nextId = 0;
  const tasks = new Map();
  return {
    setTimeout(callback, delay = 0) {
      const id = ++nextId;
      tasks.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout(id) { tasks.delete(id); },
    tick(ms) {
      const end = now + ms;
      while (true) {
        const next = [...tasks].sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!next || next[1].at > end) break;
        now = next[1].at;
        tasks.delete(next[0]);
        next[1].callback();
      }
      now = end;
    },
  };
}

test('unconfirmed observations are dropped and confirmed Full observations are delivered in order', async () => {
  const h = harness();
  h.dispatch(captureEnvelope('chat-a'));
  h.dispatch(captureEnvelope('chat-b'));
  await flush();

  assert.deepEqual(h.delivered, []);

  h.resolveContext({ ok: true, mode: 'full', consent_epoch: CONSENT_EPOCH });
  await flush();
  assert.deepEqual(h.delivered, []);
  h.dispatch(captureEnvelope('chat-a'));
  h.dispatch(captureEnvelope('chat-b'));
  await flush();
  await flush();

  assert.equal(h.delivered.length, 2);
  assert.ok(h.delivered.every((delivery) => delivery.type === CAPTURE_DELIVERY_TYPE));
  assert.ok(h.delivered.every(isCaptureDelivery));
  assert.ok(h.delivered.every((delivery) => delivery.consent_epoch === CONSENT_EPOCH));
  assert.deepEqual(
    h.delivered.map((delivery) => delivery.observation.record.chat_id),
    ['chat-a', 'chat-b'],
  );
  assert.notEqual(h.delivered[0].delivery_id, h.delivered[1].delivery_id);
});

test('a delayed state confirmation cannot revive a bridge after pause', async () => {
  const h = harness();
  h.pause();
  h.resolveContext({ ok: true, mode: 'full', consent_epoch: CONSENT_EPOCH });
  await flush();
  h.dispatch(captureEnvelope('late-confirmation'));
  await flush();
  assert.deepEqual(h.delivered, []);
});


test('page readiness changes wake the worker only after the status changes', async () => {
  const h = harness();
  const status = (socketOpen) => ({
    type: 'ofca.capture.control.status',
    version: 1,
    status: { mode: 'full', active: true, forwarding: true, ws2_socket_open: socketOpen },
  });
  h.dispatch(status(false));
  h.dispatch(status(false));
  h.dispatch(status(true));
  await flush();

  const changes = h.delivered.filter(message => message.type === 'ofca.capture.state.changed');
  assert.equal(changes.length, 1);
});

test('100 alternating page statuses send at most 11 worker notifications in 10 seconds', async () => {
  const timers = fakeTimers();
  const h = harness({ timers });
  const status = (socketOpen) => ({
    type: 'ofca.capture.control.status', version: 1,
    status: { mode: 'full', active: true, forwarding: true, ws2_socket_open: socketOpen },
  });
  h.dispatch(status(false));
  for (let i = 0; i < 100; i++) {
    h.dispatch(status(i % 2 === 0));
    timers.tick(100);
  }
  timers.tick(1_000);
  await flush();
  assert.ok(h.delivered.filter(message => message.type === 'ofca.capture.state.changed').length <= 11);
});

test('100 queue drops send at most 11 queue notifications in 10 seconds', () => {
  const timers = fakeTimers();
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const originalChrome = globalThis.chrome;
  const delivered = [];
  globalThis.setTimeout = timers.setTimeout;
  globalThis.clearTimeout = timers.clearTimeout;
  globalThis.chrome = { runtime: { sendMessage(message) { delivered.push(structuredClone(message)); } } };
  try {
    const queue = new CaptureDeliveryQueue({ send: async () => ({ ok: true }), maxEntries: 0 });
    for (let i = 0; i < 100; i++) {
      assert.throws(() => queue.enqueue({ sequence: i }));
      timers.tick(100);
    }
    timers.tick(1_000);
    assert.ok(delivered.filter(message => message.type === 'ofca.capture.queue.changed').length <= 11);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    if (originalChrome === undefined) delete globalThis.chrome;
    else globalThis.chrome = originalChrome;
  }
});
