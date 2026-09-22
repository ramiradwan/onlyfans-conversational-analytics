import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { staticFixtures } from './static-fixtures.mjs';
import { SURFACE_STATES, surfaceDocument } from '../../extension/qualification/surface-fixtures.mjs';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const fixtures = await staticFixtures();

test('static capture covers the existing popup matrix, both disclosure steps, and all setup stages', async () => {
  const extension = fixtures.filter((item) => item.surface !== 'provisioning');
  assert.deepEqual(extension.map((item) => item.name).sort(), Object.keys(SURFACE_STATES).sort());
  assert.equal(fixtures.filter((item) => item.surface === 'popup').length, 6);
  assert.equal(fixtures.filter((item) => item.surface === 'setup').length, 25);
  assert.equal(fixtures.filter((item) => item.surface === 'options').length, 3);
  assert.equal(fixtures.filter((item) => item.surface === 'provisioning').length, 12);
  for (const name of ['software_activation', 'mode_choice', 'mode_choice_full', 'full_review', 'pairing_compare']) {
    assert.equal(fixtures.find((item) => item.name === name).surface, 'setup');
  }
  for (const fixture of extension) {
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

test('setup fixtures use real controller outcomes rather than invented status copy', () => {
  const expected = { connect: 0, confirm: 1, approve: 2, finish: 3, completed: -1 };
  for (const [name, step] of Object.entries(expected)) {
    const dom = new JSDOM(fixtures.find((item) => item.surface === 'provisioning' && item.name === name).html);
    const steps = [...dom.window.document.querySelectorAll('[data-step]')];
    assert.equal(dom.window.document.querySelectorAll('script').length, 0);
    assert.equal(steps.findIndex((node) => node.dataset.state === 'current'), step);
    dom.window.close();
  }
});

test('approval feedback stays local and never implies approval from an attempted check', () => {
  for (const name of ['approval-pending', 'approval-offline']) {
    const dom = new JSDOM(fixtures.find((item) => item.name === name).html);
    const document = dom.window.document;
    const feedback = document.getElementById('provisioning-status');
    assert.equal(feedback.closest('[aria-current="step"]').id, 'binding-step');
    assert.equal(document.getElementById('finalize-provisioning').disabled, true);
    assert.equal(document.getElementById('acquire-association').getAttribute('aria-describedby'), 'provisioning-status');
    assert(feedback.textContent.trim().length > 0);
    assert(!feedback.textContent.includes(';'));
    assert.equal(feedback.dataset.tone, name === 'approval-offline' ? 'error' : 'neutral');
    dom.window.close();
  }
});
