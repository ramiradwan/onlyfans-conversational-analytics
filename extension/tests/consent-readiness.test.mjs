import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { build } from 'esbuild';
import { ACTIVATION_EVIDENCE_DATABASE_NAME, ACTIVATION_EVIDENCE_STORE, ActivationEvidenceStore } from '../runtime/activation-evidence.mjs';
import { CONSENT_STORAGE_KEY, ConsentController, UI_RELOAD_TABS_MESSAGE_TYPE } from '../runtime/consent-controller.mjs';
import { DELETE_INTENT_KEY } from '../runtime/deletion-state.mjs';
import { LEGAL_ACTIVATION_FLOW_STORAGE_KEY, LegalActivationController } from '../runtime/legal-activation-controller.mjs';
import { LegalConsentAuthorization, authorizationScope } from '../runtime/legal-consent-authorization.mjs';
import { clearExtensionLocalData } from '../runtime/local-data.mjs';
import { PreviewMetricsStore, PREVIEW_METRICS_STORAGE_KEY } from '../runtime/preview-metrics.mjs';
import { AgentRuntime } from '../transport/agent-runtime-core.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
import { modeChoiceAvailable, needsAgreement } from '../ui/presentation.mjs';

const bindings = JSON.parse(await readFile(new URL('./fixtures/legal-instrument-bindings.synthetic.json', import.meta.url)));
const now = () => new Date('2030-01-08T12:00:00.000Z');
const surfaceBundles = Object.fromEntries(await Promise.all(['setup', 'popup'].map(async (surface) => {
  const result = await build({ entryPoints: [fileURLToPath(new URL(`../${surface}.js`, import.meta.url))],
    bundle: true, write: false, format: 'iife' });
  return [surface, result.outputFiles[0].text];
})));

// Execute the real page, surface client, presentation and action locking without a browser.
async function renderSurface(surface, model) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { dataset: {}, disabled: false, checked: false, textContent: '',
        classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
          contains: (name) => classes.has(name) },
        setAttribute() {}, removeAttribute() {}, addEventListener() {} });
    }
    return nodes.get(id);
  };
  const messages = [];
  const chrome = { runtime: {
    getURL: (path) => `chrome-extension://synthetic/${path}`,
    connect: () => ({ onMessage: event(), onDisconnect: event(), postMessage() {}, disconnect() {} }),
    async sendMessage(message) {
      messages.push(message.type);
      if (message.type === 'ofca.ui.status') return { ok: true, status: model.status };
      if (message.type === 'ofca.legal-activation.status') return { ok: true, result: model.legal };
      throw new Error(`Unexpected surface message: ${message.type}`);
    },
  }, storage: { onChanged: event(), session: { async get() { return {}; }, async remove() {} } } };
  runInNewContext(surfaceBundles[surface], {
    chrome, URL, TextEncoder, AbortController, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    location: { hash: '' }, sessionStorage: { getItem: () => null },
    window: { addEventListener() {}, removeEventListener() {} },
    document: { getElementById: node, querySelector: node, querySelectorAll: () => [],
      addEventListener() {}, removeEventListener() {} },
    fetch: async () => ({ json: async () => ({}) }),
  });
  for (let attempt = 0; attempt < 30 && node('main').dataset.ready !== 'true'; attempt += 1) {
    await new Promise(setImmediate);
  }
  assert.equal(node('main').dataset.ready, 'true', `${surface} renders the status response`);
  assert.deepEqual(messages, ['ofca.ui.status', 'ofca.legal-activation.status']);
  return node;
}

async function losePrerequisites(h, lost) {
  await h.evidenceStore.close();
  const records = h.indexedDb.databases.get(ACTIVATION_EVIDENCE_DATABASE_NAME)
    .stores.get(ACTIVATION_EVIDENCE_STORE).records;
  for (const [key, record] of records) {
    if (record.record_type === 'pre_mode'
      && (lost === 'both' || record.legal_meaning === (lost === 'terms' ? 'terms' : 'risk_disclosure'))) {
      records.delete(key);
    }
  }
}
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
      async executeScript({ world, files }) {
        if (world === 'MAIN') flags.documentMode = files[0].match(/mode-(\w+)/u)[1];
        return [{ frameId: 0, documentId: 'document-1' }];
      },
      async getRegisteredContentScripts() { return structuredClone(scripts); },
      async registerContentScripts(next) { scripts.push(...next); },
      async unregisterContentScripts() { scripts.length = 0; },
    },
    tabs: {
      async query() { return [{ id: 1 }]; },
      async sendMessage(_id, message) {
        if (message.action === 'stop') flags.documentMode = null;
        if (message.action === 'status' && flags.documentMode) return {
          mode: flags.documentMode, active: true, forwarding: true, ws2_socket_open: false,
        };
      },
      async reload() { flags.reloads += 1; flags.documentMode = scripts[0]?.id.split('-')[1]; },
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

for (const mode of ['preview', 'full']) for (const paused of [false, true]) for (const lost of ['terms', 'risk', 'both']) {
  test(`F1 surviving ${mode}, paused=${paused}, missing=${lost}`, async () => {
    const h = harness({ indexedDb: new FakeIndexedDb() });
    const first = await h.activate(mode);
    if (paused) await h.consent.setMode('pause');
    const before = (await h.legal.status()).flow;
    await losePrerequisites(h, lost);
    const surviving = await h.evidenceStore.exportAuditTrail();
    const legal = await h.legal.status();
    const model = { legal, status: await h.consent.status() };
    assert.equal(legal.flow.stage, 'pre_mode');
    assert.equal(legal.requires_reauthorization, true, 'surviving mode evidence cannot authorize missing prerequisites');
    assert.equal(needsAgreement(model), true);
    const setup = await renderSurface('setup', model);
    assert.equal(setup('main').dataset.step, 'agree');
    assert.equal(setup('terms-accepted').disabled, lost === 'risk');
    assert.equal(setup('risk-acknowledged').disabled, lost === 'terms');
    assert.equal(setup('activate-software').disabled, false);
    assert.equal(setup('activate-software').classList.contains('hidden'), false);
    const popup = await renderSurface('popup', model);
    assert.equal(popup('journey-primary').textContent, 'Review changes');
    assert.equal(legal.flow.terms_event_id, lost === 'risk' ? before.terms_event_id : null);
    assert.equal(legal.flow.risk_event_id, lost === 'terms' ? before.risk_event_id : null);
    await h.consent.reconcile();
    assert.equal((await h.consent.status()).consent.mode, 'paused');
    assert.equal(h.consent.captureScope.isOpen, false);
    assert.equal(h.scripts.length, 2);
    assert.equal(await h.authorization.recordAuthorizes(first.evidence.event_id, mode), false);
    await assert.rejects(h.consent.setMode('resume'));
    assert.equal((await h.legal.status()).requires_reauthorization, true);
    assert.deepEqual(await h.evidenceStore.exportAuditTrail(), surviving, 'polling and reconciliation never create evidence');
    assert.ok(await h.evidenceStore.event(first.evidence.event_id), 'mode record survives');
  });
}

for (const mode of ['preview', 'full']) for (const lost of ['terms', 'risk', 'both']) {
  test(`F3 active ${mode} recovery through surfaces, missing=${lost}`, { timeout: 3000 }, async () => {
    const h = harness({ indexedDb: new FakeIndexedDb() });
    const first = await h.activate(mode);
    const original = await h.evidenceStore.event(first.evidence.event_id);
    await losePrerequisites(h, lost);
    const surviving = await h.evidenceStore.exportAuditTrail();
    const model = { legal: await h.legal.status(), status: await h.consent.status() };
    assert.equal(model.legal.consent_mode, 'paused', 'status must reconcile the unsupported active mode');
    assert.equal(model.status.consent.mode, 'paused');
    assert.equal(h.consent.captureScope.isOpen, false);
    assert.equal(h.consent.allowsFullCapture(), false);
    assert.equal(h.scripts.length, 2);
    assert.deepEqual(await h.evidenceStore.exportAuditTrail(), surviving);
    assert.equal((await renderSurface('popup', model))('journey-primary').textContent, 'Review changes');
    const setup = await renderSurface('setup', model);
    assert.equal(setup('main').dataset.step, 'agree');
    assert.equal(setup('activate-software').disabled, false);
    h.evidenceStore.now = () => new Date('2030-01-09T12:00:00.000Z');
    for (const [id, action, needed] of [
      ['terms-accepted', 'acceptTerms', lost !== 'risk'],
      ['risk-acknowledged', 'acknowledgeRisk', lost !== 'terms'],
    ]) {
      assert.equal(setup(id).disabled, !needed);
      if (needed) await h.legal[action]();
    }
    const agreed = { legal: await h.legal.status(), status: await h.consent.status() };
    assert.equal((await renderSurface('setup', agreed))('activate-software').disabled, false);
    await h.legal.activateSoftware();
    const selection = { legal: await h.legal.status(), status: await h.consent.status() };
    assert.equal(modeChoiceAvailable(selection), true);
    const choice = await renderSurface('setup', selection);
    assert.equal(choice('main').dataset.step, 'mode');
    assert.equal(choice('mode-choice').classList.contains('hidden'), false);
    assert.equal(choice('enable-full').classList.contains('hidden'), false);
    assert.equal(choice('enable-full').disabled, false);
    assert.equal(h.consent.captureScope.isOpen, false);
    const recovered = await h.legal.chooseMode('full');
    const record = await h.evidenceStore.event(recovered.evidence.event_id);
    assert.equal(recovered.status.consent.mode, 'full');
    for (const [field, action, replaced] of [
      ['terms_event_id', 'terms', lost !== 'risk'],
      ['risk_event_id', 'risk_disclosure', lost !== 'terms'],
    ]) {
      assert.equal(record[field], selection.legal.flow[field]);
      assert.equal(record[field] !== original[field], replaced);
      const prerequisite = await h.evidenceStore.event(record[field]);
      assert.equal(prerequisite.occurred_at, replaced ? '2030-01-09T12:00:00.000Z' : now().toISOString());
      assert.equal(record.envelope.actions[action].timestamp, prerequisite.occurred_at);
    }
    assert.deepEqual(await h.evidenceStore.event(original.event_id), original);
    assert.equal((await h.legal.status()).requires_reauthorization, false);
    assert.equal(h.consent.captureScope.isOpen, true);
  });
}

for (const first of ['status', 'agreement']) {
  test(`F3 status races agreement with ${first} admitted first`, { timeout: 3000 }, async () => {
    const h = harness({ indexedDb: new FakeIndexedDb() });
    await h.activate('full');
    await losePrerequisites(h, 'terms');
    const lookup = h.evidenceStore.event.bind(h.evidenceStore);
    const entered = deferred(); const release = deferred();
    let held = false;
    h.evidenceStore.event = async (id) => {
      if (!held) { held = true; entered.resolve(); await release.promise; }
      return lookup(id);
    };
    const leading = first === 'status' ? h.legal.status() : h.legal.acceptTerms();
    await entered.promise;
    const trailing = first === 'status' ? h.legal.acceptTerms() : h.legal.status();
    release.resolve();
    const results = await Promise.all([leading, trailing]);
    assert.ok(results.every((result) => result.consent_mode === 'paused'));
    assert.equal(results[0].flow.transaction_id, results[1].flow.transaction_id);
    assert.equal(h.consent.captureScope.isOpen, false);
    const current = await h.legal.status();
    assert.ok(current.flow.terms_event_id);
    assert.equal(current.requires_reauthorization, true);
    await h.legal.activateSoftware();
    assert.equal(modeChoiceAvailable({ legal: await h.legal.status(), status: await h.consent.status() }), true);
    assert.equal((await h.legal.chooseMode('full')).status.consent.mode, 'full');
    assert.equal((await h.evidenceStore.exportAuditTrail()).length, 4);
  });
}

test('F3 fallback operation queue delegates reconciliation before status returns', async () => {
  const h = harness({ indexedDb: new FakeIndexedDb() }); await h.activate('preview');
  await losePrerequisites(h, 'risk');
  const controller = new LegalActivationController({
    chromeApi: h.chromeApi, evidenceStore: h.evidenceStore, bindings: () => h.bindingRef.current,
    consentController: {
      status: () => h.consent.status(),
      setMode: (...args) => h.consent.setMode(...args),
      reconcile: () => h.consent.reconcile(),
    },
  });
  assert.equal((await controller.status()).consent_mode, 'paused');
  assert.equal(h.consent.captureScope.isOpen, false);
});

for (const site of ['flow', 'mode', 'prerequisite']) for (const persistent of [false, true]) {
  test(`F3 status ${site} read failure, persistent=${persistent}`, { timeout: 3000 }, async () => {
    const h = harness(); await h.activate('full');
    const before = structuredClone(h.local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY]);
    const audit = await h.evidenceStore.exportAuditTrail();
    const lookup = h.evidenceStore.event.bind(h.evidenceStore);
    const reconcile = h.authorization.reconcileActiveMode.bind(h.authorization);
    let reconciliations = 0; let reads = 0;
    h.authorization.reconcileActiveMode = async (options) => { reconciliations += 1; return reconcile(options); };
    const failAt = { flow: 1, mode: 3, prerequisite: 4 }[site];
    h.evidenceStore.event = async (id) => {
      reads += 1;
      if (reads === failAt || (persistent && reads > failAt)) throw new Error('F3 injected evidence read failure');
      return lookup(id);
    };
    await assert.rejects(h.legal.status(), /F3 injected evidence read failure/);
    assert.equal(reconciliations, 1, 'failed status read must hand active mode to reconciliation');
    assert.equal(h.consent.state.mode, persistent ? 'paused' : 'full');
    assert.equal(h.consent.captureScope.isOpen, !persistent, 'reconciliation owns read-failure policy');
    assert.deepEqual(h.local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY], before);
    h.evidenceStore.event = lookup;
    assert.deepEqual(await h.evidenceStore.exportAuditTrail(), audit);
    assert.equal((await h.legal.status()).requires_reauthorization, false);
    if (persistent) await h.consent.setMode('resume');
    assert.equal(h.consent.captureScope.isOpen, true);
  });
}

for (const mode of ['preview', 'full']) {
  test(`F3 all-present ${mode} status polls do not reconcile or write`, async () => {
    const h = harness(); await h.activate(mode);
    const before = structuredClone(h.local);
    const audit = await h.evidenceStore.exportAuditTrail();
    h.authorization.reconcileActiveMode = async () => assert.fail('unexpected reconciliation');
    h.consent.captureScope.close = () => assert.fail('unexpected capture closure');
    h.chromeApi.storage.local.set = async () => assert.fail('unexpected local write');
    const database = await h.evidenceStore.databasePromise;
    const transaction = database.transaction.bind(database);
    database.transaction = (stores, access) => {
      assert.equal(access, 'readonly', 'polling must not write evidence');
      return transaction(stores, access);
    };
    for (let i = 0; i < 5; i += 1) {
      const [status, legal] = await Promise.all([h.consent.status(), h.legal.status()]);
      assert.equal(status.consent.mode, mode);
      assert.equal(legal.consent_mode, mode);
      assert.equal(legal.requires_reauthorization, false);
      assert.equal(h.consent.captureScope.isOpen, true);
    }
    assert.deepEqual(h.local, before);
    assert.deepEqual(await h.evidenceStore.exportAuditTrail(), audit);
  });
}

test('F1 later Full choice uses replacement prerequisites while the prior mode record survives', async () => {
  const h = harness({ indexedDb: new FakeIndexedDb() });
  const first = await h.activate('full');
  const original = await h.evidenceStore.event(first.evidence.event_id);
  await losePrerequisites(h, 'both');
  await h.consent.reconcile();
  assert.equal((await h.legal.status()).requires_reauthorization, true);
  h.evidenceStore.now = () => new Date('2030-01-09T12:00:00.000Z');
  await h.legal.acceptTerms();
  await h.legal.acknowledgeRisk();
  const legal = await h.legal.status();
  assert.equal(legal.requires_reauthorization, true, 'replacement actions do not authorize the old mode record');
  const setup = await renderSurface('setup', { legal, status: await h.consent.status() });
  assert.equal(setup('activate-software').disabled, false);
  await h.legal.activateSoftware();
  const selection = { legal: await h.legal.status(), status: await h.consent.status() };
  assert.equal(modeChoiceAvailable(selection), true);
  assert.equal((await renderSurface('setup', selection))('main').dataset.step, 'mode');
  const recovered = await h.legal.chooseMode('full');
  const record = await h.evidenceStore.event(recovered.evidence.event_id);
  assert.notEqual(record.event_id, original.event_id);
  for (const [field, action] of [['terms_event_id', 'terms'], ['risk_event_id', 'risk_disclosure']]) {
    assert.equal(record[field], legal.flow[field]);
    assert.notEqual(record[field], original[field]);
    const replacement = await h.evidenceStore.event(record[field]);
    assert.equal(replacement.occurred_at, '2030-01-09T12:00:00.000Z');
    assert.equal(record.envelope.actions[action].timestamp, replacement.occurred_at);
  }
  assert.deepEqual(await h.evidenceStore.event(original.event_id), original);
  assert.equal((await h.legal.status()).requires_reauthorization, false);
  assert.equal(recovered.status.consent.mode, 'full');
  assert.equal(h.consent.captureScope.isOpen, true);
});

test('F1 all-present active modes keep agreement and its actions unchanged', async () => {
  for (const mode of ['preview', 'full']) {
    const h = harness();
    await h.activate(mode);
    const model = { legal: await h.legal.status(), status: await h.consent.status() };
    assert.equal(model.legal.requires_reauthorization, false);
    assert.equal(needsAgreement(model), false);
    const setup = await renderSurface('setup', model);
    assert.notEqual(setup('main').dataset.step, 'agree');
    for (const id of ['terms-accepted', 'risk-acknowledged', 'activate-software']) assert.equal(setup(id).disabled, true);
    assert.equal(setup('activate-software').classList.contains('hidden'), true);
    assert.notEqual((await renderSurface('popup', model))('journey-primary').textContent, 'Review changes');
    assert.equal(h.consent.captureScope.isOpen, true);
  }
});

for (const [prior, lost] of ['initial setup', 'paused Full'].flatMap(
  (prior) => ['terms', 'risk', 'both'].map((lost) => [prior, lost]),
)) {
  test(`setup recovery returns to agreement after losing ${lost} records during ${prior}`, async () => {
    const indexedDb = new FakeIndexedDb();
    const h = harness({ indexedDb });
    await h.legal.acceptTerms();
    await h.legal.acknowledgeRisk();
    await h.legal.activateSoftware();
    const first = prior === 'paused Full' ? await h.legal.chooseMode('full') : null;
    const before = (await h.legal.status()).flow;
    const original = await h.evidenceStore.exportAuditTrail();
    await h.evidenceStore.close();
    if (lost === 'both') {
      // Recreate an empty evidence database while extension storage survives.
      indexedDb.databases.delete(ACTIVATION_EVIDENCE_DATABASE_NAME);
    } else {
      const records = indexedDb.databases.get(ACTIVATION_EVIDENCE_DATABASE_NAME)
        .stores.get(ACTIVATION_EVIDENCE_STORE).records;
      for (const record of original) {
        if (record.record_type === 'mode_envelope'
          || record.legal_meaning === (lost === 'terms' ? 'terms' : 'risk_disclosure')) {
          records.delete(record.record_key);
        }
      }
    }
    const surviving = await h.evidenceStore.exportAuditTrail();
    await h.consent.reconcile();
    assert.equal((await h.consent.status()).consent.mode, first ? 'paused' : 'off');
    // Reopening setup must repair storage even though the binding scope is unchanged.
    const reopened = new LegalActivationController({ chromeApi: h.chromeApi, consentController: h.consent,
      evidenceStore: h.evidenceStore, bindings: () => h.bindingRef.current });
    const legal = await reopened.status();
    const model = { legal, status: await h.consent.status() };
    assert.equal(legal.requires_reauthorization, first !== null);
    assert.equal(needsAgreement(model), true, 'setup must show the Terms and Risk step');
    assert.equal(modeChoiceAvailable(model), false);
    assert.equal(legal.flow.stage, 'pre_mode');
    assert.equal(legal.flow.binding_scope, before.binding_scope);
    assert.notEqual(legal.flow.transaction_id, before.transaction_id);
    assert.equal(legal.flow.terms_event_id, lost === 'risk' ? before.terms_event_id : null);
    assert.equal(legal.flow.risk_event_id, lost === 'terms' ? before.risk_event_id : null);
    assert.equal(legal.flow.completed_mode, null);
    assert.equal(legal.flow.completed_event_id, null);
    assert.equal(legal.flow.pending_mode, null);
    assert.equal(legal.flow.pending_event_type, null);
    assert.deepEqual(h.local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY], legal.flow);
    assert.deepEqual((await reopened.status()).flow, legal.flow, 'status polling preserves the repair');
    assert.deepEqual(await h.evidenceStore.exportAuditTrail(), surviving, 'status never creates evidence');
    await assert.rejects(reopened.chooseMode('full'), /Activate Software must complete/);
    await assert.rejects(reopened.activateSoftware(), /Terms and risk actions must be completed/);

    h.evidenceStore.now = () => new Date('2030-01-09T12:00:00.000Z');
    if (legal.flow.terms_event_id === null) await reopened.acceptTerms();
    if (legal.flow.risk_event_id === null) await reopened.acknowledgeRisk();
    const accepted = await reopened.status();
    assert.equal(accepted.flow.stage, 'pre_mode', 'acceptance does not skip Activate Software');
    assert.equal(modeChoiceAvailable({ legal: accepted, status: model.status }), false);
    await reopened.activateSoftware();
    assert.equal(modeChoiceAvailable({ legal: await reopened.status(), status: model.status }), true);
    const recovered = await reopened.chooseMode('full');
    assert.equal(recovered.status.consent.mode, 'full');
    if (first) assert.notEqual(recovered.evidence.event_id, first.evidence.event_id);
    const full = await h.evidenceStore.event(recovered.evidence.event_id);
    for (const [field, action] of [['terms_event_id', 'terms'], ['risk_event_id', 'risk_disclosure']]) {
      const record = await h.evidenceStore.event(accepted.flow[field]);
      assert.equal(full[field], record.event_id);
      assert.equal(full.envelope.actions[action].timestamp, record.occurred_at);
      if (legal.flow[field] === null) {
        assert.notEqual(record.event_id, before[field]);
        assert.equal(record.occurred_at, '2030-01-09T12:00:00.000Z');
      }
    }
    for (const record of surviving) assert.deepEqual(await h.evidenceStore.event(record.event_id), record);
    assert.equal((await h.evidenceStore.exportAuditTrail()).length, 3);
  });
}

test('setup recovery preserves the flow and retry when all records are present', async () => {
  const h = harness();
  const first = await h.activate('full');
  const before = (await h.legal.status()).flow;
  const audit = await h.evidenceStore.exportAuditTrail();
  assert.deepEqual((await h.legal.status()).flow, before);
  const retry = await h.legal.chooseMode('full');
  assert.equal(retry.retried, true);
  assert.deepEqual(retry.evidence, first.evidence);
  await h.consent.setMode('pause');
  const legal = await h.legal.status();
  const model = { legal, status: await h.consent.status() };
  assert.equal(legal.requires_reauthorization, false);
  assert.equal(needsAgreement(model), false);
  assert.equal(modeChoiceAvailable(model), false);
  assert.deepEqual(legal.flow, before);
  assert.deepEqual(await h.evidenceStore.exportAuditTrail(), audit);
});

test('setup recovery preserves incomplete steps and pending Full retry state', async () => {
  const h = harness();
  const initial = await h.legal.status();
  assert.deepEqual((await h.legal.status()).flow, initial.flow);
  const terms = await h.legal.acceptTerms();
  assert.equal(terms.flow.transaction_id, initial.flow.transaction_id);
  assert.deepEqual((await h.legal.status()).flow, terms.flow);
  const risk = await h.legal.acknowledgeRisk();
  assert.equal(risk.flow.transaction_id, initial.flow.transaction_id);
  assert.equal(risk.flow.stage, 'pre_mode');
  await h.legal.activateSoftware();
  const recordModeChoice = h.evidenceStore.recordModeChoice.bind(h.evidenceStore);
  h.evidenceStore.recordModeChoice = async () => { throw new Error('synthetic interrupted write'); };
  await assert.rejects(h.legal.chooseMode('full'), /synthetic interrupted write/);
  const pending = structuredClone(h.local[LEGAL_ACTIVATION_FLOW_STORAGE_KEY]);
  assert.equal(pending.pending_mode, 'full');
  assert.deepEqual((await h.legal.status()).flow, pending);
  h.evidenceStore.recordModeChoice = recordModeChoice;
  assert.equal((await h.legal.chooseMode('full')).status.consent.mode, 'full');
});

test('setup recovery keeps the Full evidence guard when records vanish after flow validation', async () => {
  const indexedDb = new FakeIndexedDb();
  const h = harness({ indexedDb });
  await h.legal.acceptTerms();
  await h.legal.acknowledgeRisk();
  await h.legal.activateSoftware();
  const recordModeChoice = h.evidenceStore.recordModeChoice.bind(h.evidenceStore);
  h.evidenceStore.recordModeChoice = async (options) => {
    await h.evidenceStore.close();
    indexedDb.databases.delete(ACTIVATION_EVIDENCE_DATABASE_NAME);
    return recordModeChoice(options);
  };
  await assert.rejects(h.legal.chooseMode('full'), /Mode evidence requires persisted Terms and risk events/);
  assert.equal((await h.consent.status()).consent.mode, 'off');
  assert.deepEqual(await h.evidenceStore.exportAuditTrail(), []);
});

test('native legal mode choice completes without queue recursion and reactivates after same-worker deletion', { timeout: 2000 }, async () => {
  const h = harness();
  const first = await h.activate();
  assert.equal(first.status.consent.schema, 'ofca-consent/v2');
  assert.equal(first.status.consent.authorization_event_id, first.evidence.event_id);
  assert.equal(h.consent.captureScope.isOpen, true);
  assert.equal(h.flags.reloads, 0);
  assert.equal(first.status.reload_required, false);
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

test('idempotent Full mode retry retains its epoch and rejects legacy reload requests', { timeout: 2000 }, async () => {
  const h = harness(); const first = await h.activate('full');
  const response = deferred();
  const sender = { id: 'synthetic', url: 'chrome-extension://synthetic/popup.html' };
  const handled = h.chromeApi.runtime.onMessage.listeners.some((listener) => listener(
    { type: UI_RELOAD_TABS_MESSAGE_TYPE }, sender, response.resolve,
  ));
  assert.equal(handled, false);
  assert.deepEqual(await response.promise, { ok: false, code: 'reload_unsupported' });
  assert.equal(h.flags.reloads, 0);
  const retried = await h.legal.chooseMode('full');
  assert.equal(retried.status.consent.consent_epoch, first.status.consent.consent_epoch);
  assert.equal(retried.status.reload_required, false);
  assert.equal(h.flags.reloads, 0);
  await h.consent.setMode('pause');
  assert.equal(h.flags.reloads, 0);
  assert.equal(h.consent.captureScope.isOpen, false);
});

test('paused Full rejects legacy reload requests', async () => {
  const h = harness();
  await h.activate('full');
  await h.consent.setMode('pause');
  const response = deferred();
  const sender = { id: 'synthetic', url: 'chrome-extension://synthetic/popup.html' };
  h.chromeApi.runtime.onMessage.listeners.some((listener) => listener(
    { type: UI_RELOAD_TABS_MESSAGE_TYPE }, sender, response.resolve,
  ));
  await response.promise;
  assert.equal(h.flags.reloads, 0);
  assert.equal(h.consent.captureScope.isOpen, false);
});

test('Preview pause and resume require no document reload', async () => {
  const h = harness();
  await h.activate('preview');
  await h.consent.setMode('pause');
  const model = { legal: await h.legal.status(), status: await h.consent.status() };
  assert.equal(model.status.reload_required, false);
  const popup = await renderSurface('popup', model);
  assert.equal(popup('journey-primary').textContent, 'Resume analytics');
  assert.equal((await h.consent.setMode('resume')).reload_required, false);
});

test('legacy reload requests are rejected immediately even for frozen documents', async () => {
  const h = harness(); await h.activate('full');
  const response = deferred();
  h.chromeApi.tabs.reload = () => { throw Error('reload forbidden'); };
  const sender = { id: 'synthetic', url: 'chrome-extension://synthetic/setup.html' };
  assert.equal(h.chromeApi.runtime.onMessage.listeners.some((listener) => listener(
    { type: UI_RELOAD_TABS_MESSAGE_TYPE }, sender, response.resolve,
  )), false);
  assert.deepEqual(await response.promise, { ok: false, code: 'reload_unsupported' });
  await h.consent.setMode('pause');
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
  assert.equal((await h.consent.status()).reload_required, false);
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
  assert.deepEqual(h.scripts.map((script) => script.id), ['ofca-identity-main', 'ofca-identity-isolated']);
  assert.equal(h.messages.some(({ message }) => message.action === 'stop'), false, 'a transient refusal must not stop the document bridge');
  h.messages.length = 0;
  h.chromeApi.tabs.sendMessage = async (tabId, message, options) => {
    assert.equal(tabId, 1);
    if (message.action !== 'refresh_identity') return;
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
  assert.equal(h.messages.filter(({ message }) => message.action === 'refresh_identity').length, 2);
  assert.equal(h.flags.reloads, 0);
  await h.restarted.setMode('pause');
  assert.equal(h.restarted.phase, 'paused');
  assert.equal(h.timers.size, 0);
  assert.equal(h.scripts.length, 2);
});

test('recovery skips frozen and discarded documents without bypassing their lifecycle', async () => {
  const h = await restartedFullHarness();
  h.chromeApi.tabs.query = async () => [{ id: 1, frozen: true }, { id: 2, discarded: true }];
  h.messages.length = 0;
  await h.retry();
  assert.deepEqual(h.messages, []);
  assert.equal(h.restarted.phase, 'identity');
  assert.equal(h.flags.reloads, 0);
  await h.restarted.setMode('pause');
});

test('unpaired Full consent reconfigures identity observation without a reload', async () => {
  const h = await restartedFullHarness({ paired: false });
  assert.equal(h.messages.some(({ message }) => message.action === 'stop'), false);
  assert.deepEqual(h.scripts.map((script) => script.id), ['ofca-identity-main', 'ofca-identity-isolated']);
  assert.equal((await h.restarted.status()).reload_required, false);
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
