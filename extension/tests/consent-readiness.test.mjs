import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import { CONSENT_STORAGE_KEY, ConsentController, UI_RELOAD_TABS_MESSAGE_TYPE } from '../runtime/consent-controller.mjs';
import { DELETE_INTENT_KEY } from '../runtime/deletion-state.mjs';
import { LegalActivationController } from '../runtime/legal-activation-controller.mjs';
import { LegalConsentAuthorization, authorizationScope } from '../runtime/legal-consent-authorization.mjs';
import { clearExtensionLocalData } from '../runtime/local-data.mjs';
import { PreviewMetricsStore, PREVIEW_METRICS_STORAGE_KEY } from '../runtime/preview-metrics.mjs';
import { AgentRuntime } from '../transport/agent-runtime-core.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const bindings = JSON.parse(await readFile(new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url)));
const now = () => new Date('2030-01-08T12:00:00.000Z');
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function event() {
  const listeners = [];
  return { addListener: (fn) => listeners.push(fn), removeListener() {}, listeners };
}

function area(values) {
  return {
    async get(keys) {
      return structuredClone(Object.fromEntries((keys === null ? Object.keys(values) : keys)
        .filter((key) => Object.hasOwn(values, key)).map((key) => [key, values[key]])));
    },
    async set(update) { Object.assign(values, structuredClone(update)); },
    async remove(keys) { for (const key of typeof keys === 'string' ? [keys] : keys) delete values[key]; },
    async clear() { for (const key of Object.keys(values)) delete values[key]; },
    async setAccessLevel() {},
  };
}

// Enforce closed-connection and blocked-deletion behavior absent from the minimal fake.
function evidenceDatabase() {
  const raw = new FakeIndexedDb();
  const connections = new Set();
  return {
    connections,
    async databases() { return [...raw.databases.keys()].map((name) => ({ name })); },
    open(...args) {
      const request = raw.open(...args);
      return new Proxy(request, {
        set(target, key, value) {
          target[key] = key === 'onsuccess' ? (...eventArgs) => {
            const database = target.result;
            const transaction = database.transaction.bind(database);
            let closed = false;
            database.transaction = (...transactionArgs) => {
              if (closed) throw new Error('Connection is closed');
              return transaction(...transactionArgs);
            };
            database.close = () => { closed = true; connections.delete(database); };
            connections.add(database);
            value(...eventArgs);
          } : value;
          return true;
        },
      });
    },
    deleteDatabase(name) {
      if (connections.size === 0) return raw.deleteDatabase(name);
      const request = {};
      queueMicrotask(() => request.onblocked?.());
      return request;
    },
  };
}

function harness({ local = {}, indexedDb = evidenceDatabase(), bindingRef = { current: structuredClone(bindings) } } = {}) {
  const session = {};
  const scripts = [];
  const flags = { permission: true, removePermission: true, reloads: 0, starts: 0, stops: 0 };
  const chromeApi = {
    runtime: { id: 'synthetic', getURL: (path = '') => `chrome-extension://synthetic/${path}`, onMessage: event() },
    storage: { local: area(local), session: area(session), onChanged: event() },
    permissions: {
      onAdded: event(), onRemoved: event(),
      async contains(query) { return query.permissions ? false : flags.permission; },
      async remove() { if (flags.removePermission) flags.permission = false; return flags.removePermission; },
    },
    scripting: {
      async getRegisteredContentScripts() { return structuredClone(scripts); },
      async registerContentScripts(next) { scripts.push(...next); },
      async unregisterContentScripts() { scripts.length = 0; },
    },
    tabs: {
      async query() { return [{ id: 1 }]; },
      async sendMessage() {},
      async reload() { flags.reloads += 1; },
    },
    alarms: { onAlarm: event(), async create() {} },
  };
  const evidenceStore = new ActivationEvidenceStore({ indexedDb, softwareVersion: '2.0.1', now });
  const authorization = new LegalConsentAuthorization({ evidenceStore, bindings: () => bindingRef.current });
  const preview = new PreviewMetricsStore({ storage: chromeApi.storage.local, now });
  const runtime = new AgentRuntime({
    registerWakeListeners: () => () => {},
    initialize: async () => ({ transport: {
      start() { flags.starts += 1; }, stop() { flags.stops += 1; }, ensureConnected() {},
    } }),
  });
  const bridge = { register() {}, unregister() {}, async clearContexts() {} };
  const consent = new ConsentController({
    chromeApi, runtime, previewMetrics: preview, activeModeAuthorization: authorization,
    activationEvidenceStore: evidenceStore, provisioningIdentityBridge: bridge,
    adapter: { async loadBrainBinding() { return {}; }, async clearBrainBinding() {} },
    clearLocalData: () => clearExtensionLocalData({ chromeApi, indexedDb }),
    fetchImpl: async () => ({ ok: true }), now,
  });
  const legal = new LegalActivationController({ chromeApi, consentController: consent, evidenceStore, bindings: () => bindingRef.current });
  const activate = async (mode = 'preview') => {
    await legal.acceptTerms();
    await legal.acknowledgeRisk();
    await legal.activateSoftware();
    return legal.chooseMode(mode);
  };
  return { local, session, indexedDb, bindingRef, flags, chromeApi, evidenceStore, authorization, preview, runtime, consent, legal, activate, scripts };
}

test('native legal mode choice completes without queue recursion and reactivates after same-worker deletion', { timeout: 2000 }, async () => {
  const h = harness();
  const first = await h.activate();
  assert.equal(first.status.consent.schema, 'ofca-consent/v2');
  assert.equal(first.status.consent.authorization_event_id, first.evidence.event_id);
  assert.equal(h.consent.captureScope.isOpen, true);
  assert.equal(h.flags.reloads, 0);
  assert.equal(first.status.reload_required, true);
  await h.consent.deleteLocalData();
  assert.equal(h.indexedDb.connections.size, 0);
  assert.deepEqual(h.local, {});
  assert.equal(h.consent.captureScope.isOpen, false);
  h.flags.permission = true;
  const second = await h.activate();
  assert.notEqual(second.evidence.event_id, first.evidence.event_id);
  assert.equal((await h.evidenceStore.exportAuditTrail()).length, 3);
  assert.equal(h.consent.captureScope.isOpen, true);
});

test('deletion waits for an admitted legal write and prevents its late flow save from resurrecting data', { timeout: 2000 }, async () => {
  const h = harness();
  const entered = deferred();
  const release = deferred();
  const original = h.evidenceStore.recordTermsAcceptance.bind(h.evidenceStore);
  h.evidenceStore.recordTermsAcceptance = async (options) => {
    entered.resolve(); await release.promise; return original(options);
  };
  const accepting = h.legal.acceptTerms();
  const rejected = assert.rejects(accepting, { code: 'delete_requested' });
  await entered.promise;
  const deleting = h.consent.deleteLocalData();
  assert.equal(h.consent.legalScope.isOpen, false);
  release.resolve();
  await rejected;
  await deleting;
  assert.deepEqual(h.local, {});
  assert.deepEqual(await h.indexedDb.databases(), []);
});

test('deletion drains a Preview storage write already in progress before clearing storage', { timeout: 2000 }, async () => {
  const h = harness(); await h.activate();
  const entered = deferred(); const release = deferred();
  const original = h.chromeApi.storage.local.set;
  h.chromeApi.storage.local.set = async (update) => {
    if (Object.hasOwn(update, PREVIEW_METRICS_STORAGE_KEY)) { entered.resolve(); await release.promise; }
    await original(update);
  };
  const recording = h.consent.captureScope.run(({ assertCurrent }) => h.preview.record({
    kind: 'message', direction: 'inbound', observed_at: now().toISOString(),
    activity_at: now().toISOString(), creator_id: 'creator-synthetic', record_id: 'message-synthetic', chat_id: 'chat-synthetic',
  }, { assertCurrent }));
  const rejected = assert.rejects(recording, { code: 'delete_requested' });
  await entered.promise;
  const deleting = h.consent.deleteLocalData();
  release.resolve();
  await rejected; await deleting;
  assert.deepEqual(h.local, {});
});

test('failed permission removal preserves intent and blocks activation until restart recovery succeeds', { timeout: 2000 }, async () => {
  const h = harness(); await h.activate(); h.flags.removePermission = false;
  await assert.rejects(h.consent.deleteLocalData(), { code: 'delete_incomplete' });
  assert.equal(h.consent.state.mode, 'off');
  assert.equal(h.local[DELETE_INTENT_KEY].requested_at, now().toISOString());
  await assert.rejects(h.legal.acceptTerms(), { code: 'delete_incomplete' });
  const restarted = harness({ local: h.local, indexedDb: h.indexedDb });
  await restarted.consent.initialize();
  assert.equal(restarted.consent.state.mode, 'off');
  assert.equal(restarted.local[DELETE_INTENT_KEY], undefined);
  assert.equal(restarted.flags.starts, 0);
});

test('failed startup deletion recovery can be retried in the same worker', { timeout: 2000 }, async () => {
  const local = { [DELETE_INTENT_KEY]: { schema: 'ofca-delete-intent/v1', requested_at: now().toISOString() } };
  const h = harness({ local }); h.flags.removePermission = false;
  await assert.rejects(h.consent.initialize(), { code: 'delete_incomplete' });
  h.flags.removePermission = true;
  await h.consent.initialize();
  assert.equal(h.local[DELETE_INTENT_KEY], undefined);
  h.flags.permission = true;
  await h.activate();
});

test('v1 active consent migrates to paused and cannot borrow an obsolete authorization pointer', { timeout: 2000 }, async () => {
  const local = { [CONSENT_STORAGE_KEY]: {
    schema: 'ofca-consent/v1', mode: 'full', resume_mode: null, policy_revision: '1', updated_at: now().toISOString(),
  }, retained_data: 'synthetic', ofca_legal_authorization_v1: { mode: 'full', event_id: crypto.randomUUID() } };
  const h = harness({ local }); await h.consent.initialize();
  assert.equal(h.consent.state.mode, 'paused');
  assert.equal(h.consent.state.authorization_event_id, null);
  assert.equal(h.local.retained_data, 'synthetic');
  await assert.rejects(h.consent.setMode('resume'), /requires Legal/);
  assert.equal(h.flags.starts, 0);
});

test('stop cancels startup synchronously and the stale initializer cannot reopen capture', { timeout: 2000 }, async () => {
  const h = harness(); await h.activate('full');
  const saved = structuredClone(h.local);
  const restarted = harness({ local: saved, indexedDb: h.indexedDb });
  const entered = deferred(); const release = deferred();
  restarted.runtime.initialize = async ({ signal }) => {
    entered.resolve(signal); await release.promise;
    return { transport: { start() { restarted.flags.starts += 1; }, stop() { restarted.flags.stops += 1; } } };
  };
  const initializing = restarted.consent.initialize();
  const rejected = assert.rejects(initializing, { code: 'stale_control' });
  const signal = await entered.promise;
  const stopping = restarted.consent.setMode('revoked');
  assert.equal(signal.aborted, true);
  assert.equal(restarted.consent.captureScope.isOpen, false);
  release.resolve();
  await rejected; await stopping;
  assert.equal(restarted.flags.starts, 0);
  assert.equal(restarted.consent.state.mode, 'revoked');
  assert.equal(restarted.consent.state.authorization_event_id, null);
});

test('a changed privacy notice reconciles pending choices after restart and creates current reauthorization', { timeout: 2000 }, async () => {
  const h = harness(); const first = await h.activate();
  const priorScope = authorizationScope(h.bindingRef.current, 'preview');
  h.bindingRef.current.legal_repository_revision = 'e'.repeat(40);
  assert.equal(authorizationScope(h.bindingRef.current, 'preview'), priorScope);
  await h.consent.reconcile();
  assert.equal(h.consent.state.mode, 'preview');
  h.bindingRef.current.instruments.extension_privacy_notice.rendered_sha256 = 'd'.repeat(64);
  await h.consent.reconcile();
  assert.equal(h.consent.state.mode, 'paused');
  const current = await h.legal.status();
  assert.equal(current.requires_reauthorization, true);
  assert.equal(current.flow.completed_event_id, null);
  const reopened = new LegalActivationController({ chromeApi: h.chromeApi, consentController: h.consent,
    evidenceStore: h.evidenceStore, bindings: () => h.bindingRef.current });
  const second = await reopened.chooseMode('preview');
  assert.equal(second.evidence.event_type, 'reauthorization');
  assert.notEqual(second.evidence.event_id, first.evidence.event_id);
  assert.equal((await reopened.status()).requires_reauthorization, false);
});

test('changed Terms remain usable and record terms reacceptance with the new acceptance timestamp', { timeout: 2000 }, async () => {
  const h = harness(); await h.activate();
  h.bindingRef.current.instruments.terms_of_service.rendered_sha256 = 'b'.repeat(64);
  await h.consent.reconcile();
  const status = await h.legal.status();
  assert.equal(status.flow.terms_event_id, null);
  assert.notEqual(status.flow.risk_event_id, null);
  await h.legal.acceptTerms(); await h.legal.activateSoftware();
  const accepted = await h.legal.chooseMode('preview');
  assert.equal(accepted.evidence.event_type, 'terms_reacceptance');
  assert.equal(accepted.evidence.actions.terms.action, 'accepted');
});

test('failed evidence open resets its cache and versionchange permits a fresh connection', { timeout: 2000 }, async () => {
  const h = harness();
  const open = h.indexedDb.open.bind(h.indexedDb);
  let attempts = 0;
  h.indexedDb.open = (...args) => {
    if (attempts++ > 0) return open(...args);
    const request = {};
    queueMicrotask(() => request.onerror?.());
    return request;
  };
  await assert.rejects(h.evidenceStore.exportAuditTrail(), /failed to open/);
  assert.equal(h.evidenceStore.databasePromise, null);
  await h.evidenceStore.exportAuditTrail();
  const prior = await h.evidenceStore.databasePromise;
  prior.onversionchange();
  assert.equal(h.evidenceStore.databasePromise, null);
  await h.evidenceStore.exportAuditTrail();
  assert.notEqual(await h.evidenceStore.databasePromise, prior);
});

test('idempotent Full mode retry retains its document epoch and explicit reload is the only tab reload', { timeout: 2000 }, async () => {
  const h = harness(); const first = await h.activate('full');
  const response = deferred();
  const sender = { id: 'synthetic', url: 'chrome-extension://synthetic/popup.html' };
  const handled = h.chromeApi.runtime.onMessage.listeners.some((listener) => listener(
    { type: UI_RELOAD_TABS_MESSAGE_TYPE }, sender, response.resolve,
  ));
  assert.equal(handled, true);
  assert.equal((await response.promise).status.reload_required, false);
  assert.equal(h.flags.reloads, 1);
  const retried = await h.legal.chooseMode('full');
  assert.equal(retried.status.consent.consent_epoch, first.status.consent.consent_epoch);
  assert.equal(retried.status.reload_required, false);
  assert.equal(h.flags.reloads, 1);
  await h.consent.setMode('pause');
  assert.equal(h.flags.reloads, 1);
  assert.equal(h.consent.captureScope.isOpen, false);
});

test('a frozen tab cannot hold setup status or pause behind its pending reload acknowledgement', async (t) => {
  const h = harness();
  await h.activate('full');
  t.mock.timers.enable({ apis: ['setTimeout'] });
  h.chromeApi.tabs.reload = () => { h.flags.reloads += 1; return new Promise(() => {}); };
  const response = deferred();
  const sender = { id: 'synthetic', url: 'chrome-extension://synthetic/setup.html' };
  assert.equal(h.chromeApi.runtime.onMessage.listeners.some((listener) => listener(
    { type: UI_RELOAD_TABS_MESSAGE_TYPE }, sender, response.resolve,
  )), true);
  while (h.flags.reloads === 0) await new Promise((resolve) => setImmediate(resolve));
  t.mock.timers.tick(2_000);
  assert.equal((await response.promise).status.reload_required, false);
  assert.equal((await h.consent.status()).consent.mode, 'full');
  await h.consent.setMode('pause');
  t.mock.timers.tick(60_000);
  assert.equal(h.flags.reloads, 1, 'a slow browser acknowledgement must never schedule another reload');
  assert.equal((await h.consent.status()).consent.mode, 'paused');
});

test('permission recovery retains consent and replaces stale script definitions without automatic reload', { timeout: 2000 }, async () => {
  const h = harness(); const first = await h.activate('full');
  h.flags.permission = false;
  h.chromeApi.permissions.onRemoved.listeners[0]({ origins: ['https://onlyfans.com/*'] });
  assert.equal(h.consent.captureScope.isOpen, false);
  assert.equal((await h.consent.status()).phase, 'permission_required');
  h.flags.permission = true;
  h.chromeApi.permissions.onAdded.listeners[0]({ origins: ['https://onlyfans.com/*'] });
  const restored = await h.consent.status();
  assert.equal(restored.phase, 'full');
  assert.equal(restored.consent.authorization_event_id, first.evidence.event_id);
  assert.equal(restored.consent.consent_epoch, first.status.consent.consent_epoch);
  h.scripts[0].world = 'ISOLATED';
  h.scripts[0].runAt = 'document_idle';
  await h.consent.reconcile();
  assert.equal(h.scripts[0].world, 'MAIN');
  assert.equal(h.scripts[0].runAt, 'document_start');
  assert.equal(h.flags.reloads, 0);
  assert.equal((await h.consent.status()).reload_required, true);
});

test('historical records without authorization scope cannot become current authorization', { timeout: 2000 }, async () => {
  const h = harness(); const first = await h.activate();
  const record = await h.evidenceStore.event(first.evidence.event_id);
  const legacy = { ...record, schema: 'ofca-mode-legal-evidence/v1' };
  delete legacy.authorization_scope;
  const policy = new LegalConsentAuthorization({
    evidenceStore: { async event() { return legacy; } }, bindings: () => bindings,
  });
  assert.equal(await policy.authorizeTransition({ currentState: { mode: 'off' },
    requestedMode: 'preview', evidenceEventId: legacy.event_id }), false);
  assert.equal(await policy.authorizeResume({ resumeMode: 'preview',
    currentState: { mode: 'paused', authorization_event_id: legacy.event_id } }), false);
});

async function restartedFullHarness({ paired = true } = {}) {
  const h = harness();
  await h.activate('full');
  const timers = new Map();
  let nextTimer = 0;
  const messages = [];
  let bound = false;
  h.chromeApi.tabs.sendMessage = async (tabId, message, options) => {
    messages.push({ tabId, message, options });
  };
  const restarted = new ConsentController({
    chromeApi: h.chromeApi, runtime: h.runtime, previewMetrics: h.preview,
    activeModeAuthorization: h.authorization, activationEvidenceStore: h.evidenceStore,
    provisioningIdentityBridge: { register() {}, unregister() {}, async clearContexts() {} },
    adapter: {
      async loadBrainBinding() { if (!bound) throw new Error('companion_session_refused'); return {}; },
      async clearBrainBinding() {},
    },
    hasSavedPairing: async () => paired,
    clearLocalData: async () => {}, now,
    scheduler: {
      setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
      clearTimeout(id) { timers.delete(id); },
    },
  });
  await restarted.initialize();
  const retry = async () => {
    const [id, timer] = timers.entries().next().value;
    timers.delete(id);
    await timer.callback();
    return timer.delay;
  };
  return { ...h, restarted, timers, messages, retry, bind: () => { bound = true; } };
}

test('cold Full worker preserves the live bridge and recovers via a bounded identity probe without reload', async () => {
  const h = await restartedFullHarness();
  assert.equal(h.restarted.phase, 'identity');
  assert.equal(h.restarted.captureScope.isOpen, false);
  assert.deepEqual(h.scripts.map((script) => script.id), ['ofca-full-main', 'ofca-full-isolated']);
  assert.deepEqual(h.messages, [], 'a transient refusal must not stop the document bridge');
  h.chromeApi.tabs.sendMessage = async (tabId, message, options) => {
    assert.equal(tabId, 1);
    assert.equal(message.action, 'refresh_identity');
    assert.equal(message.version, 1);
    assert.deepEqual(options, { frameId: 0 });
    h.bind();
  };
  assert.equal(await h.retry(), 500);
  assert.equal(h.restarted.phase, 'full');
  assert.equal(h.restarted.captureScope.isOpen, true);
  assert.equal(h.flags.reloads, 0);
  assert.equal(h.timers.size, 0);
});

test('identity recovery remains closed without an authenticated binding and preserves retry backoff', async () => {
  const h = await restartedFullHarness();
  assert.equal(await h.retry(), 500);
  assert.equal(await h.retry(), 1000);
  assert.equal(h.restarted.phase, 'identity');
  assert.equal(h.restarted.captureScope.isOpen, false);
  assert.equal(h.messages.length, 2);
  assert.equal(h.flags.reloads, 0);
  await h.restarted.setMode('pause');
  assert.equal(h.restarted.phase, 'paused');
  assert.equal(h.timers.size, 0);
  assert.equal(h.scripts.length, 0);
});

test('recovery skips frozen and discarded documents without bypassing their lifecycle', async () => {
  const h = await restartedFullHarness();
  h.chromeApi.tabs.query = async () => [{ id: 1, frozen: true }, { id: 2, discarded: true }];
  await h.retry();
  assert.deepEqual(h.messages, []);
  assert.equal(h.restarted.phase, 'identity');
  assert.equal(h.flags.reloads, 0);
  await h.restarted.setMode('pause');
});

test('unpaired Full consent still stops old Full scripts and requires explicit reload', async () => {
  const h = await restartedFullHarness({ paired: false });
  assert.equal(h.messages[0].message.action, 'stop');
  assert.deepEqual(h.scripts.map((script) => script.id), ['ofca-identity-main', 'ofca-identity-isolated']);
  assert.equal(h.restarted.reloadRequired, true);
  assert.equal(h.timers.size, 0);
  assert.equal(h.flags.reloads, 0);
});

test('a stalled identity reply is bounded and cannot revive Full after Pause', async () => {
  const h = await restartedFullHarness();
  h.chromeApi.tabs.sendMessage = async (_tabId, message) => {
    if (message.action === 'refresh_identity') return new Promise(() => {});
  };
  const retrying = h.retry();
  while (![...h.timers.values()].some((timer) => timer.delay === 2_000)) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const pausing = h.restarted.setMode('pause');
  h.bind();
  const timeout = [...h.timers.values()].find((timer) => timer.delay === 2_000);
  timeout.callback();
  await retrying;
  await pausing;
  assert.equal(h.restarted.phase, 'paused');
  assert.equal(h.restarted.captureScope.isOpen, false);
  assert.equal(h.timers.size, 0);
  assert.equal(h.flags.reloads, 0);
});
