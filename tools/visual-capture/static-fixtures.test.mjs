import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { staticFixtures } from './static-fixtures.mjs';
import { SURFACE_STATES, surfaceDocument } from '../../extension/qualification/surface-fixtures.mjs';
import { renderSetup } from '../../frontend/onboarding/view.js';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const fixtures = await staticFixtures();
const nativeDocument = await readFile(new URL('../../frontend/onboarding/index.html', import.meta.url), 'utf8');
const nativeState = (next_task, extra = {}) => ({ connected: true, pending: null, error: null,
  snapshot: { next_task, facts: {}, pending_operations: [], allowed_actions: [], ...extra } });

test('static capture covers the existing popup matrix, both disclosure steps, and all setup stages', async () => {
  assert.deepEqual(fixtures.map((item) => item.name).sort(), Object.keys(SURFACE_STATES).sort());
  assert.equal(fixtures.filter((item) => item.surface === 'popup').length, 8);
  assert.equal(fixtures.filter((item) => item.surface === 'setup').length, 31);
  assert.equal(fixtures.filter((item) => item.surface === 'options').length, 4);
  for (const name of ['start_choice', 'software_activation', 'software_activation_ready', 'mode_choice', 'mode_choice_full', 'full_review', 'desktop_app_needed', 'desktop_app_unavailable', 'pairing_compare']) {
    assert.equal(fixtures.find((item) => item.name === name).surface, 'setup');
  }
  for (const name of ['software_activation', 'software_activation_ready']) assert.equal(fixtures.find(item => item.name === name).state.hash, 'full');
  for (const fixture of fixtures) {
    // Extension fixtures load the unchanged production document and its real bundle.
    // Visibility is exercised by the browser, not a separately maintained fake DOM.
    assert.equal(fixture.html, await surfaceDocument(fixture.state));
    assert(fixture.script.length > 0);
    const dom = new JSDOM(fixture.html);
    const doc = dom.window.document;
    assert.equal(doc.querySelectorAll('script').length, 1);
    assert.equal(doc.querySelector('script').getAttribute('src'), `${fixture.surface}.js`);
    if (fixture.surface === 'popup') {
      assert.equal(doc.querySelector('#pre-mode, #mode-choice, #companion-pairing, #delete-local-data'), null);
    }
    dom.window.close();
  }
});

test('native setup renders the authoritative stage and no manual completion check', () => {
  const dom = new JSDOM(nativeDocument);
  try {
    const document = dom.window.document;
    const expected = { 'browser-access': 0, 'account-connection': 1, 'finish-setup': 2, complete: -1 };
    for (const [task, step] of Object.entries(expected)) {
      renderSetup(document, nativeState(task));
      const steps = [...document.querySelectorAll('[data-step]')];
      assert.equal(steps.findIndex(node => node.getAttribute('aria-current') === 'step'), step);
      assert.equal(document.querySelectorAll('[data-step]').length, 3);
    }
    renderSetup(document, { ...nativeState('complete'), continuing: true });
    assert.equal(document.querySelector('#task-status').textContent, 'Opening dashboard…');
    assert.equal(document.querySelector('#task-action').hidden, true);
    assert.equal(document.querySelector('#browser-reopen').hidden, true);
  } finally { dom.window.close(); }
});

test('native recovery and uncertain operations preserve authority and linked feedback', () => {
  const dom = new JSDOM(nativeDocument);
  try {
    const document = dom.window.document;
    renderSetup(document, { snapshot: null, connected: false, pending: null, error: null });
    assert.equal(document.querySelector('main').dataset.journeyState, 'desktop.setup_loading');
    assert.equal(document.querySelector('#task-action').hidden, true);
    const state = nativeState('account-connection', { allowed_actions: ['account.connect'] });
    for (const error of ['status_unavailable', 'session_unavailable', 'operation_unconfirmed']) {
      renderSetup(document, { ...state, connected: false, error });
      assert(document.querySelector('#task-status').textContent.trim());
      assert.equal(document.querySelector('#task-status').getAttribute('aria-live'), 'polite');
      assert.equal(document.querySelector('#task-action').disabled, true);
      assert.equal(document.querySelector('#recovery-package'), null);
    }
    renderSetup(document, nativeState('account-connection', {
      pending_operations: [{ operation_id: 'op', action: 'account.connect', reason: 'none' }],
      decision_status: { operation_id: 'op', state: 'accepted' },
    }));
    assert.equal(document.querySelector('#task-status').textContent, 'Account connection accepted. Finishing setup…');
    for (const id of ['task-action', 'sign-in-options', 'sign-in-switch', 'browser-reopen', 'setup-code-open']) {
      assert.equal(document.getElementById(id).hidden, true, `Accepted setup exposes another action: ${id}`);
    }
  } finally { dom.window.close(); }
});
