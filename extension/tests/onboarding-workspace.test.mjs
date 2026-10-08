import test from 'node:test';
import assert from 'node:assert/strict';
import { createOnboardingWorkspace, WORKSPACE_RECORD_KEY } from '../runtime/onboarding-workspace.mjs';

const journey = 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed';
const scope = { scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64) };
const nextScope = { ...scope, scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46feb' };
const checked = { terms_checked: true, risk_checked: true, full_checked: true };
const routes = { extension: 'chrome-extension://synthetic/setup.html', hosted: 'https://onboarding.example.test/setup',
  provisioning: 'http://bridge.localhost:17871/provisioning/', bridge: 'http://bridge.localhost:17871/' };
function fixture(initialTabs = []) {
  const values = {}, session = {}, calls = [], tabs = initialTabs.map((tab) => ({ ...tab }));
  const storage = (data) => ({ async get(key) { return structuredClone({ [key]: data[key] }); },
    async set(record) { Object.assign(data, structuredClone(record)); } });
  const chromeApi = { runtime: { id: 'synthetic', getURL: (path) => `chrome-extension://synthetic/${path}` },
    storage: { local: storage(values), session: storage(session) },
    tabs: { async query() { return structuredClone(tabs); },
      async get(id) { const tab = tabs.find((entry) => entry.id === id); if (!tab) throw Error('closed'); return { ...tab }; },
      async create(options) { calls.push(['create', options]); const tab = { id: tabs.length + 10, windowId: 1, ...options }; tabs.push(tab); return { ...tab }; },
      async update(id, options) { calls.push(['update', id, options]); const tab = tabs.find((entry) => entry.id === id); Object.assign(tab, options); return { ...tab }; } },
    windows: { async update(id, options) { calls.push(['window', id, options]); } } };
  const workspace = createOnboardingWorkspace({ chromeApi, routes });
  const open = () => workspace.open({ journey_id: journey, route: 'extension', explicit: true, draft_scope: scope });
  const sender = () => { const tab = tabs.find((entry) => entry.url.endsWith(journey));
    return { frameId: 0, tab: { ...tab }, url: tab.url,
      ...(tab.url.startsWith('chrome-extension:') ? { id: 'synthetic' } : {}) }; };
  return { chromeApi, workspace, values, session, calls, tabs, sender, open };
}
test('concurrent explicit opens adopt the trusted existing journey and never navigate a user tab', async () => {
  const f = fixture([{ id: 1, windowId: 1, url: 'https://onlyfans.com/my/chats' },
    { id: 2, windowId: 1, url: `${routes.bridge}#journey=${journey}` }]);
  const opened = await Promise.all([f.open(), f.open()]);
  assert.deepEqual(opened.map((item) => item.tab_id), [2, 2]);
  assert.equal(f.tabs.length, 2);
  assert.equal(f.calls.some(([type, id, options]) => type === 'update' && (id === 1 || options.url)), false);
  await f.workspace.navigate(f.sender(), { route: 'extension' });
  assert.equal(f.tabs[1].url, `${routes.extension}#journey=${journey}`);
});
test('draft survives worker restart and tab close; has no authority fields or effects', async () => {
  const f = fixture(); await f.open();
  await f.workspace.saveDraft(f.sender(), { scope_id: scope.scope_id, draft: checked });
  const previousCalls = f.calls.length;
  const restored = createOnboardingWorkspace({ chromeApi: f.chromeApi, routes });
  assert.deepEqual((await restored.read()).draft, checked);
  assert.equal(f.calls.length, previousCalls, 'read or draft updates cannot steal focus');
  assert.deepEqual(Object.keys(f.values[WORKSPACE_RECORD_KEY]).sort(), ['draft', 'draft_scope', 'journey_id', 'route', 'version']);
  f.tabs.splice(0); Object.keys(f.session).forEach((key) => delete f.session[key]);
  await restored.open({ journey_id: journey, route: 'extension', explicit: true, draft_scope: scope });
  assert.deepEqual((await restored.read()).draft, checked);
  assert.equal(f.tabs.length, 1);
});
test('navigation uses registered route IDs and rejects extra URL/token fields and stale senders', async () => {
  const f = fixture(); await f.open(); const sender = f.sender();
  for (const request of [{ route: 'https://evil.test/' }, { route: 'bridge', url: 'https://evil.test/' },
    { route: 'bridge', token: 'secret' }]) await assert.rejects(f.workspace.navigate(sender, request));
  await assert.rejects(f.workspace.saveDraft(sender, { scope_id: scope.scope_id, draft: { ...checked, consent_granted: true } }));
  await assert.rejects(f.workspace.navigate({ ...sender, frameId: 1 }, { route: 'bridge' }));
  f.tabs[0].url = 'https://onlyfans.com/my/chats';
  await assert.rejects(f.workspace.navigate(sender, { route: 'bridge' }), /workspace_sender_stale/);
  await f.open(); assert.equal(f.tabs.length, 2);
  assert.equal(f.tabs[0].url, 'https://onlyfans.com/my/chats');
});
test('draft scope fences stale writes and clears Full choice on creator change, all choices on disclosure change', async () => {
  const f = fixture(); await f.open(); const sender = f.sender();
  await f.workspace.saveDraft(sender, { scope_id: scope.scope_id, draft: checked });
  await f.workspace.resetDraftScope(sender, { expected_scope_id: scope.scope_id, draft_scope: nextScope });
  assert.deepEqual((await f.workspace.read()).draft, { ...checked, full_checked: false });
  await assert.rejects(f.workspace.saveDraft(sender, { scope_id: scope.scope_id, draft: checked }), /workspace_draft_stale/);
  await assert.rejects(f.workspace.resetDraftScope(sender, { expected_scope_id: scope.scope_id, draft_scope: nextScope }), /workspace_draft_stale/);
  await f.workspace.resetDraftScope(sender, { expected_scope_id: nextScope.scope_id,
    draft_scope: { ...scope, disclosure_bundle_id: 'b'.repeat(64) } });
  assert.deepEqual((await f.workspace.read()).draft, { terms_checked: false, risk_checked: false, full_checked: false });
});
test('only the exact registered path and canonical opaque reference can be adopted', async () => {
  const badUrls = [
    `https://onboarding.example.test/unregistered#journey=${journey}`,
    `${routes.bridge}?secret=value#journey=${journey}`, `${routes.bridge}#journey=${journey}&token=x`,
    `${routes.bridge}#journey=${journey.toUpperCase()}`, `https://evil.test/#journey=${journey}`,
  ];
  for (const url of badUrls) { const f = fixture([{ id: 1, windowId: 1, url }]); await f.open(); assert.equal(f.tabs.length, 2); }
  assert.throws(() => createOnboardingWorkspace({ chromeApi: fixture().chromeApi,
    routes: { ...routes, hosted: 'https://user:password@onboarding.example.test/setup' } }));
});
test('installation resumes only the existing setup document without changing focus', async () => {
  const f = fixture([{ id: 1, windowId: 1, url: 'https://onlyfans.com/my/chats', active: true },
    { id: 2, windowId: 1, url: `${routes.bridge}#journey=${journey}`, active: false }]);
  await f.workspace.resumeExisting({ draft_scope: scope, route: 'extension' });
  assert.equal(f.tabs.length, 2);
  assert.equal(f.tabs[1].url, `${routes.extension}#journey=${journey}`);
  assert.equal(f.tabs[1].active, false);
  assert.deepEqual(f.calls, [['update', 2, { url: `${routes.extension}#journey=${journey}` }]]);
});
test('packaged document contexts restore ownership when Chrome withholds tabs.get URL', async () => {
  const f = fixture(); await f.open(); const sender = { ...f.sender(), documentId: 'current-document' };
  const actualGet = f.chromeApi.tabs.get;
  f.chromeApi.tabs.get = async (id) => { const tab = await actualGet(id); delete tab.url; return tab; };
  f.chromeApi.runtime.getContexts = async () => [{ tabId: sender.tab.id, windowId: 1, frameId: 0,
    documentId: 'current-document', documentUrl: sender.url }];
  await f.workspace.saveDraft(sender, { scope_id: scope.scope_id, draft: checked });
  await assert.rejects(f.workspace.saveDraft({ ...sender, documentId: 'old-document' },
    { scope_id: scope.scope_id, draft: checked }), /workspace_sender_stale/);
});
