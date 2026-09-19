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
  assert.equal(permitted('popup', 'ofca.ui.transition', 'paused'), true);
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
