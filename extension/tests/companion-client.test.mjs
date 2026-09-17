import test from 'node:test';
import assert from 'node:assert/strict';
import { createCompanionClient, PAIRING_PORT_NAME } from '../runtime/companion-client.mjs';
import { fixturePrivateJwk, vector } from '../test-fixtures/pairing/vendored-vector.mjs';
import { lp, unb64u } from '../transport/pairing-contract.mjs';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const key = await crypto.subtle.importKey('jwk', fixturePrivateJwk(vector.fixture_labels.agent_identity_key, vector.request.agent_identity_jwk), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
const publicKey = await crypto.subtle.importKey('jwk', vector.request.agent_identity_jwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
const ACCOUNT = vector.detected_account_id;
const STORAGE_KEY = Buffer.alloc(32, 7).toString('base64');
function event() { const listeners = []; return { listeners, addListener(fn) { listeners.push(fn); }, removeListener(fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); } }; }
function area() { const values = {}; return { values, async get(keys) { return Object.fromEntries(keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]])); }, async set(update) { Object.assign(values, structuredClone(update)); }, async remove(keys) { for (const key of keys) delete values[key]; } }; }

function harness({ full = true, channelGate = null, unsealGate = null, wrongPin = false, malformed = false, chromeApi = null, refusal = null } = {}) {
  const stats = { stores: 0, snow: 0, networks: 0, cancel: 0, forget: 0, proofValid: false, closedStores: 0 }, channels = [];
  const chrome = chromeApi ?? { runtime: { id: 'a'.repeat(32), getURL: (path) => `chrome-extension://${'a'.repeat(32)}/${path}`, onConnect: event(), onStartup: event(), onInstalled: event(), onMessage: event() }, storage: { local: area(), session: area() }, tabs: { onUpdated: event() }, alarms: { onAlarm: event(), async create() {} } };
  let enabled = full, account = ACCOUNT, paired = true, pairingWait;
  const pairingStore = {
    async identity() { return { privateKey: key }; }, async status() { return { paired }; },
    async begin() { return { requestId: 'request-one', deadline: Math.floor(Date.now() / 1000) + 300, request: vector.request }; },
    async acceptOffer() { return { confirm: vector.confirm, comparisonCode: vector.expected.comparison_code }; },
    async cancel() { stats.cancel++; }, async forget() { paired = false; stats.forget++; }, close() { stats.closedStores++; },
  };
  const client = createCompanionClient({ chromeApi: chrome, allowsFull: () => enabled, detectedAccountId: async () => account,
    accountDatabaseName: async (id) => `encrypted-account-${id}`, storeFactory: async () => { stats.stores++; return pairingStore; },
    loadSnow: async () => { stats.snow++; return { SnowSession: class {}, generateStaticKeypair() { return new Uint8Array(64); } }; },
    loadTrust: async () => ({}),
    wireFactory: async (url, options) => {
      assert.equal(url, 'ws://127.0.0.1:17871/ws/agent/pairing');
      if (refusal === 'unreachable') throw new Error('companion_session_refused');
      let receives = 0, closeReason = null;
      return { send: async () => {}, close() {}, get closeReason() { return closeReason; }, receive: async () => {
        if (++receives === (refusal === 'before' ? 1 : 2) && refusal) { closeReason = 'pairing_state_refused'; throw new Error('companion_session_refused'); }
        if (receives === 1) return JSON.stringify(vector.offer);
        pairingWait = deferred();
        options.signal.addEventListener('abort', () => pairingWait.resolve('cancelled'), { once: true });
        return pairingWait.promise;
      } };
    },
    channelFactory: async (options) => {
      stats.networks++; assert.equal(options.url, 'ws://127.0.0.1:17871/ws/agent');
      const index = stats.networks, closeListeners = [], rpcCalls = [], documents = [];
      const challenge = { challenge_id: crypto.randomUUID(), challenge: Buffer.alloc(32, 9).toString('base64url'), session_id: crypto.randomUUID(), expires_at: '2026-09-12T12:00:00Z' };
      const channel = { identity: { ...vector.expected.identity, pairing_id: vector.offer.pairing_id, ...(wrongPin ? { creator_account_id: 'other' } : {}) }, closed: false, rpcCalls, documents,
        close() { if (this.closed) return; this.closed = true; for (const fn of closeListeners) fn(); },
        onClose(fn) { closeListeners.push(fn); }, onMessage(fn) { this.message = fn; return () => { this.message = null; }; },
        async send(document) { documents.push(document); },
        async rpc(method, params) {
          rpcCalls.push({ method, params });
          if (method === 'agent.challenge') return challenge;
          if (method === 'agent.authenticate') {
            const id = chrome.storage.local.values.agent_installation_id;
            stats.proofValid = await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, publicKey, unb64u(params.signature), lp('OFCA-AGENT-REQUEST-V1', [challenge.session_id, challenge.challenge, 'POST', '/agent/session-ticket', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'agent-websocket', id, ACCOUNT, vector.offer.pairing_id, vector.expected.identity.installation_id]));
            return { creator_account_id: ACCOUNT, auth_ticket: `fresh-ticket-${index}`, storage_bootstrap: 'sealed-bootstrap' };
          }
          if (method === 'agent.storage.unseal') {
            if (unsealGate) await unsealGate.promise;
            return { schema: 'ofca-extension-storage-unlock/v1', creator_account_id: ACCOUNT, credential_kind: 'pairing', auth_ticket: `fresh-ticket-${index}`, storage_key_base64: STORAGE_KEY, ...(malformed ? { extra: true } : {}) };
          }
          if (method === 'agent.storage.rotate') return { schema: 'ofca-extension-storage-rotation/v1', storage_bootstrap: 'replacement-bootstrap' };
          if (method === 'agent.config.get') return { status: 304, etag: 'config-1', document: null };
          throw new Error('test_method_missing');
        },
      };
      channels.push(channel); options.signal.addEventListener('abort', () => channel.close(), { once: true });
      if (channelGate) await channelGate.promise;
      return channel;
    },
  });
  return { client, stats, channels, chrome, setFull(value) { enabled = value; }, setAccount(value) { account = value; client.invalidate(); },
    unpair() { paired = false; }, pairingResult() { pairingWait.resolve(JSON.stringify(vector.result)); } };
}

test('Preview and unavailable modes never initialize companion storage, crypto or networking', async () => {
  const h = harness({ full: false });
  assert.deepEqual(await h.client.status(), { state: 'unavailable', comparison_code: null });
  await assert.rejects(h.client.adapter.loadBrainBinding()); await assert.rejects(h.client.pair());
  assert.equal(h.stats.stores + h.stats.snow + h.stats.networks, 0);
});

test('current Agent proof, storage unseal, configuration and rotation use only the encrypted channel', async () => {
  const h = harness();
  try {
    const binding = await h.client.adapter.loadBrainBinding();
    assert.equal(binding.creatorAccountId, ACCOUNT); assert.equal(binding.storageKey, STORAGE_KEY); assert.equal(h.stats.proofValid, true);
    assert.deepEqual(Object.keys(h.chrome.storage.local.values), ['agent_installation_id']);
    assert.deepEqual(Object.keys(h.chrome.storage.session.values), ['active_account_partition_v5']);
    const id = await h.client.adapter.loadAgentInstallationId();
    const context = { authTicket: 'config-ticket', creatorAccountId: ACCOUNT, agentInstallationId: id, currentEtag: null, currentConfigRevision: null, supportedSchemaVersions: ['2'] };
    assert.equal((await h.client.configAdapter.fetchConfig(context)).status, 304);
    await h.client.adapter.saveReconnectAuthTicket({ creatorAccountId: ACCOUNT, agentInstallationId: id, authTicket: 'reconnect-ticket', configAuthTicket: 'config-ticket' });
    assert.equal(await h.client.adapter.loadReconnectAuthTicket(), null);
    assert.deepEqual(h.channels[0].rpcCalls.map((call) => call.method), ['agent.challenge', 'agent.authenticate', 'agent.storage.unseal', 'agent.config.get', 'agent.storage.rotate']);
    assert.equal(JSON.stringify([h.chrome.storage.local.values, h.chrome.storage.session.values]).includes('ticket'), false);
    assert.equal(JSON.stringify(await h.client.status()).includes('bootstrap'), false);
  } finally { h.client.invalidate(); }
});

test('each reconnect reconstructs authority with a new proof instead of durable credentials', async () => {
  const h = harness();
  try {
    const first = await h.client.adapter.loadBrainBinding();
    h.channels[0].close();
    const second = await h.client.adapter.loadBrainBinding();
    assert.notEqual(first.authTicket, second.authTicket); assert.equal(h.stats.networks, 2);
    assert.equal(h.channels[1].rpcCalls[0].method, 'agent.challenge');
    const restarted = harness({ chromeApi: h.chrome });
    try { await restarted.client.adapter.loadBrainBinding(); assert.equal(restarted.stats.proofValid, true); }
    finally { restarted.client.invalidate(); }
  } finally { h.client.invalidate(); }
});

test('account mismatch and unexpected unseal fields never publish a Full binding', async () => {
  for (const options of [{ wrongPin: true }, { malformed: true }]) {
    const h = harness(options);
    await assert.rejects(h.client.adapter.loadBrainBinding(), { message: 'companion_session_refused' });
    assert.equal(h.client.connected, false); assert.equal(h.channels[0].closed, true);
    assert.deepEqual(h.chrome.storage.session.values, {});
    if (options.wrongPin) assert.equal(h.channels[0].rpcCalls.length, 0);
  }
});

test('invalidation fences a late channel and a late storage-key response', async () => {
  for (const phase of ['channelGate', 'unsealGate']) {
    const gate = deferred(), h = harness({ [phase]: gate });
    const operation = h.client.adapter.loadBrainBinding();
    const rejected = assert.rejects(operation);
    for (let i = 0; i < 10 && (!h.channels.length || (phase === 'unsealGate' && h.channels[0].rpcCalls.length < 3)); i++) await tick();
    h.setAccount('another-account');
    await rejected; gate.resolve(); await tick();
    assert.equal(h.channels[0].closed, true); assert.equal(h.client.connected, false);
    assert.deepEqual(h.chrome.storage.session.values, {});
  }
});

test('closing a connecting protocol facade aborts its pending protected connection', async () => {
  const gate = deferred(), h = harness({ channelGate: gate });
  const socket = h.client.webSocketFactory(); await tick(); socket.close(); gate.resolve(); await tick();
  assert.equal(h.client.connected, false); assert.equal(socket.readyState, 3);
  assert.equal(h.channels[0].closed, true);
});

test('protocol facade publishes only the fresh authenticated ticket and encrypted documents', async () => {
  const h = harness(), socket = h.client.webSocketFactory();
  try {
    await new Promise((resolve) => { socket.onopen = resolve; });
    assert.equal(socket.authTicket, 'fresh-ticket-1');
    const document = { protocol_version: '2', type: 'agent.heartbeat' };
    socket.send(JSON.stringify(document)); await tick(); assert.deepEqual(h.channels[0].documents, [document]);
    const received = new Promise((resolve) => { socket.onmessage = resolve; });
    h.channels[0].message(document); assert.deepEqual(JSON.parse((await received).data), document);
  } finally { socket.close(); }
});

test('popup pairing is restricted to the packaged popup and disconnect cancels comparison', async () => {
  const h = harness(); h.unpair(); h.client.registerPopup();
  const listener = h.chrome.runtime.onConnect.listeners[0];
  const foreign = { name: PAIRING_PORT_NAME, sender: { id: h.chrome.runtime.id, url: 'https://onlyfans.com/' } };
  listener(foreign); assert.equal(h.stats.stores, 0);
  const states = [], port = { name: PAIRING_PORT_NAME, sender: { id: h.chrome.runtime.id, url: h.chrome.runtime.getURL('popup.html#pairing') }, onMessage: event(), onDisconnect: event(), postMessage(state) { states.push(state); } };
  listener(port); await tick(); port.onMessage.listeners[0]({ type: 'pair' });
  for (let i = 0; i < 10 && !states.some((state) => state.state === 'compare'); i++) await tick();
  assert.equal(states.find((state) => state.state === 'compare').comparison_code, vector.expected.comparison_code);
  port.onDisconnect.listeners[0](); await tick(); assert.equal(h.stats.cancel, 1); assert.equal(h.stats.networks, 0);
  assert.ok(states.every((state) => Object.keys(state).sort().join() === 'comparison_code,state'));
});

test('the transient toolbar popup can inspect status but cannot start a comparison', async () => {
  const h = harness(); h.unpair(); h.client.registerPopup();
  const port = { name: PAIRING_PORT_NAME, sender: { id: h.chrome.runtime.id, url: h.chrome.runtime.getURL('popup.html') }, onMessage: event(), onDisconnect: event(), postMessage() {} };
  h.chrome.runtime.onConnect.listeners[0](port); await tick();
  port.onMessage.listeners[0]({ type: 'pair' }); await tick();
  assert.equal(h.stats.snow, 0); assert.equal(h.stats.networks, 0);
});

test('a refusal before any offer reports that the desktop app is not ready for pairing', async () => {
  for (const [refusal, expected] of [['before', 'desktop_not_ready'], ['after', 'pairing_failed'], ['unreachable', 'pairing_failed']]) {
    const h = harness({ refusal }); h.unpair();
    await assert.rejects(h.client.pair(), { message: 'companion_session_refused' });
    assert.deepEqual(await h.client.status(), { state: expected, comparison_code: null }, refusal);
    assert.equal(h.stats.cancel, 1); assert.equal(h.stats.networks, 0);
  }
});

test('cancelling a comparison returns to unpaired and expiry reports a failure', async (t) => {
  const cancelled = harness(); cancelled.unpair();
  const controller = new AbortController(), operation = cancelled.client.pair({ signal: controller.signal });
  const rejected = assert.rejects(operation);
  for (let i = 0; i < 20 && (await cancelled.client.status()).state !== 'compare'; i++) await tick();
  controller.abort(); await rejected;
  assert.deepEqual(await cancelled.client.status(), { state: 'unpaired', comparison_code: null });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const expiring = harness(); expiring.unpair();
  const expiry = assert.rejects(expiring.client.pair());
  for (let i = 0; i < 20 && (await expiring.client.status()).state !== 'compare'; i++) await tick();
  t.mock.timers.tick(300_000); await expiry;
  assert.deepEqual(await expiring.client.status(), { state: 'pairing_failed', comparison_code: null });
});

test('forget closes the current session before deleting its pin', async () => {
  const h = harness(); await h.client.adapter.loadBrainBinding(); await h.client.forget();
  assert.equal(h.channels[0].closed, true); assert.equal(h.stats.forget, 1);
  assert.deepEqual(await h.client.status(), { state: 'unpaired', comparison_code: null });
  assert.equal(h.chrome.storage.local.values.agent_installation_id.length, 36);
});

test('startup has a ten-second total deadline even when crypto loading never returns', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness({ channelGate: deferred() });
  const operation = h.client.adapter.loadBrainBinding(); const rejected = assert.rejects(operation);
  await tick(); t.mock.timers.tick(10000); await rejected;
  assert.equal(h.channels[0].closed, true); assert.equal(h.client.connected, false);
});
