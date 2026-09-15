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

function harness() {
  const pageListeners = [];
  const delivered = [];
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
      onMessage: { addListener() {} },
      sendMessage(message, callback) {
        if (message?.type === 'ofca.capture.context.query') {
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
    setTimeout,
    clearTimeout,
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
    resolveContext(response) {
      assert.equal(typeof contextCallback, 'function');
      const callback = contextCallback;
      contextCallback = null;
      callback(response);
    },
  };
}

test('Full observations arriving before context readiness are buffered and delivered in order', async () => {
  const h = harness();
  h.dispatch(captureEnvelope('chat-a'));
  h.dispatch(captureEnvelope('chat-b'));
  await flush();

  assert.deepEqual(h.delivered, []);

  h.resolveContext({ ok: true, consent_epoch: CONSENT_EPOCH });
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
