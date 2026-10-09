import assert from 'node:assert/strict';
import test from 'node:test';
import { registerOnboardingWorkspace } from '../runtime/onboarding-entry.mjs';

const recordKey = 'onboarding_workspace_v1';
const launchKey = 'onboarding_native_launch_v1';
const checked = { terms_checked: true, risk_checked: true, full_checked: true };
const event = () => { const listeners = []; return { listeners, addListener: (fn) => listeners.push(fn),
  emit: (...args) => { for (const fn of listeners) fn(...args); } }; };
function fixture({ saved, account = 'creator-a', initialize = async () => {}, read } = {}) {
  const local = structuredClone(saved?.local ?? {}), session = {}, tabs = structuredClone(saved?.tabs ?? []);
  const changed = event(), messages = event(), calls = [];
  const state = { account, reads: 0 };
  const storage = (values) => ({ async get(keys) {
    return structuredClone(Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((key) => [key, values[key]])));
  }, async set(update) { Object.assign(values, structuredClone(update)); } });
  const chromeApi = { storage: { local: storage(local), session: storage(session) },
    runtime: { id: 'synthetic', getURL: (file) => `chrome-extension://synthetic/${file}`,
      onMessage: messages, onMessageExternal: event(), onConnect: event(), onInstalled: event(),
      async getContexts() { return tabs.map((tab) => ({ tabId: tab.id, windowId: tab.windowId,
        frameId: 0, documentId: tab.documentId, documentUrl: tab.url })); } },
    tabs: { async query() { return structuredClone(tabs); },
      async get(id) { return structuredClone(tabs.find((tab) => tab.id === id)); },
      async create(options) { const tab = { id: 7, windowId: 1, documentId: 'current-document', ...options };
        tabs.push(tab); calls.push(['create']); return structuredClone(tab); },
      async update(id, options) { Object.assign(tabs.find((tab) => tab.id === id), options); calls.push(['update', options]); } },
    windows: { async update() {} }, action: { onClicked: event() } };
  const entry = registerOnboardingWorkspace({ chromeApi, consentController: { initialize }, identityBridge: {
    async currentAccountId() { state.reads++; return read ? read(state) : state.account; },
    onAccountChange: changed.addListener,
  } });
  const sender = () => ({ id: 'synthetic', frameId: 0, tab: { id: tabs[0].id },
    documentId: tabs[0].documentId, url: tabs[0].url });
  const command = (action, extra = {}) => new Promise((resolve) => messages.listeners[0](
    { type: 'ofca.workspace.v1', action, ...extra }, sender(), resolve));
  return { entry, chromeApi, local, session, tabs, state, changed, calls, command, sender };
}
async function savedFixture() {
  const f = fixture(); await f.entry.open();
  assert.equal((await f.command('draft', { scope_id: f.local[recordKey].draft_scope.scope_id, draft: checked })).ok, true);
  return f;
}

test('worker restoration initializes the actual current identity before preserving a valid draft and launch', async () => {
  const saved = await savedFixture(); const expected = structuredClone(saved.local[recordKey]);
  const f = fixture({ saved });
  f.changed.emit();
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.deepEqual(f.local[recordKey], expected);
  assert.deepEqual(f.session[launchKey].draft_scope, expected.draft_scope);
  f.changed.emit(); assert.equal((await f.command('read')).ok, true);
  assert.deepEqual(f.local[recordKey], expected);
  assert.deepEqual(f.calls, [], 'identity reconciliation never navigates or creates a page');
});

for (const next of ['creator-b', null]) test(`launch reconciles current ${next} before storing its intent even before notification`, async () => {
  const f = await savedFixture(); const previous = structuredClone(f.local[recordKey]);
  f.state.account = next;
  assert.equal((await f.command('prepare_launch')).ok, true);
  const current = structuredClone(f.local[recordKey]);
  assert.notEqual(current.draft_scope.scope_id, previous.draft_scope.scope_id);
  assert.deepEqual(current.draft, { ...checked, full_checked: false });
  assert.deepEqual(f.session[launchKey].draft_scope, current.draft_scope);
  f.changed.emit(); await f.command('read');
  assert.deepEqual(f.local[recordKey], current, 'late notification cannot rotate an already reconciled identity');
});

test('a cold unknown identity remains unknown; its first independent observation rotates scope', async () => {
  const saved = await savedFixture(); const f = fixture({ saved, account: null });
  const before = await f.command('read'); assert.equal(before.ok, true);
  f.state.account = 'creator-a'; f.changed.emit();
  const after = await f.command('read'); assert.equal(after.ok, true);
  assert.notEqual(after.result.draft_scope.scope_id, before.result.draft_scope.scope_id);
  assert.equal(after.result.draft.full_checked, false);
});

test('restored consent gates baseline reads and queued changes cannot overtake launch', async () => {
  const saved = await savedFixture();
  let initialized, baselineRead;
  const initialization = new Promise((resolve) => { initialized = resolve; });
  const baseline = new Promise((resolve) => { baselineRead = resolve; });
  const f = fixture({ saved, initialize: () => initialization,
    read: (state) => state.reads === 1 ? baseline : state.account });
  const pending = f.command('prepare_launch'); f.changed.emit();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.state.reads, 0); assert.equal(f.session[launchKey], undefined);
  initialized(); await new Promise((resolve) => setImmediate(resolve));
  f.state.account = 'creator-b'; f.changed.emit(); baselineRead('creator-a');
  assert.equal((await pending).ok, true);
  assert.notEqual(f.local[recordKey].draft_scope.scope_id, saved.local[recordKey].draft_scope.scope_id);
  assert.deepEqual(f.session[launchKey].draft_scope, f.local[recordKey].draft_scope);
  assert.equal(f.local[recordKey].draft.full_checked, false);
});

test('failed scope persistence does not advance the tracked identity or poison reconciliation', async () => {
  const f = await savedFixture(); const previous = structuredClone(f.local[recordKey]);
  const set = f.chromeApi.storage.local.set; let fail = true;
  f.chromeApi.storage.local.set = async (update) => {
    if (fail && update[recordKey]?.draft_scope.scope_id !== previous.draft_scope.scope_id) {
      fail = false; throw Error('fixture scope write failed');
    }
    return set(update);
  };
  f.state.account = 'creator-b';
  assert.equal((await f.command('prepare_launch')).ok, false);
  assert.equal(f.session[launchKey], undefined);
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.notEqual(f.local[recordKey].draft_scope.scope_id, previous.draft_scope.scope_id);
  assert.equal(f.local[recordKey].draft.full_checked, false);
});

test('temporary consent restoration failure is retried by the next explicit workspace action', async () => {
  const saved = await savedFixture(); let attempts = 0;
  const f = fixture({ saved, initialize: async () => { if (++attempts === 1) throw Error('fixture restoration failed'); } });
  assert.equal((await f.command('prepare_launch')).ok, false);
  assert.equal(f.state.reads, 0); assert.equal(f.session[launchKey], undefined);
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.deepEqual(f.local[recordKey], saved.local[recordKey]);
});

test('a persisted identity changed while the worker was absent cannot inherit the prior creator draft', async () => {
  const saved = await savedFixture(); const f = fixture({ saved, account: 'creator-b' });
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.notEqual(f.local[recordKey].draft_scope.scope_id, saved.local[recordKey].draft_scope.scope_id);
  assert.deepEqual(f.local[recordKey].draft, { ...checked, full_checked: false });
});

test('external recovery messages use the closed union and reconcile creator scope before dispatch', async () => {
  const f = await savedFixture(); const entryId = crypto.randomUUID();
  const callback = { id: 8, windowId: 1, documentId: 'native-callback', url: 'http://bridge.localhost:17871/provisioning/native-return' };
  f.tabs.push(callback);
  const sender = { tab: { id: callback.id }, frameId: 0, documentId: callback.documentId, url: callback.url };
  const command = (message) => new Promise((resolve) => f.chromeApi.runtime.onMessageExternal.listeners[0](message, sender, resolve));
  const prepare = { type: 'ofca.workspace.recovery-prepare.v1', entry_id: entryId };
  assert.deepEqual(await command({ ...prepare, url: 'https://example.test/' }), { ok: false, code: 'return_unavailable' });
  const prepared = await command(prepare);
  assert.equal(prepared.ok, true); assert.equal(prepared.result.status, 'recovery_ready');
  const previous = structuredClone(f.local[recordKey]);
  f.state.account = 'creator-b';
  assert.deepEqual(await command({ type: 'ofca.workspace.recovery-return.v1', entry_id: entryId,
    recovery_id: prepared.result.recovery_id, previous_journey_id: previous.journey_id,
    journey_id: crypto.randomUUID(), route: 'provisioning' }), { ok: false, code: 'return_unavailable' });
  assert.equal(f.local[recordKey].draft.full_checked, false);
  assert.notEqual(f.local[recordKey].draft_scope.scope_id, previous.draft_scope.scope_id);
  assert.equal(f.local[recordKey].journey_id, previous.journey_id);
  assert.equal(f.calls.filter(([kind]) => kind === 'create').length, 1);
});

test('an account notification during document navigation invalidates recovery before receipt or focus', async () => {
  const f = await savedFixture(); const entryId = crypto.randomUUID();
  const prior = f.local[recordKey].journey_id;
  f.tabs[0].url = `http://bridge.localhost:17871/provisioning#journey=${prior}`;
  f.local[recordKey].route = 'provisioning';
  const callback = { id: 8, windowId: 1, active: true, documentId: 'native-callback', url: 'http://bridge.localhost:17871/provisioning/native-return' };
  f.tabs.push(callback);
  const updated = event(); updated.removeListener = (fn) => { updated.listeners.splice(updated.listeners.indexOf(fn), 1); };
  f.chromeApi.tabs.onUpdated = updated;
  f.chromeApi.scripting = { executeScript: async ({ args, target }) => {
    assert.deepEqual(target.documentIds, ['current-document']);
    f.state.account = 'creator-b'; f.changed.emit();
    f.tabs[0].url = args[1]; f.tabs[0].documentId = 'new-document';
    queueMicrotask(() => updated.emit(f.tabs[0].id, { status: 'complete' }));
    return [];
  } };
  const sender = { tab: { id: callback.id }, frameId: 0, documentId: callback.documentId, url: callback.url };
  const command = (message) => new Promise((resolve) => f.chromeApi.runtime.onMessageExternal.listeners[0](message, sender, resolve));
  const prepared = await command({ type: 'ofca.workspace.recovery-prepare.v1', entry_id: entryId });
  assert.equal(prepared.ok, true);
  const baseline = f.calls.length;
  assert.deepEqual(await command({ type: 'ofca.workspace.recovery-return.v1', entry_id: entryId,
    recovery_id: prepared.result.recovery_id, previous_journey_id: prior,
    journey_id: crypto.randomUUID(), route: 'provisioning' }), { ok: false, code: 'return_unavailable' });
  await f.command('read');
  assert.equal(f.local[recordKey].journey_id, prior);
  assert.equal(f.local[recordKey].draft.full_checked, false);
  assert.equal(f.calls.length, baseline);
  assert.equal(f.session.onboarding_native_recovery_v1.phase, 'returning');
});
