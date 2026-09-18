import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { staticFixtures } from './static-fixtures.mjs';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const fixtures = await staticFixtures();

test('static capture covers the existing popup matrix, both disclosure steps, and all setup stages', () => {
  assert.equal(fixtures.filter((item) => item.surface === 'popup').length, 26);
  assert.equal(fixtures.filter((item) => item.surface === 'provisioning').length, 12);
  const secondary = { mode_choice_full: 'Not now', full_review: 'Keep Preview' };
  for (const fixture of fixtures) {
    const dom = new JSDOM(fixture.html);
    const doc = dom.window.document;
    assert.equal(doc.querySelectorAll('script').length, 0);
    if (fixture.name === 'mode_choice') {
      assert(!doc.querySelector('#preview-disclosure').classList.contains('hidden'));
      assert(doc.querySelector('#full-disclosure').classList.contains('hidden'));
    }
    if (fixture.name in secondary) {
      assert(doc.querySelector('#preview-disclosure').classList.contains('hidden'));
      assert(!doc.querySelector('#full-disclosure').classList.contains('hidden'));
      assert.equal(doc.querySelector('#full-secondary').textContent, secondary[fixture.name]);
    }
    dom.window.close();
  }
});

test('setup fixtures use real controller outcomes rather than invented status copy', () => {
  const expected = { connect: 0, confirm: 1, approve: 2, finish: 3, completed: -1 };
  for (const [name, step] of Object.entries(expected)) {
    const dom = new JSDOM(fixtures.find((item) => item.surface === 'provisioning' && item.name === name).html);
    const steps = [...dom.window.document.querySelectorAll('[data-step]')];
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
