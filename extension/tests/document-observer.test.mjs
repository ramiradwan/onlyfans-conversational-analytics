import test from 'node:test';
import assert from 'node:assert/strict';
import { DocumentObserverCoordinator, OBSERVER_HELPER_KEY, OBSERVER_STATE_TYPE } from '../runtime/document-observer.mjs';
const event = () => { const listeners = []; return { addListener(fn) { listeners.push(fn); }, emit(...args) { for (const fn of listeners) fn(...args); } }; };
function fixture({ silent = false, tabs = [{ id: 7, url: 'https://onlyfans.com/my/chats', frozen: false }] } = {}) {
  const stored = {}, session = {}, calls = [], documents = new Map(); let scans = 0;
  const browserTabs = new Map(tabs.map((value) => [value.id, { ...value }]));
  const api = { runtime: { id: 'fixture' }, storage: { local: {
    async get(keys) { return Object.fromEntries(keys.map((key) => [key, structuredClone(stored[key])])); },
    async set(values) { Object.assign(stored, structuredClone(values)); },
  }, session: { async get(keys) { return Object.fromEntries(keys.map((key) => [key, session[key]])); },
    async set(values) { Object.assign(session, values); } } }, tabs: { onUpdated: event(), onRemoved: event(), onReplaced: event(), onActivated: event(),
    async query() { scans++; return [...browserTabs.values()].map((value) => ({ ...value })); },
    async sendMessage(id, value) { calls.push(['message', id, value.action]);
      if (value.action !== 'status') return { ok: true };
      return silent ? null : { mode: documents.get(id)?.mode ?? 'full', active: true, forwarding: true, ws2_socket_open: true }; },
    async create(options) { calls.push(['create', options]); const tab = { id: 12, ...options }; browserTabs.set(12, tab); return { ...tab }; },
    async update(id, values) { calls.push(['update', id, values]); Object.assign(browserTabs.get(id), values); },
    async reload() { assert.fail('product reload'); },
  }, scripting: { async executeScript(request) {
    calls.push(['inject', request.target.tabId, request.world]);
    if (request.world === 'MAIN') documents.set(request.target.tabId, { mode: request.files[0].match(/mode-(\w+)/u)[1] });
    return [{ frameId: 0, documentId: `document-${request.target.tabId}` }];
  } } };
  const observer = new DocumentObserverCoordinator({ chromeApi: api });
  return { api, observer, calls, stored, session, scans: () => scans, browserTabs };
}
test('attaches to an existing user document without navigation; reads cached status', async () => {
  const f = fixture(); await f.observer.configure('preview');
  assert.equal(f.observer.snapshot().attached, true);
  for (let i = 0; i < 30; i++) f.observer.snapshot();
  assert.equal(f.scans(), 1);
  assert.equal(f.calls.some(([type]) => ['create', 'update'].includes(type)), false);
  await f.observer.configure('full');
  assert.equal(f.observer.snapshot().tabs[0].status.mode, 'full');
  assert.equal(f.calls.some(([type]) => type === 'update'), false);
});
test('silent documents permit only one owned inactive initial helper navigation', async () => {
  const f = fixture({ silent: true });
  await Promise.all([f.observer.configure('full'), f.observer.configure('full')]);
  await f.observer.configure('full');
  assert.equal(f.calls.filter(([type]) => type === 'create').length, 1);
  assert.deepEqual(f.calls.find(([type]) => type === 'create')[1], { url: 'about:blank', active: false });
  assert.deepEqual(f.calls.filter(([type]) => type === 'update'), [['update', 12, { url: 'https://onlyfans.com/' }]]);
  assert.equal(f.browserTabs.get(7).url, 'https://onlyfans.com/my/chats');
});
test('helper closure survives worker restart and only an explicit request reopens it', async () => {
  const f = fixture({ silent: true }); await f.observer.configure('preview');
  f.browserTabs.delete(12); f.api.tabs.onRemoved.emit(12); await Promise.resolve();
  assert.equal(f.stored[OBSERVER_HELPER_KEY].closed, true);
  const next = new DocumentObserverCoordinator({ chromeApi: f.api }); await next.configure('preview');
  assert.equal(next.snapshot().helper, 'closed');
  assert.equal(f.calls.filter(([type]) => type === 'create').length, 1);
  await next.reopen();
  assert.equal(f.calls.filter(([type]) => type === 'create').length, 2);
});
test('stale document and foreign state pushes cannot claim observer readiness', async () => {
  const f = fixture(); await f.observer.configure('full');
  const sender = { id: 'fixture', frameId: 0, url: 'https://onlyfans.com/my/chats', tab: { id: 7 }, documentId: 'document-7' };
  const message = { type: OBSERVER_STATE_TYPE, status: { mode: 'full', active: true, forwarding: true, ws2_socket_open: false } };
  assert.equal(f.observer.observe(message, { ...sender, documentId: 'old-document' }), false);
  assert.equal(f.observer.observe(message, { ...sender, id: 'foreign' }), false);
  assert.equal(f.observer.observe(message, sender), true);
  f.api.tabs.onUpdated.emit(7, { status: 'loading' }, { id: 7, url: sender.url });
  assert.equal(f.observer.observe(message, sender), false);
  assert.equal(f.observer.snapshot().attached, false);
});
test('pause never opens a helper, and unconfirmed pause tears down its old bridge', async () => {
  const f = fixture(); await f.observer.configure('preview');
  f.api.tabs.sendMessage = async (id, value) => { f.calls.push(['message', id, value.action]); return null; };
  await f.observer.configure('preview', true);
  assert.ok(f.calls.some(([type, , action]) => type === 'message' && action === 'stop'));
  assert.equal(f.calls.some(([type]) => type === 'create'), false);
});
test('an invalidated attachment cannot make its newer generation ready', async () => {
  const f = fixture(); let finish;
  f.api.scripting.executeScript = () => new Promise((resolve) => { finish = resolve; });
  const pending = f.observer.configure('full');
  while (!finish) await new Promise(setImmediate);
  f.observer.invalidate(); finish([{ frameId: 0, documentId: 'stale-document' }]);
  await pending;
  assert.equal(f.observer.snapshot().attached, false);
  assert.equal(f.calls.some(([type]) => type === 'create'), false);
});

test('explicit helper reopen fails when there is no closed active helper', async () => {
  const f = fixture(); await assert.rejects(f.observer.reopen(), /helper_not_ready/);
  await f.observer.configure('preview'); await assert.rejects(f.observer.reopen(), /helper_not_ready/);
  assert.equal(f.calls.some(([type]) => type === 'create'), false);
});

test('a socket-close push attempts at most one helper without probing or navigating user tabs', async () => {
  const f = fixture(); await f.observer.configure('preview');
  const message = { type: OBSERVER_STATE_TYPE, status: { mode: 'preview', active: true, forwarding: true, ws2_socket_open: false } };
  const sender = { id: 'fixture', frameId: 0, tab: { id: 7 }, url: 'https://onlyfans.com/my/chats', documentId: 'document-7' };
  f.observer.observe(message, sender); f.observer.observe(message, sender); await f.observer.queue;
  assert.equal(f.calls.filter(([type]) => type === 'create').length, 1);
  assert.equal(f.scans(), 1);
  assert.equal(f.browserTabs.get(7).url, 'https://onlyfans.com/my/chats');
});

test('window shutdown suspends helper creation but permits recovery on browser restart', async () => {
  const f = fixture({ silent: true }); await f.observer.configure('preview');
  f.browserTabs.delete(12); f.api.tabs.onRemoved.emit(12, { isWindowClosing: true });
  assert.equal(f.observer.snapshot().helper, 'none');
  await f.observer.openHelper(f.observer.generation);
  assert.equal(f.calls.filter(([kind]) => kind === 'create').length, 1);
  for (const key of Object.keys(f.session)) delete f.session[key];
  const restarted = new DocumentObserverCoordinator({ chromeApi: f.api });
  await restarted.configure('preview');
  assert.equal(f.calls.filter(([kind]) => kind === 'create').length, 2);
  assert.equal(f.stored[OBSERVER_HELPER_KEY].closed, false);
});
test('a missing helper is lifecycle loss, while legacy explicit/unknown closures remain respected', async () => {
  for (const closed of [false, true]) {
    const f = fixture({ silent: true, tabs: [] });
    f.stored.onboarding_observer_helper_v1 = { version: 1, tab_id: closed ? null : 90,
      initial_url: 'https://onlyfans.com/', closed, touched: false };
    await f.observer.configure('preview');
    assert.equal(f.calls.filter(([kind]) => kind === 'create').length, closed ? 0 : 1);
    assert.equal(f.stored[OBSERVER_HELPER_KEY].version, 2);
    assert.equal(f.observer.snapshot().helper, closed ? 'closed' : 'open');
  }
});
test('a restored usable user tab is attached, never reclaimed as a helper by URL or reused ID', async () => {
  const f = fixture({ tabs: [{ id: 12, url: 'https://onlyfans.com/' }] });
  f.stored[OBSERVER_HELPER_KEY] = { version: 2, tab_id: 12, owner_session: 'previous-browser',
    initial_url: 'https://onlyfans.com/', closed: false, touched: false, closure: 'none' };
  await f.observer.configure('preview');
  assert.equal(f.observer.snapshot().attached, true);
  assert.equal(f.observer.snapshot().helper, 'none');
  f.api.tabs.onRemoved.emit(12, { isWindowClosing: false });
  assert.equal(f.stored[OBSERVER_HELPER_KEY].closed, false);
  assert.equal(f.calls.some(([kind]) => ['create', 'update'].includes(kind)), false);
});
test('worker restart in the same browser keeps ownership and creates no duplicate helper', async () => {
  const f = fixture({ silent: true }); await f.observer.configure('preview');
  await new DocumentObserverCoordinator({ chromeApi: f.api }).configure('preview');
  assert.equal(f.calls.filter(([kind]) => kind === 'create').length, 1);
});
test('replacement is discovered without stop intent, new navigation or stale readiness', async () => {
  const f = fixture({ silent: true }); await f.observer.configure('preview');
  f.browserTabs.delete(12); f.browserTabs.set(33, { id: 33, url: 'https://onlyfans.com/' });
  f.api.tabs.onReplaced.emit(33, 12); await f.observer.queue;
  assert.equal(f.stored[OBSERVER_HELPER_KEY].closed, false);
  assert.equal(f.observer.snapshot().helper, 'none');
  assert.ok(f.calls.some(([kind, id]) => kind === 'inject' && id === 33));
  assert.equal(f.calls.filter(([kind]) => kind === 'create').length, 1);
});
test('lifecycle recovery never creates a helper while paused or without an observation phase', async () => {
  for (const [mode, paused] of [[null, false], ['preview', true]]) {
    const f = fixture({ silent: true, tabs: [] });
    f.stored[OBSERVER_HELPER_KEY] = { version: 2, tab_id: 90, owner_session: 'old',
      initial_url: 'https://onlyfans.com/', closed: false };
    await f.observer.configure(mode, paused);
    assert.equal(f.calls.some(([kind]) => kind === 'create'), false);
  }
});
