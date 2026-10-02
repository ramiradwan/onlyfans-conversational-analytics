import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { CaptureDeliveryQueue } from '../capture/delivery-queue.mjs';
import { CONSENT_STORAGE_KEY } from '../runtime/consent-controller.mjs';
import { PartitionAwareConsentController } from '../runtime/partition-aware-consent-controller.mjs';
import { createAccountBoundCaptureMessageBridge } from '../transport/account-bound-capture-bridge.mjs';

const bundles = await Promise.all(['page-hook', 'content'].map(async (name) => (
  await build({ entryPoints: [fileURLToPath(new URL(`../${name}.js`, import.meta.url))],
    bundle: true, write: false, format: 'iife', platform: 'browser', target: ['chrome132'],
  })).outputFiles[0].text));
const flush = async () => { for (let i = 0; i < 12; i += 1) await new Promise(setImmediate); };
const event = () => ({ listeners: [], addListener(fn) { this.listeners.push(fn); } });
const control = (action) => ({ type: 'ofca.capture.control', version: 1, action });
const frame = (id) => ({ type: 'new_message', data: {
  id, text: 'Synthetic', chat_id: 'chat-a', chatUserId: 'peer-a', fromUser: { id: 'peer-a' },
  createdAt: '2030-01-01T00:00:00Z',
} });

async function workerCapture(h) {
  let contextCalls = 0;
  let ingested = 0;
  const pageEpoch = crypto.randomUUID();
  const bridge = createAccountBoundCaptureMessageBridge({
    ingestion: {
      rejectBridgeMessage() { return { ok: false, code: 'invalid_bridge_message', retryable: false }; },
      async ingest() { ingested += 1; return { ok: true }; },
    },
    runtime: {
      configuration: { activeDocument: { creator_account_id: 'account-a',
        history_acquisition: { authorized_platform_creator_id: 'creator-a' } } },
      async wake() { return { creatorAccountId: 'account-a' }; },
    },
    provisioningIdentityBridge: { async withCaptureContext(_sender, work) {
      contextCalls += 1;
      return work({ consent_epoch: h.controller.state.consent_epoch,
        sender_key: '1:document-a', page_epoch: pageEpoch, observed_platform_id: 'creator-a' });
    } },
    allowsCapture: () => h.controller.allowsFullCapture(),
    currentConsent: () => h.controller.state,
    diagnostics: { record() {} },
    chromeApi: { runtime: { id: 'synthetic', onMessage: { addListener() {} } } },
  });
  const delivery = {
    type: 'ofca.capture.delivery', version: 1, delivery_id: crypto.randomUUID(),
    created_at_ms: Date.now(), consent_epoch: h.controller.state.consent_epoch,
    observation: {
      event_type: 'chat.observed', observed_at: '2030-01-01T00:00:00Z',
      source_path: '/api2/v2/chats', creator_platform_user_id: 'creator-a',
      context_chat_id: null, page_epoch: pageEpoch,
      record: { chat_id: 'chat-a', platform_user_id: 'peer-a', display_name: 'Peer',
        updated_at: '2030-01-01T00:00:00Z' },
    },
  };
  const sender = { id: 'synthetic', frameId: 0, tab: { id: 1 },
    url: 'https://onlyfans.com/my/chats', documentId: 'document-a', documentLifecycle: 'active' };
  const response = await new Promise((resolve) => {
    assert.equal(bridge.listener(delivery, sender, resolve), true);
  });
  return { response, contextCalls, ingested };
}

function silenceControl(h, action) {
  const tabs = h.controller.chromeApi.tabs;
  const send = tabs.sendMessage;
  tabs.sendMessage = (id, message) => message.action === action
    ? new Promise(() => {}) : send(id, message);
}

async function harness({ initialMode = 'full', document = true, bridgeFirst = false } = {}) {
  const local = { [CONSENT_STORAGE_KEY]: {
    schema: 'ofca-consent/v2', mode: initialMode, resume_mode: initialMode === 'paused' ? 'full' : null,
    policy_revision: '1', updated_at: null, authorization_event_id: crypto.randomUUID(),
    consent_epoch: crypto.randomUUID(),
  } };
  const scripts = [];
  const messages = [];
  const delivered = [];
  const listeners = new Map();
  const bridgeMessages = event();
  let controller;
  let authorized = true;
  let hookContext;
  let bridgeContext;
  let hookStatusEnabled = true;
  let tabPresent = false;
  const posts = [];
  class Socket {
    constructor() { this.readyState = 1; this.listeners = new Map(); }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    removeEventListener(name) { this.listeners.delete(name); }
    emit(id) { this.listeners.get('message')?.({ data: JSON.stringify(frame(id)) }); }
  }
  class Xhr { open() {} send() {} addEventListener() {} }
  const window = {
    location: { origin: 'https://onlyfans.com', href: 'https://onlyfans.com/my/chats' },
    WebSocket: Socket, history: {},
    fetch: async () => new Response(JSON.stringify({ id: 'creator-a' })),
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(fn);
    },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    postMessage(data) {
      posts.push(structuredClone(data));
      if (!hookStatusEnabled && data.action === 'status') return;
      queueMicrotask(() => {
        for (const fn of [...(listeners.get('message') ?? [])]) {
          fn({ data: structuredClone(data), source: window, origin: window.location.origin });
        }
      });
    },
  };
  const chromeApi = {
    runtime: { id: 'synthetic', onMessage: event() },
    storage: { local: {
      async get(keys) { return Object.fromEntries(keys.filter((key) => key in local).map((key) => [key, structuredClone(local[key])])); },
      async set(update) { Object.assign(local, structuredClone(update)); },
      async remove(keys) { for (const key of keys) delete local[key]; },
    }, onChanged: event() },
    permissions: { onAdded: event(), onRemoved: event(), async contains() { return true; }, async remove() { return true; } },
    scripting: {
      async getRegisteredContentScripts() { return structuredClone(scripts); },
      async registerContentScripts(next) { scripts.push(...structuredClone(next)); },
      async unregisterContentScripts() { scripts.length = 0; },
    },
    tabs: {
      async query() { return tabPresent ? [{ id: 1 }] : []; },
      async sendMessage(_id, message) {
        messages.push(structuredClone(message));
        return new Promise((resolve) => {
          let handled = false;
          for (const fn of bridgeMessages.listeners) handled = fn(message, {}, resolve) === true || handled;
          if (!handled) queueMicrotask(() => resolve(undefined));
        });
      },
      async reload() { assert.fail('unexpected reload'); },
    },
  };
  const options = {
    chromeApi, runtime: { async start() {}, async suspend() {} },
    adapter: { async loadBrainBinding() {}, async clearBrainBinding() {} },
    provisioningIdentityBridge: { register() {}, async clearContexts() {} },
    previewMetrics: { async record() {}, async summary() { return {}; }, async clear() {}, async prune() {} },
    clearLocalData: async () => {}, fetchImpl: async () => ({ ok: true }),
    activeModeAuthorization: { async authorizeTransition() { return true; }, async authorizeResume() { return true; },
      async reconcileActiveMode() { return authorized; } },
  };
  async function restart() {
    chromeApi.runtime.onMessage.listeners.length = 0;
    controller = new PartitionAwareConsentController(options);
    await controller.initialize();
    await flush();
    return controller;
  }
  const sender = { id: 'synthetic', frameId: 0, tab: { id: 1 }, url: window.location.href };
  async function install() {
    tabPresent = true;
    const common = { window, URL, TextEncoder, TextDecoder, crypto, structuredClone,
      AbortController, DOMException, setTimeout, clearTimeout, console };
    hookContext = vm.createContext({ ...common, XMLHttpRequest: Xhr,
      __OFCA_CAPTURE_MODE__: initialMode === 'preview' ? 'preview' : 'full' });
    if (!bridgeFirst) vm.runInContext(bundles[0], hookContext);
    bridgeContext = vm.createContext({ ...common, chrome: { runtime: {
      onMessage: bridgeMessages, lastError: null,
      sendMessage(message, callback) {
        if (message.type === 'ofca.capture.context.query') {
          callback({ ok: true, consent_epoch: controller.state.consent_epoch });
          return;
        }
        if (message.type === 'ofca.capture.delivery' || message.type === 'ofca.preview.observation'
          || message.type.startsWith('ofca.provisioning.identity.')) {
          delivered.push(structuredClone(message)); callback({ ok: true }); return;
        }
        let handled = false;
        for (const fn of chromeApi.runtime.onMessage.listeners) handled = fn(message, sender, callback) === true || handled;
        if (!handled) callback({ ok: false });
      },
    } } });
    vm.runInContext(bundles[1], bridgeContext);
    await flush();
    if (bridgeFirst) { vm.runInContext(bundles[0], hookContext); await flush(); }
    await window.fetch('/api2/v2/users/me');
    await flush();
  }
  await restart();
  if (document) await install();
  return { get controller() { return controller; }, window, delivered, posts, messages, scripts, local, restart, install,
    authorize(value) { authorized = value; },
    oldHook() { hookStatusEnabled = false; },
    get hook() { return hookContext.__OFCA_PAGE_HOOK_CONTROLLER__; },
    captureIds: () => delivered.filter((m) => m.type === 'ofca.capture.delivery').map((m) => m.observation.record?.message_id),
    relay(data) { window.postMessage(data); },
  };
}

test('a bridge confirmed before the Full hook installs still arms the new document', async () => {
  const h = await harness({ bridgeFirst: true });
  new h.window.WebSocket('wss://ws2.onlyfans.com/ws').emit('late-hook');
  await flush();
  assert.deepEqual(h.captureIds(), ['late-hook']);
  assert.equal((await h.controller.status()).reload_required, false);
});

test('RC5 soft pause keeps the original socket armed and forwards a later frame', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  const constructor = h.window.WebSocket;
  socket.emit('before'); await flush();
  assert.deepEqual(h.captureIds(), ['before']);
  await h.controller.setMode('pause'); await flush();
  assert.equal(h.controller.allowsFullCapture(), false);
  await assert.rejects(h.controller.captureScope.run(() => assert.fail('paused scope admitted capture')));
  assert.equal(h.window.WebSocket, constructor);
  assert.equal(h.scripts.length, 2);
  assert.equal((await h.controller.status()).reload_required, false);
  await h.controller.setMode('resume'); await flush();
  socket.emit('after'); await flush();
  assert.deepEqual(h.captureIds(), ['before', 'after']);
  assert.equal((await h.controller.status()).reload_required, false);
});

test('privacy drops paused bridge posts without buffering or flushing on resume', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  socket.emit('sample'); await flush();
  const observation = h.posts.find((m) => m.type === 'ofca.capture.observation');
  assert.ok(observation);
  await h.controller.setMode('pause'); await flush();
  const count = h.delivered.length;
  socket.emit('paused-socket');
  h.relay({ ...observation, observation: { ...observation.observation,
    record: { ...observation.observation.record, message_id: 'paused-bridge' } } });
  await flush();
  assert.equal(h.delivered.length, count, 'zero bridge posts during pause');
  await h.controller.setMode('resume'); await flush();
  socket.emit('resumed'); await flush();
  assert.deepEqual(h.captureIds(), ['sample', 'resumed'], 'paused content never flushes');
});

test('forged page resume cannot deliver captures through a paused bridge or flush them later', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  await h.controller.setMode('pause'); await flush();
  const postsBefore = h.posts.length;
  h.relay(control('resume')); await flush();
  socket.emit('forged-resume'); await flush();
  assert.equal(h.posts.slice(postsBefore).some((m) => m.type === 'ofca.capture.observation'
    && m.observation.record?.message_id === 'forged-resume'), true,
    'the forged control rearms the page hook');
  assert.deepEqual(h.captureIds(), [], 'the paused bridge sends no capture delivery');
  await h.controller.setMode('resume'); await flush();
  socket.emit('legitimate-resume'); await flush();
  assert.deepEqual(h.captureIds(), ['legitimate-resume'], 'the paused frame never flushes');
});

test('silent pause permanently rejects Full delivery and reports reload required', async () => {
  const h = await harness();
  silenceControl(h, 'pause');
  await h.controller.setMode('pause'); await flush();
  assert.deepEqual(await workerCapture(h), {
    response: { ok: false, code: 'capture_disabled', retryable: false },
    contextCalls: 0, ingested: 0,
  });
  assert.equal(h.controller.allowsFullCapture(), false);
  assert.equal((await h.controller.status()).reload_required, true);
  let attempts = 0;
  const queue = new CaptureDeliveryQueue({ send: async () => {
    attempts += 1;
    return (await workerCapture(h)).response;
  } });
  await assert.rejects(queue.enqueue({ created_at_ms: Date.now(), type: 'ofca.capture.delivery' }),
    { code: 'capture_disabled' });
  await h.controller.setMode('resume'); await flush();
  assert.equal(attempts, 1, 'the rejected delivery never retries after resume');
});

test('silent Full to Preview stop rejects Full delivery and reports reload required', async () => {
  const h = await harness();
  silenceControl(h, 'stop');
  await h.controller.setMode('preview'); await flush();
  assert.deepEqual(await workerCapture(h), {
    response: { ok: false, code: 'capture_disabled', retryable: false },
    contextCalls: 0, ingested: 0,
  });
  assert.equal(h.controller.allowsFullCapture(), false);
  assert.equal((await h.controller.status()).reload_required, true);
});

test('worker restart derives reload requirement from stopped and live documents', async () => {
  const h = await harness();
  assert.equal((await h.controller.status()).reload_required, false);
  await h.controller.setMode('pause'); await flush();
  await h.restart();
  assert.equal((await h.controller.status()).reload_required, false);
  h.relay(control('stop')); await flush();
  await h.restart();
  assert.equal((await h.controller.status()).reload_required, true);
});

test('old hook without status response requires reload within the one second deadline', async () => {
  const h = await harness(); h.oldHook();
  const start = performance.now();
  assert.equal((await h.controller.status()).reload_required, true);
  assert.ok(performance.now() - start < 1500);
});

for (const refusal of ['undefined', 'null', 'rejected']) test(`a legacy bridge with ${refusal} pause acknowledgement is stopped for privacy`, async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  const tabs = h.controller.chromeApi.tabs;
  const send = tabs.sendMessage;
  tabs.sendMessage = (id, message) => {
    if (message.action !== 'pause') return send(id, message);
    if (refusal === 'rejected') return Promise.reject(new Error('no_response'));
    return Promise.resolve(refusal === 'null' ? null : undefined);
  };
  await h.controller.setMode('pause'); await flush();
  const count = h.delivered.length;
  socket.emit('legacy-pause'); await flush();
  assert.equal(h.delivered.length, count);
  assert.equal((await h.controller.status()).reload_required, true);
});

test('automatic Full pause and a new paused document keep forwarding closed', async () => {
  const h = await harness(); h.authorize(false);
  await h.controller.reconcile(); await flush();
  assert.equal(h.scripts.length, 2);
  assert.equal(h.controller.allowsFullCapture(), false);
  const fresh = await harness({ initialMode: 'paused' });
  new fresh.window.WebSocket('wss://ws2.onlyfans.com/ws').emit('paused-new-document');
  await flush();
  assert.deepEqual(fresh.delivered, []);
  assert.equal((await fresh.controller.status()).reload_required, false);
});

test('policy revision validation soft pauses an installed Full hook across restart', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  h.local[CONSENT_STORAGE_KEY].policy_revision = 'older';
  await h.restart();
  assert.equal(h.controller.state.mode, 'paused');
  assert.equal(h.scripts.length, 2);
  assert.equal((await h.controller.status()).reload_required, false);
  socket.emit('paused-policy'); await flush();
  assert.deepEqual(h.captureIds(), []);
  await h.controller.setMode('resume'); await flush();
  socket.emit('resumed-policy'); await flush();
  assert.deepEqual(h.captureIds(), ['resumed-policy']);
});

test('reload status checks every tab instead of accepting the first healthy hook', async () => {
  const h = await harness();
  const tabs = h.controller.chromeApi.tabs;
  const send = tabs.sendMessage;
  tabs.query = async () => [{ id: 1 }, { id: 2 }];
  tabs.sendMessage = (id, message) => id === 2 ? Promise.resolve(null) : send(id, message);
  assert.equal((await h.controller.status()).reload_required, true);
});

test('Full epoch replacement refreshes identity without stopping the socket', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  const before = h.controller.state.consent_epoch;
  h.messages.length = 0;
  await h.controller.setMode('full', { evidenceEventId: crypto.randomUUID() }); await flush();
  assert.notEqual(h.controller.state.consent_epoch, before);
  assert.ok(h.messages.some((m) => m.action === 'refresh_identity'));
  assert.ok(!h.messages.some((m) => m.action === 'stop'));
  socket.emit('new-epoch'); await flush();
  assert.deepEqual(h.captureIds(), ['new-epoch']);
  assert.equal(h.delivered.find((m) => m.type === 'ofca.capture.delivery').consent_epoch, h.controller.state.consent_epoch);
});

test('resume reopens identity observation while the binding is being recovered', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  await h.controller.setMode('pause');
  h.controller.adapter.loadBrainBinding = async () => { throw new Error('binding_pending'); };
  const status = await h.controller.setMode('resume'); await flush();
  assert.equal(status.reload_required, false);
  assert.equal(h.controller.phase, 'identity');
  assert.equal(h.controller.allowsFullCapture(), false);
  assert.equal(h.scripts[0].id, 'ofca-full-main');
  assert.ok(h.delivered.some((m) => m.type === 'ofca.provisioning.identity.update'));
  h.controller.adapter.loadBrainBinding = async () => {};
  await h.controller.reconcile(); await flush();
  socket.emit('recovered'); await flush();
  assert.deepEqual(h.captureIds(), ['recovered']);
});

for (const transition of ['preview', 'full', 'revoked', 'off', 'account']) {
  test(`hard transition ${transition} still stops the hook and requires reload`, async () => {
    const h = await harness({ initialMode: transition === 'full' ? 'preview' : 'full' });
    const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
    if (transition === 'off') await h.controller.deleteLocalData();
    else if (transition === 'account') {
      h.controller.storageListener({ active_account_partition_v5: { oldValue: 'partition-a', newValue: 'partition-b' } }, 'session');
      await h.controller.status();
    } else await h.controller.setMode(transition);
    await flush(); socket.emit('stopped'); await flush();
    assert.equal(h.hook, undefined);
    assert.deepEqual(h.captureIds(), []);
    if (['off', 'revoked'].includes(transition)) await h.controller.setMode('full');
    assert.equal((await h.controller.status()).reload_required, true);
  });
}

test('a queued account switch stays hard when followed by a Full epoch replacement', async () => {
  const h = await harness();
  const socket = new h.window.WebSocket('wss://ws2.onlyfans.com/ws');
  h.controller.storageListener({ active_account_partition_v5: {
    oldValue: 'partition-a', newValue: 'partition-b',
  } }, 'session');
  await h.controller.setMode('full', { evidenceEventId: crypto.randomUUID() });
  await flush(); socket.emit('retired-account'); await flush();
  assert.deepEqual(h.captureIds(), []);
  assert.equal((await h.controller.status()).reload_required, true);
});
