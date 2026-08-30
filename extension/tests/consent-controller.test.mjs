import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CONSENT_STORAGE_KEY,
  ConsentController,
  ONLYFANS_ORIGIN_PATTERN,
} from '../runtime/consent-controller.mjs';

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

function harness({ unregisterFails = false } = {}) {
  const local = {};
  const session = {};
  const registeredScripts = [];
  const removedPermissions = [];
  const permissionState = { onlyFans: false, localService: false, history: false };
  const counters = {
    starts: 0,
    suspends: 0,
    reloads: 0,
    deletes: 0,
    bindingClears: 0,
  };
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
        if (unregisterFails) throw new Error('scripting teardown failed');
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
        if (query.permissions) return permissionState.history;
        return false;
      },
      async remove(query) {
        removedPermissions.push(structuredClone(query));
        permissionState.onlyFans = false;
        permissionState.localService = false;
        permissionState.history = false;
        return true;
      },
    },
    alarms: {
      onAlarm: event(),
      async create() {},
    },
    tabs: {
      async query() { return [{ id: 7 }]; },
      async sendMessage() { return { ok: true }; },
      async reload() { counters.reloads += 1; },
    },
  };
  const preview = {
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
  };
  const bridge = () => ({ register() {}, unregister() {} });
  const activeModeAuthorization = {
    async authorizeTransition() { return true; },
    async authorizeResume() { return true; },
    async reconcileActiveMode() { return true; },
  };
  const controller = new ConsentController({
    chromeApi,
    runtime: {
      async start() { counters.starts += 1; },
      async suspend() { counters.suspends += 1; },
    },
    adapter: {
      async loadBrainBinding() { throw new Error('not bound'); },
      async clearBrainBinding() { counters.bindingClears += 1; },
    },
    brainBindingBridge: bridge(),
    provisioningIdentityBridge: bridge(),
    previewMetrics: preview,
    async clearLocalData() {
      counters.deletes += 1;
      await chromeApi.storage.local.clear();
      await chromeApi.storage.session.clear();
    },
    activeModeAuthorization,
    fetchImpl: async () => { throw new Error('not installed'); },
    now: () => new Date('2030-01-08T12:00:00Z'),
  });
  return {
    chromeApi,
    controller,
    counters,
    local,
    permissionState,
    registeredScripts,
    removedPermissions,
    session,
  };
}

test('capture is absent before consent and optional site access', async () => {
  const h = harness();
  await h.controller.initialize();
  assert.equal((await h.controller.status()).phase, 'off');
  assert.deepEqual(h.registeredScripts, []);
  await assert.rejects(h.controller.setMode('preview'), /must be granted/);
  assert.deepEqual(h.registeredScripts, []);
  assert.equal(Object.hasOwn(h.local, CONSENT_STORAGE_KEY), false);
});

test('preview can be enabled, paused, resumed, and fully deleted without a local service', async () => {
  const h = harness();
  h.permissionState.onlyFans = true;
  await h.controller.setMode('preview');
  assert.equal((await h.controller.status()).phase, 'preview');
  assert.deepEqual(
    h.registeredScripts.map((entry) => entry.id).sort(),
    ['ofca-preview-isolated', 'ofca-preview-main'],
  );
  assert.deepEqual(h.registeredScripts[0].matches, [ONLYFANS_ORIGIN_PATTERN]);

  await h.controller.setMode('pause');
  assert.equal((await h.controller.status()).phase, 'paused');
  assert.deepEqual(h.registeredScripts, []);

  h.permissionState.onlyFans = true;
  await h.controller.setMode('resume');
  assert.equal((await h.controller.status()).phase, 'preview');
  assert.equal(h.registeredScripts.length, 2);

  await h.controller.deleteLocalData();
  const deleted = await h.controller.status();
  assert.equal(deleted.phase, 'off');
  assert.equal(deleted.consent.mode, 'off');
  assert.deepEqual(h.registeredScripts, []);
  assert.deepEqual(h.local, {});
  assert.deepEqual(h.session, {});
  assert.equal(h.permissionState.onlyFans, false);
  assert.equal(h.counters.deletes, 1);
  assert.equal(h.counters.bindingClears, 1);
  assert.equal(h.removedPermissions.length, 1);
});

test('saved preview consent enters permission-required state and can be re-granted', async () => {
  const h = harness();
  h.permissionState.onlyFans = true;
  await h.controller.setMode('preview');
  h.permissionState.onlyFans = false;
  await h.controller.reconcile();
  let status = await h.controller.status();
  assert.equal(status.consent.mode, 'preview');
  assert.equal(status.phase, 'permission_required');
  assert.deepEqual(h.registeredScripts, []);

  h.permissionState.onlyFans = true;
  await h.controller.setMode('preview');
  status = await h.controller.status();
  assert.equal(status.phase, 'preview');
  assert.equal(h.registeredScripts.length, 2);
});

test('full analytics requires separate local service access', async () => {
  const h = harness();
  h.permissionState.onlyFans = true;
  await assert.rejects(h.controller.setMode('full'), /Local analytics service access/);
  h.permissionState.localService = true;
  await h.controller.setMode('full');
  assert.equal((await h.controller.status()).phase, 'identity');
});

test('pause and revoke fail closed when content-script teardown fails', async () => {
  const paused = harness({ unregisterFails: true });
  paused.permissionState.onlyFans = true;
  await paused.controller.setMode('preview');
  await assert.rejects(paused.controller.setMode('pause'), /scripting teardown failed/);
  assert.equal((await paused.controller.status()).phase, 'unavailable');

  const revoked = harness({ unregisterFails: true });
  revoked.permissionState.onlyFans = true;
  await revoked.controller.setMode('preview');
  await assert.rejects(revoked.controller.setMode('revoked'), /scripting teardown failed/);
  assert.equal((await revoked.controller.status()).phase, 'unavailable');
  assert.equal(revoked.removedPermissions.length, 1);
});

test('delete attempts permission and storage cleanup after content-script teardown failure', async () => {
  const h = harness({ unregisterFails: true });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('preview');
  await assert.rejects(h.controller.deleteLocalData(), /scripting teardown failed/);
  assert.equal((await h.controller.status()).phase, 'unavailable');
  assert.equal(h.removedPermissions.length, 1);
  assert.equal(h.counters.bindingClears, 1);
  assert.equal(h.counters.deletes, 1);
  assert.deepEqual(h.local, {});
  assert.deepEqual(h.session, {});
});
