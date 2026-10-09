import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { createOnboardingWorkspace, DESKTOP_HANDOFF_KEY } from '../runtime/onboarding-workspace.mjs';

for (const entry of ['background.js', 'background-read-only.js']) test(`${entry} forwards the desktop document navigation callback`, async () => {
  const source = await readFile(new URL(`../${entry}`, import.meta.url), 'utf8');
  const callback = source.match(/openStep: ([\s\S]*?),\s*onPaired:/)?.[1];
  assert.ok(callback, 'the packaged entry must wire the desktop port');
  const opened = [];
  const open = Function('openSurface', 'openCreatorAccount', `return (${callback});`)(
    (request) => opened.push(request), () => {});
  const anchorTab = { id: 7, windowId: 1, documentId: 'current', url: 'http://bridge.localhost:17871/' };
  const navigate = () => {};
  for (const step of ['setup', 'access']) {
    await open(step, { anchorTab, navigate });
    assert.equal(opened.at(-1).anchorTab, anchorTab);
    assert.equal(opened.at(-1).navigate, navigate);
  }
});

const journey = '11111111-1111-4111-8111-111111111111';
const scope = { scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) };
const routes = { extension: 'chrome-extension://synthetic/setup.html', bridge: 'http://bridge.localhost:17871/',
  provisioning: 'http://bridge.localhost:17871/provisioning', hosted: null };
const event = () => { const listeners = new Set(); return { addListener: (fn) => listeners.add(fn),
  removeListener: (fn) => listeners.delete(fn), emit: (...args) => { for (const fn of listeners) fn(...args); } }; };
test('the local browser page can navigate only to the setup resource, which cannot be embedded', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.web_accessible_resources, [{ resources: ['setup.html'], matches: ['http://bridge.localhost/*'] }]);
  assert.match(manifest.content_security_policy.extension_pages, /frame-ancestors 'none';/);
});
async function fixture() {
  const local = {}, session = {}, sent = [], updates = [], changed = event();
  const tab = { id: 7, windowId: 1, documentId: 'bridge-document', url: `${routes.bridge}#journey=${journey}` };
  let clock = 1_000;
  const storage = (data) => ({ async get(key) { return structuredClone({ [key]: data[key] }); },
    async set(value) { Object.assign(data, structuredClone(value)); } });
  const chromeApi = { runtime: { id: 'synthetic', getURL: (file) => `chrome-extension://synthetic/${file}` },
    storage: { local: storage(local), session: storage(session) },
    tabs: { onUpdated: changed, async query() { return [{ ...tab }]; }, async get(id) { if (id !== tab.id) throw Error('closed'); return { ...tab }; },
      async create() { throw Error('no_new_tab'); }, async update(id, value) { updates.push(value); Object.assign(tab, value); } },
    windows: { async update() {} } };
  const workspace = createOnboardingWorkspace({ chromeApi, routes, now: () => clock, navigationTimeoutMs: 30 });
  await workspace.open({ journey_id: journey, route: 'bridge', explicit: true, draft_scope: scope });
  await workspace.reconcileIdentity({ account_digest: 'b'.repeat(64) });
  const current = await workspace.read();
  const sender = () => ({ tab: { id: tab.id, windowId: tab.windowId }, frameId: 0, url: tab.url,
    documentId: tab.documentId, ...(tab.url.startsWith('chrome-extension:') ? { id: 'synthetic' } : {}) });
  const navigate = (route) => { tab.url = `${routes[route]}#journey=${journey}`; tab.documentId = `${route}-document`;
    changed.emit(tab.id, { status: 'complete' }); };
  const begin = () => workspace.handoffFromDesktop(sender(), (request) => { sent.push(request); navigate('extension'); });
  const bindReturn = async () => {
    const port = { sender: sender(), onDisconnect: event(), postMessage(message) {
      if (message.type === 'navigate') { sent.push(message); navigate('bridge'); }
    }, disconnect() { throw Error('unexpected_disconnect'); } };
    await workspace.bindNavigationPort(port);
  };
  return { workspace, tab, session, local, sent, updates, sender, begin, bindReturn, current,
    expire: () => { clock += 1_800_001; } };
}

test('desktop handoff and return preserve the single tab, journey and draft', async () => {
  const f = await fixture();
  const draft = { terms_checked: true, risk_checked: true, full_checked: true };
  await f.workspace.saveDraft(f.sender(), { scope_id: f.current.draft_scope.scope_id, draft });
  await f.begin();
  assert.deepEqual(await f.workspace.desktopHandoffContext(f.sender()), { expires_at: 1_801_000 });
  await f.bindReturn();
  assert.deepEqual(await f.workspace.returnDesktopHandoff(f.sender()), { status: 'returned' });
  const record = await f.workspace.read();
  assert.deepEqual(record.draft, draft); assert.deepEqual(record.draft_scope, f.current.draft_scope);
  assert.equal(record.journey_id, journey); assert.equal(f.tab.id, 7); assert.equal(record.route, 'bridge');
  assert.equal(f.sent.length, 2); assert.equal(f.session[DESKTOP_HANDOFF_KEY].phase, 'returned');
  assert.equal(f.updates.some((value) => value.url), false, 'each admitted document navigates itself');
});
for (const change of ['document', 'url', 'journey', 'other-tab']) test(`forward handoff refuses stale ${change}`, async () => {
  const f = await fixture(), sender = f.sender();
  if (change === 'document') sender.documentId = 'old-document';
  if (change === 'url') sender.url += '&other=true';
  if (change === 'journey') sender.url = `${routes.bridge}#journey=${crypto.randomUUID()}`;
  if (change === 'other-tab') sender.tab.id = 8;
  await assert.rejects(f.workspace.handoffFromDesktop(sender, (request) => f.sent.push(request)));
  assert.equal(f.sent.length, 0);
});
for (const change of ['expired', 'document', 'identity', 'disclosure', 'no-port']) test(`return handoff refuses ${change}`, async () => {
  const f = await fixture(); await f.begin();
  if (change !== 'no-port') await f.bindReturn();
  if (change === 'expired') f.expire();
  if (change === 'document') f.tab.documentId = 'replacement-document';
  if (change === 'identity') await f.workspace.reconcileIdentity({ account_digest: 'c'.repeat(64) });
  if (change === 'disclosure') await f.workspace.refreshScope({ ...f.current.draft_scope, disclosure_bundle_id: 'd'.repeat(64) });
  await assert.rejects(f.workspace.returnDesktopHandoff(f.sender()), /workspace_handoff_unavailable/);
  assert.equal(f.sent.length, 1); assert.equal(f.tab.url, `${routes.extension}#journey=${journey}`);
});
test('an uncertain forward dispatch cannot be replayed or extend its deadline', async () => {
  const f = await fixture();
  await assert.rejects(f.workspace.handoffFromDesktop(f.sender(), (request) => f.sent.push(request)), /workspace_handoff_unconfirmed/);
  const saved = structuredClone(f.session[DESKTOP_HANDOFF_KEY]);
  await assert.rejects(f.workspace.handoffFromDesktop(f.sender(), (request) => f.sent.push(request)), /workspace_handoff_unconfirmed/);
  assert.deepEqual(f.session[DESKTOP_HANDOFF_KEY], saved); assert.equal(f.sent.length, 1);
});
test('a reload restores presentation intent only to the admitted new document with the original deadline', async () => {
  const f = await fixture(); await f.begin();
  const old = f.sender(), deadline = f.session[DESKTOP_HANDOFF_KEY].expires_at;
  f.tab.documentId = 'reloaded-extension-document';
  assert.deepEqual(await f.workspace.desktopHandoffContext(f.sender()), { expires_at: deadline });
  await f.bindReturn();
  await assert.rejects(f.workspace.returnDesktopHandoff(old), /workspace_sender_stale/);
  await f.workspace.returnDesktopHandoff(f.sender());
  assert.equal(f.sent.length, 2); assert.equal(f.session[DESKTOP_HANDOFF_KEY].expires_at, deadline);
});
test('a late forward arrival restores presentation without dispatching navigation again', async () => {
  const f = await fixture();
  await assert.rejects(f.workspace.handoffFromDesktop(f.sender(), (request) => f.sent.push(request)), /workspace_handoff_unconfirmed/);
  const deadline = f.session[DESKTOP_HANDOFF_KEY].expires_at;
  f.tab.url = `${routes.extension}#journey=${journey}`; f.tab.documentId = 'late-extension-document';
  assert.deepEqual(await f.workspace.desktopHandoffContext(f.sender()), { expires_at: deadline });
  assert.equal(f.sent.length, 1); assert.equal(f.session[DESKTOP_HANDOFF_KEY].phase, 'open');
});
