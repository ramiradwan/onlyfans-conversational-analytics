import test from 'node:test';
import assert from 'node:assert/strict';
import { DocumentObserverCoordinator, OBSERVER_HELPER_KEY, OBSERVER_STATE_TYPE } from '../runtime/document-observer.mjs';
const event = () => { const listeners = []; return { addListener(fn) { listeners.push(fn); }, emit(...args) { for (const fn of listeners) fn(...args); } }; };
function fixture({ silent = false, tabs = [{ id: 7, url: 'https://onlyfans.com/my/chats', frozen: false }] } = {}) {
  const stored = {}, calls = [], documents = new Map(); let scans = 0;
  const browserTabs = new Map(tabs.map((value) => [value.id, { ...value }]));
  const api = { runtime: { id: 'fixture' }, storage: { local: {
    async get(keys) { return Object.fromEntries(keys.map((key) => [key, structuredClone(stored[key])])); },
    async set(values) { Object.assign(stored, structuredClone(values)); },
  } }, tabs: { onUpdated: event(), onRemoved: event(), onReplaced: event(), onActivated: event(),
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
  return { api, observer, calls, stored, scans: () => scans, browserTabs };
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
