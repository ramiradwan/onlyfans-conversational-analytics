import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { CAPTURE_LIMITS } from '../capture/delivery-queue.mjs';
import { readBoundedJson } from '../capture/bounded-json.mjs';

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
  vm.runInContext(bundle, vm.createContext({ window, XMLHttpRequest: Xhr, URL, crypto,
    TextEncoder, TextDecoder, AbortController, setTimeout, clearTimeout, console,
    __OFCA_CAPTURE_MODE__: mode,
  }));
  async function respond(path, body, status = 200) {
    next = () => Promise.resolve(new Response(JSON.stringify(body), { status }));
    await window.fetch(path); await flush();
  }
  return { window, posts, respond, Xhr, setFetch: (impl) => { next = impl; },
    captures: () => posts.filter((post) => post.observation?.record),
  };
}

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

test('unknown Full identity produces no content while Preview remains identifier free', async () => {
  const full = harness(); await full.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(full.captures().length, 0);
  const preview = harness('preview');
  await preview.respond('/api2/v2/users/me', { id: 'creator-a' });
  await preview.respond('/api2/v2/chats', { list: [chat] });
  assert.equal(preview.posts.length, 1);
  assert.deepEqual(Object.keys(preview.posts[0].observation).sort(), ['kind', 'observed_at']);
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
