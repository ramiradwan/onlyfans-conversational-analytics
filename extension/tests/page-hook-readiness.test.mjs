import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { parseTypedResponse } from 'local-authenticated-read-connector/browser-signing';
import { CAPTURE_LIMITS } from '../capture/delivery-queue.mjs';
import { readBoundedJson } from '../capture/bounded-json.mjs';
import { createProvisioningCompanionGuard } from '../runtime/provisioning-companion-guard.mjs';
import { normalizeSignerMessage } from '../transport/signer-normalization.mjs';
import { mapPlatformObservation } from '../transport/capture-ingestion.mjs';
import { mapPlatformObservation as mapReadOnlyPlatformObservation } from '../transport/read-only-capture-ingestion.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';

const bundle = (await build({ entryPoints: [fileURLToPath(new URL('../page-hook.js', import.meta.url))],
  bundle: true, format: 'iife', platform: 'browser', target: ['chrome132'], write: false,
})).outputFiles[0].text;
const flush = () => new Promise((resolve) => setImmediate(resolve));
const chat = { id: 'chat-a', withUser: { id: 'fan-a', name: 'Synthetic' }, updatedAt: '2030-01-01T00:00:00Z' };
const message = { id: 'message-a', text: 'Synthetic', fromUser: { id: 'fan-a' },
  chatUserId: 'fan-a', chat_id: 'chat-a', createdAt: '2030-01-01T00:00:00Z' };
function harness(mode = 'full', confirmed = true) {
  const posts = [];
  const listeners = new Map();
  let next = () => Promise.resolve(new Response('{}'));
  class Socket {
    constructor() { this.listeners = new Map(); }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    removeEventListener(name) { this.listeners.delete(name); }
    emit(frame) { this.listeners.get('message')?.({ data: JSON.stringify(frame) }); }
  }
  class Xhr {
    constructor() { this.listeners = new Map(); this.status = 200; }
    open() {}
    send() {}
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    complete(body, status = 200) {
      this.status = status; this.responseText = JSON.stringify(body);
      const callback = this.listeners.get('loadend');
      this.listeners.delete('loadend'); callback?.();
    }
  }
  const window = { location: { origin: 'https://onlyfans.com', href: 'https://onlyfans.com/my/chats' },
    WebSocket: Socket, fetch: (...args) => next(...args),
    postMessage: (value) => posts.push(structuredClone(value)),
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    history: { pushState(_state, _title, path) { window.location.href = new URL(path, window.location.href).href; } },
  };
  const context = vm.createContext({ window, XMLHttpRequest: Xhr, URL, crypto,
    TextEncoder, TextDecoder, AbortController, setTimeout, clearTimeout, console,
    __OFCA_CAPTURE_MODE__: mode,
  });
  vm.runInContext(bundle, context);
  posts.length = 0;
  if (confirmed) listeners.get('message')?.({ source: window, origin: window.location.origin,
    data: { type: 'ofca.capture.control', version: 1, action: 'resume' } });
  async function respond(path, body, status = 200) {
    next = () => Promise.resolve(new Response(JSON.stringify(body), { status }));
    await window.fetch(path); await flush();
  }
  return { window, posts, respond, Xhr, setFetch: (impl) => { next = impl; },
    reinstall() { context.__OFCA_CAPTURE_MODE__ = mode; vm.runInContext(bundle, context); },
    control(action) { listeners.get('message')?.({ source: window, origin: window.location.origin,
      data: { type: 'ofca.capture.control', version: 1, action } }); },
    rawControl(data) { listeners.get('message')?.({ source: window, origin: window.location.origin, data }); },
    captures: () => posts.filter((post) => post.observation?.record),
  };
}

const ownEcho = Object.freeze({
  id: 'message-own-1', text: 'Synthetic own reply', createdAt: '2030-01-01T00:00:30Z',
  toUser: { id: 'fan-a' }, responseType: 'message',
  giphyId: null, lockedText: false, isFree: true, price: 0, isMediaReady: true,
  mediaCount: 0, media: [], previews: [], isTip: false, isReportedByMe: false,
  isCouplePeopleMedia: false, queueId: null,
});

test('senderless own socket echo becomes one outbound canonical record', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit({ api2_chat_message: ownEcho });
  assert.equal(h.captures().length, 1);
  assert.deepEqual(h.captures()[0].observation.record, {
    message_id: 'message-own-1', chat_id: 'fan-a', sender_platform_user_id: 'creator-a',
    text: 'Synthetic own reply', sent_at: '2030-01-01T00:00:30.000Z', direction: 'outbound',
  });
});

test('own echo requires a distinct recipient and verified socket creator', async () => {
  const h = harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit({ api2_chat_message: ownEcho });
  assert.equal(h.captures().length, 0);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ api2_chat_message: { ...ownEcho, toUser: undefined } });
  socket.emit({ api2_chat_message: { ...ownEcho, toUser: { id: 'creator-a' } } });
  socket.emit({ api2_chat_message: { ...ownEcho, fromUser: null } });
  socket.emit({ api2_chat_message: { ...ownEcho, author: { id: 'creator-a' } } });
  assert.equal(h.captures().length, 0);
  const preview = harness('preview');
  await preview.respond('/api2/v2/users/me', { id: 'creator-a' });
  new preview.window.WebSocket('wss://ws2.onlyfans.com/ws').emit({ api2_chat_message: ownEcho });
  assert.equal(preview.captures().length, 0);
});

test('own echo, page HTTP, and history yield identical canonical message fields', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  new h.window.WebSocket('wss://ws2.onlyfans.com/ws').emit({ api2_chat_message: ownEcho });
  const echo = h.captures()[0].observation.record;
  const httpRaw = { ...ownEcho,
    fromUser: { id: 'creator-a' }, chatUserId: 'fan-a',
  };
  await h.respond('/api2/v2/chats/fan-a/messages', { list: [httpRaw] });
  const http = h.captures()[1].observation.record;
  const page = parseTypedResponse({ operation: 'message-page',
    parameters: { conversationId: 'fan-a' }, status: 200, contentType: 'application/json',
    body: { list: [httpRaw], hasMore: false },
  });
  assert.equal(page.summary.semantic_success, true);
  const history = normalizeSignerMessage(page.data.items[0], {
    creatorPlatformId: 'creator-a', conversationId: 'fan-a', observedAt: '2030-01-01T00:01:00Z',
  }).message;
  assert.deepEqual(echo, http);
  assert.deepEqual(echo, history);
  const { page_epoch: _epoch, ...observation } = h.captures()[0].observation;
  assert.deepEqual(mapPlatformObservation(observation).change.message, history);
  assert.deepEqual(mapReadOnlyPlatformObservation(observation), mapPlatformObservation(observation));
});

test('own socket echo crosses worker ingestion into the durable outbound outbox', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  new h.window.WebSocket('wss://ws2.onlyfans.com/ws').emit({ api2_chat_message: ownEcho });
  assert.equal(h.captures().length, 1);
  const { page_epoch: _epoch, ...observation } = h.captures()[0].observation;
  for (const prefix of ['', 'read-only-']) {
    const { DurableIngestOutbox } = await import(`../transport/${prefix}durable-outbox.mjs`);
    const { DeliveryCaptureIngestionService } = await import(`../transport/${prefix}delivery-capture-ingestion.mjs`);
    const outbox = new DurableIngestOutbox({ storage: new InMemoryIngestionStorage(),
      creatorAccountId: 'synthetic-account' });
    await outbox.initialize();
    const transport = { outbox, async flushOutbox() { throw new Error('offline'); } };
    const ingestion = new DeliveryCaptureIngestionService({
      diagnostics: { record() {} },
      runtime: {
        configuration: { activeDocument: { capture_policy: { rules: [{ enabled: true,
          resource: 'messages', url_pattern: '/ws',
        }] } } },
        async wake() { return transport; },
      },
    });
    const delivery = { delivery_id: crypto.randomUUID(), created_at_ms: Date.now() };
    const accepted = await ingestion.ingest(observation, { delivery });
    assert.equal(accepted.ok, true);
    const entries = await outbox.entries();
    assert.equal(entries.at(-1).change.message.direction, 'outbound');
  }
});

test('soft pause drops delayed HTTP responses and paused socket frames across resume', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  let release;
  h.setFetch(() => new Promise((resolve) => { release = resolve; }));
  const pending = h.window.fetch('/api2/v2/chats');
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  h.control('pause');
  const count = h.posts.length;
  const xhr = new h.Xhr(); xhr.open('GET', '/api2/v2/chats'); xhr.send();
  socket.emit({ type: 'new_message', data: message });
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.posts.length, count);
  h.control('resume');
  release(new Response(JSON.stringify({ list: [chat] })));
  xhr.complete({ list: [chat] });
  await pending; await flush();
  assert.equal(h.captures().length, 0);
  socket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures().length, 1);
});

test('new Full documents stay closed until confirmation and controls retain exact keys', async () => {
  const h = harness('full', false);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.readyState = 1;
  h.rawControl({ type: 'ofca.capture.control', version: 1, action: 'resume', extra: true });
  socket.emit({ type: 'new_message', data: message });
  assert.equal(h.posts.length, 0);
  h.control('status');
  assert.deepEqual(h.posts.at(-1).status, { mode: 'full', active: true, forwarding: false, ws2_socket_open: true });
  h.control('resume');
  socket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures().length, 1);
});

test('identity refresh reports only the current document observation and stops with the hook', async () => {
  const h = harness('identity');
  h.control('refresh_identity');
  assert.equal(h.posts.length, 0, 'Unknown identity must not manufacture a sign-out observation');
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const observed = h.posts.at(-1);
  h.control('refresh_identity');
  assert.deepEqual(h.posts.at(-1), observed);
  h.window.history.pushState(null, '', '/my/chats/chat/chat-b');
  const afterNavigation = h.posts.length;
  h.control('refresh_identity');
  assert.equal(h.posts.at(-1).type, 'ofca.provisioning.identity.reset');
  assert.equal(h.posts.length, afterNavigation);
  h.control('stop');
  const count = h.posts.length;
  h.control('refresh_identity');
  assert.equal(h.posts.length, count);
});

test('navigation and transient identity failures fence capture without closing the companion', async () => {
  let listener, invalidations = 0;
  createProvisioningCompanionGuard({
    chromeApi: { runtime: { id: 'synthetic-extension-id', onMessage: { addListener(fn) { listener = fn; } } } },
    companionClient: { invalidate() { invalidations += 1; } },
    configuredPlatformIdentity: () => 'creator-a',
  }).register();
  const sender = { id: 'synthetic-extension-id', frameId: 0, url: 'https://onlyfans.com/my/chats',
    tab: { id: 17 }, documentId: 'document-a', documentLifecycle: 'active' };
  const h = harness();
  let delivered = 0;
  const relay = () => { while (delivered < h.posts.length) listener(h.posts[delivered++], sender); };
  await h.respond('/api2/v2/users/me', { id: 'creator-a' }); relay();
  for (let index = 0; index < 10; index += 1) {
    h.window.history.pushState(null, '', `/my/chats/chat/${index}`);
    h.control('refresh_identity');
    await h.respond('/api2/v2/users/me', {}, 503);
    await h.respond('/api2/v2/chats', { list: [chat] });
    relay();
    assert.equal(h.captures().length, 0);
    await h.respond('/api2/v2/users/me', { id: 'creator-a' }); relay();
  }
  assert.equal(invalidations, 0);
  await h.respond('/api2/v2/users/me', {}, 401); relay();
  assert.equal(invalidations, 1, 'an observed authentication refusal still closes the channel');
  await h.respond('/api2/v2/users/me', { id: 'creator-b' }); relay();
  assert.equal(invalidations, 2, 'an actual account switch still closes the channel');
});

test('a response begun under account A cannot become a capture under account B', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  let release;
  h.setFetch(() => new Promise((resolve) => { release = resolve; }));
  const delayed = h.window.fetch('/api2/v2/chats');
  await h.respond('/api2/v2/users/me', { id: 'creator-b' });
  release(new Response(JSON.stringify({ list: [chat] })));
  await delayed; await flush();
  assert.equal(h.captures().length, 0);
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures()[0].observation.creator_platform_user_id, 'creator-b');
});

test('failed identity responses and navigation clear ownership until a fresh identity is observed', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  await h.respond('/api2/v2/users/me', { id: 'creator-a' }, 401);
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 0);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  h.window.history.pushState(null, '', '/my/profile');
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 0);
});

test('XHR send and socket creation retain their original ownership across account switches', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const xhr = new h.Xhr(); xhr.open('GET', '/api2/v2/chats'); xhr.send();
  const oldSocket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  await h.respond('/api2/v2/users/me', { id: 'creator-b' });
  xhr.complete({ list: [chat] }); oldSocket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures().length, 0);
  const newSocket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  newSocket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures()[0].observation.creator_platform_user_id, 'creator-b');
});

test('the original socket resumes after navigation revalidates the same creator', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit({ type: 'new_message', data: message });
  const firstEpoch = h.captures()[0].observation.page_epoch;
  h.window.history.pushState(null, '', '/my/chats/chat/chat-b');
  socket.emit({ type: 'new_message', data: { ...message, id: 'unknown-owner' } });
  assert.equal(h.captures().length, 1, 'unknown navigation state must remain fenced');
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'after-navigation' } });
  assert.equal(h.captures().length, 2);
  assert.notEqual(h.captures()[1].observation.page_epoch, firstEpoch);
  assert.equal(h.captures()[1].observation.creator_platform_user_id, 'creator-a');
  await h.respond('/api2/v2/users/me', {}, 401);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'retired-session' } });
  assert.equal(h.captures().length, 2, 'sign-out permanently retires the old socket authority');
});

test('repeated same-mode injection preserves the original socket and creates no duplicate observers', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  const fetch = h.window.fetch;
  const socketConstructor = h.window.WebSocket;
  for (let index = 0; index < 5; index += 1) h.reinstall();
  assert.equal(h.window.fetch, fetch);
  assert.equal(h.window.WebSocket, socketConstructor);
  socket.emit({ type: 'new_message', data: message });
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 2);
  h.control('stop');
  socket.emit({ type: 'new_message', data: message });
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 2, 'stopped observers never deliver');
});

test('a socket opened before the initial identity binds once and never follows an account switch', async () => {
  const h = harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures().length, 0);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: message });
  assert.equal(h.captures().length, 1);
  await h.respond('/api2/v2/users/me', { id: 'creator-b' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'wrong-account' } });
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'retired-account' } });
  assert.equal(h.captures().length, 1);
});

test('unknown Full identity produces no content and Preview emits only deduplication metadata', async () => {
  const full = harness(); await full.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(full.captures().length, 0);
  const preview = harness('preview');
  await preview.respond('/api2/v2/users/me', { id: 'creator-a' });
  await preview.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(preview.posts.length, 1);
  assert.deepEqual(Object.keys(preview.posts[0].observation).sort(), ['activity_at', 'creator_id', 'kind', 'observed_at', 'record_id']);
});

test('oversized bodies are cancelled and oversized normalized fields are reported', async () => {
  let cancelled = false;
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(new Uint8Array(CAPTURE_LIMITS.responseBytes + 1));
  }, cancel() { cancelled = true; } });
  await assert.rejects(readBoundedJson(new Response(stream), CAPTURE_LIMITS.responseBytes), /too_large/);
  assert.equal(cancelled, true);
  const h = harness(); await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  await h.respond('/api2/v2/chats', { list: [{ ...chat, withUser: { id: 'fan-a', name: 'x'.repeat(1025) } }] });
  assert.equal(h.captures().length, 0);
  assert.equal(h.posts.at(-1).observation.code, 'capture_too_large');
});

test('a stalled response body releases its reader at the capture deadline', async () => {
  let cancelled = false;
  const stream = new ReadableStream({ cancel() { cancelled = true; } });
  await assert.rejects(readBoundedJson(new Response(stream), 1024, 5), /timeout/);
  assert.equal(cancelled, true);
});

test('Preview waits for identity, keeps it across SPA navigation, and rejects stale account responses', async () => {
  const h = harness('preview');
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.posts.length, 0);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  assert.equal(h.posts.at(-1).observation.creator_id, 'creator-a');
  h.window.history.pushState(null, '', '/my/chats/chat/chat-a');
  await h.respond('/api2/v2/chats/chat-a/messages', { list: [message] });
  assert.equal(h.posts.at(-1).observation.creator_id, 'creator-a');
  let release;
  h.setFetch(() => new Promise((resolve) => { release = resolve; }));
  const pending = h.window.fetch('/api2/v2/chats/chat-a/messages');
  await h.respond('/api2/v2/users/me', { id: 'creator-b' });
  const count = h.posts.length;
  release(new Response(JSON.stringify({ list: [message] })));
  await pending; await flush();
  assert.equal(h.posts.length, count);
});
