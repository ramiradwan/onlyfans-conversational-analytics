import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { probeBrowserState, readObservedBrainState } from './brain-probe-observation.mjs';

const PRIVATE = 'PRIVATE-ticket-url-payload-close-text';
const parameters = { authTicket: PRIVATE, creatorAccountId: 'synthetic-account', timeout: 1000, catchup: true };
const snapshot = { analytics: { value: PRIVATE } }, agent = { status: 'connected' };

function harness({ constructFails = false, sendFails = false, closeFails = false } = {}) {
  let nextTimer = 0;
  const timers = new Map(), scheduled = [], sockets = [];
  class Socket {
    constructor(url) {
      if (constructFails) throw new Error(PRIVATE);
      this.url = url; this.listeners = new Map(); this.sent = []; this.closed = [];
      sockets.push(this);
    }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    removeEventListener(name, listener) { if (this.listeners.get(name) === listener) this.listeners.delete(name); }
    emit(name, value = {}) { this.listeners.get(name)?.(value); }
    message(type, payload) { this.emit('message', { data: JSON.stringify({ type, payload }) }); }
    send(value) { if (sendFails) throw new Error(PRIVATE); this.sent.push(JSON.parse(value)); }
    close(...args) { this.closed.push(args); if (closeFails) throw new Error(PRIVATE); }
  }
  const globals = { WebSocket: Socket, crypto: { randomUUID: () => 'synthetic-random-uuid' },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, callback); scheduled.push(delay); return id; },
    clearTimeout(id) { timers.delete(id); } };
  const page = { evaluate: async (fn, args) => runInNewContext(`(${fn.toString()})(parameters)`, { ...globals, parameters: args }) };
  const read = args => readObservedBrainState(page, { ...parameters, ...args });
  const expire = () => { for (const [id, callback] of [...timers]) { timers.delete(id); callback(); } };
  return { read, page, sockets, timers, scheduled, expire };
}

async function failure(pending) {
  const error = await pending.then(() => assert.fail('Failure became success'), error => error);
  assert(error.message.startsWith('Brain probe failed: '));
  assert(!error.message.includes(PRIVATE));
  assert(error.message.length < 600);
  const observation = JSON.parse(error.message.slice('Brain probe failed: '.length));
  assert.equal(observation.transport_detail, 'unavailable');
  return observation;
}

function cleaned(h) {
  assert.equal(h.timers.size, 0);
  for (const socket of h.sockets) {
    assert.equal(socket.listeners.size, 0);
    assert.deepEqual(socket.closed, [[1000, 'E2E probe complete']]);
  }
}

for (const catchup of [false, true]) {
  test(`serialized probe preserves hello bindings and requires both state frames (catchup=${catchup})`, async () => {
    const h = harness({ closeFails: true });
    const pending = h.read({ catchup });
    const socket = h.sockets[0];
    assert.equal(socket.url, 'ws://bridge.localhost:17871/ws/bridge');
    socket.emit('open');
    assert.deepEqual(socket.sent[0], { type: 'bridge.hello', protocol_version: '2', message_id: 'synthetic-random-uuid',
      payload: { auth_ticket: PRIVATE, bridge_session_id: 'synthetic-random-uuid', requested_creator_account_id: 'synthetic-account',
        capabilities: ['state.snapshot', 'state.delta', 'presence.state', 'message.page', ...(catchup ? ['state.catchup_freshness'] : [])],
        client_version: 'e2e-capture-2', last_view_revision: null } });
    socket.message('state.snapshot', snapshot);
    assert.equal(socket.closed.length, 0);
    socket.message('agent.state', agent);
    assert.deepEqual(JSON.parse(JSON.stringify(await pending)), { snapshot, agent });
    assert.deepEqual(h.scheduled, [1000]); cleaned(h);
  });
}

for (const [events, phase] of [
  [[], 'connecting'], [['open'], 'awaiting_snapshot_and_agent'],
  [['open', 'state.snapshot'], 'awaiting_agent'], [['open', 'agent.state'], 'awaiting_snapshot'],
]) {
  test(`the single original deadline reports ${phase}`, async () => {
    const h = harness(), pending = h.read(), socket = h.sockets[0];
    for (const event of events) event === 'open' ? socket.emit(event) : socket.message(event, {});
    h.expire();
    const observation = await failure(pending);
    assert.equal(observation.code, 'deadline'); assert.equal(observation.phase, phase);
    assert.equal(observation.close_code, null);
    assert.deepEqual(h.scheduled, [1000]); cleaned(h);
  });
}

test('transport error retains the subsequent normal close without an additional timer', async () => {
  const h = harness(), pending = h.read(), socket = h.sockets[0];
  socket.emit('open'); socket.emit('error', { message: PRIVATE });
  assert.equal(socket.closed.length, 0);
  socket.message('state.snapshot', snapshot); socket.message('agent.state', agent);
  assert.equal(socket.closed.length, 0, 'State cannot turn an observed error into success');
  socket.emit('close', { code: 1006, wasClean: false, reason: PRIVATE });
  const observation = await failure(pending);
  assert.equal(observation.code, 'socket_error'); assert.equal(observation.phase, 'state_received');
  assert.equal(observation.transport_error, true); assert.equal(observation.close_code, 1006);
  assert.equal(observation.close_clean, false);
  assert.deepEqual(h.scheduled, [1000]); cleaned(h);
});

test('missing close after transport error ends only at the original deadline', async () => {
  const h = harness(), pending = h.read(), socket = h.sockets[0];
  socket.emit('error', { message: PRIVATE }); h.expire();
  const observation = await failure(pending);
  assert.equal(observation.code, 'socket_error'); assert.equal(observation.opened, false);
  assert.equal(observation.close_code, null); assert.equal(observation.close_clean, null);
  assert.deepEqual(h.scheduled, [1000]); cleaned(h);
});

test('later malformed or protocol frames cannot discard the first transport failure or its close', async () => {
  const h = harness(), pending = h.read(), socket = h.sockets[0];
  socket.emit('error'); socket.emit('message', { data: PRIVATE });
  socket.message('protocol.error', { code: 'unauthorized', detail: PRIVATE });
  assert.equal(socket.closed.length, 0);
  socket.emit('close', { code: 1008, wasClean: true, reason: PRIVATE });
  const observation = await failure(pending);
  assert.equal(observation.code, 'socket_error'); assert.equal(observation.close_code, 1008);
  assert.equal(observation.protocol_code, 'unauthorized');
  assert.deepEqual(h.scheduled, [1000]); cleaned(h);
});

test('close values are bounded and arbitrary close reasons are excluded', async () => {
  for (const [code, clean, expected] of [[1008, true, 1008], [5000, PRIVATE, null], [Infinity, null, null], [PRIVATE, {}, null]]) {
    const h = harness(), pending = h.read();
    h.sockets[0].emit('close', { code, wasClean: clean, reason: PRIVATE });
    const observation = await failure(pending);
    assert.equal(observation.code, 'socket_closed'); assert.equal(observation.close_code, expected);
    assert.equal(observation.close_clean, typeof clean === 'boolean' ? clean : null); cleaned(h);
  }
});

test('protocol and malformed input failures expose only closed classifications', async () => {
  for (const [data, code, protocol] of [
    [PRIVATE, 'malformed_json', null], ['null', 'malformed_json', null],
    [JSON.stringify({ type: 'protocol.error', payload: { code: 'unauthorized', detail: PRIVATE } }), 'protocol_error', 'unauthorized'],
    [JSON.stringify({ type: 'protocol.error', payload: { code: PRIVATE } }), 'protocol_error', 'unknown'],
  ]) {
    const h = harness(), pending = h.read();
    h.sockets[0].emit('open'); h.sockets[0].emit('message', { data });
    const observation = await failure(pending);
    assert.equal(observation.code, code); assert.equal(observation.protocol_code, protocol); cleaned(h);
  }
});

test('construction and send exceptions never expose raw errors', async () => {
  const constructed = harness({ constructFails: true });
  assert.equal((await failure(constructed.read())).code, 'construction_failed');
  assert.equal(constructed.timers.size, 0);
  const sent = harness({ sendFails: true }), pending = sent.read();
  sent.sockets[0].emit('open');
  const observation = await failure(pending);
  assert.equal(observation.code, 'send_failed'); assert.equal(observation.phase, 'hello_not_sent'); cleaned(sent);
});

test('overlapping probes cannot borrow another socket state or close observation', async () => {
  const h = harness(), first = h.read(), second = h.read();
  h.sockets[0].emit('error');
  h.sockets[1].emit('open'); h.sockets[1].message('state.snapshot', snapshot); h.sockets[1].message('agent.state', agent);
  assert.deepEqual(JSON.parse(JSON.stringify(await second)), { snapshot, agent });
  h.expire();
  const observation = await failure(first);
  assert.equal(observation.opened, false); assert.equal(observation.snapshot_seen, false);
  assert.equal(observation.close_code, null); cleaned(h);
});

test('page evaluation and malformed observation failures are explicitly unavailable', async () => {
  for (const page of [
    { evaluate: async () => { throw new Error(PRIVATE); } },
    { evaluate: async () => ({ ok: false, observation: { code: PRIVATE } }) },
    { evaluate: async () => ({ ok: false, observation: { code: 'deadline', private: PRIVATE } }) },
  ]) {
    const observation = await failure(readObservedBrainState(page, parameters));
    assert.equal(observation.code, 'evaluation_unavailable'); assert.equal(observation.phase, 'unavailable');
    assert.equal(observation.opened, null); assert.equal(observation.transport_error, null);
  }
  assert(probeBrowserState.toString().includes('new WebSocket'));
});
