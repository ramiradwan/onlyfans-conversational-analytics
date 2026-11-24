import assert from 'node:assert/strict';
import test from 'node:test';

import { fullStatus, offStatus, surfaceHarness } from './surface-client-fixture.mjs';
import { READINESS_REQUEST, READINESS_RESPONSE } from '../runtime/surface-readiness.mjs';
import { createSurfaceClient } from '../ui/surface-client.mjs';

const status = { consent: { mode: 'full', resume_mode: null, consent_epoch: 1 },
  delivery: { transport_state: 'disconnected' }, phase: 'identity', brain_reachable: false };

test('connection recovery exhausts safely and resumes from explicit retry or page wake', async () => {
  const original = {
    chrome: globalThis.chrome, document: globalThis.document,
    window: globalThis.window, fetch: globalThis.fetch,
  };
  const events = {};
  const portMessages = [], disconnectListeners = [];
  let connections = 0, allowed = false, stateChanges = 0;
  const port = {
    onMessage: { addListener(listener) { portMessages.push(listener); } },
    onDisconnect: { addListener(listener) { disconnectListeners.push(listener); } },
    postMessage() {}, disconnect() {},
  };
  globalThis.chrome = {
    runtime: {
      async sendMessage(message) {
        return { ok: true, result: message.type?.includes('legal')
          ? { configured: false } : status };
      },
      getURL() { return 'chrome-extension://test/config.json'; },
      connect() {
        connections++;
        if (!allowed) throw new Error('no extension port');
        return port;
      },
    },
    storage: {
      session: { async get() { return {}; } },
      onChanged: { addListener() {}, removeListener() {} },
    },
  };
  globalThis.fetch = async () => { throw new Error('fixture config unavailable'); };
  globalThis.document = { visibilityState: 'visible',
    addEventListener() {}, removeEventListener() {} };
  globalThis.window = { addEventListener(name, listener) { events[name] = listener; },
    removeEventListener() {} };
  const observed = [];
  const client = createSurfaceClient(model => {
    stateChanges++; observed.push(model.connectionRecovery);
  }, () => {});
  try {
    await client.start();
    assert.equal(client.model.connectionRecovery, 'retrying');
    await new Promise(resolve => setTimeout(resolve, 1950));
    assert.equal(connections, 4, 'one initial attempt plus three bounded retries');
    assert.equal(client.model.connectionRecovery, 'exhausted');
    const fixed = stateChanges;
    await new Promise(resolve => setTimeout(resolve, 350));
    assert.equal(connections, 4, 'exhausted state cannot silently poll forever');
    assert.equal(stateChanges, fixed);
    allowed = true;
    await client.sync();
    assert.equal(connections, 5, 'Try again starts another attempt after exhaustion');
    assert.equal(client.model.connectionRecovery, 'retrying');
    portMessages[0]({ state: 'unpaired' });
    assert.equal(client.model.connectionRecovery, 'idle');
    allowed = false;
    disconnectListeners[0]();
    await new Promise(resolve => setTimeout(resolve, 1950));
    assert.equal(client.model.connectionRecovery, 'exhausted');
    allowed = true;
    events.focus();
    assert.equal(client.model.connectionRecovery, 'retrying');
    assert.equal(connections, 9, 'wake restarts bounded connection recovery');
    portMessages[1]({ state: 'unpaired' });
    assert.equal(client.model.connectionRecovery, 'idle');
    assert.equal(client.model.desktopRuntimeReachable, false);
    assert.ok(observed.includes('retrying') && observed.includes('exhausted'));
  } finally {
    client.stop();
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});

function checkLostInvalidation(channel, notify) {
  test('F07 ' + channel + ' invalidation during a held read results in one follow-up read', async t => {
    const fixture = surfaceHarness(t);
    await fixture.client.start();
    const initialRequestCount = fixture.readCount();
    const oldRead = Promise.withResolvers();
    fixture.holdStatus(oldRead.promise);
    const refresh = fixture.client.refresh();
    assert.equal(fixture.readCount(), initialRequestCount + 1);
    fixture.setStatus(offStatus);
    notify(fixture);
    notify(fixture);
    oldRead.resolve(fullStatus);
    await refresh;
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual({ mode: fixture.client.model.status?.consent.mode, requests: fixture.readCount() },
      { mode: 'revoked', requests: initialRequestCount + 2 });
  });
}
checkLostInvalidation('port', fixture => fixture.fromWorker.deliver({ type: 'surface_changed' }));
checkLostInvalidation('storage', fixture => fixture.chromeApi.storage.onChanged.deliver({}, 'local'));

for (const onlineOutcome of ['reply', 'failure']) {
  test('F08 a held status ' + onlineOutcome + ' does not overwrite a companion disconnect', async t => {
    const fixture = surfaceHarness(t);
    await fixture.client.start();
    fixture.fromWorker.deliver({ state: 'paired' });
    assert.equal(fixture.client.model.desktopRuntimeReachable, true);
    const oldRead = Promise.withResolvers();
    fixture.holdStatus(oldRead.promise);
    const pending = fixture.client.refresh();
    fixture.closePort();
    if (onlineOutcome === 'reply') oldRead.resolve(fullStatus);
    else oldRead.reject(new Error('obsolete status failure'));
    await pending;
    assert.equal(fixture.errors.length, 0);
    assert.deepEqual({ reachable: fixture.client.model.desktopRuntimeReachable, pairing: fixture.client.model.pairing.state,
      recovery: fixture.client.model.connectionRecovery }, { reachable: false, pairing: 'unavailable', recovery: 'retrying' });
  });
}

function deliverReadiness(channel, request, authority) {
  channel.deliver({ type: READINESS_RESPONSE, request_id: request.request_id,
    commercial_authority: authority, analysis_admission: authority === 'active' ? 'admitted' : 'blocked' });
}

for (const seed of [7, 17, 37, 71, 109, 181]) {
  test('F09 matches the current readiness reply in generated delivery order ' + seed, async t => {
    const fixture = surfaceHarness(t);
    await fixture.client.start();
    fixture.fromWorker.deliver({ state: 'paired' });
    const requests = () => fixture.posts.filter(value => value.type === READINESS_REQUEST);
    const old = requests().at(-1);
    fixture.setStatus({ ...fullStatus, consent: { ...fullStatus.consent, consent_epoch: crypto.randomUUID() } });
    await fixture.client.refresh();
    const current = requests().at(-1);
    assert.notEqual(old.request_id, current.request_id);
    const replies = [
      { request: old, authority: 'unavailable' },
      { request: old, authority: 'required' },
      { request: current, authority: 'active' },
    ];
    let order = seed;
    for (let index = replies.length - 1; index > 0; index--) {
      order = (Math.imul(order, 1664525) + 1013904223) >>> 0;
      const replacement = order % (index + 1);
      [replies[replacement], replies[index]] = [replies[index], replies[replacement]];
    }
    let expected = 'unknown';
    for (const reply of replies) {
      if (reply.request === current) expected = 'active';
      deliverReadiness(fixture.fromWorker, reply.request, reply.authority);
      assert.equal(fixture.client.model.analysisReadiness.commercial_authority, expected);
    }
  });
}

test('F09 cannot settle a replacement port from an old paired channel', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const fixture = surfaceHarness(t);
  await fixture.client.start();
  const original = fixture.fromWorker;
  original.deliver({ state: 'paired' });
  const requests = () => fixture.posts.filter(value => value.type === READINESS_REQUEST);
  const old = requests().at(-1);
  fixture.closePort();
  t.mock.timers.tick(200);
  await new Promise(resolve => setImmediate(resolve));
  assert.notEqual(fixture.fromWorker, original);
  fixture.fromWorker.deliver({ state: 'paired' });
  const current = requests().at(-1);
  assert.notEqual(current.request_id, old.request_id);
  deliverReadiness(original, old, 'active');
  assert.equal(fixture.client.model.analysisReadiness.commercial_authority, 'unknown');
  deliverReadiness(fixture.fromWorker, current, 'required');
  assert.equal(fixture.client.model.analysisReadiness.commercial_authority, 'required');
});
