import test from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { DESKTOP_PORT_NAME, admitDesktopSender, closeFinishedHelper, desktopStage, registerDesktopPort } from '../runtime/desktop-port.mjs';
import { payload, observation, id } from '../test-fixtures/browser-task.mjs';
import { openPairingStore } from '../runtime/companion-pairing-store.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';
const ORIGIN = 'http://bridge.localhost:17871';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const event = () => { const listeners = new Set(); return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn), emit: value => { for (const fn of listeners) fn(value); } }; };
const sender = (change = {}) => ({ origin: ORIGIN, url: ORIGIN + '/browser-tasks#presentation=' + id(4), documentId: 'document-1', frameId: 0, tab: { id: 12, windowId: 4 }, ...change });
const legal = (overrides = {}) => ({ configured: true, requires_reauthorization: false, flow: { terms_event_id: 'terms', risk_event_id: 'risk', stage: 'mode_selection' }, ...overrides });
const consent = (mode, extra = {}) => ({ consent: { mode }, phase: mode === 'full' ? 'identity' : mode, reload_required: false, ...extra });
test('the stage follows the extension-owned prerequisites in order', () => {
  const stage = (value) => desktopStage(value);
  assert.equal(stage({ consent: consent('off'), legal: { configured: false } }), 'unavailable');
  assert.equal(stage({ consent: consent('off'), legal: legal({ flow: { stage: 'pre_mode' } }) }), 'needs_terms');
  assert.equal(stage({ consent: consent('full'), legal: legal({ requires_reauthorization: true }) }), 'needs_terms');
  assert.equal(stage({ consent: consent('off'), legal: legal() }), 'needs_full');
  assert.equal(stage({ consent: consent('preview'), legal: legal() }), 'needs_full');
  assert.equal(stage({ consent: consent('paused'), legal: legal() }), 'paused');
  assert.equal(stage({ consent: consent('full', { phase: 'permission_required' }), legal: legal() }), 'needs_site_access');
  assert.equal(stage({ consent: consent('full', { reload_required: true }), legal: legal() }), 'ready_to_pair');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'setup_incomplete' } }), 'needs_account');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'unpaired' } }), 'ready_to_pair');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'pairing_failed' } }), 'ready_to_pair');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'compare' } }), 'pairing');
  assert.equal(stage({ consent: consent('full'), legal: legal(), pairing: { state: 'paired' } }), 'paired');
});


function harness(t) {
  const onConnectExternal = event(), session = {}, subscribers = new Set();
  const calls = { attach: 0, reads: 0, open: 0, pair: 0, cancel: 0, auth: 0, closeDocument: 0 };
  const tabs = new Map([[12, { url: sender().url, windowId: 4 }], [13, { url: sender().url, windowId: 4 }]]);
  let clock = Date.now(), pairingOwner = null;
  const value = payload(clock), next = observation(value.task);
  const chromeApi = { runtime: { id: value.extension_id, onConnectExternal }, tabs: { async get(id) { return tabs.get(id); } },
    scripting: { async executeScript(options) { calls.closeDocument++; calls.lastClose = options;
      return [{ frameId: 0, documentId: options.target.documentIds[0], result: true }]; } },
    storage: { session: { async set(v) { Object.assign(session, v); } } } };
  const companion = { subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); }, owns: entry => entry === pairingOwner,
    cancelFor(entry) { if (pairingOwner === entry) { calls.cancel++; pairingOwner = null; } },
    async savedPairing() { return null; },
    async pairFor(entry, controls) { calls.lastPair = controls; if (!pairingOwner) { calls.pair++; pairingOwner = entry; } } };
  const owner = { async attach() { calls.attach++; return {}; }, async read() { calls.reads++; return { ...next }; }, async open() { calls.open++; } };
  const authStatus = { state: 'idle', notification: null };
  const authentication = { betweenGuards: async () => {}, subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    async read() { return { ...authStatus }; }, async launch(_payload, guard) {
      await guard(); await authentication.betweenGuards(); await guard(); calls.auth++;
      authStatus.state = 'opening';
    } };
  const portOwner = registerDesktopPort({ chromeApi, companion, owner, authentication, now: () => clock }); portOwner.register();
  t.after(() => portOwner.unregister());
  function connect(admission = sender()) {
    const port = { name: DESKTOP_PORT_NAME, sender: admission, onMessage: event(), onDisconnect: event(), sent: [], disconnected: false,
      postMessage(value) { this.sent.push(value); }, disconnect() { this.disconnected = true; this.onDisconnect.emit(); } };
    onConnectExternal.emit(port); return port;
  }
  const attach = async (port, p = value) => { port.onMessage.emit({ type: 'attach', payload: p }); await tick(); await tick(); };
  const message = (port, type) => port.onMessage.emit({ type, task_id: value.task.task_id, admission_generation: value.task.admission_generation });
  return { value, next, calls, connect, attach, message, portOwner, tabs, chromeApi, authentication, authStatus, owner, companion,
    advance: ms => { clock += ms; }, announce: value => { for (const fn of subscribers) fn(value); } };
}

function authenticateTask(h) {
  h.value.task.purpose = 'browser.authentication';
  h.value.creator_account_id = 'creator-test';
  h.value.ceremony = { entry_url: 'https://provider.example/onboarding/browser-ceremonies/' + 'A'.repeat(43),
    ceremony_id: '0199b234-5678-7000-8000-000000000001', attempt_id: 'A'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: 'https://' + h.value.extension_id + '.chromiumapp.org/browser-ceremony/v1/complete' },
    expires_at: new Date(h.value.task.expires_at * 1000).toISOString() };
  h.next.state = 'ready'; h.next.creator_account_id = h.value.creator_account_id;
}

test('authentication starts only from its explicit scoped command and pushes bounded correlation', async t => {
  const h = harness(t); authenticateTask(h);
  const port = h.connect(); await h.attach(port);
  assert.equal(h.calls.auth, 0); assert.equal(port.sent.at(-1).state, 'idle');
  h.message(port, 'authenticate'); await tick(); await tick();
  assert.equal(h.calls.auth, 1); assert.equal(port.sent.at(-1).state, 'opening');
  h.authStatus.state = 'checking';
  h.authStatus.notification = { ceremony_id: h.value.ceremony.ceremony_id,
    attempt_id: h.value.ceremony.attempt_id, notification: 'unavailable' };
  h.announce(); await tick();
  assert.deepEqual(port.sent.at(-1), { type: 'authentication', task_id: h.value.task.task_id,
    admission_generation: h.value.task.admission_generation, ...h.authStatus });
  const sent = port.sent.length;
  h.announce(); await tick(); assert.equal(port.sent.length, sent);
  h.next.revision++; h.announce(); await tick();
  assert.equal(port.sent.length, sent + 2, 'new observation retains the current authentication status');
});

test('authentication rechecks creator, permission, navigation and deadline immediately before opening', async t => {
  for (const mutate of [
    h => { h.next.creator_account_id = 'another-creator'; },
    h => { h.next.state = 'needs-permission'; h.next.creator_account_id = null; },
    h => { h.tabs.set(12, { url: 'https://onlyfans.com/', windowId: 4 }); },
    h => { h.advance(31000); },
  ]) {
    const h = harness(t); authenticateTask(h);
    const port = h.connect(); await h.attach(port);
    h.authentication.betweenGuards = async () => mutate(h);
    h.message(port, 'authenticate'); await tick(); await tick();
    assert.equal(h.calls.auth, 0); assert.equal(port.disconnected, true);
  }
});

test('an access task cannot invoke authentication even when a permitted account is present', async t => {
  const h = harness(t), port = h.connect();
  h.next.state = 'ready'; h.next.creator_account_id = 'creator-test';
  await h.attach(port); h.message(port, 'authenticate'); await tick();
  assert.equal(h.calls.auth, 0); assert.equal(port.disconnected, true);
});

test('only an exact top-level helper document is admitted', () => {
  assert.equal(admitDesktopSender(sender()), true);
  for (const change of [{ origin: 'https://evil.test' }, { url: ORIGIN + '/settings' }, { frameId: 1 }, { id: 'a'.repeat(32) },
    { documentId: undefined }, { tab: undefined }, { url: sender().url + '&extra=1' }]) assert.equal(admitDesktopSender(sender(change)), false);
});
test('reads and opens require the scoped handshake; old general commands are refused', async t => {
  const h = harness(t), port = h.connect(); await tick(); assert.equal(h.calls.reads, 0);
  port.onMessage.emit({ type: 'open', version: 1, step: 'creator' }); await tick();
  assert.equal(port.disconnected, true); assert.equal(h.calls.open, 0); assert.equal(h.calls.pair, 0);
});
test('an admitted task only opens its declared action and coalesces notifications', async t => {
  const h = harness(t), port = h.connect(); await h.attach(port);
  assert.equal(h.calls.attach, 1); assert.equal(h.calls.open, 0); assert.equal(port.sent[0].observation.state, 'needs-consent');
  const reads = h.calls.reads; for (let i = 0; i < 10; i++) h.portOwner.changed(); await tick();
  assert.equal(h.calls.reads, reads + 1); assert.equal(port.sent.length, 1);
  h.message(port, 'open'); h.message(port, 'open'); await tick(); assert.equal(h.calls.open, 1);
  h.message(port, 'pair'); await tick(); assert.equal(h.calls.pair, 0); assert.equal(port.disconnected, true);
});
test('expired, wrong extension, wrong generation and extra-field handshakes do nothing', async t => {
  const h = harness(t);
  for (const change of [{ extension_id: 'b'.repeat(32) }, { extra: true }, { task: { ...h.value.task, admission_generation: id(99) } },
    { task: { ...h.value.task, expires_at: Date.now() / 1000 - 1 } }]) {
    const port = h.connect(); await h.attach(port, { ...h.value, ...change }); assert.equal(port.disconnected, true);
  }
  assert.equal(h.calls.attach, 0); assert.equal(h.calls.pair, 0);
});
test('a second tab cannot steal an attached task and navigation fences the owner', async t => {
  const h = harness(t), first = h.connect(), second = h.connect(sender({ tab: { id: 13, windowId: 4 } }));
  await h.attach(first); await h.attach(second); assert.equal(second.disconnected, true); assert.equal(h.calls.attach, 1);
  h.tabs.set(12, { url: 'https://onlyfans.com/', windowId: 4 }); h.message(first, 'open'); await tick();
  assert.equal(first.disconnected, true); assert.equal(h.calls.open, 0);
});

test('a helper disconnected during attachment releases a late returned lease', async t => {
  const h = harness(t), port = h.connect(), lease = {};
  let finish, options, releases = 0;
  h.owner.attach = async (_payload, controls) => { options = controls; return new Promise(resolve => { finish = resolve; }); };
  h.owner.release = async context => { if (context === lease) releases++; };
  const attached = h.attach(port); await tick();
  port.disconnect(); assert.equal(options.signal.aborted, true);
  await assert.rejects(options.authorize(), /task_changed/);
  finish(lease); await attached; await tick();
  assert.equal(releases, 1); assert.equal(h.portOwner.connected, 0); assert.equal(h.calls.reads, 0);
});
test('task liveness reads no account state and cannot extend its fixed expiry', async t => {
  const h = harness(t), port = h.connect(); await h.attach(port);
  const reads = h.calls.reads;
  h.message(port, 'ping'); await tick();
  assert.deepEqual(port.sent.at(-1), { type: 'pong', task_id: h.value.task.task_id, admission_generation: h.value.task.admission_generation });
  assert.equal(h.calls.reads, reads); assert.equal(h.calls.open, 0);
  h.advance(1800001); h.message(port, 'ping'); await tick();
  assert.equal(port.sent.at(-1).reason, 'task_expired'); assert.equal(port.disconnected, true);
  assert.equal(h.calls.reads, reads);
});
test('only the pairing task sees its independent comparison code and owns cancellation', async t => {
  const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
  h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
  const first = h.connect(); await h.attach(first); h.message(first, 'pair'); await tick();
  assert.equal(h.calls.pair, 1);
  h.announce({ state: 'compare', comparison_code: '042917' });
  assert.equal(first.sent.at(-1).comparison_code, '042917');
  assert.equal(first.sent.at(-1).task_id, h.value.task.task_id);
  first.disconnect(); assert.equal(h.calls.cancel, 1);
});

test('pairing completion is an exact attempt receipt, not a global paired announcement', async t => {
  const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
  h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
  let complete;
  const gate = new Promise(resolve => { complete = resolve; }), original = h.companion.pairFor;
  h.companion.pairFor = async (...args) => { await original(...args); return gate; };
  const port = h.connect(); await h.attach(port); h.message(port, 'pair'); await tick();
  h.message(port, 'pair'); await tick();
  assert.equal(h.calls.pair, 1, 'a repeated command cannot supersede the in-flight receipt owner');
  h.announce({ state: 'paired', comparison_code: null });
  assert.equal(port.sent.some(value => value.type === 'pairing-completed'), false);
  complete(true); await tick(); await tick();
  assert.deepEqual(port.sent.filter(value => value.type === 'pairing-completed'), [{ type: 'pairing-completed',
    task_id: h.value.task.task_id, admission_generation: h.value.task.admission_generation }]);
  h.message(port, 'pair'); await tick();
  assert.equal(h.calls.pair, 1, 'this completed task cannot start another pairing attempt');
  assert.equal(port.sent.filter(value => value.type === 'pairing-completed').length, 1);
});

for (const cause of ['disconnect', 'account', 'permission', 'navigation', 'deadline', 'replacement']) {
  test(`pairing completion cannot notify a helper after ${cause}`, async t => {
    const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
    h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
    let complete;
    const gate = new Promise(resolve => { complete = resolve; }), original = h.companion.pairFor;
    h.companion.pairFor = async (...args) => { await original(...args); return gate; };
    const port = h.connect(); await h.attach(port); h.message(port, 'pair'); await tick();
    if (cause === 'disconnect') port.disconnect();
    if (cause === 'account') h.next.creator_account_id = 'another-creator';
    if (cause === 'permission') { h.next.state = 'needs-permission'; h.next.creator_account_id = null; }
    if (cause === 'navigation') h.tabs.set(12, { url: 'https://onlyfans.com/', windowId: 4 });
    if (cause === 'deadline') h.advance(31000);
    if (cause === 'replacement') {
      const nextPayload = { ...h.value, task: { ...h.value.task, admission_generation: id(88) } };
      const url = ORIGIN + '/browser-tasks#presentation=' + id(88);
      h.tabs.set(13, { url, windowId: 4 }); h.next.admission_generation = id(88);
      await h.attach(h.connect(sender({ url, documentId: 'replacement', tab: { id: 13, windowId: 4 } })), nextPayload);
    }
    complete(true); await tick(); await tick();
    assert.equal(port.sent.some(value => value.type === 'pairing-completed'), false);
    assert.equal(port.disconnected, true);
  });
}

test('a refused or failed pairing attempt emits no completion receipt', async t => {
  for (const result of [false, Error('pairing_refused')]) {
    const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
    h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
    h.companion.pairFor = async () => { if (result instanceof Error) throw result; return result; };
    const port = h.connect(); await h.attach(port); h.message(port, 'pair'); await tick(); await tick();
    assert.equal(port.sent.some(value => value.type === 'pairing-completed'), false);
  }
});

test('pairing rechecks account and browser access before opening an attempt', async t => {
  for (const state of ['needs-consent', 'needs-permission', 'needs-account', 'ready']) {
    const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
    h.value.creator_account_id = 'creator-test';
    h.next.state = state; h.next.creator_account_id = state === 'ready' ? 'another-creator' : null;
    const port = h.connect(); await h.attach(port); h.message(port, 'pair'); await tick();
    assert.equal(h.calls.pair, 0); assert.equal(port.disconnected, true);
  }
});

test('saved pairing requires an explicit task-bound replacement after current creator checks', async t => {
  const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
  h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
  h.companion.savedPairing = async () => 'opaque-current-pin';
  const port = h.connect(); await h.attach(port);
  h.message(port, 'pair'); await tick(); await tick();
  assert.equal(h.calls.pair, 0);
  assert.deepEqual(port.sent.at(-1), { type: 'pairing-saved', task_id: h.value.task.task_id,
    admission_generation: h.value.task.admission_generation });
  assert.doesNotMatch(JSON.stringify(port.sent), /opaque-current-pin/);
  h.message(port, 'replace-pairing'); await tick(); await tick();
  assert.equal(h.calls.pair, 1);
  assert.equal(h.calls.lastPair.replacement, 'opaque-current-pin');
  h.next.creator_account_id = 'another-creator';
  await assert.rejects(h.calls.lastPair.authorize(), /task_changed/);
});

test('replacement without a prior saved-pin presentation and a stale replacement cannot pair', async t => {
  for (const cause of ['unpresented', 'account', 'consent', 'deadline']) {
    const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
    h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
    h.companion.savedPairing = async () => 'opaque-current-pin';
    const port = h.connect(); await h.attach(port);
    if (cause !== 'unpresented') { h.message(port, 'pair'); await tick(); await tick(); }
    if (cause === 'account') h.next.creator_account_id = 'other';
    if (cause === 'consent') { h.next.state = 'needs-consent'; h.next.creator_account_id = null; }
    if (cause === 'deadline') h.advance(3_600_000);
    h.message(port, 'replace-pairing'); await tick(); await tick();
    assert.equal(h.calls.pair, 0, cause);
    assert.equal(port.disconnected, true, cause);
  }
});

test('delayed replacement transaction cannot forget a pin after the task deadline', async t => {
  const indexedDB = new FakeIndexedDb(), store = await openPairingStore({ indexedDB, name: 'desktop-replacement-expiry' });
  t.after(() => store.close());
  const root = indexedDB.databases.get('desktop-replacement-expiry').stores.get('state').records.get('root');
  root.pin = { pairingId: 'saved-pin', identity: { installation_id: 'saved-installation' } };
  const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
  h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
  h.companion.savedPairing = () => store.savedPairing();
  const port = h.connect(); await h.attach(port);
  h.message(port, 'pair'); await tick(); await tick();
  h.message(port, 'replace-pairing'); await tick(); await tick();
  const controls = h.calls.lastPair;
  await controls.authorize();
  controls.assertCurrent();
  const mutation = store.forget({ expectedToken: controls.replacement, signal: controls.signal,
    assertCurrent: controls.assertCurrent });
  // Expire between transaction creation and its queued read callback; no timer fires.
  h.advance(31000);
  await assert.rejects(mutation);
  assert.equal((await store.status()).paired, true);
  assert.equal((await store.status()).identity.installation_id, 'saved-installation');
});

test('an owner check rechecks the deadline after the asynchronous tab lookup', async t => {
  const h = harness(t), port = h.connect(); await h.attach(port);
  const get = h.chromeApi.tabs.get;
  h.chromeApi.tabs.get = async id => { const tab = await get(id); h.advance(31000); return tab; };
  h.message(port, 'ping'); await tick(); await tick();
  assert.equal(port.sent.at(-1).reason, 'task_expired');
  assert.equal(port.sent.some(value => value.type === 'pong'), false);
});

test('a fresh generation fences the prior helper without changing the operation', async t => {
  const h = harness(t), first = h.connect(); await h.attach(first);
  const replacement = { ...h.value, task: { ...h.value.task, admission_generation: id(88) } };
  const url = ORIGIN + '/browser-tasks#presentation=' + id(88);
  h.tabs.set(13, { url, windowId: 4 });
  const next = h.connect(sender({ url, documentId: 'replacement', tab: { id: 13, windowId: 4 } }));
  h.next.admission_generation = id(88);
  await h.attach(next, replacement);
  assert.equal(first.disconnected, true); assert.equal(next.disconnected, false);
  assert.equal(h.portOwner.connected, 1); assert.equal(h.calls.pair, 0);
  h.message(first, 'open'); await tick(); assert.equal(h.calls.open, 0);
});

test('finish closes only the exact admitted helper document and acknowledges presentation cleanup', async t => {
  const h = harness(t), port = h.connect(); await h.attach(port);
  const reads = h.calls.reads;
  h.message(port, 'finish'); await tick(); await tick();
  assert.equal(h.calls.closeDocument, 1); assert.equal(h.calls.reads, reads);
  assert.deepEqual(h.calls.lastClose.target, { tabId: 12, documentIds: ['document-1'] });
  assert.equal(h.calls.lastClose.world, 'ISOLATED');
  assert.equal(h.calls.lastClose.func, closeFinishedHelper);
  assert.deepEqual(h.calls.lastClose.args, [h.value.task.admission_generation, h.value.task.expires_at]);
  assert.deepEqual(port.sent.at(-1), { type: 'finished', task_id: h.value.task.task_id,
    admission_generation: h.value.task.admission_generation });
  assert.equal(port.disconnected, true); assert.equal(h.calls.open, 0);
});

for (const cause of ['task', 'generation', 'extra', 'expiry', 'navigation', 'pending-navigation', 'replaced-document']) {
  test(`finish refuses ${cause} without closing another document`, async t => {
    const h = harness(t), port = h.connect(); await h.attach(port);
    const message = { type: 'finish', task_id: h.value.task.task_id, admission_generation: h.value.task.admission_generation };
    if (cause === 'task') message.task_id = id(89);
    if (cause === 'generation') message.admission_generation = id(88);
    if (cause === 'extra') message.url = sender().url;
    if (cause === 'expiry') h.advance(31000);
    if (cause === 'navigation') h.tabs.set(12, { url: 'https://onlyfans.com/', windowId: 4 });
    if (cause === 'pending-navigation') h.tabs.set(12, { url: sender().url, pendingUrl: 'https://onlyfans.com/', windowId: 4 });
    if (cause === 'replaced-document') h.chromeApi.scripting.executeScript = async options => {
      assert.deepEqual(options.target, { tabId: 12, documentIds: ['document-1'] });
      // Chrome refuses this document-targeted injection after document replacement.
      throw Error('No document with id document-1');
    };
    port.onMessage.emit(message); await tick(); await tick();
    assert.equal(h.calls.closeDocument, 0); assert.equal(port.disconnected, true);
    assert.equal(port.sent.some(value => value.type === 'finished'), false);
  });
}

test('finish cannot terminate an in-flight pairing before the authenticated completion receipt', async t => {
  const h = harness(t); h.value.task.purpose = 'browser.pairing-confirmation';
  h.value.creator_account_id = h.next.creator_account_id = 'creator-test'; h.next.state = 'ready';
  const port = h.connect(); await h.attach(port); h.message(port, 'pair'); await tick();
  h.message(port, 'finish'); await tick(); await tick();
  assert.equal(h.calls.closeDocument, 0); assert.equal(h.calls.cancel, 1);
  assert.equal(port.sent.some(value => value.type === 'finished'), false);
});

test('the injected closure rechecks the top-level exact URL and original expiry at execution', () => {
  const generation = id(4), expiresAt = 1800000000;
  for (const cause of ['valid', 'origin', 'path', 'generation', 'query', 'iframe', 'expiry', 'invalid-generation', 'invalid-expiry']) {
    let closed = 0;
    const window = { location: { href: ORIGIN + '/browser-tasks#presentation=' + generation }, close() { closed++; } };
    window.top = cause === 'iframe' ? {} : window;
    if (cause === 'origin') window.location.href = 'https://provider.example/browser-tasks#presentation=' + generation;
    if (cause === 'path') window.location.href = ORIGIN + '/settings#presentation=' + generation;
    if (cause === 'generation') window.location.href = ORIGIN + '/browser-tasks#presentation=' + id(88);
    if (cause === 'query') window.location.href = ORIGIN + '/browser-tasks?other=1#presentation=' + generation;
    const result = runInNewContext('(' + closeFinishedHelper.toString() + ')(generation, expiresAt)', { window,
      generation: cause === 'invalid-generation' ? 'invalid' : generation,
      expiresAt: cause === 'invalid-expiry' ? NaN : expiresAt,
      Date: { now: () => (expiresAt - (cause === 'expiry' ? 0 : 1)) * 1000 } });
    assert.equal(result, cause === 'valid', cause); assert.equal(closed, cause === 'valid' ? 1 : 0, cause);
  }
});
