import assert from 'node:assert/strict';
import test from 'node:test';
import { BrowserTaskClient } from '../../frontend/browser-tasks/client.js';
import { connectExtension } from '../../frontend/browser-tasks/extension.js';
import { createCompanionClient } from '../runtime/companion-client.mjs';
import { openPairingStore } from '../runtime/companion-pairing-store.mjs';
import { DESKTOP_PORT_NAME, registerDesktopPort } from '../runtime/desktop-port.mjs';
import { openCompanionChannel } from '../transport/companion-channel.mjs';
import { createFragmentReceiver, fragmentMessage } from '../transport/companion-fragments.mjs';
import { b64u, grantDigest, normalizeSignature, pairingDigest, proofMessage, transcriptOf, unb64u } from '../transport/pairing-contract.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { fixtureMaterial, fixturePrivateJwk, vector, trustSet } from '../test-fixtures/pairing/vendored-vector.mjs';
import { payload, observation } from '../test-fixtures/browser-task.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const event = () => { const listeners = new Set(); return {
  addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
  emit: value => { for (const fn of listeners) fn(value); },
}; };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate) {
  const deadline = performance.now() + 5000;
  while (!await predicate() && performance.now() < deadline) await tick();
  assert(await predicate(), 'controlled pairing phase reached');
}
const area = () => { const values = {}; return {
  async get() { return { ...values }; }, async set(update) { Object.assign(values, update); }, async remove() {},
}; };
const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
const installationKey = await crypto.subtle.importKey('jwk',
  fixturePrivateJwk(vector.fixture_labels.installation_key, vector.offer.installation_jwk),
  { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);

// This suite exercises the real store, grant/proof validation, session state
// machine, client, port and helper. Only the Noise primitive and wire peer are
// controlled; the independent cryptographic suites qualify that boundary.
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

async function harness(t) {
  t.mock.method(Date, 'now', () => vector.now * 1000);
  const stored = await openPairingStore({ indexedDB: new FakeIndexedDb(), name: 'pairing-completion', clock: () => vector.now });
  const commitEntered = deferred(), releaseCommit = deferred(), cancelled = deferred(), result = deferred();
  const calls = { commits: 0, cancels: 0, paired: 0, ownerReleased: 0, finish: 0 }, receipts = [], presentations = [];
  const store = { ...stored,
    async commit(...args) { commitEntered.resolve(); await releaseCommit.promise; await stored.commit(...args); calls.commits++; },
    async cancel() { await stored.cancel(); calls.cancels++; cancelled.resolve(); },
  };
  const value = payload(), observed = observation(value.task);
  value.task.purpose = 'browser.pairing-confirmation';
  value.creator_account_id = observed.creator_account_id = vector.detected_account_id;
  observed.state = 'ready';
  const origin = 'http://bridge.localhost:17871', url = origin + '/browser-tasks#presentation=' + value.task.admission_generation;
  const chromeApi = { runtime: { id: value.extension_id, onConnectExternal: event(), getURL: path => 'chrome-extension://' + value.extension_id + '/' + path },
    scripting: { async executeScript(options) {
      assert.deepEqual(options.target, { tabId: 12, documentIds: ['document-1'] });
      assert.deepEqual(options.args, [value.task.admission_generation, value.task.expires_at]);
      calls.finish++; return [{ frameId: 0, documentId: 'document-1', result: true }];
    } },
    storage: { local: area(), session: area() }, tabs: { async get() { return { url, windowId: 4 }; } } };
  let wireClosed = false, brainCompleted = false, comparisonPosted = false, portClosed = false, socket;
  const companion = createCompanionClient({ chromeApi, allowsFull: () => true, detectedAccountId: async () => vector.detected_account_id,
    accountDatabaseName: async () => 'synthetic-account', storeFactory: async () => store, loadTrust: async () => trust,
    loadSnow: async () => ({ SnowSession: FakeSnow, generateStaticKeypair: () => new Uint8Array([
      ...fixtureMaterial(vector.fixture_labels.agent_noise_key), ...unb64u(vector.request.agent_noise_key),
    ]) }),
    wireFactory: async (_url, { signal }) => {
      let request, receives = 0;
      signal.addEventListener('abort', () => { wireClosed = true; result.resolve('cancelled'); }, { once: true });
      return { close() { wireClosed = true; }, async send(frame) {
        const message = JSON.parse(frame); if (message.type === 'pair.request') request = message;
      }, async receive() {
        if (++receives > 1) return result.promise;
        const offer = structuredClone(vector.offer);
        const digest = await pairingDigest(await transcriptOf(request, offer, vector.expected.identity,
          await grantDigest(offer.creator_account_binding, offer.installation_grant)));
        offer.brain_proof = b64u(normalizeSignature(new Uint8Array(await crypto.subtle.sign(
          { name: 'ECDSA', hash: 'SHA-256' }, installationKey, proofMessage('brain', digest)))));
        return JSON.stringify(offer);
      } };
    },
    channelFactory: options => {
      let phase = 0;
      const fragments = createFragmentReceiver();
      const emit = bytes => queueMicrotask(() => { if (socket.readyState === 1) socket.onmessage({ data: bytes.slice().buffer }); });
      const reply = (request, response = {}) => {
        for (const frame of fragmentMessage({ type: 'rpc.response', id: request.id, result: response })) emit(new Uint8Array([1, ...frame]));
      };
      return openCompanionChannel({ ...options, clock: () => vector.now, webSocketFactory() {
        socket = { readyState: 1, bufferedAmount: 0, close() { this.readyState = 3; }, send(bytes) {
          if (phase++ === 0) { emit(new Uint8Array([1])); return; }
          if (phase === 2) {
            emit(new Uint8Array([0, ...new TextEncoder().encode('server-ready')]));
            emit(new Uint8Array([1, ...new TextEncoder().encode(JSON.stringify(vector.session_authorization))]));
            return;
          }
          const request = fragments.receive(bytes.slice(1));
          if (!request) return;
          if (request.method === 'agent.challenge') reply(request, { challenge_id: 'test', challenge: b64u(new Uint8Array(32).fill(9)),
            session_id: 'test', expires_at: '2026-09-12T12:00:00Z' });
          else if (request.method === 'agent.authenticate') reply(request, { creator_account_id: vector.detected_account_id,
            auth_ticket: 'test-ticket', storage_bootstrap: 'test-bootstrap' });
          else if (request.method === 'agent.storage.unseal') reply(request, { schema: 'ofca-extension-storage-unlock/v1',
            creator_account_id: vector.detected_account_id, credential_kind: 'pairing', auth_ticket: 'test-ticket',
            storage_key_base64: Buffer.alloc(32, 7).toString('base64') });
          else reply(request);
        } };
        queueMicrotask(() => socket.onopen()); return socket;
      } });
    },
  });
  const owner = { async attach() { return {}; }, async read() { return { ...observed }; }, async release() { calls.ownerReleased++; } };
  const ports = registerDesktopPort({ chromeApi, owner, companion, onPaired: async () => { calls.paired++; } });
  ports.register();
  let extensionPort, helperPort;
  const runtime = { connect() {
    const disconnect = () => { if (portClosed) return; portClosed = true; extensionPort.onDisconnect.emit(); helperPort.onDisconnect.emit(); };
    extensionPort = { name: DESKTOP_PORT_NAME, sender: { origin, url, documentId: 'document-1', frameId: 0, tab: { id: 12, windowId: 4 } },
      onMessage: event(), onDisconnect: event(), disconnect, postMessage(message) {
        receipts.push(message); helperPort.onMessage.emit(message);
      } };
    helperPort = { onMessage: event(), onDisconnect: event(), disconnect,
      postMessage: message => extensionPort.onMessage.emit(message) };
    chromeApi.runtime.onConnectExternal.emit(extensionPort); return helperPort;
  } };
  const helper = new BrowserTaskClient({ generation: value.task.admission_generation, receive: value => presentations.push(value),
    connect: options => connectExtension({ ...options, runtime }), request: async path => {
      let response;
      if (path.endsWith('/session')) response = { role: 'browser_task', generation: value.task.admission_generation,
        csrf_token: 'A'.repeat(43), task_id: value.task.task_id, journey_id: value.task.journey_id };
      else if (path.endsWith('/payload')) response = { ...value, task: { ...value.task, state: brainCompleted ? 'completed' : 'accepted' } };
      else if (path.endsWith('/pairing')) { comparisonPosted = true; response = {}; }
      else throw Error('unexpected helper request');
      return { ok: true, text: async () => JSON.stringify(response) };
    } });
  t.after(async () => { helper.stop(); ports.unregister(); companion.invalidate(); result.resolve('cancelled'); releaseCommit.resolve(); await tick(); stored.close(); });
  await helper.start();
  await until(() => comparisonPosted);
  const completed = () => receipts.filter(item => item.type === 'pairing-completed');
  return { helper, companion, stored, calls, receipts, presentations, commitEntered, releaseCommit, cancelled, completed,
    result: () => result.resolve(JSON.stringify(vector.result)), get portClosed() { return portClosed; }, get wireClosed() { return wireClosed; },
    async completeBrain() { brainCompleted = true; return helper.finishCompleted(await helper.currentPayload()); } };
}

test('closing the helper while the authenticated pin commit waits cancels real pending state', async t => {
  const h = await harness(t); h.result(); await h.commitEntered.promise;
  assert.equal((await h.stored.status()).pending, true);
  assert.equal((await h.stored.status()).paired, false);
  h.helper.stop(); await h.cancelled.promise; h.releaseCommit.resolve(); await tick();
  assert.equal((await h.stored.status()).pending, false);
  assert.equal(await h.stored.savedPairing(), null);
  assert.equal(h.calls.commits, 0); assert.equal(h.calls.paired, 0);
  assert.deepEqual(h.completed(), []);
});

test('Brain completion keeps this helper alive until the extension commits and reports this pairing', async t => {
  const h = await harness(t); h.result(); await h.commitEntered.promise;
  await h.completeBrain();
  assert.equal(h.portClosed, false, 'Brain completion precedes the extension pin commit');
  assert.equal((await h.stored.status()).pending, true); assert.deepEqual(h.completed(), []);
  h.releaseCommit.resolve(); await until(() => h.portClosed);
  assert.equal((await h.stored.status()).paired, true); assert.equal((await h.stored.status()).pending, false);
  assert.equal(h.calls.commits, 1); assert.equal(h.calls.paired, 1); assert.equal(h.calls.cancels, 0);
  assert.equal(h.completed().length, 1); assert.equal(h.companion.connected, true);
  assert.equal(h.calls.finish, 1);
  assert.equal(h.presentations.at(-1).state, 'task_completed');
});

test('the exact extension receipt alone does not complete Brain work or close the live helper', async t => {
  const h = await harness(t); h.result(); await h.commitEntered.promise; h.releaseCommit.resolve();
  await until(() => h.completed().length === 1);
  assert.equal(h.portClosed, false); assert.equal(h.calls.paired, 1);
  assert.equal(h.presentations.some(value => value.state === 'task_completed'), false);
  await h.completeBrain();
  await until(() => h.portClosed);
  assert.equal(h.portClosed, true); assert.equal(h.calls.cancels, 0);
  assert.equal(h.calls.finish, 1);
  assert.equal((await h.stored.status()).paired, true); assert.equal(h.companion.connected, true);
});

test('disconnect before Brain confirmation still cancels its exact pending pairing', async t => {
  const h = await harness(t);
  h.helper.stop(); await h.cancelled.promise;
  assert.equal(h.wireClosed, true); assert.equal((await h.stored.status()).pending, false);
  assert.equal(await h.stored.savedPairing(), null); assert.equal(h.calls.paired, 0);
  assert.deepEqual(h.completed(), []);
});
