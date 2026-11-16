import assert from 'node:assert/strict';
import test from 'node:test';
import { BrowserTaskClient } from '../../frontend/browser-tasks/client.js';
import { connectExtension, openWaiting } from '../../frontend/browser-tasks/extension.js';
import { createBrowserAuth } from '../runtime/browser-auth.mjs';
import { DESKTOP_PORT_NAME, registerDesktopPort } from '../runtime/desktop-port.mjs';
import { tabChangeSource } from '../runtime/tab-change-source.mjs';
import { payload, observation } from '../test-fixtures/browser-task.mjs';
import { identityNavigation } from '../test-fixtures/identity-navigation.mjs';

const ORIGIN = 'https://provider.example';
const event = () => { const listeners = new Set(); return {
  addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
  emit: value => { for (const fn of listeners) fn(value); },
}; };
const area = () => { const values = {}; return {
  async get(key) { return key === undefined ? { ...values } : { [key]: values[key] }; },
  async set(update) { Object.assign(values, update); }, async remove(key) { delete values[key]; },
}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate, limit = 5000) {
  const deadline = performance.now() + limit;
  while (!predicate() && performance.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  return predicate();
}

// Real helper, extension port and sign-in popup owner. Only Chrome and Brain's HTTP replies are simulated.
function harness(t, { preparingReads, outcome = 'ready', nativeLaunch = true, prepared = false, stages, changeSources = [], onCreate = () => {},
  extensionState = 'ready', creatorNavigation = false }) {
  const ready = payload();
  Object.assign(ready.task, { purpose: 'browser.authentication', state: 'pending', expires_at: Math.floor(Date.now() / 1000) + 1790 });
  ready.creator_account_id = 'synthetic-creator';
  ready.ceremony = { entry_url: `${ORIGIN}/onboarding/browser-ceremonies/${'A'.repeat(43)}`,
    ceremony_id: '0199b234-5678-7000-8000-000000000002', attempt_id: 'Q'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: `https://${ready.extension_id}.chromiumapp.org/browser-ceremony/v1/complete` },
    expires_at: new Date(Date.now() + 600000).toISOString() };
  const preparing = { ...ready, ceremony: null };
  const cancelled = { ...preparing, task: { ...preparing.task, state: 'cancelled' } };
  const observed = { ...observation(ready.task), state: extensionState,
    creator_account_id: ['ready', 'paused'].includes(extensionState) ? 'synthetic-creator' : null };
  const url = 'http://bridge.localhost:17871/browser-tasks#presentation=' + ready.task.admission_generation;
  const created = [], updated = [], removed = [];
  const chromeApi = {
    runtime: { id: ready.extension_id, onConnectExternal: event(), getURL: path => `chrome-extension://${ready.extension_id}/${path}` },
    identity: { getRedirectURL: path => `https://${ready.extension_id}.chromiumapp.org/${path}` },
    storage: { local: area(), session: area() },
    alarms: { onAlarm: event(), async create() {}, async clear() {} },
    windows: { onRemoved: event(), async create(options) { created.push(options); onCreate(options); return { id: 40 + created.length, tabs: [{ id: 80 }] }; },
      async remove(id) { removed.push(id); }, async update() {} },
    tabs: { async get() { return { url, windowId: 4 }; }, async update(id, options) { updated.push([id, options]); calls.push('popup'); } },
    scripting: { async executeScript() { return [{ frameId: 0, documentId: 'document-1', result: true }]; } },
  };
  const navigation = creatorNavigation ? identityNavigation(chromeApi) : null;
  const authentication = createBrowserAuth({ chromeApi, hostedOrigin: ORIGIN, onStage: (id, phase) => stages?.mark(id, phase) });
  const calls = [];
  const owner = navigation?.owner ?? { async attach() { calls.push('attach'); return {}; }, async read() { calls.push('read'); return { ...observed }; },
    async release(context) { if (context) calls.push('release'); },
    ...(prepared ? { async prepare() { calls.push('prepare'); return { prepared: true }; },
      async attachPrepared(value) { calls.push('attachPrepared'); return value; },
      async forget() { calls.push('forget'); } } : {}) };
  const companion = { subscribe: () => () => {}, owns: () => false, cancelFor() {} };
  const ports = registerDesktopPort({ chromeApi, owner, companion, authentication, changeSources, ...(stages ? { stages } : {}) });
  ports.register();
  const runtime = { connect() {
    connects++;
    let extensionPort;
    const helperPort = { onMessage: event(), onDisconnect: event(), disconnect() { extensionPort.onDisconnect.emit(); },
      postMessage: message => extensionPort.onMessage.emit(message) };
    extensionPort = { name: DESKTOP_PORT_NAME, sender: { origin: 'http://bridge.localhost:17871', url, documentId: 'document-1', frameId: 0,
      tab: { id: 12, windowId: 4 } }, onMessage: event(), onDisconnect: event(), disconnect() { helperPort.onDisconnect.emit(); },
      postMessage: message => { frames.push(message); helperPort.onMessage.emit(message); } };
    chromeApi.runtime.onConnectExternal.emit(extensionPort);
    return helperPort;
  } };
  let reads = 0, connects = 0;
  const presentations = [], timings = [], ended = [], frames = [];
  const helper = new BrowserTaskClient({ generation: ready.task.admission_generation, nativeLaunch,
    receive: value => presentations.push(value.state), connect: options => connectExtension({ ...options, runtime }),
    wait: options => openWaiting({ ...options, runtime }),
    events: async () => new Promise(() => {}),
    request: async (path, options = {}) => {
      let response;
      if (path.endsWith('/timing')) timings.push(JSON.parse(options.body));
      if (path.endsWith('/ended')) ended.push(JSON.parse(options.body));
      if (path.endsWith('/session')) response = { role: 'browser_task', generation: ready.task.admission_generation,
        csrf_token: 'A'.repeat(43), task_id: ready.task.task_id, journey_id: ready.task.journey_id };
      else if (path.endsWith('/payload')) response = reads++ < preparingReads ? preparing : outcome === 'ready' ? ready : cancelled;
      else response = { state: 'recorded' };
      return { ok: true, text: async () => JSON.stringify(response) };
    } });
  t.after(async () => { helper.stop(); ports.unregister?.(); navigation?.identity.unregister(); await tick(); });
  return { helper, created, updated, removed, presentations, timings, ended, ready, calls, ports, chromeApi, navigation, owner, frames,
    get reads() { return reads; }, get connects() { return connects; } };
}

for (const order of ['reset-first', 'url-first']) test(`step-1 participant survives navigation through desktop sign-in (${order})`, async t => {
  const marks = [];
  const h = harness(t, { preparingReads: 0, creatorNavigation: true,
    stages: { begin() {}, mark: (id, phase) => marks.push(phase), flush() {} } });
  await h.navigation.confirm();
  const access = payload();
  const first = await h.owner.read(await h.owner.attach(access));
  assert.equal(first.state, 'ready');
  h.ready.participant = Object.fromEntries(['instance_id', 'draft_scope_id', 'disclosure_bundle_id'].map(key => [key, first[key]]));
  await h.navigation.navigate(order);
  await h.helper.start();
  assert.equal(await until(() => h.created.length > 0 || h.frames.some(frame => frame.type === 'refused')), true);
  assert.equal(h.frames.some(frame => frame.type === 'refused'), false);
  assert.equal(h.created.length, 1);
  assert.equal(h.created[0].url, h.ready.ceremony.entry_url + '#system-browser');
  assert.ok(marks.indexOf('attach-done') < marks.indexOf('open-guarded'));
  assert.ok(h.frames.some(frame => frame.type === 'authentication'));
  assert.deepEqual(h.ended, []);
});

for (const preparingReads of [0, 3]) {
  test(`a sign-in helper opens its popup exactly once after ${preparingReads} preparing reads`, async t => {
    const h = harness(t, { preparingReads });
    await h.helper.start();
    assert.equal(await until(() => h.created.length > 0), true, `popup opened (presentations: ${h.presentations.join(',')})`);
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(h.created.length, 1);
    if (preparingReads === 0) {
      assert.equal(h.created[0].url, h.ready.ceremony.entry_url + '#system-browser');
      assert.deepEqual(h.updated, []);
    } else {
      // The waiting window opens on the first preparing read, then shows the exact ceremony once.
      assert.equal(h.created[0].url, `chrome-extension://${h.ready.extension_id}/sign-in-waiting.html`);
      assert.deepEqual(h.updated, [[80, { url: h.ready.ceremony.entry_url + '#system-browser', active: true }]]);
    }
    assert.deepEqual(h.removed, []);
    assert.ok(h.reads > preparingReads);
  });
}

test('the waiting window opens on the first preparing read, before the ceremony exists', async t => {
  const h = harness(t, { preparingReads: 50 });
  void h.helper.start();
  assert.equal(await until(() => h.created.length > 0), true);
  assert.ok(h.reads <= 2, `opened after ${h.reads} reads`);
  assert.equal(h.created[0].url, `chrome-extension://${h.ready.extension_id}/sign-in-waiting.html`);
  assert.deepEqual(h.updated, []);
});

test('a sign-in cancelled while it is prepared closes its waiting window without opening a ceremony', async t => {
  const h = harness(t, { preparingReads: 3, outcome: 'cancelled' });
  await h.helper.start();
  assert.equal(await until(() => h.removed.length > 0), true, `presentations: ${h.presentations.join(',')}`);
  assert.equal(h.created.length, 1);
  assert.deepEqual(h.removed, [41]);
  assert.deepEqual(h.updated, []);
  assert.ok(h.presentations.includes('task_changed'));
});

test('a helper the customer opened by hand shows no waiting window', async t => {
  const h = harness(t, { preparingReads: 3, nativeLaunch: false });
  await h.helper.start();
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.deepEqual(h.created, []);
});

test('a prepared sign-in attaches on the port it prepared with and uses its read ahead', async t => {
  const h = harness(t, { preparingReads: 3, prepared: true });
  await h.helper.start();
  assert.equal(await until(() => h.created.length > 0 && h.updated.length > 0), true, `presentations: ${h.presentations.join(',')}`);
  // One waiting port and one task port. The task port prepared, then attached.
  assert.equal(h.connects, 2);
  assert.deepEqual(h.calls.filter(call => call !== 'read').slice(0, 2), ['prepare', 'attachPrepared']);
  assert.equal(h.calls.includes('attach'), false);
  // One read ahead and the two guards of the popup opening. Attach reads nothing.
  assert.deepEqual(h.calls.slice(0, h.calls.indexOf('popup') + 1), ['prepare', 'read', 'attachPrepared', 'read', 'read', 'popup']);
  // Only the popup owner's attempt record is stored. The read ahead stores nothing.
  assert.deepEqual(Object.keys(await h.chromeApi.storage.local.get()), ['browser_auth_attempts_v2']);
});

test('a change after the read ahead makes attach read in full', async t => {
  const h = harness(t, { preparingReads: 30, prepared: true });
  void h.helper.start();
  assert.equal(await until(() => h.calls.includes('read')), true);
  h.ports.changed();
  assert.equal(await until(() => h.created.length > 0 && h.updated.length > 0, 8000), true);
  assert.deepEqual(h.calls.filter(call => call !== 'read').slice(0, 3), ['prepare', 'forget', 'attach']);
});

test('a sign-in cancelled while it is prepared forgets its read ahead and stores nothing', async t => {
  const h = harness(t, { preparingReads: 3, outcome: 'cancelled', prepared: true });
  await h.helper.start();
  assert.equal(await until(() => h.calls.includes('forget')), true, `calls: ${h.calls.join(',')}`);
  assert.deepEqual(h.calls, ['prepare', 'read', 'forget']);
  assert.deepEqual(await h.chromeApi.storage.local.get(), {});
});

test('an attach records why it did not use its read ahead, and the stages up to the popup', async t => {
  const marks = [], flushed = [];
  const stages = { begin: (id, queued) => marks.push(['attach-received', queued]), mark: (id, phase) => marks.push([phase]),
    flush: id => flushed.push(id) };
  let tab = null;
  const source = Object.assign(changed => { tab = changed; return () => { tab = null; }; }, { label: 'tab' });
  // The waiting page finishes loading after the read ahead, as Chrome reports it.
  const h = harness(t, { preparingReads: 3, prepared: true, stages, changeSources: [source],
    onCreate: options => { if (options.url.endsWith('sign-in-waiting.html')) setTimeout(() => tab?.(), 0); } });
  await h.helper.start();
  assert.equal(await until(() => h.updated.length > 0), true, `presentations: ${h.presentations.join(',')}`);
  assert.deepEqual(marks.map(([phase]) => phase), ['attach-received', 'ahead-changed-tab', 'attach-done', 'read-done',
    'auth-published', 'authenticate-received', 'open-guarded', 'navigate-sent']);
  assert.ok(Number.isFinite(marks[0][1]));
  // The helper records the auth status after its attach stages.
  const helper = h.timings.filter(value => value.marks);
  assert.equal(helper.length, 2);
  assert.deepEqual(helper.flatMap(value => value.marks.map(mark => mark.phase)), ['payload-ready', 'attach-sent', 'auth-idle']);
  for (const value of helper[0].marks) assert.deepEqual(Object.keys(value).sort(), ['elapsed_ms', 'operation', 'phase', 'status']);
});

test('a sign-in the extension cannot open sends its stages, reports why and closes its helper', async t => {
  const marks = [], flushed = [];
  const stages = { begin: () => marks.push('attach-received'), mark: (id, phase) => marks.push(phase), flush: id => flushed.push(id) };
  const h = harness(t, { preparingReads: 3, prepared: true, stages, extensionState: 'needs-account' });
  await h.helper.start();
  assert.equal(await until(() => h.ended.length > 0 && h.removed.length > 0), true, `presentations: ${h.presentations.join(',')}`);
  assert.deepEqual(marks.slice(-2), ['read-done', 'not-ready']);
  assert.equal(flushed[0], h.ready.task.task_id);
  assert.deepEqual(h.ended, [{ reason: 'needs_account' }]);
  assert.ok(h.presentations.includes('auth_access_required'));
  // The waiting window closes and no ceremony opens.
  assert.deepEqual(h.updated, []);
  assert.deepEqual(h.removed, [41]);
});

// The waiting page, or a tab given as an event of its own, finishes loading after the read ahead.
async function aheadAfterTabLoad(t, tabFor) {
  const marks = [];
  const stages = { begin: () => marks.push('attach-received'), mark: (id, phase) => marks.push(phase), flush() {} };
  const onUpdated = { listeners: new Set(), addListener(fn) { this.listeners.add(fn); }, removeListener(fn) { this.listeners.delete(fn); } };
  const extensionId = payload().extension_id;
  const source = tabChangeSource({ runtime: { id: extensionId }, tabs: { onUpdated } });
  const h = harness(t, { preparingReads: 3, prepared: true, stages, changeSources: [source],
    onCreate: options => { if (options.url.endsWith('sign-in-waiting.html')) setTimeout(() => {
      for (const [info, tab] of tabFor(options.url)) for (const fn of onUpdated.listeners) fn(80, info, tab);
    }, 0); } });
  await h.helper.start();
  assert.equal(await until(() => h.updated.length > 0), true, `presentations: ${h.presentations.join(',')}`);
  return marks[1];
}

test('the waiting page and the helper finishing their loads keep the read ahead', async t => {
  assert.equal(await aheadAfterTabLoad(t, url => [[{ status: 'complete' }, { url }],
    [{ status: 'complete' }, { url: 'http://bridge.localhost:17871/browser-tasks#presentation=1' }]]), 'ahead-reused');
});

for (const [name, events] of [
  ['an OnlyFans tab finishing its load', [[{ status: 'complete' }, { url: 'https://onlyfans.com/' }]]],
  ['a tab without a visible URL finishing its load', [[{ status: 'complete' }, {}]]],
  ['a tab leaving OnlyFans for another site', [[{ status: 'loading', url: 'https://example.com/' }, { url: 'https://example.com/' }],
    [{ status: 'complete' }, { url: 'https://example.com/' }]]],
  ['a tab on a look-alike helper host', [[{ status: 'complete' }, { url: 'http://bridge.localhost:178710/' }]]],
]) {
  test(`${name} still discards the read ahead`, async t => {
    assert.equal(await aheadAfterTabLoad(t, () => events), 'ahead-changed-tab');
  });
}

test('an attach with an unchanged read ahead records its reuse', async t => {
  const marks = [];
  const stages = { begin: () => marks.push('attach-received'), mark: (id, phase) => marks.push(phase), flush() {} };
  const h = harness(t, { preparingReads: 3, prepared: true, stages });
  await h.helper.start();
  assert.equal(await until(() => h.updated.length > 0), true);
  assert.deepEqual(marks.slice(0, 2), ['attach-received', 'ahead-reused']);
});
