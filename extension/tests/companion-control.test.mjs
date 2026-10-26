import test from 'node:test';
import assert from 'node:assert/strict';

import { createCompanionClient, PAIRING_PORT_NAME } from '../runtime/companion-client.mjs';
import { applyControl, browserSurface, createSurfaceReporter } from '../runtime/browser-surface.mjs';
import { fixturePrivateJwk, vector } from '../test-fixtures/pairing/vendored-vector.mjs';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const ACCOUNT = vector.detected_account_id;
const STORAGE_KEY = Buffer.alloc(32, 7).toString('base64');
const key = await crypto.subtle.importKey('jwk', fixturePrivateJwk(vector.fixture_labels.agent_identity_key, vector.request.agent_identity_jwk), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
const SURFACE = { schema: 'ofca-browser-surface/v2', capture: 'paused', site_access: 'granted', history_permission: 'missing', legal_review_required: false, history_error_code: null };

function event() { const listeners = []; return { listeners, addListener(fn) { listeners.push(fn); }, removeListener(fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); } }; }
function area() { const values = {}; return { values, async get(keys) { return Object.fromEntries(keys.filter((k) => Object.hasOwn(values, k)).map((k) => [k, values[k]])); }, async set(update) { Object.assign(values, structuredClone(update)); }, async remove(keys) { for (const k of keys) delete values[k]; } }; }

function harness({ mode = 'paused', onBrowserTask = null, beforeRpc = null } = {}) {
  let consent = mode, paired = true, time = 1_800_000_000_000;
  const timers = new Set();
  const scheduler = {
    setTimeout(handler, delay) { const timer = { handler, due: time + delay }; timers.add(timer); return timer; },
    clearTimeout(timer) { timers.delete(timer); },
  };
  const advance = (ms) => { time += ms; for (const timer of [...timers]) if (timer.due <= time) { timers.delete(timer); timer.handler(); } };
  const chrome = { runtime: { id: 'a'.repeat(32), getURL: (path) => `chrome-extension://${'a'.repeat(32)}/${path}`, onConnect: event(), onStartup: event(), onInstalled: event(), onMessage: event() }, storage: { local: area(), session: area() }, tabs: { onUpdated: event() }, alarms: { onAlarm: event(), async create() {} } };
  const channels = [], controls = [], stats = { forget: 0, revoked: 0 };
  const pairingStore = {
    async identity() { return { privateKey: key }; },
    async status() { return { paired, identity: paired ? { creator_account_id: ACCOUNT } : null }; },
    async forget() { paired = false; stats.forget++; }, async cancel() {}, close() {},
  };
  const client = createCompanionClient({
    chromeApi: chrome, now: () => time, scheduler, random: () => 0.5,
    allowsFull: () => consent === 'full',
    allowsControl: () => consent === 'paused',
    onControl: async (action) => { controls.push(action); },
    onBrowserTask,
    detectedAccountId: async () => ACCOUNT,
    accountDatabaseName: async (id) => `encrypted-account-${id}`,
    storeFactory: async () => pairingStore,
    loadSnow: async () => ({ SnowSession: class {} }),
    loadTrust: async () => ({}),
    channelFactory: async (options) => {
      const closeListeners = [], controlListeners = [], rpcCalls = [];
      const channel = {
        identity: { ...vector.expected.identity, pairing_id: vector.offer.pairing_id }, closed: false, rpcCalls, options,
        close() { if (this.closed) return; this.closed = true; for (const fn of [...closeListeners]) fn(); },
        onClose(fn) { closeListeners.push(fn); return () => closeListeners.splice(closeListeners.indexOf(fn), 1); },
        onMessage() { return () => {}; },
        onControl(fn) { controlListeners.push(fn); return () => {}; },
        control(value) { for (const fn of controlListeners) fn(value); },
        async rpc(method, params) {
          rpcCalls.push({ method, params });
          if (beforeRpc) await beforeRpc(method, params, channel);
          if (method === 'agent.challenge') return { challenge_id: crypto.randomUUID(), challenge: Buffer.alloc(32, 9).toString('base64url'), session_id: crypto.randomUUID(), expires_at: '2026-09-12T12:00:00Z' };
          if (method === 'agent.authenticate') return { creator_account_id: ACCOUNT, auth_ticket: 'ticket', storage_bootstrap: 'sealed-bootstrap' };
          if (method === 'agent.storage.unseal') return { schema: 'ofca-extension-storage-unlock/v1', creator_account_id: ACCOUNT, credential_kind: 'pairing', auth_ticket: 'ticket', storage_key_base64: STORAGE_KEY };
          if (['agent.surface.report', 'agent.browser-task.report'].includes(method)) return {};
          throw new Error('test_method_missing');
        },
      };
      channels.push(channel);
      options.signal.addEventListener('abort', () => channel.close(), { once: true });
      return channel;
    },
  });
  client.onRevoked(async () => { stats.revoked++; });
  return { client, chrome, channels, controls, stats, advance, setMode(value) { consent = value; } };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function delayedSurfaceHarness({ delayAuthentication = false } = {}) {
  const authenticationStarted = deferred(), authentication = deferred(), reports = [];
  let nextReport = deferred();
  const h = harness({ beforeRpc: async (method, params, channel) => {
    if (method === 'agent.authenticate') {
      authenticationStarted.resolve();
      if (delayAuthentication) await authentication.promise;
    }
    if (method === 'agent.surface.report') {
      const report = { ...deferred(), params, channel };
      reports.push(report);
      nextReport.resolve(report);
      nextReport = deferred();
      await report.promise;
    }
  } });
  return { ...h, authenticationStarted, authentication, reports,
    report(index) {
      if (reports[index]) return Promise.resolve(reports[index]);
      assert.equal(index, reports.length);
      return nextReport.promise;
    } };
}

test('a paused Full extension holds one authenticated control-only session', async () => {
  const h = harness();
  h.client.reportSurface(SURFACE);
  await h.client.ensureControl();
  assert.equal(h.channels.length, 1);
  assert.equal(h.channels[0].options.accountId, ACCOUNT, 'the pinned account, not a page detection');
  assert.deepEqual(h.channels[0].rpcCalls.map((call) => call.method), ['agent.challenge', 'agent.authenticate', 'agent.surface.report']);
  assert.ok(!h.channels[0].rpcCalls.some((call) => call.method === 'agent.storage.unseal'), 'no storage key is unlocked');
  assert.deepEqual(h.channels[0].rpcCalls[2].params, SURFACE);
  assert.equal(h.client.controlReady(), true);
  await h.client.ensureControl();
  assert.equal(h.channels.length, 1, 'repeated signals reuse the session');
  h.setMode('off');
  await h.client.ensureControl();
  assert.equal(h.channels[0].closed, true);
  assert.equal(h.client.controlReady(), false);
});

test('paused tasks stay on the pinned control channel and reconnect never repeats them', async () => {
  const deliveries = [];
  const h = harness({ onBrowserTask: async (payload, controls) => {
    deliveries.push({ payload, controls });
    await controls.notify({ state: 'opening', notification: null });
  } });
  await h.client.ensureControl();
  const channel = h.channels[0], task = { task_id: crypto.randomUUID(), admission_generation: crypto.randomUUID() };
  channel.control({ id: task.task_id, action: 'browser.task', payload: { creator_account_id: 'other', task } });
  await tick();
  assert.equal(deliveries.length, 0);
  channel.control({ id: task.task_id, action: 'browser.task', payload: { creator_account_id: ACCOUNT, task } });
  await tick();
  assert.equal(deliveries.length, 1);
  assert.deepEqual(channel.rpcCalls.at(-1), { method: 'agent.browser-task.report', params: { ...task, state: 'opening', notification: null } });
  assert.ok(!channel.rpcCalls.some(call => call.method === 'agent.storage.unseal'));
  channel.close(); await tick();
  assert.equal(deliveries[0].controls.signal.aborted, true);
  await assert.rejects(deliveries[0].controls.notify({ state: 'opening', notification: null }));
  h.advance(1000); await tick(); await tick(); await tick();
  assert.equal(h.channels.length, 2);
  assert.equal(deliveries.length, 1);
  h.setMode('off');
  h.channels[1].control({ id: task.task_id, action: 'browser.task', payload: { creator_account_id: ACCOUNT, task } });
  await tick();
  assert.equal(deliveries.length, 1);
  await h.client.ensureControl();
});

test('only paired extensions in paused Full open a control session', async () => {
  for (const mode of ['full', 'off', 'preview']) {
    const h = harness({ mode });
    await h.client.ensureControl();
    assert.equal(h.channels.length, 0, mode);
  }
  const unpaired = harness();
  await unpaired.client.adapter.clearBrainBinding().catch(() => undefined);
  await unpaired.client.ensureControl();
  assert.equal(unpaired.channels.filter((channel) => !channel.closed).length, 0);
});

test('a closed control session reconnects through the shared recovery budget', async () => {
  const h = harness();
  await h.client.ensureControl();
  h.channels[0].close();
  await tick();
  assert.equal(h.channels.length, 1, 'no immediate reconnect after a short session');
  h.advance(1_000);
  await tick(); await tick(); await tick();
  assert.equal(h.channels.length, 2);
  assert.ok(h.chrome.storage.local.values.companion_recovery_v1.attempts >= 1);
});

test('controls are applied once through the consent callback and revocation waits for Brain to close', async () => {
  const h = harness();
  await h.client.ensureControl();
  const channel = h.channels[0];
  const id = crypto.randomUUID();
  channel.control({ id, action: 'capture.resume' });
  channel.control({ id, action: 'capture.resume' });
  await tick();
  assert.deepEqual(h.controls, ['capture.resume']);

  channel.control({ id: crypto.randomUUID(), action: 'companion.revoked' });
  h.advance(3_000); await tick();
  assert.equal(h.stats.forget, 0, 'a revocation that did not close the session keeps the pin');

  const next = harness();
  await next.client.ensureControl();
  next.channels[0].control({ id: crypto.randomUUID(), action: 'companion.revoked' });
  await tick();
  next.channels[0].close();
  await tick(); await tick();
  assert.equal(next.stats.forget, 1);
  assert.equal(next.stats.revoked, 1);
});

test('surface pages learn when the desktop app can control this browser', async () => {
  const h = harness();
  h.client.reportSurface(SURFACE);
  const states = [];
  h.client.registerPopup();
  const port = { name: PAIRING_PORT_NAME, sender: { id: h.chrome.runtime.id, url: h.chrome.runtime.getURL('popup.html'), frameId: 0 },
    onMessage: event(), onDisconnect: event(), postMessage(value) { states.push(value); } };
  h.chrome.runtime.onConnect.listeners[0](port);
  await tick();
  assert.equal(states.at(-1).desktop_control, false);
  await h.client.ensureControl();
  await tick();
  assert.ok(states.some((value) => value.type === 'surface_changed'));
  port.onMessage.listeners[0]({ type: 'status' });
  await tick(); await tick();
  assert.equal(states.at(-1).desktop_control, true);
});

test('desktop control waits for current authentication and surface acknowledgment', async () => {
  const h = delayedSurfaceHarness({ delayAuthentication: true }), states = [], readiness = [];
  h.client.registerPopup();
  const port = { name: PAIRING_PORT_NAME, sender: { id: h.chrome.runtime.id, url: h.chrome.runtime.getURL('popup.html'), frameId: 0 },
    onMessage: event(), onDisconnect: event(), postMessage(value) { states.push(value); } };
  h.chrome.runtime.onConnect.listeners[0](port);
  h.client.subscribe(() => readiness.push(h.client.controlReady()));
  h.client.reportSurface(SURFACE);
  const opening = h.client.ensureControl();
  await h.authenticationStarted.promise;
  assert.equal(h.client.controlReady(), false);
  h.authentication.resolve();
  const report = await h.report(0);
  port.onMessage.listeners[0]({ type: 'status' });
  await tick();
  assert.equal(h.client.controlReady(), false);
  assert.equal(states.at(-1).desktop_control, false);
  report.resolve();
  await opening;
  assert.equal(h.client.controlReady(), true);
  assert.equal(readiness.at(-1), true);
  port.onMessage.listeners[0]({ type: 'status' });
  await tick();
  assert.equal(states.at(-1).desktop_control, true);
  h.client.reportSurface({ ...SURFACE });
  assert.equal(h.reports.length, 1, 'unchanged meaning retains its acknowledgment');
  h.setMode('off');
  await h.client.ensureControl();
  assert.equal(h.client.controlReady(), false);
  assert.equal(readiness.at(-1), false);
  assert.ok(!h.channels[0].rpcCalls.some(call => call.method === 'agent.storage.unseal'));
});

test('changed surface meaning fences stale acknowledgments and failed reports', async () => {
  const h = delayedSurfaceHarness(), readiness = [];
  h.client.subscribe(() => readiness.push(h.client.controlReady()));
  h.client.reportSurface(SURFACE);
  const opening = h.client.ensureControl(), first = await h.report(0);
  const changed = { ...SURFACE, history_permission: 'granted' };
  h.client.reportSurface(changed);
  const second = await h.report(1);
  h.client.reportSurface(SURFACE);
  const current = await h.report(2);
  first.resolve();
  await opening;
  assert.equal(h.client.controlReady(), false, 'an earlier identical surface cannot acknowledge the current report');
  second.resolve();
  await tick();
  assert.equal(h.client.controlReady(), false);
  current.resolve();
  await tick();
  assert.equal(h.client.controlReady(), true);
  h.client.reportSurface(changed);
  const failed = await h.report(3);
  assert.equal(h.client.controlReady(), false);
  assert.equal(readiness.at(-1), false);
  failed.reject(new Error('report_failed'));
  await tick();
  assert.equal(h.client.controlReady(), false);
  h.client.reportSurface(changed);
  const retry = await h.report(4);
  retry.resolve();
  await tick();
  assert.equal(h.client.controlReady(), true);
  h.client.reportSurface(SURFACE);
  const oldFailure = await h.report(5);
  h.client.reportSurface(changed);
  const latest = await h.report(6);
  latest.resolve();
  await tick();
  oldFailure.reject(new Error('old_report_failed'));
  await tick();
  assert.equal(h.client.controlReady(), true, 'an old failure cannot clear the current acknowledgment');
  assert.ok(!h.channels[0].rpcCalls.some(call => call.method === 'agent.storage.unseal'));
  h.setMode('off');
  await h.client.ensureControl();
});

test('old channel replies cannot acknowledge or clear a replacement control channel', async () => {
  const h = delayedSurfaceHarness();
  h.client.reportSurface(SURFACE);
  const opening = h.client.ensureControl(), first = await h.report(0);
  first.resolve();
  await opening;
  h.client.reportSurface({ ...SURFACE, history_permission: 'granted' });
  const oldSuccess = await h.report(1);
  h.client.reportSurface({ ...SURFACE, history_permission: 'granted' });
  const oldFailure = await h.report(2);
  h.channels[0].close();
  assert.equal(h.client.controlReady(), false);
  await tick();
  h.advance(1_000);
  const replacement = await h.report(3);
  assert.notEqual(replacement.channel, oldSuccess.channel);
  oldSuccess.resolve();
  await tick();
  assert.equal(h.client.controlReady(), false);
  replacement.resolve();
  await tick();
  assert.equal(h.client.controlReady(), true);
  oldFailure.reject(new Error('old_channel_failed'));
  await tick();
  assert.equal(h.client.controlReady(), true);
  h.channels[1].close();
  assert.equal(h.client.controlReady(), false);
  assert.ok(!h.channels.flatMap(channel => channel.rpcCalls).some(call => call.method === 'agent.storage.unseal'));
  h.setMode('off');
  await h.client.ensureControl();
});

test('the reported browser state is closed and identifier-free', () => {
  const base = { consent: { mode: 'full' }, phase: 'full', onlyfans_permission: true, reload_required: false, history_permission: true };
  assert.deepEqual(browserSurface({ consent: base, legal: { requires_reauthorization: false } }), {
    schema: 'ofca-browser-surface/v2', capture: 'active', site_access: 'granted', history_permission: 'granted', legal_review_required: false, history_error_code: null,
  });
  assert.equal(browserSurface({ consent: { ...base, consent: { mode: 'paused', resume_mode: 'full' } } }).capture, 'paused');
  assert.equal(browserSurface({ consent: { ...base, consent: { mode: 'paused', resume_mode: 'preview' } } }).capture, 'off');
  assert.equal(browserSurface({ consent: { ...base, phase: 'permission_required' } }).site_access, 'needs_approval');
  assert.equal(browserSurface({ consent: { ...base, reload_required: true } }).site_access, 'granted');
  assert.equal(browserSurface({ consent: { ...base, history_permission: false } }).history_permission, 'missing');
  assert.equal(browserSurface({ consent: base, legal: { requires_reauthorization: true } }).legal_review_required, true);
  assert.equal(browserSurface({ consent: base, historyErrorCode: 'unsupported_revision' }).history_error_code, 'unsupported_revision');
  assert.equal(browserSurface({ consent: base, historyErrorCode: 'raw signing header must stay private' }).history_error_code, null);
});

test('controls map only to pause and resume in the matching consent state', async () => {
  const calls = [];
  const controller = (mode) => ({ state: { mode }, setMode: async (value) => { calls.push(value); } });
  await applyControl(controller('full'), 'capture.pause');
  await applyControl(controller('paused'), 'capture.resume');
  await applyControl(controller('paused'), 'capture.pause');
  await applyControl(controller('full'), 'capture.resume');
  await applyControl(controller('full'), 'companion.revoked');
  assert.deepEqual(calls, ['pause', 'resume']);
});

test('the reporter reacts to companion session changes and coalesces Full-family state reads', async () => {
  let relevant = false, reads = 0, ensured = 0, sessionChanged = null;
  const reported = [];
  const reporter = createSurfaceReporter({
    companion: {
      reportSurface: (value) => reported.push(value),
      ensureControl: async () => { ensured += 1; },
      subscribe: (listener) => { sessionChanged = listener; return () => {}; },
    },
    readState: async () => { reads += 1; return { consent: { consent: { mode: 'full' }, phase: 'full' }, legal: {} }; },
    relevant: () => relevant,
  });
  reporter.changed();
  await tick();
  assert.equal(reads, 0);
  assert.equal(ensured, 1);
  relevant = true;
  sessionChanged(); sessionChanged(); sessionChanged();
  await tick();
  assert.equal(reads, 1);
  assert.equal(reported.length, 1);
});
