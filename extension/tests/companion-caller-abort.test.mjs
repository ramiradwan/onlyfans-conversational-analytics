import test from 'node:test';
import assert from 'node:assert/strict';
import { openCompanionChannel } from '../transport/companion-channel.mjs';
import { createCompanionClient, PAIRING_PORT_NAME } from '../runtime/companion-client.mjs';
import { createFragmentReceiver, fragmentMessage } from '../transport/companion-fragments.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { fixturePrivateJwk, vector, trustSet } from '../test-fixtures/pairing/vendored-vector.mjs';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
async function until(predicate) {
  for (let i = 0; i < 2000 && !predicate(); i++) await tick();
  assert.ok(predicate(), 'controlled phase reached');
}
const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
const key = await crypto.subtle.importKey('jwk', fixturePrivateJwk(vector.fixture_labels.agent_identity_key, vector.request.agent_identity_jwk), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
const readiness = { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' };
const event = () => { const listeners = new Set(); return { addListener: (fn) => listeners.add(fn), emit: (value) => { for (const fn of listeners) fn(value); } }; };
const area = () => { const values = {}; return { async get() { return { ...values }; }, async set(update) { Object.assign(values, update); }, async remove() {} }; };

class FakeSnow {
  write_handshake() { return new Uint8Array([1]); }
  read_handshake() {}
  handshake_finished() { return true; }
  enter_transport() {}
  encrypt_transport(value) { return value; }
  decrypt_transport(value) { return value; }
  close() {}
  free() {}
}

function peer({ auto = false } = {}) {
  const requests = [], fragments = createFragmentReceiver();
  let socket, phase = 0;
  const emit = (value) => queueMicrotask(() => { if (socket.readyState === 1) socket.onmessage({ data: value.slice().buffer }); });
  const application = (document) => { for (const frame of fragmentMessage(document)) emit(new Uint8Array([1, ...frame])); };
  const reply = (request, result = {}) => application({ type: 'rpc.response', id: request.id, result });
  const store = {
    onInvalidate() { return () => {}; },
    async sessionMaterial() { return { privateKey: new Uint8Array(32), peerKey: new Uint8Array(32), pairingId: vector.offer.pairing_id,
      pairingDigest: new Uint8Array(32), identity: vector.expected.identity, commit: null }; },
  };
  function factory() {
    socket = { readyState: 1, bufferedAmount: 0, close() { this.readyState = 3; }, send(value) {
      if (phase++ === 0) { emit(new Uint8Array([1])); return; }
      if (phase === 2) {
        emit(new Uint8Array([0, ...new TextEncoder().encode('server-ready')]));
        emit(new Uint8Array([1, ...new TextEncoder().encode(JSON.stringify(vector.session_authorization))]));
        return;
      }
      const document = fragments.receive(value.slice(1));
      if (!document) return;
      requests.push(document);
      if (!auto) return;
      if (document.method === 'agent.challenge') reply(document, { challenge_id: 'test', challenge: Buffer.alloc(32, 9).toString('base64url'), session_id: 'test', expires_at: '2026-09-12T12:00:00Z' });
      if (document.method === 'agent.authenticate') reply(document, { creator_account_id: vector.detected_account_id, auth_ticket: 'test-ticket', storage_bootstrap: 'test-bootstrap' });
      if (document.method === 'agent.storage.unseal') reply(document, { schema: 'ofca-extension-storage-unlock/v1', creator_account_id: vector.detected_account_id, credential_kind: 'pairing', auth_ticket: 'test-ticket', storage_key_base64: Buffer.alloc(32, 7).toString('base64') });
    } };
    queueMicrotask(() => socket.onopen());
    return socket;
  }
  return { requests, application, reply, open: (options = {}) => openCompanionChannel({ ...options, url: 'fake:', store, SnowSession: FakeSnow,
    accountId: vector.detected_account_id, trust, clock: () => vector.now, webSocketFactory: factory }) };
}

function harness(t, { gate = null, paired = true } = {}) {
  const peers = [], channels = [], pairingResult = deferred();
  let account = vector.detected_account_id, cancelled = 0, wireClosed = false, pairingWaiting = false;
  const chromeApi = { runtime: { id: 'a'.repeat(32), getURL: (path) => `chrome-extension://${'a'.repeat(32)}/${path}`, onConnect: event() }, storage: { local: area(), session: area() } };
  const client = createCompanionClient({ chromeApi, allowsFull: () => true, detectedAccountId: async () => account,
    accountDatabaseName: async () => 'test', loadSnow: async () => ({}), loadTrust: async () => trust,
    storeFactory: async () => ({ async identity() { return { privateKey: key }; }, async status() { return { paired }; },
      async begin() { return { requestId: 'test', request: vector.request, deadline: Math.floor(Date.now() / 1000) + 300 }; },
      async acceptOffer() { return { confirm: vector.confirm, comparisonCode: vector.expected.comparison_code }; },
      async cancel() { cancelled++; }, async forget() { paired = false; } }),
    wireFactory: async (_url, { signal }) => {
      signal.addEventListener('abort', () => { wireClosed = true; pairingResult.resolve('cancelled'); }, { once: true });
      let receives = 0;
      return { async send() {}, close() { wireClosed = true; }, async receive() {
        if (++receives === 1) return JSON.stringify(vector.offer);
        pairingWaiting = true;
        return pairingResult.promise;
      } };
    },
    channelFactory: async (options) => {
      const p = peer({ auto: true }); peers.push(p);
      if (gate) await gate.promise;
      const channel = await p.open(options); channels.push(channel); return channel;
    },
  });
  t.after(() => client.invalidate());
  function port(surface = 'popup') {
    const values = [], onMessage = event(), onDisconnect = event();
    const value = { name: PAIRING_PORT_NAME, sender: { id: chromeApi.runtime.id, url: chromeApi.runtime.getURL(`${surface}.html`) },
      onMessage, onDisconnect, postMessage: (message) => values.push(message) };
    chromeApi.runtime.onConnect.emit(value);
    return { values, send: (type) => onMessage.emit({ type }), close: () => onDisconnect.emit() };
  }
  return { client, peers, channels, port, pairingResult, setAccount: (value) => { account = value; },
    get cancelled() { return cancelled; }, get wireClosed() { return wireClosed; }, get pairingWaiting() { return pairingWaiting; } };
}

async function abandoned(t) {
  const p = peer(), channel = await p.open(); t.after(() => channel.close());
  const controller = new AbortController();
  const rejected = assert.rejects(channel.rpc('test', {}, { signal: controller.signal }));
  await until(() => p.requests.length === 1); controller.abort(); await rejected;
  return { p, channel };
}

test('caller abort rejects only its pending request and a second request resolves', async (t) => {
  const { p, channel } = await abandoned(t);
  assert.equal(channel.closed, false);
  const result = channel.rpc('second');
  await until(() => p.requests.length === 2); p.reply(p.requests[1], { ok: true });
  assert.deepEqual(await result, { ok: true });
});

test('late reply for an abandoned id leaves the channel open', async (t) => {
  const { p, channel } = await abandoned(t);
  p.reply(p.requests[0]); await tick();
  assert.equal(channel.closed, false);
});

test('reply for an id never issued closes the channel', async (t) => {
  const p = peer(), channel = await p.open(); t.after(() => channel.close());
  p.reply({ id: 'never-issued' }); await tick(); assert.equal(channel.closed, true);
});

test('reply timeout still closes the channel at ten seconds', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const p = peer(), channel = await p.open(); t.after(() => channel.close());
  const rejected = assert.rejects(channel.rpc('test'));
  await until(() => p.requests.length === 1);
  t.mock.timers.tick(9999); assert.equal(channel.closed, false);
  t.mock.timers.tick(1); await rejected; assert.equal(channel.closed, true);
});

test('malformed reply for an abandoned id still closes the channel', async (t) => {
  const { p, channel } = await abandoned(t);
  p.application({ type: 'rpc.response', id: p.requests[0].id, result: {}, extra: true });
  await tick(); assert.equal(channel.closed, true);
});

test('abandoned id expires at the original reply deadline', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const p = peer(), channel = await p.open(); t.after(() => channel.close());
  const controller = new AbortController();
  const rejected = assert.rejects(channel.rpc('test', {}, { signal: controller.signal }));
  await until(() => p.requests.length === 1);
  t.mock.timers.tick(9000); controller.abort(); await rejected;
  t.mock.timers.tick(1000); p.reply(p.requests[0]); await tick(); assert.equal(channel.closed, true);
});

test('abandoned id retention is bounded by count', async (t) => {
  const p = peer(), channel = await p.open(); t.after(() => channel.close());
  for (let i = 0; i < 64; i++) {
    const controller = new AbortController();
    const rejected = assert.rejects(channel.rpc('test', {}, { signal: controller.signal }));
    await until(() => p.requests.length === i + 1); controller.abort(); await rejected;
    assert.equal(channel.closed, false);
  }
  const overflow = new AbortController();
  const rejected = assert.rejects(channel.rpc('overflow', {}, { signal: overflow.signal }));
  await tick(); overflow.abort(); await rejected;
  assert.equal(p.requests.length, 64);
  p.reply(p.requests[0]); await tick(); assert.equal(channel.closed, false);
  const next = channel.rpc('next');
  await until(() => p.requests.length === 65); p.reply(p.requests.at(-1)); await next;
});

for (const surface of ['popup', 'setup', 'options']) test(`${surface} disconnect aborts readiness with nothing posted afterwards`, async (t) => {
  const h = harness(t); await h.client.adapter.loadBrainBinding();
  const facade = h.client.webSocketFactory(); await until(() => facade.readyState === 1);
  h.client.registerPopup(); const port = h.port(surface); await tick(); port.send('readiness');
  const p = h.peers[0]; await until(() => p.requests.some((r) => r.method === 'agent.analysis.readiness'));
  const count = port.values.length; port.close(); await tick();
  assert.equal(port.values.length, count, 'no post after disconnect');
  p.reply(p.requests.at(-1), readiness); await tick();
  assert.equal(port.values.length, count, 'no late post after disconnect');
  assert.equal(h.client.connected, true); assert.equal(facade.readyState, 1);
  assert.equal(h.client.diagnosticEvents.some((e) => e.event === 'channel-close'), false);
});

test('first caller abort after admission leaves the channel open', async (t) => {
  const h = harness(t), controller = new AbortController();
  await h.client.adapter.loadBrainBinding({ signal: controller.signal });
  controller.abort(); await tick();
  assert.equal(h.client.connected, true); assert.equal(h.channels[0].closed, false);
});

test('caller abort during handshake still admits the waiting facade', async (t) => {
  const gate = deferred(), h = harness(t, { gate }), controller = new AbortController();
  const rejected = assert.rejects(h.client.adapter.loadBrainBinding({ signal: controller.signal }));
  await until(() => h.peers.length === 1);
  const facade = h.client.webSocketFactory(); await tick();
  controller.abort(); await rejected; gate.resolve();
  await until(() => facade.readyState !== 0);
  assert.equal(facade.readyState, 1); assert.equal(h.client.connected, true); assert.equal(h.peers.length, 1);
});

test('confirmed pairing channel survives setup port closing', async (t) => {
  const h = harness(t, { paired: false }); h.client.registerPopup(); const port = h.port('setup');
  await tick(); port.send('pair'); await until(() => h.pairingWaiting);
  h.pairingResult.resolve(JSON.stringify(vector.result));
  await until(() => port.values.some((v) => v.state === 'paired'));
  port.close(); await tick(); assert.equal(h.client.connected, true); assert.equal(h.channels[0].closed, false);
});

test('setup disconnect still cancels its unconfirmed pairing wire', async (t) => {
  const h = harness(t, { paired: false }); h.client.registerPopup(); const port = h.port('setup');
  await tick(); port.send('pair'); await until(() => h.pairingWaiting);
  port.close(); await until(() => h.cancelled === 1); assert.equal(h.wireClosed, true); assert.equal(h.channels.length, 0);
});

for (const action of ['invalidate', 'forget', 'account', 'facade']) test(`${action} still closes the admitted channel`, async (t) => {
  const h = harness(t); await h.client.adapter.loadBrainBinding();
  if (action === 'account') { h.setAccount('changed'); await assert.rejects(h.client.adapter.loadBrainBinding()); }
  else if (action === 'facade') { const facade = h.client.webSocketFactory(); await until(() => facade.readyState === 1); facade.close(); }
  else await h.client[action]();
  assert.equal(h.channels[0].closed, true); assert.equal(h.client.connected, false);
});
