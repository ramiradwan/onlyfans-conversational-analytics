import assert from 'node:assert/strict';
import test from 'node:test';

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
