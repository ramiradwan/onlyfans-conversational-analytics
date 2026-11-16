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
import { identityNavigation } from '../test-fixtures/identity-navigation.mjs';
import { payload } from '../test-fixtures/browser-task.mjs';
import { assertCaptureContext } from '../runtime/capture-context.mjs';
import { DocumentObserverCoordinator } from '../runtime/document-observer.mjs';
import { statusPresentation } from '../ui/presentation.mjs';
import { browserSurface } from '../runtime/browser-surface.mjs';

const bundle = (await build({ entryPoints: [fileURLToPath(new URL('../page-hook.js', import.meta.url))],
  bundle: true, format: 'iife', platform: 'browser', target: ['chrome132'], write: false,
})).outputFiles[0].text;
const flush = () => new Promise((resolve) => setImmediate(resolve));
const chat = { id: 'chat-a', withUser: { id: 'fan-a', name: 'Synthetic' }, updatedAt: '2030-01-01T00:00:00Z' };
const message = { id: 'message-a', text: 'Synthetic', fromUser: { id: 'fan-a' },
  chatUserId: 'fan-a', chat_id: 'chat-a', createdAt: '2030-01-01T00:00:00Z' };
function harness(mode = 'full', confirmed = true, prerendering = false) {
  const posts = [];
  const listeners = new Map();
  let next = () => Promise.resolve(new Response('{}'));
  class Socket {
    constructor() { this.listeners = new Map(); this.readyState = 0; }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    removeEventListener(name) { this.listeners.delete(name); }
    emit(frame) { this.listeners.get('message')?.({ data: JSON.stringify(frame) }); }
    open() { this.readyState = 1; this.listeners.get('open')?.({}); }
    close() { this.readyState = 3; this.listeners.get('close')?.({}); }
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
  const documentListeners = new Map();
  const document = { prerendering, addEventListener(name, fn) { documentListeners.set(name, fn); },
    removeEventListener(name) { documentListeners.delete(name); } };
  let requests = 0;
  const window = { document, location: { origin: 'https://onlyfans.com', href: 'https://onlyfans.com/my/chats' },
    WebSocket: Socket, fetch: (...args) => { requests++; return next(...args); },
    postMessage: (value) => posts.push(structuredClone(value)),
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    history: Object.fromEntries(['pushState', 'replaceState'].map(name => [name,
      (_state, _title, path) => { window.location.href = new URL(path, window.location.href).href; }])),
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
    event(name, value = {}) { listeners.get(name)?.(value); },
    activate() { document.prerendering = false; documentListeners.get('prerenderingchange')?.(); },
    requests: () => requests,
    reinstall() { context.__OFCA_CAPTURE_MODE__ = mode; vm.runInContext(bundle, context); },
    control(action) { listeners.get('message')?.({ source: window, origin: window.location.origin,
      data: { type: 'ofca.capture.control', version: 1, action } }); },
    rawControl(data) { listeners.get('message')?.({ source: window, origin: window.location.origin, data }); },
    captures: () => posts.filter((post) => post.observation?.record),
  };
}

test('current identity response carries bounded display labels and never reuses them for another account', async () => {
  const h = harness('identity');
  await h.respond('/api2/v2/users/me', { id: 'creator-one', name: '  Synthetic Creator  ', username: 'synthetic_handle' });
  const latest = () => h.posts.filter(post => post.type === 'ofca.provisioning.identity.update').at(-1).authenticated_profile;
  assert.deepEqual(latest(), { creator_account_id: 'creator-one', display_name: 'Synthetic Creator', username: 'synthetic_handle' });
  h.control('refresh_identity');
  assert.equal(latest().display_name, 'Synthetic Creator');
  await h.respond('/api2/v2/init', { user: { id: 'creator-two', name: 'bad\nlabel', username: 'x'.repeat(129) } });
  assert.deepEqual(latest(), { creator_account_id: 'creator-two' });
  await h.respond('/api2/v2/users/me', { id: 'creator-three', name: '\u202eevil', username: ' next_handle ' });
  assert.deepEqual(latest(), { creator_account_id: 'creator-three', username: 'next_handle' });
});

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
  assert.deepEqual(h.posts.at(-1), observed);
  assert.equal(h.posts.length, afterNavigation + 1);
  assert.equal(h.posts.at(-1).page_epoch, observed.page_epoch);
  h.control('pause');
  h.control('refresh_identity');
  assert.equal(h.posts.length, afterNavigation + 1);
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

test('failed identity responses clear ownership but same-document navigation retains confirmation', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  await h.respond('/api2/v2/users/me', { id: 'creator-a' }, 401);
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 0);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  h.window.history.pushState(null, '', '/my/profile');
  await h.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(h.captures().length, 1);
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

test('the original socket continues across navigation under the same confirmed creator', async () => {
  const h = harness();
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit({ type: 'new_message', data: message });
  const firstEpoch = h.captures()[0].observation.page_epoch;
  h.window.history.pushState(null, '', '/my/chats/chat/chat-b');
  socket.emit({ type: 'new_message', data: { ...message, id: 'unknown-owner' } });
  assert.equal(h.captures().length, 2, 'same-document navigation retains confirmed ownership');
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'after-navigation' } });
  assert.equal(h.captures().length, 3);
  assert.equal(h.captures()[1].observation.page_epoch, firstEpoch);
  assert.equal(h.captures()[1].observation.creator_platform_user_id, 'creator-a');
  await h.respond('/api2/v2/users/me', {}, 401);
  await h.respond('/api2/v2/users/me', { id: 'creator-a' });
  socket.emit({ type: 'new_message', data: { ...message, id: 'retired-session' } });
  assert.equal(h.captures().length, 3, 'sign-out permanently retires the old socket authority');
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


test('Full page socket lifecycle publishes readiness changes', () => {
  const h = harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  h.posts.length = 0;
  socket.open();
  assert.equal(h.posts.at(-1).status.ws2_socket_open, true);
  socket.close();
  assert.equal(h.posts.at(-1).status.ws2_socket_open, false);
});

for (const navigation of ['pushState', 'replaceState', 'hashchange', 'popstate']) {
  test(`I1 I4 I6: ${navigation} retains labels, epoch, HTTP ownership and socket authority passively`, async () => {
    const h = harness();
    await h.respond('/api2/v2/users/me', { id: 'synthetic-creator', name: 'Synthetic', username: 'synthetic' });
    const identity = h.posts.at(-1);
    const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
    let release;
    h.setFetch(() => new Promise(resolve => { release = resolve; }));
    const pending = h.window.fetch('/api2/v2/chats');
    const xhr = new h.Xhr(); xhr.open('GET', '/api2/v2/chats'); xhr.send();
    const requests = h.requests();
    if (navigation.endsWith('State')) h.window.history[navigation](null, '', '/my/profile');
    else h.event(navigation);
    h.control('refresh_identity');
    assert.deepEqual(h.posts.at(-1), identity);
    h.control('status');
    assert.equal(h.posts.at(-1).status.forwarding, true);
    assert.equal(h.requests(), requests, 'navigation and refresh start no request');
    release(new Response(JSON.stringify({ list: [chat] })));
    xhr.complete({ list: [chat] });
    await pending; await flush();
    socket.emit({ type: 'new_message', data: message });
    assert.equal(h.captures().length, 3);
    assert.ok(h.captures().every(post => post.observation.creator_platform_user_id === 'synthetic-creator'
      && post.observation.page_epoch === identity.page_epoch));
  });
}

for (const transport of ['fetch', 'xhr']) {
  for (const outcome of ['same', 'different', 'invalid', '401', '403', '503', 'malformed', 'network']) {
    test(`I2: ${transport} identity ${outcome} spanning navigation applies immediately`, async () => {
      const h = harness(); await h.respond('/api2/v2/users/me', { id: 'synthetic-first' });
      const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
      let release, reject, pending, xhr;
      if (transport === 'fetch') {
        h.setFetch(() => new Promise((done, fail) => { release = done; reject = fail; }));
        pending = h.window.fetch('/api2/v2/init');
      } else { xhr = new h.Xhr(); xhr.open('GET', '/api2/v2/init'); xhr.send(); }
      h.window.history.pushState(null, '', '/my/profile');
      const body = outcome === 'invalid' ? {} : { user: { id: outcome === 'same' ? 'synthetic-first' : 'synthetic-next' } };
      const status = /^\d+$/.test(outcome) ? Number(outcome) : 200;
      if (transport === 'fetch') {
        if (outcome === 'network') reject(new Error('synthetic failure'));
        else release(new Response(outcome === 'malformed' ? '{' : JSON.stringify(body), { status }));
        if (outcome === 'network') await assert.rejects(pending);
        else await pending;
        await flush();
      } else if (outcome === 'malformed') {
        xhr.status = 200; xhr.responseText = '{'; xhr.listeners.get('loadend')();
      } else xhr.complete(body, outcome === 'network' ? 0 : status);
      const latest = h.posts.filter(post => post.type.startsWith('ofca.provisioning.identity.')).at(-1);
      if (['same', 'different'].includes(outcome)) {
        assert.equal(latest.authenticated_profile.creator_account_id, body.user.id);
      } else if (['401', '403'].includes(outcome)) assert.equal(latest.authenticated_profile, null);
      else assert.equal(latest.type, 'ofca.provisioning.identity.reset');
      await h.respond('/api2/v2/chats', { list: [chat] });
      socket.emit({ type: 'new_message', data: message });
      assert.equal(h.captures().length, outcome === 'same' ? 2 : outcome === 'different' ? 1 : 0);
    });
  }
  test(`I1 I2: latest-issued ${transport} identity wins across navigation in both completion orders`, async () => {
    for (const newestFirst of [true, false]) for (const staleRefusal of [true, false]) {
      const h = harness(); await h.respond('/api2/v2/users/me', { id: 'synthetic-initial' });
      const pending = [];
      const issue = () => {
        if (transport === 'xhr') {
          const xhr = new h.Xhr(); xhr.open('GET', '/api2/v2/users/me'); xhr.send();
          return body => xhr.complete(body, body ? 200 : 401);
        }
        let release; h.setFetch(() => new Promise(resolve => { release = resolve; }));
        pending.push(h.window.fetch('/api2/v2/users/me'));
        return body => release(new Response(JSON.stringify(body), { status: body ? 200 : 401 }));
      };
      const older = issue(); h.event('popstate'); const newer = issue(); h.event('hashchange');
      if (newestFirst) { newer({ id: 'synthetic-latest' }); await flush(); older(staleRefusal ? null : { id: 'synthetic-stale' }); }
      else {
        older(staleRefusal ? null : { id: 'synthetic-stale' }); await flush(); h.control('refresh_identity');
        assert.equal(h.posts.at(-1).authenticated_profile.creator_account_id, 'synthetic-initial');
        newer({ id: 'synthetic-latest' });
      }
      await Promise.all(pending); await flush(); h.control('refresh_identity');
      assert.equal(h.posts.at(-1).authenticated_profile.creator_account_id, 'synthetic-latest');
    }
  });
  for (const mode of ['identity', 'full', 'preview']) {
    test(`I2: passive ${transport} API refusal fences ${mode} without treating ordinary 403 as sign-out`, async () => {
      const h = harness(mode); await h.respond('/api2/v2/users/me', { id: 'synthetic-creator' });
      const response = async (path, status) => {
        if (transport === 'fetch') await h.respond(path, {}, status);
        else { const xhr = new h.Xhr(); xhr.open('GET', path); xhr.send(); xhr.complete({}, status); }
      };
      for (const path of ['/api2/v2/chats', '/api2/v2/chats/synthetic/messages', '/api2/v2/subscriptions']) {
        await response(path, 403);
        h.control('refresh_identity');
        if (mode !== 'preview') assert.equal(h.posts.at(-1).authenticated_profile.creator_account_id, 'synthetic-creator');
        const before = h.posts.length;
        await response(path, 401);
        if (mode !== 'preview') assert.equal(h.posts.filter(post => post.type.startsWith('ofca.provisioning.identity.')).at(-1).type, 'ofca.provisioning.identity.reset');
        await h.respond('/api2/v2/chats', { list: [chat] });
        assert.equal(h.captures().length, 0);
        await h.respond('/api2/v2/users/me', { id: 'synthetic-creator' });
        assert.ok(h.posts.length >= before);
      }
      await response('https://example.test/api2/v2/chats', 401);
      await response('/static/resource', 401);
      h.control('refresh_identity');
      if (mode !== 'preview') assert.equal(h.posts.at(-1).authenticated_profile.creator_account_id, 'synthetic-creator');
    });
  }
}

for (const lifecycle of ['new-document', 'bfcache', 'prerender', 'withdraw_identity', 'reset_identity']) {
  test(`I3 I5 I7: ${lifecycle} starts unconfirmed and cannot reuse earlier responses or sockets`, async () => {
    let h = harness('full', true, lifecycle === 'prerender');
    await h.respond('/api2/v2/users/me', { id: 'synthetic-creator' });
    const oldSocket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
    let release; h.setFetch(() => new Promise(resolve => { release = resolve; }));
    const pending = h.window.fetch('/api2/v2/users/me');
    if (lifecycle === 'new-document') { h.control('stop'); h = harness(); }
    else if (lifecycle === 'bfcache') h.event('pageshow', { persisted: true });
    else if (lifecycle === 'prerender') h.activate();
    else { h.control(lifecycle); h.control('resume'); }
    release(new Response(JSON.stringify({ id: 'synthetic-creator' })));
    await pending; await flush();
    await h.respond('/api2/v2/chats', { list: [chat] });
    oldSocket.emit({ type: 'new_message', data: message });
    assert.equal(h.captures().length, 0);
    await h.respond('/api2/v2/users/me', { id: 'synthetic-creator' });
    oldSocket.emit({ type: 'new_message', data: message });
    assert.equal(h.captures().length, 0, 'a retired socket never regains authority');
    new h.window.WebSocket('wss://ws2.onlyfans.com/ws').emit({ type: 'new_message', data: message });
    assert.equal(h.captures().length, 1);
  });
}

test('ordinary pageshow preserves live confirmation and passive resets cancel pending identity reads', async () => {
  const h = harness(); await h.respond('/api2/v2/users/me', { id: 'synthetic-creator' });
  const identity = h.posts.at(-1); h.event('pageshow', { persisted: false }); h.control('refresh_identity');
  assert.deepEqual(h.posts.at(-1), identity);
  let release; h.setFetch(() => new Promise(resolve => { release = resolve; }));
  const pending = h.window.fetch('/api2/v2/users/me');
  await h.respond('/api2/v2/subscriptions', {}, 401);
  release(new Response(JSON.stringify({ id: 'synthetic-creator' })));
  await pending; await flush(); h.control('refresh_identity');
  assert.equal(h.posts.at(-1).type, 'ofca.provisioning.identity.reset');
});

for (const order of ['hook-first', 'url-first']) {
  test(`measured profile to feed to Messages continues capture, observer, popup and access projections (${order})`, async () => {
    const page = harness(), nav = identityNavigation();
    const observer = new DocumentObserverCoordinator({ chromeApi: nav.chromeApi });
    observer.start(); observer.mode = 'full';
    observer.tabs.set(17, { id: 17, document_id: nav.sender.documentId, url: nav.sender.url });
    let delivered = 0;
    const relay = async () => {
      while (delivered < page.posts.length) {
        const post = page.posts[delivered++];
        if (post.type.startsWith('ofca.provisioning.identity.')) await nav.dispatch(post);
        if (post.type === 'ofca.capture.control.status') observer.observe({ type: 'ofca.observer.state', status: post.status }, nav.sender);
      }
    };
    nav.refresh = async () => {
      const action = nav.refreshes.at(-1)[1].action;
      await flush(); page.control(action); await relay();
    };
    await page.respond('/api2/v2/users/me', { id: 'synthetic-creator' }); await relay();
    const owner = await nav.owner.attach(payload());
    const first = await nav.owner.read(owner);
    const socket = new page.window.WebSocket('wss://ws2.onlyfans.com/ws'); socket.open(); await relay();
    const requests = page.requests();
    for (const path of ['/my/profile', '/', '/my/chats']) {
      if (order === 'hook-first') { page.window.history.pushState(null, '', path); await relay(); }
      nav.chromeApi.tabs.onUpdated.emit(17, { url: new URL(path, page.window.location.origin).href }, {
        id: 17, url: new URL(path, page.window.location.origin).href,
      });
      if (order === 'url-first') page.window.history.pushState(null, '', path);
      const current = await nav.owner.read(owner);
      assert.equal(current.state, 'ready'); assert.equal(current.creator_account_id, first.creator_account_id);
      await flush(); await relay();
      assert.equal(observer.snapshot().attached, true);
      const before = page.captures().length;
      await page.respond('/api2/v2/chats', { list: [chat] }); socket.emit({ type: 'new_message', data: message });
      assert.equal(page.captures().length, before + 2);
      for (const post of page.captures().slice(before)) {
        await nav.identity.withCaptureContext(nav.sender, (context, assertCurrent) => {
          assertCurrent();
          assert.equal(assertCaptureContext({ sender: nav.sender, extensionId: nav.chromeApi.runtime.id, context,
            configuration: { creator_account_id: 'synthetic-account', history_acquisition: {
              authorized_platform_creator_id: 'synthetic-creator' } }, boundCreatorAccountId: 'synthetic-account',
            observation: post.observation, consent: nav.consent, delivery: { consent_epoch: nav.consent.consent_epoch } }), 'synthetic-creator');
        });
      }
      const status = { consent: nav.consent, phase: 'full', onlyfans_permission: true,
        observer: { attachment: observer.snapshot().attached ? 'ready' : 'hook_not_armed' },
        delivery: { transport_state: 'authenticated' } };
      assert.equal(statusPresentation({ status, pairing: { state: 'paired' }, desktopRuntimeReachable: true,
        analysisReadiness: { commercial_authority: 'active', analysis_admission: 'admitted' } }).label, 'Ready');
      assert.equal(browserSurface({ consent: status, legal: {} }).capture, 'active');
    }
    assert.equal(page.requests(), requests + 3, 'only the page test sends the three capture requests');
    nav.identity.unregister();
  });
}
