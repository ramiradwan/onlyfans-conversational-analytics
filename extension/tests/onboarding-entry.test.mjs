import assert from 'node:assert/strict';
import test from 'node:test';
import { registerOnboardingWorkspace } from '../runtime/onboarding-entry.mjs';
import { createBrowserTaskOwner } from '../runtime/browser-task-owner.mjs';
import { BROWSER_TASK_INSTANCE_KEY, BROWSER_TASK_ADOPTION_KEY } from '../runtime/browser-task-entry.mjs';
import { payload, id } from '../test-fixtures/browser-task.mjs';

const stateKey = 'onboarding_workspace_state_v3';
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
  }, async set(update) { Object.assign(values, structuredClone(update)); },
  async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete values[key]; } });
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
  assert.equal((await f.command('draft', { scope_id: f.local[stateKey].record.draft_scope.scope_id, draft: checked })).ok, true);
  return f;
}

function browserOwner(f, authentication) {
  return createBrowserTaskOwner({ chromeApi: f.chromeApi, onboarding: f.entry,
    authentication,
    consentController: { async status() { return { consent: { mode: 'full', consent_epoch: 'approved' }, onlyfans_permission: true }; } },
    legalActivationController: { async status() { return { configured: true, requires_reauthorization: false }; } },
    identityBridge: { async currentAccountId() { return f.state.account; } },
    openCreatorAccount: () => assert.fail('no account page is required') });
}

test('desktop access with existing consent retains its profile binding through sign-in and worker restart', async () => {
  const f = fixture(), access = payload(), owner = browserOwner(f);
  const observation = await owner.read(await owner.attach(access));
  assert.equal(observation.state, 'ready');
  const authentication = { ...access, participant: Object.fromEntries(
    ['instance_id', 'draft_scope_id', 'disclosure_bundle_id'].map(key => [key, observation[key]])),
    creator_account_id: 'creator-a', task: { ...access.task, task_id: id(12), purpose: 'browser.authentication' } };
  await owner.attach(authentication);
  const restarted = fixture({ saved: f }), next = browserOwner(restarted);
  const current = await next.read(await next.attach(authentication));
  assert.equal(current.draft_scope_id, observation.draft_scope_id);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(restarted.calls, []);
  restarted.state.account = 'creator-b';
  await assert.rejects(next.attach(authentication), /task_changed/);
});

test('extension-first attachment cannot create missing launch evidence or replace a saved journey', async () => {
  const f = fixture(), value = payload();
  value.entry_source = 'extension';
  await assert.rejects(browserOwner(f).attach(value), /task_changed/);
  assert.equal(f.local[stateKey], undefined);
  const saved = await savedFixture(), before = structuredClone(saved.local);
  await assert.rejects(browserOwner(saved).attach(value), /task_changed/);
  assert.deepEqual(saved.local, before);
});

test('explicit desktop access replaces stale presentation while preserving consent, identity and the accepted task', async () => {
  const f = await savedFixture(), prior = structuredClone(f.local[stateKey]);
  f.local[BROWSER_TASK_INSTANCE_KEY] = id(71);
  f.local.consent = { mode: 'full', consent_epoch: 'existing-approval' };
  f.local.browser_auth_attempts_v1 = [{ retained: 'unknown correlation' }];
  const task = payload(), original = structuredClone(task), owner = browserOwner(f);
  const context = await owner.attach(task), current = f.local[stateKey];
  assert.equal((await owner.read(context)).state, 'ready');
  assert.equal(current.record.journey_id, task.task.journey_id);
  assert.notEqual(current.record.draft_scope.scope_id, prior.record.draft_scope.scope_id);
  assert.deepEqual(current.record.draft, { terms_checked: false, risk_checked: false, full_checked: false });
  assert.equal(current.identity.account_digest, prior.identity.account_digest);
  assert.equal(f.local[BROWSER_TASK_INSTANCE_KEY], id(71));
  assert.deepEqual(f.local.consent, { mode: 'full', consent_epoch: 'existing-approval' });
  assert.deepEqual(f.local.browser_auth_attempts_v1, [{ retained: 'unknown correlation' }]);
  assert.deepEqual(task, original);
  assert.equal((await f.command('read')).ok, false, 'old setup document loses admission');
  assert.equal(f.calls.length, 1, 'recovery never opens or navigates a tab');
  const scope = structuredClone(current.record.draft_scope);
  await owner.attach(task);
  const restarted = fixture({ saved: f }); await browserOwner(restarted).attach(task);
  assert.deepEqual(restarted.local[stateKey].record.draft_scope, scope, 'same-journey retry and worker restart retain scope');
});

for (const document of ['exact', 'reloaded', 'navigated', 'closed']) {
  test(`desktop access can retire a confirmed ${document} native launch intent`, async () => {
    const f = await savedFixture(); assert.equal((await f.command('prepare_launch')).ok, true);
    if (document === 'reloaded') f.tabs[0].documentId = 'replacement-document';
    if (document === 'navigated') {
      f.tabs[0].url = 'https://onlyfans.com/'; f.chromeApi.runtime.getContexts = async () => [];
    }
    if (document === 'closed') f.tabs.splice(0);
    await browserOwner(f).attach(payload());
    assert.equal(f.session[launchKey], undefined);
    assert.equal(f.session.onboarding_workspace_tab_v1, undefined);
    assert.equal(f.local[stateKey].record.journey_id, payload().task.journey_id);
  });
}

for (const unavailable of ['contexts-fail', 'contexts-missing', 'same-url-unknown-document', 'tab-query-fail', 'malformed-scope']) {
  test(`ambiguous native launch inspection (${unavailable}) leaves storage unchanged`, async () => {
    const f = await savedFixture(); await f.command('prepare_launch');
    if (unavailable === 'contexts-fail') f.chromeApi.runtime.getContexts = async () => { throw Error('unavailable'); };
    if (unavailable === 'contexts-missing') delete f.chromeApi.runtime.getContexts;
    if (unavailable === 'same-url-unknown-document') f.chromeApi.runtime.getContexts = async () => [];
    if (unavailable === 'tab-query-fail') {
      f.chromeApi.runtime.getContexts = async () => [];
      f.chromeApi.tabs.query = async () => { throw Error('unavailable'); };
    }
    if (unavailable === 'malformed-scope') f.session[launchKey].draft_scope.scope_id = 'malformed';
    const before = structuredClone({ local: f.local, session: f.session });
    await assert.rejects(browserOwner(f).attach(payload()), /task_changed/);
    assert.deepEqual({ local: f.local, session: f.session }, before);
  });
}

test('an attached extension task wins a concurrent desktop rebind until its exact owner releases', async () => {
  const f = await savedFixture(); await f.command('prepare_launch');
  const previous = structuredClone(f.local[stateKey]), old = payload();
  old.entry_source = 'extension'; old.task.journey_id = previous.record.journey_id;
  const owner = browserOwner(f), [attached, rebound] = await Promise.allSettled([owner.attach(old), owner.attach(payload())]);
  assert.equal(attached.status, 'fulfilled'); assert.equal(rebound.status, 'rejected');
  assert.match(rebound.reason.message, /task_changed/);
  assert.deepEqual(f.local[stateKey], previous);
  await owner.release(attached.value);
  await owner.attach(payload());
  await assert.rejects(owner.attach(old), /task_changed/);
});

test('a desktop rebind wins before a stale extension attachment and fences duplicate owner release', async () => {
  const f = await savedFixture(); await f.command('prepare_launch');
  const old = payload(); old.entry_source = 'extension'; old.task.journey_id = f.local[stateKey].record.journey_id;
  const owner = browserOwner(f), next = payload();
  const [attached, stale] = await Promise.allSettled([owner.attach(next), owner.attach(old)]);
  assert.equal(attached.status, 'fulfilled'); assert.equal(stale.status, 'rejected');
  const replacement = await owner.attach(next);
  await owner.release(attached.value);
  await assert.rejects(owner.read(attached.value), /task_changed/);
  assert.equal((await owner.read(replacement)).journey_id, next.task.journey_id);
  const other = payload(); other.task.journey_id = id(73);
  await assert.rejects(owner.attach(other), /task_changed/);
  await owner.release(replacement); await owner.attach(other);
});

test('a live authentication window prevents rebind after its transport owner closes', async () => {
  const f = await savedFixture(); let live = false;
  const owner = browserOwner(f, { hasLiveFlow: () => live }), old = payload();
  old.task.journey_id = f.local[stateKey].record.journey_id;
  const context = await owner.attach(old); live = true; await owner.release(context);
  const before = structuredClone(f.local);
  await assert.rejects(owner.attach(payload()), /task_changed/);
  assert.deepEqual(f.local, before);
  live = false; await owner.attach(payload());
});

test('authentication starting during document inspection prevents presentation writes', async () => {
  const f = await savedFixture(); await f.command('prepare_launch'); let live = false;
  const getContexts = f.chromeApi.runtime.getContexts;
  f.chromeApi.runtime.getContexts = async () => { live = true; return getContexts(); };
  const before = structuredClone({ local: f.local, session: f.session });
  await assert.rejects(browserOwner(f, { hasLiveFlow: () => live }).attach(payload()), /task_changed/);
  assert.deepEqual({ local: f.local, session: f.session }, before);
});

test('another stable instance and malformed stored identity fail before workspace changes', async () => {
  const f = await savedFixture(), value = payload();
  value.participant = { instance_id: id(75), draft_scope_id: id(76), disclosure_bundle_id: 'b'.repeat(64) };
  for (const stored of [id(77), 'corrupt']) {
    f.local[BROWSER_TASK_INSTANCE_KEY] = stored;
    const before = structuredClone(f.local);
    await assert.rejects(browserOwner(f).attach(value), stored === 'corrupt' ? /unavailable/ : /profile_mismatch/);
    assert.deepEqual(f.local, before);
  }
});

test('a supplied participant in the same instance cannot replace another journey', async () => {
  const f = await savedFixture(), value = payload();
  f.local[BROWSER_TASK_INSTANCE_KEY] = id(78);
  value.participant = { instance_id: id(78), draft_scope_id: f.local[stateKey].record.draft_scope.scope_id,
    disclosure_bundle_id: f.local[stateKey].record.draft_scope.disclosure_bundle_id };
  const before = structuredClone(f.local);
  await assert.rejects(browserOwner(f).attach(value), /task_changed/);
  assert.deepEqual(f.local, before);
  assert.equal(f.local[BROWSER_TASK_ADOPTION_KEY], undefined);
});

test('identity reconciliation can retire an obsolete launch scope without discarding a new matching launch', async () => {
  const f = await savedFixture(); await f.command('prepare_launch');
  const old = structuredClone(f.session[launchKey]);
  f.state.account = 'creator-b'; await f.command('read');
  assert.notEqual(f.local[stateKey].record.draft_scope.scope_id, old.draft_scope.scope_id);
  const value = payload(); value.task.journey_id = old.journey_id;
  const owner = browserOwner(f); await owner.attach(value);
  assert.equal(f.session[launchKey], undefined);
  await f.command('prepare_launch'); const matching = structuredClone(f.session[launchKey]);
  await owner.attach(value);
  assert.deepEqual(f.session[launchKey], matching);
});

for (const failure of ['before-record', 'after-record', 'marker-cleanup']) {
  test(`interrupted rebind at ${failure} recovers through exact retry with no replacement task`, async () => {
    const f = await savedFixture(); await f.command('prepare_launch');
    const previous = structuredClone({ record: f.local[stateKey], session: f.session });
    const set = f.chromeApi.storage.local.set, remove = f.chromeApi.storage.session.remove;
    let interrupt = true;
    f.chromeApi.storage.local.set = async change => {
      if (interrupt && change[stateKey]?.record.journey_id === payload().task.journey_id && failure !== 'marker-cleanup') {
        interrupt = false;
        if (failure === 'after-record') await set(change);
        throw Error('interrupted');
      }
      await set(change);
    };
    f.chromeApi.storage.session.remove = async keys => {
      if (interrupt && failure === 'marker-cleanup') { interrupt = false; throw Error('interrupted'); }
      await remove(keys);
    };
    const owner = browserOwner(f), value = payload(), beforeTask = structuredClone(value);
    await assert.rejects(owner.attach(value), /interrupted/);
    assert.deepEqual(f.session, previous.session, 'failed commit or cleanup retains prior markers');
    const committedScope = f.local[stateKey].record.draft_scope.scope_id;
    if (failure === 'before-record') assert.deepEqual(f.local[stateKey], previous.record);
    await owner.attach(value);
    if (failure !== 'before-record') assert.equal(f.local[stateKey].record.draft_scope.scope_id, committedScope);
    assert.equal(f.session[launchKey], undefined);
    assert.equal(f.session.onboarding_workspace_tab_v1, undefined);
    assert.deepEqual(value, beforeTask);
  });
}

for (const interruption of ['inspection', 'submitted-record']) {
  test(`attachment cancelled during ${interruption} cannot retain a task owner`, async () => {
    const f = await savedFixture(); await f.command('prepare_launch');
    const abort = new AbortController(), before = structuredClone(f.local[stateKey]);
    if (interruption === 'inspection') {
      const getContexts = f.chromeApi.runtime.getContexts;
      f.chromeApi.runtime.getContexts = async () => { abort.abort(); return getContexts(); };
    } else {
      const set = f.chromeApi.storage.local.set;
      f.chromeApi.storage.local.set = async change => {
        await set(change); if (change[stateKey]?.record.journey_id === payload().task.journey_id) abort.abort();
      };
    }
    const owner = browserOwner(f);
    await assert.rejects(owner.attach(payload(), { signal: abort.signal }), /task_changed/);
    if (interruption === 'inspection') assert.deepEqual(f.local[stateKey], before);
    assert.equal(f.local[BROWSER_TASK_ADOPTION_KEY], undefined);
    const next = payload(); next.task.journey_id = id(79);
    assert.equal((await owner.read(await owner.attach(next))).journey_id, id(79));
  });
}

for (const committed of [false, true]) {
  test(`same-worker retry recovers a failed first instance write (committed=${committed})`, async () => {
    const f = await savedFixture(), set = f.chromeApi.storage.local.set;
    let interrupt = true, submitted;
    f.chromeApi.storage.local.set = async change => {
      if (interrupt && change[BROWSER_TASK_INSTANCE_KEY]) {
        interrupt = false; submitted = change[BROWSER_TASK_INSTANCE_KEY];
        if (committed) await set(change);
        throw Error('instance write interrupted');
      }
      await set(change);
    };
    const owner = browserOwner(f), value = payload();
    await assert.rejects(owner.attach(value), /instance write interrupted/);
    const scope = f.local[stateKey].record.draft_scope.scope_id;
    const observed = await owner.read(await owner.attach(value));
    assert.equal(observed.state, 'ready'); assert.equal(observed.draft_scope_id, scope);
    if (committed) assert.equal(observed.instance_id, submitted, 'a confirmed stored instance is reused');
  });
}

test('a delayed observation cannot overwrite the next journey adoption after release', async () => {
  const f = await savedFixture(), value = payload(), owner = browserOwner(f);
  value.task.journey_id = f.local[stateKey].record.journey_id;
  const context = await owner.attach(value), set = f.chromeApi.storage.local.set;
  let entered, finish;
  const writing = new Promise(resolve => { entered = resolve; });
  const suspended = new Promise(resolve => { finish = resolve; });
  f.state.account = 'creator-b';
  f.chromeApi.storage.local.set = async change => {
    if (change[BROWSER_TASK_ADOPTION_KEY]?.journey_id === value.task.journey_id) { entered(); await suspended; }
    await set(change);
  };
  const reading = owner.read(context).catch(error => { assert.match(error.message, /task_changed/); });
  await writing;
  const released = owner.release(context), rebound = owner.attach(payload());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.local[stateKey].record.journey_id, value.task.journey_id, 'rebind waits for the old publication');
  finish(); await reading; await released;
  const next = await rebound;
  assert.equal(f.local[BROWSER_TASK_ADOPTION_KEY].journey_id, payload().task.journey_id);
  assert.equal((await owner.read(next)).state, 'ready');
  await assert.rejects(owner.read(context), /task_changed/);
});

test('worker restoration initializes the actual current identity before preserving a valid draft and launch', async () => {
  const saved = await savedFixture(); const expected = structuredClone(saved.local[stateKey].record);
  const f = fixture({ saved });
  f.changed.emit();
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.deepEqual(f.local[stateKey].record, expected);
  assert.deepEqual(f.session[launchKey].draft_scope, expected.draft_scope);
  f.changed.emit(); assert.equal((await f.command('read')).ok, true);
  assert.deepEqual(f.local[stateKey].record, expected);
  assert.deepEqual(f.calls, [], 'identity reconciliation never navigates or creates a page');
});

for (const next of ['creator-b', null]) test(`launch reconciles current ${next} before storing its intent even before notification`, async () => {
  const f = await savedFixture(); const previous = structuredClone(f.local[stateKey].record);
  f.state.account = next;
  assert.equal((await f.command('prepare_launch')).ok, true);
  const current = structuredClone(f.local[stateKey].record);
  assert.notEqual(current.draft_scope.scope_id, previous.draft_scope.scope_id);
  assert.deepEqual(current.draft, { ...checked, full_checked: false });
  assert.deepEqual(f.session[launchKey].draft_scope, current.draft_scope);
  f.changed.emit(); await f.command('read');
  assert.deepEqual(f.local[stateKey].record, current, 'late notification cannot rotate an already reconciled identity');
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
  assert.notEqual(f.local[stateKey].record.draft_scope.scope_id, saved.local[stateKey].record.draft_scope.scope_id);
  assert.deepEqual(f.session[launchKey].draft_scope, f.local[stateKey].record.draft_scope);
  assert.equal(f.local[stateKey].record.draft.full_checked, false);
});

test('failed scope persistence does not advance the tracked identity or poison reconciliation', async () => {
  const f = await savedFixture(); const previous = structuredClone(f.local[stateKey].record);
  const set = f.chromeApi.storage.local.set; let fail = true;
  f.chromeApi.storage.local.set = async (update) => {
    if (fail && update[stateKey]?.record?.draft_scope.scope_id !== previous.draft_scope.scope_id) {
      fail = false; throw Error('fixture scope write failed');
    }
    return set(update);
  };
  f.state.account = 'creator-b';
  assert.equal((await f.command('prepare_launch')).ok, false);
  assert.equal(f.session[launchKey], undefined);
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.notEqual(f.local[stateKey].record.draft_scope.scope_id, previous.draft_scope.scope_id);
  assert.equal(f.local[stateKey].record.draft.full_checked, false);
});

test('temporary consent restoration failure is retried by the next explicit workspace action', async () => {
  const saved = await savedFixture(); let attempts = 0;
  const f = fixture({ saved, initialize: async () => { if (++attempts === 1) throw Error('fixture restoration failed'); } });
  assert.equal((await f.command('prepare_launch')).ok, false);
  assert.equal(f.state.reads, 0); assert.equal(f.session[launchKey], undefined);
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.deepEqual(f.local[stateKey].record, saved.local[stateKey].record);
});

test('a persisted identity changed while the worker was absent cannot inherit the prior creator draft', async () => {
  const saved = await savedFixture(); const f = fixture({ saved, account: 'creator-b' });
  assert.equal((await f.command('prepare_launch')).ok, true);
  assert.notEqual(f.local[stateKey].record.draft_scope.scope_id, saved.local[stateKey].record.draft_scope.scope_id);
  assert.deepEqual(f.local[stateKey].record.draft, { ...checked, full_checked: false });
});

for (const type of ['ofca.workspace.recovery-prepare.v1','ofca.workspace.saved-continuation-prepare.v1',
  'ofca.workspace.launch-return.v1','ofca.workspace.activation.v1','ofca.workspace.recovery-ready.v1']) {
  test(`retired ${type} cannot call the workspace owner`, async () => {
    const f = await savedFixture(); const before = structuredClone(f.local);
    assert.equal(f.chromeApi.runtime.onMessageExternal.listeners[0]({type},f.sender(),()=>assert.fail('unexpected reply')),false);
    assert.deepEqual(f.local,before);
  });
}
