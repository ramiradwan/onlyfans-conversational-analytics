import test from 'node:test';
import assert from 'node:assert/strict';
import { uiSurface, allowsUiMessage, registerSurfaceNavigation, UI_OPEN_SURFACE_MESSAGE_TYPE } from '../runtime/ui-surfaces.mjs';

const runtime = { id: 'synthetic', getURL: (path) => `chrome-extension://synthetic/${path}` };
const sender = (page, extra = {}) => ({ id: runtime.id, url: runtime.getURL(page), frameId: 0, ...extra });
test('only explicit packaged top-level UI documents are admitted', () => {
  for (const page of ['popup', 'setup', 'options']) {
    assert.equal(uiSurface(sender(`${page}.html`), { runtime }), page);
    assert.equal(uiSurface(sender(`${page}.html#connection`), { runtime }), page);
  }
  for (const value of [sender('content.js'), sender('setup.html?proxy=yes'), sender('setup.html.evil'),
    sender('setup.html', { id: 'foreign' }), sender('setup.html', { frameId: 2 }),
    sender('', { url: 'https://onlyfans.com/' }), {}, null]) assert.equal(uiSurface(value, { runtime }), null);
});
test('page command ownership leaves legal review in setup and data management in Options', () => {
  const permitted = (page, type, mode) => allowsUiMessage(sender(`${page}.html`), { type, mode }, { runtime });
  for (const page of ['popup', 'setup', 'options']) {
    assert.equal(permitted(page, 'ofca.ui.status'), true);
    assert.equal(permitted(page, 'ofca.legal-activation.status'), true);
    assert.equal(permitted(page, 'ofca.legal-activation.accept-terms'), page === 'setup');
    assert.equal(permitted(page, 'ofca.ui.delete-local-data'), page === 'options');
    assert.equal(permitted(page, 'ofca.ui.clear-preview'), page === 'options');
    assert.equal(permitted(page, 'ofca.ui.transition', 'revoked'), page === 'options');
  }
  assert.equal(permitted('popup', 'ofca.ui.transition', 'full'), false);
  assert.equal(permitted('popup', 'ofca.ui.transition', 'pause'), true);
  assert.equal(permitted('popup', 'ofca.ui.transition', 'paused'), false);
  assert.equal(permitted('popup', 'ofca.ui.transition', 'resume'), true);
  assert.doesNotThrow(() => permitted('setup', {}));
});
test('concurrent opens reuse the setup tab and explicit section navigation stays same-document', async () => {
  let listener; const contexts = [], updates = [], focused = [];
  const api = { runtime: { ...runtime, onMessage: { addListener(fn) { listener = fn; } }, getContexts: async () => contexts },
    tabs: { async create({ url }) { contexts.push({ documentUrl: url, tabId: 8, windowId: 3 }); },
      async update(id, options) { updates.push({ id, ...options }); } },
    windows: { async update(id, options) { focused.push({ id, ...options }); } } };
  registerSurfaceNavigation(api);
  const open = (surface, section = '') => new Promise((resolve) => {
    assert.equal(listener({ type: UI_OPEN_SURFACE_MESSAGE_TYPE, surface, section }, sender('popup.html'), resolve), true);
  });
  assert.deepEqual(await Promise.all([open('setup'), open('setup')]), [{ ok: true }, { ok: true }]);
  assert.equal(contexts.length, 1); assert.deepEqual(updates, [{ id: 8, active: true }]);
  assert.deepEqual(focused, [{ id: 3, focused: true }]);
  await open('setup', 'full');
  assert.equal(updates.at(-1).url, runtime.getURL('setup.html#full'));
  assert.equal(listener({ type: UI_OPEN_SURFACE_MESSAGE_TYPE, surface: 'setup', section: 'https://other.test' }, sender('popup.html'), () => {}), false);
  assert.equal(listener({ type: UI_OPEN_SURFACE_MESSAGE_TYPE, surface: 'setup', section: '' }, sender('content.js'), () => {}), false);
});

test('the desktop handoff opens a compact window centred on the desktop tab and records it', async () => {
  const { openSurfacePage, DESKTOP_HANDOFF_STORAGE_KEY } = await import('../runtime/ui-surfaces.mjs');
  const created = [], stored = {};
  const api = { runtime: { ...runtime, getContexts: async () => [] },
    storage: { session: { async set(value) { Object.assign(stored, value); } } },
    tabs: { async create() { throw new Error('a tab is not the handoff presentation'); } },
    windows: { async get(id) { assert.equal(id, 4); return { left: 100, top: 50, width: 1480, height: 960 }; },
      async create(options) { created.push(options); } } };
  await openSurfacePage(api, { surface: 'setup', section: 'desktop', presentation: 'window', anchorTab: { id: 12, windowId: 4 } });
  assert.deepEqual(created, [{ url: runtime.getURL('setup.html#desktop'), type: 'popup', focused: true,
    width: 480, height: 760, left: 600, top: 150 }]);
  assert.deepEqual(stored[DESKTOP_HANDOFF_STORAGE_KEY], { tab_id: 12, window_id: 4 });
  await assert.rejects(openSurfacePage(api, { surface: 'setup', section: 'https://other.test', presentation: 'window' }));
  await assert.rejects(openSurfacePage(api, { surface: 'popup', section: '' }));
});

test('the desktop handoff focuses an existing setup page instead of opening another', async () => {
  const { openSurfacePage } = await import('../runtime/ui-surfaces.mjs');
  const updates = [], focused = [];
  const api = { runtime: { ...runtime, getContexts: async () => [{ tabId: 8, windowId: 3, documentUrl: runtime.getURL('setup.html') }] },
    storage: { session: { async set() {} } },
    tabs: { async update(id, options) { updates.push({ id, ...options }); } },
    windows: { async update(id, options) { focused.push({ id, ...options }); }, async create() { throw new Error('duplicate'); } } };
  await openSurfacePage(api, { surface: 'setup', section: 'desktop', presentation: 'window', anchorTab: { id: 12, windowId: 4 } });
  assert.deepEqual(updates, [{ id: 8, active: true, url: runtime.getURL('setup.html#desktop') }]);
  assert.deepEqual(focused, [{ id: 3, focused: true }]);
});
