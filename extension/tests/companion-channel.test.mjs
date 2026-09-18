import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import initialize, { SnowSession } from '../vendor/companion-snow/ofca_snow_wasm.js';
import { openCompanionChannel, openLoopbackSocket } from '../transport/companion-channel.mjs';
import { createFragmentReceiver, fragmentMessage } from '../transport/companion-fragments.mjs';
import { key32 } from '../transport/pairing-contract.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { vector, trustSet, fixtureMaterial } from '../test-fixtures/pairing/vendored-vector.mjs';

await initialize({ module_or_path: await readFile(new URL('../vendor/companion-snow/ofca_snow_wasm_bg.wasm', import.meta.url)) });
const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
const bytes = (hex) => new Uint8Array(Buffer.from(hex, 'hex'));
const encode = (document) => new TextEncoder().encode(JSON.stringify(document));
const record = (document) => { const text = encode(document); const value = new Uint8Array(text.length + 1); value[0] = 1; value.set(text, 1); return value; };
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function waitFor(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await tick();
  assert.ok(predicate(), 'the controlled asynchronous phase was entered');
}

function fixture({ wrongKey = false, authorization = vector.session_authorization, stall = false, sentFrame = () => {}, delayedCommit = null, delayedMaterial = null } = {}) {
  const captured = [], received = [], fragments = createFragmentReceiver();
  let socket, responder, phase = 0, invalidated, commitSignal, materialSignal, pinned = false;
  const store = {
    onInvalidate(listener) { invalidated = listener; return () => {}; },
    async sessionMaterial({ signal }) { materialSignal = signal; if (delayedMaterial) await delayedMaterial;
      return {
      privateKey: fixtureMaterial(vector.fixture_labels.agent_noise_key), peerKey: key32(vector.offer.brain_noise_key),
      pairingId: vector.offer.pairing_id, pairingDigest: bytes(vector.expected.pairing_digest), identity: vector.expected.identity, commit: delayedCommit ? {} : null,
    }; },
    async commit(_token, signal, controls) { commitSignal = signal; await delayedCommit; controls.assertCurrent(); signal.throwIfAborted(); pinned = true; },
  };
  function emit(value) {
    const copy = value.slice(); queueMicrotask(() => { if (socket.readyState === 1) socket.onmessage?.({ data: copy.buffer }); });
  }
  function fragment(frame) { const plain = new Uint8Array(frame.length + 1); plain[0] = 1; plain.set(frame, 1); emit(responder.encrypt_transport(plain)); }
  function application(document) { for (const frame of fragmentMessage(document)) fragment(frame); }
  function factory() {
    responder = new SnowSession(false, fixtureMaterial(wrongKey ? 'companion-test-wrong-brain' : vector.fixture_labels.brain_noise_key), key32(vector.request.agent_noise_key), bytes(vector.expected.session_prologue));
    socket = { readyState: 0, bufferedAmount: 0, close() { if (this.readyState === 3) return; this.readyState = 3; responder.close(); responder.free(); queueMicrotask(() => this.onclose?.()); },
      send(value) {
        captured.push(new Uint8Array(value));
        if (stall) return;
        try {
          if (phase === 0) {
            assert.deepEqual(new Uint8Array(value).slice(0, 32), key32(vector.offer.pairing_id));
            responder.read_handshake(value.slice(32)); emit(responder.write_handshake()); responder.enter_transport(); phase++;
          } else if (phase === 1) {
            assert.deepEqual(responder.decrypt_transport(value), new Uint8Array([0, ...new TextEncoder().encode('client-ready')]));
            emit(responder.encrypt_transport(new Uint8Array([0, ...new TextEncoder().encode('server-ready')])));
            emit(responder.encrypt_transport(record(authorization))); phase++;
          } else {
            sentFrame();
            const plain = responder.decrypt_transport(value); assert.equal(plain[0], 1);
            const document = fragments.receive(plain.slice(1));
            if (document) {
              received.push(document);
              if (document.type === 'rpc.request') application({ type: 'rpc.response', id: document.id, result: { secret: 'protected-ticket', method: document.method } });
            }
          }
        } catch { socket.close(); }
      },
    };
    queueMicrotask(() => { socket.readyState = 1; socket.onopen?.(); });
    return socket;
  }
  return { captured, received, application, fragment, get socket() { return socket; }, get commitSignal() { return commitSignal; },
    get materialSignal() { return materialSignal; }, get pinned() { return pinned; }, invalidate: () => invalidated(),
    open: (extra = {}) => openCompanionChannel({ url: 'ws://127.0.0.1:17871/ws/agent', store, SnowSession, accountId: vector.detected_account_id, trust, clock: () => vector.now, webSocketFactory: factory, ...extra }) };
}

test('packaged Snow authenticates the pinned peer before encrypted RPC and protocol traffic', async () => {
  const peer = fixture(), channel = await peer.open();
  try {
    assert.equal(peer.received.length, 0);
    assert.equal(channel.identity.creator_account_id, vector.detected_account_id);
    assert.deepEqual(await channel.rpc('agent.challenge'), { secret: 'protected-ticket', method: 'agent.challenge' });
    const document = { protocol_version: '2', message_type: 'snapshot.chunk', value: 1.5, body: 'private-message-content'.repeat(4000) };
    await channel.send(document);
    assert.deepEqual(peer.received.at(-1), document);
    const inbound = new Promise((resolve) => channel.onMessage(resolve));
    peer.application({ protocol_version: '2', message_type: 'heartbeat', secret: 'protected-config-ticket' });
    assert.equal((await inbound).secret, 'protected-config-ticket');
    assert.equal(Buffer.concat(peer.captured).includes(Buffer.from('private-message-content')), false);
    assert.equal(Buffer.concat(peer.captured).includes(Buffer.from('agent.challenge')), false);
    peer.invalidate(); assert.equal(channel.closed, true);
  } finally { channel.close(); }
});

test('a hostile process with another Noise key receives no application data', async () => {
  const peer = fixture({ wrongKey: true });
  await assert.rejects(peer.open(), { message: 'companion_session_refused' });
  assert.equal(peer.captured.length, 1); assert.equal(peer.received.length, 0);
});

test('grant refusal closes the real Noise channel before application release', async () => {
  const peer = fixture({ authorization: { ...vector.session_authorization, creator_account_binding: 'invalid' } });
  await assert.rejects(peer.open(), { message: 'companion_session_refused' });
  assert.equal(peer.received.length, 0); assert.equal(peer.captured.length, 2);
});

test('abort closes a late or stalled handshake without releasing Full data', async () => {
  const controller = new AbortController(), peer = fixture({ stall: true });
  const operation = peer.open({ signal: controller.signal });
  await tick(); controller.abort();
  await assert.rejects(operation, { message: 'companion_session_refused' });
  assert.equal(peer.received.length, 0); assert.equal(peer.socket.readyState, 3);
});

test('raw socket rejects text, oversize frames and queue overflow with fixed errors', async () => {
  for (const bad of ['credential', new ArrayBuffer(36865), 'overflow']) {
    let socket;
    const wire = await openLoopbackSocket('ws://127.0.0.1:17871/ws/agent', { webSocketFactory() {
      socket = { readyState: 1, bufferedAmount: 0, close() {} }; queueMicrotask(() => socket.onopen()); return socket;
    } });
    if (bad === 'overflow') for (let i = 0; i < 5; i++) socket.onmessage({ data: new ArrayBuffer(1) });
    else socket.onmessage({ data: bad });
    assert.equal(wire.closed, true);
    await assert.rejects(wire.receive(), { message: 'companion_session_refused' });
  }
});

test('raw socket keeps a well-formed peer close reason and ignores malformed ones', async () => {
  const open = async () => {
    let socket;
    const wire = await openLoopbackSocket('ws://127.0.0.1:17871/ws/agent/pairing', { text: true, webSocketFactory() {
      socket = { readyState: 1, bufferedAmount: 0, close() {} }; queueMicrotask(() => socket.onopen()); return socket;
    } });
    return { wire, socket };
  };
  const refused = await open();
  refused.socket.onclose({ code: 1008, reason: 'pairing_state_refused' });
  assert.equal(refused.wire.closed, true);
  assert.equal(refused.wire.closeReason, 'pairing_state_refused');
  const malformed = await open();
  malformed.socket.onclose({ code: 1008, reason: 'Pairing refused!' });
  assert.equal(malformed.wire.closeReason, null);
  const local = await open();
  local.wire.close();
  local.socket.onclose({ code: 4008, reason: 'companion_session_closed' });
  assert.equal(local.wire.closeReason, null);
});

test('a silent peer holding a partial encrypted document is closed at its assembly deadline', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const peer = fixture(), channel = await peer.open();
  try {
    peer.fragment(fragmentMessage({ protocol_version: '2', text: 'x'.repeat(9000) })[0]);
    await tick(); t.mock.timers.tick(10000); await tick();
    assert.equal(channel.closed, true);
  } finally { channel.close(); }
});

test('the whole outgoing document has one deadline across all encrypted fragments', async (t) => {
  let elapsed = performance.now();
  t.mock.method(performance, 'now', () => elapsed);
  const peer = fixture({ sentFrame() { elapsed += 2000; } }), channel = await peer.open();
  await assert.rejects(channel.send({ protocol_version: '2', text: 'x'.repeat(40000) }), { message: 'companion_session_refused' });
  assert.equal(channel.closed, true); assert.equal(peer.received.length, 0);
  assert.ok(peer.captured.length < 10);
});

test('a phase deadline aborts an entered pin commit before delayed completion can persist it', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let release;
  const peer = fixture({ delayedCommit: new Promise((resolve) => { release = resolve; }) });
  const operation = peer.open(); const rejected = assert.rejects(operation, { message: 'companion_session_refused' });
  await waitFor(() => peer.commitSignal);
  assert.ok(peer.commitSignal); assert.equal(peer.commitSignal.aborted, false);
  t.mock.timers.tick(2000); assert.equal(peer.commitSignal.aborted, true);
  release(); await rejected;
  assert.equal(peer.pinned, false); assert.equal(peer.socket.readyState, 3);
});

test('the phase deadline also aborts material loading before any handshake is sent', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let release;
  const peer = fixture({ delayedMaterial: new Promise((resolve) => { release = resolve; }) });
  const operation = peer.open(); const rejected = assert.rejects(operation, { message: 'companion_session_refused' });
  await tick(); assert.equal(peer.materialSignal.aborted, false);
  t.mock.timers.tick(2000); assert.equal(peer.materialSignal.aborted, true);
  release(); await rejected; assert.equal(peer.captured.length, 0);
});

test('the commit callback checks monotonic expiry even before a delayed timer fires', async (t) => {
  let elapsed = performance.now(), release;
  t.mock.method(performance, 'now', () => elapsed);
  const peer = fixture({ delayedCommit: new Promise((resolve) => { release = resolve; }) });
  const operation = peer.open(); const rejected = assert.rejects(operation, { message: 'companion_session_refused' });
  await waitFor(() => peer.commitSignal);
  assert.ok(peer.commitSignal);
  elapsed += 2001; release(); await rejected;
  assert.equal(peer.pinned, false); assert.equal(peer.commitSignal.aborted, true);
});
