import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import initialize, { SnowSession } from '../vendor/companion-snow/ofca_snow_wasm.js';
import { openCompanionChannel, openLoopbackSocket, safeCompanionCloseReason } from '../transport/companion-channel.mjs';
import { createCompanionClient } from '../runtime/companion-client.mjs';
import { AgentWebSocketClient } from '../transport/agent-websocket.mjs';
import { ReadOnlyAgentWebSocketClient } from '../transport/read-only-agent-websocket.mjs';
import { createFragmentReceiver, fragmentMessage } from '../transport/companion-fragments.mjs';
import { key32 } from '../transport/pairing-contract.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { vector, trustSet, fixtureMaterial, fixturePrivateJwk } from '../test-fixtures/pairing/vendored-vector.mjs';

await initialize({ module_or_path: await readFile(new URL('../vendor/companion-snow/ofca_snow_wasm_bg.wasm', import.meta.url)) });
const trust = await loadGrantTrustSet(trustSet, { allowNonProduction: true });
const bytes = (hex) => new Uint8Array(Buffer.from(hex, 'hex'));
const encode = (document) => new TextEncoder().encode(JSON.stringify(document));
const record = (document) => { const text = encode(document); const value = new Uint8Array(text.length + 1); value[0] = 1; value.set(text, 1); return value; };
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('RPC capacity diagnostics distinguish admission contention without request material', async () => {
  const peer = fixture(), channel = await peer.open();
  try {
    const pending = Array.from({ length: 8 }, () => channel.rpc('private_method', { text: 'private_message' }));
    await assert.rejects(channel.rpc('agent.analysis.readiness', { token: 'private_token' }), error => {
      assert.deepEqual(error.diagnostic, { cause: 'rpc_capacity', pendingRpcs: 8, abandonedRpcs: 0, queuedSends: 8 });
      assert.doesNotMatch(JSON.stringify(error.diagnostic), /private_/);
      return true;
    });
    await Promise.all(pending);
    assert.equal(channel.closed, false);
  } finally { channel.close(); }
});

test('send capacity close diagnostics preserve the initiating cause and queue size', async () => {
  const peer = fixture(), channel = await peer.open();
  const pending = Array.from({ length: 9 }, () => channel.send({ protocol_version: '2', text: 'private_message' }));
  await Promise.allSettled(pending);
  assert.equal(channel.closed, true);
  assert.equal(channel.closeDiagnostic.cause, 'send_capacity');
  assert.equal(channel.closeDiagnostic.queuedSends, 8);
  assert.doesNotMatch(JSON.stringify(channel.closeDiagnostic), /private_/);
});

async function waitFor(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await tick();
  assert.ok(predicate(), 'the controlled asynchronous phase was entered');
}

function fixture({ wrongKey = false, authorization = vector.session_authorization, stall = false, sentFrame = () => {}, delayedCommit = null, delayedMaterial = null, reply = document => ({ secret: 'protected-ticket', method: document.method }) } = {}) {
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
              if (document.type === 'rpc.request') {
                const result = reply(document);
                if (result !== undefined) application({ type: 'rpc.response', id: document.id, result });
              }
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

test('credential rotation survives eight pending RPCs without resending material', async () => {
  const account = vector.detected_account_id, held = [], rotations = [];
  const challenge = { challenge_id: crypto.randomUUID(), challenge: Buffer.alloc(32, 9).toString('base64url'),
    session_id: crypto.randomUUID(), expires_at: '2026-09-30T12:00:00Z' };
  const peer = fixture({ reply(document) {
    if (document.method === 'agent.challenge') return challenge;
    if (document.method === 'agent.authenticate') return { creator_account_id: account,
      auth_ticket: 'fixture-ticket', storage_bootstrap: 'fixture-bootstrap' };
    if (document.method === 'agent.storage.unseal') return { schema: 'ofca-extension-storage-unlock/v1',
      creator_account_id: account, credential_kind: 'pairing', auth_ticket: 'fixture-ticket',
      storage_key_base64: Buffer.alloc(32, 7).toString('base64') };
    if (document.method === 'agent.storage.rotate') {
      rotations.push(document);
      return { schema: 'ofca-extension-storage-rotation/v1', storage_bootstrap: 'rotated-bootstrap' };
    }
    held.push(document);
  } });
  const channel = await peer.open();
  const privateKey = await crypto.subtle.importKey('jwk',
    fixturePrivateJwk(vector.fixture_labels.agent_identity_key, vector.request.agent_identity_jwk),
    { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const values = {};
  const area = { async get() { return { ...values }; }, async set(update) { Object.assign(values, update); } };
  const client = createCompanionClient({ chromeApi: { storage: { local: area, session: area } },
    allowsFull: () => true, detectedAccountId: async () => account, accountDatabaseName: async () => 'fixture-storage',
    storeFactory: async () => ({ identity: async () => ({ privateKey }) }), loadSnow: async () => ({ SnowSession }),
    loadTrust: async () => trust, channelFactory: async () => channel });
  const pending = [];
  try {
    await client.adapter.loadBrainBinding();
    for (let i = 0; i < 8; i++) pending.push(channel.rpc('agent.analysis.readiness'));
    await waitFor(() => held.length === 8);
    const rotation = client.adapter.saveReconnectAuthTicket({ creatorAccountId: account,
      agentInstallationId: await client.adapter.loadAgentInstallationId(), authTicket: 'reconnect-fixture',
      configAuthTicket: 'config-fixture' });
    void rotation.catch(() => undefined);
    await tick();
    assert.equal(rotations.length, 1);
    for (const document of held) {
      peer.application({ type: 'rpc.response', id: document.id, result: {} });
      await tick();
    }
    await Promise.all(pending);
    await rotation;
    assert.equal(rotations.length, 1);
    assert.equal(client.credentialRotation.completed, 1);
    assert.equal(channel.closed, false);
  } finally {
    client.invalidate(); channel.close();
    await Promise.allSettled(pending);
  }
});

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
  assert.equal(refused.wire.closeCode, 1008);
  const malformed = await open();
  malformed.socket.onclose({ code: 1008, reason: 'Pairing refused!' });
  assert.equal(malformed.wire.closeReason, 'other');
  const local = await open();
  local.wire.close();
  local.socket.onclose({ code: 4008, reason: 'companion_session_closed' });
  assert.equal(local.wire.closeReason, null);
  assert.equal(local.wire.closeCode, null);
});

test('raw socket never keeps unknown peer close text', async () => {
  let socket;
  const wire = await openLoopbackSocket('ws://127.0.0.1:17871/ws/agent/pairing', { text: true, webSocketFactory() {
    socket = { readyState: 1, bufferedAmount: 0, close() {} };
    queueMicrotask(() => socket.onopen());
    return socket;
  } });
  socket.onclose({ code: 1008, reason: 'private_note' });
  assert.equal(wire.closeReason, 'other');
  assert.equal(JSON.stringify(wire).includes('private_note'), false);
});

test('known service close reasons retain fixed diagnostic codes', () => {
  assert.equal(safeCompanionCloseReason('unsupported_version'), 'unsupported_version');
  assert.equal(safeCompanionCloseReason('Agent heartbeat lease expired'), 'heartbeat_lease_expired');
  assert.equal(safeCompanionCloseReason(safeCompanionCloseReason('Agent heartbeat lease expired')), 'heartbeat_lease_expired');
  assert.equal(safeCompanionCloseReason('private_note'), 'other');
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

test('encrypted session controls reach control listeners without a protocol observer', async () => {
  const peer = fixture(), channel = await peer.open();
  try {
    const controls = [];
    channel.onControl((value) => controls.push(value));
    const id = crypto.randomUUID();
    peer.application({ type: 'session.control', id, action: 'capture.pause' });
    await waitFor(() => controls.length === 1);
    assert.deepEqual(controls, [{ id, action: 'capture.pause' }]);
    assert.equal(channel.closed, false);
  } finally { channel.close(); }
});

test('a malformed or unknown session control closes the channel', async () => {
  for (const control of [
    { type: 'session.control', id: crypto.randomUUID(), action: 'consent.full' },
    { type: 'session.control', id: 'not-a-uuid', action: 'capture.pause' },
    { type: 'session.control', id: crypto.randomUUID(), action: 'capture.pause', extra: true },
  ]) {
    const peer = fixture(), channel = await peer.open();
    const controls = [];
    channel.onControl((value) => controls.push(value));
    peer.application(control);
    await waitFor(() => channel.closed);
    assert.deepEqual(controls, [], JSON.stringify(control));
  }
});

async function rotationTransport(t, Client, { localRefusal = null, rotationResult = 'success' } = {}) {
  let time = 0, refusal = localRefusal;
  t.mock.method(performance, 'now', () => time);
  const timers = new Map();
  let timerId = 0;
  t.mock.method(globalThis, 'setTimeout', (handler, delay) => {
    const id = ++timerId;
    timers.set(id, { handler, due: time + delay });
    return id;
  });
  t.mock.method(globalThis, 'clearTimeout', id => timers.delete(id));
  const peers = [], channels = [], pending = [], errors = [], values = {};
  const account = vector.detected_account_id;
  const area = { async get() { return { ...values }; }, async set(update) { Object.assign(values, update); } };
  const privateKey = await crypto.subtle.importKey('jwk',
    fixturePrivateJwk(vector.fixture_labels.agent_identity_key, vector.request.agent_identity_jwk),
    { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const client = createCompanionClient({ chromeApi: { storage: { local: area, session: area } },
    allowsFull: () => true, detectedAccountId: async () => account, accountDatabaseName: async () => 'fixture-storage',
    storeFactory: async () => ({ identity: async () => ({ privateKey }) }), loadSnow: async () => ({ SnowSession }),
    loadTrust: async () => trust, now: () => 1_800_000_000_000 + time, random: () => 0.5,
    channelFactory: async () => {
      const held = [], rotations = [];
      const peer = fixture({ reply(document) {
        if (document.method === 'agent.challenge') return { challenge_id: crypto.randomUUID(),
          challenge: Buffer.alloc(32, 9).toString('base64url'), session_id: crypto.randomUUID(), expires_at: '2026-09-30T12:00:00Z' };
        if (document.method === 'agent.authenticate') return { creator_account_id: account,
          auth_ticket: 'fixture-ticket', storage_bootstrap: 'fixture-bootstrap' };
        if (document.method === 'agent.storage.unseal') return { schema: 'ofca-extension-storage-unlock/v1',
          creator_account_id: account, credential_kind: 'pairing', auth_ticket: 'fixture-ticket',
          storage_key_base64: Buffer.alloc(32, 7).toString('base64') };
        if (document.method === 'agent.storage.rotate') {
          rotations.push(document);
          if (rotationResult === 'refused') {
            peer.application({ type: 'rpc.response', id: document.id, error: 'session_request_refused' });
            return;
          }
          if (rotationResult === 'ambiguous') return;
          return { schema: 'ofca-extension-storage-rotation/v1', storage_bootstrap: 'rotated-bootstrap' };
        }
        held.push({ document, at: time });
      } });
      peers.push(Object.assign(peer, { held, rotations }));
      const channel = await peer.open();
      channels.push(channel);
      return new Proxy({}, { get(_target, name) {
        if (name !== 'rpc') return Reflect.get(channel, name);
        return (method, params, controls) => {
          if (method === 'agent.storage.rotate' && refusal) return Promise.reject(Object.assign(
            new Error('companion_session_refused'), { code: 'companion_session_refused',
              diagnostic: { cause: refusal, pendingRpcs: 8, abandonedRpcs: 0, queuedSends: 0 } }));
          return channel.rpc(method, params, controls);
        };
      } });
    } });
  await client.adapter.loadBrainBinding();
  const installation = await client.adapter.loadAgentInstallationId();
  const session = JSON.parse(await readFile(new URL('../../shared/fixtures/protocol/v2/agent.session.json', import.meta.url)));
  Object.assign(session.payload, { creator_account_id: account, agent_installation_id: installation });
  const transport = new Client({ creatorAccountId: account, authTicket: 'fixture-ticket', extensionVersion: '2.0.3',
    identity: { agentInstallationId: installation, agentStreamId: session.payload.agent_stream_id,
      lastAcknowledgedSourceSeq: 10, appliedConfigRevision: 'config-8' },
    webSocketFactory: client.webSocketFactory, monotonicNow: () => time,
    persistReconnectAuthTicket: (authTicket, configAuthTicket, controls) => client.adapter.saveReconnectAuthTicket({
      creatorAccountId: account, agentInstallationId: installation, authTicket, configAuthTicket }, controls),
    onValidationError: error => errors.push(error) });
  const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
  const advance = async ms => {
    time += ms;
    for (const [id, timer] of [...timers]) if (timer.due <= time && timers.delete(id)) timer.handler();
    await settle();
  };
  const accept = async () => {
    await waitFor(() => peers.at(-1).received.some(document => document.type === 'agent.hello'));
    peers.at(-1).application(session);
    await settle();
  };
  t.after(async () => { transport.stop(); client.invalidate(); await Promise.allSettled(pending); });
  return { client, transport, peers, channels, errors, values, pending, accept, advance, settle,
    get time() { return time; }, release() { refusal = null; },
    traffic(kind) {
      for (let i = 0; i < 8; i++) {
        if (kind === 'surface') client.reportSurface({ schema: 'ofca-browser-surface/v1', capture: 'active',
          site_access: 'granted', history_permission: 'granted', legal_review_required: i % 2 === 0 });
        else { const call = client.analysisReadiness(); void call.catch(() => {}); pending.push(call); }
      }
    },
    async completeTraffic() {
      for (const { document, at } of peers.at(-1).held.splice(0)) {
        assert.ok(time - at <= 200, 'ordinary RPCs complete within 200 simulated milliseconds');
        peers.at(-1).application({ type: 'rpc.response', id: document.id, result:
          document.method === 'agent.analysis.readiness'
            ? { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' } : {} });
        await tick();
      }
    } };
}

for (const Client of [ReadOnlyAgentWebSocketClient, AgentWebSocketClient]) {
  for (const kind of ['readiness', 'surface']) {
    test(`${Client.name}: rolling ${kind} contention cannot starve rotation or disable reconnect`, async t => {
      const h = await rotationTransport(t, Client);
      h.transport.start();
      await waitFor(() => h.peers[0].received.some(document => document.type === 'agent.hello'));
      await h.settle();
      h.traffic(kind);
      await waitFor(() => h.peers[0].held.length === 8);
      await h.accept();
      for (let elapsed = 0; elapsed < 10_025 && !h.channels[0].closed; elapsed += 25) {
        await h.completeTraffic(); await h.settle();
        h.traffic(kind); await h.settle();
        await h.advance(25);
      }
      assert.equal(h.transport.reconnectAllowed, true, 'local-only deadline must preserve automatic reconnect');
      assert.ok(h.time >= 10_000, JSON.stringify({ time: h.time, close: h.channels[0].closeDiagnostic }));
      assert.equal(h.peers[0].rotations.length, 1, 'rotation has a reserved admission slot');
      assert.equal(h.client.credentialRotation.completed, 1);
      assert.equal(h.channels[0].closed, false);
      h.channels[0].close(); await h.settle();
      h.transport.ensureConnected(); await h.advance(0);
      await waitFor(() => h.channels.length === 2);
      await h.accept();
      assert.equal(h.client.credentialRotation.completed, 2);
    });
  }

  for (const cause of ['rpc_capacity', 'rpc_backlog']) {
    test(`${Client.name}: undispatched ${cause} rotation uses the six-attempt recovery circuit`, async t => {
      const h = await rotationTransport(t, Client, { localRefusal: cause });
      h.transport.start();
      for (let attempt = 1; attempt <= 6; attempt++) {
        await h.accept();
        if (cause === 'rpc_capacity') await h.advance(10_000);
        await h.settle();
        assert.equal(h.transport.reconnectAllowed, true);
        assert.equal(h.values.companion_recovery_v1.attempts, attempt, 'failed rotation cannot reset the circuit');
        assert.equal(h.peers.at(-1).rotations.length, 0);
        const delay = Math.max(0, h.values.companion_recovery_v1.next_attempt_at - (1_800_000_000_000 + h.time));
        for (let wake = 0; wake < 20; wake++) h.transport.ensureConnected();
        await h.settle();
        if (delay > 0) {
          await assert.rejects(h.client.adapter.loadBrainBinding(), { code: 'companion_recovery_backoff' });
          await h.advance(delay - 1);
          assert.equal(h.channels.length, attempt, 'wakes and timers respect the persisted deadline');
        }
        if (attempt === 6) {
          assert.equal(delay, cause === 'rpc_capacity' ? 290_000 : 300_000);
          h.release();
        }
        await h.advance(delay > 0 ? 1 : 0);
        await waitFor(() => h.channels.length === attempt + 1);
      }
      await h.accept();
      assert.equal(h.client.credentialRotation.completed, 1);
      assert.equal(h.values.companion_recovery_v1.attempts, 1);
    });
  }

  for (const invalidation of ['stop', 'binding', 'channel']) {
    test(`${Client.name}: pending rotation is fenced after ${invalidation}`, async t => {
      const h = await rotationTransport(t, Client, { localRefusal: 'rpc_capacity' });
      h.transport.start(); await h.accept();
      if (invalidation === 'stop') h.transport.stop();
      if (invalidation === 'binding') h.client.invalidate();
      if (invalidation === 'channel') h.channels[0].close();
      await h.settle();
      h.release(); await h.advance(25);
      assert.equal(h.peers[0].rotations.length, 0);
      assert.equal(h.client.credentialRotation.completed, 0);
      assert.equal(h.errors.length, 0);
    });
  }

  for (const rotationResult of ['refused', 'ambiguous']) {
    test(`${Client.name}: a dispatched ${rotationResult} rotation is never replayed`, async t => {
      const h = await rotationTransport(t, Client, { rotationResult });
      h.transport.start(); await h.accept();
      if (rotationResult === 'ambiguous') await h.advance(10_000);
      for (let wake = 0; wake < 20; wake++) h.transport.ensureConnected();
      await h.settle();
      assert.equal(h.peers[0].rotations.length, 1);
      assert.equal(h.client.credentialRotation.completed, 0);
      assert.ok(h.peers.every(peer => peer.rotations.length <= 1));
      assert.equal(h.client.credentialRotation.attempts, 1);
    });
  }
}

test('rotation reserves admission and send capacity while ordinary RPC limits stay unchanged', async () => {
  const peer = fixture({ reply: () => undefined }), channel = await peer.open(), pending = [];
  try {
    for (let i = 0; i < 8; i++) pending.push(channel.rpc('agent.analysis.readiness'));
    pending.push(channel.rpc('agent.storage.rotate'));
    for (const operation of pending) void operation.catch(() => {});
    await tick();
    assert.equal(channel.closed, false);
    assert.equal(peer.received.length, 9);
    for (const method of ['agent.analysis.readiness', 'agent.storage.rotate']) {
      await assert.rejects(channel.rpc(method), error => error.code === 'companion_session_refused'
        && error.diagnostic.cause === 'rpc_capacity');
    }
    for (const document of peer.received) {
      peer.application({ type: 'rpc.response', id: document.id, result: {} });
      await tick();
    }
    await Promise.all(pending);
  } finally { channel.close(); await Promise.allSettled(pending); }
});

test('ordinary abandoned RPCs cannot consume the rotation reservation', async () => {
  const peer = fixture({ reply: document => document.method === 'agent.storage.rotate' ? {} : undefined });
  const channel = await peer.open(), pending = [];
  try {
    for (let batch = 0; batch < 8; batch++) {
      const controller = new AbortController();
      for (let i = 0; i < 8; i++) {
        const operation = channel.rpc('agent.analysis.readiness', {}, { signal: controller.signal });
        void operation.catch(() => {}); pending.push(operation);
      }
      await tick(); controller.abort(); await tick();
    }
    await assert.rejects(channel.rpc('agent.analysis.readiness'), error => error.diagnostic.cause === 'rpc_backlog');
    await channel.rpc('agent.storage.rotate');
    assert.equal(peer.received.filter(document => document.method === 'agent.storage.rotate').length, 1);
    assert.equal(channel.closed, false);
  } finally { channel.close(); await Promise.allSettled(pending); }
});

test('an abandoned rotation keeps its reservation until its response is discarded', async () => {
  const peer = fixture({ reply: () => undefined }), channel = await peer.open();
  const controller = new AbortController();
  try {
    const rotation = channel.rpc('agent.storage.rotate', {}, { signal: controller.signal });
    void rotation.catch(() => {}); await tick();
    controller.abort(); await assert.rejects(rotation);
    await assert.rejects(channel.rpc('agent.storage.rotate'), error => error.diagnostic.cause === 'rpc_capacity');
    assert.equal(peer.received.length, 1);
    peer.application({ type: 'rpc.response', id: peer.received[0].id, result: {} }); await tick();
    const next = channel.rpc('agent.storage.rotate');
    void next.catch(() => {}); await tick();
    peer.application({ type: 'rpc.response', id: peer.received[1].id, result: {} });
    await next;
  } finally { channel.close(); }
});
