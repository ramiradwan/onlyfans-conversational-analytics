import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { CAPTURE_LIMITS } from '../capture/delivery-queue.mjs';
import { readBoundedJson } from '../capture/bounded-json.mjs';
import { createProvisioningCompanionGuard } from '../runtime/provisioning-companion-guard.mjs';

const bundle = (await build({ entryPoints: [fileURLToPath(new URL('../page-hook.js', import.meta.url))],
  bundle: true, format: 'iife', platform: 'browser', target: ['chrome132'], write: false,
})).outputFiles[0].text;
const flush = () => new Promise((resolve) => setImmediate(resolve));
const chat = { id: 'chat-a', withUser: { id: 'fan-a', name: 'Synthetic' }, updatedAt: '2030-01-01T00:00:00Z' };
const message = { id: 'message-a', text: 'Synthetic', fromUser: { id: 'fan-a' },
  chatUserId: 'fan-a', chat_id: 'chat-a', createdAt: '2030-01-01T00:00:00Z' };
function harness(mode = 'full') {
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
  async function respond(path, body, status = 200) {
    next = () => Promise.resolve(new Response(JSON.stringify(body), { status }));
    await window.fetch(path); await flush();
  }
  return { window, posts, respond, Xhr, setFetch: (impl) => { next = impl; },
    reinstall() { context.__OFCA_CAPTURE_MODE__ = mode; vm.runInContext(bundle, context); },
    control(action) { listeners.get('message')?.({ source: window, origin: window.location.origin,
      data: { type: 'ofca.capture.control', version: 1, action } }); },
    captures: () => posts.filter((post) => post.observation?.record),
  };
}

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
