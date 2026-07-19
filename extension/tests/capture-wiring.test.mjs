import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

import {
  CAPTURE_MESSAGE_TYPE,
  CAPTURE_PROTOCOL_VERSION,
  CaptureDiagnostics,
  CaptureIngestionService,
  createCaptureMessageBridge,
  mapPlatformObservation,
} from '../transport/capture-ingestion.mjs';

const CHAT_OBSERVATION = Object.freeze({
  event_type: 'chat.observed',
  observed_at: '2026-07-19T08:00:00Z',
  source_path: '/api2/v2/chats',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: null,
  record: {
    id: 41,
    withUser: { id: 73, name: 'Synthetic Fan' },
    updatedAt: '2026-07-19T07:59:00Z',
  },
});

const MESSAGE_OBSERVATION = Object.freeze({
  event_type: 'message.observed',
  observed_at: '2026-07-19T08:01:00Z',
  source_path: '/api2/v2/chats/41/messages',
  creator_platform_user_id: 'creator-synthetic',
  context_chat_id: '41',
  record: {
    id: 99,
    fromUser: { id: 'creator-synthetic' },
    text: 'Synthetic message',
    createdAt: '2026-07-19T08:00:30Z',
  },
});

function runtimeMessage(observation) {
  return {
    type: CAPTURE_MESSAGE_TYPE,
    protocol_version: CAPTURE_PROTOCOL_VERSION,
    observation,
  };
}

function chromeMessageHarness() {
  const listeners = [];
  return {
    listeners,
    chromeApi: {
      runtime: {
        id: 'synthetic-extension-id',
        onMessage: {
          addListener(listener) { listeners.push(listener); },
          removeListener(listener) {
            const index = listeners.indexOf(listener);
            if (index >= 0) listeners.splice(index, 1);
          },
        },
      },
    },
  };
}

function sendRuntimeMessage(listener, message, sender) {
  return new Promise((resolve) => {
    const keepAlive = listener(message, sender, resolve);
    assert.equal(keepAlive, true);
  });
}

test('manifest statically installs the MAIN-world hook before the isolated bridge', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.content_scripts, [
    {
      matches: ['https://onlyfans.com/*'],
      js: ['page-hook.js'],
      run_at: 'document_start',
      all_frames: false,
      world: 'MAIN',
    },
    {
      matches: ['https://onlyfans.com/*'],
      js: ['content.js'],
      run_at: 'document_start',
      all_frames: false,
      world: 'ISOLATED',
    },
  ]);

  const pageHook = await readFile(new URL('../page-hook.js', import.meta.url), 'utf8');
  const contentBridge = await readFile(new URL('../content.js', import.meta.url), 'utf8');
  for (const mutationChannel of ['_OF_BACKEND_', 'send_ws_message', 'send_fetch_command']) {
    assert.equal(pageHook.includes(mutationChannel), false);
    assert.equal(contentBridge.includes(mutationChannel), false);
  }
  assert.equal(contentBridge.includes('runtime.onMessage'), false);
});

test('page hook posts raw observations only to the page origin', async () => {
  const posts = [];
  class FakeWebSocket {
    addEventListener() {}
  }
  class FakeXmlHttpRequest {
    open() {}
    send() {}
    addEventListener() {}
  }
  const response = {
    clone() {
      return {
        async json() {
          return {
            list: [{
              id: 41,
              withUser: { id: 73, name: 'Synthetic Fan' },
              updatedAt: '2026-07-19T07:59:00Z',
            }],
          };
        },
      };
    },
  };
  const pageWindow = {
    location: {
      origin: 'https://onlyfans.com',
      href: 'https://onlyfans.com/my/chats',
    },
    WebSocket: FakeWebSocket,
    async fetch() { return response; },
    postMessage(message, targetOrigin) { posts.push({ message, targetOrigin }); },
  };
  const context = vm.createContext({
    console,
    Date,
    JSON,
    Proxy,
    Reflect,
    URL,
    WeakMap,
    XMLHttpRequest: FakeXmlHttpRequest,
    window: pageWindow,
  });
  vm.runInContext(
    await readFile(new URL('../page-hook.js', import.meta.url), 'utf8'),
    context,
  );

  assert.strictEqual(await pageWindow.fetch('/api2/v2/chats'), response);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].targetOrigin, 'https://onlyfans.com');
  assert.equal(posts[0].message.type, CAPTURE_MESSAGE_TYPE);
  assert.equal(posts[0].message.observation.event_type, 'chat.observed');
  assert.deepEqual(
    JSON.parse(JSON.stringify(posts[0].message.observation.record)),
    {
      id: 41,
      withUser: { id: 73, name: 'Synthetic Fan' },
      updatedAt: '2026-07-19T07:59:00Z',
    },
  );
});

test('page hook recognizes bounded wrappers, empty pages, and keyed websocket messages', async () => {
  const posts = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
    }

    addEventListener(type, listener) {
      this.listeners.set(type, listener);
    }

    receive(document) {
      this.listeners.get('message')?.({ data: JSON.stringify(document) });
    }
  }
  class FakeXmlHttpRequest {
    open() {}
    send() {}
    addEventListener() {}
  }
  const bodies = new Map([
    ['/api2/v2/users/me', { id: 1 }],
    ['/api2/v2/chats', { response: { result: { items: [] } } }],
    ['/api2/v2/users/1/chats', { items: [null, 'schema-shift'] }],
    ['/api2/v2/chats/73/messages', {
      result: {
        data: {
          items: [{
            id: 100,
            fromUser: { id: 73 },
            text: 'Wrapped history message',
            createdAt: '2026-07-19T08:02:00Z',
          }],
        },
      },
    }],
  ]);
  const pageWindow = {
    location: {
      origin: 'https://onlyfans.com',
      href: 'https://onlyfans.com/my/chats',
    },
    WebSocket: FakeWebSocket,
    async fetch(input) {
      const pathname = new URL(String(input), this.location.href).pathname;
      return {
        clone() {
          return { async json() { return bodies.get(pathname); } };
        },
      };
    },
    postMessage(message, targetOrigin) { posts.push({ message, targetOrigin }); },
  };
  const context = vm.createContext({
    console,
    Date,
    JSON,
    Proxy,
    Reflect,
    Set,
    URL,
    WeakMap,
    XMLHttpRequest: FakeXmlHttpRequest,
    window: pageWindow,
  });
  vm.runInContext(
    await readFile(new URL('../page-hook.js', import.meta.url), 'utf8'),
    context,
  );

  const unidentifiedSocket = new pageWindow.WebSocket('wss://ws2.onlyfans.com/ws3/');
  unidentifiedSocket.receive({
    new_message: {
      id: 98,
      fromUser: { id: 74, isMe: false },
      text: 'Identity-independent inbound message',
      createdAt: '2026-07-19T08:00:00Z',
    },
  });
  unidentifiedSocket.receive({
    new_message: {
      id: 99,
      fromUser: { id: 1, isMe: true },
      toUser: { id: 75 },
      text: 'Identity-independent outbound message',
      createdAt: '2026-07-19T08:01:00Z',
    },
  });
  assert.deepEqual(
    posts.map(({ message }) => message.observation.context_chat_id),
    ['74', '75'],
  );

  await pageWindow.fetch('/api2/v2/users/me');
  await pageWindow.fetch('/api2/v2/chats');
  await pageWindow.fetch('/api2/v2/chats/73/messages');
  await pageWindow.fetch('/api2/v2/users/1/chats');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(posts.length, 4, 'only a truly empty recognized page may be silent');
  assert.equal(posts[2].message.observation.event_type, 'message.observed');
  assert.equal(posts[2].message.observation.context_chat_id, '73');
  assert.equal(posts[3].message.observation.event_type, 'hook.diagnostic');
  assert.equal(posts[3].message.observation.code, 'unrecognized_payload');

  const socket = new pageWindow.WebSocket('wss://ws2.onlyfans.com/ws3/');
  socket.receive({
    api2_chat_message: {
      id: 101,
      fromUser: { id: 73 },
      text: 'Keyed websocket message',
      createdAt: '2026-07-19T08:03:00Z',
    },
  });
  socket.receive({
    new_message: {
      id: 102,
      fromUser: { id: 1 },
      toUser: { id: 73 },
      text: 'Second keyed websocket message',
      createdAt: '2026-07-19T08:04:00Z',
    },
  });
  assert.deepEqual(
    posts.slice(4).map(({ message }) => message.observation.context_chat_id),
    ['73', '73'],
  );
});

test('content bridge requires same-window same-origin envelopes and logs no payload data', async () => {
  const listeners = [];
  const delivered = [];
  const warnings = [];
  const pageWindow = {
    location: { origin: 'https://onlyfans.com' },
    addEventListener(type, listener) {
      if (type === 'message') listeners.push(listener);
    },
  };
  const context = vm.createContext({
    console: { warn: (...values) => warnings.push(values) },
    chrome: {
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          delivered.push(message);
          callback({ ok: true });
        },
      },
    },
    window: pageWindow,
  });
  vm.runInContext(
    await readFile(new URL('../content.js', import.meta.url), 'utf8'),
    context,
  );
  assert.equal(listeners.length, 1);

  const envelope = runtimeMessage(CHAT_OBSERVATION);
  listeners[0]({ source: {}, origin: 'https://onlyfans.com', data: envelope });
  listeners[0]({ source: pageWindow, origin: 'https://example.test', data: envelope });
  assert.equal(delivered.length, 0);

  listeners[0]({ source: pageWindow, origin: 'https://onlyfans.com', data: envelope });
  assert.equal(delivered.length, 1);

  listeners[0]({
    source: pageWindow,
    origin: 'https://onlyfans.com',
    data: { ...envelope, extra: { text: 'Synthetic private marker' } },
  });
  assert.equal(delivered.length, 1);
  assert.equal(warnings.length, 1);
  assert.equal(JSON.stringify(warnings).includes('Synthetic private marker'), false);
  assert.deepEqual(
    JSON.parse(JSON.stringify(warnings[0][1])),
    { reason: 'invalid_page_envelope', count: 1 },
  );
});

test('raw chat and message observations map to exact protocol v2 upserts', () => {
  const chat = mapPlatformObservation(CHAT_OBSERVATION);
  assert.deepEqual(chat, {
    ok: true,
    eventType: 'chat.observed',
    resource: 'chats',
    sourcePath: '/api2/v2/chats',
    change: {
      type: 'chat.upsert',
      chat: {
        chat_id: '41',
        record_kind: 'full',
        platform_user_id: '73',
        display_name: 'Synthetic Fan',
        updated_at: '2026-07-19T07:59:00.000Z',
      },
    },
  });

  const message = mapPlatformObservation(MESSAGE_OBSERVATION);
  assert.deepEqual(message, {
    ok: true,
    eventType: 'message.observed',
    resource: 'messages',
    sourcePath: '/api2/v2/chats/41/messages',
    change: {
      type: 'message.upsert',
      message: {
        message_id: '99',
        chat_id: '41',
        sender_platform_user_id: 'creator-synthetic',
        text: 'Synthetic message',
        sent_at: '2026-07-19T08:00:30.000Z',
        direction: 'outbound',
      },
    },
  });

  const peerIdentifiedChat = mapPlatformObservation({
    ...CHAT_OBSERVATION,
    record: {
      with_user: { id: 74, username: 'synthetic-peer' },
      last_message: { created_at: '2026-07-19T08:01:30Z' },
    },
  });
  assert.equal(peerIdentifiedChat.ok, true);
  assert.equal(peerIdentifiedChat.change.chat.chat_id, '74');
  assert.equal(peerIdentifiedChat.change.chat.platform_user_id, '74');
});

test('malformed and unrecognized observations produce counted non-sensitive diagnostics', async () => {
  const reports = [];
  const diagnostics = new CaptureDiagnostics((diagnostic) => reports.push(diagnostic));
  const service = new CaptureIngestionService({
    diagnostics,
    runtime: {
      async wake() { throw new Error('Malformed observations must not wake transport'); },
    },
  });
  const invalid = {
    ...MESSAGE_OBSERVATION,
    record: { text: 'Synthetic diagnostic marker' },
  };

  assert.equal((await service.ingest(invalid)).code, 'invalid_message');
  assert.equal((await service.ingest(invalid)).code, 'invalid_message');
  assert.equal((await service.ingest({ event_type: 'unrecognized.synthetic' })).code, 'unrecognized_event');
  assert.deepEqual(diagnostics.snapshot(), {
    'message.observed:invalid_message': 2,
    'unknown:unrecognized_event': 1,
  });
  assert.deepEqual(reports.at(1), {
    reason: 'invalid_message',
    event_type: 'message.observed',
    count: 2,
  });
  assert.equal(JSON.stringify(reports).includes('Synthetic diagnostic marker'), false);
});

test('message ingestion requests one atomic placeholder-parent capture', async () => {
  const calls = [];
  const runtime = {
    configuration: {
      activeDocument: {
        capture_policy: {
          rules: [
            { resource: 'chats', url_pattern: '/api2/v2/chats', enabled: true },
            {
              resource: 'messages',
              url_pattern: '/api2/v2/chats/*/messages',
              enabled: true,
            },
          ],
        },
      },
    },
    async wake() {
      return {
        async captureDelta() {
          throw new Error('message capture must not enqueue a dependent delta alone');
        },
        async captureMessageWithParent(message, parent) {
          calls.push({ message, parent });
          return { source_seq: 2 };
        },
      };
    },
  };
  const service = new CaptureIngestionService({ runtime });
  assert.deepEqual(await service.ingest(MESSAGE_OBSERVATION), {
    ok: true,
    event_type: 'message.observed',
    source_seq: 2,
    material_transition: true,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].parent.type, 'chat.upsert');
  assert.deepEqual(calls[0].parent.chat, {
    chat_id: '41',
    record_kind: 'placeholder',
    platform_user_id: null,
    display_name: null,
    updated_at: null,
  });
  assert.equal(calls[0].message.message.chat_id, calls[0].parent.chat.chat_id);
});

test('background bridge validates sender and capture policy before durable enqueue', async () => {
  const captured = [];
  const diagnostics = new CaptureDiagnostics();
  let captureEnabled = true;
  const runtime = {
    configuration: null,
    async wake() {
      this.configuration = {
        activeDocument: {
          capture_policy: {
            rules: [{
              resource: 'chats',
              url_pattern: '/api2/v2/chats',
              enabled: captureEnabled,
            }],
          },
        },
      };
      return {
        async captureDelta(change) {
          captured.push(change);
          return { source_seq: captured.length };
        },
      };
    },
  };
  const ingestion = new CaptureIngestionService({ runtime, diagnostics });
  const h = chromeMessageHarness();
  const bridge = createCaptureMessageBridge({ ingestion, chromeApi: h.chromeApi });
  bridge.register();
  assert.equal(h.listeners.length, 1);

  const trusted = {
    id: h.chromeApi.runtime.id,
    frameId: 0,
    url: 'https://onlyfans.com/my/chats',
  };
  assert.equal(h.listeners[0](runtimeMessage(CHAT_OBSERVATION), {
    ...trusted,
    url: 'https://example.test/',
  }, () => {}), false);
  assert.equal(captured.length, 0);

  let malformedResponse;
  assert.equal(h.listeners[0]({
    ...runtimeMessage(CHAT_OBSERVATION),
    extra: true,
  }, trusted, (response) => { malformedResponse = response; }), false);
  assert.equal(malformedResponse.code, 'invalid_bridge_message');

  const response = await sendRuntimeMessage(
    h.listeners[0],
    runtimeMessage(CHAT_OBSERVATION),
    trusted,
  );
  assert.deepEqual(response, {
    ok: true,
    event_type: 'chat.observed',
    source_seq: 1,
    material_transition: true,
  });
  assert.equal(captured.length, 1);
  assert.equal(captured[0].type, 'chat.upsert');
  captureEnabled = false;
  assert.deepEqual(
    await sendRuntimeMessage(h.listeners[0], runtimeMessage(CHAT_OBSERVATION), trusted),
    { ok: false, code: 'capture_disabled', retryable: false },
  );
  assert.equal(captured.length, 1);
  assert.deepEqual(diagnostics.snapshot(), {
    'unknown:invalid_bridge_message': 1,
    'chat.observed:capture_disabled': 1,
  });
});
