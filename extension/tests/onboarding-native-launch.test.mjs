import assert from 'node:assert/strict';
import test from 'node:test';
import { createOnboardingWorkspace, WORKSPACE_RECORD_KEY, WORKSPACE_IDENTITY_KEY } from '../runtime/onboarding-workspace.mjs';
import { NATIVE_LAUNCH_KEY, NATIVE_LAUNCH_TTL_MS, NATIVE_RECOVERY_KEY, workspaceAppLink } from '../runtime/onboarding-native-launch.mjs';

const journey = 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed';
const scope = { scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64) };
const routes = { extension: 'chrome-extension://synthetic/setup.html', hosted: null,
  provisioning: 'http://bridge.localhost:17871/provisioning', bridge: 'http://bridge.localhost:17871/' };
function fixture(routeOverrides = {}) {
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
    scripting: { executeScript: async ({ target, args, func }) => {
      const tab = tabs.find((value) => value.id === target.tabId);
      if (!tab || (target.documentIds && !target.documentIds.includes(tab.documentId))) throw Error('stale document');
      if (args) {
        calls.push({ script: true, target: structuredClone(target), args: [...args], func });
        if (tab.url === args[0]) {
          tab.url = args[1]; tab.documentId = 'committed-document';
          queueMicrotask(() => updated.emit(tab.id, { status: 'complete' }));
        }
      }
      return [{ frameId: 0, documentId: tab.documentId }];
    } },
    tabs: { onUpdated: updated, onActivated: events(), query: async () => structuredClone(tabs), get: async (id) => {
      const tab = tabs.find((value) => value.id === id); if (!tab) throw Error('closed'); return { ...tab };
    }, update: async (id, options) => { calls.push({ id, ...options }); Object.assign(tabs.find((tab) => tab.id === id), options); },
    create: async () => { throw Error('must reuse workspace'); } },
    windows: { onFocusChanged: events(), get: async () => ({ focused: true }), update: async (...args) => { windowCalls.push(args); } } };
  const make = () => createOnboardingWorkspace({ chromeApi, routes: { ...routes, ...routeOverrides }, now: () => clock[0], navigationTimeoutMs: 25 });
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

const entryId = '33333333-3333-4333-8333-333333333333';
const renewedJourney = '44444444-4444-4444-8444-444444444444';
async function recoveryFixture(route = 'provisioning') {
  const f = fixture(); await f.open();
  await f.workspace.reconcileIdentity({ account_digest: 'c'.repeat(64) });
  const record = await f.workspace.read();
  await f.workspace.saveDraft(f.sender(2), { scope_id: record.draft_scope.scope_id,
    draft: { terms_checked: true, risk_checked: true, full_checked: true } });
  f.tabs[1].url = `${routes[route]}#journey=${journey}`;
  f.local[WORKSPACE_RECORD_KEY].route = route;
  const prepare = () => f.workspace.prepareNativeRecovery(f.sender(3), { entry_id: entryId });
  const request = (result) => ({ entry_id: entryId, recovery_id: result.recovery_id,
    previous_journey_id: journey, journey_id: renewedJourney, route: 'provisioning' });
  return { ...f, prepare, recoveryRequest: request };
}

test('renewal reuses the exact local owner document and preserves matching choices and identity', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare();
  assert.equal(prepared.status, 'recovery_ready');
  assert.deepEqual(await f.prepare(), prepared);
  const before = await f.workspace.read();
  const request = f.recoveryRequest(prepared);
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), request), { status: 'returned' });
  const script = f.calls.find((call) => call.script);
  assert.deepEqual(script.target, { tabId: 2, documentIds: ['owner-document'] });
  assert.deepEqual(script.args, [`${routes.provisioning}#journey=${journey}`, `${routes.provisioning}#journey=${renewedJourney}`]);
  assert.deepEqual((await f.workspace.read()).draft, before.draft);
  assert.deepEqual((await f.workspace.read()).draft_scope, before.draft_scope);
  assert.equal(f.local[WORKSPACE_IDENTITY_KEY].journey_id, renewedJourney);
  const calls = f.calls.length;
  assert.deepEqual(await f.make().returnFromNativeRecovery(f.sender(3), request), { status: 'returned' });
  assert.equal(f.calls.length, calls);
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});

test('closed owner continues in the callback itself without creating or closing a tab', async () => {
  const f = await recoveryFixture(); f.tabs.splice(1, 1);
  const prepared = await f.prepare();
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'continued' });
  assert.equal(f.tabs.length, 2);
  assert.equal(f.tabs.find((tab) => tab.id === 3).url, `${routes.provisioning}#journey=${renewedJourney}`);
  assert.equal(f.calls.filter((call) => call.script).length, 1);
  assert.equal(f.calls.filter((call) => call.active).length, 0);
});

test('extension owner receives only the separate scoped recovery navigation command', async () => {
  const f = await recoveryFixture('extension'); const prepared = await f.prepare();
  let command; const original = f.port.postMessage;
  f.port.postMessage = (message) => { command = message; original(message); };
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'returned' });
  assert.equal(command.type, 'recover'); assert.equal(command.previous_journey_id, journey);
  assert.equal(command.journey_id, renewedJourney);
  assert.equal(f.calls.some((call) => call.script), false);
});

for (const change of ['owner-document', 'owner-url', 'owner-closed', 'scope', 'disclosure', 'identity', 'callback-document', 'callback-url', 'competing-owner', 'wrong-entry', 'wrong-recovery', 'wrong-prior', 'runtime-route', 'extra-field', 'expired']) {
  test(`recovery refuses ${change} before navigation`, async () => {
    const f = await recoveryFixture(); const prepared = await f.prepare(); const request = f.recoveryRequest(prepared);
    const sender = f.sender(3);
    if (change === 'owner-document') f.tabs[1].documentId = 'replacement';
    if (change === 'owner-url') f.tabs[1].url = `${routes.bridge}#journey=${journey}`;
    if (change === 'owner-closed') f.tabs.splice(1, 1);
    if (change === 'scope') await f.workspace.refreshScope({ ...scope, scope_id: crypto.randomUUID() });
    if (change === 'disclosure') await f.workspace.refreshScope({ ...scope, disclosure_bundle_id: 'b'.repeat(64) });
    if (change === 'identity') await f.workspace.reconcileIdentity({ account_digest: 'd'.repeat(64) });
    if (change === 'callback-document') f.tabs[2].documentId = 'replacement';
    if (change === 'callback-url') f.tabs[2].url += '&other=true';
    if (change === 'competing-owner') f.tabs.push({ id: 4, windowId: 1, url: `${routes.provisioning}#journey=${crypto.randomUUID()}` });
    if (change === 'wrong-entry') request.entry_id = crypto.randomUUID();
    if (change === 'wrong-recovery') request.recovery_id = crypto.randomUUID();
    if (change === 'wrong-prior') request.previous_journey_id = crypto.randomUUID();
    if (change === 'runtime-route') request.route = 'bridge';
    if (change === 'extra-field') request.url = 'https://example.test/';
    if (change === 'expired') f.clock[0] += 300_000;
    await assert.rejects(f.workspace.returnFromNativeRecovery(sender, request), /return_unavailable|workspace_exists/);
    assert.deepEqual(f.calls, []);
  });
}

test('guarded script never targets a replacement document between check and dispatch', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); const execute = f.chromeApi.scripting.executeScript;
  f.chromeApi.scripting.executeScript = async (options) => {
    if (options.target.documentIds) f.tabs[1].documentId = 'replacement';
    return execute(options);
  };
  await assert.rejects(f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), /return_unavailable/);
  assert.deepEqual(f.calls, []); assert.equal(f.tabs[1].documentId, 'replacement');
});

test('unknown recovery navigation is not redispatched and late commit is reconciled without focus', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); const execute = f.chromeApi.scripting.executeScript;
  let attempts = 0, pending;
  f.chromeApi.scripting.executeScript = async (options) => {
    if (options.target.documentIds) { attempts++; pending = options; throw Error('unknown'); }
    return execute(options);
  };
  const request = f.recoveryRequest(prepared);
  await assert.rejects(f.workspace.returnFromNativeRecovery(f.sender(3), request), /return_unavailable/);
  await assert.rejects(f.make().returnFromNativeRecovery(f.sender(3), request), /return_unavailable/);
  await assert.rejects(f.prepare(), /return_unavailable/);
  assert.equal(attempts, 1);
  await execute(pending);
  assert.deepEqual(await f.make().returnFromNativeRecovery(f.sender(3), request), { status: 'returned' });
  assert.equal(attempts, 1); assert.equal(f.calls.filter((call) => call.active).length, 0);
});

test('a lost injection acknowledgement still observes the one committed destination', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); const execute = f.chromeApi.scripting.executeScript;
  f.chromeApi.scripting.executeScript = async (options) => {
    const result = await execute(options); if (options.target.documentIds) throw Error('lost'); return result;
  };
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'returned' });
  assert.equal(f.calls.filter((call) => call.script).length, 1);
});

test('expected target URL on the old document waits for the actual replacement document', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); const execute = f.chromeApi.scripting.executeScript;
  f.chromeApi.scripting.executeScript = async (options) => {
    if (!options.target.documentIds) return execute(options);
    f.tabs[1].url = options.args[1];
    f.updated.emit(2, { status: 'complete' });
    setTimeout(() => { f.tabs[1].documentId = 'committed-document'; f.updated.emit(2, { status: 'complete' }); }, 5);
    return [];
  };
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'returned' });
  assert.equal((await f.workspace.read()).journey_id, renewedJourney);
});

test('recovery finishing in the background does not steal focus', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); f.tabs[2].active = false;
  await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared));
  assert.equal(f.calls.filter((call) => call.active).length, 0); assert.deepEqual(f.windowCalls, []);
});

for (const event of ['tab', 'window']) test(`a ${event} switch during the owner lookup prevents late recovery focus`, async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare();
  let checkedSource = false; const get = f.chromeApi.tabs.get;
  f.chromeApi.windows.get = async () => { checkedSource = true; return { focused: true }; };
  f.chromeApi.tabs.get = async (id) => {
    if (checkedSource && id === 2) {
      if (event === 'tab') f.chromeApi.tabs.onActivated.emit({ tabId: 1, windowId: 1 });
      else f.chromeApi.windows.onFocusChanged.emit(2);
    }
    return get(id);
  };
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'returned' });
  assert.equal(f.calls.filter((call) => call.active).length, 0); assert.deepEqual(f.windowCalls, []);
});

for (const recovery of [false, true]) test(`a window switch during tab activation prevents window focus, recovery=${recovery}`, async () => {
  const f = await recoveryFixture(recovery ? 'provisioning' : 'extension'); f.tabs[1].windowId = 2;
  const prepared = recovery ? await f.prepare() : await f.workspace.prepareNativeLaunch(f.sender(2));
  const update = f.chromeApi.tabs.update;
  f.chromeApi.tabs.update = async (...args) => { await update(...args); f.chromeApi.windows.onFocusChanged.emit(3); };
  const result = recovery ? await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared))
    : await f.workspace.returnFromNative(f.sender(3), f.request);
  assert.deepEqual(result, { status: 'returned' }); assert.deepEqual(f.windowCalls, []);
});

test('callback replacement during navigation never receives a successful receipt', async () => {
  const f = await recoveryFixture(); const prepared = await f.prepare(); const execute = f.chromeApi.scripting.executeScript;
  f.chromeApi.scripting.executeScript = async (options) => {
    if (options.target.documentIds) f.tabs[2].documentId = 'new-callback';
    return execute(options);
  };
  await assert.rejects(f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), /return_unavailable/);
  assert.equal(f.calls.filter((call) => call.active).length, 0);
  assert.equal(f.session[NATIVE_RECOVERY_KEY].phase, 'returning');
});

test('current first-launch intent remains normal; long installation only focuses the unchanged extension owner', async () => {
  const f = await recoveryFixture('extension'); await f.workspace.prepareNativeLaunch(f.sender(2));
  const before = structuredClone(f.session[NATIVE_LAUNCH_KEY]);
  assert.deepEqual(await f.prepare(), { status: 'launch_pending', journey_id: journey });
  f.clock[0] += NATIVE_LAUNCH_TTL_MS;
  assert.deepEqual(await f.prepare(), { status: 'launch_expired' });
  assert.deepEqual(f.session[NATIVE_LAUNCH_KEY], before);
  assert.equal(f.session[NATIVE_RECOVERY_KEY], undefined);
  assert.deepEqual(f.calls, [{ id: 2, active: true }]);
  await f.workspace.prepareNativeLaunch(f.sender(2));
  assert.deepEqual(await f.prepare(), { status: 'launch_pending', journey_id: journey });
});

test('expired launch fallback cannot target an absent, changed or background owner', async () => {
  for (const change of ['absent', 'document', 'scope', 'background']) {
    const f = await recoveryFixture('extension'); await f.workspace.prepareNativeLaunch(f.sender(2));
    f.clock[0] += NATIVE_LAUNCH_TTL_MS;
    if (change === 'absent') f.tabs.splice(1, 1);
    if (change === 'document') f.tabs[1].documentId = 'replacement';
    if (change === 'scope') await f.workspace.reconcileIdentity({ account_digest: null });
    if (change === 'background') f.tabs[2].active = false;
    const result = await f.prepare();
    assert.equal(result.status, change === 'background' ? 'launch_expired' : 'recovery_ready');
    assert.deepEqual(f.calls, []);
  }
});

for (const expired of [false, true]) test(`saved continuation replaces ordinary launch selection with its exact intent, expired=${expired}`, async () => {
  const f = await recoveryFixture('extension'); await f.workspace.prepareNativeLaunch(f.sender(2));
  if (expired) f.clock[0] += NATIVE_LAUNCH_TTL_MS;
  else assert.deepEqual(await f.prepare(), { status: 'launch_pending', journey_id: journey });
  const prepared = await f.workspace.prepareNativeRecovery(f.sender(3), { entry_id: entryId }, () => true, { savedContinuation: true });
  assert.equal(prepared.status, 'recovery_ready');
  assert.equal(f.session[NATIVE_RECOVERY_KEY].version, 2);
  assert.equal(f.session[NATIVE_RECOVERY_KEY].saved_continuation, true);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryRequest(prepared)), { status: 'returned' });
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});

test('saved continuation reuses the exact hosted document; ordinary recovery does not gain that route', async () => {
  const hosted = 'https://onboarding.example.test/public/onboarding/setup';
  const continuation = 'https://onboarding.example.test/public/onboarding/installation-continuation';
  const f = fixture({ hosted }); await f.open();
  await f.workspace.reconcileIdentity({ account_digest: 'c'.repeat(64) });
  f.tabs[1].url = `${continuation}#journey=${journey}`;
  f.local[WORKSPACE_RECORD_KEY].route = 'hosted';
  await assert.rejects(f.workspace.prepareNativeRecovery(f.sender(3), { entry_id: entryId }), /workspace_exists/);
  const prepared = await f.workspace.prepareNativeRecovery(f.sender(3), { entry_id: entryId }, () => true, { savedContinuation: true });
  const result = await f.workspace.returnFromNativeRecovery(f.sender(3), { entry_id: entryId, recovery_id: prepared.recovery_id,
    previous_journey_id: journey, journey_id: renewedJourney, route: 'provisioning' });
  assert.deepEqual(result, { status: 'returned' });
  const script = f.calls.find((call) => call.script);
  assert.deepEqual(script.target, { tabId: 2, documentIds: ['owner-document'] });
  assert.deepEqual(script.args, [`${continuation}#journey=${journey}`, `${routes.provisioning}#journey=${renewedJourney}`]);
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});


async function savedReattachFixture() {
  const f = await recoveryFixture('extension');
  f.tabs[2].url = `${routes.bridge}provisioning/native-return`;
  const prepared = await f.workspace.prepareNativeRecovery(f.sender(3), { entry_id: entryId }, () => true, { savedContinuation: true });
  const oldSender = f.sender(3);
  const original = structuredClone(f.session[NATIVE_RECOVERY_KEY]);
  f.tabs[2].documentId = 'restored-callback';
  const request = { entry_id: entryId, recovery_id: prepared.recovery_id,
    previous_journey_id: journey, journey_id: renewedJourney };
  return { ...f, oldSender, original, reattach: request, recoveryReturn: { ...request, route: 'provisioning' } };
}

test('saved callback reattachment retains intent identity and deadline, then uses exact replacement document', async () => {
  const f = await savedReattachFixture();
  assert.deepEqual(await f.workspace.reattachNativeRecovery(f.sender(3), f.reattach), { status: 'reattached' });
  assert.deepEqual(f.session[NATIVE_RECOVERY_KEY], { ...f.original, journey_id: renewedJourney,
    source: { ...f.original.source, document_id: 'restored-callback' } });
  assert.deepEqual(f.calls, []);
  await assert.rejects(f.workspace.returnFromNativeRecovery(f.oldSender, f.recoveryReturn), /return_unavailable/);
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryReturn), { status: 'returned' });
  assert.equal(f.calls.filter((call) => call.url).length, 1);
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});

for (const change of ['different-tab', 'url', 'owner-document', 'scope', 'identity', 'expired', 'target', 'entry', 'recovery', 'prior', 'ordinary']) {
  test(`saved callback reattachment refuses ${change}`, async () => {
    const f = await savedReattachFixture(); let id = 3;
    if (change === 'different-tab') { f.tabs.push({ ...f.tabs[2], id: 4 }); id = 4; }
    if (change === 'url') f.tabs[2].url += `#journey=${journey}`;
    if (change === 'owner-document') f.tabs[1].documentId = 'replacement-owner';
    if (change === 'scope') await f.workspace.refreshScope({ ...scope, scope_id: crypto.randomUUID() });
    if (change === 'identity') await f.workspace.reconcileIdentity({ account_digest: null });
    if (change === 'expired') f.clock[0] = f.original.expires_at;
    if (change === 'target') f.session[NATIVE_RECOVERY_KEY].journey_id = crypto.randomUUID();
    if (change === 'entry') f.reattach.entry_id = crypto.randomUUID();
    if (change === 'recovery') f.reattach.recovery_id = crypto.randomUUID();
    if (change === 'prior') f.reattach.previous_journey_id = crypto.randomUUID();
    if (change === 'ordinary') { f.session[NATIVE_RECOVERY_KEY].version = 1; delete f.session[NATIVE_RECOVERY_KEY].saved_continuation; }
    const before = structuredClone(f.session[NATIVE_RECOVERY_KEY]);
    await assert.rejects(f.workspace.reattachNativeRecovery(f.sender(id), f.reattach), /return_unavailable|workspace_exists/);
    assert.deepEqual(f.session[NATIVE_RECOVERY_KEY], before); assert.deepEqual(f.calls, []);
  });
}

for (const change of ['source-document', 'intent-generation', 'owner-document', 'scope']) {
  test(`saved reattachment rechecks ${change} after asynchronous reads`, async () => {
    const f = await savedReattachFixture(); const sender = f.sender(3);
    const get = f.chromeApi.storage.session.get; let reads = 0;
    f.chromeApi.storage.session.get = async (key) => {
      const result = await get(key);
      if (key === NATIVE_RECOVERY_KEY && ++reads === 2) {
        if (change === 'source-document') f.tabs[2].documentId = 'newer-callback';
        if (change === 'intent-generation') f.session[NATIVE_RECOVERY_KEY].recovery_id = crypto.randomUUID();
        if (change === 'owner-document') f.tabs[1].documentId = 'newer-owner';
        if (change === 'scope') f.local[WORKSPACE_RECORD_KEY].draft_scope.scope_id = crypto.randomUUID();
      }
      return result;
    };
    await assert.rejects(f.workspace.reattachNativeRecovery(sender, f.reattach), /return_unavailable|workspace_exists/);
    assert.equal(f.session[NATIVE_RECOVERY_KEY].source.document_id, f.original.source.document_id);
    assert.deepEqual(f.calls, []);
  });
}

test('lost reattachment reply can repeat the same binding without new intent or navigation', async () => {
  const f = await savedReattachFixture();
  await f.workspace.reattachNativeRecovery(f.sender(3), f.reattach);
  const first = structuredClone(f.session[NATIVE_RECOVERY_KEY]);
  f.clock[0]++;
  assert.deepEqual(await f.workspace.reattachNativeRecovery(f.sender(3), f.reattach), { status: 'reattached' });
  assert.deepEqual(f.session[NATIVE_RECOVERY_KEY], first); assert.deepEqual(f.calls, []);
});

for (const phase of ['returning', 'returned']) test(`reattachment after ${phase} only reconciles the exact committed target`, async () => {
  const f = await savedReattachFixture();
  await f.workspace.reattachNativeRecovery(f.sender(3), f.reattach);
  await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryReturn);
  f.session[NATIVE_RECOVERY_KEY].phase = phase;
  f.tabs[2].documentId = 'second-restored-callback';
  const priorCalls = structuredClone(f.calls);
  await f.workspace.reattachNativeRecovery(f.sender(3), f.reattach);
  assert.equal(f.session[NATIVE_RECOVERY_KEY].phase, phase);
  assert.equal(f.session[NATIVE_RECOVERY_KEY].expires_at, f.original.expires_at);
  assert.deepEqual(await f.workspace.returnFromNativeRecovery(f.sender(3), f.recoveryReturn), { status: 'returned' });
  assert.deepEqual(f.calls, priorCalls);
});

test('unknown navigation without a committed target cannot reattach or redispatch', async () => {
  const f = await savedReattachFixture();
  f.session[NATIVE_RECOVERY_KEY].phase = 'returning';
  f.session[NATIVE_RECOVERY_KEY].journey_id = renewedJourney;
  await assert.rejects(f.workspace.reattachNativeRecovery(f.sender(3), f.reattach), /return_unavailable/);
  assert.deepEqual(f.calls, []);
  assert.equal(f.session[NATIVE_RECOVERY_KEY].phase, 'returning');
});


for (const field of ['entry_id', 'recovery_id', 'previous_journey_id', 'journey_id']) {
  test(`reattachment rejects coercible arrays for ${field}`, async () => {
    const f = await savedReattachFixture();
    f.reattach[field] = [f.reattach[field]];
    await assert.rejects(f.workspace.reattachNativeRecovery(f.sender(3), f.reattach), /return_unavailable/);
    assert.deepEqual(f.session[NATIVE_RECOVERY_KEY], f.original);
  });
}

for (const change of ['scope', 'expiry']) test(`reattachment checks ${change} immediately before storing`, async () => {
  const f = await savedReattachFixture(); const get = f.chromeApi.tabs.get;
  let reads = 0; let current = true;
  f.chromeApi.tabs.get = async (id) => {
    const result = await get(id);
    if (id === 3 && ++reads === 4) {
      if (change === 'expiry') f.clock[0] = f.original.expires_at;
      else current = false;
    }
    return result;
  };
  await assert.rejects(f.workspace.reattachNativeRecovery(f.sender(3), f.reattach, () => current), /return_unavailable/);
  assert.deepEqual(f.session[NATIVE_RECOVERY_KEY], f.original); assert.deepEqual(f.calls, []);
});
