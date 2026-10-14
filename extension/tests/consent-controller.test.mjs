import assert from 'node:assert/strict';
import test from 'node:test';
import { OBSERVER_HELPER_KEY, OBSERVER_STATE_TYPE } from '../runtime/document-observer.mjs';
import { createConnectionRecovery, CONNECTION_RECOVERY_KEY } from '../runtime/recovery-backoff.mjs';

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

function harness({ unregisterFails = false, ...options } = {}) {
  const local = {};
  const session = {};
  const registeredScripts = [];
  const onlyFansTabs = [{ id: 7, frozen: false }];
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
      async executeScript({ target }) { return [{ frameId: 0, documentId: `document-${target.tabId}` }]; },
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
        if (query.origins?.includes('https://bridge.localhost:17871/*')) return permissionState.localService;
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
      async query() { return structuredClone(onlyFansTabs); },
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
    ...options,
  });
  return {
    chromeApi,
    controller,
    counters,
    local,
    onlyFansTabs,
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

test('capture notifications require the exact trusted content envelope and are throttled', async () => {
  let time = 0;
  let nextTimer = 0;
  const timers = new Map();
  const scheduler = {
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, at: time + delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    tick(ms) {
      const end = time + ms;
      while (true) {
        const due = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!due || due[1].at > end) break;
        time = due[1].at;
        timers.delete(due[0]);
        due[1].callback();
      }
      time = end;
    },
  };
  let reports = 0;
  const h = harness({ scheduler, now: () => new Date(time), runtime: {
    async start() {}, async suspend() {},
    history: { requestCaptureStateReport() { reports++; } },
  } });
  h.controller.register();
  const listener = h.chromeApi.runtime.onMessage.listeners[0];
  const trusted = { id: 'synthetic-extension-id', frameId: 0, url: 'https://onlyfans.com/chats' };
  const stateChanged = { type: 'ofca.capture.state.changed' };
  const queueChanged = { type: 'ofca.capture.queue.changed' };
  listener(stateChanged, { ...trusted, url: 'https://untrusted.example/' }, () => assert.fail('no response'));
  listener({ ...stateChanged, extra: true }, trusted, () => assert.fail('no response'));
  assert.equal(reports, 0);
  listener(stateChanged, trusted, () => assert.fail('no response'));
  assert.equal(reports, 1, 'the leading notification requests a report immediately');
  for (let i = 0; i < 100; i++) {
    listener(i % 2 ? stateChanged : queueChanged, trusted, () => assert.fail('no response'));
    scheduler.tick(100);
  }
  scheduler.tick(5_000);
  assert.ok(reports > 0);
  assert.ok(reports <= 3, `expected at most three reports, got ${reports}`);
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
  assert.equal(h.registeredScripts.length, 2, 'paused Preview retains its compatible observer');

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

test('observer status messages receive a reply without accepting stale or foreign documents', async () => {
  const h = harness();
  h.permissionState.onlyFans = true;
  await h.controller.setMode('preview');
  h.controller.register();
  const listener = h.chromeApi.runtime.onMessage.listeners[0];
  const sender = { id: h.chromeApi.runtime.id, frameId: 0, tab: { id: 7 },
    url: 'https://onlyfans.com/', documentId: 'document-7' };
  const status = { mode: 'preview', active: true, forwarding: true, ws2_socket_open: true };
  for (const [candidate, ok] of [[sender, true], [{ ...sender, documentId: 'stale' }, false],
    [{ ...sender, id: 'foreign' }, false]]) {
    const replies = [];
    assert.equal(listener({ type: OBSERVER_STATE_TYPE, status }, candidate, (reply) => replies.push(reply)), false);
    assert.deepEqual(replies, [{ ok }]);
  }
});

test('permission-removal reconciliation after deletion cannot recreate an active helper record', async () => {
  const h = harness(); h.permissionState.onlyFans = true;
  h.onlyFansTabs.length = 0;
  h.chromeApi.tabs.create = async (options) => { const tab = { id: 12, ...options }; h.onlyFansTabs.push(tab); return { ...tab }; };
  h.chromeApi.tabs.update = async (id, values) => Object.assign(h.onlyFansTabs.find((tab) => tab.id === id), values);
  h.chromeApi.tabs.query = async () => h.permissionState.onlyFans ? structuredClone(h.onlyFansTabs) : [];
  await h.controller.setMode('preview');
  assert.equal(h.local[OBSERVER_HELPER_KEY].tab_id, 12);
  const remove = h.chromeApi.permissions.remove;
  h.chromeApi.permissions.remove = async (query) => {
    const result = await remove(query);
    for (const listener of h.chromeApi.permissions.onRemoved.listeners) listener();
    return result;
  };
  await h.controller.deleteLocalData();
  const status = await h.controller.status();
  assert.deepEqual(h.local, {}); assert.deepEqual(h.session, {});
  assert.equal(status.consent.mode, 'off'); assert.equal(status.observer.helper, 'none');
  assert.equal(h.controller.observer.ownerSession, null);
  assert.equal(h.counters.reloads, 0);
});

test('deletion waits for a pending helper closure write before the final local-data clear', async () => {
  const h = harness(); h.permissionState.onlyFans = true;
  h.onlyFansTabs.length = 0;
  h.chromeApi.tabs.onRemoved = event();
  h.chromeApi.tabs.create = async (options) => { const tab = { id: 12, ...options }; h.onlyFansTabs.push(tab); return { ...tab }; };
  h.chromeApi.tabs.update = async (id, values) => Object.assign(h.onlyFansTabs.find((tab) => tab.id === id), values);
  await h.controller.setMode('preview');
  const set = h.chromeApi.storage.local.set;
  let finishWrite;
  h.chromeApi.storage.local.set = (values) => {
    if (!Object.hasOwn(values, OBSERVER_HELPER_KEY)) return set(values);
    const captured = structuredClone(values);
    return new Promise((resolve) => { finishWrite = async () => { await set(captured); resolve(); }; });
  };
  h.onlyFansTabs.splice(h.onlyFansTabs.findIndex((tab) => tab.id === 12), 1);
  for (const listener of h.chromeApi.tabs.onRemoved.listeners) listener(12, { isWindowClosing: false });
  const deletion = h.controller.deleteLocalData();
  while (!h.controller.observer.helperPersistenceSuspended) await new Promise(setImmediate);
  assert.equal(h.counters.deletes, 0);
  await finishWrite(); await deletion; await h.controller.reconcile();
  assert.deepEqual(h.local, {}); assert.deepEqual(h.session, {});
  assert.equal(h.controller.observer.snapshot().helper, 'none');
  assert.equal(h.counters.deletes, 1);
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

test('Full status reports when every OnlyFans tab is sleeping', async () => {
  const h = harness();
  h.controller.adapter.loadBrainBinding = async () => {};
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  h.onlyFansTabs[0].frozen = true;
  h.controller.observer.tabs.get(7).frozen = true;
  assert.equal((await h.controller.status()).delivery.browser_tab_sleeping, true);
  h.onlyFansTabs.push({ id: 8, frozen: false });
  h.controller.observer.tabs.set(8, { id: 8, frozen: false });
  assert.equal((await h.controller.status()).delivery.browser_tab_sleeping, false);
});

test('Full requires authenticated pairing without local HTTP host access', async () => {
  const h = harness();
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  assert.equal(h.permissionState.localService, false);
  assert.equal((await h.controller.status()).phase, 'identity');
});

test('pause and revoke fail closed when content-script teardown fails', async () => {
  const paused = harness({ unregisterFails: true });
  paused.permissionState.onlyFans = true;
  await paused.controller.setMode('preview');
  await paused.controller.setMode('pause');
  assert.equal((await paused.controller.status()).phase, 'paused');
  assert.equal(paused.controller.captureScope.isOpen, false);

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
  await assert.rejects(h.controller.deleteLocalData(), { code: 'delete_incomplete' });
  assert.equal((await h.controller.status()).phase, 'unavailable');
  assert.equal(h.removedPermissions.length, 1);
  assert.equal(h.counters.bindingClears, 1);
  assert.equal(h.counters.deletes, 1);
  assert.equal(h.local.ofca_delete_intent_v1.schema, 'ofca-delete-intent/v1');
  assert.deepEqual(h.session, {});
});

function manualScheduler() {
  const timers = new Set();
  return {
    timers,
    setTimeout(callback, delay) {
      const timer = { callback, delay };
      timers.add(timer);
      return timer;
    },
    clearTimeout(timer) { timers.delete(timer); },
    async fire() {
      const timer = timers.values().next().value;
      assert.ok(timer, 'expected a pending binding retry');
      timers.delete(timer);
      await timer.callback();
      return timer.delay;
    },
  };
}

test('saved pairing retries binding with capped backoff without leaving identity, then starts full', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  let bound = false;
  const probePhases = [];
  h.controller.adapter.loadBrainBinding = async () => {
    probePhases.push(h.controller.phase);
    if (!bound) throw new Error('offline');
  };
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  for (const delay of [500, 1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    assert.equal(await scheduler.fire(), delay);
    assert.equal(h.controller.phase, 'identity');
    assert.equal(h.counters.starts, 0);
    assert.equal(probePhases.at(-1), 'identity');
  }
  bound = true;
  await scheduler.fire();
  assert.equal(h.controller.phase, 'full');
  assert.equal(h.counters.starts, 1);
  assert.equal(scheduler.timers.size, 0);
  assert.equal((await h.controller.status()).reload_required, false);
});

test('missing or unreadable saved pairing does not schedule binding probes', async () => {
  for (const hasSavedPairing of [undefined, async () => false, async () => { throw new Error('unavailable'); }]) {
    const scheduler = manualScheduler();
    const h = harness({ scheduler, ...(hasSavedPairing ? { hasSavedPairing } : {}) });
    h.permissionState.onlyFans = true;
    await h.controller.setMode('full');
    assert.equal(h.controller.phase, 'identity');
    assert.equal(scheduler.timers.size, 0);
  }
});

test('saved pairing recovers on the next periodic wake after bounded retries are exhausted', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  const alarms = [];
  h.chromeApi.alarms.create = async (name, options) => { alarms.push({ name, options }); };
  let bound = false;
  h.controller.adapter.loadBrainBinding = async () => {
    if (!bound) throw new Error('connection unavailable during recovery cooldown');
  };
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  for (let attempt = 0; attempt < 9; attempt += 1) await scheduler.fire();
  assert.equal(h.controller.phase, 'identity');
  assert.equal(scheduler.timers.size, 0);
  assert.ok(alarms.some(({ name, options }) => name === 'ofca-agent-reconcile'
    && options.delayInMinutes === 1 && options.periodInMinutes === 1));

  bound = true;
  for (const listener of h.chromeApi.alarms.onAlarm.listeners) {
    listener({ name: 'ofca-agent-reconcile' });
  }
  await h.controller.controlQueue.run(async () => {});
  assert.equal(h.controller.phase, 'full');
  assert.equal(h.counters.starts, 1);
  assert.equal(h.counters.reloads, 0);
  assert.equal(scheduler.timers.size, 0);
  h.controller.adapter.loadBrainBinding = async () => assert.fail('Full runtime owns later wakeups');
  await reconcileAlarm(h);
  assert.equal(h.counters.starts, 1);
});

async function reconcileAlarm(h) {
  for (const listener of h.chromeApi.alarms.onAlarm.listeners) listener({ name: 'ofca-agent-reconcile' });
  await h.controller.controlQueue.run(async () => {});
}

test('periodic binding recovery retains the persisted connection cooldown', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  let time = 0, available = false, connected = false;
  const recovery = createConnectionRecovery({ storage: h.chromeApi.storage.local,
    now: () => time, random: () => 0.5 });
  h.controller.adapter.loadBrainBinding = async () => {
    if (connected) return;
    await recovery.reserve();
    if (!available) throw new Error('local service offline');
    connected = true;
  };
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  for (let attempt = 0; attempt < 9; attempt++) {
    time += scheduler.timers.values().next().value.delay;
    await scheduler.fire();
  }
  const exhausted = structuredClone(h.local[CONNECTION_RECOVERY_KEY]);
  assert.equal(exhausted.attempts, 6);
  assert.ok(time < exhausted.next_attempt_at);
  available = true;
  await reconcileAlarm(h);
  assert.deepEqual(h.local[CONNECTION_RECOVERY_KEY], exhausted);
  assert.equal(h.controller.phase, 'identity');
  assert.equal(h.counters.starts, 0);
  time = exhausted.next_attempt_at;
  await reconcileAlarm(h);
  assert.equal(h.controller.phase, 'full');
  assert.equal(h.counters.starts, 1);
  assert.equal(h.local[CONNECTION_RECOVERY_KEY].attempts, 1);
});

test('binding alarm does not duplicate a startup timer or an in-flight probe', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  let probes = 0;
  h.controller.adapter.loadBrainBinding = async () => { probes++; throw new Error('offline'); };
  await reconcileAlarm(h);
  assert.equal(probes, 0);
  for (let attempt = 0; attempt < 9; attempt++) await scheduler.fire();
  probes = 0;
  let release, entered;
  const pending = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  h.controller.adapter.loadBrainBinding = async () => {
    probes++;
    entered();
    await pending;
    throw new Error('offline');
  };
  const first = reconcileAlarm(h);
  await started;
  const duplicates = Array.from({ length: 20 }, () => reconcileAlarm(h));
  release();
  await Promise.all([first, ...duplicates]);
  assert.equal(probes, 1);
  assert.equal(scheduler.timers.size, 0);
  await reconcileAlarm(h);
  assert.equal(probes, 2, 'a later alarm may retry');
});

test('periodic recovery requires saved pairing, site access and active Full consent', async () => {
  for (const blockedBy of ['pairing absent', 'pairing unreadable', 'site access', 'pause', 'revoke', 'delete']) {
    const scheduler = manualScheduler();
    const h = harness({ scheduler, hasSavedPairing: async () => true });
    h.permissionState.onlyFans = true;
    await h.controller.setMode('full');
    for (let attempt = 0; attempt < 9; attempt++) await scheduler.fire();
    if (blockedBy === 'pairing absent') h.controller.hasSavedPairing = async () => false;
    if (blockedBy === 'pairing unreadable') h.controller.hasSavedPairing = async () => { throw new Error('unavailable'); };
    if (blockedBy === 'site access') h.permissionState.onlyFans = false;
    if (blockedBy === 'pause' || blockedBy === 'revoke') await h.controller.setMode(blockedBy === 'revoke' ? 'revoked' : 'pause');
    if (blockedBy === 'delete') await h.controller.deleteLocalData();
    h.controller.adapter.loadBrainBinding = async () => assert.fail(`probe blocked by ${blockedBy}`);
    await reconcileAlarm(h);
    assert.equal(h.counters.starts, 0);
    assert.equal(scheduler.timers.size, 0);
  }
});

test('a consent transition fences an in-flight periodic pairing read', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  for (let attempt = 0; attempt < 9; attempt++) await scheduler.fire();
  let release, entered;
  const pending = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  h.controller.hasSavedPairing = async () => { entered(); await pending; return true; };
  h.controller.adapter.loadBrainBinding = async () => assert.fail('stale retry cannot connect');
  const retry = reconcileAlarm(h);
  await started;
  const paused = h.controller.setMode('pause');
  release();
  await Promise.all([retry, paused]);
  assert.equal(h.controller.phase, 'paused');
  assert.equal(h.counters.starts, 0);
});

test('consent transition cancels binding retry and stale callback cannot probe', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  const stale = scheduler.timers.values().next().value;
  await h.controller.setMode('pause');
  let probes = 0;
  h.controller.adapter.loadBrainBinding = async () => { probes += 1; };
  await stale.callback();
  assert.equal(probes, 0);
  assert.equal(scheduler.timers.size, 0);
  assert.equal(h.controller.phase, 'paused');
});

test('partition event during successful retry lets queued reconcile finish recovery', async () => {
  const scheduler = manualScheduler();
  const h = harness({ scheduler, hasSavedPairing: async () => true });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  let first = true;
  h.controller.adapter.loadBrainBinding = async () => {
    if (first) {
      first = false;
      for (const listener of h.chromeApi.storage.onChanged.listeners) {
        listener({ active_account_partition_v5: {} }, 'session');
      }
    }
  };
  await scheduler.fire();
  assert.equal((await h.controller.status()).phase, 'full');
  assert.equal(scheduler.timers.size, 0);
});

test('invalidation during saved-pairing check skips scheduling without rejecting transition', async () => {
  const scheduler = manualScheduler();
  let paused;
  const h = harness({ scheduler, hasSavedPairing: async () => {
    paused = h.controller.setMode('pause');
    return true;
  } });
  h.permissionState.onlyFans = true;
  await h.controller.setMode('full');
  await paused;
  assert.equal(h.controller.phase, 'paused');
  assert.equal(scheduler.timers.size, 0);
});

for (const change of ['removed', 'created', 'updated', 'replaced']) {
  test(`tab ${change} reports observation changes without a runtime wake`, async () => {
    const h = harness();
    for (const name of ['onRemoved', 'onCreated', 'onUpdated', 'onReplaced']) h.chromeApi.tabs[name] = event();
    h.controller.state = { mode: 'full' };
    h.controller.phase = 'full';
    h.controller.captureScope = { isOpen: true };
    h.chromeApi.tabs.sendMessage = async (_id, value) => value.action === 'status'
      ? { mode: 'full', active: true, forwarding: true, ws2_socket_open: true }
      : { document: 'fixture-document', expired: 0, rejected: 0 };
    const reports = [];
    h.controller.runtime.configuration = { activeDocument: { history_acquisition: { enabled: true } } };
    h.controller.runtime.history = { async requestCaptureStateReport() {
      reports.push(await h.controller.captureState());
    } };
    await h.controller.observer.configure('full');
    assert.equal((await h.controller.captureState()).observing, true);
    h.controller.register();
    if (change === 'removed') h.onlyFansTabs.length = 0;
    else if (change === 'updated') h.onlyFansTabs[0].frozen = true;
    else if (change === 'created') h.onlyFansTabs.push({ id: 8, discarded: true });
    else h.chromeApi.tabs.sendMessage = async () => ({ mode: 'full', active: true, forwarding: true, ws2_socket_open: false });
    const source = h.chromeApi.tabs[`on${change[0].toUpperCase()}${change.slice(1)}`];
    for (const listener of source.listeners) {
      if (change === 'created') listener({ id: 8, url: 'https://onlyfans.com/', discarded: true });
      else if (change === 'replaced') listener(9, 7);
      else listener(7, { frozen: true }, { id: 7, frozen: true, url: 'https://onlyfans.com/' });
    }
    for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(reports.length, 1);
    assert.equal(h.counters.starts, 0);
    if (change === 'created') assert.equal(reports[0].tabs.discarded, 1);
    else assert.equal(reports[0].observing, false);
    if (change === 'removed') assert.equal(reports[0].reason, 'no_onlyfans_tab');
    if (change === 'replaced') assert.equal(reports[0].page_socket_open, false);
  });
}
