import assert from 'node:assert/strict';
import test from 'node:test';
import { createSurfaceClient } from '../ui/surface-client.mjs';
import { productionWorkspaceRoutes } from '../runtime/onboarding-entry.mjs';

test('production workspace uses the exact registered Brain path', () => {
  assert.equal(productionWorkspaceRoutes({ runtime: { getURL: () => 'chrome-extension://fixture/setup.html' } }).provisioning,
    'http://bridge.localhost:17871/provisioning');
});

test('repeated worker disconnects exhaust finite recovery; only a visible wake restarts it', async () => {
  const previous = Object.fromEntries(['chrome', 'document', 'window', 'fetch', 'setTimeout', 'clearTimeout'].map((key) => [key, globalThis[key]]));
  const scheduled = new Map(); let id = 0; const connections = []; const wake = new Map();
  globalThis.setTimeout = (callback, delay) => { scheduled.set(++id, { callback, delay }); return id; };
  globalThis.clearTimeout = (key) => scheduled.delete(key);
  globalThis.document = { visibilityState: 'visible', addEventListener: (name, fn) => wake.set(name, fn), removeEventListener() {} };
  globalThis.window = { addEventListener: (name, fn) => wake.set(name, fn), removeEventListener() {} };
  globalThis.fetch = async () => ({ json: async () => ({}) });
  globalThis.chrome = {
    runtime: { getURL: () => 'fixture', async sendMessage() { return { ok: true, status: { consent: { mode: 'preview' } } }; },
      connect() {
        let disconnected;
        const port = { onMessage: { addListener() {} }, onDisconnect: { addListener(fn) { disconnected = fn; } },
          disconnect() { disconnected?.(); }, postMessage() {} };
        connections.push(port); return port;
      } },
    storage: { session: { async get() { return {}; } }, onChanged: { addListener() {}, removeListener() {} } },
  };
  let client;
  try {
    client = createSurfaceClient(() => {}, (error) => { throw error; });
    await client.start();
    for (let attempt = 0; attempt < 4; attempt++) {
      connections.at(-1).disconnect();
      const timer = [...scheduled.entries()].find(([, value]) => value.delay <= 1000);
      if (attempt < 3) {
        assert.ok(timer); scheduled.delete(timer[0]); timer[1].callback(); await client.refresh();
      } else assert.equal(timer, undefined);
    }
    assert.equal(connections.length, 4);
    assert.equal(scheduled.size, 0, 'no status-polling timer survives the retry budget');
    wake.get('focus')(); await client.refresh();
    assert.equal(connections.length, 5);
  } finally {
    client?.stop();
    Object.assign(globalThis, previous);
  }
});
