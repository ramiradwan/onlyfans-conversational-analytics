import assert from 'node:assert/strict';
import test from 'node:test';
import { createOnboardingWorkspace } from '../runtime/onboarding-workspace.mjs';
import { NATIVE_LAUNCH_KEY, NATIVE_LAUNCH_TTL_MS, workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const journey = 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed';
const scope = { scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64) };
const routes = { extension: 'chrome-extension://synthetic/setup.html', hosted: null,
  provisioning: 'http://bridge.localhost:17871/provisioning', bridge: 'http://bridge.localhost:17871/' };
function fixture() {
  const local = {}, session = {}, calls = [], windowCalls = [], clock = [1000];
  const events = () => { const listeners = new Set(); return { addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn), emit: (...args) => { for (const fn of listeners) fn(...args); } }; };
  const updated = events();
  const tabs = [{ id: 1, windowId: 1, documentId: 'user-document', url: 'https://onlyfans.com/my/chats' },
    { id: 2, windowId: 1, documentId: 'owner-document', url: `${routes.extension}#journey=${journey}` },
    { id: 3, windowId: 1, active: true, documentId: 'return-document', url: `${routes.bridge}provisioning/native-return#journey=${journey}` }];
  const storage = (data) => ({ get: async (key) => structuredClone({ [key]: data[key] }),
    set: async (value) => { Object.assign(data, structuredClone(value)); } });
  const chromeApi = { runtime: { id: 'synthetic', getURL: (path) => `chrome-extension://synthetic/${path}`,
    getContexts: async () => tabs.filter((tab) => tab.url.startsWith('chrome-extension:')).map((tab) => ({
      tabId: tab.id, frameId: 0, documentId: tab.documentId, documentUrl: tab.url,
    })) }, storage: { local: storage(local), session: storage(session) },
    scripting: { executeScript: async ({ target }) => [{ frameId: 0, documentId: tabs.find((tab) => tab.id === target.tabId).documentId }] },
    tabs: { onUpdated: updated, query: async () => structuredClone(tabs), get: async (id) => {
      const tab = tabs.find((value) => value.id === id); if (!tab) throw Error('closed'); return { ...tab };
    }, update: async (id, options) => { calls.push({ id, ...options }); Object.assign(tabs.find((tab) => tab.id === id), options); },
    create: async () => { throw Error('must reuse workspace'); } },
    windows: { get: async () => ({ focused: true }), update: async (...args) => { windowCalls.push(args); } } };
  const make = () => createOnboardingWorkspace({ chromeApi, routes, now: () => clock[0], navigationTimeoutMs: 25 });
  const workspace = make();
  const sender = (id) => { const tab = tabs.find((value) => value.id === id); return {
    tab: { ...tab }, url: tab.url, frameId: 0, documentId: tab.documentId,
    ...(tab.url.startsWith('chrome-extension:') ? { id: 'synthetic' } : {}),
  }; };
  const port = { sender: sender(2), onDisconnect: events(), disconnect() { this.onDisconnect.emit(); },
    postMessage(message) {
      if (message.type === 'ready') return;
      calls.push({ id: 2, url: `${routes[message.route]}#journey=${message.journey_id}` });
      queueMicrotask(() => { tabs[1].url = `${routes[message.route]}#journey=${message.journey_id}`;
        tabs[1].documentId = 'committed-document'; updated.emit(2, { status: 'complete' }); });
    } };
  const open = async () => { await workspace.open({ journey_id: journey, route: 'extension', explicit: true, draft_scope: scope });
    await workspace.bindNavigationPort(port); calls.length = 0; windowCalls.length = 0; };
  const request = { journey_id: journey, route: 'provisioning' };
  return { local, session, calls, windowCalls, clock, tabs, chromeApi, workspace, make, sender, open, request, port, updated };
}

test('explicit launch returns only a journey URI and keeps a bounded nonauthorizing intent', async () => {
  const f = fixture(); await f.open();
  assert.deepEqual(await f.workspace.prepareNativeLaunch(f.sender(2)), { journey_id: journey, app_link: workspaceAppLink(journey) });
  assert.equal(f.session[NATIVE_LAUNCH_KEY].expires_at - f.clock[0], NATIVE_LAUNCH_TTL_MS);
  assert.deepEqual(f.calls, []);
  assert.equal(Object.keys(f.session[NATIVE_LAUNCH_KEY]).some((key) => /token|cookie|csrf|secret|consent/u.test(key)), false);
});
test('native return reuses exact owner; duplicate receipt never repeats navigation or touches a user tab', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  assert.deepEqual(await f.workspace.returnFromNative(f.sender(3), f.request), { status: 'returned' });
  assert.deepEqual(f.calls, [{ id: 2, url: `${routes.provisioning}#journey=${journey}` }, { id: 2, active: true }]);
  await f.make().returnFromNative(f.sender(3), f.request);
  assert.equal(f.calls.filter((call) => call.url).length, 1);
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});
test('a clock tick during preparation does not invalidate the launch intent', async () => {
  const f = fixture(); await f.open();
  let tick = f.clock[0];
  Object.defineProperty(f.clock, 0, { get: () => ++tick });
  await f.workspace.prepareNativeLaunch(f.sender(2));
  const intent = f.session[NATIVE_LAUNCH_KEY];
  assert.equal(intent.expires_at - intent.created_at, NATIVE_LAUNCH_TTL_MS);
  assert.deepEqual(await f.workspace.returnFromNative(f.sender(3), f.request), { status: 'returned' });
});
test('no existing workspace permits normal native entry; an existing owner without intent refuses a competing wizard', async () => {
  const f = fixture();
  const owner = f.tabs.splice(1, 1)[0];
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /no_workspace/);
  f.tabs.push(owner);
  await f.open();
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /workspace_exists/);
  assert.deepEqual(f.calls, []);
});
for (const change of ['expired', 'future', 'extended-expiry', 'journey', 'scope', 'disclosure', 'owner-document', 'owner-url']) {
  test(`native return refuses ${change} launch intent without navigation`, async () => {
    const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
    if (change === 'expired') f.clock[0] += NATIVE_LAUNCH_TTL_MS;
    if (change === 'future') f.clock[0]--;
    if (change === 'extended-expiry') f.session[NATIVE_LAUNCH_KEY].expires_at++;
    if (change === 'journey') f.session[NATIVE_LAUNCH_KEY].journey_id = crypto.randomUUID();
    if (change === 'scope') await f.workspace.refreshScope({ ...scope, scope_id: crypto.randomUUID() });
    if (change === 'disclosure') await f.workspace.refreshScope({ ...scope, disclosure_bundle_id: 'b'.repeat(64) });
    if (change === 'owner-document') f.tabs[1].documentId = 'reloaded-document';
    if (change === 'owner-url') f.tabs[1].url = `${routes.bridge}#journey=${journey}`;
    await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /workspace_exists/);
    assert.deepEqual(f.calls, []);
  });
}
test('exact callback document and closed route are required, not localhost or a journey claim alone', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  for (const sender of [{ ...f.sender(3), frameId: 1 }, { ...f.sender(3), id: 'other-extension' },
    { ...f.sender(3), documentId: 'old-document' }, { ...f.sender(3), url: `${routes.provisioning}#journey=${journey}` },
    { ...f.sender(3), url: `${routes.bridge}provisioning/native-return?token=bad#journey=${journey}` }]) {
    await assert.rejects(f.workspace.returnFromNative(sender, f.request), /return_unavailable/);
  }
  for (const request of [{ ...f.request, route: 'hosted' }, { ...f.request, route: 'https://evil.test/' },
    { ...f.request, cookie: 'forbidden' }, { ...f.request, journey_id: crypto.randomUUID() }]) {
    await assert.rejects(f.workspace.returnFromNative(f.sender(3), request), /return_unavailable/);
  }
  assert.deepEqual(f.calls, []);
});
for (const background of ['tab', 'window']) {
  test(`background ${background} completion returns the workspace without stealing focus`, async () => {
    const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
    if (background === 'tab') f.tabs[2].active = false;
    else f.chromeApi.windows.get = async () => ({ focused: false });
    await f.workspace.returnFromNative(f.sender(3), f.request);
    assert.deepEqual(f.calls, [{ id: 2, url: `${routes.provisioning}#journey=${journey}` }]);
    assert.deepEqual(f.windowCalls, []);
  });
}
test('lost navigation reply reconciles committed destination after worker restart without replay', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  const dispatch = f.port.postMessage;
  f.port.postMessage = (...args) => { dispatch(...args); throw Error('lost reply'); };
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /return_unavailable/);
  assert.deepEqual(await f.make().returnFromNative(f.sender(3), f.request), { status: 'returned' });
  assert.equal(f.calls.length, 1);
});
test('unknown uncommitted navigation is never replayed or replaced by a fresh intent', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  let attempts = 0;
  f.port.postMessage = () => { attempts++; throw Error('unknown'); };
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /return_unavailable/);
  await assert.rejects(f.make().returnFromNative(f.sender(3), f.request), /return_unavailable/);
  await assert.rejects(f.workspace.prepareNativeLaunch(f.sender(2)), /workspace_return_unconfirmed/);
  assert.equal(attempts, 1);
});
test('accepted receipt cannot be reused by another local return document', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  await f.workspace.returnFromNative(f.sender(3), f.request);
  f.tabs[2].documentId = 'different-document';
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /workspace_exists/);
  assert.equal(f.calls.filter((call) => call.url).length, 1);
});

test('owner navigation during an asynchronous return check is never overwritten', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  f.chromeApi.windows.get = async () => { f.tabs[1].url = 'https://onlyfans.com/my/chats';
    f.tabs[1].documentId = 'user-replacement'; return { focused: true }; };
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /workspace_exists/);
  assert.deepEqual(f.calls, []); assert.equal(f.tabs[1].documentId, 'user-replacement');
});

test('navigation return waits for actual document commit without polling or another dispatch', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  const dispatch = f.port.postMessage;
  f.port.postMessage = (message) => setTimeout(() => dispatch(message), 5);
  const result = f.workspace.returnFromNative(f.sender(3), f.request);
  await Promise.resolve(); assert.equal(f.tabs[1].documentId, 'owner-document');
  assert.deepEqual(await result, { status: 'returned' });
  assert.equal(f.calls.filter((call) => call.url).length, 1);
});

test('late committed navigation is reconciled read-only after timeout', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  let command; const dispatch = f.port.postMessage;
  f.port.postMessage = (message) => { command = message; };
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /return_unavailable/);
  dispatch(command); await Promise.resolve();
  assert.deepEqual(await f.make().returnFromNative(f.sender(3), f.request), { status: 'returned' });
  assert.equal(f.calls.filter((call) => call.url).length, 1);
});

test('manual native entry discovers only the current pending locator without consuming or extending intent', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
  const before = structuredClone(f.session[NATIVE_LAUNCH_KEY]);
  assert.deepEqual(await f.workspace.discoverNativeLaunch(f.sender(3)), { status: 'pending_launch', journey_id: journey });
  assert.deepEqual(f.session[NATIVE_LAUNCH_KEY], before); assert.deepEqual(f.calls, []);
});

test('manual bare callback returns only its armed journey without needing a fragment or a new document', async () => {
  const f = fixture(); await f.open(); f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), f.request), /workspace_exists/);
  await f.workspace.prepareNativeLaunch(f.sender(2));
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), { ...f.request, journey_id: crypto.randomUUID() }), /workspace_exists/);
  await assert.rejects(f.workspace.returnFromNative({ ...f.sender(3), documentId: 'old' }, f.request), /return_unavailable/);
  await assert.rejects(f.workspace.returnFromNative(f.sender(3), { ...f.request, extra: true }), /return_unavailable/);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(await f.workspace.returnFromNative(f.sender(3), f.request), { status: 'returned' });
  assert.equal(f.calls.filter((value) => value.url).length, 1);
});
test('manual discovery refuses a fragment, query, iframe, stale document or absent scoped intent', async () => {
  const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
  await assert.rejects(f.workspace.discoverNativeLaunch(f.sender(3)), /return_unavailable/);
  f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
  for (const sender of [{ ...f.sender(3), frameId: 1 }, { ...f.sender(3), documentId: 'old' },
    { ...f.sender(3), url: `${f.tabs[2].url}?journey=${journey}` }]) {
    await assert.rejects(f.workspace.discoverNativeLaunch(sender), /return_unavailable/);
  }
  delete f.session[NATIVE_LAUNCH_KEY];
  await assert.rejects(f.workspace.discoverNativeLaunch(f.sender(3)), /workspace_exists/);
  assert.deepEqual(f.calls, []);
});
for (const change of ['expired', 'scope', 'document', 'returning', 'returned']) {
  test(`manual discovery cannot adopt ${change} intent`, async () => {
    const f = fixture(); await f.open(); await f.workspace.prepareNativeLaunch(f.sender(2));
    f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
    if (change === 'expired') f.clock[0] += NATIVE_LAUNCH_TTL_MS;
    if (change === 'scope') await f.workspace.refreshScope({ ...scope, scope_id: crypto.randomUUID() });
    if (change === 'document') f.tabs[1].documentId = 'new-document';
    if (['returning', 'returned'].includes(change)) f.session[NATIVE_LAUNCH_KEY].phase = change;
    await assert.rejects(f.workspace.discoverNativeLaunch(f.sender(3)), /workspace_exists/);
    assert.deepEqual(f.calls, []);
  });
}
test('manual discovery only permits ordinary entry when no registered workspace actually remains', async () => {
  const f = fixture(); await f.open();
  f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
  f.tabs.splice(1, 1);
  await assert.rejects(f.workspace.discoverNativeLaunch(f.sender(3)), /no_workspace/);
});
test('explicit native recovery focuses existing workspace without navigation, intent or readiness', async () => {
  const f = fixture(); await f.open();
  assert.deepEqual(await f.workspace.focusFromNative(f.sender(3)), { status: 'focused' });
  assert.deepEqual(f.calls, [{ id: 2, active: true }]);
  assert.equal(f.session[NATIVE_LAUNCH_KEY], undefined);
  await assert.rejects(f.workspace.focusFromNative({ ...f.sender(3), url: 'https://evil.test/' }), /return_unavailable/);
  f.tabs.splice(1, 1);
  await assert.rejects(f.workspace.focusFromNative(f.sender(3)), /no_workspace/);
});
