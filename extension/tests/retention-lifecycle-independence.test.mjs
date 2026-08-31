import assert from 'node:assert/strict';
import test from 'node:test';

import { ConsentController } from '../runtime/consent-controller.mjs';

function event() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    removeListener(listener) {
      const index = listeners.indexOf(listener);
      if (index >= 0) listeners.splice(index, 1);
    },
  };
}

function storageArea(values) {
  return {
    async get(keys) {
      return Object.fromEntries(
        keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])]),
      );
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async remove(keys) {
      for (const key of keys) delete values[key];
    },
    async clear() {
      for (const key of Object.keys(values)) delete values[key];
    },
  };
}

function harness() {
  const local = {};
  const session = {};
  const registeredScripts = [];
  const permissionState = { onlyFans: true, localService: true, history: true };
  const vaultRequests = [];
  const counters = { starts: 0, suspends: 0 };
  const chromeApi = {
    runtime: {
      id: 'synthetic-extension-id',
      getURL: (path = '') => `chrome-extension://synthetic-extension-id/${path}`,
      onMessage: event(),
    },
    storage: {
      local: storageArea(local),
      session: storageArea(session),
      onChanged: event(),
    },
    scripting: {
      async getRegisteredContentScripts() { return structuredClone(registeredScripts); },
      async registerContentScripts(scripts) { registeredScripts.push(...structuredClone(scripts)); },
      async unregisterContentScripts({ ids }) {
        for (const id of ids) {
          const index = registeredScripts.findIndex((entry) => entry.id === id);
          if (index >= 0) registeredScripts.splice(index, 1);
        }
      },
    },
    permissions: {
      onRemoved: event(),
      async contains(query) {
        if (query.origins?.includes('https://onlyfans.com/*')) return permissionState.onlyFans;
        if (query.origins?.includes('http://bridge.localhost:17871/*')) return permissionState.localService;
        if (query.permissions?.includes('webRequest')) return permissionState.history;
        return false;
      },
      async remove(query) {
        if (query.permissions?.includes('webRequest')) permissionState.history = false;
        if (query.origins?.includes('https://onlyfans.com/*')) permissionState.onlyFans = false;
        if (query.origins?.includes('http://bridge.localhost:17871/*')) permissionState.localService = false;
        return true;
      },
    },
    alarms: { onAlarm: event(), async create() {} },
    tabs: {
      async query() { return []; },
      async sendMessage() { return { ok: true }; },
      async reload() {},
    },
  };
  const bridge = () => ({ register() {}, unregister() {} });
  const controller = new ConsentController({
    chromeApi,
    runtime: {
      async start() { counters.starts += 1; },
      async suspend() { counters.suspends += 1; },
    },
    adapter: {
      async loadBrainBinding() { return { bound: true }; },
      async clearBrainBinding() {},
    },
    brainBindingBridge: bridge(),
    provisioningIdentityBridge: bridge(),
    previewMetrics: {
      async record() {},
      async summary() {
        return {
          retention_days: 7,
          chat_observations: 0,
          message_observations: 0,
          inbound_observations: 0,
          outbound_observations: 0,
          unknown_direction_observations: 0,
          days: [],
        };
      },
      async clear() {},
      async prune() {},
    },
    async clearLocalData() {},
    activeModeAuthorization: {
      async authorizeTransition() { return true; },
      async authorizeResume() { return true; },
      async reconcileActiveMode() { return true; },
    },
    async fetchImpl(url) {
      if (String(url).includes('/api/v1/settings/creator-vault')) vaultRequests.push(String(url));
      return { ok: true };
    },
    now: () => new Date('2030-01-08T12:00:00Z'),
  });
  return { chromeApi, controller, counters, permissionState, vaultRequests };
}

async function enterFull(h) {
  await h.controller.setMode('full', { evidenceEventId: 'mode-choice-full' });
  const status = await h.controller.status();
  assert.equal(status.phase, 'full');
  assert.equal(status.consent.mode, 'full');
}

test('pause leaves Creator Vault lifecycle untouched', async () => {
  const h = harness();
  await enterFull(h);

  await h.controller.setMode('pause');

  const status = await h.controller.status();
  assert.equal(status.phase, 'paused');
  assert.equal(status.consent.resume_mode, 'full');
  assert.equal(h.counters.suspends > 0, true);
  assert.deepEqual(h.vaultRequests, []);
});

test('Full to Preview leaves Creator Vault lifecycle untouched', async () => {
  const h = harness();
  await enterFull(h);

  await h.controller.setMode('preview', { evidenceEventId: 'mode-choice-preview' });

  const status = await h.controller.status();
  assert.equal(status.phase, 'preview');
  assert.equal(status.consent.mode, 'preview');
  assert.equal(h.counters.suspends > 0, true);
  assert.deepEqual(h.vaultRequests, []);
});

test('history permission revocation leaves Creator Vault lifecycle untouched', async () => {
  const h = harness();
  await enterFull(h);
  assert.equal(h.chromeApi.permissions.onRemoved.listeners.length, 1);

  h.permissionState.history = false;
  h.chromeApi.permissions.onRemoved.listeners[0]({ permissions: ['webRequest'] });
  await new Promise((resolve) => setImmediate(resolve));

  const status = await h.controller.status();
  assert.equal(status.history_permission, false);
  assert.equal(status.consent.mode, 'full');
  assert.deepEqual(h.vaultRequests, []);
});
