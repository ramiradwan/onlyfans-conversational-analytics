import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { staticFixtures } from './static-fixtures.mjs';
import { onboardingStaticAssets } from './onboarding-static-fixtures.mjs';
import { BROWSER_TASK_STATES, DESKTOP_ONBOARDING_STATES } from './onboarding-surface-states.mjs';
import { SURFACE_STATES, surfaceDocument } from '../../extension/qualification/surface-fixtures.mjs';
import { renderBrowserTask } from '../../frontend/browser-tasks/view.js';
import { renderSetup } from '../../frontend/onboarding/view.js';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const fixtures = await staticFixtures();
const nativeDocument = await readFile(new URL('../../frontend/onboarding/index.html', import.meta.url), 'utf8');
const browserTaskDocument = await readFile(new URL('../../frontend/browser-tasks/index.html', import.meta.url), 'utf8');
const browserTaskViewSource = await readFile(new URL('../../frontend/browser-tasks/view.js', import.meta.url), 'utf8');
const nativeState = (next_task, extra = {}) => ({ connected: true, pending: null, error: null,
  snapshot: { next_task, facts: {}, pending_operations: [], allowed_actions: [], ...extra } });

test('static capture covers extension surfaces, desktop onboarding, and every browser task view state', async () => {
  const extensionFixtures = fixtures.filter((item) => ['popup', 'setup', 'options'].includes(item.surface));
  const onboardingFixtures = fixtures.filter((item) => item.surface === 'desktop-onboarding');
  const browserTaskFixtures = fixtures.filter((item) => item.surface === 'browser-task');

  assert.deepEqual(extensionFixtures.map((item) => item.name).sort(), Object.keys(SURFACE_STATES).sort());
  assert.equal(fixtures.filter((item) => item.surface === 'popup').length, 8);
  assert.equal(fixtures.filter((item) => item.surface === 'setup').length, 31);
  assert.equal(fixtures.filter((item) => item.surface === 'options').length, 4);
  for (const name of ['start_choice', 'software_activation', 'software_activation_ready', 'mode_choice', 'mode_choice_full', 'full_review', 'desktop_app_needed', 'desktop_app_unavailable', 'pairing_compare']) {
    assert.equal(extensionFixtures.find((item) => item.name === name).surface, 'setup');
  }
  for (const name of ['software_activation', 'software_activation_ready']) assert.equal(extensionFixtures.find(item => item.name === name).state.fullIntent, true);
  for (const fixture of extensionFixtures) {
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

  const assets = await onboardingStaticAssets();
  assert(assets['/desktop-onboarding.css'].body.length > 0);
  assert(assets['/browser-task.css'].body.length > 0);

  assert.deepEqual(onboardingFixtures.map((item) => item.name).sort(), Object.keys(DESKTOP_ONBOARDING_STATES).sort());
  assert.equal(onboardingFixtures.length, Object.keys(DESKTOP_ONBOARDING_STATES).length);
  for (const fixture of onboardingFixtures) {
    assert.equal(fixture.urlPath, '/onboarding/index.html');
    assert(fixture.script.length > 0);
    const dom = new JSDOM(nativeDocument);
    try {
      renderSetup(dom.window.document, fixture.state);
      assert(dom.window.document.querySelector('main').dataset.journeyState.startsWith('desktop.'));
      assert(dom.window.document.getElementById('task-title').textContent.trim().length > 0);
    } finally {
      dom.window.close();
    }
  }

  assert.deepEqual(browserTaskFixtures.map((item) => item.name).sort(), Object.keys(BROWSER_TASK_STATES).sort());
  const journeyBlock = browserTaskViewSource.match(/const journeyStates = \{([\s\S]*?)\};/u)?.[1] ?? '';
  const viewStateKeys = [...journeyBlock.matchAll(/(?:'([^']+)'|([a-z_]+))\s*:/gu)].map((match) => match[1] ?? match[2]);
  assert.equal(viewStateKeys.length, 27);
  const coveredViewStates = new Set(browserTaskFixtures.map((fixture) => fixture.state.state));
  for (const viewState of viewStateKeys) {
    assert(coveredViewStates.has(viewState), `Missing browser-task fixture for view.js state: ${viewState}`);
  }
  for (const fixture of browserTaskFixtures) {
    assert.equal(fixture.urlPath, '/browser-tasks/index.html');
    assert(fixture.script.length > 0);
    const dom = new JSDOM(browserTaskDocument);
    try {
      renderBrowserTask(dom.window.document, fixture.state);
      assert(dom.window.document.querySelector('main').dataset.journeyState.startsWith('browser.'));
      assert(['loading', 'auth_idle', 'ready'].includes(fixture.state.state)
        ? dom.window.document.getElementById('task-status').textContent === ''
        : dom.window.document.getElementById('task-status').textContent.trim().length > 0);
    } finally {
      dom.window.close();
    }
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
      assert(document.querySelector('main[data-task="unavailable"] #task-title, main:not([data-task="unavailable"]) #task-status').textContent.trim());
      assert.equal(document.querySelector('#task-status').getAttribute('aria-live'), 'polite');
      assert.equal(document.querySelector('#task-action').disabled, true);
      assert.equal(document.querySelector('#recovery-package'), null);
    }
    renderSetup(document, nativeState('account-connection', {
      pending_operations: [{ operation_id: 'op', action: 'account.connect', state: 'waiting', reason: 'none' }],
      decision_status: { operation_id: 'op', state: 'accepted' },
    }));
    assert.equal(document.querySelector('#task-pending').textContent, 'Connecting your account…');
    for (const id of ['task-action', 'sign-in-options', 'sign-in-switch', 'browser-reopen', 'setup-code-open']) {
      assert.equal(document.getElementById(id).hidden, true, `Accepted setup exposes another action: ${id}`);
    }
    renderSetup(document, nativeState('finish-setup'));
    assert.equal(document.querySelector('#task-pending').hidden, true);
    assert.equal(document.querySelector('[data-step][aria-current="step"]')?.dataset.step, 'finish-setup');
  } finally { dom.window.close(); }
});
